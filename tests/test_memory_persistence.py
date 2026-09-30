from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_memory_module():
    # Load only the storage module, without importing the plugin entry point or
    # registering matchers, starting background work, or touching real bot data.
    package_name = "_catty_memory_persistence_tests"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [
            str(Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai")
        ]
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.memory")


memory = _load_memory_module()


class MemoryPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = memory.Config(catty_memory_path=str(Path(temporary.name) / "memory.json"))
        self.store = memory.MemoryStore(self.config)

    def test_failed_flush_keeps_dirty_data_for_retry(self) -> None:
        self.store.set_preferred_name("10001", "new name")
        with patch.object(self.store, "_persist_now", side_effect=OSError("disk unavailable")):
            self.assertFalse(self.store.flush_sync())
        self.assertTrue(self.store.flush_sync())
        self.assertEqual(memory.MemoryStore(self.config).preferred_name_for("10001"), "new name")
        self.assertFalse(self.store.flush_sync())

    def test_refresh_failure_does_not_replace_unsaved_memory(self) -> None:
        self.store.set_preferred_name("10001", "old name")
        self.assertTrue(self.store.flush_sync())
        self.store.set_preferred_name("10001", "new name")
        with patch.object(self.store, "_persist_now", side_effect=OSError("disk unavailable")):
            with self.assertRaises(memory.MemoryPersistenceError):
                self.store.refresh()
        self.assertEqual(self.store.preferred_name_for("10001"), "new name")
        self.assertTrue(self.store.flush_sync())
        self.assertEqual(memory.MemoryStore(self.config).preferred_name_for("10001"), "new name")

    def test_strict_flush_preserves_original_error_and_can_retry(self) -> None:
        self.store.set_preferred_name("10001", "new name")
        failure = PermissionError("file is temporarily locked")
        with patch.object(self.store, "_persist_now", side_effect=failure):
            with self.assertRaises(memory.MemoryPersistenceError) as caught:
                self.store.flush_sync(raise_on_error=True)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertTrue(self.store.flush_sync(raise_on_error=True))
        self.assertEqual(memory.MemoryStore(self.config).preferred_name_for("10001"), "new name")

    def test_partial_entity_write_failure_retries_entire_snapshot(self) -> None:
        for user_id in ("10001", "10002"):
            self.store.set_preferred_name(user_id, "old name")
        self.assertTrue(self.store.flush_sync())
        for user_id in ("10001", "10002"):
            self.store.set_preferred_name(user_id, "new name")
        write_text = memory._atomic_write_text

        def fail_second_user(path, content):
            if path == self.store._user_file("10002"):
                raise OSError("disk full after first entity")
            write_text(path, content)

        with patch.object(memory, "_atomic_write_text", side_effect=fail_second_user):
            self.assertFalse(self.store.flush_sync())
        self.assertTrue(self.store.flush_sync())
        restored = memory.MemoryStore(self.config)
        for user_id in ("10001", "10002"):
            self.assertEqual(restored.preferred_name_for(user_id), "new name")
        self.assertFalse(self.store.flush_sync())

    def test_successful_refresh_flushes_pending_changes(self) -> None:
        self.store.set_preferred_name("10001", "new name")
        self.store.refresh()
        self.assertEqual(self.store.preferred_name_for("10001"), "new name")
        self.assertEqual(memory.MemoryStore(self.config).preferred_name_for("10001"), "new name")
        self.assertFalse(self.store.flush_sync())

    def test_clean_refresh_loads_external_changes(self) -> None:
        external = memory.MemoryStore(self.config)
        external.set_preferred_name("10001", "external name")
        self.assertTrue(external.flush_sync())
        self.store.refresh()
        self.assertEqual(self.store.preferred_name_for("10001"), "external name")

    def test_clean_or_disabled_strict_flush_is_a_noop(self) -> None:
        with patch.object(self.store, "_persist_now") as persist:
            self.assertFalse(self.store.flush_sync(raise_on_error=True))
            persist.assert_not_called()
        disabled = memory.MemoryStore(self.config.model_copy(update={"catty_memory_enabled": False}))
        disabled.set_preferred_name("10001", "unused name")
        with patch.object(disabled, "_persist_now") as persist:
            self.assertFalse(disabled.flush_sync(raise_on_error=True))
            disabled.refresh()
            persist.assert_not_called()

    def test_atomic_replace_failure_keeps_previous_file_and_retries(self) -> None:
        self.store.set_preferred_name("10001", "old name")
        self.assertTrue(self.store.flush_sync())
        old_index = self.store.path.read_bytes()
        self.store.set_preferred_name("10001", "new name")
        with patch.object(Path, "replace", side_effect=PermissionError("temporary file lock")):
            self.assertFalse(self.store.flush_sync())
        self.assertEqual(self.store.path.read_bytes(), old_index)
        self.assertFalse(self.store.path.with_suffix(".json.tmp").exists())
        self.assertTrue(self.store.flush_sync())
        self.assertEqual(memory.MemoryStore(self.config).preferred_name_for("10001"), "new name")


if __name__ == "__main__":
    unittest.main()
