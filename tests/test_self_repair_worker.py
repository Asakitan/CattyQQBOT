"""Worker/CLI tests use temporary config/state and mocked monitors/repair runs only."""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from catty_self_repair import worker
from catty_self_repair.agent import RepairCancelled
from catty_self_repair.config import RepairConfig, state_dir
from catty_self_repair.storage import FileLock, atomic_json, read_json


KEY = "fixture-worker-secret-827364"
OTHER_KEY = "fixture-main-secret-573829"


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="catty-repair-worker-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = RepairConfig.from_mapping({
            "enabled": True, "base_url": "https://fixture.invalid/v1", "api_key": KEY,
            "model": "fixture-model", "poll_seconds": 0.1,
        })
        self.configure()
        self.monitor = Mock()
        self.monitor.poll.return_value = [{"id": "fixture", "level": "ERROR", "source": "logs/fixture.log",
                                           "text": "fixture problem", "occurrences": 1,
                                           "first_seen": 1.0, "last_seen": 2.0}]
        self.addCleanup(patch.stopall)
        self.monitor_factory = patch.object(worker, "LogMonitor", return_value=self.monitor).start()
        self.repair = patch.object(worker, "run_repair", return_value={"status": "no_changes", "summary": "外因"}).start()
        self.instance = worker.RepairWorker(self.root)

    def configure(self, **changes):
        self.config = replace(self.config, **changes)
        (self.root / "config.json").write_text(json.dumps({
            "self_repair": asdict(self.config), "ai": {"api_key": OTHER_KEY},
        }), encoding="utf-8")

    def ready_result(self):
        run_id = "fixture-run"
        path = state_dir(self.root) / "runs" / run_id / "release.json"
        atomic_json(path, {"run_id": run_id, "config_fingerprint": self.config.fingerprint()})
        return {"status": "ready", "run_id": run_id, "release_path": str(path), "summary": "最小修复",
                "validation": {"ok": True}, "requests": [{"settled": True}]}

    def assert_no_ready(self):
        self.assertFalse((state_dir(self.root) / "ready.json").exists())

    def test_disabled_never_reads_logs_or_requests_model(self):
        self.configure(enabled=False)
        result = self.instance.tick()
        self.assertEqual(result["status"], "disabled")
        self.monitor_factory.assert_not_called()
        self.repair.assert_not_called()
        self.assert_no_ready()

    def test_incomplete_route_does_not_borrow_main_ai_credentials(self):
        for changes in ({"base_url": ""}, {"base_url": "https://fixture.invalid/v1", "model": "TARGET"},
                        {"model": "fixture-model", "api_key": "TOKEN"}):
            with self.subTest(changes=changes):
                self.configure(**changes)
                self.assertEqual(self.instance.tick()["status"], "unconfigured")
        self.monitor_factory.assert_not_called()
        self.repair.assert_not_called()

    def test_empty_model_uses_main_route_for_monitor_and_worker(self):
        document = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        document["self_repair"].update(base_url="HOST", api_key="TOKEN", model="")
        document["ai"] = {"base_url": "https://main.invalid/v1/responses", "model": "main-model",
                          "api_key": OTHER_KEY, "http_proxy": "socks5h://127.0.0.1:11080"}
        (self.root / "config.json").write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.instance.tick()["status"], "no_changes")
        config = self.repair.call_args.args[1]
        self.assertEqual(config.model, "main-model")
        self.assertEqual(config.api_key, OTHER_KEY)
        self.assertEqual(config.http_proxy, "socks5h://127.0.0.1:11080")
        self.assertNotIn(OTHER_KEY, json.dumps(worker.public_status(self.root)))

    def test_hot_reload_uses_new_config_and_never_keeps_old_enabled_copy(self):
        self.configure(enabled=False)
        self.instance.tick()
        self.configure(enabled=True)
        self.assertEqual(self.instance.tick()["status"], "no_changes")
        self.configure(enabled=False)
        self.assertEqual(self.instance.tick()["status"], "disabled")
        self.repair.assert_called_once()
        self.monitor.poll.assert_called_once()

    def test_malformed_config_fails_closed_without_echoing_raw_content(self):
        self.instance.tick()
        (self.root / "config.json").write_text("broken " + OTHER_KEY, encoding="utf-8")
        result = self.instance.tick()
        self.assertEqual(result["status"], "configuration_error")
        self.assertNotIn(OTHER_KEY, json.dumps(result))
        self.repair.assert_called_once()

    def test_ready_or_active_journal_pauses_before_monitor(self):
        for name in ("ready.json", "active_release.json"):
            path = state_dir(self.root) / name
            atomic_json(path, {"fixture": True})
            self.assertEqual(self.instance.tick()["status"], "paused")
            path.unlink()
        self.monitor_factory.assert_not_called()
        self.repair.assert_not_called()

    def test_global_file_lock_excludes_manual_and_automatic_runs(self):
        state = state_dir(self.root)
        atomic_json(state / "status.json", {"status": "existing"})
        with FileLock(state / "worker.lock"):
            self.assertEqual(self.instance.tick("upgrade")["status"], "busy")
            self.assertEqual(self.instance.run(once=True)["status"], "busy")
        self.assertEqual(read_json(state / "status.json"), {"status": "existing"})
        self.repair.assert_not_called()

    def test_auto_apply_false_retains_validated_candidate_only(self):
        self.configure(auto_apply=False)
        self.repair.return_value = self.ready_result()
        result = self.instance.tick()
        self.assertEqual(result["status"], "candidate")
        self.assertTrue((self.root / result["release_path"]).is_file())
        self.assert_no_ready()
        self.assertEqual(result["config_fingerprint"], self.config.fingerprint())

    def test_ready_is_fixed_root_relative_manifest_and_fingerprint(self):
        self.repair.return_value = self.ready_result()
        result = self.instance.tick()
        self.assertEqual(result["status"], "ready")
        ready = read_json(state_dir(self.root) / "ready.json")
        self.assertEqual(ready, {"run_id": "fixture-run",
                                 "release_path": "data/self_repair/runs/fixture-run/release.json",
                                 "config_fingerprint": self.config.fingerprint()})
        self.assertFalse(Path(ready["release_path"]).is_absolute())
        self.assertFalse((state_dir(self.root) / "active_release.json").exists())
        self.assertEqual(result["request_count"], 1)

    def test_config_change_before_publication_blocks_ready(self):
        def generate(*args, **kwargs):
            result = self.ready_result()
            self.configure(model="new-model")
            return result
        self.repair.side_effect = generate
        result = self.instance.tick()
        self.assertEqual(result["status"], "config_changed")
        self.assert_no_ready()
        self.assertTrue(Path(self.repair.call_args.args[0]).samefile(self.root))

    def test_disable_before_publication_blocks_ready(self):
        def generate(*args, **kwargs):
            result = self.ready_result()
            self.configure(enabled=False)
            return result
        self.repair.side_effect = generate
        self.assertEqual(self.instance.tick()["status"], "disabled")
        self.assert_no_ready()

    def test_active_journal_appearing_during_run_blocks_ready(self):
        def generate(*args, **kwargs):
            result = self.ready_result()
            atomic_json(state_dir(self.root) / "active_release.json", {"fixture": True})
            return result
        self.repair.side_effect = generate
        self.assertEqual(self.instance.tick()["status"], "paused")
        self.assert_no_ready()

    def test_release_outside_fixed_run_path_is_rejected(self):
        outside = self.root / "release.json"
        outside.write_text("{}", encoding="utf-8")
        self.repair.return_value = {"status": "ready", "run_id": "fixture-run", "release_path": str(outside)}
        self.assertEqual(self.instance.tick()["status"], "failed")
        self.assert_no_ready()

    def test_manual_request_uses_same_pipeline_and_redacts_secret(self):
        result = self.instance.tick("upgrade " + OTHER_KEY)
        self.assertEqual(result["status"], "no_changes")
        self.monitor_factory.assert_not_called()
        root, config, incidents = self.repair.call_args.args
        self.assertTrue(root.samefile(self.root))
        self.assertEqual(config.fingerprint(), self.config.fingerprint())
        self.assertEqual(incidents[0]["source"], "manual")
        self.assertNotIn(OTHER_KEY, incidents[0]["text"])
        self.configure(enabled=False)
        self.assertEqual(self.instance.tick("upgrade")["status"], "disabled")
        self.repair.assert_called_once()

    def test_manual_request_is_not_silently_truncated(self):
        self.configure(max_incident_chars=256)
        self.assertEqual(self.instance.tick("猫" * 100)["status"], "invalid_request")
        self.assertEqual(self.instance.tick(" ")["status"], "invalid_request")
        self.repair.assert_not_called()

    def test_no_incidents_waits_without_a_request(self):
        self.monitor.poll.return_value = []
        self.assertEqual(self.instance.tick()["status"], "idle")
        self.repair.assert_not_called()

    def test_budget_exhaustion_and_validation_failure_never_stage(self):
        for status in ("budget_exhausted", "validation_failed", "failed"):
            self.repair.return_value = {"status": status, "summary": "fixture", "validation": {"ok": False}}
            self.assertEqual(self.instance.tick()["status"], status)
            self.assert_no_ready()

    def test_status_is_read_only_redacted_and_does_not_expose_config(self):
        atomic_json(state_dir(self.root) / "status.json", {
            "status": "failed", "summary": KEY + " " + OTHER_KEY,
            "api_key": KEY, "base_url": "https://fixture.invalid/v1", "extra_headers": {"secret": OTHER_KEY},
        })
        output = worker.public_status(self.root)
        encoded = json.dumps(output)
        self.assertNotIn(KEY, encoded)
        self.assertNotIn(OTHER_KEY, encoded)
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("base_url", encoded)
        self.assertTrue(output["enabled"])
        self.repair.assert_not_called()
        self.monitor_factory.assert_not_called()

    def test_status_file_size_is_bounded(self):
        path = state_dir(self.root) / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (worker.MAX_STATUS_BYTES + 1))
        self.assertEqual(worker.public_status(self.root)["last"]["status"], "unavailable")
        self.repair.assert_not_called()

    def test_cancellation_signal_unwinds_active_run_and_releases_lock(self):
        def cancel(*args, **kwargs):
            self.instance._signal(signal.SIGTERM, None)
        self.repair.side_effect = cancel
        result = self.instance.tick()
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(self.instance.stop.is_set())
        self.assertFalse(self.instance._in_repair)
        self.assert_no_ready()
        with FileLock(state_dir(self.root) / "worker.lock"):
            pass

    def test_cancellation_after_validation_keeps_candidate_without_ready(self):
        def finish_and_cancel(*args, **kwargs):
            result = self.ready_result()
            self.instance.stop.set()
            return result
        self.repair.side_effect = finish_and_cancel
        self.assertEqual(self.instance.tick()["status"], "cancelled")
        self.assert_no_ready()

    def test_default_loop_stops_and_restores_signal_handlers(self):
        def stop(*args, **kwargs):
            self.instance.stop.set()
            return {"status": "no_changes", "summary": "fixture"}
        self.repair.side_effect = stop
        with patch.object(worker.signal, "signal") as register, patch.object(worker.signal, "getsignal", return_value=signal.SIG_DFL):
            result = self.instance.run()
        self.assertEqual(result["status"], "no_changes")
        expected = [getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name)]
        self.assertEqual(register.call_count, 2 * len(expected))
        self.repair.assert_called_once()
        with FileLock(state_dir(self.root) / "worker.lock"):
            pass

    def test_unexpected_exception_is_redacted_and_lock_is_released(self):
        self.repair.side_effect = RuntimeError(KEY)
        result = self.instance.tick()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(KEY, json.dumps(result))
        with FileLock(state_dir(self.root) / "worker.lock"):
            pass

    def test_cli_status_never_constructs_a_running_worker(self):
        output = io.StringIO()
        with patch.object(worker, "RepairWorker") as constructor, redirect_stdout(output):
            code = worker.main(["--root", str(self.root), "--status"])
        self.assertEqual(code, 0)
        constructor.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "status")
        self.assertNotIn(KEY, output.getvalue())

    def test_cli_once_and_request_arguments_and_exit_codes(self):
        for arguments, expected_once, expected_request in ((["--once"], True, None),
                                                            (["--request", "upgrade fixture"], False, "upgrade fixture")):
            instance = Mock()
            instance.run.return_value = {"status": "budget_exhausted", "summary": "fixture"}
            with patch.object(worker, "RepairWorker", return_value=instance), redirect_stdout(io.StringIO()):
                code = worker.main(["--root", str(self.root), *arguments])
            self.assertEqual(code, 3)
            instance.run.assert_called_once_with(once=expected_once, request=expected_request)


if __name__ == "__main__":
    unittest.main()
