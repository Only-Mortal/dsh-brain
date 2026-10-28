"""推理后端测试（不依赖本机 Ollama 是否在跑）。"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn.inference import (  # noqa: E402
    RuleBackend, OllamaBackend, describe, get_backend, TEST_BANNER,
)
from newborn.reasoner import build_prompt, make_reasoner  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402


def env(text: str = "你好") -> EventEnvelope:
    return EventEnvelope(event_type="message.received", source="test",
                         payload={"text": text}, instance_id="t", actor_id="u")


class InferenceTest(unittest.TestCase):
    def test_rule_backend_always_available(self) -> None:
        b = RuleBackend()
        self.assertTrue(b.available())
        r = b.complete("sys", [{"role": "user", "content": "在吗"}])
        self.assertIn("在吗", r.text)
        self.assertTrue(r.text.startswith(TEST_BANNER))

    def test_dead_ollama_is_reported_unavailable(self) -> None:
        b = OllamaBackend(base="http://127.0.0.1:9", model="nope")
        self.assertFalse(b.available())

    def test_get_backend_falls_back_to_rule(self) -> None:
        old = os.environ.get("NEWBORN_OLLAMA_BASE")
        os.environ["NEWBORN_OLLAMA_BASE"] = "http://127.0.0.1:9"
        try:
            b = get_backend("ollama")
            self.assertEqual(b.name, "rule", "连不上 Ollama 时必须退回规则后端")
        finally:
            if old is None:
                os.environ.pop("NEWBORN_OLLAMA_BASE", None)
            else:
                os.environ["NEWBORN_OLLAMA_BASE"] = old

    def test_describe_marks_test_only(self) -> None:
        info = describe(RuleBackend())
        self.assertTrue(info["test_only"])

    def test_prompt_contains_state_and_memories(self) -> None:
        msgs = build_prompt(env("这句话"), {"valence": 0.1}, [{"epistemic_status": "reported",
                                                             "content": "他住在悉尼"}])
        body = msgs[0]["content"]
        self.assertIn("valence=0.10", body)
        self.assertIn("他住在悉尼", body)
        self.assertIn("这句话", body)

    def test_reasoner_falls_back_when_backend_errors(self) -> None:
        class Broken:
            name = "broken"

            def available(self) -> bool:
                return True

            def complete(self, system, messages, *, timeout=60.0):
                from newborn.inference import InferenceResult
                return InferenceResult(text="", backend="broken", error="boom")

        text = make_reasoner(Broken())(env("测试"), {"valence": 0.0}, [])
        self.assertIn("推理后端暂时不可用", text)


if __name__ == "__main__":
    unittest.main()
