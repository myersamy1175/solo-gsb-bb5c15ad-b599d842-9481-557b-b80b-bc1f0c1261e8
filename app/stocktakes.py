"""盘点服务:快照建批 → 提交实盘结果(逐项版本校验+差异) → 确认调账。

工作流
------
1. 建批(open):按样品编号集合,在单个 `BEGIN IMMEDIATE` 事务内逐项保存
   「系统余量 / 单位 / 版本」快照。样品必须全部存在,否则整批不建。
2. 提交实盘(submitted):逐项校验样品仍存在、且数据库余量自快照以来未被
   其他操作改变(samples.version 乐观锁,余量变动即 +1),计算
   差异 = 实盘量 − 系统量。本步不改任何库存。
3. 确认(confirmed):在一个事务内对每一项**再次**校验版本仍等于提交时版本;
   全部通过才把确认的实盘量写入盘点调整流水(stocktake_adjustments)、
   更新样品余量(并再 +1 版本),同时记录操作者与原因。
   任一项冲突则整批回滚,库存一律不动。

幂等
----
- 建批:同批次号 + 同请求体重放返回当前批次结果(`replayed: true`);
  同号不同内容返回 `409 stocktake_no_conflict`。
- 提交:submitted 状态下同请求体重放;不同内容 `409 stocktake_result_conflict`。
- 确认:confirmed 状态下重复确认幂等回放原调整结果。
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
    stocktake_no: Optional[str] = None,
    sample_id: Optional[str] = None,
    requested: Optional[float] = None,
    available: Optional[float] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=stocktake_no,
        sample_id=sample_id,
        requested=requested,
        available=available,
    )


def _get_header(
    conn: sqlite3.Connection, stocktake_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stocktakes WHERE stocktake_no = ?", (stocktake_no,)
    ).fetchone()


def _load_items(
    conn: sqlite3.Connection, stocktake_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stocktake_items WHERE stocktake_no = ?"
        " ORDER BY seq_index, item_id",
        (stocktake_no,),
    ).fetchall()


def _load_adjustments(
    conn: sqlite3.Connection, stocktake_no: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stocktake_adjustments WHERE stocktake_no = ?"
        " ORDER BY adjustment_id",
        (stocktake_no,),
    ).fetchall()


def _build_result(
    conn: sqlite3.Connection,
    header: sqlite3.Row,
    *,
    replayed: bool,
) -> s.StocktakeResponse:
    """组装批次结果:明细附带快照、实盘/差异以及查询时刻的余量版本视图。"""
    rows = _load_items(conn, header["stocktake_no"])
    sample_ids = [r["sample_id"] for r in rows]
    current: dict[str, sqlite3.Row] = {}
    if sample_ids:
        qmarks = ",".join("?" * len(sample_ids))
        current = {
            r["sample_id"]: r
            for r in conn.execute(
                f"SELECT sample_id, remaining, version FROM samples"
                f" WHERE sample_id IN ({qmarks})",
                sample_ids,
            ).fetchall()
        }
    applied: dict[str, sqlite3.Row] = {
        r["sample_id"]: r for r in _load_adjustments(conn, header["stocktake_no"])
    }

    items: list[s.StocktakeItemOut] = []
    for r in rows:
        cur = current.get(r["sample_id"])
        drift = cur is not None and (
            cur["version"] != r["snapshot_version"]
            or abs(_r(cur["remaining"]) - _r(r["snapshot_remaining"])) > _EPS
        )
        adj = applied.get(r["sample_id"])
        items.append(s.StocktakeItemOut(
            sample_id=r["sample_id"],
            unit=r["snapshot_unit"],
            snapshot_remaining=_r(r["snapshot_remaining"]),
            snapshot_version=r["snapshot_version"],
            counted_amount=None if r["counted_amount"] is None
            else _r(r["counted_amount"]),
            difference=None if r["difference"] is None else _r(r["difference"]),
            submitted_version=r["submitted_version"],
            current_remaining=None if cur is None else _r(cur["remaining"]),
            current_version=None if cur is None else cur["version"],
            version_drift=bool(drift),
            delta_applied=None if adj is None else _r(adj["delta"]),
        ))

    adjustments = [
        s.StocktakeAdjustmentOut(
            sample_id=r["sample_id"],
            unit=r["unit"],
            system_amount=_r(r["system_amount"]),
            counted_amount=_r(r["counted_amount"]),
            delta=_r(r["delta"]),
            operator=r["operator"],
            reason=r["reason"],
            from_version=r["from_version"],
            to_version=r["to_version"],
            created_at=r["created_at"],
        )
        for r in applied.values()
    ]

    return s.StocktakeResponse(
        stocktake_no=header["stocktake_no"],
        status=header["status"],  # type: ignore[arg-type]
        items=items,
        note=header["note"],
        operator=header["operator"],
        reason=header["reason"],
        confirm_operator=header["operator"] if header["status"] == "confirmed"
        else None,
        replayed=replayed,
        adjustments=adjustments,
        created_at=header["created_at"],
        submitted_at=header["submitted_at"],
        confirmed_at=header["confirmed_at"],
    )


# ---------------------------------------------------------------- 建批

def create_stocktake(
    conn: sqlite3.Connection, req: s.StocktakeRequest
) -> s.StocktakeResponse:
    """同一事务内为样品集合保存余量/版本快照;样品不存在则整批不建。"""
    payload = req.model_dump_json()

    # 批内样品号去重(model 已保证,防御性聚合)
    sample_ids = list(dict.fromkeys(i.sample_id for i in req.items))

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _get_header(conn, req.stocktake_no)
        if existing is not None:
            if existing["payload"] != payload:
                raise StocktakeError([
                    _conflict(
                        "stocktake_no_conflict",
                        f"盘点批次号 {req.stocktake_no} 已存在但请求内容不同,"
                        "同一批次号必须对应同一次盘点(幂等键冲突)",
                        stocktake_no=req.stocktake_no,
                    )
                ])
            result = _build_result(conn, existing, replayed=True)
            conn.execute("COMMIT")
            return result

        qmarks = ",".join("?" * len(sample_ids))
        sample_rows = {
            r["sample_id"]: r
            for r in conn.execute(
                f"SELECT sample_id, remaining, version, unit FROM samples"
                f" WHERE sample_id IN ({qmarks})",
                sample_ids,
            ).fetchall()
        }
        conflicts = [
            _conflict(
                "sample_not_found",
                f"样品 {sid} 不存在,无法纳入盘点批次",
                stocktake_no=req.stocktake_no,
                sample_id=sid,
            )
            for sid in sample_ids if sid not in sample_rows
        ]
        if conflicts:
            raise StocktakeError(conflicts)

        conn.execute(
            "INSERT INTO stocktakes (stocktake_no, status, payload, note, operator)"
            " VALUES (?, 'open', ?, ?, ?)",
            (req.stocktake_no, payload, req.note, req.operator),
        )
        for idx, sid in enumerate(sample_ids):
            row = sample_rows[sid]
            conn.execute(
                "INSERT INTO stocktake_items (stocktake_no, sample_id, seq_index,"
                " snapshot_remaining, snapshot_unit, snapshot_version)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (req.stocktake_no, sid, idx,
                 _r(row["remaining"]), row["unit"], row["version"]),
            )

        header = _get_header(conn, req.stocktake_no)
        result = _build_result(conn, header, replayed=False)  # type: ignore[arg-type]
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 提交实盘

def submit_stocktake(
    conn: sqlite3.Connection,
    stocktake_no: str,
    req: s.StocktakeSubmitRequest,
) -> s.StocktakeResponse:
    """逐项校验样品仍存在且余量未被其他操作改变,计算差异;不改库存。

    提交内容必须与批次明细**逐项一致**(同样品集合,不重不漏)。
    """
    payload = req.model_dump_json()

    conn.execute("BEGIN IMMEDIATE")
    try:
        header = _get_header(conn, stocktake_no)
        if header is None:
            raise StocktakeError([
                _conflict(
                    "stocktake_not_found",
                    f"盘点批次 {stocktake_no} 不存在,无法提交实盘结果",
                    stocktake_no=stocktake_no,
                )
            ])
        if header["status"] == "confirmed":
            raise StocktakeError([
                _conflict(
                    "stocktake_closed",
                    f"盘点批次 {stocktake_no} 已确认,不能再次提交实盘结果",
                    stocktake_no=stocktake_no,
                )
            ])

        items = _load_items(conn, stocktake_no)
        by_id = {r["sample_id"]: r for r in items}
        counted = {i.sample_id: _r(i.counted_amount) for i in req.items}

        if header["status"] == "submitted":
            # 已提交:只允许同请求体幂等重放
            if header["submit_payload"] != payload:
                raise StocktakeError([
                    _conflict(
                        "stocktake_result_conflict",
                        f"盘点批次 {stocktake_no} 已提交过不同的实盘结果,"
                        "同批次只能重放相同提交(幂等键冲突)",
                        stocktake_no=stocktake_no,
                    )
                ])
            result = _build_result(conn, header, replayed=True)
            conn.execute("COMMIT")
            return result

        # open:校验样品集合逐项一致(不重不漏;重样在 Pydantic 层已拒)
        conflicts: list[s.Conflict] = []
        missing = [sid for sid in by_id if sid not in counted]
        extra = [sid for sid in counted if sid not in by_id]
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"缺少样品 {', '.join(missing)}")
            if extra:
                detail.append(f"批次外样品 {', '.join(extra)}")
            conflicts.append(_conflict(
                "stocktake_items_mismatch",
                f"实盘明细与盘点批次快照不一致:{'；'.join(detail)}",
                stocktake_no=stocktake_no,
                sample_id=(missing + extra)[0],
            ))

        # 逐项版本校验:样品仍存在 + 余量自快照以来未被其他操作改变
        for sid, amount in counted.items():
            snap = by_id.get(sid)
            if snap is None:
                continue
            cur = conn.execute(
                "SELECT remaining, version FROM samples WHERE sample_id = ?",
                (sid,),
            ).fetchone()
            if cur is None:
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {sid} 在提交实盘时已不存在",
                    stocktake_no=stocktake_no,
                    sample_id=sid,
                ))
                continue
            if cur["version"] != snap["snapshot_version"] or abs(
                _r(cur["remaining"]) - _r(snap["snapshot_remaining"])
            ) > _EPS:
                conflicts.append(_conflict(
                    "stocktake_version_conflict",
                    f"样品 {sid} 的数据库余量自盘点快照后已被其他操作改变:"
                    f"快照 {_r(snap['snapshot_remaining'])}"
                    f"(v{snap['snapshot_version']}) → 当前 {_r(cur['remaining'])}"
                    f"(v{cur['version']}),请重建盘点批次后再提交",
                    stocktake_no=stocktake_no,
                    sample_id=sid,
                    requested=_r(snap["snapshot_remaining"]),
                    available=_r(cur["remaining"]),
                ))
        if conflicts:
            raise StocktakeError(conflicts)

        # 全部通过:写实盘量与差异(差异 = 实盘量 − 提交时系统量),仍不动库存
        for sid, amount in counted.items():
            snap = by_id[sid]
            cur = conn.execute(
                "SELECT remaining, version FROM samples WHERE sample_id = ?",
                (sid,),
            ).fetchone()
            conn.execute(
                "UPDATE stocktake_items SET counted_amount = ?,"
                " difference = ?, submitted_version = ? WHERE item_id = ?",
                (amount, _r(amount - _r(cur["remaining"])),
                 cur["version"], snap["item_id"]),
            )

        conn.execute(
            "UPDATE stocktakes SET status = 'submitted', submit_payload = ?,"
            " submitted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),"
            " operator = COALESCE(?, operator) WHERE stocktake_no = ?",
            (payload, req.operator, stocktake_no),
        )
        if req.note is not None:
            conn.execute(
                "UPDATE stocktakes SET note = ? WHERE stocktake_no = ?",
                (req.note, stocktake_no),
            )

        header = _get_header(conn, stocktake_no)
        result = _build_result(conn, header, replayed=False)  # type: ignore[arg-type]
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 确认调账

def confirm_stocktake(
    conn: sqlite3.Connection,
    stocktake_no: str,
    req: s.StocktakeConfirmRequest,
) -> s.StocktakeResponse:
    """事务内二次校验版本,写调整流水并更新余量;任一冲突整批不改库存。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        header = _get_header(conn, stocktake_no)
        if header is None:
            raise StocktakeError([
                _conflict(
                    "stocktake_not_found",
                    f"盘点批次 {stocktake_no} 不存在,无法确认",
                    stocktake_no=stocktake_no,
                )
            ])
        if header["status"] == "confirmed":
            # 已确认:相同操作者+原因的重复确认幂等回放;不同内容视为冲突
            if header["operator"] == req.operator and header["reason"] == req.reason:
                result = _build_result(conn, header, replayed=True)
                conn.execute("COMMIT")
                return result
            raise StocktakeError([
                _conflict(
                    "stocktake_already_confirmed",
                    f"盘点批次 {stocktake_no} 已确认(操作者 {header['operator']},"
                    f"原因 {header['reason']}),不能以不同操作者/原因再次确认",
                    stocktake_no=stocktake_no,
                )
            ])
        if header["status"] == "open":
            raise StocktakeError([
                _conflict(
                    "stocktake_not_submitted",
                    f"盘点批次 {stocktake_no} 尚未提交实盘结果,不能确认",
                    stocktake_no=stocktake_no,
                )
            ])

        items = _load_items(conn, stocktake_no)

        # 1) 事务内逐项再次校验:样品仍存在 + 版本仍等于提交时版本
        #    (即提交→确认之间数据库余量没有被任何其他操作改变)
        conflicts: list[s.Conflict] = []
        current: dict[str, sqlite3.Row] = {}
        for it in items:
            cur = conn.execute(
                "SELECT remaining, version, unit FROM samples WHERE sample_id = ?",
                (it["sample_id"],),
            ).fetchone()
            if cur is None:
                conflicts.append(_conflict(
                    "sample_not_found",
                    f"样品 {it['sample_id']} 在确认盘点时已不存在",
                    stocktake_no=stocktake_no,
                    sample_id=it["sample_id"],
                ))
                continue
            current[it["sample_id"]] = cur
            if cur["version"] != it["submitted_version"]:
                conflicts.append(_conflict(
                    "stocktake_version_conflict",
                    f"样品 {it['sample_id']} 的数据库余量在提交盘点后已被其他操作改变:"
                    f"提交时 v{it['submitted_version']} → 确认时 v{cur['version']},"
                    "整批拒绝调整,请重建盘点批次",
                    stocktake_no=stocktake_no,
                    sample_id=it["sample_id"],
                    requested=it["submitted_version"],
                    available=cur["version"],
                ))
        if conflicts:
            raise StocktakeError(conflicts)

        # 2) 全部通过:逐项写盘点调整流水并更新余量(有差异才改余量/递增版本)
        for it in items:
            cur = current[it["sample_id"]]
            system_amount = _r(cur["remaining"])
            counted_amount = _r(it["counted_amount"])
            delta = _r(counted_amount - system_amount)
            from_version = cur["version"]
            to_version = from_version + (1 if abs(delta) > _EPS else 0)
            conn.execute(
                "INSERT INTO stocktake_adjustments (stocktake_no, sample_id,"
                " system_amount, counted_amount, delta, unit, operator, reason,"
                " from_version, to_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stocktake_no, it["sample_id"], system_amount, counted_amount,
                 delta, cur["unit"], req.operator, req.reason,
                 from_version, to_version),
            )
            if abs(delta) > _EPS:
                conn.execute(
                    "UPDATE samples SET remaining = ?, version = version + 1"
                    " WHERE sample_id = ?",
                    (counted_amount, it["sample_id"]),
                )

        conn.execute(
            "UPDATE stocktakes SET status = 'confirmed',"
            " reason = ?, operator = ?,"
            " confirmed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE stocktake_no = ?",
            (req.reason, req.operator, stocktake_no),
        )

        header = _get_header(conn, stocktake_no)
        result = _build_result(conn, header, replayed=False)  # type: ignore[arg-type]
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 查询

def get_stocktake(
    conn: sqlite3.Connection, stocktake_no: str
) -> s.StocktakeResponse:
    header = _get_header(conn, stocktake_no)
    if header is None:
        raise StocktakeError([
            _conflict(
                "stocktake_not_found",
                f"盘点批次 {stocktake_no} 不存在",
                stocktake_no=stocktake_no,
            )
        ])
    return _build_result(conn, header, replayed=False)
