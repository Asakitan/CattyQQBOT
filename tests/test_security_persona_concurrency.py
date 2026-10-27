from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "catty_qq_ai" / "persona_override_store.py"
spec = importlib.util.spec_from_file_location("_persona_concurrency_test_store", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Store = module.PersonaOverrideStore


CHILD = r"""
import importlib.util
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("_child_override_store", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = module.PersonaOverrideStore(Path(sys.argv[2]) / "memory.json")
mode = sys.argv[3]
print("ready", flush=True)
if sys.stdin.readline().strip() != "start":
    raise RuntimeError("missing start")
if mode == "first":
    write = store._write_locked
    def hold(candidate):
        print("held", flush=True)
        if sys.stdin.readline().strip() != "release":
            raise RuntimeError("missing release")
        return write(candidate)
    store._write_locked = hold
    store.set("group:first", "catty")
elif mode == "second":
    try_lock = module._try_file_lock
    notified = False
    def observe(fd):
        global notified
        try:
            return try_lock(fd)
        except OSError:
            if not notified:
                notified = True
                print("contended", flush=True)
            raise
    module._try_file_lock = observe
    if not store.clear("group:seed"):
        raise RuntimeError("seed disappeared before clear")
    store.set("group:second", "fadianji")
elif mode == "abandon":
    with module._exclusive_file_lock(store._path.with_name(store._path.name + ".lock")):
        print("held", flush=True)
        sys.stdin.readline()
else:
    raise RuntimeError("invalid child mode")
print("done", flush=True)
"""


class PersonaConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = self.root / "memory.json"
        self.path = self.root / "persona_overrides.json"

    def spawn(self, mode):
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", CHILD,
             str(MODULE_PATH), str(self.root), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", bufsize=1,
        )
        lines = queue.Queue()

        def pump():
            for line in process.stdout:
                lines.put(line.strip())
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()

        self.addCleanup(cleanup)
        return process, lines

    def expect_line(self, lines, expected):
        try:
            actual = lines.get(timeout=10)
        except queue.Empty:
            self.fail(f"timed out waiting for {expected!r}")
        self.assertEqual(actual, expected)

    @staticmethod
    def send(process, line):
        process.stdin.write(line + "\n")
        process.stdin.flush()

    def test_interleaved_set_clear_and_fresh_reads(self):
        first, second = Store(self.memory), Store(self.memory)
        first.set("group:1", "catty")
        self.assertEqual(second.get("group:1"), "catty")
        second.set("group:2", "fadianji")
        self.assertEqual(first.all_overrides(), {"group:1": "catty", "group:2": "fadianji"})
        self.assertTrue(first.clear("group:2"))
        second.set("private:3", "other")
        self.assertTrue(second.clear("group:1"))
        self.assertFalse(first.clear("group:2"))
        self.assertEqual(first.all_overrides(), {"private:3": "other"})
        self.assertEqual(Store(self.memory).all_overrides(), {"private:3": "other"})

    def test_unrelated_records_and_invalid_times_survive_merge(self):
        raw = {"group:1": {"persona": "catty", "set_at": "bad", "extra": 7},
               "group:2": {"persona": "other", "set_at": "nan"}, "unknown": "preserve"}
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        store = Store(self.memory)
        self.assertEqual(store._data["group:1"]["set_at"], 0.0)
        self.assertEqual(store._data["group:2"]["set_at"], 0.0)
        store.set("private:1", "fadianji")
        result = json.loads(self.path.read_text(encoding="utf-8"))
        for key, value in raw.items():
            self.assertEqual(result[key], value)

    def test_threaded_instances_contend_and_merge(self):
        first, second = Store(self.memory), Store(self.memory)
        entered, release, contended = threading.Event(), threading.Event(), threading.Event()
        write, try_lock = first._write_locked, module._try_file_lock

        def hold(candidate):
            entered.set()
            if not release.wait(8):
                raise TimeoutError("test writer was not released")
            return write(candidate)

        def observe(fd):
            try:
                return try_lock(fd)
            except OSError:
                contended.set()
                raise

        with patch.object(first, "_write_locked", side_effect=hold), patch.object(module, "_try_file_lock", side_effect=observe):
            with ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(first.set, "group:first", "catty")
                try:
                    self.assertTrue(entered.wait(5))
                    two = pool.submit(second.set, "group:second", "fadianji")
                    self.assertTrue(contended.wait(5))
                finally:
                    release.set()
                one.result(timeout=10)
                two.result(timeout=10)
        self.assertEqual(Store(self.memory).all_overrides(), {"group:first": "catty", "group:second": "fadianji"})

    def test_failed_set_and_clear_preserve_disk_and_local_snapshot(self):
        first, second = Store(self.memory), Store(self.memory)
        first.set("group:1", "catty")
        old_memory = deepcopy(first._data)
        second.set("group:2", "fadianji")
        old_disk = self.path.read_bytes()
        with patch.object(module.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                first.set("group:3", "other")
        self.assertEqual(first._data, old_memory)
        self.assertEqual(self.path.read_bytes(), old_disk)
        with patch.object(module.Path, "replace", side_effect=PermissionError("replace denied")):
            with self.assertRaises(PermissionError):
                first.clear("group:1")
        self.assertEqual(first._data, old_memory)
        self.assertEqual(self.path.read_bytes(), old_disk)
        self.assertEqual(list(self.root.glob(".persona_overrides.json.*.tmp")), [])
        self.assertEqual(first.all_overrides(), {"group:1": "catty", "group:2": "fadianji"})

    def test_corruption_after_initialization_is_never_overwritten(self):
        store = Store(self.memory)
        store.set("group:1", "catty")
        old_memory = deepcopy(store._data)
        for damaged in (b"{bad", b"\xff", b"[]"):
            with self.subTest(damaged=damaged):
                self.path.write_bytes(damaged)
                with self.assertRaises(OSError):
                    store.set("group:2", "other")
                with self.assertRaises(OSError):
                    store.clear("group:1")
                with self.assertRaises(OSError):
                    store.clear("group:missing")
                self.assertEqual(store._data, old_memory)
                self.assertEqual(self.path.read_bytes(), damaged)
                self.assertEqual(store.get("group:1"), "catty")
        self.path.write_text(json.dumps(old_memory), encoding="utf-8")
        store.set("group:2", "other")
        self.assertEqual(store.all_overrides(), {"group:1": "catty", "group:2": "other"})

    def test_two_real_processes_collide_and_keep_both_updates(self):
        observer = Store(self.memory)
        observer.set("group:seed", "old")
        first, first_lines = self.spawn("first")
        second, second_lines = self.spawn("second")
        self.expect_line(first_lines, "ready")
        self.expect_line(second_lines, "ready")
        self.send(first, "start")
        self.expect_line(first_lines, "held")
        self.send(second, "start")
        self.expect_line(second_lines, "contended")
        self.send(first, "release")
        self.expect_line(first_lines, "done")
        self.expect_line(second_lines, "done")
        self.assertEqual(first.wait(timeout=10), 0)
        self.assertEqual(second.wait(timeout=10), 0)
        self.assertEqual(observer.all_overrides(), {"group:first": "catty", "group:second": "fadianji"})

    def test_timeout_and_killed_holder_release_lock(self):
        store = Store(self.memory)
        store.set("group:1", "catty")
        old_disk, old_memory = self.path.read_bytes(), deepcopy(store._data)
        holder, lines = self.spawn("abandon")
        self.expect_line(lines, "ready")
        self.send(holder, "start")
        self.expect_line(lines, "held")
        with patch.object(module, "_LOCK_TIMEOUT_SECONDS", 0.15):
            with self.assertRaises(TimeoutError):
                store.set("group:2", "other")
        self.assertEqual(self.path.read_bytes(), old_disk)
        self.assertEqual(store._data, old_memory)
        holder.kill()
        holder.wait(timeout=10)
        self.assertTrue(self.path.with_name(self.path.name + ".lock").exists())
        store.set("group:2", "other")
        self.assertEqual(Store(self.memory).all_overrides(), {"group:1": "catty", "group:2": "other"})


if __name__ == "__main__":
    unittest.main()
