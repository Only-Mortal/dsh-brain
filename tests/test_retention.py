"""阶段 B/E 验收：删除、隐私代次、派生一致性、反馈与 Trait。

要证明的：
1. 删除单个对象后，检索不到，且派生语义退出默认检索；
2. 删除会话/实例按范围生效；
3. 隐私代次会递增，巩固在代次变化时整轮放弃；
4. 撤销账本独立于数据库，恢复旧备份后可重放，删掉的内容不会复活；
5. 反馈能翻转/确认记忆，Trait 只由反馈提出（没有反馈就不动状态）。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import backup, consolidate, db, feedback, kernel, retention  # noqa: E402
from newborn import memory as memory_mod  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402
from newborn.memory import AccessContext  # noqa: E402

NOW = int(time.time() // 60) * 60


def env(key: str, text: str, actor="u1", conv="c1") -> EventEnvelope:
    return EventEnvelope(
        event_type="message.received", source="v", payload={"text": text},
        instance_id="default", actor_id=actor, conversation_id=conv,
        privacy_scope="conversation", trust_level="user", idempotency_key=key)


def retrieve(conn, who="u1", conv="c1"):
    return memory_mod.retrieve(conn, AccessContext(
        instance_id="default", conversation_id=conv, subject_id=who))


class RetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "b.sqlite3")
        self.conn = db.connect(self.path)
        for i in range(4):
            kernel.process_event(self.conn, env(f"k{i}", f"第{i}条经历 关于加班"))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _mem_ids(self) -> list[str]:
        return [r["memory_id"] for r in self.conn.execute(
            "SELECT memory_id FROM memories WHERE memory_type='episodic'").fetchall()]

    def test_delete_object_hides_it(self) -> None:
        mid = self._mem_ids()[0]
        res = retention.request_deletion(self.conn, self.path, "default", "object", mid)
        self.assertEqual(res["status"], "cleaned")
        rows = [r["memory_id"] for r in retrieve(self.conn)]
        self.assertNotIn(mid, rows, "删掉的记忆不该还能被检索到")
        state = self.conn.execute(
            "SELECT lifecycle_state FROM memories WHERE memory_id=?", (mid,)).fetchone()
        self.assertEqual(state["lifecycle_state"], "deleted")

    def test_delete_advances_privacy_epoch(self) -> None:
        before = retention.privacy_epoch(self.conn, "default")
        retention.request_deletion(self.conn, self.path, "default", "object", self._mem_ids()[0])
        self.assertEqual(retention.privacy_epoch(self.conn, "default"), before + 1)

    def test_delete_writes_ledger(self) -> None:
        retention.request_deletion(self.conn, self.path, "default", "object", self._mem_ids()[0])
        ledger = retention.ledger_path(self.path)
        self.assertTrue(ledger.exists())
        lines = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["scope"], "object")
        # 账本不含正文
        self.assertNotIn("加班", ledger.read_text(encoding="utf-8"))

    def test_delete_conversation_scope(self) -> None:
        self.conn.execute("UPDATE memories SET conversation_id='c2' WHERE rowid = 1")
        res = retention.request_deletion(self.conn, self.path, "default", "conversation",
                                         "default|c1")
        self.assertEqual(res["status"], "cleaned")
        left = self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE conversation_id='c1'"
            " AND lifecycle_state <> 'deleted'").fetchone()[0]
        self.assertEqual(left, 0)

    def test_restore_does_not_revive_deleted(self) -> None:
        """备份 → 删除 → 恢复旧备份 → 重放账本 → 删掉的内容依然不可见。"""
        bdir = Path(self.tmp.name) / "bk"
        backup.create_backup(self.path, bdir)
        mid = self._mem_ids()[0]
        retention.request_deletion(self.conn, self.path, "default", "object", mid)
        target = Path(self.tmp.name) / "restored.sqlite3"
        backup.restore_backup(bdir, target, confirm=True)
        conn = db.connect(target)
        try:
            # 恢复出来的旧库里它还在
            row = conn.execute("SELECT lifecycle_state FROM memories WHERE memory_id=?",
                               (mid,)).fetchone()
            self.assertEqual(row["lifecycle_state"], "active")
            # 重放账本后必须再次消失（备份里没有账本，这是设计上的外部依赖）
            conn.close()
            conn = db.connect(target)
            retention.replay_ledger(conn, retention.ledger_path(self.path))
            row = conn.execute("SELECT lifecycle_state FROM memories WHERE memory_id=?",
                               (mid,)).fetchone()
            self.assertEqual(row["lifecycle_state"], "deleted")
        finally:
            conn.close()


class ConsolidationEpochTest(unittest.TestCase):
    def test_epoch_change_aborts_round(self) -> None:
        import uuid
        conn = db.connect(":memory:")
        for d in range(2):
            for i in range(3):
                mid = "epi_" + uuid.uuid4().hex[:8]
                ts = NOW - d * 86400 - i
                conn.execute(
                    "INSERT INTO memories (memory_id, instance_id, conversation_id, subject_ids,"
                    " memory_type, content, source_event_ids, provenance, epistemic_status,"
                    " confidence, importance, salience, occurred_at, created_at, updated_at,"
                    " lifecycle_state, privacy_scope)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, "default", "c1", '["u1"]', "episodic", f"第{d}天第{i}次加班",
                     json.dumps([mid]), "user_statement", "reported", .6, .4, .5,
                     ts, ts, ts, "active", "conversation"))
        import newborn.retention as r
        original = r.privacy_epoch
        calls = {"n": 0}

        def flaky(c, i):
            calls["n"] += 1
            return 0 if calls["n"] == 1 else 1    # 第二次读时假装代次变了

        r.privacy_epoch = flaky
        try:
            res = consolidate.consolidate(conn, "default", window_hours=72, seed=1, min_days=1)
        finally:
            r.privacy_epoch = original
        self.assertEqual(res["status"], "privacy_epoch_changed", res)


class FeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        kernel.process_event(self.conn, env("fb-1", "我不喜欢开会"))
        row = self.conn.execute(
            "SELECT memory_id FROM memories WHERE memory_type='episodic'").fetchone()
        self.mid = row["memory_id"]

    def test_reject_marks_disputed(self) -> None:
        feedback.add_feedback(self.conn, "default", "reject", memory_id=self.mid, note="说错了")
        row = self.conn.execute(
            "SELECT epistemic_status, confidence FROM memories WHERE memory_id=?",
            (self.mid,)).fetchone()
        self.assertEqual(row["epistemic_status"], "disputed")
        self.assertLessEqual(row["confidence"], 0.2)

    def test_confirm_raises_confidence(self) -> None:
        before = self.conn.execute("SELECT confidence FROM memories WHERE memory_id=?",
                                   (self.mid,)).fetchone()["confidence"]
        feedback.add_feedback(self.conn, "default", "confirm", memory_id=self.mid)
        after = self.conn.execute("SELECT confidence FROM memories WHERE memory_id=?",
                                  (self.mid,)).fetchone()["confidence"]
        self.assertGreater(after, before)

    def test_trait_only_from_feedback(self) -> None:
        # 没有反馈：不产生 Trait 变化
        self.assertEqual(feedback.trait_deltas_from_feedback(self.conn, "default"), {})
        feedback.add_feedback(self.conn, "default", "confirm")
        feedback.add_feedback(self.conn, "default", "correct")
        deltas = feedback.trait_deltas_from_feedback(self.conn, "default")
        self.assertTrue(deltas)
        self.assertTrue(feedback.evidence_fingerprint(self.conn, "default"))


if __name__ == "__main__":
    unittest.main()
