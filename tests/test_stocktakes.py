"""盘点工作流端到端测试:快照建批、提交版本校验与差异、确认整批原子调账、
幂等重放/冲突、调整流水纳入账本、并发确认序列化与旧库迁移。"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import stocktakes as st
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


def _sample(client, op_no, sid, amount, unit="mL"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": "血清", "amount": amount, "unit": unit,
    })
    assert r.status_code == 200, r.text


def _create(client, no="STK-1", sids=("S1", "S2"), **extra):
    return client.post("/api/v1/stocktakes", json={
        "stocktake_no": no, "items": [{"sample_id": s} for s in sids],
        **extra,
    })


def _submit(client, no, counts, **extra):
    return client.post(f"/api/v1/stocktakes/{no}/submit", json={
        "items": [
            {"sample_id": sid, "counted_amount": amt} for sid, amt in counts
        ],
        **extra,
    })


def _confirm(client, no, operator="张库管", reason="月度盘点差异调整"):
    return client.post(f"/api/v1/stocktakes/{no}/confirm", json={
        "operator": operator, "reason": reason,
    })


# ---------------------------------------------------------------- 建批 / 快照

def test_create_stocktake_snapshots_remaining_and_version(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)

    r = _create(client, note="9月盘点")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stocktake_no"] == "STK-1"
    assert body["status"] == "open"
    assert body["replayed"] is False
    snap = {i["sample_id"]: i for i in body["items"]}
    assert snap["S1"]["snapshot_remaining"] == 100.0
    assert snap["S1"]["snapshot_version"] == 0
    assert snap["S1"]["unit"] == "mL"
    assert snap["S1"]["version_drift"] is False
    assert snap["S2"]["snapshot_remaining"] == 50.0
    # open 阶段尚无实盘/差异/调整
    for item in body["items"]:
        assert item["counted_amount"] is None
        assert item["difference"] is None
        assert item["delta_applied"] is None


def test_create_stocktake_unknown_sample_conflicts_and_nothing_created(client):
    _sample(client, "OP-1", "S1", 100)
    r = _create(client, no="STK-BAD", sids=("S1", "GHOST"))
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"
    assert client.get("/api/v1/stocktakes/STK-BAD").status_code == 404


def test_create_stocktake_idempotent_replay_and_no_conflict(client):
    _sample(client, "OP-1", "S1", 100)
    body = {
        "stocktake_no": "STK-ID",
        "note": "同号重放",
        "items": [{"sample_id": "S1"}],
    }
    r1 = client.post("/api/v1/stocktakes", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False
    r2 = client.post("/api/v1/stocktakes", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["items"][0]["snapshot_remaining"] == 100.0

    # 同号不同内容 -> 409
    r3 = client.post("/api/v1/stocktakes", json={
        "stocktake_no": "STK-ID",
        "items": [{"sample_id": "S1"}, {"sample_id": "S2"}],
    })
    # S2 不存在:幂等键冲突优先于样品校验
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "stocktake_no_conflict"


# ---------------------------------------------------------------- 提交实盘

def test_submit_counts_and_differences(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _create(client)

    # S1 盘亏 12,S2 盘盈 3.5
    r = _submit(client, "STK-1", [("S1", 88), ("S2", 53.5)], operator="李四")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "submitted"
    assert body["operator"] == "李四"
    assert body["submitted_at"] is not None
    diff = {i["sample_id"]: i for i in body["items"]}
    assert diff["S1"]["counted_amount"] == 88.0
    assert diff["S1"]["difference"] == -12.0
    assert diff["S1"]["submitted_version"] == 0
    assert diff["S2"]["difference"] == 3.5

    # 提交不改库存
    avail = client.get("/api/v1/reservations?sample_id=S1&sample_id=S2").json()
    rem = {x["sample_id"]: x["remaining"] for x in avail["samples"]}
    assert rem == {"S1": 100.0, "S2": 50.0}


def test_submit_zero_count_allowed(client):
    _sample(client, "OP-1", "S1", 10)
    _create(client, no="STK-Z", sids=("S1",))
    r = _submit(client, "STK-Z", [("S1", 0)])
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["counted_amount"] == 0.0
    assert item["difference"] == -10.0


def test_submit_version_conflict_when_balance_changed_after_snapshot(client):
    _sample(client, "OP-1", "S1", 100)
    _create(client, sids=("S1",))

    # 建批之后有人分装,S1 余量 100 -> 60,版本 0 -> 1
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-2", "parent_id": "S1",
        "outputs": [{"sample_id": "A1", "amount": 40}],
    })

    # 查询时已能观察到漂移
    got = client.get("/api/v1/stocktakes/STK-1").json()
    assert got["items"][0]["version_drift"] is True
    assert got["items"][0]["current_remaining"] == 60.0
    assert got["items"][0]["current_version"] == 1

    r = _submit(client, "STK-1", [("S1", 58)])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "stocktake_version_conflict"
    assert c["sample_id"] == "S1"
    assert c["requested"] == 100.0  # 快照量
    assert c["available"] == 60.0   # 当前量

    # 提交失败:批次仍是 open,无实盘结果
    assert client.get("/api/v1/stocktakes/STK-1").json()["status"] == "open"


def test_submit_items_mismatch(client):
    _sample(client, "OP-1", "S1", 10)
    _sample(client, "OP-2", "S2", 10)
    _sample(client, "OP-3", "S3", 10)
    _create(client, sids=("S1", "S2"))

    # 缺少 S2、多出 S3
    r = _submit(client, "STK-1", [("S1", 10), ("S3", 10)])
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "stocktake_items_mismatch"
    assert "S2" in c["message"] and "S3" in c["message"]
    assert client.get("/api/v1/stocktakes/STK-1").json()["status"] == "open"


def test_submit_duplicate_sample_rejected(client):
    _sample(client, "OP-1", "S1", 10)
    _create(client, sids=("S1",))
    r = _submit(client, "STK-1", [("S1", 10), ("S1", 9)])
    assert r.status_code == 422


def test_create_duplicate_sample_rejected(client):
    _sample(client, "OP-1", "S1", 10)
    r = client.post("/api/v1/stocktakes", json={
        "stocktake_no": "STK-DUP",
        "items": [{"sample_id": "S1"}, {"sample_id": "S1"}],
    })
    assert r.status_code == 422


def test_submit_replay_and_payload_conflict(client):
    _sample(client, "OP-1", "S1", 100)
    _create(client, sids=("S1",))
    body = {"items": [{"sample_id": "S1", "counted_amount": 88}]}
    r1 = client.post("/api/v1/stocktakes/STK-1/submit", json=body)
    assert r1.json()["replayed"] is False
    r2 = client.post("/api/v1/stocktakes/STK-1/submit", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True

    r3 = client.post("/api/v1/stocktakes/STK-1/submit", json={
        "items": [{"sample_id": "S1", "counted_amount": 89}],
    })
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "stocktake_result_conflict"


def test_submit_lifecycle_guards(client):
    _sample(client, "OP-1", "S1", 10)
    # 未知批次
    r = _submit(client, "NOPE", [("S1", 10)])
    assert r.status_code == 404
    assert r.json()["conflicts"][0]["code"] == "stocktake_not_found"

    _create(client, no="STK-L", sids=("S1",))
    _submit(client, "STK-L", [("S1", 10)])
    _confirm(client, "STK-L")
    # 已确认批次不能再提交
    r = _submit(client, "STK-L", [("S1", 10)])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "stocktake_closed"


# ---------------------------------------------------------------- 确认调账

def test_confirm_applies_adjustments_and_versions(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _create(client)
    _submit(client, "STK-1", [("S1", 88), ("S2", 53.5)])

    r = _confirm(client, "STK-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["confirmed_at"] is not None
    assert body["reason"] == "月度盘点差异调整"
    assert body["confirm_operator"] == "张库管"

    by = {i["sample_id"]: i for i in body["items"]}
    assert by["S1"]["current_remaining"] == 88.0
    assert by["S1"]["current_version"] == 1  # 有差异,版本 +1
    assert by["S1"]["delta_applied"] == -12.0
    assert by["S2"]["current_remaining"] == 53.5
    assert by["S2"]["current_version"] == 1
    assert by["S2"]["delta_applied"] == 3.5

    # 调整流水逐项保留操作者与原因
    adj = {a["sample_id"]: a for a in body["adjustments"]}
    assert adj["S1"]["system_amount"] == 100.0
    assert adj["S1"]["counted_amount"] == 88.0
    assert adj["S1"]["delta"] == -12.0
    assert adj["S1"]["operator"] == "张库管"
    assert adj["S1"]["reason"] == "月度盘点差异调整"
    assert adj["S1"]["from_version"] == 0
    assert adj["S1"]["to_version"] == 1

    # 样品余量已更新
    got = client.get("/api/v1/samples").json()
    rem = {i["sample_id"]: i["remaining"] for i in got["items"]}
    assert rem == {"S1": 88.0, "S2": 53.5}


def test_confirm_requires_submitted_state(client):
    _sample(client, "OP-1", "S1", 10)
    _create(client, no="STK-O", sids=("S1",))
    r = _confirm(client, "STK-O")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "stocktake_not_submitted"

    r = _confirm(client, "MISSING")
    assert r.status_code == 404


def test_confirm_rechecks_version_and_whole_batch_rolls_back(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _create(client)
    _submit(client, "STK-1", [("S1", 88), ("S2", 53.5)])

    # 提交后、确认前,其他操作改动 S1(版本不再等于提交时版本)
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-3", "parent_id": "S1",
        "outputs": [{"sample_id": "A1", "amount": 40}],
    })

    r = _confirm(client, "STK-1")
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "stocktake_version_conflict"
    assert c["sample_id"] == "S1"
    assert c["requested"] == 0  # 提交时版本
    assert c["available"] == 1  # 确认时版本

    # 整批不改库存:S2 也没有被调整
    assert client.get("/api/v1/stocktakes/STK-1").json()["status"] == "submitted"
    got = client.get("/api/v1/samples").json()
    rem = {i["sample_id"]: i["remaining"] for i in got["items"]}
    assert rem == {"S1": 60.0, "S2": 50.0, "A1": 40.0}
    # 无任何调整流水落库
    assert client.get("/api/v1/stocktakes/STK-1").json()["adjustments"] == []


def test_confirm_idempotent_and_conflict(client):
    _sample(client, "OP-1", "S1", 100)
    _create(client, sids=("S1",))
    _submit(client, "STK-1", [("S1", 88)])
    r1 = _confirm(client, "STK-1")
    assert r1.json()["replayed"] is False
    r2 = _confirm(client, "STK-1")
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    # 幂等重放不重复写调整流水
    assert len(r2.json()["adjustments"]) == 1

    # 不同操作者/原因再次确认 -> 409
    r3 = _confirm(client, "STK-1", operator="王五", reason="强行改账")
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "stocktake_already_confirmed"


def test_zero_delta_leaves_version_untouched(client):
    _sample(client, "OP-1", "S1", 100)
    _create(client, sids=("S1",))
    _submit(client, "STK-1", [("S1", 100)])  # 账实相符
    r = _confirm(client, "STK-1")
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["delta_applied"] == 0.0
    assert item["current_version"] == 0  # 无差异,版本不变
    # 仍保留一条零差异审计流水
    assert len(r.json()["adjustments"]) == 1
    assert r.json()["adjustments"][0]["to_version"] == 0


# ---------------------------------------------------------------- 账本一致性

def test_balance_ledger_includes_stocktake_adjustments(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _create(client)
    _submit(client, "STK-1", [("S1", 88), ("S2", 53.5)])
    _confirm(client, "STK-1")

    r = client.get("/api/v1/checks/balances")
    data = r.json()
    assert data["consistent"] is True, data
    bal = {b["sample_id"]: b for b in data["samples"]}
    assert bal["S1"]["stored_remaining"] == 88.0
    assert bal["S1"]["ledger_remaining"] == 88.0
    assert bal["S1"]["adjustments"] == -12.0
    assert bal["S2"]["adjustments"] == 3.5

    r = client.get("/api/v1/samples/S1/balance")
    assert r.json()["consistent"] is True
    assert r.json()["samples"][0]["adjustments"] == -12.0


# ---------------------------------------------------------------- 并发确认

def test_concurrent_confirms_serialize_at_most_one_adjustment(tmp_path):
    """两个线程以不同实盘并发确认同一批次:版本二次校验保证只有一个能改账。"""
    db_path = str(tmp_path / "c.db")
    setup = connect(db_path)
    init_db(setup)
    setup.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, version, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 0, 1)"
    )
    setup.execute(
        "INSERT INTO stocktakes (stocktake_no, status, payload) VALUES"
        " ('STK-C', 'submitted', '{}')"
    )
    setup.execute(
        "INSERT INTO stocktake_items (stocktake_no, sample_id, seq_index,"
        " snapshot_remaining, snapshot_unit, snapshot_version, counted_amount,"
        " difference, submitted_version) VALUES"
        " ('STK-C', 'S1', 0, 100, 'mL', 0, 88, -12, 0)"
    )
    setup.commit()
    setup.close()

    from app.schemas import StocktakeConfirmRequest

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(operator: str) -> None:
        c = connect(db_path)
        barrier.wait()
        try:
            st.confirm_stocktake(c, "STK-C", StocktakeConfirmRequest(
                operator=operator, reason="并发确认"))
            outcomes.append("ok")
        except st.StocktakeError:
            outcomes.append("conflict")
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("甲",))
    t2 = threading.Thread(target=worker, args=("乙",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(outcomes) == ["conflict", "ok"]

    check = connect(db_path)
    row = check.execute(
        "SELECT remaining, version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()
    assert row["remaining"] == 88.0
    assert row["version"] == 1
    n = check.execute(
        "SELECT COUNT(*) AS c FROM stocktake_adjustments"
    ).fetchone()["c"]
    assert n == 1
    check.close()


# ---------------------------------------------------------------- 旧库迁移

def test_migration_adds_version_to_legacy_samples_table(tmp_path):
    """仅含旧版 samples 表(无 version 列)的库,init_db 后盘点全流程可用。"""
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE samples (sample_id TEXT PRIMARY KEY, material TEXT NOT NULL,"
        " unit TEXT NOT NULL, initial_amount REAL NOT NULL, remaining REAL NOT NULL,"
        " location TEXT, is_root INTEGER NOT NULL DEFAULT 0, created_at TEXT)"
    )
    raw.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 1)"
    )
    raw.commit()
    raw.close()

    conn = connect(db_path)
    init_db(conn)

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(samples)")]
    assert "version" in cols
    row = conn.execute(
        "SELECT version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()
    assert row["version"] == 0

    # 迁移登记只执行一次:再次 init_db 不报错、不重复
    init_db(conn)
    names = [
        r["name"] for r in conn.execute("SELECT name FROM schema_migrations")
    ]
    assert "samples_add_version" in names

    # 迁移后盘点全流程可用
    from app.schemas import StocktakeRequest, StocktakeSubmitRequest, StocktakeConfirmRequest

    st.create_stocktake(conn, StocktakeRequest(
        stocktake_no="STK-M",
        items=[{"sample_id": "S1"}],  # type: ignore[list-item]
    ))
    st.submit_stocktake(conn, "STK-M", StocktakeSubmitRequest(
        items=[{"sample_id": "S1", "counted_amount": 90}],  # type: ignore[list-item]
    ))
    res = st.confirm_stocktake(conn, "STK-M", StocktakeConfirmRequest(
        operator="迁移人", reason="迁移后盘点"))
    assert res.status == "confirmed"
    row = conn.execute(
        "SELECT remaining, version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()
    assert row["remaining"] == 90.0
    assert row["version"] == 1
    conn.close()
