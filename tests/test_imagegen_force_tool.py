import ast
from pathlib import Path
import importlib.util
import sys
import types
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = types.ModuleType("catty_qq_ai")
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault("catty_qq_ai", _PACKAGE)
_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "tools.py"
_SPEC = importlib.util.spec_from_file_location("catty_qq_ai.tools", _MODULE_PATH)
_tools = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = _tools
_SPEC.loader.exec_module(_tools)


class ImagegenForceToolTests(unittest.TestCase):
    def test_nsfw_caption_composer_marks_provider_call_auxiliary(self) -> None:
        path = _ROOT / "src" / "catty_qq_ai" / "catty_nsfw_imagegen.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_compose_base_caption"
        )
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_post_chat_completion_raw"
        ]

        self.assertEqual(len(calls), 1)
        route = next(
            keyword.value
            for keyword in calls[0].keywords
            if keyword.arg == "request_route"
        )
        self.assertIsInstance(route, ast.Constant)
        self.assertEqual(route.value, "imagegen_caption")

    def test_explicit_self_portrait_request_forces_imagegen(self) -> None:
        self.assertTrue(
            _tools.should_force_imagegen_tool(
                "猫猫 画一张你自己的自画像，穿泳衣的",
                is_directly_requested=True,
            )
        )

    def test_capability_question_does_not_force_imagegen(self) -> None:
        self.assertFalse(
            _tools.should_force_imagegen_tool("猫猫你会画图吗", is_directly_requested=True)
        )

    def test_ambient_drawing_talk_does_not_force_imagegen(self) -> None:
        self.assertFalse(
            _tools.should_force_imagegen_tool("群友说画画真难", is_directly_requested=False)
        )

    def test_explicit_nsfw_action_does_not_force_imagegen(self) -> None:
        self.assertFalse(
            _tools.should_force_imagegen_tool("猫猫画一下蜜穴", is_directly_requested=True)
        )


if __name__ == "__main__":
    unittest.main()
