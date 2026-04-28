from pathlib import Path
import importlib.util
import sys
import tempfile
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "catty_hot_reload.py"
_SPEC = importlib.util.spec_from_file_location("catty_hot_reload", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_hot_reload = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _hot_reload
_SPEC.loader.exec_module(_hot_reload)


class HotReloadWatcherTests(unittest.TestCase):
    def test_ignored_directories_are_not_watched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "catty.py").write_text("print('ok')", encoding="utf-8")
            (root / ".venv" / "Lib").mkdir(parents=True)
            (root / ".venv" / "Lib" / "ignored.py").write_text("print('no')", encoding="utf-8")
            (root / "build").mkdir()
            (root / "build" / "ignored.py").write_text("print('no')", encoding="utf-8")

            watched = {path.relative_to(root).as_posix() for path in _hot_reload.iter_watch_files(root)}

        self.assertIn("src/catty.py", watched)
        self.assertNotIn(".venv/Lib/ignored.py", watched)
        self.assertNotIn("build/ignored.py", watched)

    def test_changed_files_reports_modified_files(self) -> None:
        before = {"src/catty.py": (1, 10), "README.md": (1, 20)}
        after = {"src/catty.py": (2, 10), "README.md": (1, 20)}

        self.assertEqual(_hot_reload.changed_files(before, after), ["src/catty.py"])


if __name__ == "__main__":
    unittest.main()
