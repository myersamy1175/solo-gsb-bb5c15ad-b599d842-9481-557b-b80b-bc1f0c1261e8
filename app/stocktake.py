"""盘点服务:余量快照、实盘差异、确认调整。

工作流:
1. create_batch  :按样品编号集合在同一事务保存「盘点时」余量/版本快照
                   (状态 open);批次号幂等,同请求重放、不同内容 409。
2. submit_results :逐项提交实盘量,校验样品仍存在且快照版本未被其他操作改变;
                   通过则把实盘量与当时系统量落库(状态 counted),计算差异。
3. confirm_batch  :在一个事务内再次校验版本;全部无冲突时逐项写入
                   stocktake_adjustments 调整流水(带操作者/原因),
                   按差异更新样品余量并递增版本(状态 confirmed)。
                   任一项冲突则整批回滚,库存不变。
4. get_batch      :返回快照、实盘、系统量、差异与确认状态。

乐观并发以 samples.version 为准:取样/分装/合并等一切余量写操作都会令版本 +1,
盘点依据「快照版本 == 当前版本」判断数据库余量是否被其他操作改变。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import schemas as s

_EPS = 1e-9  # 运算时浮点比较


class StocktakeError(Exception):
    """盘点业务冲突,携带结构化冲突列表。"""

    def __init__(self, conflicts: list[s.Conflict]):
        self.conflicts = conflicts
        super().__init__(conflicts[0].message if conflicts else "stocktake conflict")


def _r(x: float) -> float:
    """统一保留 9 位小数,消除浮点尾巴。"""
    return round(float(x), 9)


def _conflict(
    code: str,
    message: str,
    *,
    batch_no: Optional[str] = None,
    sample_id: Optional[str] = None,
    requested: Optional[float] = None,
    available: Optional[float] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=batch_no,
        sample_id=sample_id,
        requested=requested,
        available=available,
    )


# ---------------------------------------------------------------- 组装响应

def _load_header(
    conn: sqlite3.Connection, batch_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stocktake_batches WHERE batch_no = ?", (batch_no,)
    ).fetchone()


def _load_items(
    conn: sqlite3.Connection, batch_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stocktake_items WHERE batch_no = ?"
        " ORDER BY seq_index, sample_id",
        (batch_no,),
    ).fetchall()


def _get_sample(
    conn: sqlite3.Connection, sample_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT sample_id, unit, remaining, version FROM samples"
        " WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()


def _build_response(
    conn: sqlite3.Connection, header: sqlite3.Row, *, replayed: bool
) -> s.StocktakeBatchResponse:
    items_out: list[s.StocktakeItemOut] = []
    for row in _load_items(conn, header["batch_no"]):
        smp = _get_sample(conn, row["sample_id"])
        current_remaining = _r(smp["remaining"]) if smp is not None else None
        current_version = smp["version"] if smp is not None else None

        counted = (
            _r(row["counted_amount"])
            if row["counted_amount"] is not None
            else None
        )
        if counted is not None:
            # 提交后系统量按提交时记录的数据库余量给出;variance 与之配对,
            # changed_since_snapshot 表示建批次快照到提交之间余量是否曾被改动
            system_remaining = _r(row["system_remaining"])
            variance = _r(counted - system_remaining)
            changed = bool(row["snapshot_version"] != row["counted_version"])
        else:
            system_remaining = None
            variance = None
            changed = None

        final_remaining: Optional[float] = None
        if header["status"] == "confirmed":
            final_remaining = current_remaining

        items_out.append(s.StocktakeItemOut(
            sample_id=row["sample_id"],
            unit=row["unit"],
            snapshot_remaining=_r(row["snapshot_remaining"]),
            snapshot_version=row["snapshot_version"],
            current_remaining=current_remaining,
            current_version=current_version,
            counted_amount=counted,
            system_remaining=system_remaining,
            system_version=(
                row["counted_version"]
                if row["counted_version"] is not None
                else None
            ),
            variance=variance,
            changed_since_snapshot=changed,
            final_remaining=final_remaining,
        ))

    return s.StocktakeBatchResponse(
        batch_no=header["batch_no"],
        status=header["status"],  # type: ignore[arg-type]
        items=items_out,
        note=header["note"],
        operator=header["operator"],
        reason=header["reason"],
        replayed=replayed,
        created_at=header["created_at"],
        counted_at=header["counted_at"],
        confirmed_at=header["confirmed_at"],
    )


# ---------------------------------------------------------------- 1. 新建批次

def create_batch(
    conn: sqlite3.Connection, req: s.StocktakeBatchRequest
) -> s.StocktakeBatchResponse:
    """同一事务内校验样品全部存在,并保存各样品盘点时的余量/版本快照。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _load_header(conn, req.batch_no)
        if existing is not None:
            if existing["payload"] != payload:
                raise StocktakeError([
                    _conflict(
                        "stocktake_batch_conflict",
                        f"盘点批次号 {req.batch_no} 已存在但请求内容不同,"
                        "同一批次号必须对应同一次盘点(幂等键冲突)",
                        batch_no=req.batch_no,
                    )
                ])
            result = _build_response(conn, existing, replayed=True)
            conn.execute("COMMIT")
            return result

        sample_ids = list(dict.fromkeys(req.sample_ids))  # 去重保序(防御性)
        conflicts: list[s.Conflict] = []
        for sid in sample_ids:
            if _get_sample(conn, sid) is None:
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {sid} 不存在,无法纳入盘点批次",
                    batch_no=req.batch_no, sample_id=sid,
                ))
        if conflicts:
            raise StocktakeError(conflicts)

        conn.execute(
            "INSERT INTO stocktake_batches (batch_no, status, payload, note)"
            " VALUES (?, 'open', ?, ?)",
            (req.batch_no, payload, req.note),
        )
        for idx, sid in enumerate(sample_ids):
            smp = _get_sample(conn, sid)
            conn.execute(
                "INSERT INTO stocktake_items"
                " (batch_no, sample_id, seq_index, unit,"
                " snapshot_remaining, snapshot_version)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (req.batch_no, sid, idx, smp["unit"],
                 _r(smp["remaining"]), smp["version"]),
            )

        header = _load_header(conn, req.batch_no)
        result = _build_response(conn, header, replayed=False)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 2. 提交实盘结果

def submit_results(
    conn: sqlite3.Connection, req: s.StocktakeCountRequest
) -> s.StocktakeBatchResponse:
    """逐项校验样品仍存在且快照版本未变;记录实盘量与当时系统量,计算差异。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        header = _load_header(conn, req.batch_no)
        if header is None:
            raise StocktakeError([
                _conflict(
                    "stocktake_batch_not_found",
                    f"盘点批次 {req.batch_no} 不存在",
                    batch_no=req.batch_no,
                )
            ])
        if header["status"] == "confirmed":
            raise StocktakeError([
                _conflict(
                    "stocktake_batch_conflict",
                    f"盘点批次 {req.batch_no} 已确认,不能再次提交实盘结果",
                    batch_no=req.batch_no,
                )
            ])

        want = [it for it in req.items]
        want_ids = {it.sample_id for it in want}
        stored_ids = {r["sample_id"] for r in _load_items(conn, req.batch_no)}
        if want_ids != stored_ids:
            raise StocktakeError([
                _conflict(
                    "stocktake_items_mismatch",
                    f"提交样品集合与批次 {req.batch_no} 快照不一致:"
                    f"批次为 {sorted(stored_ids)},提交为 {sorted(want_ids)}",
                    batch_no=req.batch_no,
                )
            ])

        # 已提交过:同体重放(可能是网络重试),异体冲突
        if header["status"] == "counted":
            if header["count_payload"] != payload:
                raise StocktakeError([
                    _conflict(
                        "stocktake_batch_conflict",
                        f"盘点批次 {req.batch_no} 已提交过不同的实盘结果,"
                        "同一批次号必须对应同一次提交(幂等键冲突)",
                        batch_no=req.batch_no,
                    )
                ])
            result = _build_response(conn, header, replayed=True)
            conn.execute("COMMIT")
            return result

        # 逐项校验:样品仍存在,且余量版本与建批次时的快照一致
        conflicts: list[s.Conflict] = []
        for it in want:
            item = conn.execute(
                "SELECT * FROM stocktake_items WHERE batch_no = ? AND sample_id = ?",
                (req.batch_no, it.sample_id),
            ).fetchone()
            smp = _get_sample(conn, it.sample_id)
            if smp is None:
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {it.sample_id} 已不存在,无法提交盘点结果",
                    batch_no=req.batch_no, sample_id=it.sample_id,
                ))
                continue
            if smp["version"] != item["snapshot_version"]:
                conflicts.append(_conflict(
                    "stocktake_version_conflict",
                    f"样品 {it.sample_id} 余量版本已变化:"
                    f"快照版本 {item['snapshot_version']},"
                    f"当前版本 {smp['version']},"
                    "数据库余量已被其他操作改变,请重建盘点批次后重试",
                    batch_no=req.batch_no, sample_id=it.sample_id,
                    requested=_r(item["snapshot_remaining"]),
                    available=_r(smp["remaining"]),
                ))
        if conflicts:
            raise StocktakeError(conflicts)

        # 全部通过:记录实盘量、提交时的系统量与版本
        for it in want:
            smp = _get_sample(conn, it.sample_id)
            conn.execute(
                "UPDATE stocktake_items SET counted_amount = ?,"
                " counted_version = ?, system_remaining = ?,"
                " submitted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                " WHERE batch_no = ? AND sample_id = ?",
                (_r(it.counted_amount), smp["version"], _r(smp["remaining"]),
                 req.batch_no, it.sample_id),
            )
        conn.execute(
            "UPDATE stocktake_batches SET status = 'counted', count_payload = ?,"
            " counted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE batch_no = ?",
            (payload, req.batch_no),
        )

        header = _load_header(conn, req.batch_no)
        result = _build_response(conn, header, replayed=False)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 3. 确认批次

def confirm_batch(
    conn: sqlite3.Connection, req: s.StocktakeConfirmRequest
) -> s.StocktakeBatchResponse:
    """单事务再次校验版本,写调整流水并按差异更新样品余量;任一冲突整批回滚。"""
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        header = _load_header(conn, req.batch_no)
        if header is None:
            raise StocktakeError([
                _conflict(
                    "stocktake_batch_not_found",
                    f"盘点批次 {req.batch_no} 不存在",
                    batch_no=req.batch_no,
                )
            ])
        if header["status"] == "open":
            raise StocktakeError([
                _conflict(
                    "stocktake_batch_conflict",
                    f"盘点批次 {req.batch_no} 尚未提交实盘结果,不能确认",
                    batch_no=req.batch_no,
                )
            ])

        if header["status"] == "confirmed":
            # 同操作者/原因重放返回首次确认结果;不同内容视为幂等键冲突
            if header["confirm_payload"] != payload:
                raise StocktakeError([
                    _conflict(
                        "stocktake_batch_conflict",
                        f"盘点批次 {req.batch_no} 已确认但请求内容不同,"
                        "同一批次号必须对应同一次确认(幂等键冲突)",
                        batch_no=req.batch_no,
                    )
                ])
            result = _build_response(conn, header, replayed=True)
            conn.execute("COMMIT")
            return result

        # 事务内逐项再次校验:样品仍在、余量版本与建批次快照一致
        item_rows = _load_items(conn, req.batch_no)
        conflicts: list[s.Conflict] = []
        for row in item_rows:
            smp = _get_sample(conn, row["sample_id"])
            if smp is None:
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {row['sample_id']} 已不存在,无法确认盘点调整",
                    batch_no=req.batch_no, sample_id=row["sample_id"],
                ))
                continue
            if smp["version"] != row["snapshot_version"]:
                conflicts.append(_conflict(
                    "stocktake_version_conflict",
                    f"样品 {row['sample_id']} 余量版本已变化:"
                    f"快照版本 {row['snapshot_version']},"
                    f"当前版本 {smp['version']},"
                    "确认前数据库余量已被其他操作改变,本次盘点整批不调整库存",
                    batch_no=req.batch_no, sample_id=row["sample_id"],
                    requested=_r(row["snapshot_remaining"]),
                    available=_r(smp["remaining"]),
                ))
        if conflicts:
            raise StocktakeError(conflicts)

        # 全部无冲突:逐项写调整流水并更新余量(差异为 0 也留痕)
        for row in item_rows:
            smp = _get_sample(conn, row["sample_id"])
            system_remaining = _r(smp["remaining"])
            counted = _r(row["counted_amount"])
            variance = _r(counted - system_remaining)

            conn.execute(
                "INSERT INTO stocktake_adjustments"
                " (batch_no, sample_id, unit, snapshot_remaining,"
                " system_remaining, counted_amount, variance, operator, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (req.batch_no, row["sample_id"], row["unit"],
                 _r(row["snapshot_remaining"]), system_remaining,
                 counted, variance, req.operator, req.reason),
            )
            if abs(variance) >= _EPS:
                conn.execute(
                    "UPDATE samples SET remaining = ?,"
                    " version = version + 1 WHERE sample_id = ?",
                    (counted, row["sample_id"]),
                )
            # 回写提交时系统量,响应中差异与确认时点一致
            conn.execute(
                "UPDATE stocktake_items SET system_remaining = ?,"
                " counted_version = ? WHERE batch_no = ? AND sample_id = ?",
                (system_remaining, smp["version"], req.batch_no, row["sample_id"]),
            )

        conn.execute(
            "UPDATE stocktake_batches SET status = 'confirmed',"
            " confirm_payload = ?, operator = ?, reason = ?,"
            " confirmed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE batch_no = ?",
            (payload, req.operator, req.reason, req.batch_no),
        )

        header = _load_header(conn, req.batch_no)
        result = _build_response(conn, header, replayed=False)
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 4. 查询

def get_batch(
    conn: sqlite3.Connection, batch_no: str
) -> s.StocktakeBatchResponse:
    header = _load_header(conn, batch_no)
    if header is None:
        raise StocktakeError([
            _conflict(
                "stocktake_batch_not_found",
                f"盘点批次 {batch_no} 不存在",
                batch_no=batch_no,
            )
        ])
    return _build_response(conn, header, replayed=False)
