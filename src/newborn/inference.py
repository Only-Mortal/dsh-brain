"""推理后端（InferenceAPI，README 第 12 节）。

设计：
- 统一接口 `Inference`：`complete(system, messages, ...) -> InferenceResult`。
- 目前两个实现：
    * `OllamaBackend` —— 本机 Ollama（**标记为测试用**：模型小、输出可能不稳）
    * `RuleBackend`   —— 不依赖模型的规则回复，离线也能跑，测试用
- 通过环境变量选择，换模型不改内核：
    NEWBORN_INFER_BACKEND=ollama|rule   （默认 ollama，未就绪自动退回 rule）
    NEWBORN_OLLAMA_BASE=http://127.0.0.1:11434
    NEWBORN_OLLAMA_MODEL=qwen2.5:1.5b
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:1.5b"
TEST_BANNER = "【测试后端】"


@dataclass
class InferenceResult:
    text: str
    backend: str
    model: str | None = None
    latency_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text, "backend": self.backend, "model": self.model,
            "latency_ms": self.latency_ms, "usage": self.usage, "error": self.error,
        }


class Inference(Protocol):
    name: str

    def available(self) -> bool: ...

    def complete(self, system: str, messages: list[dict[str, str]], *,
                 timeout: float = 60.0) -> InferenceResult: ...


class RuleBackend:
    """不依赖模型。用于离线验收与回归基线。"""

    name = "rule"

    def available(self) -> bool:
        return True

    def complete(self, system: str, messages: list[dict[str, str]], *,
                 timeout: float = 60.0) -> InferenceResult:
        started = time.time()
        last = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last = m.get("content", "")
                break
        text = f"{TEST_BANNER}收到：{last[:60]}" if last else f"{TEST_BANNER}……"
        return InferenceResult(text=text, backend=self.name, model=None,
                               latency_ms=int((time.time() - started) * 1000))


class OllamaBackend:
    """本机 Ollama。**测试用**：默认只挂小模型，不作为能力承诺。"""

    name = "ollama"

    def __init__(self, base: str | None = None, model: str | None = None,
                 timeout: float = 60.0) -> None:
        self.base = (base or os.environ.get("NEWBORN_OLLAMA_BASE") or DEFAULT_BASE).rstrip("/")
        self.model = model or os.environ.get("NEWBORN_OLLAMA_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/api/tags", timeout=3) as resp:
                tags = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return False
        names = {m.get("name") for m in tags.get("models", [])}
        return self.model in names or any(
            n and n.split(":")[0] == self.model.split(":")[0] for n in names
        )

    def complete(self, system: str, messages: list[dict[str, str]], *,
                 timeout: float | None = None) -> InferenceResult:
        started = time.time()
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0.7, "num_predict": 200},
            "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
        }
        try:
            raw = self._post("/api/chat", payload, timeout or self.timeout)
        except Exception as exc:
            return InferenceResult(text="", backend=self.name, model=self.model,
                                   latency_ms=int((time.time() - started) * 1000),
                                   error=f"{type(exc).__name__}: {exc}")
        msg = (raw.get("message") or {}).get("content", "")
        return InferenceResult(
            text=msg.strip(),
            backend=self.name,
            model=self.model,
            latency_ms=int((time.time() - started) * 1000),
            usage={k: raw.get(k) for k in ("eval_count", "prompt_eval_count", "total_duration")
                   if raw.get(k) is not None},
            raw={"done_reason": raw.get("done_reason")},
        )


def get_backend(name: str | None = None) -> Inference:
    """按 env 或显式名字选后端；ollama 不可用就退回 rule，不抛错。"""
    choice = (name or os.environ.get("NEWBORN_INFER_BACKEND") or "ollama").lower()
    if choice == "rule":
        return RuleBackend()
    ollama = OllamaBackend()
    if ollama.available():
        return ollama
    return RuleBackend()


def describe(backend: Inference) -> dict[str, Any]:
    info: dict[str, Any] = {"name": backend.name, "test_only": backend.name in ("ollama", "rule")}
    if isinstance(backend, OllamaBackend):
        info.update({"base": backend.base, "model": backend.model})
    return info
