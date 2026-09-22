"""预留模块端到端测试:多样品事务锁定、幂等重放、整单释放、可用量查询、审计持久化。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import reservations as rsv
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


# ---------------------------------------------------------------- 创建 / 查询

def test_reserve_multiple_samples_and_query_availability(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)

    r = client.post("/api/v1/reservations", json={
        "reservation_no": "RSV-1",
        "note": "实验A备料",
        "items": [
            {"sample_id": "S1", "amount": 30},
            {"sample_id": "S2", "amount": 20},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reservation_no"] == "RSV-1"
    assert body["status"] == "active"
    assert body["replayed"] is False
    items = {i["sample_id"]: i for i in body["items"]}
    assert items["S1"]["amount"] == 30.0
    assert items["S1"]["unit"] == "mL"
    assert items["S1"]["remaining"] == 100.0
    assert items["S1"]["reserved"] == 30.0
    assert items["S1"]["available"] == 70.0
    assert items["S2"]["available"] == 30.0

    # 查询接口:多个样品
    r = client.get("/api/v1/reservations?sample_id=S1&sample_id=S2")
    assert r.status_code == 200, r.text
    avail = {x["sample_id"]: x for x in r.json()["samples"]}
    assert avail["S1"] == {
        "sample_id": "S1", "unit": "mL",
        "remaining": 100.0, "reserved": 30.0, "available": 70.0,
    }
    assert avail["S2"]["available"] == 30.0

    # 不带参数:返回全部样品
    r = client.get("/api/v1/reservations")
    assert {x["sample_id"] for x in r.json()["samples"]} == {"S1", "S2"}

    # 按预留号查询
    r = client.get("/api/v1/reservations/RSV-1")
    assert r.status_code == 200
    assert r.json()["replayed"] is False
    assert len(r.json()["items"]) == 2


def test_reserve_all_or_nothing_partial_shortage_rolls_back(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 10)

    # S1 足够、S2 不足 -> 整单失败,S1 上不留任何预留
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "RSV-BAD",
        "items": [
            {"sample_id": "S1", "amount": 90},
            {"sample_id": "S2", "amount": 20},
        ],
    })
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["code"] == "insufficient_available"
    assert c["sample_id"] == "S2"
    assert c["requested"] == 20.0
    assert c["available"] == 10.0
    assert c["op_no"] == "RSV-BAD"

    # 整单回滚:预留单与明细都不存在
    r = client.get("/api/v1/reservations/RSV-BAD")
    assert r.status_code == 404
    r = client.get("/api/v1/reservations?sample_id=S1")
    s1 = next(x for x in r.json()["samples"] if x["sample_id"] == "S1")
    assert s1["reserved"] == 0.0
    assert s1["available"] == 100.0


def test_reserve_checks_unreleased_reservations(client):
    _sample(client, "OP-1", "S1", 100)
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "R1",
        "items": [{"sample_id": "S1", "amount": 60}],
    })
    assert r.status_code == 200

    # 再锁 50:账面余量 100 足够,但可用量只剩 40 -> 冲突
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "R2",
        "items": [{"sample_id": "S1", "amount": 50}],
    })
    assert r.status_code == 409
    c = r.json()["conflicts"][0]
    assert c["code"] == "insufficient_available"
    assert c["requested"] == 50.0
    assert c["available"] == 40.0

    # 40 刚好等于可用量:成功
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "R3",
        "items": [{"sample_id": "S1", "amount": 40}],
    })
    assert r.status_code == 200
    s1 = next(i for i in r.json()["items"] if i["sample_id"] == "S1")
    assert s1["reserved"] == 100.0
    assert s1["available"] == 0.0


def test_reserve_unknown_sample_conflicts_and_does_not_create(client):
    _sample(client, "OP-1", "S1", 100)
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "R-GHOST",
        "items": [
            {"sample_id": "S1", "amount": 1},
            {"sample_id": "GHOST", "amount": 1},
        ],
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "sample_not_found"
    assert client.get("/api/v1/reservations/R-GHOST").status_code == 404


def test_reserve_duplicate_sample_in_request_rejected(client):
    # Pydantic 层拒绝同一样品在同一单内出现多次
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "R-DUP",
        "items": [
            {"sample_id": "S1", "amount": 1},
            {"sample_id": "S1", "amount": 2},
        ],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------- 幂等

def test_reserve_idempotent_replay_and_payload_conflict(client):
    _sample(client, "OP-1", "S1", 100)
    body = {
        "reservation_no": "RSV-IDEM",
        "items": [{"sample_id": "S1", "amount": 70}],
    }
    r1 = client.post("/api/v1/reservations", json=body)
    assert r1.status_code == 200
    assert r1.json()["replayed"] is False

    # 即使之后其余量已被其他预留占掉,同号重放仍返回首次结果
    client.post("/api/v1/reservations", json={
        "reservation_no": "RSV-OTHER",
        "items": [{"sample_id": "S1", "amount": 30}],
    })
    r2 = client.post("/api/v1/reservations", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert len(r2.json()["items"]) == 1
    assert r2.json()["items"][0]["amount"] == 70.0

    # 同号不同内容 -> 409
    r3 = client.post("/api/v1/reservations", json={
        "reservation_no": "RSV-IDEM",
        "items": [{"sample_id": "S1", "amount": 71}],
    })
    assert r3.status_code == 409
    assert r3.json()["conflicts"][0]["code"] == "reservation_no_conflict"

    # 只有两条明细(没有重复落库)
    r = client.get("/api/v1/reservations/RSV-IDEM")
    assert sum(
        i["amount"] for i in r.json()["items"] if i["sample_id"] == "S1"
    ) == 70.0


# ---------------------------------------------------------------- 释放

def test_release_restores_availability_and_is_idempotent(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 100)
    client.post("/api/v1/reservations", json={
        "reservation_no": "RSV-REL",
        "items": [
            {"sample_id": "S1", "amount": 60},
            {"sample_id": "S2", "amount": 40},
        ],
    })

    r = client.delete("/api/v1/reservations/RSV-REL")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "released"
    assert body["replayed"] is False
    assert body["released_at"] is not None
    avail = {i["sample_id"]: i for i in body["items"]}
    assert avail["S1"]["reserved"] == 0.0
    assert avail["S1"]["available"] == 100.0
    assert avail["S2"]["available"] == 100.0

    # 可用量查询确认锁已归还
    r = client.get("/api/v1/reservations?sample_id=S1")
    assert r.json()["samples"][0]["reserved"] == 0.0

    # 释放后可用量可被新预留重新锁定
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "RSV-NEW",
        "items": [{"sample_id": "S1", "amount": 100}],
    })
    assert r.status_code == 200

    # 重复释放:幂等回放
    r = client.delete("/api/v1/reservations/RSV-REL")
    assert r.status_code == 200
    assert r.json()["replayed"] is True

    # 已释放的预留号不能再用于创建
    r = client.post("/api/v1/reservations", json={
        "reservation_no": "RSV-REL",
        "items": [{"sample_id": "S1", "amount": 1}],
    })
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["code"] == "reservation_closed"


def test_release_unknown_reservation_404(client):
    r = client.delete("/api/v1/reservations/NOPE")
    assert r.status_code == 404
    assert r.json()["conflicts"][0]["code"] == "reservation_not_found"


def test_release_one_reservation_does_not_touch_others(client):
    _sample(client, "OP-1", "S1", 100)
    client.post("/api/v1/reservations", json={
        "reservation_no": "A", "items": [{"sample_id": "S1", "amount": 30}],
    })
    client.post("/api/v1/reservations", json={
        "reservation_no": "B", "items": [{"sample_id": "S1", "amount": 20}],
    })
    client.delete("/api/v1/reservations/A")

    r = client.get("/api/v1/reservations?sample_id=S1")
    s1 = r.json()["samples"][0]
    assert s1["reserved"] == 20.0
    assert s1["available"] == 80.0


# ---------------------------------------------------------------- 审计持久化

def test_reservation_lifecycle_via_api(client):
    """API 层完整生命周期:预留 -> 重放 -> 查询 -> 释放,行为始终正确。"""
    _sample(client, "OP-1", "S1", 100)
    body = {"reservation_no": "RSV-E",
            "items": [{"sample_id": "S1", "amount": 10}]}
    assert client.post("/api/v1/reservations", json=body).json()["replayed"] is False
    assert client.post("/api/v1/reservations", json=body).json()["replayed"] is True
    assert client.get("/api/v1/reservations?sample_id=S1").status_code == 200
    assert client.delete("/api/v1/reservations/RSV-E").status_code == 200
    # 释放后查询可用量已恢复
    r = client.get("/api/v1/reservations?sample_id=S1")
    assert r.json()["samples"][0]["available"] == 100.0


def test_events_persisted_direct_db(tmp_path):
    """直接对文件库断言 reservation_events:reserve/replay/query/release 均入库。"""
    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 1)"
    )
    conn.commit()

    from app.schemas import ReservationRequest

    body = ReservationRequest(
        reservation_no="RSV-D",
        items=[{"sample_id": "S1", "amount": 10}],  # type: ignore[arg-type]
    )
    rsv.create_reservation(conn, body)
    rsv.create_reservation(conn, body)  # replay
    rsv.get_availability(conn, ["S1"])
    rsv.release_reservation(conn, "RSV-D")
    rsv.release_reservation(conn, "RSV-D")  # replay release

    actions = [
        r["action"]
        for r in conn.execute(
            "SELECT action FROM reservation_events ORDER BY event_id"
        ).fetchall()
    ]
    assert actions == ["reserve", "replay", "query", "release", "release"]

    # 释放事件 detail 中记录了释放后的可用量
    row = conn.execute(
        "SELECT detail FROM reservation_events WHERE action = 'release'"
        " ORDER BY event_id LIMIT 1"
    ).fetchone()
    import json
    detail = json.loads(row["detail"])
    assert detail["status"] == "released"
    assert detail["items"][0]["available"] == 100.0

    # 释放后预留头状态为 released,明细保留(审计痕迹不删除)
    status = conn.execute(
        "SELECT status FROM reservations WHERE reservation_no = 'RSV-D'"
    ).fetchone()["status"]
    assert status == "released"
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM reservation_items WHERE reservation_no = 'RSV-D'"
    ).fetchone()["c"]
    assert n == 1
    conn.close()


# ---------------------------------------------------------------- 并发

def test_concurrent_reservations_serialize_without_oversell(tmp_path):
    """两个线程并发锁同一样品(各 60,共 120 > 100),恰好一个失败且不超卖。"""
    db_path = str(tmp_path / "c.db")
    setup = connect(db_path)
    init_db(setup)
    setup.execute(
        "INSERT INTO samples (sample_id, material, unit, initial_amount,"
        " remaining, is_root) VALUES ('S1', 'M', 'mL', 100, 100, 1)"
    )
    setup.commit()
    setup.close()

    from app.schemas import ReservationRequest

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(no: str) -> None:
        c = connect(db_path)
        barrier.wait()
        try:
            rsv.create_reservation(c, ReservationRequest(
                reservation_no=no,
                items=[{"sample_id": "S1", "amount": 60}],  # type: ignore[arg-type]
            ))
            outcomes.append("ok")
        except rsv.ReservationError:
            outcomes.append("conflict")
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("RC-1",))
    t2 = threading.Thread(target=worker, args=("RC-2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    check = connect(db_path)
    row = check.execute(
        "SELECT COALESCE(SUM(ri.amount), 0) AS locked FROM reservation_items ri"
        " JOIN reservations r ON r.reservation_no = ri.reservation_no"
        " WHERE ri.sample_id = 'S1' AND r.status = 'active'"
    ).fetchone()
    assert row["locked"] == 60.0
    check.close()
