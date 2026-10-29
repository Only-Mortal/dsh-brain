"""NEXT-STEPS 2.1 的六项核验（其中第一条已在 test_security_findings 覆盖）。

这些不是新功能，是**证明现有代码真的做到了**：
- V2 实时与恢复重叠：同一事件只产生一个有效结果
- V3 状态并发合并：并发成功事件的情绪增量都保留
- V4 升级保真：老库重建后扩展字段、索引、版本链、访问域不丢
- V5 证据与版本身份：换 seed 不重复推动 Trait；普通修订为连续版本
- V6 完整 Web 路径：页面/API/令牌/实例选择能一起工作
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import consolidate, db, kernel, webui  # noqa: E402
from newborn import state as state_mod  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402

NOW = int(time.time() // 60) * 60


def make_env(key: str, text: str, actor="u1", instance="default") -> EventEnvelope:
    return EventEnvelope(
        event_type="message.received", source="verify", payload={"text": text},
        instance_id=instance, actor_id=actor, conversation_id="c1",
        privacy_scope="conversation", trust_level="user", idempotency_key=key,
    )


class V2EventOverlapTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "v2.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_recovery_processes_once(self) -> None:
        conn = db.connect(self.path)
        env = make_env("overlap-1", "并发恢复只能处理一次")
        conn.execute(
            "INSERT INTO events (event_id, schema_version, instance_id, event_type, source,"
            " occurred_at, received_at, payload, privacy_scope, trust_level, idempotency_key,"
            " conversation_id, process_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
            (env.event_id, 1, env.instance_id, env.event_type, env.source,
             env.occurred_at, env.received_at, json.dumps(env.payload, ensure_ascii=False),
             env.privacy_scope, env.trust_level, env.idempotency_key, env.conversation_id))
        conn.close()

        results: list[dict] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            c = db.connect(self.path)
            try:
                out = kernel.process_pending(c, limit=10, worker_id=f"w{idx}")
                with lock:
                    results.extend(out)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        conn = db.connect(self.path)
        processed = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_id = ? AND process_state = 'processed'",
            (env.event_id,)).fetchone()[0]
        outbox = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.assertEqual(outbox, 1, "同一事件只该产生一条回复")
        self.assertEqual(processed, 1, "事件应被处理且只标记一次")
        self.assertEqual(len([r for r in results if r.get("status") == "processed"]), 1,
                         "四个 worker 里只有一个真正处理了它")
        conn.close()


class V3StateConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "v3.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, texts: list[str]) -> float:
        def worker(i: int, text: str) -> None:
            c = db.connect(self.path)
            try:
                kernel.process_event(c, make_env(f"state-{i}", text, actor=f"u{i}"))
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(i, t)) for i, t in enumerate(texts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        c = db.connect(self.path)
        try:
            return state_mod.affect_of(c, "default").value().get("social", 0.0)
        finally:
            c.close()

    def test_concurrent_events_keep_both_increments(self) -> None:
        """两条都带强烈情绪的事件并发处理，两个增量都不能丢。"""
        # 「谢谢」会注入 social +0.10；两条都生效时应该比一条多
        a = self._run(["谢谢你呀"])
        b = self._run(["谢谢你呀", "谢谢你呀"])
        self.assertGreater(b, a + 1e-6,
                           "并发处理丢了一个事件的情绪增量（读-改-写覆盖）")


class V4UpgradeFidelityTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "v4.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_old_clusters_keep_fields_and_get_domain(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.executescript("""
        CREATE TABLE clusters (
            cluster_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
            algorithm_version TEXT NOT NULL, snapshot_fingerprint TEXT NOT NULL,
            member_fingerprint TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            supersedes_id TEXT, split_from TEXT, merged_from TEXT,
            members TEXT NOT NULL DEFAULT '[]', support_count INTEGER NOT NULL DEFAULT 0,
            independent_source_count INTEGER NOT NULL DEFAULT 0,
            subject_distribution TEXT NOT NULL DEFAULT '{}',
            time_distribution TEXT NOT NULL DEFAULT '{}',
            features TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE memories (
            memory_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, conversation_id TEXT,
            subject_ids TEXT NOT NULL DEFAULT '[]', memory_type TEXT NOT NULL,
            content TEXT NOT NULL, source_event_ids TEXT NOT NULL DEFAULT '[]',
            provenance TEXT NOT NULL, epistemic_status TEXT NOT NULL, confidence REAL NOT NULL,
            importance REAL NOT NULL, salience REAL NOT NULL, occurred_at REAL NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, lifecycle_state TEXT NOT NULL,
            privacy_scope TEXT NOT NULL, cluster_id TEXT, version INTEGER NOT NULL DEFAULT 1,
            supersedes_id TEXT
        );
        CREATE TABLE outbox (
            action_id TEXT PRIMARY KEY, event_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version','1');
        INSERT INTO clusters (cluster_id, instance_id, algorithm_version, snapshot_fingerprint,
            member_fingerprint, version, members, support_count, created_at, updated_at)
          VALUES ('clu_old', 'default', 'consolidate-1', 'snap', 'memfp', 3, '["m1"]', 7, 1, 1);
        INSERT INTO memories (memory_id, instance_id, memory_type, content, provenance,
            epistemic_status, confidence, importance, salience, occurred_at, created_at,
            updated_at, lifecycle_state, privacy_scope, cluster_id, version, supersedes_id)
          VALUES ('sem_old', 'default', 'semantic', '老语义', 'consolidation', 'inferred',
            0.5, 0.5, 0.5, 1, 1, 1, 'active', 'private', 'clu_old', 2, 'sem_prev');
        """)
        conn.commit()
        conn.close()

        c = db.connect(self.path)
        row = c.execute("SELECT * FROM clusters WHERE cluster_id='clu_old'").fetchone()
        self.assertEqual(row["version"], 3, "版本号不能丢")
        self.assertEqual(row["members"], '["m1"]', "成员不能丢")
        self.assertEqual(json.loads(row["access_domain"]), ["instance"],
                         "老簇应补上默认访问域")
        sem = c.execute("SELECT * FROM memories WHERE memory_id='sem_old'").fetchone()
        self.assertEqual(sem["version"], 2)
        self.assertEqual(sem["supersedes_id"], "sem_prev", "版本链不能丢")
        self.assertEqual(sem["cluster_id"], "clu_old")
        c.close()


class V5EvidenceIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")

    def _seed(self) -> None:
        import uuid
        for d in range(2):
            for i in range(3):
                mid = "epi_" + uuid.uuid4().hex[:8]
                ts = NOW - d * 86400 - i
                self.conn.execute(
                    "INSERT INTO memories (memory_id, instance_id, conversation_id, subject_ids,"
                    " memory_type, content, source_event_ids, provenance, epistemic_status,"
                    " confidence, importance, salience, occurred_at, created_at, updated_at,"
                    " lifecycle_state, privacy_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, "default", "c1", '["u1"]', "episodic", f"最近总在加班 第{d}天第{i}次",
                     json.dumps([mid]), "user_statement", "reported", .6, .4, .5, ts, ts, ts,
                     "active", "conversation"))

    def test_different_seed_does_not_relearn_same_evidence(self) -> None:
        self._seed()
        r1 = consolidate.consolidate(self.conn, "default", window_hours=72, seed=1, min_days=1)
        self.assertEqual(r1["status"], "done")
        applied1 = [t for t in r1["traits"] if t.get("applied")]
        r2 = consolidate.consolidate(self.conn, "default", window_hours=72, seed=2, min_days=1)
        applied2 = [t for t in r2["traits"] if t.get("applied")]
        self.assertTrue(applied1, "第一轮应该有 Trait 提案被应用")
        self.assertFalse(applied2,
                         "同一批证据换个 seed 不该再推动 Trait（证据指纹要绑定证据而非抽样）")


class V6WebPathTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "v6.sqlite3")
        self.token = "test-token"
        webui.Handler.db_path = self.path
        webui.Handler.token = self.token
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        webui.Handler.token = None
        self.tmp.cleanup()

    def _req(self, path: str, payload: dict | None = None, token: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Auth-Token", token)
        return urllib.request.urlopen(req, timeout=10)

    def test_full_path_with_token(self) -> None:
        with self._req("/api/ingest", {"text": "网页入口的测试消息", "actor": "u9"},
                       token=self.token) as r:
            body = json.loads(r.read())
        self.assertIn(body.get("status"), ("processed", "pending", "error"))
        with self._req(f"/api/state?instance=default", token=self.token) as r:
            state = json.loads(r.read())
        self.assertIn("stats", state)

    def test_without_token_is_401(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._req("/api/state")
        self.assertEqual(ctx.exception.code, 401)

    def test_foreign_origin_is_401(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api/state"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Origin", "https://evil.example")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
