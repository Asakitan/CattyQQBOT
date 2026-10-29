from __future__ import annotations

import ast
import asyncio
from dataclasses import asdict, replace
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from catty_self_repair.config import RepairConfig, load_config, state_dir
from catty_self_repair.storage import FileLock, atomic_json, read_json
from catty_self_repair.supervisor import (
    RecoveryBlocked, RepairSupervisor, STARTUP_RELATIVE_PATH, _worker_env,
)

spec = importlib.util.spec_from_file_location("repair_watcher_test", ROOT / "scripts/catty_hot_reload.py")
watcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watcher
spec.loader.exec_module(watcher)


class FakeProcess:
    def __init__(self, pid=123):
        self.pid = pid
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -1


def workspace_stub(**overrides):
    module = ModuleType("catty_self_repair.workspace")
    defaults = {
        "apply_release": {"status": "applied", "run_id": "fixture"},
        "recover_release": {"status": "rolled_back"},
        "rollback_release": {"status": "rolled_back"},
        "confirm_release": {"status": "confirmed"},
    }
    for name, result in defaults.items():
        setattr(module, name, overrides.get(name, Mock(return_value=result)))
    return module


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = [1000.0]
        self.supervisor = RepairSupervisor(self.root, clock=lambda: self.now[0])
        self.addCleanup(self.supervisor.close)

    def config(self, **overrides):
        settings = {
            "enabled": True, "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "fixture-only-key", "model": "fixture", **overrides,
        }
        config = RepairConfig.from_mapping(settings)
        atomic_json(self.root / "config.json", {"self_repair": asdict(config)})
        return config

    def ready(self, config=None, **overrides):
        config = config or self.config()
        release = state_dir(self.root) / "runs/fixture/release.json"
        atomic_json(release, {})
        atomic_json(state_dir(self.root) / "ready.json", {
            "run_id": "fixture", "release_path": "data/self_repair/runs/fixture/release.json",
            "config_fingerprint": config.fingerprint(), **overrides,
        })
        return release

    def active(self, **overrides):
        self.supervisor._active_run = "fixture"
        self.supervisor._health_config = replace(RepairConfig(), health_grace_seconds=10.0, health_timeout_seconds=60.0, **overrides)
        self.supervisor.bot_started(123, 1000.0)

    def health(self, *, pid=123, at=1001.0, beat=None, loaded=True):
        atomic_json(self.root / STARTUP_RELATIVE_PATH, {"pid": pid, "at": at, "plugin_loaded": loaded})
        path = self.root / "logs/bot_heartbeat"
        path.write_text("fixture", encoding="utf-8")
        value = self.now[0] if beat is None else beat
        os.utime(path, (value, value))

    def test_default_disabled_never_spawns_or_borrows_main_route(self):
        atomic_json(self.root / "config.json", {"ai": {"api_key": "main-only-key", "model": "main"}})
        with patch("catty_self_repair.supervisor.subprocess.Popen") as spawn:
            self.supervisor.poll_worker()
        spawn.assert_not_called()

    def test_placeholder_config_never_spawns(self):
        self.config(base_url="HOST", api_key="TOKEN", model="TARGET")
        with patch("catty_self_repair.supervisor.subprocess.Popen") as spawn:
            self.supervisor.poll_worker()
        spawn.assert_not_called()

    def test_spawns_standalone_worker_with_root_and_scrubbed_env(self):
        self.config()
        process = FakeProcess()
        with patch.dict(os.environ, {"CATTY_API_KEY": "main-secret", "TOKEN": "another-secret"}), patch(
            "catty_self_repair.supervisor.subprocess.Popen", return_value=process
        ) as spawn:
            self.supervisor.poll_worker()
            self.supervisor.poll_worker()
        self.assertEqual(spawn.call_count, 1)
        args, kwargs = spawn.call_args
        self.assertIn("--root", args[0])
        self.assertIn(str(self.root), args[0])
        self.assertNotIn("CATTY_API_KEY", kwargs["env"])
        self.assertNotIn("TOKEN", kwargs["env"])
        self.assertNotIn("fixture-only-key", " ".join(args[0]))

    def test_disable_and_invalid_config_stop_existing_worker(self):
        for content in (json.dumps({"self_repair": {"enabled": False}}), "broken"):
            with self.subTest(content=content):
                process = FakeProcess()
                self.supervisor.worker = process
                (self.root / "config.json").write_text(content, encoding="utf-8")
                self.supervisor.poll_worker()
                self.assertIsNone(self.supervisor.worker)
                self.assertTrue(process.signals)

    def test_route_change_stops_old_worker_before_respawn(self):
        self.config()
        old, new = FakeProcess(123), FakeProcess(124)
        with patch("catty_self_repair.supervisor.subprocess.Popen", side_effect=[old, new]):
            self.supervisor.poll_worker()
            self.config(model="new-fixture")
            self.supervisor.poll_worker()
        self.assertTrue(old.signals)
        self.assertIs(self.supervisor.worker, new)

    def test_worker_exit_has_restart_backoff(self):
        self.config()
        old, new = FakeProcess(123), FakeProcess(124)
        with patch("catty_self_repair.supervisor.subprocess.Popen", side_effect=[old, new]) as spawn:
            self.supervisor.poll_worker()
            old.returncode = 1
            self.supervisor.poll_worker()
            self.now[0] += 29
            self.supervisor.poll_worker()
            self.assertEqual(spawn.call_count, 1)
            self.now[0] += 1
            self.supervisor.poll_worker()
            self.assertEqual(spawn.call_count, 2)

    def test_ready_candidate_prevents_a_second_worker(self):
        self.ready()
        with patch("catty_self_repair.supervisor.subprocess.Popen") as spawn:
            self.supervisor.poll_worker()
        spawn.assert_not_called()

    def test_disabled_changed_and_preview_configs_reject_pending_apply(self):
        for update in ({"enabled": False}, {"auto_apply": False}, {"model": "changed"}):
            with self.subTest(update=update):
                self.ready()
                self.config(**update)
                self.assertFalse(self.supervisor.has_ready())
                self.assertFalse((state_dir(self.root) / "ready.json").exists())

    def test_ready_path_cannot_escape_run_or_use_absolute_path(self):
        for path in ("../elsewhere", "C:/secret/release.json", "data/self_repair/runs/other/release.json", "data/self_repair/runs/fixture/../fixture/release.json"):
            with self.subTest(path=path):
                self.ready(release_path=path)
                self.assertFalse(self.supervisor.has_ready())

    def test_apply_stops_worker_and_binds_health_config(self):
        config = self.config()
        self.ready(config)
        process = FakeProcess()
        self.supervisor.worker = process
        stub = workspace_stub()
        with patch.dict(sys.modules, {stub.__name__: stub}):
            self.assertTrue(self.supervisor.apply_pending())
        self.assertTrue(process.signals)
        self.assertTrue(self.supervisor.active)
        self.assertEqual(self.supervisor._health_config, config)
        self.assertFalse((state_dir(self.root) / "ready.json").exists())
        stub.apply_release.assert_called_once()

    def test_apply_failure_recovers_before_returning(self):
        self.ready()
        stub = workspace_stub(apply_release=Mock(side_effect=ValueError("fixture error")))
        with patch.dict(sys.modules, {stub.__name__: stub}):
            self.assertFalse(self.supervisor.apply_pending())
        stub.recover_release.assert_called_once_with(self.root)
        self.assertFalse(self.supervisor.active)

    def test_apply_recovery_conflict_blocks_startup(self):
        self.ready()
        stub = workspace_stub(
            apply_release=Mock(side_effect=ValueError("fixture error")),
            recover_release=Mock(return_value={"status": "conflict", "conflicts": ["source.py"]}),
        )
        with patch.dict(sys.modules, {stub.__name__: stub}), self.assertRaises(RecoveryBlocked):
            self.supervisor.apply_pending()

    def test_health_requires_correct_plugin_pid_fresh_marker_and_heartbeat(self):
        stub = workspace_stub()
        for values in ({"loaded": False}, {"pid": 777}, {"at": 900.0}, {"beat": 900.0}):
            with self.subTest(values=values):
                self.active()
                self.now[0] = 1061.0
                self.health(**values)
                with patch.dict(sys.modules, {stub.__name__: stub}):
                    self.assertEqual(self.supervisor.health_action(running=True), "startup_or_heartbeat_timeout")
        stub.confirm_release.assert_not_called()

    def test_windows_launcher_accepts_only_its_actual_direct_child(self):
        stub = workspace_stub()
        self.active()
        self.now[0] = 1002.0
        self.health(pid=777)
        with patch("catty_self_repair.supervisor.os.name", "nt"), patch(
            "catty_self_repair.supervisor._windows_parent_pid", return_value=321
        ) as parent, patch.dict(sys.modules, {stub.__name__: stub}):
            self.assertEqual(self.supervisor.health_action(running=True), "")
            self.assertIsNone(self.supervisor._healthy_since)
            parent.return_value = 123
            self.assertEqual(self.supervisor.health_action(running=True), "")
            self.assertEqual(self.supervisor._healthy_since, 1002.0)
            self.now[0] = 1012.0
            self.health(pid=777)
            self.assertEqual(self.supervisor.health_action(running=True), "")
        stub.confirm_release.assert_called_once_with(self.root)

    def test_success_requires_continuous_health_window(self):
        self.active()
        self.now[0] = 1002.0
        self.health()
        stub = workspace_stub()
        with patch.dict(sys.modules, {stub.__name__: stub}):
            self.assertEqual(self.supervisor.health_action(running=True), "")
            stub.confirm_release.assert_not_called()
            self.now[0] = 1012.0
            self.health()
            self.assertEqual(self.supervisor.health_action(running=True), "")
            self.assertFalse(self.supervisor.active)
        stub.confirm_release.assert_called_once_with(self.root)

    def test_stale_heartbeat_resets_probation(self):
        self.active()
        self.now[0] = 1002.0
        self.health()
        self.supervisor.health_action(running=True)
        self.now[0] = 1011.0
        self.health(beat=900.0)
        self.supervisor.health_action(running=True)
        self.assertIsNone(self.supervisor._healthy_since)

    def test_process_exit_requests_rollback_without_heartbeat(self):
        self.active()
        self.assertEqual(self.supervisor.health_action(running=False), "process_exit")

    def test_rollback_conflict_keeps_active_state(self):
        self.active()
        stub = workspace_stub(rollback_release=Mock(return_value={"status": "conflict"}))
        with patch.dict(sys.modules, {stub.__name__: stub}), self.assertRaises(RecoveryBlocked):
            self.supervisor.rollback("fixture")
        self.assertTrue(self.supervisor.active)

    def test_rollback_success_clears_active_state(self):
        self.active()
        stub = workspace_stub()
        with patch.dict(sys.modules, {stub.__name__: stub}):
            self.supervisor.rollback("fixture")
        self.assertFalse(self.supervisor.active)

    def test_start_recovers_incomplete_release_even_if_disabled(self):
        self.config(enabled=False)
        atomic_json(state_dir(self.root) / "active_release.json", {})
        stub = workspace_stub()
        with patch.dict(sys.modules, {stub.__name__: stub}):
            self.supervisor.start()
        stub.recover_release.assert_called_once_with(self.root)

    def test_two_supervisors_cannot_own_one_workspace(self):
        self.supervisor.start()
        other = RepairSupervisor(self.root)
        try:
            with self.assertRaises(RecoveryBlocked):
                other.start()
        finally:
            other.close()

    def test_worker_env_contains_no_ambient_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture", "HTTP_PROXY": "fixture", "CATTY_TOKEN": "fixture"}):
            env = _worker_env()
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("CATTY_TOKEN", env)


class WatcherWiringTests(unittest.TestCase):
    def test_start_bot_exports_fresh_startup_path(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watcher.subprocess, "Popen") as spawn:
            root = Path(directory)
            watcher._start_bot(root)
        self.assertEqual(spawn.call_args.kwargs["env"][watcher.STARTUP_ENV], str(root / watcher.STARTUP_RELATIVE_PATH))

    def test_refuses_publication_if_child_did_not_terminate(self):
        process = FakeProcess()
        with patch.object(watcher, "_stop_bot"), self.assertRaises(RuntimeError):
            watcher._stop_for_repair(process, 0)

    def test_application_happens_between_join_and_next_start(self):
        events = []
        first, second = FakeProcess(1), FakeProcess(2)
        second.returncode = 0
        children = iter((first, second))
        stage = [0]

        def start(root):
            process = next(children)
            events.append("start" + str(process.pid))
            return process

        def stop(process, timeout):
            events.append("join" + str(process.pid))
            process.returncode = 0

        def apply():
            stage[0] += 1
            if stage[0] == 2:
                self.assertIsNotNone(first.poll())
                events.append("apply")
                return True
            return False

        repair = SimpleNamespace(
            poll_worker=Mock(), apply_pending=apply, bot_started=Mock(),
            health_action=Mock(return_value=""), has_ready=Mock(return_value=True),
            active=False, close=Mock(), rollback=Mock(),
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(watcher, "_start_bot", side_effect=start), patch.object(
            watcher, "_stop_bot", side_effect=stop
        ), patch.object(watcher, "_load_settings", return_value={"restart_on_crash": False}), patch.object(
            watcher.time, "sleep"
        ):
            self.assertEqual(watcher._run_watcher(Path(directory), repair), 0)
        self.assertEqual(events, ["start1", "join1", "apply", "start2"])

    def test_startup_marker_reports_failed_plugin_without_importing_bot(self):
        tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_mark_supervisor_startup_ready")
        function.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.json"
            namespace = {"Path": Path, "os": os, "json": json, "time": SimpleNamespace(time=lambda: 1001.0), "_startup_ready_file": str(path), "_catty_plugin": None, "logger": Mock()}
            exec(compile(module, "bot-startup-fixture", "exec"), namespace)
            asyncio.run(namespace[function.name]())
            result = read_json(path)
            self.assertFalse(result["plugin_loaded"])
            self.assertEqual(result["pid"], os.getpid())
            namespace["_catty_plugin"] = object()
            asyncio.run(namespace[function.name]())
            self.assertTrue(read_json(path)["plugin_loaded"])

    def test_config_remains_excluded_from_code_watcher(self):
        self.assertNotIn("config.json", watcher.WATCH_FILES)
        self.assertNotIn("data", watcher.WATCH_DIRS)


class StorageTests(unittest.TestCase):
    def test_atomic_json_roundtrip_and_corrupt_state_is_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/state.json"
            atomic_json(path, {"text": "修复", "number": 1})
            self.assertEqual(read_json(path), {"text": "修复", "number": 1})
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_json(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_file_lock_is_exclusive_and_reusable_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.lock"
            first, second = FileLock(path), FileLock(path)
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.close()
                self.assertTrue(second.acquire())
            finally:
                first.close()
                second.close()
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()