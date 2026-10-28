"""M1 验收测试（对应 README 第 16 节）。

跑法：python -m unittest discover -s tests -v

上一轮审查指出的缺口，这里逐条补了测试：
- 会话隐私隔离
- 推理失败后事件仍可恢复
- Outbox 并发领取与租约
- 幂等键跨实例作用域
- 写入不变量（非法内容/层名/修订目标）
- 不是所有消息都成长期记忆
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import db, kernel, memory as memory_mod, state as state_mod  # noqa: E402
from newborn.affect import Affect  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402
from newborn.memory import AccessContext, MemoryError, MemoryObject  # noqa: E402
from newborn.state import StateError  # noqa: E402


def make_env(text: str, key: str | None = None, instance: str = "t1", *,
             conversation: str | None = "c1", actor: str | None = "user1",
             privacy: str = "private") -> EventEnvelope:
    return EventEnvelope(
        event_type="message.received",
        source="test",
        payload={"text": text},
        instance_id=instance,
        actor_id=actor,
        conversation_id=conversation,
        privacy_scope=privacy,
        idempotency_key=key,
    )


def ctx(instance: str = "t1", conversation: str | None = "c1",
        actor: str | None = "user1") -> AccessContext:
    return AccessContext(instance_id=instance, conversation_id=conversation,
                         subject_id=actor, purpose="test")


class M1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")

    # ---------------------------------------------------------- 基本闭环

    def test_duplicate_event_has_no_side_effects(self) -> None:
        r1 = kernel.process_event(self.conn, make_env("我喜欢简短的回答", key="k1"))
        r2 = kernel.process_event(self.conn, make_env("随便变了", key="k1"))
        self.assertEqual(r1["status"], "processed")
        self.assertEqual(r2["status"], "duplicate")
        n = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        self.assertEqual(n, 1, "重复事件不该再写一条记忆")

    def test_memory_has_provenance_and_is_retrievable(self) -> None:
        res = kernel.process_event(self.conn, make_env("我住在悉尼"))
        mems = memory_mod.retrieve(self.conn, ctx(), query="悉尼")
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0]["source_event_ids"], [res["event_id"]])
        self.assertEqual(mems[0]["epistemic_status"], "reported")

    def test_rejected_event_does_nothing(self) -> None:
        env = EventEnvelope(event_type="", source="test", payload={})
        res = kernel.process_event(self.conn, env)
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    # ------------------------------------------------ 1. 会话隐私隔离

    def test_conversation_privacy_isolated(self) -> None:
        kernel.process_event(
            self.conn,
            make_env("这是 A 会话的秘密", key="a1", conversation="A", privacy="conversation"),
        )
        in_a = memory_mod.retrieve(self.conn, ctx(conversation="A"), query="秘密")
        in_b = memory_mod.retrieve(self.conn, ctx(conversation="B"), query="秘密")
        self.assertEqual(len(in_a), 1, "A 会话应能读到自己的记忆")
        self.assertEqual(len(in_b), 0, "B 会话不能读到 A 会话的 conversation 级记忆")

    def test_private_memory_needs_matching_subject(self) -> None:
        kernel.process_event(self.conn, make_env("只有我能看", key="p1", actor="u1"))
        self.assertEqual(len(memory_mod.retrieve(self.conn, ctx(actor="u1"))), 1)
        self.assertEqual(len(memory_mod.retrieve(self.conn, ctx(actor="u2"))), 0)

    def test_retrieve_requires_access_context(self) -> None:
        with self.assertRaises(TypeError):
            memory_mod.retrieve(self.conn, "t1")  # 旧签名不再被接受

    # -------------------------------------------- 2. 推理失败可恢复

    def test_reasoner_failure_keeps_event(self) -> None:
        def boom(env, affect, memories):
            raise RuntimeError("推理后端挂了")

        env = make_env("这条不能丢", key="f1")
        res = kernel.process_event(self.conn, env, boom)
        self.assertEqual(res["status"], "error")
        row = self.conn.execute("SELECT process_state FROM events WHERE event_id = ?",
                                (env.event_id,)).fetchone()
        self.assertIsNotNone(row, "事件必须已经持久化")
        self.assertEqual(row["process_state"], "failed")

    def test_failed_event_can_be_retried(self) -> None:
        def boom(env, affect, memories):
            raise RuntimeError("挂了")

        env = make_env("重试我", key="f2")
        kernel.process_event(self.conn, env, boom)
        results = kernel.process_pending(self.conn)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "processed")
        self.assertEqual(results[0]["event_id"], env.event_id)

    # -------------------------------------------- 3. Outbox 领取

    def test_outbox_claimed_once(self) -> None:
        kernel.process_event(self.conn, make_env("给我回一句", key="o1"))
        first = kernel.claim_actions(self.conn, "w1")
        second = kernel.claim_actions(self.conn, "w2")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0, "已领取的行动不能被另一个 worker 再拿到")

    def test_outbox_lease_expiry_allows_takeover(self) -> None:
        kernel.process_event(self.conn, make_env("租约测试", key="o2"))
        claimed = kernel.claim_actions(self.conn, "w1", lease_seconds=0.01)
        self.assertEqual(len(claimed), 1)
        time.sleep(0.05)
        again = kernel.claim_actions(self.conn, "w2", lease_seconds=30)
        self.assertEqual(len(again), 1, "租约过期后应可被接管")

    def test_illegal_outbox_transition_rejected(self) -> None:
        kernel.process_event(self.conn, make_env("状态机", key="o3"))
        claimed = kernel.claim_actions(self.conn, "w1")
        action_id = claimed[0]["action_id"]
        with self.assertRaises(ValueError):
            kernel.finish_action(self.conn, action_id, "pending")  # running → pending 非法
        kernel.finish_action(self.conn, action_id, "succeeded")

    # -------------------------------------------- 4. 写入不变量

    def test_insert_rejects_empty_content(self) -> None:
        bad = MemoryObject(content="   ", memory_type="semantic", source_event_ids=[])
        with self.assertRaises(MemoryError):
            memory_mod.insert(self.conn, bad)

    def test_episodic_requires_source_event(self) -> None:
        bad = MemoryObject(content="没有来源的经历", memory_type="episodic")
        with self.assertRaises(MemoryError):
            memory_mod.insert(self.conn, bad)

    def test_revise_unknown_memory_rejected(self) -> None:
        new = MemoryObject(content="新版", memory_type="semantic")
        with self.assertRaises(MemoryError):
            memory_mod.revise(self.conn, "not_exists", new, reason="测试")

    def test_unknown_state_layer_rejected(self) -> None:
        with self.assertRaises(StateError):
            state_mod.put(self.conn, "t1", "affectivee", {"x": 1})

    def test_state_version_increments(self) -> None:
        state_mod.put(self.conn, "t1", "identity", {"name": "NeuroMind"})
        v = state_mod.put(self.conn, "t1", "identity", {"name": "NeuroMind v1"})
        self.assertEqual(v, 2)
        with self.assertRaises(StateError):
            state_mod.put(self.conn, "t1", "identity", {"name": "冲突"}, expect_version=1)

    # -------------------------------------------- 5. 幂等键作用域

    def test_same_key_different_instances_both_processed(self) -> None:
        r1 = kernel.process_event(self.conn, make_env("实例一", key="same", instance="i1"))
        r2 = kernel.process_event(self.conn, make_env("实例二", key="same", instance="i2"))
        self.assertEqual(r1["status"], "processed")
        self.assertEqual(r2["status"], "processed", "幂等键只在本实例内生效")

    # -------------------------------------------- 6. 记忆门槛

    def test_short_or_repeated_text_does_not_pile_up(self) -> None:
        kernel.process_event(self.conn, make_env("好", key="s1"))
        empty_active = self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE lifecycle_state = 'active'").fetchone()[0]
        self.assertEqual(empty_active, 0, "太短的话不该进长期记忆")

        kernel.process_event(self.conn, make_env("这句话会被说两次", key="s2"))
        dup = kernel.process_event(self.conn, make_env("这句话会被说两次", key="s3"))
        self.assertIsNotNone(dup["duplicate_memory_of"], "同内容应识别为重复")
        n = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        self.assertEqual(n, 1)

    # -------------------------------------------- 情绪

    def test_affect_decays_toward_baseline(self) -> None:
        a = Affect()
        t0 = time.time()
        a.inject({"stress": 0.6}, at=t0)
        high = a.value(t0)["stress"]
        later = a.value(t0 + 1200 * 4)["stress"]
        self.assertGreater(high, later, "压力应随时间回落")
        self.assertLess(abs(later - 0.15), 0.05, "长时间后应接近基线")

    def test_affect_stays_in_bounds_under_spam(self) -> None:
        a = Affect()
        t0 = time.time()
        for i in range(50):
            a.inject({"stress": 0.9, "valence": -0.9}, at=t0 + i * 0.1)
        vals = a.value(t0 + 5)
        self.assertLessEqual(vals["stress"], 1.0)
        self.assertGreaterEqual(vals["valence"], -1.0)


if __name__ == "__main__":
    unittest.main()
