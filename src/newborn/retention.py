"""删除、隐私代次与派生一致性（NEXT-STEPS 阶段 B）。

第一版范围（与计划一致，宁窄不宽）：
- 支持三种选择器：单个对象（memory_id / event_id）、整个会话、整个实例。
- 顺序固定：**先写撤销账本 → 再改主库**。账本独立于数据库快照，恢复旧备份后要能重放。
- 删除先逻辑撤权（`privacy_epoch` +1、`lifecycle_state` 置 deleted），派生（语义、簇、Trait 证据）
  一并处理；物理清理（VACUUM、WAL、介质）不在这一版承诺里。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .db import Tx, dumps, loads

LEDGER_SUFFIX = ".ledger.jsonl"


def ledger_path(db_path: str | Path) -> Path:
    p = Path(db_path)
    return p.with_name(p.name + LEDGER_SUFFIX)


def _append_ledger(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        import os
        os.fsync(fh.fileno())


def privacy_epoch(conn, instance_id: str) -> int:
    row = conn.execute("SELECT privacy_epoch FROM instance_meta WHERE instance_id = ?",
                       (instance_id,)).fetchone()
    return int(row["privacy_epoch"]) if row else 0


def bump_privacy_epoch(conn, instance_id: str) -> int:
    now = time.time()
    conn.execute(
        "INSERT INTO instance_meta (instance_id, privacy_epoch, updated_at) VALUES (?, 1, ?)"
        " ON CONFLICT(instance_id) DO UPDATE SET privacy_epoch = privacy_epoch + 1,"
        " updated_at = excluded.updated_at", (instance_id, now))
    return privacy_epoch(conn, instance_id)


def _scope_where(scope: str, target: str) -> tuple[str, list[Any]]:
    if scope == "instance":
        return "instance_id = ?", [target]
    if scope == "conversation":
        return "conversation_id = ? AND instance_id = ?", [target.split("|", 1)[0],
                                                          target.split("|")[-1]]
    return "memory_id = ?", [target]


def preview_deletion(conn, instance_id: str, scope: str, target: str) -> dict[str, Any]:
    """只统计，不动数据。"""
    if scope == "object":
        mem = conn.execute("SELECT COUNT(*) FROM memories WHERE instance_id = ?"
                           " AND memory_id = ? AND lifecycle_state <> 'deleted'",
                           (instance_id, target)).fetchone()[0]
        ev = conn.execute("SELECT COUNT(*) FROM events WHERE instance_id = ?"
                          " AND event_id = ? AND process_state <> 'rejected'",
                          (instance_id, target)).fetchone()[0]
        sem = conn.execute("SELECT COUNT(*) FROM memories WHERE instance_id = ?"
                           " AND memory_type='semantic' AND source_event_ids LIKE ?",
                           (instance_id, f"%{target}%")).fetchone()[0]
        return {"memories": mem, "events": ev, "derived_semantic": sem}
    if scope == "conversation":
        inst, conv = target.split("|", 1)
        mem = conn.execute("SELECT COUNT(*) FROM memories WHERE instance_id = ?"
                           " AND conversation_id = ? AND lifecycle_state <> 'deleted'",
                           (inst, conv)).fetchone()[0]
        ev = conn.execute("SELECT COUNT(*) FROM events WHERE instance_id = ?"
                          " AND conversation_id = ? AND process_state <> 'rejected'",
                          (inst, conv)).fetchone()[0]
        return {"memories": mem, "events": ev, "conversation": conv, "instance": inst}
    mem = conn.execute("SELECT COUNT(*) FROM memories WHERE instance_id = ?"
                       " AND lifecycle_state <> 'deleted'", (instance_id,)).fetchone()[0]
    ev = conn.execute("SELECT COUNT(*) FROM events WHERE instance_id = ?"
                      " AND process_state <> 'rejected'", (instance_id,)).fetchone()[0]
    return {"memories": mem, "events": ev}


def request_deletion(conn, db_path: str | Path, instance_id: str, scope: str,
                     target: str) -> dict[str, Any]:
    """登记删除：先落账本，再改主库，最后清派生。失败保留在 cleaning 以便重试。"""
    request_id = "dr_" + uuid.uuid4().hex[:12]
    preview = preview_deletion(conn, instance_id, scope, target)
    now = time.time()
    record = {"request_id": request_id, "instance_id": instance_id, "scope": scope,
              "target": target, "created_at": now}
    _append_ledger(ledger_path(db_path), record)      # ① 账本先持久化

    with Tx(conn):
        conn.execute(
            "INSERT INTO deletion_requests (request_id, instance_id, scope, target, status,"
            " preview, detail, created_at, updated_at)"
            " VALUES (?,?,?,?,'requested',?,?,?,?)",
            (request_id, instance_id, scope, target, dumps(preview), "{}", now, now))
        bump_privacy_epoch(conn, instance_id)          # ② 立即撤权
        conn.execute("UPDATE deletion_requests SET status='revoked', updated_at=? "
                     "WHERE request_id=?", (now, request_id))

    try:
        removed = _clean(conn, instance_id, scope, target)
    except Exception as exc:                           # 清理失败：保留位置以便重试
        with Tx(conn):
            conn.execute("UPDATE deletion_requests SET status='failed', detail=?,"
                         " updated_at=? WHERE request_id=?",
                         (dumps({"error": str(exc)}), time.time(), request_id))
        raise
    with Tx(conn):
        conn.execute("UPDATE deletion_requests SET status='cleaned', detail=?,"
                     " updated_at=? WHERE request_id=?",
                     (dumps(removed), time.time(), request_id))
    return {"request_id": request_id, "status": "cleaned", "preview": preview,
            "removed": removed}


def _clean(conn, instance_id: str, scope: str, target: str) -> dict[str, Any]:
    """逻辑清理 + 派生处理。物理清理不在本版承诺内。"""
    now = time.time()
    removed = {"memories": 0, "events": 0, "semantic": 0, "clusters": 0}
    with Tx(conn):
        if scope == "object":
            cur = conn.execute(
                "UPDATE memories SET lifecycle_state='deleted', updated_at=?"
                " WHERE instance_id=? AND memory_id=? AND lifecycle_state <> 'deleted'",
                (now, instance_id, target))
            removed["memories"] += cur.rowcount
            cur = conn.execute(
                "UPDATE events SET process_state='rejected'"
                " WHERE instance_id=? AND event_id=?", (instance_id, target))
            removed["events"] += cur.rowcount
        elif scope == "conversation":
            inst, conv = target.split("|", 1)
            cur = conn.execute(
                "UPDATE memories SET lifecycle_state='deleted', updated_at=?"
                " WHERE instance_id=? AND conversation_id=? AND lifecycle_state <> 'deleted'",
                (now, inst, conv))
            removed["memories"] += cur.rowcount
            cur = conn.execute(
                "UPDATE events SET process_state='rejected'"
                " WHERE instance_id=? AND conversation_id=?", (inst, conv))
            removed["events"] += cur.rowcount
        else:
            cur = conn.execute(
                "UPDATE memories SET lifecycle_state='deleted', updated_at=?"
                " WHERE instance_id=? AND lifecycle_state <> 'deleted'", (now, instance_id))
            removed["memories"] += cur.rowcount
            cur = conn.execute("UPDATE events SET process_state='rejected' WHERE instance_id=?",
                               (instance_id,))
            removed["events"] += cur.rowcount

        # 派生：语义记忆只要来源被清掉，就退出默认检索；簇归档
        sem = conn.execute(
            "SELECT memory_id, source_event_ids FROM memories"
            " WHERE instance_id=? AND memory_type='semantic' AND lifecycle_state='active'",
            (instance_id,)).fetchall()
        for row in sem:
            ids = loads(row["source_event_ids"]) or []
            if not ids:
                continue
            alive = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_id IN (%s) AND process_state <> 'rejected'"
                % ",".join("?" for _ in ids), ids).fetchone()[0]
            if alive == 0:
                conn.execute("UPDATE memories SET lifecycle_state='superseded', updated_at=?"
                             " WHERE memory_id=?", (now, row["memory_id"]))
                removed["semantic"] += 1
        cur = conn.execute(
            "UPDATE clusters SET status='archived', updated_at=?"
            " WHERE instance_id=? AND status='active'", (now, instance_id))
        removed["clusters"] += cur.rowcount
    return removed


def replay_ledger(conn, ledger: str | Path) -> dict[str, Any]:
    """恢复旧备份后重新应用撤销记录。

    账本**必须由调用方显式给出**：它属于最初的那套数据，不属于被恢复的那个文件；
    从 db 路径推出来会在"恢复到新文件名"时找错位置。
    """
    path = Path(ledger)
    if not path.exists():
        return {"replayed": 0, "ledger": str(path)}
    replayed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        _clean(conn, rec["instance_id"], rec["scope"], rec["target"])
        with Tx(conn):
            bump_privacy_epoch(conn, rec["instance_id"])
        replayed += 1
    return {"replayed": replayed, "ledger": str(path)}
