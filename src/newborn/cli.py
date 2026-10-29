"""newborn 命令行入口（M1）。

用法：
    python -m newborn.cli init
    python -m newborn.cli ingest "你好，我叫宝宝" --actor u1 --conversation c1
    python -m newborn.cli run
    python -m newborn.cli status
    python -m newborn.cli recall 宝宝 --conversation c1 --actor u1
    python -m newborn.cli claim --worker w1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import db, kernel, memory as memory_mod
from .events import EventEnvelope
from .reasoner import make_reasoner

# Windows 控制台默认 cp1252/cp936 打中文会崩；这里强制 utf-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_DB = Path(__file__).resolve().parents[2] / "memory" / "newborn.sqlite3"


def _db_path(arg: str | None) -> Path:
    return Path(arg) if arg else Path(os.environ.get("NEWBORN_DB") or DEFAULT_DB)


def cmd_init(args) -> int:
    conn = db.connect(_db_path(args.db))
    print(f"已初始化：{_db_path(args.db)}")
    print(json.dumps(kernel.stats(conn, args.instance), ensure_ascii=False, indent=2))
    return 0


def cmd_ingest(args) -> int:
    conn = db.connect(_db_path(args.db))
    env = EventEnvelope(
        event_type="message.received",
        source=args.source,
        payload={"text": args.text},
        instance_id=args.instance,
        actor_id=args.actor,
        conversation_id=args.conversation,
        trust_level="user",
        privacy_scope=args.privacy,
        idempotency_key=args.key,
    )
    res = kernel.process_event(conn, env, make_reasoner())
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["status"] in ("processed", "duplicate") else 1


def cmd_run(args) -> int:
    conn = db.connect(_db_path(args.db))
    results = kernel.process_pending(conn, limit=args.limit, reasoner=make_reasoner())
    print(json.dumps(results, ensure_ascii=False, indent=2) if results else "没有待处理事件")
    return 0


def cmd_status(args) -> int:
    conn = db.connect(_db_path(args.db))
    print(json.dumps(kernel.stats(conn, args.instance), ensure_ascii=False, indent=2))
    return 0


def cmd_recall(args) -> int:
    conn = db.connect(_db_path(args.db))
    ctx = memory_mod.AccessContext(
        instance_id=args.instance,
        conversation_id=args.conversation,
        subject_id=args.actor,
        purpose="cli",
    )
    rows = memory_mod.retrieve(conn, ctx, query=args.query, limit=args.limit)
    for r in rows:
        print(f"- [{r['memory_type']}/{r['epistemic_status']}/{r['privacy_scope']}]"
              f" {r['content'][:80]}  (来源 {len(r['source_event_ids'])} 个事件)")
    if not rows:
        print("没有匹配的记忆（当前访问上下文看不到任何东西）")
    return 0


def cmd_claim(args) -> int:
    conn = db.connect(_db_path(args.db))
    claimed = kernel.claim_actions(conn, args.worker, limit=args.limit,
                                   lease_seconds=args.lease)
    print(json.dumps(claimed, ensure_ascii=False, indent=2) if claimed else "没有可领取的行动")
    return 0


def cmd_worker(args) -> int:
    from . import scheduler
    res = scheduler.serve_forever(_db_path(args.db), args.instance,
                                  interval=args.interval, window_hours=args.hours,
                                  once=args.once)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_purge(args) -> int:
    from . import retention
    conn = db.connect(_db_path(args.db))
    res = retention.purge(conn, _db_path(args.db), confirm=args.confirm)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_delete(args) -> int:
    from . import retention
    conn = db.connect(_db_path(args.db))
    target = args.target
    if args.scope == "conversation" and "|" not in target:
        target = f"{args.instance}|{target}"
    if args.preview or not args.confirm:
        preview = retention.preview_deletion(conn, args.instance, args.scope, target)
        print(json.dumps({"preview": preview, "confirm_required": not args.confirm},
                         ensure_ascii=False, indent=2))
        return 0
    res = retention.request_deletion(conn, _db_path(args.db), args.instance, args.scope, target)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_feedback(args) -> int:
    from . import feedback
    conn = db.connect(_db_path(args.db))
    res = feedback.add_feedback(conn, args.instance, args.kind, memory_id=args.memory_id,
                                note=args.note or "")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_replay_ledger(args) -> int:
    from . import retention
    conn = db.connect(_db_path(args.db))
    res = retention.replay_ledger(conn, args.ledger or retention.ledger_path(_db_path(args.db)))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_jobs(args) -> int:
    conn = db.connect(_db_path(args.db))
    jobs = conn.execute(
        "SELECT job_id, instance_id, window_start, window_end, seed, algorithm_version,"
        " input_watermark, status, created_at, produced FROM consolidation_jobs"
        " WHERE instance_id = ? ORDER BY created_at DESC LIMIT ?",
        (args.instance, args.limit)).fetchall()
    out = []
    for j in jobs:
        produced = loads(j["produced"]) if j["produced"] else {}
        out.append({"job_id": j["job_id"], "window_hours": round((j["window_end"] - j["window_start"]) / 3600, 2),
                    "seed": j["seed"], "algorithm_version": j["algorithm_version"],
                    "input_watermark": j["input_watermark"], "status": j["status"],
                    "clusters": produced.get("clusters"),
                    "traits": produced.get("traits")})
    print(json.dumps({"count": len(out), "jobs": out}, ensure_ascii=False, indent=2))
    return 0


def cmd_backup(args) -> int:
    from . import backup
    manifest = backup.create_backup(_db_path(args.db), args.dest,
                                    instance_scope=args.instance)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_backup(args) -> int:
    from . import backup
    problems = backup.verify_backup(args.frm)
    print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


def cmd_restore(args) -> int:
    from . import backup
    try:
        res = backup.restore_backup(args.frm, args.to or _db_path(args.db),
                                    confirm=args.confirm)
    except backup.BackupError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_consolidate(args) -> int:
    from . import consolidate as cg
    from .inference import get_backend
    conn = db.connect(_db_path(args.db))
    backend = None if args.no_model else get_backend()
    res = cg.consolidate(
        conn, args.instance, window_hours=args.hours, seed=args.seed,
        per_day=args.per_day, per_conv=args.per_conv,
        backend=backend, min_days=args.min_days,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["status"] in ("done", "duplicate_job", "empty") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="newborn", description="NeuroMind M1 命令行")
    p.add_argument("--db", help="sqlite 路径")
    p.add_argument("--instance", default="default", help="实例 id")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="初始化数据库")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("ingest", help="投递一条文本事件")
    s.add_argument("text")
    s.add_argument("--actor", default="user")
    s.add_argument("--conversation", default=None)
    s.add_argument("--source", default="cli")
    s.add_argument("--privacy", default="private",
                   choices=["private", "conversation", "instance", "public"],
                   help="记忆可见范围；conversation 需要同时给 --conversation")
    s.add_argument("--key", default=None, help="幂等键；不传则按 event_id 生成")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("run", help="重试待处理/失败事件")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="统计")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("recall", help="按访问上下文检索记忆")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--conversation", default=None)
    s.add_argument("--actor", default=None)
    s.set_defaults(func=cmd_recall)

    s = sub.add_parser("claim", help="领取待执行行动（带租约）")
    s.add_argument("--worker", default="w1")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--lease", type=float, default=60.0)
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("worker", help="维护循环：该巩固了就巩一轮")
    s.add_argument("--once", action="store_true", help="只跑一轮")
    s.add_argument("--interval", type=float, default=600.0)
    s.add_argument("--hours", type=float, default=24.0)
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser("purge", help="物理清理已删除的正文（需 --confirm）")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(func=cmd_purge)

    s = sub.add_parser("delete", help="删除对象/会话/实例（默认只预览）")
    s.add_argument("--scope", choices=["object", "conversation", "instance"], default="object")
    s.add_argument("--target", required=True)
    s.add_argument("--instance", default="default")
    s.add_argument("--preview", action="store_true")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("feedback", help="记录一条显式反馈")
    s.add_argument("kind", choices=["correct", "reject", "confirm"])
    s.add_argument("--memory-id", default=None)
    s.add_argument("--note", default="")
    s.add_argument("--instance", default="default")
    s.set_defaults(func=cmd_feedback)

    s = sub.add_parser("replay-ledger", help="恢复后重放删除撤销账本")
    s.add_argument("--ledger", default=None)
    s.set_defaults(func=cmd_replay_ledger)

    s = sub.add_parser("jobs", help="查看巩固任务（只读）")
    s.add_argument("--instance", default="default")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_jobs)

    s = sub.add_parser("backup", help="做一份一致性备份")
    s.add_argument("--dest", required=True, help="备份目录（必须为空或不存在）")
    s.add_argument("--instance", default=None)
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("verify-backup", help="校验备份是否可用")
    s.add_argument("--from", dest="frm", required=True)
    s.set_defaults(func=cmd_verify_backup)

    s = sub.add_parser("restore", help="从备份恢复（破坏性，需 --confirm）")
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", default=None, help="恢复目标（默认当前 --db）")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("consolidate", help="巩固：把情景压成语义（皮层）")
    s.add_argument("--hours", type=float, default=24.0, help="回看时间窗（小时）")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--per-day", type=int, default=20)
    s.add_argument("--per-conv", type=int, default=20)
    s.add_argument("--min-days", type=int, default=2, help="至少覆盖几天才允许产出")
    s.add_argument("--no-model", action="store_true", help="只用规则模板，不调模型")
    s.set_defaults(func=cmd_consolidate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
