r"""把 DSH 会话接到 newborn 大脑上的最小桥。

用法（从任意目录）：
    python D:\DeepSeekHarness\newborn\tools\session_bridge.py remember "文本" [--actor u]
    python D:\DeepSeekHarness\newborn\tools\session_bridge.py recall "关键词" [--limit 5]
    python D:\DeepSeekHarness\newborn\tools\session_bridge.py status

它只做三件事：写一条事件进 newborn、按关键词取回记忆、看当前统计。
不做自动注入——注入需要 DSH 插件，那是下一步的事。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from newborn import db, kernel, memory as memory_mod  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402
from newborn.memory import AccessContext  # noqa: E402

DEFAULT_DB = ROOT / "memory" / "live.sqlite3"


def _conn(path: Path):
    return db.connect(path)


def cmd_remember(args) -> int:
    conn = _conn(args.db)
    env = EventEnvelope(
        event_type="message.received", source="dsh-session",
        payload={"text": args.text}, instance_id=args.instance, actor_id=args.actor,
        conversation_id=args.conversation, privacy_scope=args.privacy, trust_level="user")
    res = kernel.process_event(conn, env)
    print(json.dumps(res, ensure_ascii=False, default=str))
    return 0


def cmd_recall(args) -> int:
    conn = _conn(args.db)
    ctx = AccessContext(instance_id=args.instance, conversation_id=args.conversation,
                        subject_id=args.actor, purpose="recall")
    rows = memory_mod.retrieve(conn, ctx, args.query, limit=args.limit)
    out = [{"id": r["memory_id"], "type": r["memory_type"], "text": r["content"],
            "scope": r["privacy_scope"], "status": r["epistemic_status"]} for r in rows]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args) -> int:
    conn = _conn(args.db)
    print(json.dumps(kernel.stats(conn, args.instance), ensure_ascii=False, indent=2,
                     default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DSH 会话 ↔ newborn 桥")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--instance", default="default")
    ap.add_argument("--actor", default="owner")
    ap.add_argument("--conversation", default="dsh-private")
    ap.add_argument("--privacy", default="private",
                    choices=["private", "conversation", "instance", "public"])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("remember", help="写一条经历")
    p.add_argument("text")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("recall", help="按关键词取回")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("status", help="统计")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
