from pathlib import Path
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock


_MODULE_PATH = Path(__file__).resolve().parents[1] / "catty_integrations.py"
_SPEC = importlib.util.spec_from_file_location("catty_integrations", _MODULE_PATH)
_integrations = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_integrations)


class IntegrationPathTests(unittest.TestCase):
    def test_project_path_accepts_relative_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            relative, resolved = _integrations._project_path("tools/ollama", "tools/ollama", base_dir, "install_dir")

        self.assertEqual(relative, str(Path("tools") / "ollama"))
        self.assertEqual(resolved, (base_dir / "tools" / "ollama").resolve())

    def test_project_path_rejects_parent_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            with self.assertRaises(ValueError):
                _integrations._project_path("..\\outside", "tools/ollama", base_dir, "install_dir")

    def test_project_path_rejects_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            with self.assertRaises(ValueError):
                _integrations._project_path(str((base_dir / "tools" / "ollama").resolve()), "tools/ollama", base_dir, "install_dir")

    def test_ollama_models_to_check_includes_local_critic_model(self) -> None:
        models = _integrations._ollama_models_to_check(
            {"local_critic": {"model": "qwen2.5:1.5b"}},
            {"model": "qwen3:0.6b"},
        )

        self.assertEqual(models, ["qwen3:0.6b", "qwen2.5:1.5b"])

    def test_ollama_models_to_check_appends_extra_models(self) -> None:
        models = _integrations._ollama_models_to_check(
            {"local_critic": {"model": "qwen2.5:1.5b"}},
            {"model": "qwen2.5:1.5b", "extra_models": ["qwen2.5:7b", "qwen3:4b"]},
        )

        self.assertEqual(models, ["qwen2.5:1.5b", "qwen2.5:7b", "qwen3:4b"])

    def test_ollama_models_to_check_dedupes_extra_models(self) -> None:
        models = _integrations._ollama_models_to_check(
            {"local_critic": {"model": "qwen2.5:1.5b"}},
            {"model": "qwen2.5:1.5b", "extra_models": ["qwen2.5:1.5b", "qwen2.5:7b"]},
        )

        self.assertEqual(models, ["qwen2.5:1.5b", "qwen2.5:7b"])

    def test_ollama_models_to_check_accepts_string_extra_models(self) -> None:
        models = _integrations._ollama_models_to_check(
            {},
            {"model": "qwen2.5:1.5b", "extra_models": "qwen2.5:7b"},
        )

        self.assertEqual(models, ["qwen2.5:1.5b", "qwen2.5:7b"])


    def test_qzone_bridge_disabled_does_not_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(_integrations, "_start_qzone_bridge") as start_bridge:
                _integrations.start_integrated_processes(
                    {"qzone": {"enabled": False}},
                    Path(directory),
                )

        start_bridge.assert_not_called()

    def test_qzone_bridge_healthy_skips_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(_integrations, "_qzone_bridge_is_healthy", return_value=True), mock.patch.object(
                _integrations.subprocess, "Popen"
            ) as popen:
                _integrations._start_qzone_bridge({"enabled": True}, Path(directory))

        popen.assert_not_called()

    def test_qzone_bridge_missing_entry_does_not_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(_integrations, "_qzone_bridge_is_healthy", return_value=False), mock.patch.object(
                _integrations.shutil, "which", return_value="node"
            ), mock.patch.object(_integrations.subprocess, "Popen") as popen:
                _integrations._start_qzone_bridge({"enabled": True}, Path(directory))

        popen.assert_not_called()

    def test_qzone_bridge_starts_node_with_log_and_no_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            workdir = config_dir / "tools" / "onebot-qzone"
            entry = workdir / "dist" / "main.js"
            entry.parent.mkdir(parents=True)
            entry.write_text("", encoding="utf-8")
            process = mock.Mock(pid=1234)
            with mock.patch.object(_integrations, "_qzone_bridge_is_healthy", return_value=False), mock.patch.object(
                _integrations.shutil, "which", return_value="C:/node/node.exe"
            ) as which, mock.patch.object(_integrations.subprocess, "Popen", return_value=process) as popen:
                _integrations._start_qzone_bridge(
                    {
                        "enabled": True,
                        "bridge_workdir": "tools/onebot-qzone",
                        "bridge_node_executable": "node",
                        "bridge_log_path": "logs/qzone_bridge.log",
                    },
                    config_dir,
                )

            which.assert_called_once_with("node")
            args, kwargs = popen.call_args
            try:
                self.assertEqual(args[0], ["C:/node/node.exe", str(entry.resolve())])
                self.assertEqual(kwargs["cwd"], str(workdir.resolve()))
                self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
                self.assertEqual(kwargs["env"], os.environ.copy())
                expected_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                self.assertEqual(kwargs["creationflags"], expected_flags)
                self.assertEqual(kwargs["stdout"].name, str((config_dir / "logs" / "qzone_bridge.log").resolve()))
            finally:
                kwargs["stdout"].close()

    def test_child_log_rotates_when_too_large(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "qzone_bridge.log"
            log_path.write_bytes(b"old")
            with mock.patch.object(_integrations, "_CHILD_LOG_MAX_BYTES", 2):
                _integrations._rotate_child_log(log_path)
                self.assertFalse(log_path.exists())
                self.assertEqual((Path(directory) / "qzone_bridge.log.1").read_bytes(), b"old")
                log_path.write_bytes(b"new")
                _integrations._rotate_child_log(log_path)
            self.assertEqual((Path(directory) / "qzone_bridge.log.1").read_bytes(), b"new")
            self.assertEqual((Path(directory) / "qzone_bridge.log.2").read_bytes(), b"old")
            small = Path(directory) / "small.log"
            small.write_bytes(b"x")
            _integrations._rotate_child_log(small)
            self.assertTrue(small.exists())

    def test_qzone_bridge_health_check_requires_ok_json(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = json.dumps({"ok": True}).encode()
        response.__enter__.return_value = response
        with mock.patch.object(_integrations, "urlopen", return_value=response) as urlopen:
            healthy = _integrations._qzone_bridge_is_healthy("http://127.0.0.1:5700/")

        self.assertTrue(healthy)
        urlopen.assert_called_once_with("http://127.0.0.1:5700/status", timeout=1.5)

    def test_qzone_bridge_second_start_reuses_first_after_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            workdir = config_dir / "tools" / "onebot-qzone"
            entry = workdir / "dist" / "main.js"
            entry.parent.mkdir(parents=True)
            entry.write_text("", encoding="utf-8")
            process = mock.Mock(pid=1234)
            process.poll.return_value = None
            health = mock.Mock(side_effect=[False, False, True, True])
            with mock.patch.object(_integrations, "_qzone_bridge_is_healthy", health), mock.patch.object(
                _integrations.shutil, "which", return_value="C:/node/node.exe"
            ), mock.patch.object(_integrations.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
                _integrations.time, "sleep"
            ) as sleep:
                _integrations._start_qzone_bridge({"enabled": True}, config_dir)
                _integrations._start_qzone_bridge({"enabled": True}, config_dir)

            try:
                popen.assert_called_once()
                sleep.assert_called_once_with(0.1)
            finally:
                popen.call_args.kwargs["stdout"].close()

    def test_qzone_bridge_probe_rejects_non_bridge_json(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"hello": "world"}).encode()
        response.__enter__.return_value = response
        with mock.patch.object(_integrations, "urlopen", return_value=response):
            self.assertIsNone(_integrations._qzone_bridge_probe("http://127.0.0.1:5700"))

    def test_stop_logged_out_bridge_only_touches_logged_out_bridge(self) -> None:
        with mock.patch.object(
            _integrations, "_qzone_bridge_probe", return_value={"ok": True, "cookie": {}}
        ), mock.patch.object(_integrations, "_stop_qzone_port_owners") as stop:
            self.assertFalse(_integrations.stop_logged_out_qzone_bridge("http://127.0.0.1:5700"))
        stop.assert_not_called()

        with mock.patch.object(
            _integrations, "_qzone_bridge_probe", return_value={"ok": False, "cookie": {}}
        ), mock.patch.object(_integrations, "_stop_qzone_port_owners", return_value=True) as stop:
            self.assertTrue(_integrations.stop_logged_out_qzone_bridge("http://127.0.0.1:5700"))
        stop.assert_called_once_with(5700)

    def test_qzone_bridge_start_reports_failure_when_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            workdir = config_dir / "tools" / "onebot-qzone"
            entry = workdir / "dist" / "main.js"
            entry.parent.mkdir(parents=True)
            entry.write_text("", encoding="utf-8")
            process = mock.Mock(pid=4321)
            process.poll.return_value = 1
            with mock.patch.object(
                _integrations, "_qzone_bridge_is_healthy", return_value=False
            ), mock.patch.object(_integrations.shutil, "which", return_value="C:/node/node.exe"), mock.patch.object(
                _integrations.subprocess, "Popen", return_value=process
            ) as popen:
                started = _integrations._start_qzone_bridge({"enabled": True}, config_dir)

            try:
                self.assertFalse(started)
            finally:
                popen.call_args.kwargs["stdout"].close()

if __name__ == "__main__":
    unittest.main()
