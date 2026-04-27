from pathlib import Path
import importlib.util
import tempfile
import unittest


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
            {"local_critic": {"model": "qwen3:4b"}},
            {"model": "qwen3:0.6b"},
        )

        self.assertEqual(models, ["qwen3:0.6b", "qwen3:4b"])


if __name__ == "__main__":
    unittest.main()
