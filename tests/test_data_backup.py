from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "data_backup.py"
_SPEC = importlib.util.spec_from_file_location("catty_data_backup_test_module", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
backup = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = backup
_SPEC.loader.exec_module(backup)


class DataBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "memory.json").write_text('{"version": 2}', encoding="utf-8")
        (self.root / "affection.json").write_text('{"users": {}}', encoding="utf-8")
        (self.root / "config.json").write_text('{"ai": {"api_key": "secret"}}', encoding="utf-8")
        (self.root / "notes.txt").write_text("not data", encoding="utf-8")
        users = self.root / "memory_users"
        users.mkdir()
        (users / "user_1.json").write_text('{"user_id": "1"}', encoding="utf-8")
        sessions = self.root / "sessions"
        (sessions / "nested").mkdir(parents=True)
        (sessions / "nested" / "s.json").write_text("[]", encoding="utf-8")
        self.dirs = [users, self.root / "memory_groups", sessions]
        self.backup_dir = self.root / "backup"

    def test_snapshot_contains_data_but_not_config(self) -> None:
        target, count = backup.write_snapshot(self.root, self.dirs, self.backup_dir, keep=7)
        self.assertIsNotNone(target)
        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            self.assertEqual(archive.read("memory_users/user_1.json"), b'{"user_id": "1"}')
        self.assertEqual(
            names,
            {"affection.json", "memory.json", "memory_users/user_1.json", "sessions/nested/s.json"},
        )
        self.assertEqual(count, 4)
        self.assertFalse(list(self.backup_dir.glob("*.tmp")))

    def test_prune_keeps_only_newest_snapshots(self) -> None:
        for day in range(1, 6):
            backup.write_snapshot(self.root, self.dirs, self.backup_dir, keep=3, now=datetime(2026, 10, day, 3, 0, 0))
        names = [path.name for path in backup.list_snapshots(self.backup_dir)]
        self.assertEqual(
            names,
            ["data-20261003-030000.zip", "data-20261004-030000.zip", "data-20261005-030000.zip"],
        )
        (self.backup_dir / "deploy-20260815.zip").write_text("x", encoding="utf-8")
        backup.prune_snapshots(self.backup_dir, keep=1)
        self.assertTrue((self.backup_dir / "deploy-20260815.zip").exists())

    def test_newest_snapshot_age(self) -> None:
        self.assertIsNone(backup.newest_snapshot_age_seconds(self.backup_dir, time.time()))
        backup.write_snapshot(self.root, self.dirs, self.backup_dir, keep=7)
        age = backup.newest_snapshot_age_seconds(self.backup_dir, time.time())
        self.assertIsNotNone(age)
        self.assertLess(age, 60)

    def test_nothing_to_back_up_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            target, count = backup.write_snapshot(Path(empty), [], Path(empty) / "backup", keep=7)
        self.assertIsNone(target)
        self.assertEqual(count, 0)

    def test_sqlite_snapshot_includes_committed_wal_content(self) -> None:
        path = self.root / "insight_memory.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE messages (text TEXT)")
            connection.execute("INSERT INTO messages VALUES ('current')")
            connection.commit()
            target, count = backup.write_snapshot(self.root, self.dirs, self.backup_dir, keep=7)
            self.assertEqual(count, 5)
            with zipfile.ZipFile(target) as archive:
                restored = self.root / "restored.sqlite3"
                restored.write_bytes(archive.read("insight_memory.sqlite3"))
            with closing(sqlite3.connect(restored)) as snapshot:
                self.assertEqual(snapshot.execute("SELECT text FROM messages").fetchall(), [("current",)])


if __name__ == "__main__":
    unittest.main()
