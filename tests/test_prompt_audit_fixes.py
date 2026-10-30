"""普通回复长度 guard 的离线回归；不导入 NoneBot 或实际人格模块。"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


_SOURCE = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "prompt_manager.py"
_PACKAGE = "_catty_prompt_audit_fixture"
_LENGTH_GUARD = "【LEN】non-academic<=50zh;1句;keep emotion/action/core only"


class PromptAuditFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.register = next(
            node for node in cls.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "register_catty_persona"
        )
        cls.guard = next(
            node for node in cls.register.body
            if isinstance(node, ast.FunctionDef) and node.name == "_build_non_academic_length_guard"
        )

    def _evaluate(self, ctx, detector, *, user_text="早上好", technical_direct=False):
        # 相对导入只指向独占的合成包，不执行 cpu_engine/插件入口。
        package = ModuleType(_PACKAGE)
        package.__path__ = []
        engine = ModuleType(f"{_PACKAGE}.cpu_engine")
        engine.__path__ = []
        interaction = ModuleType(f"{_PACKAGE}.cpu_engine.strong_interaction")
        interaction.detect_academic_signal = detector
        namespace = {
            "__package__": _PACKAGE,
            "ctx": ctx,
            "user_text": user_text,
            "technical_direct": technical_direct,
        }
        tree = ast.Module(body=[copy.deepcopy(self.guard)], type_ignores=[])
        ast.fix_missing_locations(tree)
        modules = {module.__name__: module for module in (package, engine, interaction)}
        with patch.dict(sys.modules, modules):
            exec(compile(tree, str(_SOURCE), "exec"), namespace)
            return namespace["_build_non_academic_length_guard"]()

    def test_chat_without_image_calls_detector_and_keeps_50_chars(self):
        detector = Mock(return_value=False)
        self.assertEqual(self._evaluate({}, detector), _LENGTH_GUARD)
        detector.assert_called_once_with("早上好")

    def test_image_description_is_read_from_context(self):
        detector = Mock(return_value=True)
        result = self._evaluate({"image_description": "图中是合成方程 x + 1 = 2"}, detector)
        self.assertEqual(result, "")
        detector.assert_called_once_with("早上好\n图中是合成方程 x + 1 = 2")

    def test_text_only_academic_detection_exempts_length(self):
        detector = Mock(return_value=True)
        self.assertEqual(self._evaluate({}, detector, user_text="解释二分查找"), "")
        detector.assert_called_once_with("解释二分查找")

    def test_image_only_detection_exempts_length(self):
        detector = Mock(return_value=True)
        self.assertEqual(self._evaluate({"image_description": "合成题目"}, detector, user_text=""), "")
        detector.assert_called_once_with("合成题目")

    def test_empty_or_non_text_image_is_ignored(self):
        for image_description in (None, 123, {}, "", " \t"):
            with self.subTest(image_description=image_description):
                detector = Mock(return_value=False)
                self.assertEqual(self._evaluate({"image_description": image_description}, detector), _LENGTH_GUARD)
                detector.assert_called_once_with("早上好")

    def test_technical_direct_keeps_existing_exemption(self):
        detector = Mock(return_value=False)
        self.assertEqual(self._evaluate({}, detector, technical_direct=True), "")
        detector.assert_called_once_with("早上好")

    def test_detector_error_preserves_chat_guard_and_technical_exemption(self):
        for technical_direct, expected in ((False, _LENGTH_GUARD), (True, "")):
            with self.subTest(technical_direct=technical_direct):
                detector = Mock(side_effect=RuntimeError("synthetic detector failure"))
                self.assertEqual(self._evaluate({}, detector, technical_direct=technical_direct), expected)
                detector.assert_called_once_with("早上好")

    def test_registration_no_longer_reads_obsolete_system_prompt(self):
        self.assertNotIn("system_prompt", ast.get_docstring(self.register))
        for node in ast.walk(self.register):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx":
                    self.assertFalse(
                        node.args and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "system_prompt"
                    )
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.assertNotEqual(node.id, "system_prompt")


class SpecialCareCacheBoundaryTests(unittest.TestCase):
    def test_selected_rules_keep_text_and_stay_after_static_boundary(self):
        source_path = _SOURCE.with_name("__init__.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        builder = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_messages"
        )
        registration = next(
            node for node in ast.walk(builder)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
            and node.test.id == "special_care_context"
            and any(
                isinstance(child, ast.Constant) and child.value == "catty_special_care_rules"
                for child in ast.walk(node)
            )
        )
        package = ModuleType(_PACKAGE)
        package.__path__ = []
        memory = ModuleType(f"{_PACKAGE}.memory")
        memory.build_special_care_rules = Mock(return_value="unchanged care rules")
        persona = object()
        executable = ast.Module(body=[copy.deepcopy(registration)], type_ignores=[])
        ast.fix_missing_locations(executable)
        for selected in (False, True):
            with self.subTest(selected=selected):
                manager = Mock()
                namespace = {
                    "__package__": _PACKAGE,
                    "special_care_context": "selected user" if selected else "",
                    "_st_manager": manager,
                    "_persona": persona,
                    "logger": Mock(),
                }
                memory.build_special_care_rules.reset_mock()
                with patch.dict(sys.modules, {_PACKAGE: package, memory.__name__: memory}):
                    exec(compile(executable, str(source_path), "exec"), namespace)
                if selected:
                    memory.build_special_care_rules.assert_called_once_with(persona)
                    manager.register_static.assert_called_once_with(
                        "catty_special_care_rules", "unchanged care rules", order=493,
                    )
                    self.assertGreater(manager.register_static.call_args.kwargs["order"], 455)
                else:
                    memory.build_special_care_rules.assert_not_called()
                    manager.register_static.assert_not_called()


if __name__ == "__main__":
    unittest.main()