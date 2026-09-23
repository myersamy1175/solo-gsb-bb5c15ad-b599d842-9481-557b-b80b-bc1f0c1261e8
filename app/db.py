"""SQLite 连接管理与建表脚本。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Generator

from .config import DB_PATH

_SCHEMA = """
PRAGMA foreign_keys = ON;

-- 样品主数据:余量以出入库流水为准并冗余保存 remaining;
-- version 为乐观锁版本,余量每被改动一次加 1,盘点据此判断"期间是否被其他操作改变"
CREATE TABLE IF NOT EXISTS samples (
    sample_id        TEXT PRIMARY KEY,
    material         TEXT NOT NULL,
    unit             TEXT NOT NULL,
    initial_amount   REAL NOT NULL CHECK (initial_amount >= 0),
    remaining        REAL NOT NULL CHECK (remaining >= 0),
    version          INTEGER NOT NULL DEFAULT 0,
    location         TEXT,
    is_root          INTEGER NOT NULL DEFAULT 0 CHECK (is_root IN (0, 1)),
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

-- 已执行的数据库迁移登记(幂等升级用)
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 盘点批次:open=已建批(仅有快照),submitted=已提交实盘结果(含差异),
-- confirmed=已确认并落调整/改库存
CREATE TABLE IF NOT EXISTS stocktakes (
    stocktake_no  TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'submitted', 'confirmed')),
    payload       TEXT NOT NULL,
    submit_payload TEXT,
    note          TEXT,
    operator      TEXT,
    reason        TEXT,
    submitted_at  TEXT,
    confirmed_at  TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 盘点明细:建批时写入系统余量/单位/版本快照;提交实盘时写 counted/diff/submitted_version
CREATE TABLE IF NOT EXISTS stocktake_items (
    item_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stocktake_no      TEXT NOT NULL,
    sample_id         TEXT NOT NULL,
    seq_index         INTEGER NOT NULL,
    snapshot_remaining REAL NOT NULL,
    snapshot_unit     TEXT NOT NULL,
    snapshot_version  INTEGER NOT NULL,
    counted_amount    REAL CHECK (counted_amount IS NULL OR counted_amount >= 0),
    difference        REAL,
    submitted_version INTEGER,
    FOREIGN KEY (stocktake_no) REFERENCES stocktakes(stocktake_no),
    FOREIGN KEY (sample_id)     REFERENCES samples(sample_id),
    UNIQUE (stocktake_no, sample_id)
);

CREATE INDEX IF NOT EXISTS idx_sitems_stocktake ON stocktake_items(stocktake_no);
CREATE INDEX IF NOT EXISTS idx_sitems_sample     ON stocktake_items(sample_id);

-- 盘点调整流水:确认时逐项写入,是余量账本的一部分;
-- adjustment 方向的金额为实盘量-系统量(可正可负),非谱系转移故无 operation 关联
CREATE TABLE IF NOT EXISTS stocktake_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stocktake_no  TEXT NOT NULL,
    sample_id     TEXT NOT NULL,
    system_amount REAL NOT NULL,
    counted_amount REAL NOT NULL CHECK (counted_amount >= 0),
    delta         REAL NOT NULL,
    unit          TEXT NOT NULL,
    operator      TEXT NOT NULL,
    reason        TEXT NOT NULL,
    from_version  INTEGER NOT NULL,
    to_version    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (stocktake_no) REFERENCES stocktakes(stocktake_no),
    FOREIGN KEY (sample_id)    REFERENCES samples(sample_id)
);

CREATE INDEX IF NOT EXISTS idx_sadj_stocktake ON stocktake_adjustments(stocktake_no);
CREATE INDEX IF NOT EXISTS idx_sadj_sample    ON stocktake_adjustments(sample_id);
"""


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
    """幂等建表并执行增量迁移(对旧版本数据库补齐新列/新表)。"""
    conn.executescript(_SCHEMA)
    _migrate(conn)


# 增量迁移:每项在 schema_migrations 中登记一次,重复启动不重复执行
_MIGRATIONS = (
    # 旧库 samples 没有 version 列时补齐(乐观锁),存量样品版本从 0 起
    (
        "samples_add_version",
        "ALTER TABLE samples ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
        "samples",
        "version",
    ),
    # 盘点头补提交请求体列(早于正式发布前的中间版本库兼容)
    (
        "stocktakes_add_submit_payload",
        "ALTER TABLE stocktakes ADD COLUMN submit_payload TEXT",
        "stocktakes",
        "submit_payload",
    ),
)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn: sqlite3.Connection) -> None:
    for name, ddl, table, column in _MIGRATIONS:
        done = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
        ).fetchone()
        if done is not None:
            continue
        # 登记与 DDL 同事务;ALTER TABLE 在 SQLite 中自动提交但无嵌套事务,
        # 此处连接为 isolation_level=None(autocommit),逐条执行即可
        if not _column_exists(conn, table, column):
            conn.execute(ddl)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)", (name,)
        )


def get_conn(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """FastAPI 依赖(生成器):每个请求一个连接,结束自动关闭并提交隐式事务。"""
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
