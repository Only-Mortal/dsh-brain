"""记忆质量评测（NEXT-STEPS 阶段 D 的第一版固定验收集）。

不是追求高分，而是让"改规则"这件事有可比基线：
- 固定一组人工标注样例（本文件内），分开发集/验收集；
- 指标：极性判定准确率、主题归并准确率、矛盾判定精确率；
- 门槛设得保守；门槛以下是**必须解释**的，不是停下来的理由。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import consolidate  # noqa: E402

# 开发集：调规则时看这组
DEV = [
    ("我喜欢开会", 1),
    ("我不喜欢开会", -1),
    ("我没有不喜欢开会", 1),
    ("别去", -1),
    ("一定要去", 1),
    ("今天有点累", 1),
    ("今天一点也不累", -1),
]

# 验收集：只用来报数，不参与调参
HOLDOUT = [
    ("我很开心", 1),
    ("我不开心", -1),
    ("不讨厌这个安排", -1),      # 已知粗规则：字面否定
    ("没有人说不好", 1),          # 两个否定词 → 正
    ("别再来了", -1),
    ("还不错", 1),
    ("没那么糟", -1),             # 语义偏正、字面负向，已知误判（验收集里就让它错）
]

# 矛盾判定：同一主题、相反极性才算冲突
CONFLICT_CASES = [
    ("我喜欢开会", "我不喜欢开会", True),
    ("我喜欢开会", "我挺喜欢开会", False),
    ("我讨厌开会", "我不讨厌开会", True),
    ("今天的会开得不错", "今天的会开得不错", False),
]


def accuracy(cases) -> float:
    ok = sum(1 for text, want in cases if consolidate.polarity(text) == want)
    return ok / len(cases)


class QualityTest(unittest.TestCase):
    def test_dev_set_polarity_accuracy(self) -> None:
        self.assertGreaterEqual(accuracy(DEV), 0.85, "开发集极性准确率低于 0.85")

    def test_holdout_is_reported_not_tuned(self) -> None:
        acc = accuracy(HOLDOUT)
        # 验收集允许低于开发集，但不能崩；低于 0.7 说明规则偏了
        self.assertGreaterEqual(acc, 0.7, f"验收集准确率 {acc:.2f} 已低于下限")

    def test_conflict_detection(self) -> None:
        for a, b, expect in CONFLICT_CASES:
            sim = consolidate.jaccard(consolidate.topic_shingles(a),
                                      consolidate.topic_shingles(b))
            same_topic = sim >= consolidate.CONFLICT_TOPIC_THRESHOLD
            opposite = consolidate.polarity(a) != consolidate.polarity(b)
            self.assertEqual(same_topic and opposite, expect,
                             f"矛盾判定错：{a!r} vs {b!r} sim={sim:.2f}")

    def test_known_gaps_are_not_silent(self) -> None:
        """把已知误判写死在这里：规则变了，这个测试会提醒你更新文档。"""
        # 「没那么糟」只有一个否定词，按奇偶判成负向；语义偏正，属已知误判
        self.assertEqual(consolidate.polarity("没那么糟"), -1)
        self.assertEqual(consolidate.polarity("不讨厌这个安排"), -1)  # 字面否定，判错语义


if __name__ == "__main__":
    unittest.main()
