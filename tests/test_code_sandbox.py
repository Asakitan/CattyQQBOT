"""Python sandbox tests (2026-10-26)."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "catty_code_sandbox_test", _ROOT / "src" / "catty_qq_ai" / "code_sandbox.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_sandbox = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _sandbox
_SPEC.loader.exec_module(_sandbox)


def _config(root: Path, **overrides):
    values = {
        "catty_code_sandbox_dir": str(root),
        "catty_code_sandbox_timeout_seconds": 10.0,
        "catty_code_sandbox_max_output_chars": 4000,
        "catty_code_sandbox_max_code_chars": 60000,
        "catty_code_sandbox_max_files": 5,
        "catty_code_sandbox_max_file_bytes": 10_000_000,
        "catty_code_sandbox_max_work_bytes": 50_000_000,
        "catty_code_sandbox_ttl_hours": 24.0,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class SandboxRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "sandbox"
        self.config = _config(self.root)

    async def test_print_and_import_runtime(self) -> None:
        result = await _sandbox.run_code(
            self.config, "import json\nprint(json.dumps({'a': 1}))"
        )
        self.assertTrue(result["ok"], result)
        self.assertIn('{"a": 1}', result["output"])
        self.assertEqual(result["files"], [])

    async def test_read_inside_work_is_allowed(self) -> None:
        result = await _sandbox.run_code(
            self.config, "print(open('input.txt', encoding='utf-8').read())",
            input_files={"input.txt": "inside"},
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("inside", result["output"])
        self.assertEqual(result["files"], [])

    async def test_outside_read_is_blocked_and_secret_not_leaked(self) -> None:
        secret = self.base / "secret.txt"
        secret.write_text("TOP_SECRET_VALUE", encoding="utf-8")
        code = f"print(open({str(secret)!r}, encoding='utf-8').read())"
        result = await _sandbox.run_code(self.config, code)
        self.assertFalse(result["ok"])
        self.assertNotIn("TOP_SECRET_VALUE", result["output"])
        self.assertIn("PermissionError", result["output"])

    async def test_outside_metadata_is_blocked(self) -> None:
        host_file = r"C:\Windows\win.ini"
        exists = await _sandbox.run_code(
            self.config, f"import os; print('RESULT', os.path.exists({host_file!r}))"
        )
        self.assertIn("RESULT False", exists["output"])

        probes = (
            f"import os; print(os.stat({host_file!r}).st_size)",
            r"import os; print(os.listdir(r'C:\Windows')[:1])",
            f"import os; print(os.access({host_file!r}, os.R_OK))",
        )
        for code in probes:
            with self.subTest(code=code):
                result = await _sandbox.run_code(self.config, code)
                self.assertFalse(result["ok"])
                self.assertIn("PermissionError", result["output"])

    async def test_project_config_is_not_readable(self) -> None:
        config_path = _ROOT / "config.json"
        code = f"print(open({str(config_path)!r}, encoding='utf-8').read()[:200])"
        result = await _sandbox.run_code(self.config, code)
        self.assertFalse(result["ok"])
        self.assertIn("PermissionError", result["output"])
        self.assertNotIn("api_key", result["output"])

    async def test_blocked_modules(self) -> None:
        for module in ("subprocess", "socket", "ctypes", "getpass", "psutil"):
            with self.subTest(module=module):
                result = await _sandbox.run_code(self.config, f"import {module}")
                self.assertFalse(result["ok"])
                self.assertIn("blocked", result["output"].lower())

    async def test_host_login_is_not_exposed(self) -> None:
        result = await _sandbox.run_code(self.config, "import os; print(os.getlogin())")
        self.assertTrue(result["ok"], result)
        self.assertIn("sandbox", result["output"])

    async def test_timeout_kills_process(self) -> None:
        started = time.monotonic()
        result = await _sandbox.run_code(
            _config(self.root, catty_code_sandbox_timeout_seconds=1.0),
            "import time\ntime.sleep(5)",
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - started, 8.0)

    async def test_generated_file_is_collected(self) -> None:
        result = await _sandbox.run_code(
            self.config,
            "from pathlib import Path\nPath('out.txt').write_text('data', encoding='utf-8')",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["files"]), 1)
        item = result["files"][0]
        self.assertEqual(item["name"], "out.txt")
        self.assertEqual(Path(item["path"]).read_text(encoding="utf-8"), "data")

    async def test_oversize_code_rejected_without_spawn(self) -> None:
        result = await _sandbox.run_code(
            _config(self.root, catty_code_sandbox_max_code_chars=1000), "x" * 2000
        )
        self.assertFalse(result["ok"])
        self.assertIn("上限", result["error"])


class SandboxPackageTests(unittest.TestCase):
    def test_system_prompt_states_host_data_boundary(self) -> None:
        prompt = _sandbox.CODE_SANDBOX_SYSTEM_PROMPT
        self.assertIn("沙箱", prompt)
        self.assertIn("禁止读取", prompt)
        self.assertIn("catty_python_run", prompt)
        self.assertIn("catty_run_code", prompt)

    def test_package_allowlist_and_specs(self) -> None:
        specs, error = _sandbox._validate_package_specs(
            ["numpy==1.26.0", "pandas"], allowed={"numpy", "pandas"}, max_packages=5
        )
        self.assertIsNone(error)
        self.assertEqual(specs, ["numpy==1.26.0", "pandas"])

        specs, error = _sandbox._validate_package_specs(
            ["requests"], allowed={"numpy"}, max_packages=5
        )
        self.assertEqual(specs, [])
        self.assertIn("白名单", error)

        specs, error = _sandbox._validate_package_specs(
            ["../../evil"], allowed={"numpy"}, max_packages=5
        )
        self.assertEqual(specs, [])
        self.assertIn("不合法", error)

    def test_symlink_is_not_collected_as_generated_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sandbox"
            config = _config(root)
            paths = _sandbox.ensure_sandbox(config)
            host_file = Path(directory) / "host.txt"
            host_file.write_text("host", encoding="utf-8")
            link = paths.work / "link.txt"
            try:
                link.symlink_to(host_file)
            except OSError:
                self.skipTest("symlink creation not permitted")
            items = _sandbox._collect_generated_files(
                paths, 0.0, max_files=5, max_file_bytes=10_000_000
            )
            self.assertEqual(items, [])

    def test_cleanup_keeps_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sandbox"
            config = _config(root)
            paths = _sandbox.ensure_sandbox(config)
            old = paths.work / "old.txt"
            fresh = paths.work / "fresh.txt"
            package = paths.packages / "numpy" / "__init__.py"
            old.write_text("old", encoding="utf-8")
            fresh.write_text("fresh", encoding="utf-8")
            package.parent.mkdir(parents=True, exist_ok=True)
            package.write_text("", encoding="utf-8")
            now = time.time()
            old_time = now - 25 * 3600
            os.utime(old, (old_time, old_time))
            removed = _sandbox.cleanup_code_sandbox(config, now=now)
            self.assertGreaterEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(package.exists())


class SandboxInstallCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_uses_only_binary_target_and_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sandbox"
            config = _config(
                root, catty_code_sandbox_allowed_packages=["numpy"]
            )

            class Stdout:
                async def read(self, _size: int) -> bytes:
                    return b""

            process = types.SimpleNamespace(
                returncode=0,
                stdout=Stdout(),
                wait=AsyncMock(return_value=0),
                kill=Mock(),
            )
            with patch.object(
                _sandbox.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ) as spawn:
                result = await _sandbox.install_packages(config, ["numpy"])

            self.assertTrue(result["ok"], result)
            command = spawn.call_args.args
            self.assertIn("--only-binary=:all:", command)
            self.assertIn("--target", command)
            self.assertIn(str(root / "packages"), command)
            self.assertIn("numpy", command)


if __name__ == "__main__":
    unittest.main()
