"""中文否定与矛盾识别验收（审计 #12）。

背景：审计指出「我喜欢开会」和「我不喜欢开会」当时特征相似度 0.4，低于冲突阈值，
于是被当成两个普通推断命题，而不是 disputed。

修法两层：
1. 主题指纹先去否定词（strip_negation），让「说同一件事」先识别出来；
2. 极性按否定词次数的奇偶判定，双重否定回到正向。

注意：这是粗规则。反讽、反问、"有人说他不喜欢…"这类引述都识别不了，
所以下面同时标了「已知不覆盖」的样例，避免误以为已经完备。
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import consolidate, db  # noqa: E402

NOW = int(time.time() // 60) * 60


def epi(conn, text: str, ts: float, actor="alice", conv="c1", privacy="conversation") -> str:
    import uuid
    mid = "epi_" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO memories (memory_id, instance_id, conversation_id, subject_ids,"
        " memory_type, content, source_event_ids, provenance, epistemic_status, confidence,"
        " importance, salience, occurred_at, created_at, updated_at, lifecycle_state,"
        " privacy_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "default", conv, json.dumps([actor], ensure_ascii=False), "episodic", text,
         json.dumps([mid], ensure_ascii=False), "user_statement", "reported",
         0.6, 0.4, 0.5, ts, ts, ts, "active", privacy))
    return mid


class NegationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")

    # ------------------------------------------------------------- 词级

    def test_single_negation_is_negative(self) -> None:
        self.assertEqual(consolidate.polarity("我不喜欢开会"), -1)
        self.assertEqual(consolidate.polarity("别去"), -1)
        self.assertEqual(consolidate.polarity("never again"), -1)

    def test_double_negation_is_positive(self) -> None:
        self.assertEqual(consolidate.polarity("我没有不喜欢开会"), 1)
        # 「不讨厌」字面只有一个否定词，按奇偶算负向；语义上是正向，属已知粗规则
        self.assertEqual(consolidate.polarity("我不讨厌开会"), -1)

    def test_zero_negation_is_positive(self) -> None:
        self.assertEqual(consolidate.polarity("我喜欢开会"), 1)
        self.assertEqual(consolidate.polarity(""), 1)

    def test_topic_fingerprint_ignores_negation(self) -> None:
        a = consolidate.topic_shingles("我喜欢开会")
        b = consolidate.topic_shingles("我不喜欢开会")
        self.assertEqual(consolidate.jaccard(a, b), 1.0,
                         "去掉否定词后应指向同一主题")

    # ------------------------------------------------------------- 簇级

    def test_opposite_polarity_becomes_disputed(self) -> None:
        for i in range(5):
            epi(self.conn, f"我喜欢开会 第{i}次", NOW - i * 60)
        for i in range(3):
            epi(self.conn, f"我不喜欢开会 第{i}次", NOW - i * 60 - 30)
        res = consolidate.consolidate(self.conn, "default", window_hours=2, seed=1, min_days=1)
        self.assertEqual(res["status"], "done")

        disputed = self.conn.execute(
            "SELECT memory_id, epistemic_status FROM memories"
            " WHERE memory_type='semantic' AND epistemic_status='disputed'").fetchall()
        self.assertTrue(disputed, "极性冲突应至少产出一条 disputed")
        row = self.conn.execute(
            "SELECT contradicting_event_ids FROM memories WHERE memory_id = ?",
            (disputed[0]["memory_id"],)).fetchone()
        self.assertNotIn(row["contradicting_event_ids"], ("[]", None),
                         "disputed 必须记下反证来源")

    def test_same_polarity_is_not_disputed(self) -> None:
        for i in range(4):
            epi(self.conn, f"我喜欢开会 第{i}次", NOW - i * 60)
        for i in range(2):
            epi(self.conn, f"我挺喜欢开会 第{i}次", NOW - i * 60 - 20)
        res = consolidate.consolidate(self.conn, "default", window_hours=2, seed=2, min_days=1)
        self.assertEqual(res["status"], "done")
        disputed = self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE epistemic_status='disputed'").fetchone()[0]
        self.assertEqual(disputed, 0)

    # ------------------------------------------------- 已知不覆盖（写成测试，免得自欺）

    def test_known_gaps_are_documented(self) -> None:
        """这些样例当前判不出来，写下来是为了别把规则当成完备。"""
        gaps = {
            "有人说他不喜欢开会": -1,       # 引述他人否定，被算成负向
            "难道我不喜欢开会吗": -1,       # 反问，字面是负向
        }
        for text, expected in gaps.items():
            self.assertEqual(consolidate.polarity(text), expected)
        # 反讽「可真喜欢开会呢」会被判正向，属已知误判
        self.assertEqual(consolidate.polarity("可真喜欢开会呢"), 1)


if __name__ == "__main__":
    unittest.main()
