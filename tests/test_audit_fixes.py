"""审计修复的验收测试（2026-10-28 审计意见 1-9）。

覆盖：
- #1 巩固隐私继承 / 不跨主体聚簇
- #2 主体精确匹配（u1 ≠ u10 ≠ %）
- #3 Web UI 访问控制（回环判定、远程必须令牌）
- #5 任务键包含新证据
- #6 Trait 证据指纹（同一批证据只学一次）
- #7 语义版本链（version + supersedes_id）
- #9 事件原子领取（不会两个 worker 同时处理同一事件）
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import consolidate, db, kernel  # noqa: E402
from newborn import memory as memory_mod  # noqa: E402
from newborn import webui  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402
from newborn.memory import AccessContext  # noqa: E402

DAY = 86400.0


def add_epi(conn, text: str, *, actor="alice", conv="c1", ts=None, privacy="conversation",
            instance="default") -> str:
    ts = time.time() if ts is None else ts
    import uuid
    mid = "epi_" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO memories (memory_id, instance_id, conversation_id, subject_ids,"
        " memory_type, content, source_event_ids, provenance, epistemic_status, confidence,"
        " importance, salience, occurred_at, created_at, updated_at, lifecycle_state,"
        " privacy_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, instance, conv, json.dumps([actor], ensure_ascii=False), "episodic", text,
         json.dumps([f"evt_{mid}"], ensure_ascii=False), "user_statement", "reported",
         0.6, 0.4, 0.5, ts, ts, ts, "active", privacy))
    return mid


class AuditFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.now = int(time.time() // 60) * 60

    # ------------------------------------------------------------ #1 隐私继承

    def test_semantic_inherits_private_scope(self) -> None:
        for i in range(4):
            add_epi(self.conn, f"alice 的私房话第{i}句", actor="alice",
                    ts=self.now - i * 60, privacy="private")
        res = consolidate.consolidate(self.conn, "default", window_hours=2, seed=1,
                                      min_days=1, backend=None)
        self.assertEqual(res["status"], "done")
        sem = self.conn.execute(
            "SELECT * FROM memories WHERE memory_type='semantic' LIMIT 1").fetchone()
        self.assertIsNotNone(sem)
        self.assertEqual(sem["privacy_scope"], "private", "语义记忆必须继承最严范围")
        self.assertEqual(json.loads(sem["subject_ids"]), ["alice"])
        alice = memory_mod.retrieve(self.conn, AccessContext(
            instance_id="default", conversation_id="c1", subject_id="alice"))
        bob = memory_mod.retrieve(self.conn, AccessContext(
            instance_id="default", conversation_id="c1", subject_id="bob"))
        self.assertTrue(any(r["memory_type"] == "semantic" for r in alice))
        self.assertFalse(any(r["memory_type"] == "semantic" for r in bob),
                         "Bob 不该看到 Alice 巩固出来的语义记忆")

    def test_private_memories_of_different_subjects_not_merged(self) -> None:
        for i in range(4):
            add_epi(self.conn, f"今天很累想休息 第{i}次", actor="alice",
                    ts=self.now - i * 60, privacy="private")
            add_epi(self.conn, f"今天很累想休息 又第{i}次", actor="bob",
                    ts=self.now - i * 60 - 30, privacy="private")
        res = consolidate.consolidate(self.conn, "default", window_hours=2, seed=2,
                                      min_days=1)
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["clusters"], 2, "不同主体的私有经历不许聚成一簇")

    # ------------------------------------------------------------ #2 精确匹配

    def test_subject_match_is_exact(self) -> None:
        add_epi(self.conn, "u1 的私事", actor="u1", privacy="private")
        add_epi(self.conn, "u10 的私事", actor="u10", privacy="private")
        ctx = lambda who: AccessContext(instance_id="default", conversation_id="c1",
                                        subject_id=who)  # noqa: E731
        self.assertEqual([r["content"] for r in memory_mod.retrieve(self.conn, ctx("u1"))],
                         ["u1 的私事"])
        self.assertEqual([r["content"] for r in memory_mod.retrieve(self.conn, ctx("u10"))],
                         ["u10 的私事"])
        self.assertEqual(memory_mod.retrieve(self.conn, ctx("%")), [])

    # ------------------------------------------------------------ #3 Web UI

    def test_webui_loopback_detection(self) -> None:
        self.assertTrue(webui.is_loopback("127.0.0.1"))
        self.assertTrue(webui.is_loopback("::1"))
        self.assertFalse(webui.is_loopback("0.0.0.0"))
        self.assertFalse(webui.is_loopback("192.168.1.9"))

    def test_webui_refuses_public_bind_without_token(self) -> None:
        with self.assertRaises(SystemExit):
            webui.serve(":memory:", host="0.0.0.0", port=0, token=None)

    # ------------------------------------------------------------ #5 任务键

    def test_new_evidence_invalidates_job_key(self) -> None:
        for i in range(4):
            add_epi(self.conn, f"我在学写代码 第{i}次", ts=self.now - DAY * (i % 2) - i,
                    privacy="conversation")
        r1 = consolidate.consolidate(self.conn, "default", window_hours=72, seed=3,
                                     min_days=1)
        self.assertEqual(r1["status"], "done")
        # 同一窗口同一 seed，但没有新证据 → 幂等
        r2 = consolidate.consolidate(self.conn, "default", window_hours=72, seed=3,
                                     min_days=1)
        self.assertEqual(r2["status"], "duplicate_job")
        # 新证据进来 → 必须重新产出
        add_epi(self.conn, "我在学写代码 又记了一笔", ts=self.now - 10, privacy="conversation")
        r3 = consolidate.consolidate(self.conn, "default", window_hours=72, seed=3,
                                     min_days=1)
        self.assertEqual(r3["status"], "done", "新证据不能让任务被判成重复")

    # ------------------------------------------------------------ #7 版本链

    def test_semantic_version_chain(self) -> None:
        for i in range(3):
            add_epi(self.conn, f"每天都要复盘一下 第{i}次", ts=self.now - DAY * i - i,
                    privacy="conversation")
        r1 = consolidate.consolidate(self.conn, "default", window_hours=96, seed=4,
                                     min_days=1)
        self.assertEqual(r1["status"], "done")
        first = self.conn.execute(
            "SELECT memory_id, version, supersedes_id FROM memories"
            " WHERE memory_type='semantic' ORDER BY created_at LIMIT 1").fetchone()
        self.assertEqual(first["version"], 1)
        self.assertIsNone(first["supersedes_id"])

        for i in range(2):
            add_epi(self.conn, f"每天都要复盘一下 又第{i}次", ts=self.now - DAY * (i + 1) - 50,
                    privacy="conversation")
        r2 = consolidate.consolidate(self.conn, "default", window_hours=96, seed=5,
                                     min_days=1)
        self.assertEqual(r2["status"], "done")
        second = self.conn.execute(
            "SELECT memory_id, version, supersedes_id, lifecycle_state FROM memories"
            " WHERE memory_type='semantic' ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["supersedes_id"], first["memory_id"])
        old = self.conn.execute("SELECT lifecycle_state FROM memories WHERE memory_id = ?",
                                (first["memory_id"],)).fetchone()
        self.assertEqual(old["lifecycle_state"], "superseded")

    # ------------------------------------------------------------ #6 Trait 证据

    def test_trait_evidence_fingerprint_prevents_relearning(self) -> None:
        first = consolidate.apply_trait_proposal(
            self.conn, "default", "sociability", 0.02, cluster_id="clu_x",
            evidence=["clu_x"], evidence_fp="fp-1")
        self.assertTrue(first["applied"])
        second = consolidate.apply_trait_proposal(
            self.conn, "default", "sociability", 0.02, cluster_id="clu_x",
            evidence=["clu_x"], evidence_fp="fp-1")
        self.assertFalse(second["applied"], "同一批证据不能反复推动 Trait")
        third = consolidate.apply_trait_proposal(
            self.conn, "default", "sociability", 0.02, cluster_id="clu_x",
            evidence=["clu_x"], evidence_fp="fp-2")
        self.assertTrue(third["applied"], "换成新证据应该允许")

    # ------------------------------------------------------------ #9 事件领取

    def test_event_claimed_once(self) -> None:
        env = EventEnvelope(event_type="message.received", source="t", payload={"text": "待处理"},
                            instance_id="default", actor_id="u", conversation_id="c",
                            idempotency_key="pending-1")
        row = env.to_row()
        with db.Tx(self.conn):
            self.conn.execute(
                f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()))
        w1 = kernel.claim_events(self.conn, "w1", lease_seconds=30)
        w2 = kernel.claim_events(self.conn, "w2", lease_seconds=30)
        self.assertEqual(len(w1), 1)
        self.assertEqual(len(w2), 0, "租约未过期时第二个 worker 不该领到")
        time.sleep(0.02)
        w3 = kernel.claim_events(self.conn, "w3", lease_seconds=0.01)
        self.assertEqual(len(w3), 0, "短租约也应该先被 w1 占着")
        # 手动过期后应可接管
        self.conn.execute("UPDATE events SET lease_until = 0 WHERE event_id = ?",
                          (env.event_id,))
        w4 = kernel.claim_events(self.conn, "w4", lease_seconds=30)
        self.assertEqual(len(w4), 1, "租约过期后允许接管")


if __name__ == "__main__":
    unittest.main()
