"""备份与恢复验收（NEXT-STEPS 阶段 A / 4.2 / 4.3）。

要证明的：
1. 备份是「一致性快照」，写并发时也能过完整性检查；
2. 清单记录足够（校验值/水位/版本/代码修订），且不含凭据；
3. 被改动的备份必须被拒绝；
4. 恢复先隔离再切换，旧库保留，可回退；
5. 没有 --confirm 不许动现有库。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import backup, db  # noqa: E402
from newborn.events import EventEnvelope  # noqa: E402


def make_env(key: str, text: str = "备份测试消息") -> EventEnvelope:
    return EventEnvelope(
        event_type="message.received", source="verify", payload={"text": text},
        instance_id="default", actor_id="u1", conversation_id="c1",
        privacy_scope="conversation", trust_level="user", idempotency_key=key)


class BackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "main.sqlite3"
        conn = db.connect(self.db)
        from newborn import kernel
        for i in range(3):
            kernel.process_event(conn, make_env(f"k{i}", f"第{i}条消息"))
        conn.close()
        self.backup_dir = self.root / "backup-1"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------ 备份

    def test_backup_creates_manifest_and_file(self) -> None:
        manifest = backup.create_backup(self.db, self.backup_dir)
        self.assertTrue((self.backup_dir / backup.DB_FILE).exists())
        self.assertTrue((self.backup_dir / backup.MANIFEST).exists())
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(len(manifest["db_sha256"]), 64)
        self.assertGreater(manifest["input_watermark"], 0)
        self.assertIsNone(manifest["deletion_ledger_watermark"])
        self.assertNotIn("token", json.dumps(manifest).lower())

    def test_verify_ok(self) -> None:
        backup.create_backup(self.db, self.backup_dir)
        self.assertEqual(backup.verify_backup(self.backup_dir), [])

    def test_tampered_backup_is_rejected(self) -> None:
        backup.create_backup(self.db, self.backup_dir)
        target = self.backup_dir / backup.DB_FILE
        data = bytearray(target.read_bytes())
        data[-1] ^= 0xFF
        target.write_bytes(bytes(data))
        problems = backup.verify_backup(self.backup_dir)
        self.assertTrue(any("校验值" in p for p in problems), problems)

    def test_missing_manifest_is_rejected(self) -> None:
        backup.create_backup(self.db, self.backup_dir)
        (self.backup_dir / backup.MANIFEST).unlink()
        self.assertTrue(backup.verify_backup(self.backup_dir))

    def test_refuses_non_empty_destination(self) -> None:
        self.backup_dir.mkdir()
        (self.backup_dir / "stray.txt").write_text("x")
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.db, self.backup_dir)

    def test_consistent_snapshot_under_concurrent_writes(self) -> None:
        stop = threading.Event()

        def writer() -> None:
            c = db.connect(self.db)
            i = 0
            try:
                while not stop.is_set():
                    from newborn import kernel
                    kernel.process_event(c, make_env(f"live-{i}", f"并发写入 {i}"))
                    i += 1
            finally:
                c.close()

        t = threading.Thread(target=writer)
        t.start()
        try:
            time.sleep(0.2)
            backup.create_backup(self.db, self.backup_dir)
        finally:
            stop.set()
            t.join()
        self.assertEqual(backup.verify_backup(self.backup_dir), [],
                         "并发写入时做的快照也必须能过完整性校验")

    # ------------------------------------------------------------ 恢复

    def test_restore_requires_confirm(self) -> None:
        backup.create_backup(self.db, self.backup_dir)
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(self.backup_dir, self.db)

    def test_restore_to_new_path_keeps_data(self) -> None:
        backup.create_backup(self.db, self.backup_dir)
        target = self.root / "restored.sqlite3"
        res = backup.restore_backup(self.backup_dir, target, confirm=True)
        self.assertEqual(res["status"], "restored")
        self.assertIsNone(res["previous"])
        conn = db.connect(target)
        try:
            n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            self.assertEqual(n, 3, "恢复后数据应完整")
        finally:
            conn.close()

    def test_restore_keeps_previous_file(self) -> None:
        backup.create_backup(self.db, self.backup_dir)
        # 再往里写一条，恢复后应该回到备份时点
        conn = db.connect(self.db)
        from newborn import kernel
        kernel.process_event(conn, make_env("after-backup", "备份之后写的"))
        conn.close()
        res = backup.restore_backup(self.backup_dir, self.db, confirm=True)
        self.assertIsNotNone(res["previous"])
        self.assertTrue(Path(res["previous"]).exists(), "旧库必须保留以便回退")
        conn = db.connect(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            self.assertEqual(n, 3, "恢复后应回到备份时点的数据")
        finally:
            conn.close()

    def test_restore_rejects_tampered_backup(self) -> None:
        backup.create_backup(self.db, self.backup_dir)
        target = self.backup_dir / backup.DB_FILE
        data = bytearray(target.read_bytes())
        data[-1] ^= 0xFF
        target.write_bytes(bytes(data))
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(self.backup_dir, self.root / "x.sqlite3", confirm=True)


if __name__ == "__main__":
    unittest.main()
