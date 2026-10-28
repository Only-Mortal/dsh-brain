"""统一事件（README 第 4.1 节）。

约束：
- 任何外部来源都必须先变成 EventEnvelope 才能进入内核。
- 去重靠 idempotency_key，缺上游 ID 时由适配器给替代策略（这里要求显式传入或自动生成并标注）。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from .db import dumps

SCHEMA_VERSION = 1


@dataclass
class EventEnvelope:
    event_type: str
    source: str
    payload: dict[str, Any]
    instance_id: str = "default"
    subject_id: str | None = None
    actor_id: str | None = None
    conversation_id: str | None = None
    occurred_at: float | None = None
    privacy_scope: str = "private"
    trust_level: str = "user"
    source_event_id: str | None = None
    trace_id: str | None = None
    causation_id: str | None = None
    idempotency_key: str | None = None
    event_id: str = field(default_factory=lambda: "evt_" + uuid.uuid4().hex)

    def __post_init__(self) -> None:
        now = time.time()
        if self.occurred_at is None:
            self.occurred_at = now
        self.received_at = now
        if self.idempotency_key is None:
            raw = f"{self.instance_id}|{self.source}|{self.source_event_id or self.event_id}|{self.event_type}"
            self.idempotency_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        if self.trace_id is None:
            self.trace_id = "tr_" + uuid.uuid4().hex[:12]

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "schema_version": SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "subject_id": self.subject_id,
            "event_type": self.event_type,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "actor_id": self.actor_id,
            "conversation_id": self.conversation_id,
            "occurred_at": self.occurred_at,
            "received_at": self.received_at,
            "payload": dumps(self.payload),
            "privacy_scope": self.privacy_scope,
            "trust_level": self.trust_level,
            "trace_id": self.trace_id,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
        }


def validate(env: EventEnvelope) -> list[str]:
    """结构校验：不合格的事件拒收，交由调用方记录诊断。"""
    problems: list[str] = []
    if not env.event_type:
        problems.append("event_type 为空")
    if not env.source:
        problems.append("source 为空")
    if not env.instance_id:
        problems.append("instance_id 为空")
    if env.privacy_scope not in ("private", "conversation", "instance", "public"):
        problems.append(f"privacy_scope 非法: {env.privacy_scope}")
    if env.trust_level not in ("user", "trusted", "external", "model"):
        problems.append(f"trust_level 非法: {env.trust_level}")
    size = len(dumps(env.payload))
    if size > 200_000:
        problems.append(f"payload 过大: {size} 字节")
    return problems
