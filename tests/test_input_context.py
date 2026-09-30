from __future__ import annotations

import ast
import asyncio
import contextvars
import copy
import importlib.util
import json
import logging
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai"
SUMMARY_PREFIX = "【前情提要·AI压缩】"


def _load_functions(filename: str, names: set[str], namespace: dict) -> dict:
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    if len(nodes) != len(names):
        raise AssertionError("A tested function was removed or renamed")
    # Execute real functions without importing the live plugin or its stores.
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, str(ROOT / filename), "exec"), namespace)
    return namespace


def _history(count: int = 20, width: int = 100) -> list[dict]:
    return [
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": f"neutral message {index:04d} ".ljust(width, ".")}
        for index in range(count)
    ]


class _Cache:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = copy.deepcopy(messages)
        self.writes = 0
        self.flushes = 0
        self.metadata = {}

    def get(self, key):
        return self.messages

    def get_metadata(self, key):
        return dict(self.metadata)

    def set(self, key, messages):
        self.messages = messages
        self.writes += 1

    def update_metadata(self, key, **metadata):
        self.metadata.update(metadata)

    def flush_sync(self):
        self.flushes += 1


class _IsolatedPackage(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.package_name = "_catty_input_context_test"
        self.modules = patch.dict(sys.modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        package = types.ModuleType(self.package_name)
        package.__path__ = [str(ROOT)]
        sys.modules[self.package_name] = package
        self.config_module = types.ModuleType(f"{self.package_name}.config")
        self.config_module.config = types.SimpleNamespace(
            catty_cache_diag_enabled=False,
            catty_cache_ttl="5min",
        )
        package.config = self.config_module
        sys.modules[self.config_module.__name__] = self.config_module
        self.openai = types.ModuleType(f"{self.package_name}.openai_client")
        self.openai.get_current_model_override = lambda: "offline-model"
        self.openai.get_session_token_estimator_multiplier = lambda model: 1.0
        self.openai.get_current_scope_key = lambda: ""
        sys.modules[self.openai.__name__] = self.openai
        nlu = types.ModuleType(f"{self.package_name}.nlu")
        nlu.__path__ = []
        sys.modules[nlu.__name__] = nlu
        compressor = types.ModuleType(f"{nlu.__name__}.prompt_compressor")
        # Deterministic weights exercise orchestration independent of tokenizers.
        compressor.count_history_tokens = lambda rows: sum(len(str(m.get("content", ""))) for m in rows)
        compressor.clear_anchor_observation = lambda: None
        sys.modules[compressor.__name__] = compressor
        dashboard = types.ModuleType(f"{self.package_name}.dashboard_state")
        dashboard.start_stream = lambda **kwargs: None
        dashboard.end_stream = lambda *args, **kwargs: None
        dashboard.push_event = lambda *args, **kwargs: None
        sys.modules[dashboard.__name__] = dashboard


class CompactionInputTests(_IsolatedPackage):
    def setUp(self) -> None:
        super().setUp()
        self.cache = _Cache(_history())
        self.inputs = []
        self.during_summary = lambda: None
        self.summary_result = "Neutral summary of the prior discussion"

        async def summarize(config, messages):
            self.inputs.append(copy.deepcopy(messages))
            self.during_summary()
            if isinstance(self.summary_result, Exception):
                raise self.summary_result
            return self.summary_result

        self.openai.chat_completion_summary = summarize
        self.namespace = _load_functions("__init__.py", {
            "_run_session_ai_compact", "_is_ai_compact_block", "_append_history",
        }, {
            "__package__": self.package_name,
            "_AI_COMPACT_BLOCK_PREFIX": SUMMARY_PREFIX,
            "_AI_COMPACT_RUNNING": {"group:offline"},
            "_get_session_cache": lambda: self.cache,
            "config": types.SimpleNamespace(catty_session_context_enabled=False, catty_history_turns=1),
            "logger": Mock(), "time": time,
            "_get_time_bucket_context_store": Mock(),
            "activity_feed": Mock(),
        })

    async def _compact(self):
        await self.namespace["_run_session_ai_compact"]("group:offline")
        self.assertNotIn("group:offline", self.namespace["_AI_COMPACT_RUNNING"])

    async def test_previous_summary_is_included_in_next_summary_input(self):
        previous = {"role": "user", "content": SUMMARY_PREFIX + " Return the library book on Friday"}
        self.cache.messages.insert(0, previous)
        raw = copy.deepcopy(self.cache.messages[1:])
        await self._compact()
        self.assertIn(previous["content"], self.inputs[0][-1]["content"])
        self.assertEqual(self.cache.messages[1:], raw[-8:])
        self.assertEqual(self.cache.messages[-1], raw[-1])
        self.assertEqual(self.cache.writes, 1)
        self.assertEqual(self.cache.flushes, 1)

    async def test_previous_summaries_survive_raw_transcript_character_cap(self):
        raw = _history(220, width=1000)
        previous = [
            {"role": "user", "content": SUMMARY_PREFIX + " EARLIER_DEADLINE"},
            {"role": "system", "content": SUMMARY_PREFIX + " EARLIER_LOCATION"},
        ]
        self.cache.messages = previous + raw
        await self._compact()
        transcript = self.inputs[0][-1]["content"]
        self.assertIn("EARLIER_DEADLINE", transcript)
        self.assertIn("EARLIER_LOCATION", transcript)
        self.assertGreater(len(transcript), 60_000)
        self.assertEqual(self.cache.messages[-1], raw[-1])

    async def test_concurrent_append_is_preserved_exactly(self):
        appended = [{"role": "user", "content": "Latest question"},
                    {"role": "assistant", "content": "Latest answer"}]
        self.during_summary = lambda: self.cache.messages.extend(copy.deepcopy(appended))
        await self._compact()
        self.assertEqual(self.cache.messages[-2:], appended)
        self.assertEqual(self.cache.writes, 1)

    async def test_concurrent_head_trim_abandons_replacement(self):
        original = copy.deepcopy(self.cache.messages)
        self.during_summary = lambda: setattr(self.cache, "messages", self.cache.messages[2:])
        await self._compact()
        self.assertEqual(self.cache.messages, original[2:])
        self.assertEqual(self.cache.writes, 0)

    async def test_same_length_edit_with_unchanged_tail_abandons_replacement(self):
        def edit_prefix():
            self.cache.messages[0]["content"] = "Corrected earlier fact"
        self.during_summary = edit_prefix
        await self._compact()
        self.assertEqual(self.cache.messages[0]["content"], "Corrected earlier fact")
        self.assertEqual(self.cache.writes, 0)

    async def test_concurrent_previous_summary_edit_abandons_replacement(self):
        self.cache.messages.insert(0, {"role": "user", "content": SUMMARY_PREFIX + " Earlier fact"})
        def edit_summary():
            self.cache.messages[0]["content"] += " corrected"
        self.during_summary = edit_summary
        await self._compact()
        self.assertTrue(self.cache.messages[0]["content"].endswith("corrected"))
        self.assertEqual(self.cache.writes, 0)

    async def test_failure_or_empty_result_preserves_history(self):
        original = copy.deepcopy(self.cache.messages)
        for result in (RuntimeError("offline summary failure"), "   "):
            with self.subTest(result=type(result).__name__):
                self.summary_result = result
                await self._compact()
                self.assertEqual(self.cache.messages, original)
                self.assertEqual(self.cache.writes, 0)

    async def test_short_history_preserves_existing_summary_without_call(self):
        self.cache.messages = [{"role": "user", "content": SUMMARY_PREFIX + " Earlier fact"}] + _history(6)
        original = copy.deepcopy(self.cache.messages)
        await self._compact()
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.cache.messages, original)

    async def test_legacy_one_turn_limit_retains_latest_pair_without_expansion(self):
        self.cache.messages = _history(4)
        self.namespace["_append_history"]("group:offline", "Latest question", "Latest answer")
        self.assertEqual(self.cache.messages, [
            {"role": "user", "content": "Latest question"},
            {"role": "assistant", "content": "Latest answer"},
        ])

    async def test_legacy_larger_limit_keeps_existing_anchor_behavior(self):
        self.namespace["config"].catty_history_turns = 3
        self.cache.messages = _history(12)
        first_pair = copy.deepcopy(self.cache.messages[:2])
        self.namespace["_append_history"]("group:offline", "Latest question", "Latest answer")
        self.assertEqual(len(self.cache.messages), 6)
        self.assertEqual(self.cache.messages[:2], first_pair)
        self.assertEqual(self.cache.messages[-1]["content"], "Latest answer")


class _NativeStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return types.SimpleNamespace(content=[{"type": "text", "text": "Offline result"}], usage={}, id="offline")


class _NativeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(stream=self.stream))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def stream(self, **kwargs):
        self.payloads.append(copy.deepcopy(kwargs))
        return _NativeStream()


class RequestInputIsolationTests(_IsolatedPackage):
    def setUp(self) -> None:
        super().setUp()
        self.payloads = []
        sdk = types.ModuleType("anthropic")
        sdk.AsyncAnthropic = lambda **kwargs: _NativeClient(self.payloads)
        sys.modules["anthropic"] = sdk
        name = f"{self.package_name}.anthropic_native_client"
        spec = importlib.util.spec_from_file_location(name, ROOT / "anthropic_native_client.py")
        self.native = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.native
        spec.loader.exec_module(self.native)
        # Native diagnostics are local-only too; do not create dump files.
        for method in ("mkdir", "write_text"):
            patched = patch.object(Path, method)
            patched.start()
            self.addCleanup(patched.stop)
        self.openai_namespace = _load_functions("openai_client.py", {"_post_chat_completion_raw"}, {
            "__package__": self.package_name,
            "_logger": logging.getLogger("offline-input-test"),
            "OpenAICompatibleError": RuntimeError,
            "_with_thinking_max_defaults": lambda base, key, model, body: body,
            "normalize_openai_tool_schemas": lambda tools: tools,
            "_prepare_session_context_payload": lambda payload, **kwargs: {},
            "get_current_scope_key": lambda: "",
            "_request_class_for_route": lambda *args: "offline",
            "_next_request_identity": lambda *args, **kwargs: {"logical_turn_id": "offline"},
            "_build_cache_request_diagnostics": lambda **kwargs: {},
            "_current_cache_request_diagnostics_var": contextvars.ContextVar("offline-diagnostics"),
            "_cache_request_dump_enabled": lambda: False,
            "_is_responses_endpoint": lambda base: False,
            "_client_kwargs": lambda *args: {},
            "_chat_completions_url": lambda base: base + "/chat/completions",
            "_log_cache_stats": lambda *args, **kwargs: None,
            "urlparse": urlparse, "json": json, "asyncio": asyncio,
            "httpx": types.SimpleNamespace(AsyncClient=lambda **kwargs: self._http_client()),
        })

    def _http_client(self):
        payloads = self.payloads
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url, *, headers, json):
                payloads.append(copy.deepcopy(json))
                return types.SimpleNamespace(status_code=200, json=lambda: {"choices": [], "usage": {}})
        return Client()

    async def _request(self, provider, messages, tools, **kwargs):
        if provider == "native":
            return await self.native.post_messages_native(
                base_url="https://example.invalid/v1", api_key="offline-test-key",
                model="claude-offline", messages=messages, tools=tools,
                max_tokens=17, enable_compaction=False, **kwargs,
            )
        return await self.openai_namespace["_post_chat_completion_raw"](
            base_url="https://example.invalid/v1", api_key="offline-test-key",
            model="deepseek-offline", messages=messages, tools=tools,
            max_tokens=17, timeout=1, proxy="", temperature=None,
            extra_headers={}, extra_body={}, **kwargs,
        )

    async def test_repeated_requests_leave_nested_inputs_unchanged(self):
        for provider in ("native", "openai"):
            with self.subTest(provider=provider):
                messages = [
                    {"role": "system", "content": "Neutral instruction"},
                    {"role": "user", "content": [{"type": "text", "text": "Earlier question\n[DYN_SYS]\nOld context\n[/DYN_SYS]", "cache_control": {"type": "ephemeral"}}]},
                    {"role": "assistant", "content": "Earlier answer"},
                    {"role": "user", "content": "Latest question"},
                ]
                tools = [{"type": "function", "function": {"name": "read_note", "parameters": {"type": "object", "properties": {}}}, "cache_control": {"type": "ephemeral"}}]
                before = copy.deepcopy((messages, tools))
                await self._request(provider, messages, tools)
                first_payload = self.payloads[-1]
                await self._request(provider, messages, tools)
                self.assertEqual((messages, tools), before)
                self.assertEqual(self.payloads[-1], first_payload)
                self.assertEqual(first_payload["messages"][-1]["content"], "Latest question")
                historical = first_payload["messages"][-3]["content"]
                self.assertNotIn("Old context", json.dumps(historical))

    async def test_tool_pairs_and_latest_message_remain_in_payload(self):
        for provider in ("native", "openai"):
            with self.subTest(provider=provider):
                messages = [
                    {"role": "user", "content": "Read the neutral note"},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call-offline", "type": "function", "function": {"name": "read_note", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "call-offline", "content": "The note says Friday"},
                    {"role": "assistant", "content": "It says Friday"},
                    {"role": "user", "content": "What day was that?"},
                ]
                before = copy.deepcopy(messages)
                await self._request(provider, messages, [])
                wire = self.payloads[-1]["messages"]
                self.assertEqual(messages, before)
                self.assertEqual(wire[-1]["content"], "What day was that?")
                if provider == "native":
                    uses = [b for b in wire[1]["content"] if b.get("type") == "tool_use"]
                    results = [b for b in wire[2]["content"] if b.get("type") == "tool_result"]
                    self.assertEqual(uses[0]["id"], results[0]["tool_use_id"])
                    self.assertEqual(results[0]["content"], "The note says Friday")
                else:
                    self.assertEqual(wire, before)

    async def test_openai_enabled_cache_keeps_single_preparation_copy_per_input(self):
        messages = [{"role": "user", "content": "Neutral latest question"}]
        tools = [{"type": "function", "function": {"name": "read_note"}}]
        original = copy.deepcopy
        top_level_copies = []
        def tracked(value, *args, **kwargs):
            # Count request-list copies too when an earlier branch has already
            # replaced the original list with its own copy.
            if isinstance(value, list) and value and isinstance(value[0], dict):
                if "role" in value[0]:
                    top_level_copies.append("messages")
                elif "function" in value[0]:
                    top_level_copies.append("tools")
            return original(value, *args, **kwargs)
        with patch.object(copy, "deepcopy", side_effect=tracked):
            await self._request("openai", messages, tools, enable_cache=True)
        self.assertEqual(top_level_copies.count("messages"), 1)
        self.assertEqual(top_level_copies.count("tools"), 1)
        self.assertEqual(messages, [{"role": "user", "content": "Neutral latest question"}])


if __name__ == "__main__":
    unittest.main()
