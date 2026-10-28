"""巩固任务（皮层）验收测试。

覆盖发起者定下的硬约束：
- 分层抽样：按天/会话上限、覆盖率、最大占比、同 seed 可复现
- 幂等：同窗口同 seed 同算法版本只产一次
- support_count 与 independent_source_count 分开
- 语义记忆版本演进：strengthened / weakened
- Trait 提案：单次限幅 + 当日绝对累计（防正负抵消）
- Identity 只读
- 无模型（规则后端）也能跑通，模型只影响 display_text
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import db, consolidate  # noqa: E402
from newborn.inference import RuleBackend  # noqa: E402

DAY = 86400.0


def insert_episodic(conn, text: str, *, actor: str = "u1", conv: str = "c1",
                    created_at: float, instance: str = "t1") -> str:
    mid = "epi_" + str(abs(hash((text, created_at))))[:12]
    conn.execute(
        "INSERT INTO memories (memory_id, instance_id, conversation_id, subject_ids,"
        " memory_type, content, source_event_ids, provenance, epistemic_status, confidence,"
        " importance, salience, occurred_at, created_at, updated_at, lifecycle_state,"
        " privacy_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, instance, conv, json.dumps([actor], ensure_ascii=False), "episodic", text,
         json.dumps([f"evt_{mid}"], ensure_ascii=False), "user_statement", "reported",
         0.6, 0.4, 0.5, created_at, created_at, created_at, "active", "conversation"),
    )
    return mid


class ConsolidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.now = int(time.time() // 60) * 60.0

    def seed_days(self, n_days: int = 3, per_day: int = 10) -> None:
        for d in range(n_days):
            ts = self.now - d * DAY
            for i in range(per_day):
                insert_episodic(self.conn, f"今天有点累 第{d}天 第{i}条记录", actor=f"u{i % 3}",
                                conv=f"c{i % 2}", created_at=ts)

    # ---------------------------------------------------------------- 抽样

    def test_sampling_respects_caps_and_coverage(self) -> None:
        self.seed_days(3, 30)
        sample = consolidate.sample_episodics(
            self.conn, "t1", self.now - 5 * DAY, self.now, seed=7, per_day=5, per_conv=8)
        dist = sample.distribution
        self.assertLessEqual(len(sample.rows), 15)
        self.assertGreaterEqual(dist["days"], 2)
        self.assertTrue(all(v <= 5 for v in dist["by_day"].values()))
        self.assertEqual(consolidate.check_sampling(dist), [])

    def test_sampling_reproducible_with_seed(self) -> None:
        self.seed_days(3, 10)
        a = consolidate.sample_episodics(self.conn, "t1", self.now - 5 * DAY, self.now, seed=42)
        b = consolidate.sample_episodics(self.conn, "t1", self.now - 5 * DAY, self.now, seed=42)
        self.assertEqual([r["memory_id"] for r in a.rows], [r["memory_id"] for r in b.rows])
        c = consolidate.sample_episodics(self.conn, "t1", self.now - 5 * DAY, self.now, seed=43)
        self.assertNotEqual([r["memory_id"] for r in a.rows], [r["memory_id"] for r in c.rows])

    def test_single_day_sample_is_rejected(self) -> None:
        for i in range(10):
            insert_episodic(self.conn, f"只有今天的记录 {i}", created_at=self.now)
        sample = consolidate.sample_episodics(self.conn, "t1", self.now - DAY, self.now, seed=1)
        problems = consolidate.check_sampling(sample.distribution)
        self.assertTrue(any("天数" in p for p in problems))

    # ---------------------------------------------------------------- 幂等

    def test_consolidation_job_is_idempotent(self) -> None:
        self.seed_days(2, 5)
        r1 = consolidate.consolidate(self.conn, "t1", window_hours=5 * 24, seed=7)
        r2 = consolidate.consolidate(self.conn, "t1", window_hours=5 * 24, seed=7)
        self.assertEqual(r1["status"], "done")
        self.assertEqual(r2["status"], "duplicate_job", "同窗口同 seed 不该重复产出")
        n = self.conn.execute("SELECT COUNT(*) FROM memories WHERE memory_type='semantic'"
                              ).fetchone()[0]
        self.assertEqual(n, r1["clusters"])

    # ---------------------------------------------------------------- 统计

    def test_support_vs_independent_sources(self) -> None:
        for i in range(10):
            insert_episodic(self.conn, f"我真的很讨厌开会 第{i}次说", actor="same_user",
                            created_at=self.now - i * 60)
        res = consolidate.consolidate(self.conn, "t1", window_hours=24, seed=3, min_days=1)
        self.assertEqual(res["status"], "done")
        item = res["items"][0]
        self.assertEqual(item["support_count"], 10, "十次表达应算十次支持")
        self.assertEqual(item["independent_source_count"], 1,
                         "同一主体反复说只算一个独立来源")

    def test_second_window_strengthens(self) -> None:
        for i in range(5):
            insert_episodic(self.conn, f"我最近总在熬夜写代码 第{i}次", actor="u1",
                            created_at=self.now - 2 * DAY + i)
        r1 = consolidate.consolidate(self.conn, "t1", window_hours=72, seed=1, min_days=1)
        self.assertEqual(r1["items"][0]["change_kind"], "new")
        for i in range(5):
            insert_episodic(self.conn, f"我最近总在熬夜写代码 又第{i}次", actor="u1",
                            created_at=self.now - i)
        # 用不同窗口+seed 强制再跑一次（避开幂等）
        r2 = consolidate.consolidate(self.conn, "t1", window_hours=71.5, seed=2, min_days=1)
        kinds = {i["change_kind"] for i in r2["items"]}
        self.assertIn("strengthened", kinds, "新证据加入应升级而不是新建概念")

    # ---------------------------------------------------------------- Trait

    def test_trait_step_and_daily_caps(self) -> None:
        a = consolidate.apply_trait_proposal(self.conn, "t1", "sociability", 0.04,
                                            cluster_id=None, evidence=[])
        self.assertTrue(a["applied"])
        b = consolidate.apply_trait_proposal(self.conn, "t1", "sociability", 0.04,
                                            cluster_id=None, evidence=[])
        self.assertTrue(b["applied"])
        c = consolidate.apply_trait_proposal(self.conn, "t1", "sociability", 0.04,
                                            cluster_id=None, evidence=[])
        self.assertFalse(c["applied"], "当日累计 0.12 超过 0.10 必须拒")
        d = consolidate.apply_trait_proposal(self.conn, "t1", "sociability", 0.06,
                                            cluster_id=None, evidence=[])
        self.assertFalse(d["applied"], "单次 0.06 超过 0.05 必须拒")

    def test_trait_caps_cannot_be_bypassed_by_sign_cancellation(self) -> None:
        consolidate.apply_trait_proposal(self.conn, "t1", "curiosity", 0.05,
                                        cluster_id=None, evidence=[])
        consolidate.apply_trait_proposal(self.conn, "t1", "curiosity", -0.05,
                                        cluster_id=None, evidence=[])
        third = consolidate.apply_trait_proposal(self.conn, "t1", "curiosity", 0.05,
                                                cluster_id=None, evidence=[])
        self.assertFalse(third["applied"], "正负抵消后仍应算作已用 0.10")

    # ---------------------------------------------------------------- 只读

    def test_identity_untouched(self) -> None:
        before = self.conn.execute(
            "SELECT COUNT(*) FROM states WHERE layer='identity'").fetchone()[0]
        self.seed_days(2, 5)
        consolidate.consolidate(self.conn, "t1", window_hours=5 * 24, seed=9)
        after = self.conn.execute(
            "SELECT COUNT(*) FROM states WHERE layer='identity'").fetchone()[0]
        self.assertEqual(before, after, "巩固任务不得写 identity")

    # ---------------------------------------------------------------- 无模型

    def test_rule_backend_is_acceptable_baseline(self) -> None:
        self.seed_days(2, 5)
        res = consolidate.consolidate(self.conn, "t1", window_hours=5 * 24, seed=11,
                                      backend=RuleBackend())
        self.assertEqual(res["status"], "done")
        for item in res["items"]:
            self.assertEqual(item["display_source"], "template")
            self.assertTrue(item["display_text"])
        trait_rows = self.conn.execute("SELECT COUNT(*) FROM trait_proposals").fetchone()[0]
        self.assertGreater(trait_rows, 0, "无模型也要产 trait 提案")


if __name__ == "__main__":
    unittest.main()
