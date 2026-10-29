"""备份与恢复（NEXT-STEPS 阶段 A）。

设计要点（与 NEXT-STEPS 4.2/4.3 对齐，第一版只做本地、操作员显式发起）：

1. 用 SQLite 官方 backup API 做一致性快照，不手工拷主文件 + WAL（那组合可能不匹配）。
2. 备份目录里只有两样东西：`newborn.sqlite3` 和 `manifest.json`。
   manifest 记录：备份 ID、UTC 时间、代码修订、schema 版本、算法版本、实例范围、
   文件校验值、输入水位、格式版本。不写令牌、不写推理凭据。
3. 恢复**先隔离**：校验 → 拷到临时路径 → 完整性与版本检查 → 才换到目标路径，
   旧库改名为 `<target>.pre-restore-<ts>` 保留，出问题能退回。
4. 账本（删除撤销）目前还不存在，manifest 里留 `deletion_ledger_watermark: null`，
   等阶段 B 接上后恢复流程必须重新回放它。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
DB_FILE = "newborn.sqlite3"
MANIFEST = "manifest.json"


class BackupError(RuntimeError):
    """备份/恢复被拒绝。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_revision() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=5, cwd=str(Path(__file__).resolve().parents[2]))
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _watermark(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT MAX(received_at) FROM events").fetchone()
    return float(row[0] or 0.0)


def _schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def create_backup(db_path: str | Path, dest_dir: str | Path, *,
                  instance_scope: str | None = None,
                  algorithm_version: str = "consolidate-2") -> dict[str, Any]:
    """做一份一致快照，返回 manifest（同时写入磁盘）。"""
    src = Path(db_path)
    if not src.exists():
        raise BackupError(f"数据库不存在：{src}")
    dest = Path(dest_dir)
    if dest.exists() and any(dest.iterdir()):
        raise BackupError(f"备份目录非空，拒绝覆盖：{dest}")
    dest.mkdir(parents=True, exist_ok=True)

    tmp = dest / (DB_FILE + ".tmp")
    src_conn = sqlite3.connect(str(src), timeout=10)
    try:
        src_conn.execute("PRAGMA busy_timeout = 5000")
        dst_conn = sqlite3.connect(str(tmp))
        try:
            src_conn.backup(dst_conn)          # 官方一致性快照
        finally:
            dst_conn.close()
        check = src_conn.execute("PRAGMA integrity_check").fetchone()[0]
        schema_version = _schema_version(src_conn)
        watermark = _watermark(src_conn)
    finally:
        src_conn.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise BackupError(f"源库完整性检查失败：{check}")

    tmp.replace(dest / DB_FILE)
    manifest = {
        "format_version": FORMAT_VERSION,
        "backup_id": "bk_" + uuid.uuid4().hex[:12],
        "created_utc": _utc_now(),
        "code_revision": _code_revision(),
        "schema_version": schema_version,
        "algorithm_version": algorithm_version,
        "instance_scope": instance_scope,
        "db_file": DB_FILE,
        "db_sha256": _sha256(dest / DB_FILE),
        "db_bytes": (dest / DB_FILE).stat().st_size,
        "input_watermark": watermark,
        "deletion_ledger_watermark": None,   # 阶段 B 接入撤销账本后填
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    return manifest


def read_manifest(backup_dir: str | Path) -> dict[str, Any]:
    path = Path(backup_dir) / MANIFEST
    if not path.exists():
        raise BackupError(f"缺少清单：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackupError(f"清单损坏：{exc}") from exc


def verify_backup(backup_dir: str | Path) -> list[str]:
    """校验清单、文件校验值、库完整性和 schema 版本。返回问题列表（空 = 可用）。"""
    problems: list[str] = []
    d = Path(backup_dir)
    try:
        manifest = read_manifest(d)
    except BackupError as exc:
        return [str(exc)]
    if manifest.get("format_version") != FORMAT_VERSION:
        problems.append(f"格式版本不支持：{manifest.get('format_version')}")
    db = d / str(manifest.get("db_file") or DB_FILE)
    if not db.exists():
        problems.append(f"备份文件缺失：{db.name}")
        return problems
    actual = _sha256(db)
    if actual != manifest.get("db_sha256"):
        problems.append("文件校验值不匹配（可能被改动或损坏）")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"数据库完整性检查失败：{integrity}")
        if _schema_version(conn) > 2:
            problems.append("备份的 schema 版本比当前代码新，拒绝恢复")
    except sqlite3.DatabaseError as exc:
        problems.append(f"无法读取备份数据库：{exc}")
    finally:
        conn.close()
    return problems


def restore_backup(backup_dir: str | Path, target_db: str | Path, *,
                   confirm: bool = False) -> dict[str, Any]:
    """先校验再隔离恢复；旧库改名保留。必须显式 confirm。"""
    if not confirm:
        raise BackupError("恢复是破坏性操作：加 --confirm")
    problems = verify_backup(backup_dir)
    if problems:
        raise BackupError("备份不可用：" + "；".join(problems))

    src_dir = Path(backup_dir)
    manifest = read_manifest(src_dir)
    target = Path(target_db)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(target.suffix + ".restore-tmp")
    staging.unlink(missing_ok=True)

    shutil.copy2(src_dir / str(manifest.get("db_file") or DB_FILE), staging)
    conn = sqlite3.connect(str(staging))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            staging.unlink(missing_ok=True)
            raise BackupError(f"恢复区完整性检查失败：{integrity}")
        restored_schema = _schema_version(conn)
    finally:
        conn.close()

    previous = None
    if target.exists():
        previous = target.with_name(target.name + ".pre-restore-" + _utc_now().replace(":", ""))
        shutil.move(str(target), str(previous))
    try:
        os.replace(staging, target)
    except OSError:
        if previous:
            shutil.move(str(previous), str(target))
        raise

    return {
        "status": "restored",
        "backup_id": manifest.get("backup_id"),
        "target": str(target),
        "previous": str(previous) if previous else None,
        "schema_version": restored_schema,
        "code_revision": manifest.get("code_revision"),
        "needs_upgrade": restored_schema < 2,
    }
