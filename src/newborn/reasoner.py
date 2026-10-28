"""把推理后端包成内核用的 reasoner。

内核只需要 `reasoner(env, affect, memories) -> str`，不用知道背后是 Ollama 还是规则。
提示词也在这里组装：只喂事实与状态，不替模型下结论（对齐 README 4.4 去结论化）。
"""

from __future__ import annotations

from typing import Any

from .events import EventEnvelope
from .inference import Inference, RuleBackend, get_backend

SYSTEM_PROMPT = """你是一个正在成长中的认知体的语言层，只负责把当前处境说成一句话。
规则：
- 用中文，1-2 句，口语，不要列表，不要标题，不要解释你在做什么。
- 只依据给定的事实与记忆，不编造经历。
- 不要声称自己有意识、有身体、有真实情绪；情绪只是状态数值。
- 不确定就说不确定。
"""


def build_prompt(env: EventEnvelope, affect: dict[str, float], memories: list[dict]) -> list[dict[str, str]]:
    text = str(env.payload.get("text", ""))
    memory_lines = "\n".join(
        f"- （{m['epistemic_status']}）{m['content'][:80]}" for m in memories[:5]
    ) or "- （暂无相关记忆）"
    affect_line = "，".join(f"{k}={v:.2f}" for k, v in affect.items())
    user = (
        f"当前状态：{affect_line}\n"
        f"相关记忆：\n{memory_lines}\n\n"
        f"刚收到的话：{text}\n\n"
        f"请回一句。"
    )
    return [{"role": "user", "content": user}]


def make_reasoner(backend: Inference | None = None):
    backend = backend or get_backend()

    def reasoner(env: EventEnvelope, affect: dict[str, float], memories: list[dict]) -> str:
        if isinstance(backend, RuleBackend):
            return backend.complete(SYSTEM_PROMPT, build_prompt(env, affect, memories)).text
        result = backend.complete(SYSTEM_PROMPT, build_prompt(env, affect, memories), timeout=90)
        if result.error or not result.text:
            # 后端抽风时不装死：退回规则一句话，并在文本里标出来
            fallback = RuleBackend().complete(SYSTEM_PROMPT, build_prompt(env, affect, memories))
            return f"{fallback.text}（推理后端暂时不可用：{result.error or '空输出'}）"
        return result.text

    return reasoner
