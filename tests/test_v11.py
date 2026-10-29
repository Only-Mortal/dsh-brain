"""V1.1 新增能力验收：维护调度、物理清理、崩溃点。

崩溃点是 V1.0 明确没做的一项：进程在「领取后、提交前」死掉，恢复时必须只产生一个结果。
这里用「处理到一半抛异常」来模拟，而不是真的杀进程；两者对数据的影响一致：
事件还停在 pending/failed、租约还在、没有提交。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import db, kernel, retention, scheduler  # noqa: E402
from newborn import state as state_mod  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402

NOW = int(time.time() // 60) * 60


def epi(conn, text, ts):
    mid = "epi_" + uuid.uuid4().hex[:8]
    conn.execute(
        "INSERT INTO memories (memory_id, instance_id, conversation_id, subject_ids,"
        " memory_type, content, source_event_ids, provenance, epistemic_status, confidence,"
        " importance, salience, occurred_at, created_at, updated_at, lifecycle_state,"
        " privacy_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "default", "c1", '["u1"]', "episodic", text, json.dumps([mid]),
         "user_statement", "reported", .6, .4, .5, ts, ts, ts, "active", "conversation"))
    return mid


class SchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "s.sqlite3")
        self.conn = db.connect(self.path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_idle_when_not_enough_episodics(self) -> None:
        epi(self.conn, "只有一条", NOW)
        res = scheduler.run_once(self.conn, self.path, window_hours=72, min_gap_seconds=0)
        self.assertEqual(res["status"], "idle")

    def test_runs_when_due(self) -> None:
        for d in range(2):
            for i in range(4):
                epi(self.conn, f"第{d}天第{i}次加班", NOW - d * 86400 - i)
        res = scheduler.run_once(self.conn, self.path, window_hours=72, min_gap_seconds=0)
        self.assertEqual(res["status"], "done", res)
        jobs = self.conn.execute("SELECT COUNT(*) FROM consolidation_jobs").fetchone()[0]
        self.assertEqual(jobs, 1)

    def test_respects_min_gap(self) -> None:
        for d in range(2):
            for i in range(4):
                epi(self.conn, f"第{d}天第{i}次加班", NOW - d * 86400 - i)
        first = scheduler.run_once(self.conn, self.path, window_hours=72, min_gap_seconds=0)
        self.assertEqual(first["status"], "done")
        seconds = scheduler.run_once(self.conn, self.path, window_hours=72, min_gap_seconds=3600)
        self.assertEqual(seconds["status"], "idle")

    def test_forever_once(self) -> None:
        res = scheduler.serve_forever(self.path, once=True, interval=0)
        self.assertEqual(len(res), 1)


class PurgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "p.sqlite3")
        self.conn = db.connect(self.path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_purge_requires_confirm(self) -> None:
        res = retention.purge(self.conn, self.path)
        self.assertEqual(res["status"], "preview")
        self.assertTrue(res["confirm_required"])

    def test_purge_removes_deleted_rows(self) -> None:
        mid = epi(self.conn, "要被清理的内容", NOW)
        retention.request_deletion(self.conn, self.path, "default", "object", mid)
        res = retention.purge(self.conn, self.path, confirm=True)
        self.assertEqual(res["status"], "purged")
        left = self.conn.execute("SELECT COUNT(*) FROM memories WHERE memory_id=?",
                                 (mid,)).fetchone()[0]
        self.assertEqual(left, 0)
        # 正文不应再出现在库文件里
        raw = Path(self.path).read_bytes()
        self.assertNotIn("要被清理的内容".encode("utf-8"), raw)


class CrashPointTest(unittest.TestCase):
    """领取之后、提交之前挂掉：恢复后只能产生一个结果。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "c.sqlite3")
        self.conn = db.connect(self.path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _pending_event(self, key="crash-1") -> EventEnvelope:
        env = EventEnvelope(
            event_type="message.received", source="v", payload={"text": "崩溃点测试消息"},
            instance_id="default", actor_id="u1", conversation_id="c1",
            privacy_scope="conversation", trust_level="user", idempotency_key=key)
        row = env.to_row()
        self.conn.execute(
            f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()))
        return env

    def test_crash_before_commit_leaves_pending_then_recovers_once(self) -> None:
        env = self._pending_event()
        # 模拟：领取到了，但处理过程中异常退出（没有提交任何结果）
        claimed = kernel.claim_events(self.conn, "w-crash", lease_seconds=30)
        self.assertEqual(len(claimed), 1)

        def boom(*_a, **_k):
            raise RuntimeError("模拟进程在处理中挂掉")

        real = kernel._handle
        kernel._handle = boom
        try:
            with self.assertRaises(RuntimeError):
                kernel.process_event(self.conn, env)
        finally:
            kernel._handle = real

        row = self.conn.execute("SELECT process_state FROM events WHERE event_id=?",
                                (env.event_id,)).fetchone()
        self.assertEqual(row["process_state"], "pending", "崩溃后事件必须还在，不能凭空丢失")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0,
                         "崩溃前不该留下半条回复")

        # 恢复：租约过期后接管，只产出一次
        self.conn.execute("UPDATE events SET lease_until = 0 WHERE event_id = ?",
                          (env.event_id,))
        first = kernel.process_pending(self.conn, worker_id="w-recover")
        second = kernel.process_pending(self.conn, worker_id="w-recover2")
        self.assertEqual(len([r for r in first if r.get("status") == "processed"]), 1)
        self.assertEqual(len(second), 0, "恢复之后不该再处理一次")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
