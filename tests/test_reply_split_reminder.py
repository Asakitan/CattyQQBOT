"""拆条标记每轮贴脸提醒 + 单行气泡约束 (主人 2026-10-25: 像 tools 一样每轮都在场)。"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import re
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
import sys


_ROOT = Path(__file__).resolve().parents[1]
_INIT_PATH = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
_MARKER = "<<<CATTY_REPLY_SPLIT>>>"


def _load(namespace: dict, *names: str) -> dict:
    tree = ast.parse(_INIT_PATH.read_text(encoding="utf-8"))
    functions = [
        copy.deepcopy(node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if len(functions) != len(names):
        missing = set(names) - {node.name for node in functions}
        raise AssertionError(f"missing functions: {sorted(missing)}")
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(_INIT_PATH), "exec"), namespace)
    return namespace


_BASE_NAMESPACE = {
    "re": re,
    "config": SimpleNamespace(catty_reply_human_split_max_chunks=4),
    "REPLY_SPLIT_MARKER": _MARKER,
}
_NAMESPACE = _load(
    dict(_BASE_NAMESPACE),
    "_reply_split_prompt_enabled",
    "_reply_split_reminder_prompt",
    "_semantic_reply_split_prompt",
)
_REMINDER = _NAMESPACE["_reply_split_reminder_prompt"]
_SPLIT_PROMPT = _NAMESPACE["_semantic_reply_split_prompt"]


class ReplySplitReminderTests(unittest.TestCase):
    def test_reminder_states_marker_rule_every_turn(self) -> None:
        text = _REMINDER()
        self.assertIn(_MARKER, text)
        self.assertIn("最多 4 条", text)
        self.assertIn("程序按行发", text)
        self.assertIn("空行段落及列表/公式/代码的换行不会自动拆泡", text)
        self.assertIn("气泡内不换行", text)
        self.assertIn("普通空格不算分条", text)

    def test_reminder_tracks_max_chunks_config(self) -> None:
        namespace = dict(_BASE_NAMESPACE)
        namespace["config"] = SimpleNamespace(catty_reply_human_split_max_chunks=2)
        reminder = _load(namespace, "_reply_split_prompt_enabled", "_reply_split_reminder_prompt")["_reply_split_reminder_prompt"]
        self.assertIn("最多 2 条", reminder())

    def test_reminder_registered_post_boundary(self) -> None:
        source = _INIT_PATH.read_text(encoding="utf-8")
        self.assertIn('"catty_reply_split_reminder"', source)
        self.assertIn("order=810", source)

    def test_responses_cache_full_rules_and_keep_short_turn_reminder(self) -> None:
        tree = ast.parse(_INIT_PATH.read_text(encoding="utf-8"))
        builder = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_messages")
        registration = next(node for node in ast.walk(builder) if isinstance(node, ast.Try)
                            and any(isinstance(child, ast.Constant) and child.value == "catty_reply_split_rules"
                                    for child in ast.walk(node)))
        package = ModuleType("_split_cache_fixture")
        package.__path__ = []
        client = ModuleType(package.__name__ + ".openai_client")
        client._effective_main_model = lambda config: config.catty_openai_model
        client._is_responses_endpoint = lambda endpoint: endpoint.endswith("/responses")
        client._responses_support_explicit_cache = lambda endpoint, model: endpoint.startswith("https://api.openai.com/") and model == "gpt-6.1-terra"
        executable = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(registration)], type_ignores=[]))
        for endpoint, model, disabled, cached in (
            ("https://api.openai.com/v1/responses", "gpt-6.1-terra", [], True),
            ("https://api.openai.com/v1/responses", "gpt-5.5", [], False),
            ("https://api.deepseek.com/v1", "deepseek-chat", [], False),
            ("https://api.openai.com/v1/responses", "gpt-6.1-terra", ["catty_reply_split_rules"], False),
        ):
            with self.subTest(endpoint=endpoint, model=model, disabled=disabled):
                manager = Mock()
                namespace = {
                    "__package__": package.__name__, "_st_manager": manager,
                    "config": SimpleNamespace(catty_openai_base_url=endpoint, catty_openai_model=model, catty_prompts_disabled=disabled),
                    "_reply_split_reminder_prompt": _REMINDER, "logger": Mock(),
                }
                with patch.dict(sys.modules, {package.__name__: package, client.__name__: client}):
                    exec(compile(executable, str(_INIT_PATH), "exec"), namespace)
                calls = manager.register_static.call_args_list
                if cached:
                    self.assertEqual(calls[0].args, ("catty_reply_split_rules", _REMINDER()))
                    self.assertEqual(calls[0].kwargs["order"], 164)
                    self.assertLess(len(calls[-1].args[1]), len(_REMINDER()))
                else:
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0].args[1], _REMINDER())
                self.assertEqual(calls[-1].args[0], "catty_reply_split_reminder")
                self.assertEqual(calls[-1].kwargs["order"], 810)

    def test_history_keeps_own_marker(self) -> None:
        source = _INIT_PATH.read_text(encoding="utf-8")
        self.assertIn("REPLY_SPLIT_MARKER.join(delivered)", source)
        self.assertIn("_delivery_state.delivered_texts()", source)
        self.assertIn(
            "_append_history(history_key, _enriched_user_content_for_history, delivered_prompt)",
            source,
        )

    def test_fadianji_split_prompt_forbids_inner_newlines(self) -> None:
        text = _SPLIT_PROMPT(SimpleNamespace(name="fadianji"))
        self.assertNotIn(_MARKER, text)
        self.assertIn("具体分条格式与条数见【本轮气泡写法】", text)
        self.assertIn("数学/公式", text)

    def test_catty_split_prompt_forbids_inner_newlines(self) -> None:
        text = _SPLIT_PROMPT(None)
        self.assertNotIn(_MARKER, text)
        self.assertIn("具体分条格式与条数见【本轮气泡写法】", text)
        self.assertNotIn("<15 字", text)
        self.assertIn("短行会按行分泡", text)

    def test_disabled_flag_or_legacy_segment_alias_disables_both_prompts(self) -> None:
        configs = [SimpleNamespace(catty_reply_human_split_enabled=False)]
        configs.extend(SimpleNamespace(catty_prompts_disabled=[name]) for name in (
            "catty_semantic_reply_split_skeleton", "catty_semantic_reply_split_pointer", "catty_reply_split_reminder",
        ))
        for cfg in configs:
            namespace = {**_BASE_NAMESPACE, "config": cfg}
            _load(namespace, "_reply_split_prompt_enabled", "_semantic_reply_split_prompt", "_reply_split_reminder_prompt")
            self.assertEqual(namespace["_semantic_reply_split_prompt"](), "")
            self.assertEqual(namespace["_reply_split_reminder_prompt"](), "")


if __name__ == "__main__":
    unittest.main()
