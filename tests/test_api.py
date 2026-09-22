"""端到端测试:取样→分装→合并的谱系、幂等、冲突与回溯。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import service
from app.db import connect, init_db
from app.main import app, get_conn


@pytest.fixture()
def client():
    mem = connect(":memory:")
    init_db(mem)

    def _override():
        yield mem

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    mem.close()


# ---------------------------------------------------------------- 基础链路

def test_sampling_aliquot_merge_and_idempotency(client):
    # 取样 100mL
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "OP-1", "sample_id": "S0",
        "material": "血清", "amount": 100, "unit": "mL",
        "location": "冷库A-1",
    })
    assert r.status_code == 200, r.text
    assert r.json()["samples_created"] == ["S0"]
    assert r.json()["remaining"] == {"S0": 100.0}

    # 重复操作号:幂等回放原结果
    r2 = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "OP-1", "sample_id": "S0",
        "material": "血清", "amount": 100, "unit": "mL",
        "location": "冷库A-1",
    })
    assert r2.status_code == 200
    body = r2.json()
    assert body["replayed"] is True
    assert body["remaining"] == {"S0": 100.0}

    # 分装 60 + 30 = 90,剩 10
    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-2", "parent_id": "S0",
        "outputs": [
            {"sample_id": "A1", "amount": 60},
            {"sample_id": "A2", "amount": 30},
        ],
        "location": "操作台2",
    })
    assert r.status_code == 200, r.text
    assert r.json()["remaining"]["S0"] == 10.0
    assert set(r.json()["samples_created"]) == {"A1", "A2"}

    # 余量不足:再取 15 失败
    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-3", "parent_id": "S0",
        "outputs": [{"sample_id": "A3", "amount": 15}],
    })
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_balance"
    assert c["op_no"] == "OP-3"
    assert c["sample_id"] == "S0"
    assert c["requested"] == 15.0
    assert c["available"] == 10.0

    # 余量不足的提交没有落库
    r = client.get("/api/v1/operations/OP-3")
    assert r.status_code == 404

    # 合并 A1(40)+ A2(20) -> B1
    r = client.post("/api/v1/operations/merge", json={
        "type": "merge", "op_no": "OP-4",
        "inputs": [
            {"sample_id": "A1", "amount": 40},
            {"sample_id": "A2", "amount": 20},
        ],
        "output_id": "B1", "material": "混合血清",
        "location": "离心区",
    })
    assert r.status_code == 200, r.text
    assert r.json()["remaining"] == {"A1": 20.0, "A2": 10.0, "B1": 60.0}


def test_idempotent_op_no_with_different_payload_conflicts(client):
    client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "OP-X", "sample_id": "S0",
        "material": "M", "amount": 1, "unit": "uL",
    })
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "OP-X", "sample_id": "S0",
        "material": "M", "amount": 2, "unit": "uL",
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "op_no_conflict"


def test_unit_mismatch_and_missing_sample(client):
    client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "U1", "sample_id": "U0",
        "material": "M", "amount": 10, "unit": "mL",
    })
    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "U2", "parent_id": "U0",
        "unit": "g", "outputs": [{"sample_id": "U0a", "amount": 1}],
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "unit_mismatch"

    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "U3", "parent_id": "GHOST",
        "outputs": [{"sample_id": "G1", "amount": 1}],
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"


# ---------------------------------------------------------------- 环路

def _seed_chain(client):
    client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "C1", "sample_id": "C0",
        "material": "M", "amount": 100, "unit": "mL",
    })
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "C2", "parent_id": "C0",
        "outputs": [{"sample_id": "C0a", "amount": 50}],
    })
    # C0a -> C1a
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "C3", "parent_id": "C0a",
        "outputs": [{"sample_id": "C1a", "amount": 20}],
    })


def test_cycle_detected_with_path(client):
    _seed_chain(client)
    # 试图把下游 C1a 合回上游 C0a:C0a 已是 C1a 的上游,新边 C1a->C0a 成环
    r = client.post("/api/v1/operations/merge", json={
        "type": "merge", "op_no": "C4",
        "inputs": [{"sample_id": "C1a", "amount": 5}],
        "output_id": "C0a",
    })
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "cycle_detected"
    assert c["cycle_path"][0] == "C0a"
    assert c["cycle_path"][-1] == "C0a"
    assert "C1a" in c["cycle_path"]
    assert c["location"] is None


def test_merge_downstream_is_fine(client):
    _seed_chain(client)
    # C1a -> C0a 成环被禁;但 C0a 合入 C1a(往下走)合法
    r = client.post("/api/v1/operations/merge", json={
        "type": "merge", "op_no": "C5",
        "inputs": [{"sample_id": "C0a", "amount": 5}],
        "output_id": "C1a",
    })
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 批量

def test_batch_commit_and_atomic_rollback(client):
    body = {
        "batch_id": "B-1",
        "operations": [
            {"type": "sampling", "op_no": "B1", "sample_id": "P0",
             "material": "M", "amount": 10, "unit": "mL"},
            {"type": "aliquot", "op_no": "B2", "parent_id": "P0",
             "outputs": [{"sample_id": "P1", "amount": 4},
                         {"sample_id": "P2", "amount": 4}]},
        ],
    }
    r = client.post("/api/v1/batch", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "committed"

    # 整批中第二条余量不足 -> 全部回滚(取样也不存在)
    bad = {
        "batch_id": "B-2",
        "operations": [
            {"type": "sampling", "op_no": "B3", "sample_id": "Q0",
             "material": "M", "amount": 10, "unit": "mL"},
            {"type": "aliquot", "op_no": "B4", "parent_id": "P0",
             "outputs": [{"sample_id": "Q1", "amount": 99}]},
        ],
    }
    r = client.post("/api/v1/batch", json=bad)
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_balance"
    assert c["batch_index"] == 1
    assert c["op_no"] == "B4"

    r = client.get("/api/v1/samples")
    ids = {i["sample_id"] for i in r.json()["items"]}
    assert "Q0" not in ids  # 回滚

    # 同一批次重复操作号
    r = client.post("/api/v1/batch", json={
        "operations": [
            {"type": "sampling", "op_no": "DUP", "sample_id": "D0",
             "material": "M", "amount": 1, "unit": "mL"},
            {"type": "sampling", "op_no": "DUP", "sample_id": "D1",
             "material": "M", "amount": 1, "unit": "mL"},
        ]
    })
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "duplicate_op_no_in_batch"
    assert c["batch_index"] == 1


def test_batch_replay_is_mixed_and_idempotent(client):
    body = {"operations": [
        {"type": "sampling", "op_no": "R1", "sample_id": "R0",
         "material": "M", "amount": 5, "unit": "mL"},
    ]}
    client.post("/api/v1/batch", json=body)
    r = client.post("/api/v1/batch", json=body)
    assert r.status_code == 200
    assert r.json()["status"] == "replayed"
    assert r.json()["results"][0]["replayed"] is True


def test_batch_validate_dry_run_does_not_write(client):
    client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "V0", "sample_id": "V",
        "material": "M", "amount": 3, "unit": "mL",
    })
    r = client.post("/api/v1/batch/validate", json={"operations": [
        {"type": "aliquot", "op_no": "V1", "parent_id": "V",
         "outputs": [{"sample_id": "V-a", "amount": 2}]},
        {"type": "aliquot", "op_no": "V2", "parent_id": "V",
         "outputs": [{"sample_id": "V-b", "amount": 2}]},  # 剩 1,不足
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is False
    assert data["conflicts"][0]["batch_index"] == 1
    assert data["conflicts"][0]["code"] == "insufficient_balance"

    # dry-run 未写库
    r = client.get("/api/v1/samples")
    assert {i["sample_id"] for i in r.json()["items"]} == {"V"}

    # 合法预演
    r = client.post("/api/v1/batch/validate", json={"operations": [
        {"type": "aliquot", "op_no": "V3", "parent_id": "V",
         "outputs": [{"sample_id": "V-c", "amount": 3}]},
    ]})
    assert r.json()["valid"] is True
    r = client.get("/api/v1/samples")
    assert {i["sample_id"] for i in r.json()["items"]} == {"V"}


# ---------------------------------------------------------------- 回溯与校验

def test_trace_upstream_downstream_and_balance(client):
    # root1/root2 -> merge -> aliquot 链
    client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "T1", "sample_id": "root1",
        "material": "M", "amount": 100, "unit": "mg"})
    client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "T2", "sample_id": "root2",
        "material": "M", "amount": 100, "unit": "mg"})
    client.post("/api/v1/operations/merge", json={
        "type": "merge", "op_no": "T3",
        "inputs": [{"sample_id": "root1", "amount": 30},
                   {"sample_id": "root2", "amount": 20}],
        "output_id": "mix", "material": "M"})
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "T4", "parent_id": "mix",
        "outputs": [{"sample_id": "mix-a", "amount": 10},
                    {"sample_id": "mix-b", "amount": 15}]})

    up = client.get("/api/v1/samples/mix/trace?direction=upstream").json()
    assert set(up["roots"]) == {"root1", "root2"}
    node_ids = {n["sample_id"]: n["depth"] for n in up["nodes"]}
    assert node_ids["mix"] == 0
    assert node_ids["root1"] == 1

    down = client.get("/api/v1/samples/mix/trace?direction=downstream").json()
    assert set(down["leaves"]) == {"mix-a", "mix-b"}

    both = client.get("/api/v1/samples/mix/trace").json()
    assert set(both["roots"]) == {"root1", "root2"}
    assert set(both["leaves"]) == {"mix-a", "mix-b"}
    op_nos = {o["op_no"] for o in both["operations"]}
    assert {"T1", "T2", "T3", "T4"} <= op_nos
    # 边:root1->mix, root2->mix, mix->mix-a, mix->mix-b + 两条取样源边(parent=null)
    assert len(both["edges"]) == 6
    assert all(e["child_id"] in {"root1", "root2", "mix", "mix-a", "mix-b"}
               for e in both["edges"])

    # 余量账本一致
    r = client.get("/api/v1/checks/balances")
    assert r.json()["consistent"] is True
    bal = {b["sample_id"]: b for b in r.json()["samples"]}
    assert bal["root1"]["ledger_remaining"] == 70.0
    assert bal["mix"]["stored_remaining"] == 25.0

    r = client.get("/api/v1/samples/mix/balance")
    assert r.json()["consistent"] is True

    r = client.get("/api/v1/samples/unknown/trace")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"


def test_merge_into_existing_sample(client):
    client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "E1", "sample_id": "E-a",
        "material": "M", "amount": 10, "unit": "mL"})
    client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": "E2", "sample_id": "E-b",
        "material": "M", "amount": 10, "unit": "mL"})
    # 先分出 E-c
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "E3", "parent_id": "E-a",
        "outputs": [{"sample_id": "E-c", "amount": 4}]})
    # E-b 并入已有样品 E-c
    r = client.post("/api/v1/operations/merge", json={
        "type": "merge", "op_no": "E4",
        "inputs": [{"sample_id": "E-b", "amount": 6}],
        "output_id": "E-c"})
    assert r.status_code == 200, r.text
    assert r.json()["remaining"]["E-c"] == 10.0
    assert r.json()["remaining"]["E-b"] == 4.0


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
