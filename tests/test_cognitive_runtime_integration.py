from __future__ import annotations

import ast
import copy
from pathlib import Path
import types
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_INIT_PATH = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
_SIM_PATH = _ROOT / "src" / "catty_qq_ai" / "catty_sim_chat.py"
_CONFIG_PATH = _ROOT / "src" / "catty_qq_ai" / "config.py"
_SPEC_PATH = _ROOT / "CattyQQAI.spec"


def _source_function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing function: {name}")


class _StripRelativeImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.level:
            return ast.copy_location(ast.Pass(), node)
        return node


def _load_sync_function(path: Path, name: str, namespace: dict[str, object]):
    function = copy.deepcopy(_source_function(path, name))
    function = _StripRelativeImports().visit(function)
    assert isinstance(function, ast.FunctionDef)
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    runtime: dict[str, object] = {"Any": object, "__name__": "cognitive_runtime_test"}
    runtime.update(namespace)
    exec(compile(module, str(path), "exec"), runtime)
    return runtime[name]


def _tool_context_call(path: Path, function_name: str) -> ast.Call:
    function = _source_function(path, function_name)
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ToolContext"
    ]
    if len(calls) != 1:
        raise AssertionError(f"expected one ToolContext in {function_name}, got {len(calls)}")
    return calls[0]


class CognitiveRuntimeIntegrationTests(unittest.TestCase):
    def test_cognitive_bundle_uses_scope_persona_and_real_stores(self) -> None:
        calls: list[tuple[str, object]] = []

        def fake_harness(text, **kwargs):
            calls.append(("harness", {"text": text, **kwargs}))
            return {"text": "HARNESS", "rendered_counts": {"MEMORY": 1}, "rendered_chars": 7}

        class Timeline:
            def list(self, scope, *, persona):
                calls.append(("timeline_list", (scope, persona)))
                return [{"id": "t1"}]

            def build_prompt(self, scope, *, persona):
                calls.append(("timeline_prompt", (scope, persona)))
                return "TIMELINE"

        class Adaptive:
            def list_prompts(self, scope, *, persona, include_disabled):
                calls.append(("adaptive_list", (scope, persona, include_disabled)))
                return [{"id": "a1"}]

            def build_prompt(self, scope, *, persona, current_user_payload):
                calls.append(("adaptive_prompt", (scope, persona, current_user_payload)))
                return "【自适应进化prompt】\n短句\n【/自适应进化prompt】"

        logger = types.SimpleNamespace(debug=lambda *_args, **_kwargs: None)
        function = _load_sync_function(
            _INIT_PATH,
            "_build_cognitive_turn_context_sync",
            {
                "build_fadianji_evidence_packet": fake_harness,
                "memory_store": object(),
                "catty_rag_store": object(),
                # 2026-08-15 二轮: harness 调用新增 feed_store/config 引用, 测试命名空间同步注入
                "qzone_feed_store": object(),
                "config": types.SimpleNamespace(catty_fdj_corpus_lore_enabled=False, catty_fdj_corpus_lore_max_chars=96),
                "timeline_store": Timeline(),
                "adaptive_prompt_store": Adaptive(),
                "logger": logger,
            },
        )
        system_context, adaptive = function(
            text="还记得上次吗",
            persona=types.SimpleNamespace(name="fadianji", semantic_harness_enabled=True),
            scope_key="private:3670608232",
            is_private=True,
            user_id="3670608232",
            group_id="",
            display_name="测试群名片",
            quoted_text="引用消息中的旧话题",
        )

        self.assertEqual(system_context, "HARNESS\n\nTIMELINE")
        self.assertIn("【自适应进化prompt】", adaptive)
        harness_call = next(value for name, value in calls if name == "harness")
        self.assertEqual(harness_call["scope_key"], "private:3670608232")
        self.assertTrue(harness_call["is_private"])
        self.assertEqual(harness_call["book_k"], 0)
        self.assertEqual(harness_call["scene_k"], 12)
        self.assertEqual(harness_call["max_chars"], 12000)
        self.assertEqual(harness_call["display_name"], "测试群名片")
        self.assertEqual(harness_call["quoted_text"], "引用消息中的旧话题")
        self.assertEqual(harness_call["user_id"], "3670608232")
        self.assertFalse(harness_call["corpus_profile_enabled"])
        self.assertEqual(harness_call["corpus_profile_max_chars"], 96)
        self.assertEqual(next(value for name, value in calls if name == "adaptive_prompt")[2], "")

    def _runtime(self, builder, *, budget=12000):
        logs: list[str] = []
        function = _load_sync_function(
            _INIT_PATH, "_build_cognitive_turn_context_sync",
            {
                "build_fadianji_evidence_packet": builder,
                "memory_store": None, "catty_rag_store": None, "qzone_feed_store": None,
                "config": types.SimpleNamespace(catty_fadianji_harness_max_chars=budget),
                "timeline_store": types.SimpleNamespace(list=lambda *a, **k: []),
                "adaptive_prompt_store": types.SimpleNamespace(list_prompts=lambda *a, **k: []),
                "logger": types.SimpleNamespace(debug=logs.append, warning=logs.append),
            },
        )
        return function, logs

    def test_source_diagnostics_are_logged_without_discarding_partial_packet(self) -> None:
        function, logs = self._runtime(lambda *a, **k: {
            "text": "PARTIAL EVIDENCE",
            "diagnostics": [{"source": "rag", "error_type": "RuntimeError"}],
        })
        context, adaptive = function(
            text="上次的事", persona=types.SimpleNamespace(name="fadianji", semantic_harness_enabled=True),
            scope_key="group:42", is_private=False, user_id="7", group_id="42",
        )
        self.assertEqual(context, "PARTIAL EVIDENCE")
        self.assertEqual(adaptive, "")
        self.assertTrue(any("rag:RuntimeError" in line for line in logs))

    def test_quote_without_current_text_still_reaches_harness(self) -> None:
        calls = []
        function, _ = self._runtime(lambda text, **kwargs: calls.append((text, kwargs)) or {"text": "QUOTE"})
        context, _ = function(
            text="", quoted_text="引用旧消息", persona=types.SimpleNamespace(name="fadianji", semantic_harness_enabled=True),
            scope_key="group:42", is_private=False, user_id="7", group_id="42",
        )
        self.assertEqual(context, "QUOTE")
        self.assertEqual(calls[0][1]["quoted_text"], "引用旧消息")
        self.assertEqual(calls[0][1]["scope_key"], "group:42")

    def test_packet_failure_is_not_empty_evidence_and_does_not_expose_exception(self) -> None:
        def fail(*a, **k):
            raise RuntimeError("fixture-private-database-path")
        function, logs = self._runtime(fail)
        context, _ = function(
            text="还记得吗", persona=types.SimpleNamespace(name="fadianji", semantic_harness_enabled=True),
            scope_key="group:42", is_private=False, user_id="7", group_id="42",
        )
        self.assertIn("harness:error", context)
        self.assertNotIn("fixture-private-database-path", context + "".join(logs))

    def test_zero_budget_skips_evidence_retrieval(self) -> None:
        def fail(*a, **k):
            self.fail("zero budget must not query stores")
        function, logs = self._runtime(fail, budget=0)
        self.assertEqual(function(
            text="上次", persona=types.SimpleNamespace(name="fadianji", semantic_harness_enabled=True),
            scope_key="group:42", is_private=False, user_id="7", group_id="42",
        ), ("", ""))
        self.assertEqual(logs, [])

    def test_build_messages_offloads_bundle_and_removes_scene_only_call(self) -> None:
        function = _source_function(_INIT_PATH, "_build_messages")
        old_scene_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_scene_reference_block"
        ]
        self.assertEqual(old_scene_calls, [])
        offloads = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and node.func.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "_build_cognitive_turn_context_sync"
        ]
        self.assertEqual(len(offloads), 1)
        source = ast.unparse(function)
        self.assertIn("_adaptive_user_prompt", source)
        self.assertIn("cognitive evidence and timeline", source)
        self.assertLess(source.index("_adaptive_user_prompt"), source.index("messages.append({'role': 'user'"))
        offload = offloads[0]
        name_value = next(keyword.value for keyword in offload.keywords if keyword.arg == "display_name")
        self.assertEqual(ast.unparse(name_value), "_display_name(event)")
        quote_value = next(keyword.value for keyword in offload.keywords if keyword.arg == "quoted_text")
        self.assertEqual(ast.unparse(quote_value), "quoted_text")
        self.assertIn("quoted_text=_reply_source_retrieval_text(reply_sources)", ast.unparse(_source_function(_INIT_PATH, "handle_chat")))

    def test_rebuilt_messages_retain_user_and_cognitive_context(self) -> None:
        source = ast.unparse(_source_function(_INIT_PATH, "_build_messages"))
        self.assertIn("_slim_messages.append({'role': 'user', 'content': _user_content_raw})", source)
        self.assertIn("_append_internal_system(_slim_messages, _cognitive_system_context, label='cognitive evidence and timeline')", source)
        self.assertLess(source.index("_append_internal_system(_slim_messages, _cognitive_system_context"), source.index("_slim_messages.append({'role': 'assistant', 'content': _prefill})"))

    def test_harness_guide_uses_actual_tools_and_keeps_plain_route_tool_free(self) -> None:
        production = ast.unparse(_source_function(_INIT_PATH, "handle_chat"))
        simulation = ast.unparse(_source_function(_SIM_PATH, "_sim_chat_unlocked"))
        self.assertIn("append_harness_agent_hint(messages, () if _prefer_spark else _allowed_tool_names)", production)
        self.assertIn("append_harness_agent_hint(messages, _allowed_tool_names)", simulation)
        self.assertIn("is_owner=bool(affection_store.is_owner(str(event.user_id)))", simulation)

    def test_production_and_sim_tool_context_receive_all_cognitive_stores(self) -> None:
        expected = {
            "emoji_store",
            "scope_lorebook_store",
            "timeline_store",
            "adaptive_prompt_store",
            "session_cache",
        }
        for path, function_name in ((_INIT_PATH, "handle_chat"), (_SIM_PATH, "_sim_chat_unlocked")):
            with self.subTest(path=path.name):
                call = _tool_context_call(path, function_name)
                keywords = {keyword.arg: keyword.value for keyword in call.keywords}
                self.assertTrue(expected.issubset(keywords))
                emoji_value = keywords["emoji_store"]
                self.assertIsInstance(emoji_value, ast.Call)
                assert isinstance(emoji_value, ast.Call)
                self.assertIsInstance(emoji_value.func, ast.Name)
                assert isinstance(emoji_value.func, ast.Name)
                self.assertEqual(emoji_value.func.id, "_emoji_store_for_persona")

    def test_no_runtime_emoji_operation_uses_global_store_directly(self) -> None:
        tree = ast.parse(_INIT_PATH.read_text(encoding="utf-8"))
        forbidden_methods = {
            "select",
            "candidates_text",
            "adopt_downloaded",
            "save_downloaded",
            "update_metadata",
            "refresh",
        }
        direct_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in forbidden_methods
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "emoji_store"
        ]
        self.assertEqual(direct_calls, [])

    def test_config_and_packaging_include_new_runtime_contracts(self) -> None:
        config_source = _CONFIG_PATH.read_text(encoding="utf-8")
        for field in (
            "catty_profile_memory_enabled",
            "catty_scope_meme_enabled",
            "catty_timeline_enabled",
            "catty_adaptive_prompt_enabled",
        ):
            self.assertIn(f"{field}: bool = True", config_source)
        self.assertIn("datas.append(('fadianji', 'fadianji'))", _SPEC_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
