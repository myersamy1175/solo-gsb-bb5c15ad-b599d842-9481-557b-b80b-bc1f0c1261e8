"""SQLite 连接管理、建表脚本与增量迁移。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from .config import DB_PATH

# v1 基线表结构(取样/分装/合并 + 预留)。新增结构一律走 _MIGRATIONS,
# 这样全新库与已有库(Docker 卷中的旧库)都能得到一致的最终结构。
_SCHEMA = """
PRAGMA foreign_keys = ON;

-- 样品主数据:余量以出入库流水为准并冗余保存 remaining;
-- version 为余量乐观版本,每次余量写操作 +1(盘点等场景做并发校验)
CREATE TABLE IF NOT EXISTS samples (
    sample_id        TEXT PRIMARY KEY,
    material         TEXT NOT NULL,
    unit             TEXT NOT NULL,
    initial_amount   REAL NOT NULL CHECK (initial_amount >= 0),
    remaining        REAL NOT NULL CHECK (remaining >= 0),
    location         TEXT,
    is_root          INTEGER NOT NULL DEFAULT 0 CHECK (is_root IN (0, 1)),
    version          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 操作记录:op_no 全局唯一,是幂等键;payload 规范化后入库用于比对重放
CREATE TABLE IF NOT EXISTS operations (
    op_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    op_no      TEXT NOT NULL UNIQUE,
    op_type    TEXT NOT NULL CHECK (op_type IN ('sampling', 'aliquot', 'merge')),
    payload    TEXT NOT NULL,
    effect     TEXT NOT NULL,
    seq_index  INTEGER NOT NULL,
    batch_id   TEXT,
    location   TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 出入库流水(余量账本),与谱系边一一对应;sampling 的 in 流水无父边
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id    TEXT NOT NULL,
    op_id        INTEGER NOT NULL,
    edge_id      INTEGER,
    direction    TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    amount       REAL NOT NULL CHECK (amount >= 0),
    unit         TEXT NOT NULL,
    ref_no       TEXT NOT NULL,
    FOREIGN KEY (sample_id) REFERENCES samples(sample_id),
    FOREIGN KEY (op_id)   REFERENCES operations(op_id),
    FOREIGN KEY (edge_id) REFERENCES lineage_edges(edge_id)
);

-- 谱系边:parent -> child,parent 为 NULL 表示取样(源头)
CREATE TABLE IF NOT EXISTS lineage_edges (
    edge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id      TEXT,
    child_id       TEXT NOT NULL,
    op_id          INTEGER NOT NULL,
    amount         REAL NOT NULL CHECK (amount >= 0),
    unit           TEXT NOT NULL,
    ref_no         TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES samples(sample_id),
    FOREIGN KEY (child_id)  REFERENCES samples(sample_id),
    FOREIGN KEY (op_id)     REFERENCES operations(op_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_parent ON lineage_edges(parent_id);
CREATE INDEX IF NOT EXISTS idx_edges_child  ON lineage_edges(child_id);
CREATE INDEX IF NOT EXISTS idx_ledger_sample ON ledger_entries(sample_id);

-- 预留单:一个预留号为多个样品分别锁定数量;active=锁定中,released=已整单释放
CREATE TABLE IF NOT EXISTS reservations (
    reservation_no TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released')),
    payload        TEXT NOT NULL,
    note           TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at    TEXT
);

-- 预留明细:同一预留单内每个样品至多一条(UNIQUE 兜底)
CREATE TABLE IF NOT EXISTS reservation_items (
    item_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_no TEXT NOT NULL,
    sample_id      TEXT NOT NULL,
    amount         REAL NOT NULL CHECK (amount > 0),
    unit           TEXT NOT NULL,
    seq_index      INTEGER NOT NULL,
    FOREIGN KEY (reservation_no) REFERENCES reservations(reservation_no),
    FOREIGN KEY (sample_id)      REFERENCES samples(sample_id),
    UNIQUE (reservation_no, sample_id)
);

CREATE INDEX IF NOT EXISTS idx_ritems_reservation ON reservation_items(reservation_no);
CREATE INDEX IF NOT EXISTS idx_ritems_sample      ON reservation_items(sample_id);

-- 预留审计事件:预留 / 幂等重放 / 释放 / 查询的结果快照均持久化
CREATE TABLE IF NOT EXISTS reservation_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_no TEXT,
    action         TEXT NOT NULL CHECK (action IN ('reserve', 'replay', 'release', 'query')),
    ref_id         TEXT,
    detail         TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (reservation_no) REFERENCES reservations(reservation_no)
);

CREATE INDEX IF NOT EXISTS idx_revents_reservation ON reservation_events(reservation_no);
"""


# ---------------------------------------------------------------- 增量迁移
#
# 每个迁移是 (版本号, 迁移函数);迁移函数必须幂等(全新库上基线表已存在,
# 旧库上列/表可能缺失),成功执行后在 schema_migrations 中登记版本号。

def _migration_0001_stocktake(conn: sqlite3.Connection) -> None:
    """盘点工作流:samples.version 乐观锁 + 盘点批次/明细/调整流水。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(samples)").fetchall()}
    if "version" not in cols:
        # 旧库已有样品行默认版本 0;任何余量写操作都会把版本 +1
        conn.execute(
            "ALTER TABLE samples ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
        )

    # 盘点批次:open=已建批次(持有快照),counted=已提交实盘结果,confirmed=已确认并调整库存
    conn.execute("""
CREATE TABLE IF NOT EXISTS stocktake_batches (
    batch_no        TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'counted', 'confirmed')),
    payload         TEXT NOT NULL,
    count_payload   TEXT,
    confirm_payload TEXT,
    note            TEXT,
    operator        TEXT,
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    counted_at      TEXT,
    confirmed_at    TEXT
)""")

    # 盘点明细:创建时写入余量/版本快照,提交结果时写入实盘量与当时系统量
    conn.execute("""
CREATE TABLE IF NOT EXISTS stocktake_items (
    batch_no           TEXT NOT NULL,
    sample_id          TEXT NOT NULL,
    seq_index          INTEGER NOT NULL,
    unit               TEXT NOT NULL,
    snapshot_remaining REAL NOT NULL,
    snapshot_version   INTEGER NOT NULL,
    counted_amount     REAL,
    counted_version    INTEGER,
    system_remaining   REAL,
    submitted_at       TEXT,
    PRIMARY KEY (batch_no, sample_id),
    FOREIGN KEY (batch_no) REFERENCES stocktake_batches(batch_no),
    FOREIGN KEY (sample_id) REFERENCES samples(sample_id)
)""")

    # 盘点调整流水:确认时逐项写入(差异为 0 也留痕),带操作者与原因;
    # 差异 variance = 实盘量 - 系统量,正负分别表示盘盈/盘亏
    conn.execute("""
CREATE TABLE IF NOT EXISTS stocktake_adjustments (
    adjustment_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no           TEXT NOT NULL,
    sample_id          TEXT NOT NULL,
    unit               TEXT NOT NULL,
    snapshot_remaining REAL NOT NULL,
    system_remaining   REAL NOT NULL,
    counted_amount     REAL NOT NULL CHECK (counted_amount >= 0),
    variance           REAL NOT NULL,
    operator           TEXT,
    reason             TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (batch_no) REFERENCES stocktake_batches(batch_no),
    FOREIGN KEY (sample_id) REFERENCES samples(sample_id),
    UNIQUE (batch_no, sample_id)
)""")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sitems_batch ON stocktake_items(batch_no)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sitems_sample ON stocktake_items(sample_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sadj_batch ON stocktake_adjustments(batch_no)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sadj_sample ON stocktake_adjustments(sample_id)"
    )


_MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection], None]]] = [
    ("0001_stocktake", _migration_0001_stocktake),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """按顺序执行尚未登记的增量迁移,在同一事务内完成。"""
    conn.execute("""
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)""")
    applied = {
        r["version"]
        for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, migrate in _MIGRATIONS:
        if version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            migrate(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开连接并开启外键 / WAL / 立刻拿到写锁的基础配置。"""
    path = db_path or DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """幂等建表并执行增量迁移。"""
    conn.executescript(_SCHEMA)
    _migrate(conn)


def get_conn(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """FastAPI 依赖(生成器):每个请求一个连接,结束自动关闭并提交隐式事务。"""
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
