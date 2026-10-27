from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "catty_security_sandbox_paths", _ROOT / "src" / "catty_qq_ai" / "code_sandbox.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_sandbox = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _sandbox
_SPEC.loader.exec_module(_sandbox)


class SandboxPathSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = Path(temporary.name) / "storage"
        fixture = self.storage / "fixture"
        fixture.mkdir(parents=True)
        self.outside_file = fixture / "file.txt"
        self.outside_file.write_text("fixture only", encoding="utf-8")
        self.outside_dir = fixture / "directory"
        self.outside_dir.mkdir()
        self.outside_missing = fixture / "missing.txt"
        self.packages = self.storage / "packages"
        self.packages.mkdir()
        (self.packages / "fixture.txt").write_text("package", encoding="utf-8")
        self.config = types.SimpleNamespace(
            catty_code_sandbox_dir=str(self.storage),
            catty_code_sandbox_timeout_seconds=15,
            catty_code_sandbox_max_output_chars=8000,
        )

    async def run_fixture(self, source):
        for key, value in (
            ("__OUTSIDE_FILE__", self.outside_file),
            ("__OUTSIDE_DIR__", self.outside_dir),
            ("__OUTSIDE_MISSING__", self.outside_missing),
            ("__PACKAGES__", self.packages),
        ):
            source = source.replace(key, repr(str(value)))
        result = await _sandbox.run_code(
            self.config, source, input_files={"inside.txt": "inside"}
        )
        self.assertTrue(result["ok"], result)
        return result["output"]

    async def test_path_probes_and_dir_fd(self):
        output = await self.run_fixture("""
import os
import sys
from pathlib import Path

outside_file = __OUTSIDE_FILE__
outside_dir = __OUTSIDE_DIR__
outside_missing = __OUTSIDE_MISSING__
packages = __PACKAGES__


def denied(call, *args, **kwargs):
    try:
        value = call(*args, **kwargs)
    except PermissionError:
        return
    assert value is False, (call, args, kwargs, value)


def permission_denied(call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except PermissionError:
        return
    raise AssertionError((call, args, kwargs))


probes = [getattr(os.path, name) for name in
          ("exists", "lexists", "isfile", "isdir", "islink", "isjunction", "ismount", "isdevdrive")
          if hasattr(os.path, name)]
for path in (outside_file, outside_dir, outside_missing):
    for value in (path, os.fsencode(path), Path(path)):
        for probe in probes:
            denied(probe, value)
    for method in ("exists", "is_file", "is_dir", "is_symlink", "is_junction", "is_mount"):
        probe = getattr(Path(path), method, None)
        if probe is not None:
            try:
                denied(probe)
            except NotImplementedError:
                if method != "is_mount":
                    raise
    if sys.version_info >= (3, 13):
        for method in ("exists", "is_file", "is_dir"):
            denied(getattr(Path(path), method), follow_symlinks=False)
    if hasattr(Path(path), "info"):
        info = Path(path).info
        for method in ("exists", "is_file", "is_dir", "is_symlink"):
            denied(getattr(info, method))
        for method in ("exists", "is_file", "is_dir"):
            denied(getattr(info, method), follow_symlinks=False)
        permission_denied(info._stat)
    permission_denied(os.stat, path)
    permission_denied(os.lstat, path)
    permission_denied(os.access, path, os.F_OK)

assert os.path.exists("inside.txt")
assert os.path.isfile(os.fsencode("inside.txt"))
assert not os.path.isdir("inside.txt")
assert os.path.isdir(".")
assert not os.path.exists("absent.txt")
assert not os.path.isfile("absent.txt")
assert not os.path.isdir("absent.txt")
assert Path("inside.txt").exists() and Path("inside.txt").is_file()
assert Path(".").is_dir() and not Path("absent.txt").exists()
assert Path(packages, "fixture.txt").is_file()
assert Path(sys.executable).exists()

class Once:
    def __init__(self, first, later):
        self.values = [first, later]
        self.calls = 0
    def __fspath__(self):
        self.calls += 1
        return self.values[min(self.calls - 1, 1)]

swapper = Once(os.fsencode("inside.txt"), os.fsencode(outside_file))
assert os.path.exists(swapper) and swapper.calls == 1
swapper = Once(os.fsencode("inside.txt"), os.fsencode(outside_file))
assert os.stat(swapper).st_size == 6 and swapper.calls == 1

with open("inside.txt", "rb") as handle:
    assert os.path.exists(handle.fileno())
    assert os.stat(handle.fileno()).st_size == 6

for call, args in ((os.stat, ("inside.txt",)),
                   (os.lstat, ("inside.txt",)),
                   (os.access, ("inside.txt", os.F_OK))):
    permission_denied(call, *args, dir_fd=0)
if os.name != "nt":
    directory_fd = os.open(".", os.O_RDONLY)
    try:
        assert os.stat(os.path.abspath("inside.txt"), dir_fd=directory_fd).st_size == 6
        permission_denied(os.stat, b"inside.txt", dir_fd=directory_fd)
    finally:
        os.close(directory_fd)

if os.name == "nt":
    import nt
    for name in ("_path_exists", "_path_lexists", "_path_isfile", "_path_isdir",
                 "_path_islink", "_path_isjunction", "_path_isdevdrive"):
        probe = getattr(nt, name, None)
        if probe is None:
            continue
        for path in (outside_file, outside_dir, outside_missing):
            denied(probe, path)
            denied(probe, os.fsencode(path))
        if name == "_path_exists":
            assert probe("inside.txt")
            assert not probe("absent.txt")
    permission_denied(nt.stat, outside_file)
    if hasattr(nt, "lstat"):
        permission_denied(nt.lstat, outside_file)

print("PATH_MATRIX_OK")
""")
        self.assertIn("PATH_MATRIX_OK", output)

    async def test_symlink_and_cached_path_info(self):
        output = await self.run_fixture("""
import os
from pathlib import Path

outside_file = __OUTSIDE_FILE__
link = Path("swing")
try:
    link.symlink_to("inside.txt")
except (OSError, NotImplementedError):
    print("SYMLINK_UNAVAILABLE")
else:
    assert link.exists() and link.is_file() and os.path.islink(link)
    info = None
    if hasattr(link, "info"):
        info = next(item for item in Path(".").iterdir() if item.name == "swing").info
        assert info.exists() and info.is_file()
    link.unlink()
    link.symlink_to(outside_file)
    for probe in (os.path.exists, os.path.lexists, os.path.isfile,
                  os.path.isdir, os.path.islink):
        try:
            value = probe(link)
        except PermissionError:
            continue
        assert value is False, (probe, value)
    for method in (link.exists, link.is_file, link.is_dir, link.is_symlink):
        try:
            value = method()
        except PermissionError:
            continue
        assert value is False, (method, value)
    if info is not None:
        for method in (info.exists, info.is_file, info.is_dir, info.is_symlink):
            try:
                value = method()
            except PermissionError:
                continue
            assert value is False, (method, value)
        try:
            info._stat()
        except PermissionError:
            pass
        else:
            raise AssertionError("cached stat escaped path guard")
    print("SYMLINK_GUARD_OK")
""")
        if "SYMLINK_UNAVAILABLE" in output:
            self.skipTest("symlink creation unavailable")
        self.assertIn("SYMLINK_GUARD_OK", output)


if __name__ == "__main__":
    unittest.main()
