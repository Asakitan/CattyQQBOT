"""Offline synthetic repositories only; never open the operator's config.json."""
from __future__ import annotations

import ast
import copy
from dataclasses import replace
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from catty_self_repair.config import RepairConfig, state_dir
from catty_self_repair import workspace as ws
from catty_self_repair import validation as val


_IMPORT = """\
from pathlib import Path
import importlib.util
import json
import os
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('sample', ROOT / 'src/catty_qq_ai/sample.py')
sample = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sample)
"""
_TESTS = _IMPORT + """\
class FixtureTests(unittest.TestCase):
    def test_value(self):
        self.assertGreaterEqual(sample.VALUE, 1)
    def test_live_config_cannot_be_opened(self):
        with self.assertRaises(PermissionError):
            (ROOT.parents[4] / 'config.json').read_text()
    def test_frozen_layout_and_environment(self):
        self.assertEqual(json.loads((ROOT / 'config.example.json').read_text())['fixture'], 'readonly')
        self.assertTrue((ROOT / 'scripts/frozen_runner.py').is_file())
        self.assertTrue((ROOT / 'src/catty_self_repair/marker.py').is_file())
        self.assertFalse((ROOT / 'config.json').exists())
        self.assertFalse((ROOT / 'data').exists())
        for name in ('OPENAI_API_KEY', 'REPAIR_SECRET_SENTINEL', 'CATTY_CONFIG_PATH', 'PYTHONPATH', 'HTTP_PROXY'):
            self.assertNotIn(name, os.environ)
        self.assertNotEqual(Path(tempfile.gettempdir()), ROOT)
        self.assertEqual(Path(tempfile.gettempdir()), Path(tempfile.gettempdir()).resolve())
"""


class Crash(BaseException):
    """Simulate interruption after a durable filesystem event."""


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="repair-workspace-tests-")
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.serial = 0
        self.config = RepairConfig(
            enabled=True, allowed_paths=("src/catty_qq_ai",), test_modules=("test_fixture",),
            max_files=4, max_patch_bytes=4096, test_timeout_seconds=10,
            validation_timeout_seconds=45,
        )
        self.put("src/catty_qq_ai/__init__.py", "")
        self.put("src/catty_qq_ai/sample.py", "VALUE = 1\n")
        self.put("src/catty_qq_ai/untouched.py", "UNCHANGED = 1\n")
        self.put("src/catty_qq_ai/persona_prompts.py", "TEXT = 'frozen persona fixture'\n")
        self.put("src/catty_qq_ai/personas/role.py", "TEXT = 'frozen character fixture'\n")
        self.put("src/catty_self_repair/marker.py", "CONTROLLER = True\n")
        self.put("scripts/frozen_runner.py", "RUNNER = True\n")
        self.put("tests/test_fixture.py", _TESTS)
        self.put("tests/helper.py", "HELPER = True\n")
        self.put("config.example.json", '{"fixture":"readonly","ai":{"api_key":""}}\n')
        self.put("bot.py", "BOT = True\n")
        self.put("config.json", '{"token":"SYNTHETIC_SECRET_DO_NOT_COPY"}\n')
        self.put(".env", "TOKEN=SYNTHETIC_SECRET_DO_NOT_COPY\n")
        self.put("data/private.json", '{"secret":"SYNTHETIC_SECRET_DO_NOT_COPY"}\n')
        self.put("src/catty_qq_ai/data/private.py", "SECRET = 'SYNTHETIC_SECRET_DO_NOT_COPY'\n")
        self.put("src/catty_qq_ai/credentials.py", "SECRET = 'SYNTHETIC_SECRET_DO_NOT_COPY'\n")

    def tearDown(self) -> None:
        # Readonly copies are intentional. Only our temporary fixture is made
        # writable for Windows TemporaryDirectory cleanup.
        for path in Path(self.temporary.name).rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)
        self.temporary.cleanup()

    def put(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(0o600)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def snapshot(self, *, test_source: str | None = None, **settings) -> ws.Workspace:
        if test_source is not None:
            self.put("tests/test_fixture.py", test_source)
        self.serial += 1
        config = replace(self.config, **settings)
        return ws.create_snapshot(self.root, state_dir(self.root) / "runs" / f"run-{self.serial}", config)

    def change(self, workspace: ws.Workspace) -> None:
        workspace.replace_text("src/catty_qq_ai/sample.py", "VALUE = 1", "VALUE = 2")

    def validate_ok(self, workspace: ws.Workspace) -> dict:
        result = workspace.validate()
        self.assertTrue(result["ok"], json.dumps(result, ensure_ascii=False, indent=2))
        return result

    def ready(self, *, new_file: bool = False) -> tuple[ws.Workspace, Path]:
        workspace = self.snapshot()
        self.change(workspace)
        if new_file:
            workspace.write_file("src/catty_qq_ai/new_module.py", "NEW = 1\n")
        self.validate_ok(workspace)
        return workspace, workspace.prepare_release("synthetic fixture fix")

    def test_snapshot_whitelist_and_frozen_support(self) -> None:
        workspace = self.snapshot()
        for tree in (workspace.baseline, workspace.candidate):
            for forbidden in ("config.json", ".env", "data/private.json",
                              "src/catty_qq_ai/data/private.py", "src/catty_qq_ai/credentials.py"):
                self.assertFalse((tree / forbidden).exists(), forbidden)
            for frozen in ("tests/test_fixture.py", "tests/helper.py", "scripts/frozen_runner.py",
                           "src/catty_self_repair/marker.py", "config.example.json"):
                self.assertTrue((tree / frozen).is_file(), frozen)
                self.assertFalse((tree / frozen).stat().st_mode & stat.S_IWUSR)
        self.assertNotIn("src/catty_qq_ai/persona_prompts.py", workspace.list_files())
        self.assertNotIn("src/catty_self_repair/marker.py", workspace.list_files())
        for path in workspace.candidate.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SYNTHETIC_SECRET_DO_NOT_COPY", path.read_bytes())

    def test_example_with_real_shaped_secret_is_rejected(self) -> None:
        self.put("config.example.json", '{"ai":{"api_key":"not-a-placeholder-secret"}}')
        with self.assertRaises(ws.WorkspaceError):
            self.snapshot()

    def test_ollama_example_placeholder_is_not_a_real_secret(self) -> None:
        self.put("config.example.json", '{"fixture":"readonly","cpu_engine":{"l4_ollama_api_key":"ollama"}}')
        workspace = self.snapshot()
        self.assertTrue((workspace.baseline / "config.example.json").is_file())

    def test_model_paths_fail_closed(self) -> None:
        workspace = self.snapshot()
        paths = (
            "../config.py", "/absolute.py", "C:/escape.py", "src\\catty_qq_ai\\sample.py",
            "src//catty_qq_ai/sample.py", "src/catty_qq_ai/../sample.py",
            "src/catty_qq_ai/sample.py:stream", "src/catty_qq_ai/NUL.py",
            "src/catty_qq_ai/COM1.py", "src/catty_qq_ai/LPT9.py",
            "src/catty_qq_ai/trailing. /test.py", "src/catty_qq_ai/x.py.",
            "tests/test_fixture.py", "scripts/frozen_runner.py", "config.example.json",
            "src/catty_self_repair/marker.py", "src/catty_qq_ai/persona_prompts.py",
            "src/catty_qq_ai/personas/role.py", "src/catty_qq_ai/character_card.py",
            "src/catty_qq_ai/tests/test_hidden.py", "src/catty_qq_ai/scripts/hook.py",
            "src/catty_qq_ai/test_hidden.py", "src/catty_qq_ai/catty_self_repair/controller.py",
        )
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                workspace.write_file(path, "VALUE = 2\n")

    def test_read_search_unique_replace_and_create_only(self) -> None:
        workspace = self.snapshot()
        self.assertEqual(workspace.read_file("src/catty_qq_ai/sample.py", 1, 1), "VALUE = 1\n")
        self.assertEqual(workspace.search("VALUE")[0]["path"], "src/catty_qq_ai/sample.py")
        self.assertEqual(workspace.search("frozen persona fixture"), [])
        with self.assertRaises(ws.WorkspaceError):
            workspace.replace_text("src/catty_qq_ai/sample.py", " ", "_")
        with self.assertRaises(ws.WorkspaceError):
            workspace.write_file("src/catty_qq_ai/sample.py", "VALUE = 2\n")
        self.change(workspace)
        workspace.write_file("src/catty_qq_ai/new_module.py", "NEW = True\n")
        self.assertEqual(len(workspace.changes()), 2)
        self.assertIsNone(next(row for row in workspace.changes() if row["path"].endswith("new_module.py"))["before_sha256"])
        self.assertEqual((self.root / "src/catty_qq_ai/sample.py").read_text(), "VALUE = 1\n")

    def test_reopen_and_configuration_binding(self) -> None:
        workspace = self.snapshot()
        self.change(workspace)
        reopened = ws.Workspace(self.root, workspace.run_dir, workspace.config)
        self.assertEqual(reopened.changes(), workspace.changes())
        with self.assertRaises(ws.WorkspaceError):
            ws.Workspace(self.root, workspace.run_dir, replace(workspace.config, max_files=3))

    def test_maxfiles_and_new_file_byte_budget(self) -> None:
        workspace = self.snapshot(max_files=1, max_patch_bytes=128)
        self.change(workspace)
        with self.assertRaises(ws.WorkspaceError):
            workspace.write_file("src/catty_qq_ai/new_module.py", "NEW = True\n")
        with self.assertRaises(ws.WorkspaceError):
            workspace.replace_text("src/catty_qq_ai/sample.py", "VALUE = 2", "TEXT = " + repr("x" * 200))
        self.assertEqual(workspace.read_file("src/catty_qq_ai/sample.py"), "VALUE = 2\n")

    def test_small_edit_in_large_source_uses_diff_budget(self) -> None:
        self.put("src/catty_qq_ai/large.py", "VALUE = 1\n" + "".join(f"# line {i} " + "x" * 80 + "\n" for i in range(3000)))
        workspace = self.snapshot(max_patch_bytes=128)
        workspace.replace_text("src/catty_qq_ai/large.py", "VALUE = 1", "VALUE = 2")
        self.assertEqual(len(workspace.changes()), 1)
        self.assertLess(ws._patch_size(b"VALUE = 1\n", b"VALUE = 2\n"), 128)

    def test_case_collision_is_rejected(self) -> None:
        workspace = self.snapshot()
        with self.assertRaises(ws.WorkspaceError):
            workspace.write_file("src/catty_qq_ai/Sample.py", "VALUE = 2\n")

    def test_hardlinked_source_is_rejected(self) -> None:
        try:
            os.link(self.root / "src/catty_qq_ai/sample.py", self.root / "src/catty_qq_ai/linked.py")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        with self.assertRaises(ValueError):
            self.snapshot()

    def test_candidate_symlink_is_rejected(self) -> None:
        workspace = self.snapshot()
        link = workspace.candidate / "src/catty_qq_ai/linked.py"
        try:
            link.symlink_to(self.root / "src/catty_qq_ai/sample.py")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(str(exc))
        with self.assertRaises(ValueError):
            workspace.list_files()

    def test_reparse_attribute_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ws._regular(SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_file_attributes=0x400))

    def test_no_tests_and_zero_tests_never_release(self) -> None:
        with self.assertRaises(ValueError):
            self.snapshot(test_modules=("test_missing",))
        with self.assertRaises(ValueError):
            self.snapshot(test_modules=())
        workspace = self.snapshot(test_source="import unittest\n")
        self.change(workspace)
        self.assertFalse(workspace.validate()["ok"])
        with self.assertRaises(ws.WorkspaceError):
            workspace.prepare_release("no tests")

    def test_environment_is_scrubbed_in_real_subprocesses(self) -> None:
        workspace = self.snapshot()
        self.change(workspace)
        with patch.dict(os.environ, {"REPAIR_SECRET_SENTINEL": "TOKEN", "OPENAI_API_KEY": "TOKEN",
                                     "CATTY_CONFIG_PATH": "secret.json", "HTTP_PROXY": "http://HOST",
                                     "PYTHONPATH": str(self.root)}):
            report = self.validate_ok(workspace)
        self.assertFalse(report["is_os_sandbox"])
        self.assertEqual(len(report["integrity"]), 4)
        self.assertEqual(report["baseline"][0]["discovered"], report["candidate"][0]["discovered"])

    def test_existing_failure_is_reported_and_not_increased(self) -> None:
        source = _TESTS + "\nclass ExistingFailure(unittest.TestCase):\n    def test_red(self):\n        self.fail('existing red fixture')\n"
        workspace = self.snapshot(test_source=source)
        self.change(workspace)
        report = self.validate_ok(workspace)
        self.assertEqual(len(report["comparison"]["baseline_red"]), 1)
        self.assertEqual(len(report["comparison"]["candidate_red"]), 1)
        self.assertEqual(report["comparison"]["baseline_red"][0]["outcome"], "failure")
        self.assertEqual(workspace.prepare_release("keep old red").name, "release.json")

    def test_escaped_snapshot_and_temp_errors_keep_their_identity(self) -> None:
        source = _IMPORT + """\
class Case(unittest.TestCase):
    def test_missing_local_config(self):
        (ROOT / 'config.json').read_text()
    def test_temp_filename_repr(self):
        raise RuntimeError(repr(str(Path(tempfile.gettempdir()) / 'fixture.json')))
"""
        workspace = self.snapshot(test_source=source)
        self.change(workspace)
        report = self.validate_ok(workspace)
        before = report["comparison"]["baseline_red"]
        after = report["comparison"]["candidate_red"]
        self.assertEqual(len(before), 2)
        self.assertEqual([row["error_key"] for row in before], [row["error_key"] for row in after])
        self.assertIn("<SNAPSHOT>", before[0]["detail"])
        self.assertIn("config.json", before[0]["detail"])
        self.assertIn("<TEMP>", before[1]["detail"])
        self.assertFalse((workspace.candidate / "config.json").exists())

    def test_different_missing_filenames_remain_a_regression(self) -> None:
        source = _IMPORT + """\
class Case(unittest.TestCase):
    def test_missing(self):
        name = 'first.json' if sample.VALUE == 1 else 'second.json'
        (ROOT / name).read_text()
"""
        workspace = self.snapshot(test_source=source)
        self.change(workspace)
        report = workspace.validate()
        self.assertFalse(report["ok"])
        self.assertIn("first.json", report["comparison"]["baseline_red"][0]["detail"])
        self.assertIn("second.json", report["comparison"]["candidate_red"][0]["detail"])
        self.assertNotEqual(report["comparison"]["baseline_red"][0]["error_key"],
                            report["comparison"]["candidate_red"][0]["error_key"])

    def test_new_failure_blocks_publication(self) -> None:
        workspace = self.snapshot(test_source=_IMPORT + "class Case(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(sample.VALUE, 1)\n")
        self.change(workspace)
        self.assertFalse(workspace.validate()["ok"])
        with self.assertRaises(ws.WorkspaceError):
            workspace.prepare_release("bad fix")

    def test_new_skip_and_same_count_replaced_id_are_rejected(self) -> None:
        source = _IMPORT + """\
class Case(unittest.TestCase):
    def test_value(self):
        if sample.VALUE == 2:
            self.skipTest('new skip')
        self.assertEqual(sample.VALUE, 1)
"""
        workspace = self.snapshot(test_source=source)
        self.change(workspace)
        self.assertFalse(workspace.validate()["ok"])
        source = _IMPORT + """\
class Case(unittest.TestCase):
    if sample.VALUE == 1:
        def test_before(self):
            self.assertTrue(True)
    else:
        def test_after(self):
            self.assertTrue(True)
"""
        workspace = self.snapshot(test_source=source)
        self.change(workspace)
        report = workspace.validate()
        self.assertEqual(report["baseline"][0]["tests_run"], report["candidate"][0]["tests_run"])
        self.assertFalse(report["ok"])

    def test_frozen_test_mutation_fails_before_running(self) -> None:
        workspace = self.snapshot()
        path = workspace.candidate / "tests/test_fixture.py"
        path.chmod(0o600)
        path.write_text("import unittest\n", encoding="utf-8")
        with patch.object(val, "_run_module") as runner:
            report = workspace.validate()
        self.assertFalse(report["ok"])
        runner.assert_not_called()

    def test_post_run_whole_source_integrity_is_checked(self) -> None:
        workspace = self.snapshot()
        self.change(workspace)
        original = val._run_module

        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            (workspace.candidate / "src/catty_qq_ai/untouched.py").write_text("UNCHANGED = 9\n", encoding="utf-8")
            return result

        with patch.object(val, "_run_module", side_effect=mutate):
            report = workspace.validate()
        self.assertFalse(report["ok"])
        self.assertEqual((workspace.baseline / "src/catty_qq_ai/untouched.py").read_text(), "UNCHANGED = 1\n")

    def test_validation_timeout_reaps_children(self) -> None:
        source = _IMPORT + "class Case(unittest.TestCase):\n    def test_wait(self):\n        while sample.VALUE == 2:\n            pass\n"
        workspace = self.snapshot(test_source=source, test_timeout_seconds=0.3, validation_timeout_seconds=8)
        self.change(workspace)
        children = []
        original = val.subprocess.Popen

        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child

        with patch.object(val.subprocess, "Popen", side_effect=spawn):
            report = workspace.validate()
        self.assertFalse(report["ok"])
        self.assertTrue(any(row["status"] == "timeout" for row in report["baseline"] + report["candidate"]))
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_absolute_deadline_does_not_launch_processes(self) -> None:
        workspace = self.snapshot()
        with patch.object(val, "_run_module") as runner:
            self.assertFalse(workspace.validate(deadline=time.monotonic() - 1)["ok"])
        runner.assert_not_called()

    def test_baseexception_cancellation_reaps_owned_process(self) -> None:
        workspace = self.snapshot()
        children = []
        original = val.subprocess.Popen

        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            wait = child.wait
            calls = 0

            def interrupted(*wait_args, **wait_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise Crash("cancel validation")
                return wait(*wait_args, **wait_kwargs)

            child.wait = interrupted
            return child

        with patch.object(val.subprocess, "Popen", side_effect=spawn), self.assertRaises(Crash):
            workspace.validate()
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))
        with self.assertRaises(ws.WorkspaceError):
            workspace.prepare_release("cancelled")

    def test_prepare_requires_validation_and_whole_candidate_binding(self) -> None:
        workspace = self.snapshot()
        self.change(workspace)
        with self.assertRaises(ws.WorkspaceError):
            workspace.prepare_release("unverified")
        self.validate_ok(workspace)
        (workspace.candidate / "src/catty_qq_ai/untouched.py").write_text("UNCHANGED = 8\n", encoding="utf-8")
        with self.assertRaises(ws.WorkspaceError):
            workspace.prepare_release("stale validation")

    def test_validation_report_tampering_is_rejected(self) -> None:
        workspace = self.snapshot()
        self.change(workspace)
        self.validate_ok(workspace)
        (workspace.run_dir / "validation.json").write_text('{"ok":true}', encoding="utf-8")
        with self.assertRaises(ws.WorkspaceError):
            workspace.prepare_release("tampered result")

    def test_apply_confirm_and_recovery_are_idempotent(self) -> None:
        workspace, release = self.ready()
        self.assertEqual(release, workspace.run_dir / "release.json")
        applied = ws.apply_release(self.root, release, workspace.config)
        self.assertEqual(applied, {"status": "applied", "run_id": workspace.run_dir.name})
        self.assertEqual(ws.apply_release(self.root, release, workspace.config), applied)
        self.assertEqual(ws.confirm_release(self.root)["status"], "confirmed")
        self.assertFalse((state_dir(self.root) / "active_release.json").exists())
        self.assertEqual(ws.recover_release(self.root)["status"], "none")
        self.assertEqual(ws.confirm_release(self.root)["status"], "none")
        self.assertEqual((self.root / "src/catty_qq_ai/sample.py").read_text(), "VALUE = 2\n")

    def test_full_live_preimage_includes_unchanged_sources_and_tests(self) -> None:
        workspace, release = self.ready()
        self.put("src/catty_qq_ai/untouched.py", "UNCHANGED = 7\n")
        with self.assertRaises(ws.WorkspaceError):
            ws.apply_release(self.root, release, workspace.config)
        self.assertEqual((self.root / "src/catty_qq_ai/sample.py").read_text(), "VALUE = 1\n")
        self.put("src/catty_qq_ai/untouched.py", "UNCHANGED = 1\n")
        self.put("tests/helper.py", "HELPER = False\n")
        with self.assertRaises(ws.WorkspaceError):
            ws.apply_release(self.root, release, workspace.config)

    def test_full_candidate_and_payload_rechecked_at_apply(self) -> None:
        workspace, release = self.ready()
        target = workspace.run_dir / "release/payload/src/catty_qq_ai/sample.py"
        target.chmod(0o600)
        target.write_text("VALUE = 99\n", encoding="utf-8")
        with self.assertRaises(ws.WorkspaceError):
            ws.apply_release(self.root, release, workspace.config)
        self.assertFalse((state_dir(self.root) / "active_release.json").exists())

    def test_rollback_restores_old_and_removes_created_source(self) -> None:
        workspace, release = self.ready(new_file=True)
        ws.apply_release(self.root, release, workspace.config)
        self.assertEqual(ws.rollback_release(self.root, "health failure")["status"], "rolled_back")
        self.assertEqual((self.root / "src/catty_qq_ai/sample.py").read_text(), "VALUE = 1\n")
        self.assertFalse((self.root / "src/catty_qq_ai/new_module.py").exists())
        self.assertEqual(ws.rollback_release(self.root, "again")["status"], "none")

    def test_late_operator_edit_is_preserved_and_journal_retained(self) -> None:
        workspace, release = self.ready()
        ws.apply_release(self.root, release, workspace.config)
        self.put("src/catty_qq_ai/sample.py", "VALUE = 99\n")
        result = ws.rollback_release(self.root, "health failure")
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["conflicts"])
        self.assertTrue((state_dir(self.root) / "active_release.json").exists())
        self.assertEqual((self.root / "src/catty_qq_ai/sample.py").read_text(), "VALUE = 99\n")
        self.put("src/catty_qq_ai/sample.py", "VALUE = 2\n")
        self.assertEqual(ws.recover_release(self.root)["status"], "rolled_back")

    def test_operator_file_recreated_during_install_is_not_overwritten(self) -> None:
        workspace, release = self.ready()
        live = self.root / "src/catty_qq_ai/sample.py"
        original = ws._move

        def race(source, destination, **kwargs):
            if destination == live and "/transaction/staged/" in source.as_posix():
                live.write_text("VALUE = 99\n", encoding="utf-8")
            return original(source, destination, **kwargs)

        with patch.object(ws, "_move", side_effect=race):
            result = ws.apply_release(self.root, release, workspace.config)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(live.read_text(), "VALUE = 99\n")

    def test_operator_edit_during_preimage_move_is_preserved(self) -> None:
        workspace, release = self.ready()
        live = self.root / "src/catty_qq_ai/sample.py"
        original = ws._move

        def race(source, destination, **kwargs):
            if source == live and "/transaction/held/" in destination.as_posix():
                live.write_text("VALUE = 99\n", encoding="utf-8")
            return original(source, destination, **kwargs)

        with patch.object(ws, "_move", side_effect=race):
            result = ws.apply_release(self.root, release, workspace.config)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(live.read_text(), "VALUE = 99\n")

    def test_normal_install_failure_rolls_back(self) -> None:
        workspace, release = self.ready()
        original = ws._move

        def fail_install(source, destination, **kwargs):
            if "/transaction/staged/" in source.as_posix() and not kwargs.get("replace"):
                raise OSError("synthetic installation failure")
            return original(source, destination, **kwargs)

        with patch.object(ws, "_move", side_effect=fail_install):
            result = ws.apply_release(self.root, release, workspace.config)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual((self.root / "src/catty_qq_ai/sample.py").read_text(), "VALUE = 1\n")

    def test_crash_after_original_move_recovers_without_candidate(self) -> None:
        workspace, release = self.ready()
        live = self.root / "src/catty_qq_ai/sample.py"
        original = ws._move

        def crash_after_move(source, destination, **kwargs):
            result = original(source, destination, **kwargs)
            if source == live and "/transaction/held/" in destination.as_posix():
                raise Crash("after original move")
            return result

        with patch.object(ws, "_move", side_effect=crash_after_move), self.assertRaises(Crash):
            ws.apply_release(self.root, release, workspace.config)
        self.assertFalse(live.exists())
        (workspace.candidate / "src/catty_qq_ai/sample.py").write_text("corrupted candidate", encoding="utf-8")
        self.assertEqual(ws.recover_release(self.root)["status"], "rolled_back")
        self.assertEqual(live.read_text(), "VALUE = 1\n")
        self.assertEqual(ws.recover_release(self.root)["status"], "none")

    def test_crash_during_rollback_is_idempotently_recovered(self) -> None:
        workspace, release = self.ready()
        ws.apply_release(self.root, release, workspace.config)
        live = self.root / "src/catty_qq_ai/sample.py"
        original = ws._move

        def interrupt(source, destination, **kwargs):
            result = original(source, destination, **kwargs)
            if source == live and "/transaction/discarded/" in destination.as_posix():
                raise Crash("during rollback")
            return result

        with patch.object(ws, "_move", side_effect=interrupt), self.assertRaises(Crash):
            ws.rollback_release(self.root, "health failure")
        self.assertEqual(ws.recover_release(self.root)["status"], "rolled_back")
        self.assertEqual(live.read_text(), "VALUE = 1\n")

    def test_confirm_commit_decision_survives_cleanup_crash(self) -> None:
        workspace, release = self.ready()
        ws.apply_release(self.root, release, workspace.config)
        with patch.object(ws, "_unlink", side_effect=Crash("before journal unlink")), self.assertRaises(Crash):
            ws.confirm_release(self.root)
        self.assertEqual(ws.recover_release(self.root)["status"], "confirmed")
        self.assertEqual((self.root / "src/catty_qq_ai/sample.py").read_text(), "VALUE = 2\n")

    def test_malformed_active_journal_is_not_followed(self) -> None:
        workspace, release = self.ready()
        ws.apply_release(self.root, release, workspace.config)
        path = state_dir(self.root) / "active_release.json"
        journal = json.loads(path.read_text(encoding="utf-8"))
        journal["entries"][0]["path"] = "../outside.py"
        path.write_text(json.dumps(journal), encoding="utf-8")
        with self.assertRaises(ws.WorkspaceError):
            ws.recover_release(self.root)
        self.assertTrue(path.exists())


class RunnerNormalizationTests(unittest.TestCase):
    def normalizer(self, phase: str):
        node = next(item for item in ast.parse(val._RUNNER).body
                    if isinstance(item, ast.FunctionDef) and item.name == "normalize")
        namespace = {
            "root": PureWindowsPath("E:/fixture/猫's run") / phase,
            "temporary": PureWindowsPath("C:/scratch/临时目录") / phase,
            "re": re, "json": json,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "trusted-normalize", "exec"), namespace)
        return namespace["normalize"], namespace

    def test_windows_raw_repr_and_json_paths_share_one_identity(self) -> None:
        first, before = self.normalizer("baseline")
        second, after = self.normalizer("candidate")
        for key in ("root", "temporary"):
            for forward in (False, True):
                old_path, new_path = before[key] / "missing.json", after[key] / "missing.json"
                old = old_path.as_posix() if forward else str(old_path)
                new = new_path.as_posix() if forward else str(new_path)
                for render in (str, repr, json.dumps):
                    with self.subTest(key=key, forward=forward, render=render.__name__):
                        self.assertEqual(first(render(old)), second(render(new)))
                        self.assertIn("missing.json", first(render(old)))

    def test_hex_error_codes_are_not_object_addresses(self) -> None:
        normalize, _ = self.normalizer("baseline")
        self.assertNotEqual(normalize("HRESULT=0x80070005"), normalize("HRESULT=0x80070006"))
        self.assertEqual(normalize("status 0x23"), "status 0x23")

    def test_object_repr_address_is_volatile(self) -> None:
        normalize, _ = self.normalizer("baseline")
        self.assertEqual(normalize("<Widget object at 0x1234>"), normalize("<Widget object at 0x5678>"))


class OutcomeComparisonTests(unittest.TestCase):
    @staticmethod
    def module(cases: list[dict]) -> dict:
        return {"module": "test_fixture", "status": "completed", "exit_code": 0,
                "discovered": [row["id"] for row in cases], "tests_run": len(cases),
                "cases": cases, "fixture_errors": [], "loader_errors": []}

    @staticmethod
    def case(name: str, outcome: str = "success", **extra) -> dict:
        return {"id": name, "outcome": outcome, "detail": "fixture", "error_key": "a" * 64,
                "subtests": [], **extra}

    def test_failure_moved_between_ids_cannot_hide_in_equal_totals(self) -> None:
        before = self.module([self.case("one", "failure"), self.case("two")])
        after = self.module([self.case("one"), self.case("two", "failure")])
        result = val.compare_results([before], [after])
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["baseline_red"]), len(result["candidate_red"]))

    def test_changed_error_identity_and_skip_reason_are_rejected(self) -> None:
        before = self.module([self.case("healthy"), self.case("red", "error"), self.case("skip", "skip")])
        for index, key, value in ((1, "error_key", "b" * 64), (2, "detail", "new reason")):
            after = copy.deepcopy(before)
            after["cases"][index][key] = value
            self.assertFalse(val.compare_results([before], [after])["ok"])

    def test_missing_subtest_is_not_hidden_by_parent_success(self) -> None:
        case = self.case("parent", subtests=[self.case("parent (n=1)"), self.case("parent (n=2)")])
        before = self.module([case])
        after = copy.deepcopy(before)
        after["cases"][0]["subtests"].pop()
        self.assertFalse(val.compare_results([before], [after])["ok"])

    def test_all_skipped_and_zero_case_reports_are_not_validation(self) -> None:
        for cases in ([], [self.case("skip", "skip")]):
            record = self.module(cases)
            self.assertFalse(val.compare_results([record], [record])["ok"])

    def test_existing_red_and_unchanged_skips_are_reported(self) -> None:
        record = self.module([self.case("healthy"), self.case("red", "failure"), self.case("skip", "skip")])
        result = val.compare_results([record], [copy.deepcopy(record)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["baseline_red"][0]["id"], "red")


if __name__ == "__main__":
    unittest.main()
