"""自传式记忆（README 第 6 节）M1 子集，已按审查意见收紧。

变化：
- 写入 API 自己校验（insert/revise 内部调用 validate，不再依赖调用者自觉）。
- 检索必须传 AccessContext，没有默认「看全部」的捷径。
- 记忆保存 conversation_id，conversation 级隐私可以按会话过滤。
- revise 要求旧记忆存在，并保存修订原因。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .db import dumps

EPISTEMIC = ("reported", "inferred", "verified", "disputed")
LIFECYCLE = ("candidate", "active", "superseded", "archived", "pending_delete", "deleted")
PRIVACY = ("private", "conversation", "instance", "public")


class MemoryError(Exception):
    """写入被拒或访问越界。"""


@dataclass(frozen=True)
class AccessContext:
    """谁、在哪个会话、为了什么用途来读记忆。"""

    instance_id: str
    conversation_id: str | None = None
    subject_id: str | None = None
    purpose: str = "chat"


@dataclass
class MemoryObject:
    content: str
    memory_type: str = "episodic"
    instance_id: str = "default"
    conversation_id: str | None = None
    subject_ids: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)
    provenance: str = "user_statement"
    epistemic_status: str = "reported"
    confidence: float = 0.6
    importance: float = 0.5
    salience: float = 0.5
    privacy_scope: str = "private"
    lifecycle_state: str = "active"
    occurred_at: float | None = None
    memory_id: str = field(default_factory=lambda: "mem_" + uuid.uuid4().hex[:12])

    def row(self) -> dict[str, Any]:
        now = time.time()
        occurred = now if self.occurred_at is None else self.occurred_at
        return {
            "memory_id": self.memory_id,
            "instance_id": self.instance_id,
            "conversation_id": self.conversation_id,
            "subject_ids": dumps(self.subject_ids),
            "memory_type": self.memory_type,
            "content": self.content,
            "source_event_ids": dumps(self.source_event_ids),
            "provenance": self.provenance,
            "epistemic_status": self.epistemic_status,
            "confidence": self.confidence,
            "importance": self.importance,
            "salience": self.salience,
            "occurred_at": occurred,
            "created_at": now,
            "updated_at": now,
            "lifecycle_state": self.lifecycle_state,
            "privacy_scope": self.privacy_scope,
        }


def validate(mem: MemoryObject) -> list[str]:
    problems: list[str] = []
    if not mem.content.strip():
        problems.append("content 为空")
    if mem.epistemic_status not in EPISTEMIC:
        problems.append(f"epistemic_status 非法: {mem.epistemic_status}")
    if mem.lifecycle_state not in LIFECYCLE:
        problems.append(f"lifecycle_state 非法: {mem.lifecycle_state}")
    if mem.privacy_scope not in PRIVACY:
        problems.append(f"privacy_scope 非法: {mem.privacy_scope}")
    if not 0.0 <= mem.confidence <= 1.0:
        problems.append("confidence 越界")
    if not 0.0 <= mem.importance <= 1.0:
        problems.append("importance 越界")
    # 硬约束：没有来源事件的「经历」不许入库（README 6.1 Episodic Store）
    if mem.memory_type == "episodic" and not mem.source_event_ids:
        problems.append("episodic 记忆缺少 source_event_ids")
    # 会话级隐私必须知道是哪个会话，否则过滤无从谈起
    if mem.privacy_scope == "conversation" and not mem.conversation_id:
        problems.append("conversation 级隐私缺少 conversation_id")
    if mem.privacy_scope == "private" and not mem.subject_ids:
        problems.append("private 级隐私缺少 subject_ids")
    return problems


def insert(conn, mem: MemoryObject) -> str:
    problems = validate(mem)
    if problems:
        raise MemoryError("；".join(problems))
    row = mem.row()
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO memories ({cols}) VALUES ({marks})", tuple(row.values()))
    return mem.memory_id


def _privacy_clause(ctx: AccessContext) -> tuple[str, list[Any]]:
    """把访问上下文翻译成 SQL 条件。默认拒绝，只有明确匹配才放行。

    主体匹配用 json_each 做精确相等，不能用 LIKE：'u1' 会误伤 'u10'，
    更不能用 '%' 这种通配输入。
    """
    clauses = ["privacy_scope = 'public'", "privacy_scope = 'instance'"]
    args: list[Any] = []
    if ctx.conversation_id:
        clauses.append("(privacy_scope = 'conversation' AND conversation_id = ?)")
        args.append(ctx.conversation_id)
    if ctx.subject_id:
        clauses.append(
            "(privacy_scope = 'private' AND EXISTS ("
            " SELECT 1 FROM json_each(memories.subject_ids) AS je WHERE je.value = ?))"
        )
        args.append(ctx.subject_id)
    return "(" + " OR ".join(clauses) + ")", args


def retrieve(conn, ctx: AccessContext, query: str = "", limit: int = 10) -> list[dict]:
    if not isinstance(ctx, AccessContext):
        raise TypeError("retrieve 必须传 AccessContext")
    privacy_sql, privacy_args = _privacy_clause(ctx)
    sql = [
        "SELECT * FROM memories WHERE instance_id = ? AND lifecycle_state = 'active'",
        f"AND {privacy_sql}",
    ]
    args: list[Any] = [ctx.instance_id, *privacy_args]
    if query:
        sql.append("AND content LIKE ?")
        args.append(f"%{query}%")
    sql.append("ORDER BY importance DESC, occurred_at DESC LIMIT ?")
    args.append(limit)
    rows = conn.execute(" ".join(sql), args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["subject_ids"] = json.loads(d["subject_ids"])
        d["source_event_ids"] = json.loads(d["source_event_ids"])
        out.append(d)
    return out


def find_active_duplicate(conn, instance_id: str, content: str) -> str | None:
    row = conn.execute(
        "SELECT memory_id FROM memories WHERE instance_id = ? AND lifecycle_state = 'active'"
        " AND content = ? LIMIT 1",
        (instance_id, content),
    ).fetchone()
    return row["memory_id"] if row else None


def revise(conn, old_id: str, new_mem: MemoryObject, reason: str = "") -> str:
    old = conn.execute(
        "SELECT memory_id, instance_id FROM memories WHERE memory_id = ?", (old_id,)
    ).fetchone()
    if not old:
        raise MemoryError(f"要修订的记忆不存在: {old_id}")
    if old["instance_id"] != new_mem.instance_id:
        raise MemoryError("不能跨实例修订记忆")
    new_id = insert(conn, new_mem)
    ts = time.time()
    conn.execute(
        "UPDATE memories SET lifecycle_state = 'superseded', updated_at = ? WHERE memory_id = ?",
        (ts, old_id),
    )
    conn.execute(
        "UPDATE memories SET supersedes_id = ?, version = version + 1, updated_at = ? WHERE memory_id = ?",
        (old_id, ts, new_id),
    )
    conn.execute(
        "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (f"revision_reason:{new_id}", reason),
    )
    return new_id


def touch(conn, memory_id: str) -> None:
    conn.execute(
        "UPDATE memories SET access_count = access_count + 1, last_accessed_at = ? WHERE memory_id = ?",
        (time.time(), memory_id),
    )
