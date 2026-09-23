"""FastAPI 入口:科研样品分装追溯 API。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from . import schemas as s
from . import reservations as rsv
from . import service
from . import stocktakes as st
from .config import BALANCE_TOLERANCE, DB_PATH
from .db import connect, get_conn, init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 确保默认库文件在容器启动时即建好表
    conn = connect(DB_PATH)
    try:
        init_db(conn)
        yield
    finally:
        conn.close()


app = FastAPI(
    title="科研样品分装追溯 API",
    description=(
        "取样 / 分装 / 合并操作的事务化追溯服务:\n"
        "- 每个操作带全局唯一操作号,重复提交幂等返回原结果\n"
        "- 事务内校验父样品余量、禁止谱系环路\n"
        "- 支持批量提交与 dry-run 预校验,冲突返回操作号与批内定位\n"
        "- 按样品回溯完整来源/去向,并提供余量(账本)校验\n"
        "- 预留模块:一个预留号为多个样品分别锁定数量,\n"
        "  单事务校验各样品可用量(余量-未释放预留),全部足够才创建,\n"
        "  支持按预留号幂等重放与整单释放\n"
        "- 盘点模块:按样品编号集合建批保存余量/版本快照,\n"
        "  提交实盘时乐观锁校验余量未被改变并计算差异,\n"
        "  确认时单事务二次校验版本、写盘点调整流水并更新余量\n"
        "  (任一项冲突整批不改库存),按批次号幂等重放"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(service.ConflictError)
async def conflict_handler(_request, exc: service.ConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "conflict",
            "conflicts": [c.model_dump() for c in exc.conflicts],
        },
    )


@app.exception_handler(rsv.ReservationError)
async def reservation_error_handler(
    _request, exc: rsv.ReservationError
) -> JSONResponse:
    code = exc.conflicts[0].code if exc.conflicts else "reservation_error"
    status_code = 404 if code == "reservation_not_found" else 409
    return JSONResponse(
        status_code=status_code,
        content={
            "error": "reservation_not_found" if status_code == 404 else "conflict",
            "conflicts": [c.model_dump() for c in exc.conflicts],
        },
    )


@app.exception_handler(st.StocktakeError)
async def stocktake_error_handler(
    _request, exc: st.StocktakeError
) -> JSONResponse:
    code = exc.conflicts[0].code if exc.conflicts else "stocktake_error"
    status_code = 404 if code == "stocktake_not_found" else 409
    return JSONResponse(
        status_code=status_code,
        content={
            "error": "stocktake_not_found" if status_code == 404 else "conflict",
            "conflicts": [c.model_dump() for c in exc.conflicts],
        },
    )


@app.get("/health", tags=["系统"])
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- 操作提交

def _submit_one(
    conn: sqlite3.Connection, op: s.AnyOp
) -> s.OpResult:
    results = service.submit_batch(conn, [op], index_conflicts=False)
    return results[0]


@app.post(
    "/api/v1/operations",
    response_model=s.OpResult,
    tags=["操作"],
    summary="提交单个取样/分装/合并操作(按 type 区分)",
)
def submit_operation(
    op: s.AnyOp, conn: sqlite3.Connection = Depends(get_conn)
) -> s.OpResult:
    return _submit_one(conn, op)


@app.post(
    "/api/v1/operations/sampling",
    response_model=s.OpResult, tags=["操作"], summary="取样",
)
def submit_sampling(
    op: s.SamplingOp, conn: sqlite3.Connection = Depends(get_conn)
) -> s.OpResult:
    return _submit_one(conn, op)


@app.post(
    "/api/v1/operations/aliquot",
    response_model=s.OpResult, tags=["操作"], summary="分装",
)
def submit_aliquot(
    op: s.AliquotOp, conn: sqlite3.Connection = Depends(get_conn)
) -> s.OpResult:
    return _submit_one(conn, op)


@app.post(
    "/api/v1/operations/merge",
    response_model=s.OpResult, tags=["操作"], summary="合并",
)
def submit_merge(
    op: s.MergeOp, conn: sqlite3.Connection = Depends(get_conn)
) -> s.OpResult:
    return _submit_one(conn, op)


@app.post(
    "/api/v1/batch",
    response_model=s.BatchResponse,
    tags=["操作"],
    summary="批量操作:整批在一个事务中提交,任一冲突全部回滚",
)
def submit_batch(
    body: s.BatchRequest, conn: sqlite3.Connection = Depends(get_conn)
) -> s.BatchResponse:
    results = service.submit_batch(
        conn, body.operations, batch_id=body.batch_id
    )
    return s.BatchResponse(
        batch_id=body.batch_id,
        status=service.batch_status(results),  # type: ignore[arg-type]
        results=results,
    )


# ---------------------------------------------------------------- 预校验

from pydantic import BaseModel  # noqa: E402


class DryRunResponse(BaseModel):
    valid: bool
    conflicts: list[s.Conflict]
    results: list[s.OpResult] = []
    would_commit_status: Optional[str] = None


@app.post(
    "/api/v1/validate",
    response_model=DryRunResponse,
    tags=["校验"],
    summary="预校验单个操作(dry-run,不写库)",
)
def validate_operation(
    op: s.AnyOp, conn: sqlite3.Connection = Depends(get_conn)
) -> DryRunResponse:
    results, conflicts = service.simulate_batch(
        conn, [op], index_conflicts=False
    )
    if conflicts:
        return DryRunResponse(valid=False, conflicts=conflicts)
    return DryRunResponse(
        valid=True,
        conflicts=[],
        results=results or [],
        would_commit_status=service.batch_status(results or []),
    )


@app.post(
    "/api/v1/batch/validate",
    response_model=DryRunResponse,
    tags=["校验"],
    summary="预校验批量操作(dry-run,不写库),返回所有可静态发现的冲突及批内下标",
)
def validate_batch(
    body: s.BatchRequest, conn: sqlite3.Connection = Depends(get_conn)
) -> DryRunResponse:
    results, conflicts = service.simulate_batch(
        conn, body.operations, batch_id=body.batch_id
    )
    if conflicts:
        return DryRunResponse(valid=False, conflicts=conflicts)
    return DryRunResponse(
        valid=True,
        conflicts=[],
        results=results or [],
        would_commit_status=service.batch_status(results or []),
    )


# ---------------------------------------------------------------- 查询

@app.get(
    "/api/v1/samples", tags=["查询"], summary="样品列表"
)
def list_samples(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    rows = conn.execute(
        "SELECT * FROM samples ORDER BY sample_id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) AS c FROM samples").fetchone()["c"]
    return {
        "total": total,
        "items": [
            {
                "sample_id": r["sample_id"],
                "material": r["material"],
                "unit": r["unit"],
                "initial_amount": r["initial_amount"],
                "remaining": r["remaining"],
                "location": r["location"],
                "is_root": bool(r["is_root"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.get(
    "/api/v1/samples/{sample_id}/trace",
    response_model=s.TraceResponse,
    tags=["查询"],
    summary="回溯完整来源(upstream)/去向(downstream)/双向(both)",
)
def trace_sample(
    sample_id: str,
    direction: str = Query("both", pattern="^(upstream|downstream|both)$"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.TraceResponse:
    return service.trace_sample(conn, sample_id, direction)


@app.get(
    "/api/v1/samples/{sample_id}/balance",
    response_model=s.CheckResponse,
    tags=["校验"],
    summary="单个样品余量校验:冗余余量 vs 出入库流水重算",
)
def check_sample_balance(
    sample_id: str,
    tolerance: float = Query(BALANCE_TOLERANCE, gt=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.CheckResponse:
    b = service.get_balance(conn, sample_id, tolerance)
    conflicts = []
    if not b.consistent:
        conflicts.append(s.Conflict(
            code="balance_inconsistent",
            message=(
                f"样品 {b.sample_id} 冗余余量 {b.stored_remaining} 与账本重算 "
                f"{b.ledger_remaining} 不一致(容差 {tolerance})"
            ),
            sample_id=b.sample_id,
            available=b.stored_remaining,
            requested=b.ledger_remaining,
        ))
    return s.CheckResponse(
        sample_id=sample_id,
        consistent=b.consistent,
        tolerance=tolerance,
        samples=[b],
        conflicts=conflicts,
    )


@app.get(
    "/api/v1/checks/balances",
    response_model=s.CheckResponse,
    tags=["校验"],
    summary="全量余量一致性校验,返回所有冲突样品及定位",
)
def check_all(
    tolerance: float = Query(BALANCE_TOLERANCE, gt=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.CheckResponse:
    balances, conflicts = service.check_all_balances(conn, tolerance)
    return s.CheckResponse(
        sample_id=None,
        consistent=not conflicts,
        tolerance=tolerance,
        samples=balances,
        conflicts=conflicts,
    )


@app.get(
    "/api/v1/operations/{op_no}",
    tags=["查询"],
    summary="按操作号查询原始操作结果(幂等回放内容)",
)
def get_operation(
    op_no: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    row = conn.execute(
        "SELECT * FROM operations WHERE op_no = ?", (op_no,)
    ).fetchone()
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"操作号 {op_no} 不存在"},
        )
    return {
        "op_no": row["op_no"],
        "type": row["op_type"],
        "payload": json.loads(row["payload"]),
        "effect": json.loads(row["effect"]),
        "batch_id": row["batch_id"],
        "seq_index": row["seq_index"],
        "location": row["location"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 预留

@app.post(
    "/api/v1/reservations",
    response_model=s.ReservationResult,
    tags=["预留"],
    summary="创建预留:一个预留号为多个样品分别锁定数量,全部足够才一次创建",
)
def create_reservation(
    body: s.ReservationRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.ReservationResult:
    return rsv.create_reservation(conn, body)


@app.get(
    "/api/v1/reservations/{reservation_no}",
    response_model=s.ReservationResult,
    tags=["预留"],
    summary="按预留号查询预留单及各样品当前余量/已预留/可用量",
)
def get_reservation(
    reservation_no: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.ReservationResult:
    return rsv.get_reservation(conn, reservation_no)


@app.delete(
    "/api/v1/reservations/{reservation_no}",
    response_model=s.ReleaseResult,
    tags=["预留"],
    summary="主动释放整单预留(幂等:重复释放返回原释放结果)",
)
def release_reservation(
    reservation_no: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.ReleaseResult:
    return rsv.release_reservation(conn, reservation_no)


@app.get(
    "/api/v1/reservations",
    response_model=s.AvailabilityResponse,
    tags=["预留"],
    summary="样品可用量查询:当前余量、已预留量与可用量(可用量 = 余量 - 未释放预留)",
)
def query_availability(
    sample_id: Optional[list[str]] = Query(
        default=None, description="可重复传入,如 ?sample_id=S1&sample_id=S2;不传返回全部"
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.AvailabilityResponse:
    return rsv.get_availability(conn, sample_id)


# ---------------------------------------------------------------- 盘点

@app.post(
    "/api/v1/stocktakes",
    response_model=s.StocktakeResponse,
    tags=["盘点"],
    summary="新增盘点批次:单事务保存样品集合的余量/版本快照(按批次号幂等)",
)
def create_stocktake(
    body: s.StocktakeRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.StocktakeResponse:
    return st.create_stocktake(conn, body)


@app.post(
    "/api/v1/stocktakes/{stocktake_no}/submit",
    response_model=s.StocktakeResponse,
    tags=["盘点"],
    summary="提交实盘结果:逐项校验样品仍存在且余量未被改变,计算系统量与实盘量差异",
)
def submit_stocktake(
    stocktake_no: str,
    body: s.StocktakeSubmitRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.StocktakeResponse:
    return st.submit_stocktake(conn, stocktake_no, body)


@app.post(
    "/api/v1/stocktakes/{stocktake_no}/confirm",
    response_model=s.StocktakeResponse,
    tags=["盘点"],
    summary="确认盘点:事务内二次校验版本,写调整流水并更新余量(任一冲突整批不改)",
)
def confirm_stocktake(
    stocktake_no: str,
    body: s.StocktakeConfirmRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.StocktakeResponse:
    return st.confirm_stocktake(conn, stocktake_no, body)


@app.get(
    "/api/v1/stocktakes/{stocktake_no}",
    response_model=s.StocktakeResponse,
    tags=["盘点"],
    summary="查询盘点批次:返回快照、实盘/差异、当前版本漂移及确认状态与调整流水",
)
def get_stocktake(
    stocktake_no: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> s.StocktakeResponse:
    return st.get_stocktake(conn, stocktake_no)
