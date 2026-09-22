"""盘点模块端到端测试:余量快照、版本校验、差异计算、确认调整、幂等与迁移。"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import stocktake as stk
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


def _sample(client, op_no, sid, amount, unit="mL", material="血清"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": material, "amount": amount, "unit": unit,
    })
    assert r.status_code == 200, r.text


def _items_map(body) -> dict:
    return {i["sample_id"]: i for i in body["items"]}


def _create_batch(client, batch_no, sample_ids, note=None):
    return client.post("/api/v1/stocktakes", json={
        "batch_no": batch_no, "sample_ids": sample_ids, "note": note,
    })


def _submit(client, batch_no, counts):
    return client.post(f"/api/v1/stocktakes/{batch_no}/results", json={
        "batch_no": batch_no,
        "items": [
            {"sample_id": sid, "counted_amount": amt} for sid, amt in counts
        ],
    })


def _confirm(client, batch_no, operator="张三", reason="月度盘点"):
    return client.post(f"/api/v1/stocktakes/{batch_no}/confirm", json={
        "batch_no": batch_no, "operator": operator, "reason": reason,
    })


# ---------------------------------------------------------------- 建批次 / 快照

def test_create_batch_stores_snapshot_and_query_returns_it(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)

    r = _create_batch(client, "STK-1", ["S1", "S2"], note="冷库盘点")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_no"] == "STK-1"
    assert body["status"] == "open"
    assert body["replayed"] is False
    assert body["note"] == "冷库盘点"
    items = _items_map(body)
    assert list(_items_map(body)) == ["S1", "S2"]  # 顺序保留
    assert items["S1"]["snapshot_remaining"] == 100.0
    assert items["S1"]["snapshot_version"] == 0
    assert items["S1"]["unit"] == "mL"
    assert items["S1"]["counted_amount"] is None
    assert items["S1"]["variance"] is None
    assert items["S1"]["current_remaining"] == 100.0

    # GET 返回同样的快照与状态
    r = client.get("/api/v1/stocktakes/STK-1")
    assert r.status_code == 200
    assert r.json()["status"] == "open"
    assert r.json()["replayed"] is False
    assert _items_map(r.json())["S2"]["snapshot_remaining"] == 50.0


def test_create_batch_unknown_sample_conflicts_and_creates_nothing(client):
    _sample(client, "OP-1", "S1", 100)
    r = _create_batch(client, "STK-BAD", ["S1", "GHOST"])
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert [c["code"] for c in conflicts] == ["sample_not_found"]
    assert conflicts[0]["sample_id"] == "GHOST"
    assert conflicts[0]["op_no"] == "STK-BAD"
    assert client.get("/api/v1/stocktakes/STK-BAD").status_code == 404


def test_create_batch_duplicate_sample_ids_rejected(client):
    r = _create_batch(client, "STK-DUP", ["S1", "S1"])
    assert r.status_code == 422


def test_create_batch_idempotent_replay_and_content_conflict(client):
    _sample(client, "OP-1", "S1", 100)
    body = {"batch_no": "STK-IDEM", "sample_ids": ["S1"]}
    r1 = client.post("/api/v1/stocktakes", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False

    # 同号同体重放,即使 S1 余量之后被改过也返回首次快照
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-2", "parent_id": "S1",
        "outputs": [{"sample_id": "A1", "amount": 30}],
    })
    r2 = client.post("/api/v1/stocktakes", json=body)
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["replayed"] is True
    assert _items_map(b2)["S1"]["snapshot_remaining"] == 100.0
    assert _items_map(b2)["S1"]["snapshot_version"] == 0

    # 同号不同内容 -> 409
    r3 = client.post("/api/v1/stocktakes",
                     json={"batch_no": "STK-IDEM", "sample_ids": ["S1", "S2"]})
    # S2 还不存在,但幂等键冲突优先于样品校验
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "stocktake_batch_conflict"


# ---------------------------------------------------------------- 提交实盘结果

def test_submit_results_computes_variance_when_unchanged(client):
    _sample(client, "OP-1", "S1", 100)
    _create_batch(client, "STK-1", ["S1"])

    r = _submit(client, "STK-1", [("S1", 95.0)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "counted"
    assert body["replayed"] is False
    assert body["counted_at"] is not None
    item = _items_map(body)["S1"]
    assert item["counted_amount"] == 95.0
    assert item["system_remaining"] == 100.0
    assert item["variance"] == -5.0
    assert item["changed_since_snapshot"] is False

    # 提交实盘结果不改动库存
    r = client.get("/api/v1/reservations?sample_id=S1")
    assert r.json()["samples"][0]["remaining"] == 100.0


def test_submit_results_version_conflict_when_balance_changed(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _create_batch(client, "STK-1", ["S1", "S2"])

    # 建批次后 S1 被分装,余量版本从 0 -> 1
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-3", "parent_id": "S1",
        "outputs": [{"sample_id": "A1", "amount": 40}],
    })

    r = _submit(client, "STK-1", [("S1", 55.0), ("S2", 48.0)])
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["code"] == "stocktake_version_conflict"
    assert c["sample_id"] == "S1"
    assert c["requested"] == 100.0   # 快照余量
    assert c["available"] == 60.0    # 当前余量
    assert "版本 0" in c["message"] and "版本 1" in c["message"]

    # 冲突后批次仍为 open,可在系统恢复一致前重建批次(本批次不能提交)
    g = client.get("/api/v1/stocktakes/STK-1").json()
    assert g["status"] == "open"
    assert _items_map(g)["S1"]["counted_amount"] is None


def test_submit_results_rejects_item_set_mismatch_but_ignores_order(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _create_batch(client, "STK-1", ["S1", "S2"])

    # 少一项:集合不一致 -> 冲突,批次保持 open
    r = _submit(client, "STK-1", [("S1", 100.0)])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "stocktake_items_mismatch"
    g = client.get("/api/v1/stocktakes/STK-1").json()
    assert g["status"] == "open"

    # 多出未知样品:集合不一致 -> 冲突
    r = _submit(client, "STK-1", [
        ("S1", 100.0), ("S2", 50.0), ("GHOST", 1.0),
    ])
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "stocktake_items_mismatch"

    # 顺序不同但集合一致:接受(实盘结果按样品号匹配,与顺序无关)
    r = _submit(client, "STK-1", [("S2", 48.0), ("S1", 99.0)])
    assert r.status_code == 200, r.text
    items = _items_map(r.json())
    assert items["S1"]["counted_amount"] == 99.0
    assert items["S2"]["counted_amount"] == 48.0
    assert r.json()["status"] == "counted"


def test_submit_results_state_and_idempotency_rules(client):
    _sample(client, "OP-1", "S1", 100)
    _create_batch(client, "STK-1", ["S1"])

    # open 批次才能提交
    assert client.post("/api/v1/stocktakes/NOPE/results", json={
        "batch_no": "NOPE", "items": [{"sample_id": "S1", "counted_amount": 1}],
    }).status_code == 404

    counts_body = {
        "batch_no": "STK-1",
        "items": [{"sample_id": "S1", "counted_amount": 95}],
    }
    r1 = client.post("/api/v1/stocktakes/STK-1/results", json=counts_body)
    assert r1.json()["replayed"] is False

    # 同体重放
    r2 = client.post("/api/v1/stocktakes/STK-1/results", json=counts_body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert _items_map(r2.json())["S1"]["counted_amount"] == 95.0

    # 不同实盘量 -> 冲突
    r3 = client.post("/api/v1/stocktakes/STK-1/results", json={
        "batch_no": "STK-1",
        "items": [{"sample_id": "S1", "counted_amount": 94}],
    })
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "stocktake_batch_conflict"


def test_submit_results_negative_count_rejected(client):
    _sample(client, "OP-1", "S1", 100)
    _create_batch(client, "STK-1", ["S1"])
    r = _submit(client, "STK-1", [("S1", -1.0)])
    assert r.status_code == 422


def test_submit_results_path_body_mismatch(client):
    _sample(client, "OP-1", "S1", 100)
    _create_batch(client, "STK-1", ["S1"])
    r = client.post("/api/v1/stocktakes/STK-1/results", json={
        "batch_no": "STK-OTHER",
        "items": [{"sample_id": "S1", "counted_amount": 1}],
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "stocktake_batch_conflict"


# ---------------------------------------------------------------- 确认

def test_confirm_writes_adjustments_updates_remaining_and_ledger(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _create_batch(client, "STK-1", ["S1", "S2"])
    _submit(client, "STK-1", [("S1", 95.0), ("S2", 53.0)])

    r = _confirm(client, "STK-1", operator="李四", reason="期末盘点")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["replayed"] is False
    assert body["operator"] == "李四"
    assert body["reason"] == "期末盘点"
    assert body["confirmed_at"] is not None
    items = _items_map(body)
    assert items["S1"]["variance"] == -5.0
    assert items["S1"]["final_remaining"] == 95.0
    assert items["S2"]["variance"] == 3.0
    assert items["S2"]["final_remaining"] == 53.0

    # 样品余量已更新(S1 因调整再 +1 版本 -> 2)
    r = client.get("/api/v1/reservations?sample_id=S1&sample_id=S2")
    avail = {x["sample_id"]: x for x in r.json()["samples"]}
    assert avail["S1"]["remaining"] == 95.0
    assert avail["S2"]["remaining"] == 53.0

    # 调整流水带操作者与原因,差异为逐项记录
    from app.db import get_conn as _get_conn  # noqa: F401
    # 直接通过依赖注入的内存库校验(同一 TestClient 进程内)
    mem = next(app.dependency_overrides[get_conn]())
    rows = mem.execute(
        "SELECT * FROM stocktake_adjustments WHERE batch_no = 'STK-1'"
        " ORDER BY sample_id"
    ).fetchall()
    assert [r["sample_id"] for r in rows] == ["S1", "S2"]
    assert rows[0]["variance"] == -5.0
    assert rows[0]["system_remaining"] == 100.0
    assert rows[0]["operator"] == "李四"
    assert rows[0]["reason"] == "期末盘点"
    assert rows[1]["variance"] == 3.0
    versions = {
        r["sample_id"]: r["version"]
        for r in mem.execute("SELECT sample_id, version FROM samples").fetchall()
    }
    # 建批次前无任何余量写操作(取样版本为 0),确认调整 +1 -> 1
    assert versions["S1"] == 1
    assert versions["S2"] == 1

    # 账本一致性校验:调整流水纳入重算后仍一致
    r = client.get("/api/v1/checks/balances")
    assert r.json()["consistent"] is True


def test_confirm_zero_variance_no_version_bump_and_audit_row(client):
    _sample(client, "OP-1", "S1", 100)
    _create_batch(client, "STK-1", ["S1"])
    _submit(client, "STK-1", [("S1", 100.0)])
    r = _confirm(client, "STK-1")
    assert r.status_code == 200
    assert _items_map(r.json())["S1"]["variance"] == 0.0

    mem = next(app.dependency_overrides[get_conn]())
    # 差异为 0:余量不变,版本不递增,但仍有调整流水留痕
    row = mem.execute(
        "SELECT version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()
    assert row["version"] == 0
    n = mem.execute(
        "SELECT COUNT(*) AS c FROM stocktake_adjustments WHERE batch_no = 'STK-1'"
    ).fetchone()["c"]
    assert n == 1


def test_confirm_version_conflict_rolls_back_entire_batch(client):
    """提交后、确认前 S2 被其他操作改动 -> 整批不调整任何库存。"""
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    _create_batch(client, "STK-1", ["S1", "S2"])
    _submit(client, "STK-1", [("S1", 90.0), ("S2", 40.0)])

    # 提交结果后 S2 又被分装(版本变化)
    client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": "OP-4", "parent_id": "S2",
        "outputs": [{"sample_id": "A2", "amount": 10}],
    })

    r = _confirm(client, "STK-1")
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["code"] == "stocktake_version_conflict"
    assert c["sample_id"] == "S2"
    assert c["requested"] == 50.0
    assert c["available"] == 40.0

    # 整批回滚:批次仍 counted;S1/S2 余量均未被盘点调整
    g = client.get("/api/v1/stocktakes/STK-1").json()
    assert g["status"] == "counted"
    r = client.get("/api/v1/reservations?sample_id=S1&sample_id=S2")
    avail = {x["sample_id"]: x for x in r.json()["samples"]}
    assert avail["S1"]["remaining"] == 100.0
    assert avail["S2"]["remaining"] == 40.0
    mem = next(app.dependency_overrides[get_conn]())
    n = mem.execute(
        "SELECT COUNT(*) AS c FROM stocktake_adjustments WHERE batch_no = 'STK-1'"
    ).fetchone()["c"]
    assert n == 0

    # 冲突可重放式重试:系统侧余量恢复一致不可能(版本已永久前进),
    # 因此该批次不可再确认;用新批次号重新盘点可成功
    r = _confirm(client, "STK-1")
    assert r.status_code == 409

    _create_batch(client, "STK-2", ["S1", "S2"])
    r = _submit(client, "STK-2", [("S1", 90.0), ("S2", 35.0)])
    assert r.status_code == 200, r.text
    r = _confirm(client, "STK-2", reason="重盘")
    assert r.status_code == 200
    avail2 = {
        x["sample_id"]: x
        for x in client.get(
            "/api/v1/reservations?sample_id=S1&sample_id=S2"
        ).json()["samples"]
    }
    assert avail2["S1"]["remaining"] == 90.0
    assert avail2["S2"]["remaining"] == 35.0


def test_confirm_state_and_idempotency_rules(client):
    _sample(client, "OP-1", "S1", 100)
    _create_batch(client, "STK-1", ["S1"])

    # open 批次不能确认
    r = _confirm(client, "STK-1")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "stocktake_batch_conflict"

    # 不存在 -> 404
    assert _confirm(client, "NOPE").status_code == 404

    _submit(client, "STK-1", [("S1", 95.0)])
    body = {"batch_no": "STK-1", "operator": "王五", "reason": "盘盈"}
    r1 = client.post("/api/v1/stocktakes/STK-1/confirm", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False

    # 同体确认重放:返回首次结果,不重复写流水、不重复改余量
    r2 = client.post("/api/v1/stocktakes/STK-1/confirm", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["operator"] == "王五"

    mem = next(app.dependency_overrides[get_conn]())
    n = mem.execute(
        "SELECT COUNT(*) AS c FROM stocktake_adjustments WHERE batch_no = 'STK-1'"
    ).fetchone()["c"]
    assert n == 1

    # 已确认批次不同操作者/原因 -> 冲突
    r3 = client.post("/api/v1/stocktakes/STK-1/confirm", json={
        "batch_no": "STK-1", "operator": "赵六", "reason": "盘盈",
    })
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "stocktake_batch_conflict"

    # 已确认批次不能再提交实盘结果
    r4 = _submit(client, "STK-1", [("S1", 95.0)])
    assert r4.status_code == 409
    assert r4.json()["conflicts"][0]["code"] == "stocktake_batch_conflict"


# ---------------------------------------------------------------- 并发

def test_concurrent_confirm_and_operation_one_wins(tmp_path):
    """确认与正常操作并发:版本校验保证要么调整生效、要么整批回滚,绝不双重写。"""
    db_path = str(tmp_path / "c.db")
    setup = connect(db_path)
    init_db(setup)
    setup.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root, version) VALUES ('S1', 'M', 'mL', 100, 100, 1, 0)"
    )
    setup.commit()
    setup.close()

    from app.schemas import StocktakeBatchRequest, StocktakeConfirmRequest
    from app.schemas import StocktakeCountRequest

    def make_count_conn() -> None:
        c = connect(db_path)
        stk.create_batch(c, StocktakeBatchRequest(
            batch_no="STK-C", sample_ids=["S1"],  # type: ignore[arg-type]
        ))
        stk.submit_results(c, StocktakeCountRequest(
            batch_no="STK-C",
            items=[{"sample_id": "S1", "counted_amount": 90}],  # type: ignore[list-item]
        ))
        c.close()

    make_count_conn()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def confirm_worker() -> None:
        c = connect(db_path)
        barrier.wait()
        try:
            stk.confirm_batch(c, StocktakeConfirmRequest(
                batch_no="STK-C", operator="t", reason="并发测试",
            ))
            outcomes.append("confirm-ok")
        except stk.StocktakeError:
            outcomes.append("confirm-conflict")
        finally:
            c.close()

    def aliquot_worker() -> None:
        from app import service
        from app.schemas import AliquotOp

        c = connect(db_path)
        barrier.wait()
        try:
            service.submit_batch(c, [AliquotOp(
                type="aliquot", op_no="OP-C", parent_id="S1",
                outputs=[{"sample_id": "A1", "amount": 20}],  # type: ignore[list-item]
            )])
            outcomes.append("op-ok")
        except service.ConflictError:
            outcomes.append("op-conflict")
        finally:
            c.close()

    t1 = threading.Thread(target=confirm_worker)
    t2 = threading.Thread(target=aliquot_worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    check = connect(db_path)
    remaining = check.execute(
        "SELECT remaining, version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()
    n_adj = check.execute(
        "SELECT COUNT(*) AS c FROM stocktake_adjustments WHERE batch_no = 'STK-C'"
    ).fetchone()["c"]
    header = check.execute(
        "SELECT status FROM stocktake_batches WHERE batch_no = 'STK-C'"
    ).fetchone()
    check.close()

    if "confirm-ok" in outcomes:
        # 盘点先拿到锁:余量 = 90,之后分装取 20 -> 70,批次已确认且恰一条流水
        assert "op-ok" in outcomes
        assert remaining["remaining"] == 70.0
        assert header["status"] == "confirmed"
        assert n_adj == 1
    else:
        # 分装先提交:版本前进,盘点整批回滚,余量 = 80、无调整流水
        assert outcomes == ["op-ok", "confirm-conflict"]
        assert remaining["remaining"] == 80.0
        assert header["status"] == "counted"
        assert n_adj == 0


# ---------------------------------------------------------------- 迁移

def test_migration_from_legacy_db_adds_version_and_stocktake_tables(tmp_path):
    """旧库(无 samples.version 与盘点表)经 init_db 迁移后可完整跑盘点流程。"""
    db_path = str(tmp_path / "legacy.db")
    legacy = connect(db_path)
    # 手工构造 v1 旧库:samples 没有 version 列,也没有任何盘点表
    legacy.executescript("""
CREATE TABLE samples (
    sample_id      TEXT PRIMARY KEY,
    material       TEXT NOT NULL,
    unit           TEXT NOT NULL,
    initial_amount REAL NOT NULL CHECK (initial_amount >= 0),
    remaining      REAL NOT NULL CHECK (remaining >= 0),
    location       TEXT,
    is_root        INTEGER NOT NULL DEFAULT 0 CHECK (is_root IN (0, 1)),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE operations (
    op_id INTEGER PRIMARY KEY AUTOINCREMENT, op_no TEXT NOT NULL UNIQUE,
    op_type TEXT NOT NULL, payload TEXT NOT NULL, effect TEXT NOT NULL,
    seq_index INTEGER NOT NULL, batch_id TEXT, location TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
""")
    legacy.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 1)"
    )
    legacy.commit()
    legacy.close()

    conn = connect(db_path)
    init_db(conn)  # 触发 0001 迁移

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(samples)").fetchall()}
    assert "version" in cols
    row = conn.execute(
        "SELECT version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()
    assert row["version"] == 0

    applied = [
        r["version"] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    assert applied == ["0001_stocktake"]

    # 迁移幂等:再次 init 不报错也不重复登记
    init_db(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
    assert n == 1

    # 迁移后的旧库可直接走完整盘点工作流
    from app.schemas import StocktakeBatchRequest, StocktakeConfirmRequest
    from app.schemas import StocktakeCountRequest

    stk.create_batch(conn, StocktakeBatchRequest(
        batch_no="STK-M", sample_ids=["S1"],  # type: ignore[arg-type]
    ))
    stk.submit_results(conn, StocktakeCountRequest(
        batch_no="STK-M",
        items=[{"sample_id": "S1", "counted_amount": 97}],  # type: ignore[list-item]
    ))
    res = stk.confirm_batch(conn, StocktakeConfirmRequest(
        batch_no="STK-M", operator="迁移", reason="迁移后盘点",
    ))
    assert res.status == "confirmed"
    assert _items_map(json.loads(res.model_dump_json()))["S1"]["variance"] == -3.0
    row = conn.execute(
        "SELECT remaining, version FROM samples WHERE sample_id = 'S1'"
    ).fetchone()
    assert row["remaining"] == 97.0
    assert row["version"] == 1
    conn.close()
