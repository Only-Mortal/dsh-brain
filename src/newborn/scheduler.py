"""轻量维护调度（NEXT-STEPS 阶段 C 6.2 的第一版）。

刻意保持小：
- 只有一个循环，按间隔检查「该不该做巩固」，做了就记一条 job；
- 任务写进 consolidation_jobs（已有唯一约束保证同窗口同 seed 只有一次）；
- 不引入通用工作流，不做任务队列；
- 前台的认知事件路径完全不受影响（巩固在独立连接里跑短事务）。

用法：
    python -m newborn.cli worker --once          # 跑一轮就退出（适合计划任务）
    python -m newborn.cli worker --interval 600  # 常驻，每 10 分钟看一次
"""

from __future__ import annotations

import time
from typing import Any

from . import consolidate, db
from .inference import get_backend

DEFAULT_INTERVAL = 600.0
DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_MIN_EPISODICS = 6


def _due(conn, instance_id: str, window_hours: float, min_gap_seconds: float) -> bool:
    row = conn.execute(
        "SELECT MAX(created_at) AS last FROM consolidation_jobs WHERE instance_id = ?",
        (instance_id,)).fetchone()
    if row and row["last"] and (time.time() - row["last"]) < min_gap_seconds:
        return False
    since = time.time() - window_hours * 3600
    n = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE instance_id = ? AND memory_type='episodic'"
        " AND lifecycle_state IN ('active','candidate') AND created_at >= ?",
        (instance_id, since)).fetchone()[0]
    return n >= DEFAULT_MIN_EPISODICS


def run_once(conn, db_path: str, instance_id: str = "default", *,
             window_hours: float = DEFAULT_WINDOW_HOURS,
             min_gap_seconds: float = DEFAULT_INTERVAL,
             seed: int | None = None) -> dict[str, Any]:
    if not _due(conn, instance_id, window_hours, min_gap_seconds):
        return {"status": "idle", "reason": "还没有到巩固条件（条目太少或距上次太近）"}
    seed = seed if seed is not None else int(time.time() // 60) % 1000
    try:
        res = consolidate.consolidate(conn, instance_id, window_hours=window_hours,
                                      seed=seed, backend=get_backend())
    except Exception as exc:                      # 维护任务失败不能拖垮主循环
        return {"status": "error", "error": str(exc), "seed": seed}
    return {"status": res.get("status"), "seed": seed,
            "clusters": res.get("clusters"), "job_id": res.get("job_id")}


def serve_forever(db_path: str, instance_id: str = "default", *,
                  interval: float = DEFAULT_INTERVAL,
                  window_hours: float = DEFAULT_WINDOW_HOURS,
                  once: bool = False) -> list[dict[str, Any]]:
    """循环执行；once=True 时只跑一轮（适合交给系统计划任务）。"""
    results: list[dict[str, Any]] = []
    while True:
        conn = db.connect(db_path)
        try:
            results.append(run_once(conn, db_path, instance_id, window_hours=window_hours,
                                    min_gap_seconds=interval))
        finally:
            conn.close()
        if once:
            return results
        time.sleep(interval)
