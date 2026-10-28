"""五层状态（README 第 5 节）M1 子集，已按审查意见收紧。

- 层名走白名单，拼错直接报错，不再默默写入。
- 版本必须递增，支持期望版本比较（乐观并发）。
"""

from __future__ import annotations

import time
from typing import Any

from .affect import Affect
from .db import dumps, loads

LAYERS = ("identity", "trait", "affective", "cognitive", "social")


class StateError(Exception):
    pass


def _check_layer(layer: str) -> None:
    if layer not in LAYERS:
        raise StateError(f"未知状态层: {layer}（合法值 {', '.join(LAYERS)}）")


def get(conn, instance_id: str, layer: str, scope: str = "default") -> dict[str, Any]:
    _check_layer(layer)
    row = conn.execute(
        "SELECT * FROM states WHERE instance_id = ? AND layer = ? AND scope = ?",
        (instance_id, layer, scope),
    ).fetchone()
    if not row:
        return {"version": 0, "data": {}, "updated_at": None, "source_event_id": None}
    d = dict(row)
    d["data"] = loads(d["data"])
    return d


def put(conn, instance_id: str, layer: str, data: dict[str, Any], *,
        scope: str = "default", source_event_id: str | None = None,
        expect_version: int | None = None) -> int:
    _check_layer(layer)
    cur = get(conn, instance_id, layer, scope)
    if expect_version is not None and cur["version"] != expect_version:
        raise StateError(f"版本冲突：期望 {expect_version}，实际 {cur['version']}")
    version = cur["version"] + 1
    ts = time.time()
    conn.execute(
        """
        INSERT INTO states (instance_id, layer, scope, version, updated_at, source_event_id, data)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(instance_id, layer, scope) DO UPDATE SET
            version = excluded.version,
            updated_at = excluded.updated_at,
            source_event_id = excluded.source_event_id,
            data = excluded.data
        """,
        (instance_id, layer, scope, version, ts, source_event_id, dumps(data)),
    )
    return version


def affect_of(conn, instance_id: str) -> Affect:
    return Affect.from_dict(get(conn, instance_id, "affective")["data"])


def save_affect(conn, instance_id: str, affect: Affect, source_event_id: str | None) -> int:
    return put(conn, instance_id, "affective", affect.to_dict(), source_event_id=source_event_id)
