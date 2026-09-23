"""领域服务:事务提交、余量校验、环路检测、幂等回放、谱系回溯。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import schemas as s

_EPS = 1e-9  # 运算时浮点比较
_CYCLE_LIMIT = 100000  # 谱系遍历节点上限,防御性保护


class ConflictError(Exception):
    """业务冲突,携带结构化冲突列表(通常一条,批量预校验可能多条)。"""

    def __init__(self, conflicts: list[s.Conflict]):
        self.conflicts = conflicts
        super().__init__(conflicts[0].message if conflicts else "conflict")


# ---------------------------------------------------------------- 基础工具

def _r(x: float) -> float:
    """统一保留 9 位小数,消除浮点尾巴。"""
    return round(float(x), 9)


def _conflict(
    code: str,
    message: str,
    *,
    op: Optional[Any] = None,
    batch_index: Optional[int] = None,
    sample_id: Optional[str] = None,
    requested: Optional[float] = None,
    available: Optional[float] = None,
    cycle_path: Optional[list[str]] = None,
) -> s.Conflict:
    return s.Conflict(
        code=code,
        message=message,
        op_no=getattr(op, "op_no", None),
        batch_index=batch_index,
        sample_id=sample_id,
        location=getattr(op, "location", None),
        requested=requested,
        available=available,
        cycle_path=cycle_path,
    )


def _get_sample(conn: sqlite3.Connection, sample_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM samples WHERE sample_id = ?", (sample_id,)
    ).fetchone()


def _load_edges(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (row["parent_id"], row["child_id"])
        for row in conn.execute(
            "SELECT parent_id, child_id FROM lineage_edges WHERE parent_id IS NOT NULL"
        )
    ]


def _descendants(
    by_parent: dict[str, list[str]], start: str
) -> Optional[list[str]]:
    """从 start 沿 父->子 方向 BFS;返回可达节点(不含 start),超上限返回 None。"""
    seen: set[str] = set()
    order: list[str] = []
    stack = [start]
    while stack:
        node = stack.pop()
        for child in by_parent.get(node, ()):  # noqa: PERF
            if child in seen:
                continue
            seen.add(child)
            order.append(child)
            if len(seen) > _CYCLE_LIMIT:
                return None
            stack.append(child)
    return order


# ---------------------------------------------------------------- 幂等

def canonical_payload(op: s.AnyOp) -> str:
    """规范化请求体,用于重复操作号时判定"同一操作"。"""
    return op.model_dump_json()


def _get_existing_op(
    conn: sqlite3.Connection, op_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM operations WHERE op_no = ?", (op_no,)
    ).fetchone()


def _stored_result(row: sqlite3.Row, *, replayed: bool) -> s.OpResult:
    effect = json.loads(row["effect"])
    return s.OpResult(
        op_no=row["op_no"],
        type=row["op_type"],
        samples_created=effect["samples_created"],
        edges=[s.EdgeOut(**e) for e in effect["edges"]],
        remaining=effect["remaining"],
        replayed=replayed,
    )


# ---------------------------------------------------------------- 操作计划

@dataclass
class Plan:
    """单个操作通过全部校验后得到的落库计划(纯数据,便于测试)。"""

    new_samples: list[tuple] = field(default_factory=list)
    # (sample_id, material, unit, initial_amount, remaining, location, is_root)
    deltas: dict[str, float] = field(default_factory=dict)  # 已存在样品的余量增量
    edges: list[tuple] = field(default_factory=list)        # (parent|None, child, amount, unit)
    ledger: list[tuple] = field(default_factory=list)      # (sample, direction, amount, unit, edge_idx)
    created: list[str] = field(default_factory=list)
    remaining_view: dict[str, float] = field(default_factory=dict)


def _plan_sampling(conn: sqlite3.Connection, op: s.SamplingOp) -> Plan:
    if _get_sample(conn, op.sample_id) is not None:
        raise ConflictError([
            _conflict(
                "sample_already_exists",
                f"样品 {op.sample_id} 已存在,取样不能覆盖已有样品",
                op=op, sample_id=op.sample_id,
            )
        ])
    p = Plan()
    p.new_samples.append(
        (op.sample_id, op.material, op.unit, _r(op.amount), _r(op.amount),
         op.location, 1)
    )
    p.edges.append((None, op.sample_id, _r(op.amount), op.unit))
    # 取样的 in 流水无父边(edge_id NULL),账本重算时以 initial_amount 计来源
    p.ledger.append((op.sample_id, "in", _r(op.amount), op.unit, None))
    p.created.append(op.sample_id)
    p.remaining_view[op.sample_id] = _r(op.amount)
    return p


def _plan_aliquot(conn: sqlite3.Connection, op: s.AliquotOp) -> Plan:
    parent = _get_sample(conn, op.parent_id)
    if parent is None:
        raise ConflictError([
            _conflict(
                "sample_not_found",
                f"父样品 {op.parent_id} 不存在,无法分装",
                op=op, sample_id=op.parent_id,
            )
        ])
    unit = op.unit or parent["unit"]
    if unit != parent["unit"]:
        raise ConflictError([
            _conflict(
                "unit_mismatch",
                f"分装单位 {unit} 与父样品单位 {parent['unit']} 不一致",
                op=op, sample_id=op.parent_id,
            )
        ])

    total = _r(sum(o.amount for o in op.outputs))
    if total > parent["remaining"] + _EPS:
        raise ConflictError([
            _conflict(
                "insufficient_balance",
                f"父样品 {op.parent_id} 余量 {parent['remaining']} {unit} 不足,"
                f"本次分装需要 {total} {unit}",
                op=op, sample_id=op.parent_id,
                requested=total, available=_r(parent["remaining"]),
            )
        ])

    p = Plan()
    for part in op.outputs:
        if _get_sample(conn, part.sample_id) is not None:
            raise ConflictError([
                _conflict(
                    "sample_already_exists",
                    f"分装目标 {part.sample_id} 已存在,子样品必须为新样品",
                    op=op, sample_id=part.sample_id,
                )
            ])
        amount = _r(part.amount)
        p.new_samples.append(
            (part.sample_id, parent["material"], unit, 0.0, amount,
             op.location, 0)
        )
        p.edges.append((op.parent_id, part.sample_id, amount, unit))
        p.ledger.append((op.parent_id, "out", amount, unit, len(p.edges) - 1))
        p.ledger.append((part.sample_id, "in", amount, unit, len(p.edges) - 1))
        p.created.append(part.sample_id)
        p.remaining_view[part.sample_id] = amount

    p.deltas[op.parent_id] = -total
    p.remaining_view[op.parent_id] = _r(parent["remaining"] - total)
    return p


def _plan_merge(conn: sqlite3.Connection, op: s.MergeOp) -> Plan:
    # 1) 收集并校验所有输入
    sources: list[tuple[sqlite3.Row, float]] = []
    for src in op.inputs:
        row = _get_sample(conn, src.sample_id)
        if row is None:
            raise ConflictError([
                _conflict(
                    "sample_not_found",
                    f"合并输入 {src.sample_id} 不存在",
                    op=op, sample_id=src.sample_id,
                )
            ])
        if src.sample_id == op.output_id:
            raise ConflictError([
                _conflict(
                    "self_merge",
                    f"样品 {op.output_id} 不能合并到自身",
                    op=op, sample_id=op.output_id,
                )
            ])
        sources.append((row, _r(src.amount)))

    unit = op.unit or sources[0][0]["unit"]
    total = _r(sum(a for _, a in sources))

    for row, amount in sources:
        if row["unit"] != unit:
            raise ConflictError([
                _conflict(
                    "unit_mismatch",
                    f"输入 {row['sample_id']} 单位 {row['unit']} 与合并单位 {unit} 不一致",
                    op=op, sample_id=row["sample_id"],
                )
            ])
        if amount > row["remaining"] + _EPS:
            raise ConflictError([
                _conflict(
                    "insufficient_balance",
                    f"输入 {row['sample_id']} 余量 {row['remaining']} {unit} 不足,"
                    f"本次需要 {amount} {unit}",
                    op=op, sample_id=row["sample_id"],
                    requested=amount, available=_r(row["remaining"]),
                )
            ])

    output = _get_sample(conn, op.output_id)
    if output is not None and output["unit"] != unit:
        raise ConflictError([
            _conflict(
                "unit_mismatch",
                f"输出 {op.output_id} 单位 {output['unit']} 与合并单位 {unit} 不一致",
                op=op, sample_id=op.output_id,
            )
        ])

    # 2) 环路检测:新边 parent -> output 若令 output 可达 parent,即成环。
    #    返回环路径 output -> ... -> parent -> output(走新边闭环)。
    pairs = _load_edges(conn)
    by_parent: dict[str, list[str]] = {}
    for par, ch in pairs:
        by_parent.setdefault(par, []).append(ch)
    for row, _amount in sources:
        reach = _descendants(by_parent, op.output_id)
        if reach is None:
            raise ConflictError([
                _conflict(
                    "lineage_too_large",
                    "谱系规模超过遍历上限,拒绝写入以防环路",
                    op=op, sample_id=op.output_id,
                )
            ])
        if row["sample_id"] in set(reach):
            cycle = [op.output_id]
            cycle += _shortest_path(by_parent, op.output_id, row["sample_id"])
            cycle.append(op.output_id)
            raise ConflictError([
                _conflict(
                    "cycle_detected",
                    f"禁止环路:{op.output_id} 已是 {row['sample_id']} 的下游,"
                    f"合并会形成 { ' -> '.join(cycle) } 的环",
                    op=op, sample_id=op.output_id, cycle_path=cycle,
                )
            ])

    # 3) 守恒校验(可选)
    if op.expected_output_amount is not None and abs(
        _r(op.expected_output_amount) - total
    ) > max(_EPS, 1e-6):
        raise ConflictError([
            _conflict(
                "output_amount_mismatch",
                f"期望输出 {op.expected_output_amount} {unit} 与输入合计 {total} {unit} 不符",
                op=op, sample_id=op.output_id,
                requested=_r(op.expected_output_amount), available=total,
            )
        ])

    p = Plan()
    if output is None:
        if not op.material:
            raise ConflictError([
                _conflict(
                    "material_required",
                    f"输出样品 {op.output_id} 不存在,新建合并产物必须提供 material",
                    op=op, sample_id=op.output_id,
                )
            ])
        p.new_samples.append(
            (op.output_id, op.material, unit, 0.0, total, op.location, 0)
        )
        p.created.append(op.output_id)
        out_remaining = total
    else:
        out_remaining = _r(output["remaining"] + total)

    for row, amount in sources:
        p.edges.append((row["sample_id"], op.output_id, amount, unit))
        idx = len(p.edges) - 1
        p.ledger.append((row["sample_id"], "out", amount, unit, idx))
        p.ledger.append((op.output_id, "in", amount, unit, idx))
        p.deltas[row["sample_id"]] = p.deltas.get(row["sample_id"], 0.0) - amount
        p.remaining_view[row["sample_id"]] = _r(row["remaining"] - amount)

    # 新样品的 initial/remaining 已为 total,不再重复加增量
    if output is not None:
        p.deltas[op.output_id] = p.deltas.get(op.output_id, 0.0) + total
    p.remaining_view[op.output_id] = out_remaining
    return p


def _shortest_path(
    by_parent: dict[str, list[str]], start: str, target: str
) -> list[str]:
    """BFS 求 start -> target 的最短路径(用于环路定位),不含 start。"""
    prev: dict[str, Optional[str]] = {start: None}
    queue = [start]
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        if node == target:
            break
        for child in by_parent.get(node, ()):
            if child not in prev:
                prev[child] = node
                queue.append(child)
    path: list[str] = []
    cur: Optional[str] = target
    while cur is not None and cur != start:
        path.append(cur)
        cur = prev.get(cur)
    path.reverse()
    return path


def _build_plan(conn: sqlite3.Connection, op: s.AnyOp) -> Plan:
    if isinstance(op, s.SamplingOp):
        return _plan_sampling(conn, op)
    if isinstance(op, s.AliquotOp):
        return _plan_aliquot(conn, op)
    return _plan_merge(conn, op)


# ---------------------------------------------------------------- 落库

def _persist_plan(
    conn: sqlite3.Connection,
    op: s.AnyOp,
    plan: Plan,
    *,
    seq_index: int,
    batch_id: Optional[str],
) -> s.OpResult:
    payload = canonical_payload(op)
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq_index), -1) + 1 AS next_seq FROM operations"
    ).fetchone()
    next_seq = max(seq_row["next_seq"], seq_index)

    edges_out = [
        {"parent_id": par, "child_id": ch, "op_no": op.op_no,
         "amount": amt, "unit": unit}
        for par, ch, amt, unit in plan.edges
    ]
    effect = {
        "samples_created": plan.created,
        "edges": edges_out,
        "remaining": {k: _r(v) for k, v in plan.remaining_view.items()},
    }
    cur = conn.execute(
        "INSERT INTO operations (op_no, op_type, payload, effect, seq_index,"
        " batch_id, location) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (op.op_no, op.type, payload, json.dumps(effect, ensure_ascii=False),
         next_seq, batch_id, op.location),
    )
    op_id = cur.lastrowid

    for sid, material, unit, initial, remaining, loc, is_root in plan.new_samples:
        conn.execute(
            "INSERT INTO samples (sample_id, material, unit, initial_amount,"
            " remaining, location, is_root) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, material, unit, initial, remaining, loc, is_root),
        )
    for sid, delta in plan.deltas.items():
        if abs(delta) < _EPS:
            continue
        conn.execute(
            "UPDATE samples SET remaining = remaining + ?, version = version + 1"
            " WHERE sample_id = ?",
            (_r(delta), sid),
        )

    edge_ids: list[int] = []
    for par, ch, amt, unit in plan.edges:
        ecur = conn.execute(
            "INSERT INTO lineage_edges (parent_id, child_id, op_id, amount,"
            " unit, ref_no) VALUES (?, ?, ?, ?, ?, ?)",
            (par, ch, op_id, amt, unit, op.op_no),
        )
        edge_ids.append(ecur.lastrowid)

    for sid, direction, amt, unit, edge_idx in plan.ledger:
        edge_id = edge_ids[edge_idx] if edge_idx is not None else None
        conn.execute(
            "INSERT INTO ledger_entries (sample_id, op_id, edge_id, direction,"
            " amount, unit, ref_no) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, op_id, edge_id, direction, amt, unit, op.op_no),
        )

    return s.OpResult(
        op_no=op.op_no,
        type=op.type,
        samples_created=plan.created,
        edges=[s.EdgeOut(**e) for e in edges_out],
        remaining=effect["remaining"],
    )


# ---------------------------------------------------------------- 提交 / 批量

def submit_batch(
    conn: sqlite3.Connection,
    ops: Iterable[s.AnyOp],
    *,
    batch_id: Optional[str] = None,
    index_conflicts: bool = True,
) -> list[s.OpResult]:
    """在单个事务中顺序提交;任一操作冲突则整批回滚。

    index_conflicts=False 时(单操作接口)冲突不带批内下标。
    """
    ops = list(ops)

    # 批内操作号去重(重复操作号不允许出现在同一批次,避免自相矛盾)
    seen: set[str] = set()
    for idx, op in enumerate(ops):
        if op.op_no in seen:
            raise ConflictError([
                _conflict(
                    "duplicate_op_no_in_batch",
                    f"操作号 {op.op_no} 在同一批次中出现多次(下标 {idx})",
                    op=op, batch_index=idx if index_conflicts else None,
                )
            ])
        seen.add(op.op_no)

    conn.execute("BEGIN IMMEDIATE")
    try:
        results: list[s.OpResult] = []
        for idx, op in enumerate(ops):
            existing = _get_existing_op(conn, op.op_no)
            if existing is not None:
                if existing["payload"] != canonical_payload(op):
                    raise ConflictError([
                        _conflict(
                            "op_no_conflict",
                            f"操作号 {op.op_no} 已存在但请求内容不同,"
                            "同一操作号必须对应同一次操作(幂等键冲突)",
                            op=op, batch_index=idx if index_conflicts else None,
                        )
                    ])
                results.append(_stored_result(existing, replayed=True))
                continue
            try:
                plan = _build_plan(conn, op)
            except ConflictError as exc:
                if index_conflicts:
                    for c in exc.conflicts:
                        c.batch_index = idx
                raise
            results.append(
                _persist_plan(conn, op, plan, seq_index=idx, batch_id=batch_id)
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return results


def _snapshot_conn(src: sqlite3.Connection) -> sqlite3.Connection:
    """把当前库快照复制到内存,用于 dry-run(绝不写主库)。"""
    from .db import connect

    mem = connect(":memory:")
    src.backup(mem)
    return mem


def simulate_batch(
    conn: sqlite3.Connection,
    ops: list[s.AnyOp],
    *,
    batch_id: Optional[str] = None,
    index_conflicts: bool = True,
) -> tuple[Optional[list[s.OpResult]], list[s.Conflict]]:
    """
    预演:不写主库。返回 (结果, 冲突列表)。
    先做批内静态校验(可一次收集多条),再在内存快照里跑真实事务逻辑。
    """
    conflicts: list[s.Conflict] = []

    seen: set[str] = set()
    for idx, op in enumerate(ops):
        if op.op_no in seen:
            conflicts.append(_conflict(
                "duplicate_op_no_in_batch",
                f"操作号 {op.op_no} 在同一批次中出现多次(下标 {idx})",
                op=op, batch_index=idx if index_conflicts else None,
            ))
        seen.add(op.op_no)
    if conflicts:
        return None, conflicts

    mem = _snapshot_conn(conn)
    try:
        results = submit_batch(
            mem, ops, batch_id=batch_id, index_conflicts=index_conflicts
        )
        return results, []
    except ConflictError as exc:
        return None, exc.conflicts
    finally:
        mem.close()


def batch_status(results: list[s.OpResult]) -> str:
    replays = sum(1 for r in results if r.replayed)
    if replays == 0:
        return "committed"
    if replays == len(results):
        return "replayed"
    return "mixed"


# ---------------------------------------------------------------- 谱系回溯

def trace_sample(
    conn: sqlite3.Connection,
    sample_id: str,
    direction: str = "both",
) -> s.TraceResponse:
    if _get_sample(conn, sample_id) is None:
        raise ConflictError([
            _conflict("sample_not_found", f"样品 {sample_id} 不存在",
                      sample_id=sample_id)
        ])

    # 递归 CTE:沿谱系边向上找来源 / 向下找去向,path 用于破除环
    nodes: dict[str, int] = {sample_id: 0}
    if direction in ("upstream", "both"):
        rows = conn.execute(
            """
            WITH RECURSIVE walk(node, depth, path) AS (
                SELECT parent_id, 1, '/' || child_id || '/'
                  FROM lineage_edges WHERE child_id = ? AND parent_id IS NOT NULL
                UNION
                SELECT e.parent_id, w.depth + 1, w.path || e.child_id || '/'
                  FROM lineage_edges e JOIN walk w ON e.child_id = w.node
                 WHERE e.parent_id IS NOT NULL
                   AND instr(w.path, e.parent_id || '/') = 0
            )
            SELECT node, MIN(depth) AS depth FROM walk GROUP BY node
            """,
            (sample_id,),
        ).fetchall()
        for r in rows:
            nodes[r["node"]] = max(nodes.get(r["node"], -1), r["depth"])
    if direction in ("downstream", "both"):
        rows = conn.execute(
            """
            WITH RECURSIVE walk(node, depth, path) AS (
                SELECT child_id, 1, '/' || parent_id || '/'
                  FROM lineage_edges WHERE parent_id = ?
                UNION
                SELECT e.child_id, w.depth + 1, w.path || e.parent_id || '/'
                  FROM lineage_edges e JOIN walk w ON e.parent_id = w.node
                 WHERE instr(w.path, e.child_id || '/') = 0
            )
            SELECT node, MIN(depth) AS depth FROM walk GROUP BY node
            """,
            (sample_id,),
        ).fetchall()
        for r in rows:
            # 向上/向下都可达时(合流),记较小跳数
            nodes[r["node"]] = min(nodes.get(r["node"], r["depth"]), r["depth"])

    ids = list(nodes.keys())
    if not ids:
        sample_rows, edge_rows, op_rows = [], [], []
    else:
        qmarks = ",".join("?" * len(ids))
        sample_rows = conn.execute(
            f"SELECT * FROM samples WHERE sample_id IN ({qmarks})", ids
        ).fetchall()
        edge_rows = conn.execute(
            f"""SELECT e.*, o.op_no FROM lineage_edges e
                  JOIN operations o ON o.op_id = e.op_id
                 WHERE e.child_id IN ({qmarks})
                    OR e.parent_id IN ({qmarks})""",
            (*ids, *ids),
        ).fetchall()
        op_ids = list({r["op_id"] for r in edge_rows})
        qm2 = ",".join("?" * len(op_ids))
        op_rows = conn.execute(
            f"SELECT op_no, op_type, created_at, location FROM operations"
            f" WHERE op_id IN ({qm2}) ORDER BY seq_index",
            op_ids,
        ).fetchall() if op_ids else []

    node_out = [
        s.LineageNode(
            sample_id=r["sample_id"],
            material=r["material"],
            unit=r["unit"],
            remaining=_r(r["remaining"]),
            location=r["location"],
            depth=nodes[r["sample_id"]],
            is_root=bool(r["is_root"]),
        )
        for r in sorted(sample_rows, key=lambda r: (nodes[r["sample_id"]],
                                                    r["sample_id"]))
    ]

    edge_set: set[tuple] = set()
    edge_out: list[s.LineageEdge] = []
    involved = set(ids) | {sample_id}
    for r in edge_rows:
        if r["parent_id"] is not None and (
            r["parent_id"] not in involved or r["child_id"] not in involved
        ):
            continue
        key = (r["parent_id"], r["child_id"], r["op_no"], r["amount"])
        if key in edge_set:
            continue
        edge_set.add(key)
        edge_out.append(s.LineageEdge(
            parent_id=r["parent_id"], child_id=r["child_id"],
            op_no=r["op_no"], amount=_r(r["amount"]), unit=r["unit"],
        ))

    # 根(取样源)/叶(无下游)判定
    root_ids = [n.sample_id for n in node_out if n.is_root]
    child_set = {r["child_id"] for r in edge_rows if r["parent_id"] is not None}
    parent_set = {r["parent_id"] for r in edge_rows if r["parent_id"] is not None}
    leaves = [
        n.sample_id for n in node_out
        if n.sample_id not in parent_set
        and (direction != "upstream" or n.sample_id == sample_id)
    ]
    if direction == "upstream":
        leaves = []
    if direction == "downstream":
        root_ids = []

    return s.TraceResponse(
        sample_id=sample_id,
        direction=direction,  # type: ignore[arg-type]
        nodes=node_out,
        edges=sorted(edge_out, key=lambda e: (e.op_no, e.child_id)),
        operations=[
            s.TraceOp(op_no=r["op_no"], type=r["op_type"],
                      created_at=r["created_at"], location=r["location"])
            for r in op_rows
        ],
        roots=sorted(root_ids),
        leaves=sorted(leaves),
    )


# ---------------------------------------------------------------- 余量校验

def _ledger_remaining(conn: sqlite3.Connection, sample_id: str) -> tuple[float, float, float]:
    """账本重算余量。

    - 非根样品:Σin − Σout(全部来自谱系转移)
    - 根样品(取样):initial_amount 即外部来源,取样的那条 in 流水与它同源,
      重算时排除,避免双算;之后仍可能被其他样品并入(Σin)。
    - 盘点调整(stocktake_adjustments.delta,可正可负)作为非谱系流水一并计入,
      因此实盘确认改账后冗余余量仍可与账本重算对齐。
    """
    smp = conn.execute(
        "SELECT initial_amount, is_root FROM samples WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    is_root = smp is not None and smp["is_root"]
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN direction='in'  THEN amount ELSE 0 END),0) AS tin,
                  COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END),0) AS tout
             FROM ledger_entries
            WHERE sample_id = ?
              AND NOT (? = 1 AND edge_id IS NULL)""",
        (sample_id, 1 if is_root else 0),
    ).fetchone()
    adj = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) AS d FROM stocktake_adjustments"
        " WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    source = smp["initial_amount"] if is_root else 0.0
    tin, tout = _r(row["tin"]), _r(row["tout"])
    adjustments = _r(adj["d"])
    return tin, tout, _r(source + tin - tout + adjustments)


def get_balance(
    conn: sqlite3.Connection, sample_id: str, tolerance: float
) -> s.SampleBalance:
    smp = _get_sample(conn, sample_id)
    if smp is None:
        raise ConflictError([
            _conflict("sample_not_found", f"样品 {sample_id} 不存在",
                      sample_id=sample_id)
        ])
    tin, tout, ledger_rem = _ledger_remaining(conn, sample_id)
    stored = _r(smp["remaining"])
    adj_row = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) AS d FROM stocktake_adjustments"
        " WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    return s.SampleBalance(
        sample_id=sample_id,
        unit=smp["unit"],
        stored_remaining=stored,
        ledger_remaining=ledger_rem,
        consistent=abs(stored - ledger_rem) <= tolerance,
        total_in=tin,
        total_out=tout,
        adjustments=_r(adj_row["d"]),
        available=stored,
    )


def check_all_balances(
    conn: sqlite3.Connection, tolerance: float
) -> tuple[list[s.SampleBalance], list[s.Conflict]]:
    rows = conn.execute("SELECT sample_id FROM samples ORDER BY sample_id").fetchall()
    balances = [get_balance(conn, r["sample_id"], tolerance) for r in rows]
    conflicts = [
        s.Conflict(
            code="balance_inconsistent",
            message=(
                f"样品 {b.sample_id} 冗余余量 {b.stored_remaining} 与账本重算 "
                f"{b.ledger_remaining} 不一致(容差 {tolerance})"
            ),
            sample_id=b.sample_id,
            available=b.stored_remaining,
            requested=b.ledger_remaining,
        )
        for b in balances
        if not b.consistent
    ]
    return balances, conflicts
