"""旧库迁移验收（审计 #13）。

审计指出：之前只 ALTER 补列，CHECK 约束、唯一约束、事件复合唯一键都不会迁移。
现在做法是「版本号 < 当前 → 重建这五张表，拷公共列」，再补索引与版本号。

这里手工造一个 v1 老库（缺 claimed_by / display_text / job_key / 证据指纹），
验证三件事：
1. 连上之后缺的列都在，约束是新的；
2. 老数据一行不丢；
3. schema_version 升到当前值，再连一次不会重复重建。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import db  # noqa: E402

LEGACY_SQL = """
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    subject_id TEXT,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    source_event_id TEXT,
    actor_id TEXT,
    conversation_id TEXT,
    occurred_at REAL NOT NULL,
    received_at REAL NOT NULL,
    payload TEXT NOT NULL,
    privacy_scope TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    trace_id TEXT,
    causation_id TEXT,
    idempotency_key TEXT NOT NULL,
    process_state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    conversation_id TEXT,
    subject_ids TEXT NOT NULL DEFAULT '[]',
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    confidence REAL NOT NULL,
    importance REAL NOT NULL,
    salience REAL NOT NULL,
    occurred_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_accessed_at REAL,
    expires_at REAL,
    access_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_id TEXT,
    contradicts_ids TEXT NOT NULL DEFAULT '[]',
    lifecycle_state TEXT NOT NULL DEFAULT 'active',
    privacy_scope TEXT NOT NULL
);
CREATE TABLE outbox (
    action_id TEXT PRIMARY KEY, event_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
INSERT INTO meta (k, v) VALUES ('schema_version', '1');
INSERT INTO events (event_id, schema_version, instance_id, event_type, source, occurred_at,
    received_at, payload, privacy_scope, trust_level, idempotency_key)
  VALUES ('evt_old', 1, 'default', 'message.received', 'legacy', 1.0, 1.0,
          '{"text":"老库里的消息"}', 'private', 'user', 'k-old');
INSERT INTO memories (memory_id, instance_id, memory_type, content, provenance,
    epistemic_status, confidence, importance, salience, occurred_at, created_at,
    updated_at, lifecycle_state, privacy_scope)
  VALUES ('mem_old', 'default', 'episodic', '老库里的记忆', 'user_statement', 'reported',
          0.6, 0.5, 0.5, 1.0, 1.0, 1.0, 'active', 'private');
"""


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "legacy.sqlite3"
        conn = sqlite3.connect(self.path)
        conn.executescript(LEGACY_SQL)
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cols(self, conn, table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def test_old_columns_are_added(self) -> None:
        conn = db.connect(self.path)
        self.assertIn("claimed_by", self._cols(conn, "events"))
        self.assertIn("lease_until", self._cols(conn, "events"))
        self.assertIn("display_text", self._cols(conn, "memories"))
        self.assertIn("cluster_id", self._cols(conn, "memories"))
        self.assertIn("job_key", self._cols(conn, "consolidation_jobs"))
        self.assertIn("evidence_fingerprint", self._cols(conn, "trait_proposals"))
        conn.close()

    def test_old_rows_survive(self) -> None:
        conn = db.connect(self.path)
        ev = conn.execute("SELECT payload FROM events WHERE event_id = 'evt_old'").fetchone()
        mem = conn.execute("SELECT content FROM memories WHERE memory_id = 'mem_old'").fetchone()
        self.assertIsNotNone(ev, "事件行不能丢")
        self.assertIn("老库里的消息", ev["payload"])
        self.assertIsNotNone(mem, "记忆行不能丢")
        self.assertEqual(mem["content"], "老库里的记忆")
        conn.close()

    def test_schema_version_bumped_and_idempotent(self) -> None:
        conn = db.connect(self.path)
        v = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()["v"]
        self.assertEqual(int(v), db.SCHEMA_VERSION)
        conn.close()
        # 再连一次：不该重建、不该丢数据
        conn = db.connect(self.path)
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(n, 1)
        # 新约束生效：事件复合唯一键
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events (event_id, schema_version, instance_id, event_type, source,"
                " occurred_at, received_at, payload, privacy_scope, trust_level, idempotency_key)"
                " VALUES ('evt_dup', 1, 'default', 'message.received', 'legacy', 1, 1, '{}',"
                " 'private', 'user', 'k-old')")
        conn.close()

    def test_outbox_gets_lease_columns(self) -> None:
        conn = db.connect(self.path)
        cols = self._cols(conn, "outbox")
        self.assertIn("claimed_by", cols)
        self.assertIn("lease_until", cols)
        conn.close()


if __name__ == "__main__":
    unittest.main()
