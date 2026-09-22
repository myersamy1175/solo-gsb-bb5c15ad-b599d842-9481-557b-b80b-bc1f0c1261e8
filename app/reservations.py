"""预留服务:一个预留号为多个样品分别锁定数量。

- 创建:单事务(`BEGIN IMMEDIATE`)内对每个样品校验
  「当前余量 - 未释放预留」是否足够,全部足够才一次落库,否则整单回滚
- 幂等:同一预留号 + 同一请求体重放,返回首次结果;同号不同内容 409
- 释放:整单释放全部明细,释放同样按预留号幂等
- 查询:返回样品当前余量、已预留量(未释放预留合计)与可用量
- 审计:预留 / 重放 / 释放 / 查询事件均写入 reservation_events 持久化
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from . import schemas as s

_EPS = 1e-9  # 运算时浮点比较


class ReservationError(Exception):
    """预留业务冲突,携带结构化冲突列表。"""

    def __init__(self, conflicts: list[s.Conflict]):
        self.conflicts = conflicts
        super().__init__(conflicts[0].message if conflicts else "reservation conflict")


def _r(x: float) -> float:
    """统一保留 9 位小数,消除浮点尾巴。"""
    return round(float(x), 9)


def _conflict(
    code: str,
    message: str,
    *,
    reservation_no: Optional[str] = None,
    sample_id: Optional[str] = None,
    requested: Optional[float] = None,
    available: Optional[float] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=reservation_no,
        sample_id=sample_id,
        requested=requested,
        available=available,
    )


# ---------------------------------------------------------------- 审计事件

def _log_event(
    conn: sqlite3.Connection,
    action: str,
    detail: dict,
    *,
    reservation_no: Optional[str] = None,
    ref_id: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO reservation_events (reservation_no, action, ref_id, detail)"
        " VALUES (?, ?, ?, ?)",
        (reservation_no, action, ref_id, json.dumps(detail, ensure_ascii=False)),
    )


# ---------------------------------------------------------------- 余量视图

def _availability_map(
    conn: sqlite3.Connection, sample_ids: list[str]
) -> dict[str, dict]:
    """取各样品当前余量与未释放预留合计,计算可用量。"""
    out: dict[str, dict] = {}
    if not sample_ids:
        return out
    qmarks = ",".join("?" * len(sample_ids))
    rows = conn.execute(
        f"""SELECT s.sample_id AS sample_id, s.unit AS unit,
                   s.remaining AS remaining,
                   COALESCE((
                       SELECT SUM(ri.amount)
                         FROM reservation_items ri
                         JOIN reservations r ON r.reservation_no = ri.reservation_no
                        WHERE ri.sample_id = s.sample_id AND r.status = 'active'
                   ), 0) AS reserved
              FROM samples s
             WHERE s.sample_id IN ({qmarks})""",
        sample_ids,
    ).fetchall()
    for row in rows:
        remaining, reserved = _r(row["remaining"]), _r(row["reserved"])
        out[row["sample_id"]] = {
            "unit": row["unit"],
            "remaining": remaining,
            "reserved": reserved,
            "available": _r(remaining - reserved),
        }
    return out


def _load_items(
    conn: sqlite3.Connection, reservation_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM reservation_items WHERE reservation_no = ?"
        " ORDER BY seq_index, item_id",
        (reservation_no,),
    ).fetchall()


def _build_result(
    conn: sqlite3.Connection,
    header: sqlite3.Row,
    *,
    replayed: bool,
) -> s.ReservationResult:
    """组装预留单结果:明细附带各样品当前余量/已预留/可用量快照。"""
    rows = _load_items(conn, header["reservation_no"])
    avail = _availability_map(conn, [r["sample_id"] for r in rows])
    return s.ReservationResult(
        reservation_no=header["reservation_no"],
        status=header["status"],
        note=header["note"],
        replayed=replayed,
        created_at=header["created_at"],
        released_at=header["released_at"],
        items=[
            s.ReservationItemOut(
                sample_id=r["sample_id"],
                amount=_r(r["amount"]),
                unit=r["unit"],
                remaining=avail[r["sample_id"]]["remaining"],
                reserved=avail[r["sample_id"]]["reserved"],
                available=avail[r["sample_id"]]["available"],
            )
            for r in rows
        ],
    )


# ---------------------------------------------------------------- 创建预留

def create_reservation(
    conn: sqlite3.Connection, req: s.ReservationRequest
) -> s.ReservationResult:
    """同一事务内校验所有样品可用余量,全部足够才一次创建,否则整单失败。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM reservations WHERE reservation_no = ?",
            (req.reservation_no,),
        ).fetchone()
        if existing is not None:
            if existing["status"] == "released":
                raise ReservationError([
                    _conflict(
                        "reservation_closed",
                        f"预留号 {req.reservation_no} 已释放,不能重复创建",
                        reservation_no=req.reservation_no,
                    )
                ])
            if existing["payload"] != payload:
                raise ReservationError([
                    _conflict(
                        "reservation_no_conflict",
                        f"预留号 {req.reservation_no} 已存在但请求内容不同,"
                        "同一预留号必须对应同一次预留(幂等键冲突)",
                        reservation_no=req.reservation_no,
                    )
                ])
            result = _build_result(conn, existing, replayed=True)
            _log_event(
                conn, "replay", result.model_dump(),
                reservation_no=req.reservation_no,
            )
            conn.execute("COMMIT")
            return result

        # 聚合同一请求内每个样品的锁定量(model 已保证 sample_id 不重复,
        # 聚合是防御性写法)
        wanted: dict[str, float] = {}
        for item in req.items:
            wanted[item.sample_id] = _r(
                wanted.get(item.sample_id, 0.0) + item.amount
            )

        avail = _availability_map(conn, list(wanted.keys()))
        conflicts: list[s.Conflict] = []
        for item in req.items:
            info = avail.get(item.sample_id)
            if info is None:
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {item.sample_id} 不存在,无法预留",
                    reservation_no=req.reservation_no,
                    sample_id=item.sample_id,
                    requested=_r(item.amount),
                ))
                continue
            amount = _r(item.amount)
            if amount > info["available"] + _EPS:
                conflicts.append(_conflict(
                    "insufficient_available",
                    f"样品 {item.sample_id} 可用量 {info['available']} "
                    f"{info['unit']} 不足,本次预留需要 {amount} {info['unit']}"
                    f"(当前余量 {info['remaining']},已预留 {info['reserved']})",
                    reservation_no=req.reservation_no,
                    sample_id=item.sample_id,
                    requested=amount,
                    available=info["available"],
                ))
        if conflicts:
            raise ReservationError(conflicts)

        conn.execute(
            "INSERT INTO reservations (reservation_no, status, payload, note)"
            " VALUES (?, 'active', ?, ?)",
            (req.reservation_no, payload, req.note),
        )
        for idx, item in enumerate(req.items):
            unit = avail[item.sample_id]["unit"]
            conn.execute(
                "INSERT INTO reservation_items"
                " (reservation_no, sample_id, amount, unit, seq_index)"
                " VALUES (?, ?, ?, ?, ?)",
                (req.reservation_no, item.sample_id, _r(item.amount), unit, idx),
            )

        header = conn.execute(
            "SELECT * FROM reservations WHERE reservation_no = ?",
            (req.reservation_no,),
        ).fetchone()
        result = _build_result(conn, header, replayed=False)
        _log_event(
            conn, "reserve", result.model_dump(),
            reservation_no=req.reservation_no,
        )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 释放预留

def release_reservation(
    conn: sqlite3.Connection, reservation_no: str
) -> s.ReleaseResult:
    """整单释放:删除全部锁定(状态置为 released),按预留号幂等。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM reservations WHERE reservation_no = ?",
            (reservation_no,),
        ).fetchone()
        if existing is None:
            raise ReservationError([
                _conflict(
                    "reservation_not_found",
                    f"预留号 {reservation_no} 不存在,无法释放",
                    reservation_no=reservation_no,
                )
            ])

        replayed = existing["status"] == "released"
        if not replayed:
            conn.execute(
                "UPDATE reservations SET status = 'released',"
                " released_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                " WHERE reservation_no = ? AND status = 'active'",
                (reservation_no,),
            )
            existing = conn.execute(
                "SELECT * FROM reservations WHERE reservation_no = ?",
                (reservation_no,),
            ).fetchone()

        built = _build_result(conn, existing, replayed=replayed)
        result = s.ReleaseResult(
            reservation_no=built.reservation_no,
            status="released",
            items=built.items,
            note=built.note,
            replayed=replayed,
            created_at=built.created_at,
            released_at=built.released_at,
        )
        _log_event(
            conn, "release", result.model_dump(),
            reservation_no=reservation_no,
        )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 查询

def get_reservation(
    conn: sqlite3.Connection, reservation_no: str
) -> s.ReservationResult:
    existing = conn.execute(
        "SELECT * FROM reservations WHERE reservation_no = ?",
        (reservation_no,),
    ).fetchone()
    if existing is None:
        raise ReservationError([
            _conflict(
                "reservation_not_found",
                f"预留号 {reservation_no} 不存在",
                reservation_no=reservation_no,
            )
        ])
    return _build_result(conn, existing, replayed=False)


def get_availability(
    conn: sqlite3.Connection, sample_ids: Optional[list[str]] = None
) -> s.AvailabilityResponse:
    """样品当前余量 / 已预留量 / 可用量;sample_ids 为空时返回全部样品。

    查询事件同样持久化到 reservation_events 以便审计。
    """
    conn.execute("BEGIN")
    try:
        if sample_ids:
            ids = list(dict.fromkeys(sample_ids))  # 去重保序
            qmarks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT sample_id FROM samples WHERE sample_id IN ({qmarks})"
                " ORDER BY sample_id",
                ids,
            ).fetchall()
            found = {r["sample_id"] for r in rows}
            missing = [sid for sid in ids if sid not in found]
            if missing:
                raise ReservationError([
                    _conflict(
                        "sample_not_found",
                        f"样品 {', '.join(missing)} 不存在",
                        sample_id=missing[0],
                    )
                ])
        else:
            rows = conn.execute(
                "SELECT sample_id FROM samples ORDER BY sample_id"
            ).fetchall()
            ids = [r["sample_id"] for r in rows]

        avail = _availability_map(conn, ids)
        result = s.AvailabilityResponse(samples=[
            s.SampleAvailability(
                sample_id=sid,
                unit=avail[sid]["unit"],
                remaining=avail[sid]["remaining"],
                reserved=avail[sid]["reserved"],
                available=avail[sid]["available"],
            )
            for sid in ids
        ])
        _log_event(
            conn, "query",
            {"sample_ids": ids, "result": result.model_dump()},
            ref_id=",".join(ids),
        )
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise
