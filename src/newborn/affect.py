"""模拟情绪（README 第 7 节）。

按发起者的裁决：ΔE 本身也按记忆曲线衰减，而不是一次性叠加后永远留着。

模型
----
每一维维护一串「脉冲」：ΔE 在事件发生时注入，之后按 exp(-Δt/τ_Δ) 衰减（遗忘曲线）。
任意时刻的有效值：

    value(t) = clip(baseline + Σ_k ΔE_k · exp(-(t - t_k) / τ_Δ), [l, u])

这样稀疏事件时脉冲会自然消失，密集事件也不会无限累积——因为旧脉冲已经衰减掉了。

注意：这里只影响注意力、表达风格和候选行动排序，不参与证据与授权判断。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

DIMS = ("valence", "arousal", "curiosity", "stress", "social")

# 每维：基线、下界、上界、脉冲时间常数（秒）
PROFILE: dict[str, tuple[float, float, float, float]] = {
    "valence": (0.15, -1.0, 1.0, 1800.0),
    "arousal": (0.30, 0.0, 1.0, 900.0),
    "curiosity": (0.45, 0.0, 1.0, 2400.0),
    "stress": (0.15, 0.0, 1.0, 1200.0),
    "social": (0.40, 0.0, 1.0, 3600.0),
}

# 低于这个幅度的脉冲直接丢掉，防止无限增长
EPSILON = 1e-3
MAX_IMPULSES = 64


@dataclass
class Affect:
    """一维状态集合，序列化进 states 表。"""

    impulses: list[dict[str, float]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Affect":
        if not data:
            return cls()
        return cls(
            impulses=list(data.get("impulses") or []),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"impulses": self.impulses, "updated_at": self.updated_at}

    def inject(self, deltas: dict[str, float], at: float | None = None) -> None:
        t = time.time() if at is None else at
        for dim, delta in deltas.items():
            if dim not in PROFILE or not delta:
                continue
            self.impulses.append({"dim": dim, "delta": float(delta), "t": t})
        self.updated_at = t
        self._prune(t)

    def _prune(self, now: float) -> None:
        kept = []
        for imp in self.impulses:
            dim = imp.get("dim")
            if dim not in PROFILE:
                continue
            _, _, _, tau = PROFILE[dim]
            remaining = imp["delta"] * math.exp(-max(0.0, now - imp["t"]) / tau)
            if abs(remaining) >= EPSILON:
                kept.append(imp)
        if len(kept) > MAX_IMPULSES:
            kept = kept[-MAX_IMPULSES:]
        self.impulses = kept

    def value(self, now: float | None = None) -> dict[str, float]:
        t = time.time() if now is None else now
        out: dict[str, float] = {}
        for dim in DIMS:
            base, lo, hi, _ = PROFILE[dim]
            acc = base
            for imp in self.impulses:
                if imp.get("dim") != dim:
                    continue
                _, _, _, tau = PROFILE[dim]
                acc += imp["delta"] * math.exp(-max(0.0, t - imp["t"]) / tau)
            out[dim] = round(min(hi, max(lo, acc)), 4)
        return out
