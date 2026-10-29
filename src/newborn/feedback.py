"""显式反馈与 Trait 的行为证据（NEXT-STEPS 阶段 E 的第一版）。

只做三件事，避免把"数据覆盖度"继续冒充人格：
1. 记录三条明确反馈：correct（纠正内容）、reject（否定）、confirm（确认）。
2. reject 立刻把对应记忆标为 disputed、置信度压到 0.1，并退出默认检索的优先级。
3. Trait 只能由**反馈证据**提案（证据指纹 = 反馈集合的哈希），
   普通巩固覆盖度不再直接改 Trait——那条路径保留为指标，不写状态。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from .db import Tx, dumps

KINDS = ("correct", "reject", "confirm")


def add_feedback(conn, instance_id: str, kind: str, *, memory_id: str | None = None,
                 note: str = "") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"未知反馈类型：{kind}")
    fid = "fb_" + uuid.uuid4().hex[:12]
    now = time.time()
    with Tx(conn):
        conn.execute(
            "INSERT INTO feedback (feedback_id, instance_id, memory_id, kind, note, created_at)"
            " VALUES (?,?,?,?,?,?)", (fid, instance_id, memory_id, kind, note[:500], now))
        if kind == "reject" and memory_id:
            conn.execute(
                "UPDATE memories SET epistemic_status='disputed', confidence=0.1, updated_at=?"
                " WHERE memory_id=? AND instance_id=?", (now, memory_id, instance_id))
        if kind == "confirm" and memory_id:
            conn.execute("UPDATE memories SET confidence=MIN(1.0, confidence + 0.1),"
                         " updated_at=? WHERE memory_id=? AND instance_id=?",
                         (now, memory_id, instance_id))
    return {"feedback_id": fid, "kind": kind, "memory_id": memory_id}


def evidence_fingerprint(conn, instance_id: str) -> str | None:
    rows = conn.execute(
        "SELECT feedback_id, kind, memory_id FROM feedback WHERE instance_id = ?"
        " ORDER BY feedback_id", (instance_id,)).fetchall()
    if not rows:
        return None
    raw = "|".join(f"{r['feedback_id']}:{r['kind']}:{r['memory_id']}" for r in rows)
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()


def trait_deltas_from_feedback(conn, instance_id: str) -> dict[str, float]:
    """从反馈里提极小步长；没有反馈就不动。"""
    rows = conn.execute("SELECT kind, COUNT(*) AS n FROM feedback WHERE instance_id = ?"
                        " GROUP BY kind", (instance_id,)).fetchall()
    counts = {r["kind"]: r["n"] for r in rows}
    total = sum(counts.values())
    if total == 0:
        return {}
    good = counts.get("confirm", 0) + counts.get("correct", 0)
    bad = counts.get("reject", 0)
    balance = (good - bad) / max(1, total)          # -1..1
    return {
        "sociability": round(0.02 * balance, 4),
        "stability": round(0.02 * balance, 4),
    }
