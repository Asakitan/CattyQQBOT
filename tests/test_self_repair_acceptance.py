"""Full owned-process acceptance: loopback model fixture -> watcher -> bot restart."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from catty_self_repair.budget import WeeklyBudget
from catty_self_repair.config import RepairConfig, state_dir
from catty_self_repair.storage import atomic_json
from catty_self_repair.supervisor import RecoveryBlocked, RepairSupervisor
from catty_self_repair.validation import _WindowsJob, _environment, _kill_tree

SOURCE = "src/catty_qq_ai/normalize.py"
OLD = "def normalize(value):\n    return int(value)\n"
NEW = "def normalize(value):\n    return 0 if value is None else int(value)\n"
KEY = "fixture-loopback-model-credential"
_BOT = r'''
import importlib.util, json, os, pathlib, signal, sys, threading, time
root = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fixture_normalize", root / "src/catty_qq_ai/normalize.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.normalize("3") == 3
try:
    fixed = module.normalize(None) == 0
except TypeError:
    fixed = False
crash = fixed and os.environ.get("CATTY_FIXTURE_CRASH_ON_PATCH") == "1"
(root / "data").mkdir(exist_ok=True)
with (root / "data/bot_pids.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"pid": os.getpid(), "parent_pid": os.getppid(), "fixed": fixed, "crash": crash}) + "\n")
if crash:
    raise SystemExit(23)
for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
    number = getattr(signal, name, None)
    if number is not None:
        signal.signal(number, lambda *args: sys.exit(0))
logs = root / "logs"
logs.mkdir(exist_ok=True)
if not fixed:
    with (logs / "bot_live.log").open("a", encoding="utf-8") as stream:
        stream.write("2026-10-29 12:00:00 | WARNING | catty_qq_ai.normalize:normalize:2 - None input fails\n")
startup = pathlib.Path(os.environ["CATTY_SELF_REPAIR_STARTUP_FILE"])
startup.write_text(json.dumps({"pid": os.getpid(), "at": time.time(), "plugin_loaded": True}), encoding="utf-8")
beat = pathlib.Path(os.environ["CATTY_HEARTBEAT_FILE"])
pause = threading.Event()
while True:
    beat.write_text(str(time.time()), encoding="utf-8")
    pause.wait(0.05)
'''
_TEST = r'''
import importlib.util, pathlib, unittest
root = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("normalization_fixture", root / "src/catty_qq_ai/normalize.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
class Fixture(unittest.TestCase):
    def test_existing_input(self):
        self.assertEqual(module.normalize("3"), 3)
    def test_incident_none_input(self):
        self.assertEqual(module.normalize(None), 0)
    def test_no_private_configuration_in_snapshot(self):
        self.assertFalse((root / "config.json").exists())
'''


class FullProcessAcceptanceTests(unittest.TestCase):
    def scenario(self, *, crash_on_patch: bool) -> None:
        with tempfile.TemporaryDirectory(prefix="catty-repair-acceptance-") as directory:
            root = Path(directory) / "repo"
            (root / "src/catty_qq_ai").mkdir(parents=True)
            (root / SOURCE).write_text(OLD, encoding="utf-8", newline="")
            (root / "src/catty_qq_ai/__init__.py").write_text("", encoding="utf-8")
            shutil.copytree(ROOT / "src/catty_self_repair", root / "src/catty_self_repair", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (root / "scripts").mkdir()
            for name in ("catty_self_repair.py", "catty_hot_reload.py"):
                shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
            (root / "tests").mkdir()
            (root / "tests/test_incident_fixture.py").write_text(_TEST, encoding="utf-8")
            (root / "bot.py").write_text(_BOT, encoding="utf-8")
            (root / "config.example.json").write_text("{}", encoding="utf-8")
            calls: list[dict] = []
            faults: list[str] = []

            class ModelFixture(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    try:
                        if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + KEY:
                            raise AssertionError("wrong fixture route/auth")
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= 131072:
                            raise AssertionError("fixture request size")
                        raw = self.rfile.read(size)
                        if KEY.encode("utf-8") in raw:
                            raise AssertionError("credential leaked into request body")
                        body = json.loads(raw)
                        calls.append(body)
                        if len(calls) == 1:
                            tools = [("read", {"path": SOURCE})]
                        elif len(calls) == 2:
                            receipt = json.loads(body["messages"][-1]["content"])
                            if receipt["sha256"] != hashlib.sha256(OLD.encode("utf-8")).hexdigest():
                                raise AssertionError("read receipt did not bind the fixture source")
                            tools = [
                                ("replace_text", {"path": SOURCE, "expected_sha256": receipt["sha256"],
                                                  "old": "return int(value)", "new": "return 0 if value is None else int(value)"}),
                                ("finish", {"outcome": "changed", "summary": "处理 None 输入，同时保留数字转换"}),
                            ]
                        else:
                            raise AssertionError("unexpected repeated model request")
                        data = {"choices": [{"finish_reason": "tool_calls", "message": {
                            "role": "assistant", "content": None,
                            "tool_calls": [{"id": f"fixture_{len(calls)}_{index}", "type": "function", "function": {
                                "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
                            }} for index, (name, arguments) in enumerate(tools)],
                        }}], "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}}
                        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    except Exception as exc:
                        faults.append(type(exc).__name__ + ": " + str(exc))
                        self.send_error(500, "fixture protocol failure")

            server = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
            server.daemon_threads = True
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
            thread.start()
            config = RepairConfig.from_mapping({
                "enabled": True, "auto_apply": True, "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "api_key": KEY, "model": "local-fixture", "max_input_tokens": 32768,
                "max_output_tokens": 512, "max_rounds": 4, "request_timeout_seconds": 3,
                "run_timeout_seconds": 45, "poll_seconds": 0.1, "debounce_seconds": 0,
                "cooldown_seconds": 100, "log_paths": ["logs/bot_live.log"],
                "allowed_paths": ["src/catty_qq_ai"], "test_modules": ["test_incident_fixture"],
                "test_timeout_seconds": 10, "validation_timeout_seconds": 25,
                "health_grace_seconds": 0, "health_timeout_seconds": 5,
            })
            atomic_json(root / "config.json", {"self_repair": asdict(config), "hot_reload": {
                "enabled": True, "poll_seconds": 0.2, "debounce_seconds": 0,
                "restart_on_crash": True, "restart_on_code_change": True, "stop_timeout_seconds": 1,
            }})
            log_path = Path(directory) / "watcher.log"
            process = None
            job = None
            receipt = None
            history = []
            try:
                env = _environment(Path(directory))
                if crash_on_patch:
                    env["CATTY_FIXTURE_CRASH_ON_PATCH"] = "1"
                # A stdin gate lets the parent attach the Windows job before any child is spawned.
                bootstrap = "import runpy,sys\np=sys.argv[1]\nif sys.stdin.readline() != 'go\\n': raise SystemExit(70)\nsys.argv=[p]\nrunpy.run_path(p,run_name='__main__')"
                with log_path.open("wb") as output:
                    kwargs = {"cwd": root, "env": env, "stdin": subprocess.PIPE, "stdout": output, "stderr": subprocess.STDOUT}
                    if os.name == "nt":
                        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                    else:
                        kwargs["start_new_session"] = True
                    process = subprocess.Popen([sys.executable, "-I", "-B", "-X", "utf8", "-c", bootstrap, str(root / "scripts/catty_hot_reload.py")], **kwargs)
                    if os.name == "nt":
                        job = _WindowsJob(process)
                    process.stdin.write(b"go\n")
                    process.stdin.flush()
                    process.stdin.close()
                    deadline = time.monotonic() + 60
                    expected = "rolled_back" if crash_on_patch else "confirmed"
                    while time.monotonic() < deadline:
                        for path in (state_dir(root) / "runs").glob("*/release_receipt.json"):
                            receipt = json.loads(path.read_text(encoding="utf-8"))
                        history_path = root / "data/bot_pids.jsonl"
                        if history_path.exists():
                            history = [json.loads(row) for row in history_path.read_text(encoding="utf-8").splitlines() if row]
                        if (receipt and receipt["phase"] == expected and len(history) >= (3 if crash_on_patch else 2)
                            and not (state_dir(root) / "active_release.json").exists()
                            and not (state_dir(root) / "ready.json").exists()):
                            break
                        if receipt and receipt["phase"] != expected:
                            self.fail("unexpected release phase: " + str(receipt.get("reason")) + "; history=" + json.dumps(history))
                        try:
                            process.wait(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            continue
                        self.fail("fixture watcher exited early: " + str(process.returncode) + "\n" + log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
                    self.assertIsNotNone(receipt, "no release receipt; " + log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
                    self.assertEqual(receipt["phase"], expected)
                    self.assertEqual(len(calls), 2, faults)
                    self.assertEqual(faults, [])
                    self.assertEqual((root / SOURCE).read_text(encoding="utf-8"), OLD if crash_on_patch else NEW)
                    self.assertFalse((state_dir(root) / "active_release.json").exists())
                    self.assertFalse((state_dir(root) / "ready.json").exists())
                    budget = WeeklyBudget(state_dir(root) / "budget.sqlite3").snapshot()
                    self.assertEqual((budget["used"], budget["pending"]), (280, 0))
                    self.assertGreaterEqual(len({row["pid"] for row in history}), 3 if crash_on_patch else 2)
                    if crash_on_patch:
                        self.assertTrue(any(row["crash"] for row in history))
                        self.assertFalse(history[-1]["fixed"])
                    else:
                        self.assertTrue(history[-1]["fixed"])
                    if process is not None:
                        _kill_tree(process, job)
                        process, job = None, None
            finally:
                if process is not None:
                    _kill_tree(process, job)
                server.shutdown()
                server.server_close()
                thread.join(3)
                for path in root.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_live_process_chain_applies_and_confirms_after_restart(self):
        self.scenario(crash_on_patch=False)

    def test_live_process_chain_rolls_back_a_crashing_upgrade(self):
        self.scenario(crash_on_patch=True)


class PublicationBoundaryTests(unittest.TestCase):
    def test_disable_during_worker_shutdown_cannot_apply_old_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RepairConfig(enabled=True, base_url="http://127.0.0.1:1/v1", model="fixture")
            atomic_json(root / "config.json", {"self_repair": asdict(config)})
            release = state_dir(root) / "runs/fixture/release.json"
            atomic_json(release, {})
            atomic_json(state_dir(root) / "ready.json", {"run_id": "fixture", "release_path": "data/self_repair/runs/fixture/release.json", "config_fingerprint": config.fingerprint()})
            supervisor = RepairSupervisor(root)
            with patch.object(supervisor, "_stop_worker", side_effect=lambda: atomic_json(root / "config.json", {"self_repair": {"enabled": False}})), patch("catty_self_repair.workspace.apply_release") as publish:
                self.assertFalse(supervisor.apply_pending())
            publish.assert_not_called()

    def test_missing_active_journal_is_not_reported_as_successful_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = RepairSupervisor(Path(directory))
            supervisor._active_run = "fixture"
            with patch("catty_self_repair.workspace.rollback_release", return_value={"status": "none"}), self.assertRaises(RecoveryBlocked):
                supervisor.rollback("fixture failure")
            self.assertTrue(supervisor.active)


if __name__ == "__main__":
    unittest.main()