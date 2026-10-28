"""认知内核 M1，已按审查意见改造。

两阶段处理（README 4.3 接收可恢复）：
    阶段一：把事件以 pending 写库并提交。这一步之后进程崩了，事件也不会丢。
    阶段二：读上下文 → 推理 → 在**新事务**里原子提交 记忆 + 状态 + Outbox + processed。
            推理失败只把事件标成 failed，原始事件仍在库里，可以重试或人工看。

其它改动：
- 检索一律带 AccessContext（实例/会话/主体/用途），不再有「看全部」的默认。
- 记忆先决条件：注意力阈值 + 同内容去重，不是所有非空消息都成长期记忆。
- Outbox 用条件更新领取，带认领者与租约，禁止两个 worker 拿同一条。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any, Callable

from . import affect as affect_mod  # noqa: F401  (保留给后续扩展)
from . import memory as memory_mod
from . import state as state_mod
from .db import Tx, dumps, loads
from .events import EventEnvelope, validate as validate_event
from .memory import AccessContext, MemoryObject

# 注意力低于这个值就不进长期记忆；介于两者之间只留 candidate
MEMORY_ACTIVATION = 0.55
MEMORY_CANDIDATE_FLOOR = 0.30
MIN_TEXT_LEN = 2

KEYWORDS = {
    "好奇": {"valence": 0.05, "curiosity": 0.12},
    "谢谢": {"valence": 0.15, "social": 0.10},
    "滚": {"valence": -0.20, "stress": 0.15},
    "累": {"stress": 0.12, "arousal": -0.05},
    "?": {"curiosity": 0.06},
}

Reasoner = Callable[[EventEnvelope, dict[str, float], list[dict]], str]


def default_reasoner(env: EventEnvelope, affect: dict[str, float], memories: list[dict]) -> str:
    text = str(env.payload.get("text", "")).strip()
    if not text:
        return ""
    mood = "稳" if affect["valence"] >= 0 else "沉"
    return f"[M1 规则回复·心情{mood}] 收到：{text[:40]}"


def attention_score(env: EventEnvelope, affect: dict[str, float]) -> float:
    text = str(env.payload.get("text", ""))
    freshness = 0.4
    social = 0.3 if env.actor_id else 0.1
    curiosity = affect.get("curiosity", 0.4) * 0.3
    urgency = 0.2 if ("?" in text or "急" in text) else 0.0
    return round(min(1.0, freshness + social + curiosity + urgency), 4)


def access_context(env: EventEnvelope, purpose: str = "chat") -> AccessContext:
    return AccessContext(
        instance_id=env.instance_id,
        conversation_id=env.conversation_id,
        subject_id=env.actor_id,
        purpose=purpose,
    )


def _affect_deltas(text: str) -> dict[str, float]:
    deltas: dict[str, float] = {}
    for kw, eff in KEYWORDS.items():
        if kw in text:
            for dim, val in eff.items():
                deltas[dim] = deltas.get(dim, 0.0) + val
    return deltas


def _persist_pending(conn, env: EventEnvelope) -> str:
    """阶段一：只写事件本身。重复投递在这里被识别。"""
    row = env.to_row()
    try:
        with Tx(conn):
            conn.execute(
                f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
        return "pending"
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT event_id, process_state FROM events"
            " WHERE instance_id = ? AND source = ? AND idempotency_key = ?",
            (env.instance_id, env.source, env.idempotency_key),
        ).fetchone()
        if existing and existing["event_id"] == env.event_id \
                and existing["process_state"] in ("pending", "failed"):
            return "resume"
        return "duplicate"


def process_event(conn, env: EventEnvelope, reasoner: Reasoner | None = None) -> dict[str, Any]:
    problems = validate_event(env)
    if problems:
        return {"status": "rejected", "problems": problems, "event_id": env.event_id}

    phase = _persist_pending(conn, env)
    if phase == "duplicate":
        row = conn.execute(
            "SELECT event_id, process_state FROM events"
            " WHERE instance_id = ? AND source = ? AND idempotency_key = ?",
            (env.instance_id, env.source, env.idempotency_key),
        ).fetchone()
        return {"status": "duplicate", "event_id": row["event_id"],
                "process_state": row["process_state"]}
    return _handle(conn, env, reasoner or default_reasoner)


def _handle(conn, env: EventEnvelope, reasoner: Reasoner) -> dict[str, Any]:
    """阶段二：读上下文 → 推理 → 一个事务提交结果。推理器报错不影响事件本身。"""
    ctx = access_context(env)
    text = str(env.payload.get("text", ""))
    try:
        aff = state_mod.affect_of(conn, env.instance_id)
        aff.inject(_affect_deltas(text))
        affect_values = aff.value()
        score = attention_score(env, affect_values)
        history = memory_mod.retrieve(conn, ctx, limit=5)
        reply = reasoner(env, affect_values, history)
    except Exception as exc:  # 推理失败：事件保留，标记 failed，可重试
        with Tx(conn):
            conn.execute(
                "UPDATE events SET process_state = 'failed' WHERE event_id = ?", (env.event_id,)
            )
        return {"status": "error", "event_id": env.event_id, "error": str(exc)}

    memory_ids: list[str] = []
    duplicate_of = None
    with Tx(conn):
        if len(text.strip()) >= MIN_TEXT_LEN and score >= MEMORY_CANDIDATE_FLOOR:
            duplicate_of = memory_mod.find_active_duplicate(conn, env.instance_id, text.strip())
            if duplicate_of is None:
                cand = MemoryObject(
                    content=text.strip(),
                    memory_type="episodic",
                    instance_id=env.instance_id,
                    conversation_id=env.conversation_id,
                    subject_ids=[env.actor_id] if env.actor_id else [],
                    source_event_ids=[env.event_id],
                    provenance="user_statement" if env.trust_level == "user" else "external",
                    epistemic_status="reported",
                    importance=0.4,
                    salience=min(1.0, score),
                    privacy_scope=env.privacy_scope,
                    lifecycle_state="active" if score >= MEMORY_ACTIVATION else "candidate",
                    occurred_at=env.occurred_at,
                )
                memory_ids.append(memory_mod.insert(conn, cand))

        state_mod.save_affect(conn, env.instance_id, aff, env.event_id)

        cog = state_mod.get(conn, env.instance_id, "cognitive")["data"] or {}
        cog["last_event"] = env.event_id
        cog["last_attention"] = score
        cog["working_set"] = (cog.get("working_set") or [])[-4:] + [env.event_id]
        state_mod.put(conn, env.instance_id, "cognitive", cog, source_event_id=env.event_id)

        action_id = None
        if reply:
            action_id = "act_" + uuid.uuid4().hex[:12]
            ts = time.time()
            conn.execute(
                "INSERT INTO outbox (action_id, event_id, kind, payload, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (action_id, env.event_id, "reply",
                 dumps({"conversation_id": env.conversation_id, "text": reply}), ts, ts),
            )

        conn.execute(
            "UPDATE events SET process_state = 'processed' WHERE event_id = ?", (env.event_id,)
        )

    return {
        "status": "processed",
        "event_id": env.event_id,
        "attention": score,
        "memories": memory_ids,
        "duplicate_memory_of": duplicate_of,
        "affect": affect_values,
        "action_id": action_id,
        "reply": reply,
    }


def claim_events(conn, worker_id: str, limit: int = 100, lease_seconds: float = 60.0) -> list[dict]:
    """原子领取待处理事件：pending/failed 且没有有效租约的才拿得到。

    没有这一步，两个恢复 worker 会对同一个 pending 事件各推理一次，
    各自生成一条 Outbox 回复——审计 #9 的复现就是从这里来的。
    """
    now = time.time()
    claimed: list[dict] = []
    with Tx(conn):
        rows = conn.execute(
            "SELECT event_id FROM events"
            " WHERE process_state IN ('pending','failed')"
            " AND (lease_until IS NULL OR lease_until < ?)"
            " ORDER BY received_at LIMIT ?", (now, limit)).fetchall()
        for r in rows:
            cur = conn.execute(
                "UPDATE events SET claimed_by = ?, lease_until = ?"
                " WHERE event_id = ? AND process_state IN ('pending','failed')"
                " AND (lease_until IS NULL OR lease_until < ?)",
                (worker_id, now + lease_seconds, r["event_id"], now))
            if cur.rowcount == 1:
                claimed.append({"event_id": r["event_id"], "worker": worker_id,
                                "lease_until": now + lease_seconds})
    return claimed


def process_pending(conn, limit: int = 100, reasoner: Reasoner | None = None,
                    worker_id: str = "recovery", lease_seconds: float = 60.0) -> list[dict]:
    """启动恢复：先领取，再逐个处理；处理失败时清租约，留给下一轮重试。"""
    claimed = claim_events(conn, worker_id, limit=limit, lease_seconds=lease_seconds)
    results = []
    for c in claimed:
        r = conn.execute("SELECT * FROM events WHERE event_id = ?", (c["event_id"],)).fetchone()
        if not r:
            continue
        env = EventEnvelope(
            event_type=r["event_type"],
            source=r["source"],
            payload=loads(r["payload"]),
            instance_id=r["instance_id"],
            subject_id=r["subject_id"],
            actor_id=r["actor_id"],
            conversation_id=r["conversation_id"],
            occurred_at=r["occurred_at"],
            privacy_scope=r["privacy_scope"],
            trust_level=r["trust_level"],
            source_event_id=r["source_event_id"],
            trace_id=r["trace_id"],
            causation_id=r["causation_id"],
            idempotency_key=r["idempotency_key"],
            event_id=r["event_id"],
        )
        env.received_at = r["received_at"]
        try:
            results.append(process_event(conn, env, reasoner))
        finally:
            conn.execute("UPDATE events SET claimed_by = NULL, lease_until = NULL"
                         " WHERE event_id = ?", (env.event_id,))
    return results


# ------------------------------------------------------------------ Outbox

def claim_actions(conn, worker_id: str, limit: int = 10, lease_seconds: float = 60.0) -> list[dict]:
    """原子领取：条件更新，前一个 worker 的租约没过期就抢不到。"""
    now = time.time()
    claimed: list[dict] = []
    with Tx(conn):
        rows = conn.execute(
            "SELECT action_id FROM outbox"
            " WHERE status = 'pending' OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?)"
            " ORDER BY created_at LIMIT ?",
            (now, limit),
        ).fetchall()
        for r in rows:
            cur = conn.execute(
                "UPDATE outbox SET status = 'running', claimed_by = ?, lease_until = ?,"
                " attempts = attempts + 1, updated_at = ?"
                " WHERE action_id = ? AND (status = 'pending'"
                "   OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?))",
                (worker_id, now + lease_seconds, now, r["action_id"], now),
            )
            if cur.rowcount == 1:
                claimed.append({"action_id": r["action_id"], "worker": worker_id,
                                "lease_until": now + lease_seconds})
    return claimed


LEGAL_TRANSITIONS = {
    "running": {"succeeded", "failed", "unknown", "cancelled"},
    "unknown": {"succeeded", "failed", "cancelled"},
    "pending": {"cancelled"},
}


def finish_action(conn, action_id: str, status: str, error: str | None = None) -> None:
    row = conn.execute("SELECT status FROM outbox WHERE action_id = ?", (action_id,)).fetchone()
    if not row:
        raise ValueError(f"行动不存在: {action_id}")
    allowed = LEGAL_TRANSITIONS.get(row["status"], set())
    if status not in allowed:
        raise ValueError(f"非法状态转换: {row['status']} → {status}")
    conn.execute(
        "UPDATE outbox SET status = ?, last_error = ?, lease_until = NULL, updated_at = ?"
        " WHERE action_id = ?",
        (status, error, time.time(), action_id),
    )


def stats(conn, instance_id: str = "default") -> dict[str, Any]:
    q = lambda sql, args=(): conn.execute(sql, args).fetchone()[0]  # noqa: E731
    return {
        "events": q("SELECT COUNT(*) FROM events WHERE instance_id = ?", (instance_id,)),
        "pending_events": q(
            "SELECT COUNT(*) FROM events WHERE instance_id = ? AND process_state IN ('pending','failed')",
            (instance_id,)),
        "memories": q(
            "SELECT COUNT(*) FROM memories WHERE instance_id = ? AND lifecycle_state = 'active'",
            (instance_id,)),
        "candidates": q(
            "SELECT COUNT(*) FROM memories WHERE instance_id = ? AND lifecycle_state = 'candidate'",
            (instance_id,)),
        "outbox_pending": q("SELECT COUNT(*) FROM outbox WHERE status IN ('pending','running')"),
        "affect": state_mod.affect_of(conn, instance_id).value(),
    }
