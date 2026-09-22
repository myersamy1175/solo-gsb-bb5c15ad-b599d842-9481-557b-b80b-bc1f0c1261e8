"""SQLite 连接管理与建表脚本。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Generator

from .config import DB_PATH

_SCHEMA = """
PRAGMA foreign_keys = ON;

-- 样品主数据:余量以出入库流水为准并冗余保存 remaining
CREATE TABLE IF NOT EXISTS samples (
    sample_id        TEXT PRIMARY KEY,
    material         TEXT NOT NULL,
    unit             TEXT NOT NULL,
    initial_amount   REAL NOT NULL CHECK (initial_amount >= 0),
    remaining        REAL NOT NULL CHECK (remaining >= 0),
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
    """幂等建表。"""
    conn.executescript(_SCHEMA)


def get_conn(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """FastAPI 依赖(生成器):每个请求一个连接,结束自动关闭并提交隐式事务。"""
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
