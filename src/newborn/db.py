"""SQLite 存储层（M1，已按审查意见收紧）。

相对首版的改动：
- 事件幂等键作用域改为 (instance_id, source, idempotency_key)，不再全库唯一。
- memories 增加 conversation_id，隐私过滤可按会话隔离。
- outbox 增加 running 领取、认领者与租约，禁止并发重复领取。
- states.layer / memories.lifecycle_state / epistemic_status / outbox.status 加 CHECK 约束，
  非法值在数据库层就写不进去。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    schema_version  INTEGER NOT NULL,
    instance_id     TEXT NOT NULL,
    subject_id      TEXT,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_event_id TEXT,
    actor_id        TEXT,
    conversation_id TEXT,
    occurred_at     REAL NOT NULL,
    received_at     REAL NOT NULL,
    payload         TEXT NOT NULL,
    privacy_scope   TEXT NOT NULL CHECK (privacy_scope IN ('private','conversation','instance','public')),
    trust_level     TEXT NOT NULL CHECK (trust_level IN ('user','trusted','external','model')),
    trace_id        TEXT,
    causation_id    TEXT,
    idempotency_key TEXT NOT NULL,
    process_state   TEXT NOT NULL DEFAULT 'pending' CHECK (process_state IN ('pending','processed','rejected','failed')),
    claimed_by      TEXT,
    lease_until     REAL,
    UNIQUE (instance_id, source, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_events_pending ON events(process_state, received_at);

CREATE TABLE IF NOT EXISTS memories (
    memory_id       TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    conversation_id TEXT,
    subject_ids     TEXT NOT NULL DEFAULT '[]',
    memory_type     TEXT NOT NULL,
    content         TEXT NOT NULL CHECK (length(trim(content)) > 0),
    display_text    TEXT,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    provenance      TEXT NOT NULL,
    epistemic_status TEXT NOT NULL CHECK (epistemic_status IN ('reported','inferred','verified','disputed')),
    confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    importance      REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    salience        REAL NOT NULL CHECK (salience >= 0 AND salience <= 1),
    occurred_at     REAL NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    last_accessed_at REAL,
    expires_at      REAL,
    access_count    INTEGER NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 1,
    supersedes_id   TEXT,
    contradicts_ids TEXT NOT NULL DEFAULT '[]',
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('candidate','active','superseded','archived','pending_delete','deleted')),
    privacy_scope   TEXT NOT NULL CHECK (privacy_scope IN ('private','conversation','instance','public'))
);
CREATE INDEX IF NOT EXISTS idx_mem_lookup ON memories(instance_id, lifecycle_state, memory_type);
CREATE INDEX IF NOT EXISTS idx_mem_conv ON memories(instance_id, conversation_id);

CREATE TABLE IF NOT EXISTS states (
    instance_id TEXT NOT NULL,
    layer       TEXT NOT NULL
        CHECK (layer IN ('identity','trait','affective','cognitive','social')),
    scope       TEXT NOT NULL DEFAULT 'default',
    version     INTEGER NOT NULL CHECK (version > 0),
    updated_at  REAL NOT NULL,
    source_event_id TEXT,
    data        TEXT NOT NULL,
    PRIMARY KEY (instance_id, layer, scope)
);

CREATE TABLE IF NOT EXISTS outbox (
    action_id   TEXT PRIMARY KEY,
    event_id    TEXT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','succeeded','failed','unknown','cancelled')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    claimed_by  TEXT,
    lease_until REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, created_at);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- ===== 皮层侧（巩固产物）=====

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id      TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    snapshot_fingerprint TEXT NOT NULL,
    member_fingerprint   TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    supersedes_id   TEXT,
    split_from      TEXT,
    merged_from     TEXT,
    members         TEXT NOT NULL DEFAULT '[]',
    support_count   INTEGER NOT NULL DEFAULT 0,
    independent_source_count INTEGER NOT NULL DEFAULT 0,
    subject_distribution TEXT NOT NULL DEFAULT '{}',
    time_distribution    TEXT NOT NULL DEFAULT '{}',
    features        TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','split','merged','archived')),
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusters_inst ON clusters(instance_id, status);

CREATE TABLE IF NOT EXISTS consolidation_jobs (
    job_id          TEXT PRIMARY KEY,
    job_key         TEXT UNIQUE,
    instance_id     TEXT NOT NULL,
    window_start    REAL NOT NULL,
    window_end      REAL NOT NULL,
    seed            INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    input_watermark REAL NOT NULL,
    sampled         TEXT NOT NULL DEFAULT '{}',
    produced        TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'done'
        CHECK (status IN ('done','failed')),
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trait_proposals (
    proposal_id     TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    dim             TEXT NOT NULL,
    before_value    REAL NOT NULL,
    delta           REAL NOT NULL,
    after_value     REAL NOT NULL,
    cluster_id      TEXT,
    evidence        TEXT NOT NULL DEFAULT '[]',
    rule_version    TEXT NOT NULL,
    evidence_fingerprint TEXT,
    status          TEXT NOT NULL CHECK (status IN ('applied','rejected')),
    reject_reason   TEXT,
    day             TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trait_ledger (
    instance_id TEXT NOT NULL,
    day         TEXT NOT NULL,
    dim         TEXT NOT NULL,
    used_abs    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (instance_id, day, dim)
);
"""

# memories 表在 M2 追加的列（旧库用 ALTER 补齐）
MEMORY_EXTRA_COLUMNS = {
    "cluster_id": "TEXT",
    "structured_claim": "TEXT NOT NULL DEFAULT '{}'",
    "support_count": "INTEGER NOT NULL DEFAULT 0",
    "independent_source_count": "INTEGER NOT NULL DEFAULT 0",
    "subject_distribution": "TEXT NOT NULL DEFAULT '{}'",
    "time_distribution": "TEXT NOT NULL DEFAULT '{}'",
    "contradicting_event_ids": "TEXT NOT NULL DEFAULT '[]'",
    "algorithm_version": "TEXT",
    "input_watermark": "REAL",
    "change_kind": "TEXT",
}


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


SCHEMA_VERSION = 2

# 逐版本迁移：老库缺列就补，记录版本号
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "memories": MEMORY_EXTRA_COLUMNS,
    "events": {"claimed_by": "TEXT", "lease_until": "REAL"},
    "consolidation_jobs": {"job_key": "TEXT"},
    "trait_proposals": {"evidence_fingerprint": "TEXT"},
}


def _ddl_for(table: str) -> str:
    """从 SCHEMA 里抠出某张表的最新建表语句（用于重建旧表）。"""
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SCHEMA, re.S)
    if not m:
        raise KeyError(f"SCHEMA 里找不到表 {table}")
    return f"CREATE TABLE __NAME__ ({m.group(1)}\n)"


def _rebuild_table(conn: sqlite3.Connection, table: str) -> None:
    """建新表 → 拷公共列 → 换名。SQLite 改不了 CHECK，只能这样升约束。"""
    tmp = f"{table}__migrate"
    old_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if not old_cols:
        return
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    conn.execute(_ddl_for(table).replace("__NAME__", tmp))
    new_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tmp})")}
    common = sorted(old_cols & new_cols)
    if common:
        cols = ", ".join(common)
        conn.execute(f"INSERT INTO {tmp} ({cols}) SELECT {cols} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def _needs_rebuild(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()
    if row is None:
        # 没有版本号：只有空库（刚建）才算新库，老库走重建
        has_data = any(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("events", "memories", "outbox")
            if conn.execute(
                f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='{t}'"
            ).fetchone()[0]
        )
        return bool(has_data)
    try:
        return int(row["v"]) < SCHEMA_VERSION
    except (TypeError, ValueError):
        return True


def _migrate(conn: sqlite3.Connection) -> None:
    """老库升级：先按需重建表（补约束与列），再补索引与版本号。"""
    if _needs_rebuild(conn):
        with Tx(conn):
            for table in ("events", "memories", "outbox",
                          "consolidation_jobs", "trait_proposals"):
                _rebuild_table(conn, table)
    for table, cols in _COLUMN_MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # 唯一的复合索引用 IF NOT EXISTS 补（老库没有约束也能补上索引）
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_key ON consolidation_jobs(job_key)"
        " WHERE job_key IS NOT NULL")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_trait_evidence"
        " ON trait_proposals(instance_id, dim, evidence_fingerprint, rule_version)"
        " WHERE evidence_fingerprint IS NOT NULL AND status = 'applied'")
    conn.execute("INSERT INTO meta (k, v) VALUES ('schema_version', ?)"
                 " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(SCHEMA_VERSION),))


class Tx:
    """一个显式事务：BEGIN IMMEDIATE ... COMMIT / ROLLBACK。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


class NestedTx:
    """可嵌套事务：外层已经开启事务时不再 BEGIN，只做标记。

    用于「一个巩固任务包住若干 Trait 提案」这种场景，避免
    sqlite3.OperationalError: cannot start a transaction within a transaction。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.outer = False

    def __enter__(self) -> sqlite3.Connection:
        self.outer = not self.conn.in_transaction
        if self.outer:
            self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.outer:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        return False


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: str) -> Any:
    return json.loads(text)


def now() -> float:
    return time.time()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
