"""并发高压 + 崩溃恢复压测（一次性脚本，不是单元测试）。

用真实 SQLite 文件跑三件事：
  A. 8 个线程并发投递 240 个事件，其中一部分推理故意失败；
  B. 用「恢复」流程把 pending/failed 全部处理完；
  C. 6 个线程并发领取 Outbox，验证没有一条行动被领两次。

跑法：
    python tests/stress_m1.py            # 跑完自动删库
    python tests/stress_m1.py --keep     # 保留库方便排查

成功时打印一份 JSON 报告并以 0 退出；任何不变量被破坏以 1 退出。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from newborn import db, kernel  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402

WORKERS = 8
PER_WORKER = 30
FAIL_RATIO = 4          # 每 4 条里 1 条推理失败
CLAIM_THREADS = 6
CLAIM_BATCH = 5


def good_reasoner(env, affect, memories):
    return f"ok:{env.payload.get('text', '')[:16]}"


def bad_reasoner(env, affect, memories):
    raise RuntimeError("压测：模拟推理后端超时")


def worker(w: int, db_path: str, results: list, errors: list) -> None:
    conn = db.connect(db_path)
    try:
        for i in range(PER_WORKER):
            n = w * PER_WORKER + i
            use_bad = (n % FAIL_RATIO == 0)
            env = EventEnvelope(
                event_type="message.received",
                source="stress",
                payload={"text": f"压测消息 {w}-{i} 内容要够长才会进记忆"},
                instance_id="stress",
                actor_id=f"u{w}",
                conversation_id=f"c{w}",
                privacy_scope="conversation",
                idempotency_key=f"k-{w}-{i}",
            )
            try:
                res = kernel.process_event(conn, env, bad_reasoner if use_bad else good_reasoner)
                results.append(res["status"])
            except Exception as exc:  # 任何异常都记下来，不吞
                errors.append(f"{w}-{i}: {type(exc).__name__}: {exc}")
    finally:
        conn.close()


def claimer(name: str, db_path: str, claimed: list, lock: threading.Lock) -> None:
    conn = db.connect(db_path)
    try:
        for _ in range(3):
            got = kernel.claim_actions(conn, name, limit=CLAIM_BATCH, lease_seconds=5)
            with lock:
                claimed.extend([(c["action_id"], name) for c in got])
            time.sleep(0.01)
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    db_path = Path(args.db or (ROOT / "memory" / "stress.sqlite3"))
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    conn = db.connect(db_path)
    report: dict = {"db": str(db_path)}
    started = time.time()

    # ---------------- A. 并发投递
    results: list = []
    errors: list = []
    threads = [threading.Thread(target=worker, args=(w, str(db_path), results, errors))
               for w in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed_a = time.time() - started

    total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    by_state = {r["process_state"]: r["n"] for r in conn.execute(
        "SELECT process_state, COUNT(*) n FROM events GROUP BY process_state")}
    report["phase_a"] = {
        "submitted": WORKERS * PER_WORKER,
        "events_in_db": total_events,
        "by_state": by_state,
        "statuses": {s: results.count(s) for s in set(results)},
        "errors": errors[:5],
        "seconds": round(elapsed_a, 2),
    }

    # ---------------- B. 恢复
    t0 = time.time()
    rounds = 0
    while rounds < 20:
        rounds += 1
        res = kernel.process_pending(conn, limit=500, reasoner=good_reasoner)
        if not res:
            break
    elapsed_b = time.time() - t0
    left = conn.execute(
        "SELECT COUNT(*) FROM events WHERE process_state IN ('pending','failed')").fetchone()[0]
    processed = conn.execute(
        "SELECT COUNT(*) FROM events WHERE process_state = 'processed'").fetchone()[0]
    report["phase_b"] = {"rounds": rounds, "left_pending_or_failed": left,
                         "processed": processed, "seconds": round(elapsed_b, 2)}

    # ---------------- C. Outbox 并发领取
    claimed: list = []
    lock = threading.Lock()
    threads = [threading.Thread(target=claimer, args=(f"w{i}", str(db_path), claimed, lock))
               for i in range(CLAIM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [a for a, _ in claimed]
    dup = len(ids) - len(set(ids))
    report["phase_c"] = {"claims": len(ids), "unique": len(set(ids)), "duplicates": dup}

    # ---------------- 不变量
    mem_dups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT content, COUNT(*) c FROM memories"
        " WHERE lifecycle_state='active' GROUP BY instance_id, content HAVING c > 1)"
    ).fetchone()[0]
    orphan_mem = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE source_event_ids = '[]' AND memory_type='episodic'"
    ).fetchone()[0]
    bad_conv = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE privacy_scope='conversation' AND conversation_id IS NULL"
    ).fetchone()[0]

    checks = {
        "事件不丢": total_events == WORKERS * PER_WORKER,
        "恢复后无遗留": left == 0,
        "全部处理完": processed == WORKERS * PER_WORKER,
        "行动无重复领取": dup == 0,
        "无重复记忆": mem_dups == 0,
        "无无来源情景记忆": orphan_mem == 0,
        "会话级记忆都有会话": bad_conv == 0,
        "无异常": len(errors) == 0,
    }
    report["invariants"] = checks
    report["memory_count"] = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    report["outbox_count"] = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    report["seconds_total"] = round(time.time() - started, 2)
    report["ok"] = all(checks.values())

    print(json.dumps(report, ensure_ascii=False, indent=2))
    conn.close()

    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        print("\n压测数据已清除。", flush=True)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
