import asyncio
import copy
import importlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_responses_ws_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules[_PACKAGE_NAME] = _PACKAGE


def _load_module(name):
    path = _ROOT / "src" / "catty_qq_ai" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


responses_ws = _load_module("responses_ws")
openai_client = _load_module("openai_client")
prompt_cache = importlib.import_module(f"{_PACKAGE_NAME}.prompt_cache")


class GroupCacheTTLTests(unittest.TestCase):
    def test_three_distinct_turns_promote_and_idle_expires(self):
        tracker = prompt_cache.GroupCacheTTL()
        self.assertEqual(tracker.select("group:TARGET", "one", now=0), "30m")
        self.assertEqual(tracker.select("group:TARGET", "two", now=100), "30m")
        self.assertEqual(tracker.select("group:TARGET", "three", now=200), "12h")
        self.assertEqual(tracker.select("group:TARGET", "cold", now=2000), "30m")

    def test_retries_do_not_count_or_change_the_turn_decision(self):
        tracker = prompt_cache.GroupCacheTTL()
        for timestamp in range(5):
            self.assertEqual(tracker.select("group:TARGET", "one", now=timestamp), "30m")
        self.assertEqual(tracker.select("group:TARGET", "two", now=10), "30m")
        self.assertEqual(tracker.select("group:TARGET", "three", now=20), "12h")
        self.assertEqual(tracker.select("group:TARGET", "one", now=30), "30m")
        self.assertEqual(tracker.select("group:TARGET", "three", now=40), "12h")

    def test_sessions_are_isolated_and_empty_scopes_do_not_count(self):
        tracker = prompt_cache.GroupCacheTTL()
        scopes = ("group:TARGET", "private:TARGET", "group:TARGET:user:OTHER", "session:TARGET")
        for scope in scopes:
            with self.subTest(scope=scope):
                self.assertEqual(tracker.select(scope, "one", now=0), "30m")
                self.assertEqual(tracker.select(scope, "two", now=100), "30m")
                self.assertEqual(tracker.select(scope, "three", now=200), "12h")
                self.assertEqual(tracker.select(scope, "cold", now=2000), "30m")
        for scope in ("", " "):
            self.assertEqual(tracker.select(scope, "one", now=0), "30m")
        self.assertEqual(tracker.select("private:TARGET", "", now=0), "30m")
        self.assertEqual(set(tracker._groups), set(scopes))

    def test_window_excludes_expired_turns_and_bounds_state(self):
        tracker = prompt_cache.GroupCacheTTL()
        tracker.select("group:TARGET", "one", now=0)
        tracker.select("group:TARGET", "two", now=1)
        self.assertEqual(tracker.select("group:TARGET", "three", now=1800), "30m")
        with patch.object(tracker, "MAX_GROUPS", 2), patch.object(tracker, "MAX_TURNS", 3):
            for turn in range(10):
                tracker.select("group:TARGET", str(turn), now=1801)
            self.assertEqual(len(tracker._groups["group:TARGET"]), 3)
            tracker.select("group:OTHER", "one", now=1802)
            tracker.select("group:THIRD", "one", now=1803)
            self.assertEqual(set(tracker._groups), {"group:OTHER", "group:THIRD"})


class GroupCachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tracker = prompt_cache.GroupCacheTTL()
        self.now = 0.0
        select = self.tracker.select
        for patcher in (
            patch.object(openai_client, "_RESPONSES_GROUP_CACHE_TTL", self.tracker),
            patch.object(self.tracker, "select", side_effect=lambda scope, turn: select(scope, turn, now=self.now)),
            patch.object(openai_client, "_get_runtime_config", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.payload = {"model": "gpt-6.1-terra", "messages": [
            {"role": "system", "content": "rules <<<CACHE_BOUNDARY:catty_stable_prefix>>>"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
        ]}

    def _diagnostics(self, *, route="main", request_class="chat", base_url="https://api.openai.com/v1/responses", model="gpt-6.1-terra"):
        identity = openai_client._next_request_identity(
            self.payload["messages"], None, request_route=route, request_class=request_class,
        )
        return openai_client._build_cache_request_diagnostics(
            base_url=base_url, model=model, payload=self.payload,
            request_route=route, request_class=request_class, request_identity=identity,
        )

    def _turn(self, turn, *, scope="group:TARGET", **kwargs):
        scope_token = openai_client.set_current_scope_key(scope)
        turn_token = openai_client.set_current_logical_turn(turn)
        try:
            return self._diagnostics(**kwargs)
        finally:
            openai_client.reset_current_logical_turn(turn_token)
            openai_client.reset_current_scope_key(scope_token)

    def test_request_diagnostics_count_distinct_turns_in_all_chat_sessions(self):
        for scope in ("group:TARGET", "private:TARGET", "group:TARGET:user:OTHER", "session:TARGET"):
            with self.subTest(scope=scope):
                for _ in range(4):
                    self.assertEqual(self._turn("one", scope=scope)["responses_cache_ttl"], "30m")
                self.assertEqual(self._turn("two", scope=scope)["responses_cache_ttl"], "30m")
                self.assertEqual(self._turn("three", scope=scope)["responses_cache_ttl"], "12h")
        self.assertEqual(self._turn("one", scope="group:OTHER")["responses_cache_ttl"], "30m")

    def test_empty_auxiliary_legacy_and_gateway_requests_do_not_count(self):
        cases = (
            {"scope": ""},
            {"scope": " "},
            {"scope": "summary:group:TARGET"},
            {"route": "summary"},
            {"request_class": "auxiliary"},
            {"base_url": "https://api.openai.com/v1/chat/completions"},
            {"base_url": "https://proxy.example/v1/responses"},
            {"model": "gpt-5.5"},
        )
        for case in cases:
            with self.subTest(case=case):
                for turn in ("one", "two", "three"):
                    self.assertNotIn("responses_cache_ttl", self._turn(turn, **case))
        self.assertFalse(self.tracker._groups)

    def test_retry_keeps_bound_ttl_even_after_activity_window_expires(self):
        self._turn("one")
        self._turn("two")
        scope_token = openai_client.set_current_scope_key("group:TARGET")
        turn_token = openai_client.set_current_logical_turn("three")
        try:
            self.assertEqual(self._diagnostics()["responses_cache_ttl"], "12h")
            self.now = 1900
            self.assertEqual(self._diagnostics()["responses_cache_ttl"], "12h")
        finally:
            openai_client.reset_current_logical_turn(turn_token)
            openai_client.reset_current_scope_key(scope_token)
        self.assertEqual(self._turn("cold")["responses_cache_ttl"], "30m")

    def test_only_stable_prefix_gets_long_ttl_in_stream_and_nonstream_bodies(self):
        self._turn("one")
        self._turn("two")
        diagnostics = self._turn("three")
        original = copy.deepcopy(self.payload)
        token = openai_client._current_cache_request_diagnostics_var.set(diagnostics)
        try:
            for streaming in (False, True):
                body = openai_client._payload_to_responses_body(
                    self.payload, stream=streaming, cache_stable_prefix=True,
                )
                marks = [part["prompt_cache_breakpoint"] for item in body["input"]
                         for part in item.get("content", []) if "prompt_cache_breakpoint" in part]
                self.assertEqual([mark.get("ttl", "30m") for mark in marks], ["12h", "30m"])
                self.assertNotIn("prompt_cache_options", body)
                self.assertNotIn("responses_cache_ttl", body)
            legacy = openai_client._payload_to_responses_body(self.payload, stream=False)
            self.assertNotIn("prompt_cache_breakpoint", json.dumps(legacy))
        finally:
            openai_client._current_cache_request_diagnostics_var.reset(token)
        self.assertEqual(self.payload, original)


class ResponseItemHashTests(unittest.TestCase):
    def test_only_supported_content_cache_metadata_is_ignored(self):
        for item_type, field in (("message", "content"), ("function_call_output", "output")):
            for part in (
                {"type": "input_text", "text": "TARGET"},
                {"type": "input_image", "image_url": "https://example.test/image.png", "detail": "low"},
                {"type": "input_file", "file_id": "file_TARGET"},
            ):
                with self.subTest(item_type=item_type, part_type=part["type"]):
                    plain = {"type": item_type, "role": "user", field: [part]}
                    marked = copy.deepcopy(plain)
                    marked[field][0]["prompt_cache_breakpoint"] = {"mode": "explicit"}
                    original = copy.deepcopy(marked)
                    self.assertEqual(responses_ws.item_hash(plain), responses_ws.item_hash(marked))
                    self.assertEqual(marked, original)
                    changed = copy.deepcopy(marked)
                    changed[field][0][next(key for key in part if key != "type")] = "changed"
                    self.assertNotEqual(responses_ws.item_hash(plain), responses_ws.item_hash(changed))
                    changed = {**marked, "role": "developer"}
                    self.assertNotEqual(responses_ws.item_hash(plain), responses_ws.item_hash(changed))

    def test_tool_arguments_and_literal_cache_metadata_are_not_ignored(self):
        for plain in (
            {"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "{}"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "{}"}]},
        ):
            with self.subTest(item_type=plain["type"]):
                changed = copy.deepcopy(plain)
                literal = '{"prompt_cache_breakpoint":"PAYLOAD"}'
                if "arguments" in changed:
                    changed["arguments"] = literal
                elif "output" in changed:
                    changed["output"] = literal
                else:
                    changed["content"][0]["text"] = literal
                self.assertNotEqual(responses_ws.item_hash(plain), responses_ws.item_hash(changed))
                self.assertNotEqual(responses_ws.item_hash(plain), responses_ws.item_hash({
                    **plain, "prompt_cache_breakpoint": "not-a-content-block",
                }))


class ResponsesToolArgumentFormatTests(unittest.TestCase):
    def test_clean_arguments_keep_exact_server_bytes_only_when_requested(self):
        raw = ' {\n "query" : "\\u4f60\\u597d", "limit" : 1\n} '
        calls = [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": raw}}]
        before = copy.deepcopy(calls)
        normalized, specs = openai_client._normalize_assistant_tool_calls(calls, round_idx=0, preserve_argument_format=True)
        self.assertEqual(normalized[0]["function"]["arguments"], raw)
        self.assertEqual(specs[0]["arguments_json"], raw)
        self.assertEqual(calls, before)
        legacy, _ = openai_client._normalize_assistant_tool_calls(calls, round_idx=0)
        self.assertNotEqual(legacy[0]["function"]["arguments"], raw)
        self.assertEqual(json.loads(legacy[0]["function"]["arguments"]), json.loads(raw))

    def test_sanitization_always_takes_priority_over_continuation(self):
        cases = [
            ('{"token":"SECRET_VALUE"}', "SECRET_VALUE"),
            ('{"nested":{"password":"SECRET_VALUE"}}', "SECRET_VALUE"),
            ('{"token":"SECRET_VALUE","token":"<redacted>"}', "SECRET_VALUE"),
            ('{"nested":{"cookie":"SECRET_VALUE","cookie":"<redacted>"}}', "SECRET_VALUE"),
            ('{"payload":"data:text/plain;base64,UEFZTE9BRA=="}', "data:text/plain"),
            ('{"query":"SECRET_VALUE"', "SECRET_VALUE"),
        ]
        for raw, forbidden in cases:
            with self.subTest(raw=raw):
                result = openai_client._serialize_tool_arguments_for_history(raw, preserve_format=True)
                self.assertNotEqual(result, raw)
                self.assertNotIn(forbidden, result)
                json.loads(result)

    def test_duplicate_keys_and_oversized_whitespace_use_bounded_projection(self):
        for raw in ('{"n":1,"n":2}', '{"nested":{"n":1,"n":2}}', " " * 4100 + '{"query":"TARGET"}'):
            with self.subTest(length=len(raw)):
                result = openai_client._serialize_tool_arguments_for_history(raw, preserve_format=True)
                self.assertNotEqual(result, raw)
                self.assertEqual(json.loads(result), json.loads(raw))
                self.assertLessEqual(len(result), openai_client._TOOL_HISTORY_MAX_ARGUMENT_JSON_CHARS)


class ResponsesWSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frames: list[dict] = []
        self.received = asyncio.Event()

    async def _serve(self, handler):
        server = await serve(handler, "127.0.0.1", 0)
        self.addAsyncCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        return server, port

    async def _run_real_tool_loop(self, arguments, *, change_tools=False, max_rounds=2):
        frames = []
        executed = []
        provider_calls = []

        class FakeTransport:
            generation = 1

            async def ensure_ready(self):
                return self.generation

            async def iter_events(self, body, *, stream_id, previous_response_id):
                frames.append((copy.deepcopy(body), previous_response_id))
                output = [{"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": arguments}] if len(frames) == 1 else [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
                ]
                yield {"type": "response.completed", "response": {"id": f"resp_{len(frames)}", "status": "completed", "output": output}}

        def schemas(names):
            return [{"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}} for name in names]

        names = ["lookup", "catty_skill_search"]
        tools = schemas(names)

        def tools_provider():
            provider_calls.append(True)
            return schemas(names + (["catty_skill_load"] if change_tools and len(provider_calls) > 1 else []))

        async def executor(name, raw):
            executed.append((name, raw))
            return {"status": "ok", "value": "TARGET"}

        request = importlib.import_module(f"{_PACKAGE_NAME}.request_intent")
        harness = importlib.import_module(f"{_PACKAGE_NAME}.harness_prompt")
        messages = [
            {"role": "system", "content": "stable rules <<<CACHE_BOUNDARY:catty_stable_prefix>>>"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
            {"role": "system", "content": "【TARGET·evidence】\nstate one"},
        ]
        harness.append_harness_agent_hint(messages, names)
        request.append_context_request_hint(messages, names)
        before = copy.deepcopy((messages, tools))
        config = openai_client.Config(
            catty_openai_base_url="https://api.openai.com/v1/responses",
            catty_openai_model="gpt-6.1-terra", catty_openai_api_key="TOKEN",
            catty_anthropic_native_enabled=False, catty_tools_enabled=True,
            catty_openai_websocket_enabled=True, catty_openai_websocket_store=False,
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_fallback_http=False,
        )
        scope_token = openai_client.set_current_scope_key("private:1")
        openai_client._reset_cloud_health_for_tests()
        try:
            with tempfile.TemporaryDirectory() as directory:
                cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
                with (
                    patch.object(openai_client, "_get_runtime_config", return_value=config),
                    patch.object(openai_client, "_get_ws_transport", return_value=FakeTransport()),
                    patch.object(openai_client, "_get_ws_chain_cache", return_value=cache),
                    patch.object(openai_client, "_http_client", side_effect=AssertionError("unexpected HTTP")),
                    patch.object(openai_client, "_log_cache_stats"),
                    patch.object(openai_client, "_maybe_distill_reply"),
                    patch.object(openai_client, "_cache_request_dump_enabled", return_value=False),
                ):
                    result = await openai_client.chat_completion_with_tools(
                        config, messages, tools=tools, tool_executor=executor,
                        tools_provider=tools_provider, max_rounds=max_rounds,
                    )
        finally:
            openai_client.reset_current_scope_key(scope_token)
            openai_client._reset_cloud_health_for_tests()
        self.assertEqual(result, "done")
        self.assertEqual(executed, [("lookup", arguments)])
        self.assertEqual(len(provider_calls), max_rounds)
        self.assertEqual((messages, tools), before)
        self.assertEqual(len(frames), 2)
        return frames

    async def test_real_tool_loop_sends_only_new_result_and_followup_hint(self):
        frames = await self._run_real_tool_loop(' {\n "query" : "\\u4f60\\u597d"\n} ')
        self.assertIsNone(frames[0][1])
        self.assertEqual(frames[1][1], "resp_1")
        self.assertEqual([item["type"] for item in frames[1][0]["input"]], ["function_call_output", "message"])
        self.assertEqual(frames[1][0]["input"][1]["role"], "developer")
        self.assertFalse(any(item.get("role") == "user" for item in frames[1][0]["input"]))
        self.assertEqual(frames[0][0]["input"][-2]["content"][0]["text"], "current question")
        self.assertEqual(frames[0][0]["input"][-1]["content"][0]["text"], "【TARGET·evidence】\nstate one")

    async def test_tool_limit_final_frame_keeps_schemas_but_disables_calls(self):
        frames = await self._run_real_tool_loop(' { "query": "TARGET" } ', max_rounds=1)
        first, final = frames[0][0], frames[1][0]
        self.assertEqual(final["tools"], first["tools"])
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual(final["tool_choice"], "none")
        self.assertEqual(frames[1][1], "resp_1")
        self.assertEqual([item["type"] for item in final["input"]], ["function_call_output", "message", "message"])
        self.assertIn("不再调用工具", final["input"][-1]["content"][0]["text"])
        self.assertFalse(any(item.get("role") == "user" for item in final["input"]))

    async def test_real_tool_loop_replays_for_redaction_or_new_capabilities(self):
        for arguments, changed in (('{"token":"SECRET_VALUE"}', False), (' { "query": "TARGET" } ', True)):
            with self.subTest(redacted=not changed):
                frames = await self._run_real_tool_loop(arguments, change_tools=changed)
                self.assertIsNone(frames[1][1])
                self.assertNotIn("SECRET_VALUE", json.dumps(frames[1][0]))
                if changed:
                    self.assertIn("本轮提供 catty_skill_load", frames[1][0]["input"][0]["content"][0]["text"])
                else:
                    calls = [item for item in frames[1][0]["input"] if item["type"] == "function_call"]
                    self.assertIn("<redacted>", calls[0]["arguments"])

    async def test_rolling_history_markers_keep_append_only_continuation(self):
        frames = []

        class FakeTransport:
            generation = 1

            async def ensure_ready(self):
                return self.generation

            async def iter_events(self, body, *, stream_id, previous_response_id):
                frames.append((copy.deepcopy(body), previous_response_id))
                yield {
                    "type": "response.completed",
                    "response": {
                        "id": f"resp_{len(frames)}",
                        "output": [{"type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": "ok"},
                        ]}],
                    },
                }

        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        messages = [{"role": "system", "content": "rules <<<CACHE_BOUNDARY:catty_stable_prefix>>>"}]
        for number in range(4):
            messages.extend([
                {"role": "user", "content": f"history {number}"},
                {"role": "assistant", "content": "ok"},
            ])
        with tempfile.TemporaryDirectory() as directory:
            cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
            with patch.object(openai_client, "_get_ws_transport", return_value=FakeTransport()):
                with patch.object(openai_client, "_get_ws_chain_cache", return_value=cache):
                    for turn in range(4):
                        messages.append({"role": "user", "content": f"current {turn}"})
                        await openai_client._stream_responses_ws_attempt(
                            config=config, base_url="https://api.openai.com/v1/responses",
                            api_key="TOKEN", extra_headers={},
                            payload={"model": "gpt-6.1-terra", "messages": messages},
                            timeout=5, proxy="", dash_stream_id=None, dash_mod=None, scope="private:1",
                        )
                        messages.append({"role": "assistant", "content": "ok"})
        self.assertIsNone(frames[0][1])
        for turn in range(1, 4):
            self.assertEqual(frames[turn][1], f"resp_{turn}")
            self.assertEqual(frames[turn][0]["input"], [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": f"current {turn}"}],
            }])

    async def test_dynamic_ttl_switch_replays_once_then_resumes_incremental_input(self):
        await self._assert_dynamic_ttl_switch_for_scope("group:TARGET")

    async def test_private_and_custom_sessions_keep_dynamic_ttl_continuation(self):
        for scope in ("private:TARGET", "group:TARGET:user:OTHER", "session:TARGET"):
            with self.subTest(scope=scope):
                await self._assert_dynamic_ttl_switch_for_scope(scope)

    async def _assert_dynamic_ttl_switch_for_scope(self, session_scope):
        frames = []

        class FakeTransport:
            generation = 1

            async def ensure_ready(self):
                return self.generation

            async def iter_events(self, body, *, stream_id, previous_response_id):
                frames.append((copy.deepcopy(body), previous_response_id))
                yield {"type": "response.completed", "response": {
                    "id": f"resp_{len(frames)}",
                    "output": [{"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": "ok"},
                    ]}],
                }}

        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        tracker = prompt_cache.GroupCacheTTL()
        select = tracker.select
        timestamp = 0
        messages = [{"role": "system", "content": "rules <<<CACHE_BOUNDARY:catty_stable_prefix>>>"}]
        scope_token = openai_client.set_current_scope_key(session_scope)
        try:
            with tempfile.TemporaryDirectory() as directory:
                cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
                with (
                    patch.object(openai_client, "_get_ws_transport", return_value=FakeTransport()),
                    patch.object(openai_client, "_get_ws_chain_cache", return_value=cache),
                    patch.object(openai_client, "_get_runtime_config", return_value=None),
                    patch.object(openai_client, "_RESPONSES_GROUP_CACHE_TTL", tracker),
                    patch.object(tracker, "select", side_effect=lambda scope, turn: select(scope, turn, now=timestamp)),
                ):
                    for turn, timestamp in enumerate((0, 100, 200, 300, 2200, 2300)):
                        messages.append({"role": "user", "content": f"current {turn}"})
                        payload = {"model": "gpt-6.1-terra", "messages": messages}
                        turn_token = openai_client.set_current_logical_turn(f"turn-{turn}")
                        try:
                            identity = openai_client._next_request_identity(messages, None)
                            diagnostics = openai_client._build_cache_request_diagnostics(
                                base_url="https://api.openai.com/v1/responses", model="gpt-6.1-terra",
                                payload=payload, request_identity=identity,
                            )
                            token = openai_client._current_cache_request_diagnostics_var.set(diagnostics)
                            try:
                                await openai_client._stream_responses_ws_attempt(
                                    config=config, base_url="https://api.openai.com/v1/responses",
                                    api_key="TOKEN", extra_headers={}, payload=payload,
                                    timeout=5, proxy="", dash_stream_id=None, dash_mod=None, scope=session_scope,
                                )
                            finally:
                                openai_client._current_cache_request_diagnostics_var.reset(token)
                        finally:
                            openai_client.reset_current_logical_turn(turn_token)
                        messages.append({"role": "assistant", "content": "ok"})
        finally:
            openai_client.reset_current_scope_key(scope_token)
        self.assertEqual([previous for _, previous in frames], [None, "resp_1", None, "resp_3", None, "resp_5"])
        for index, ttl in ((0, "30m"), (2, "12h"), (4, "30m")):
            self.assertEqual(frames[index][0]["input"][0]["content"][0]["prompt_cache_breakpoint"]["ttl"], ttl)
        for index in (1, 3, 5):
            self.assertEqual(len(frames[index][0]["input"]), 1)
            self.assertEqual(frames[index][0]["input"][0]["role"], "user")

    async def test_explicit_cache_wire_and_previous_response_continuation(self):
        frames = []

        class FakeTransport:
            generation = 1

            async def ensure_ready(self):
                return self.generation

            async def iter_events(self, body, *, stream_id, previous_response_id):
                frames.append((json.loads(json.dumps(body)), previous_response_id))
                yield {
                    "type": "response.completed",
                    "response": {
                        "id": f"resp_{len(frames)}",
                        "output": [{
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }],
                    },
                }

        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        arguments = dict(
            config=config,
            base_url="https://api.openai.com/v1/responses",
            api_key="TOKEN",
            extra_headers={},
            timeout=5,
            proxy="",
            dash_stream_id=None,
            dash_mod=None,
            scope="private:1",
        )
        first_messages = [
            {"role": "system", "content": "stable <<<CACHE_BOUNDARY:catty_stable_prefix>>>"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "context one"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
            with patch.object(openai_client, "_get_ws_transport", return_value=FakeTransport()):
                with patch.object(openai_client, "_get_ws_chain_cache", return_value=cache):
                    await openai_client._stream_responses_ws_attempt(
                        payload={"model": "gpt-6.1-terra", "messages": first_messages},
                        **arguments,
                    )
                    await openai_client._stream_responses_ws_attempt(
                        payload={"model": "gpt-6.1-terra", "messages": first_messages + [
                            {"role": "assistant", "content": "ok"},
                            {"role": "user", "content": "next"},
                        ]},
                        **arguments,
                    )
                    await openai_client._stream_responses_ws_attempt(
                        payload={"model": "gpt-6.1-terra", "messages": [
                            first_messages[0], first_messages[1],
                            {"role": "assistant", "content": "ok"},
                            {"role": "user", "content": "next"},
                            {"role": "system", "content": "context two"},
                        ]},
                        **arguments,
                    )
        self.assertEqual(
            frames[0][0]["input"][0]["content"][0]["prompt_cache_breakpoint"],
            {"mode": "explicit"},
        )
        self.assertIsNone(frames[0][1])
        self.assertEqual(frames[1][1], "resp_1")
        self.assertEqual([item["role"] for item in frames[1][0]["input"]], ["user"])
        self.assertIsNone(frames[2][1])
        self.assertEqual(frames[0][0]["input"][0], frames[2][0]["input"][0])

    async def test_stream_events_and_frame_shape(self):
        async def handler(ws):
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                lane = frame.get("stream_id")
                extra = {"stream_id": lane} if lane else {}
                await ws.send(json.dumps({
                    **extra,
                    "type": "response.created",
                    "response": {"id": "resp_1"},
                }))
                await ws.send(json.dumps({
                    **extra,
                    "type": "response.output_text.delta",
                    "delta": "hello",
                }))
                await ws.send(json.dumps({
                    **extra,
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "output": [{
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "hello"}],
                        }],
                    },
                }))
                self.received.set()

        _, port = await self._serve(handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)
        events = [
            event async for event in transport.iter_events(
                {
                    "model": "gpt-6.1-terra",
                    "store": False,
                    "input": [{
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                    }],
                },
                stream_id="catty-test",
            )
        ]
        await asyncio.wait_for(self.received.wait(), timeout=5)
        self.assertEqual(self.frames[0]["type"], "response.create")
        self.assertEqual(self.frames[0]["model"], "gpt-6.1-terra")
        self.assertFalse(self.frames[0]["store"])
        self.assertEqual(self.frames[0]["stream_id"], "catty-test")
        self.assertEqual([event["type"] for event in events], [
            "response.created",
            "response.output_text.delta",
            "response.completed",
        ])

    async def test_previous_response_id_is_forwarded_and_default_lane_routes(self):
        async def handler(ws):
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                await ws.send(json.dumps({
                    "type": "response.completed",
                    "response": {"id": "resp_2", "output": []},
                }))
                self.received.set()

        _, port = await self._serve(handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)
        events = [
            event async for event in transport.iter_events(
                {"model": "gpt-6.1-terra", "store": False, "input": []},
                previous_response_id="resp_1",
            )
        ]
        await asyncio.wait_for(self.received.wait(), timeout=5)
        self.assertEqual(self.frames[0]["previous_response_id"], "resp_1")
        self.assertNotIn("stream_id", self.frames[0])
        self.assertEqual(events[-1]["response"]["id"], "resp_2")

    async def test_cancelled_request_cannot_deliver_a_late_response_to_next_turn(self):
        first_received = asyncio.Event()
        connections = 0

        async def handler(ws):
            nonlocal connections
            connections += 1
            connection = connections
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                lane = frame["stream_id"]
                if len(self.frames) == 1:
                    await ws.send(json.dumps({
                        "stream_id": lane,
                        "type": "response.created",
                        "response": {"id": "old"},
                    }))
                    continue
                if connection == 1:
                    await ws.send(json.dumps({
                        "stream_id": lane,
                        "type": "response.completed",
                        "response": {"id": "old", "output": []},
                    }))
                await ws.send(json.dumps({
                    "stream_id": lane,
                    "type": "response.completed",
                    "response": {"id": "new", "output": []},
                }))

        _, port = await self._serve(handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)

        async def first_turn():
            async for _event in transport.iter_events(
                {"model": "test", "input": []}, stream_id="same-lane"
            ):
                first_received.set()

        task = asyncio.create_task(first_turn())
        await asyncio.wait_for(first_received.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        events = [
            event async for event in transport.iter_events(
                {"model": "test", "input": []}, stream_id="same-lane"
            )
        ]
        self.assertEqual(events[-1]["response"]["id"], "new")
        self.assertEqual(connections, 2)

    async def test_distinct_transports_do_not_reuse_connection_generation(self):
        async def handler(ws):
            async for _raw in ws:
                pass

        _, port = await self._serve(handler)
        first = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses", api_key="TOKEN"
        )
        second = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses", api_key="TOKEN"
        )
        self.addAsyncCleanup(first.close)
        self.addAsyncCleanup(second.close)
        first_generation = await first.ensure_ready()
        second_generation = await second.ensure_ready()
        self.assertNotEqual(first_generation, second_generation)

    async def test_response_is_not_cached_under_a_replacement_connection(self):
        class SwitchingTransport:
            generation = 1

            async def ensure_ready(self):
                return self.generation

            async def iter_events(self, body, *, stream_id, previous_response_id):
                self.generation = 2
                yield {
                    "type": "response.completed",
                    "response": {"id": "old-connection", "output": []},
                }

        transport = SwitchingTransport()
        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
            with patch.object(openai_client, "_get_ws_transport", return_value=transport):
                with patch.object(openai_client, "_get_ws_chain_cache", return_value=cache):
                    await openai_client._stream_responses_ws_attempt(
                        config=config,
                        base_url="https://example.test/v1/responses",
                        api_key="TOKEN",
                        extra_headers={},
                        payload={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                        timeout=5,
                        proxy="",
                        dash_stream_id=None,
                        dash_mod=None,
                        scope="private:1",
                    )
            self.assertIsNone(cache.get("private:1"))

    async def test_expired_previous_response_replays_without_lane_deadlock(self):
        async def handler(ws):
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                lane = frame["stream_id"]
                if len(self.frames) == 2:
                    await ws.send(json.dumps({
                        "stream_id": lane,
                        "type": "error",
                        "status": 400,
                        "error": {"code": "previous_response_not_found"},
                    }))
                    continue
                await ws.send(json.dumps({
                    "stream_id": lane,
                    "type": "response.completed",
                    "response": {
                        "id": f"resp_{len(self.frames)}",
                        "output": [{
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }],
                    },
                }))

        _, port = await self._serve(handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            idle_timeout=5,
        )
        self.addAsyncCleanup(transport.close)
        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        arguments = dict(
            config=config,
            base_url="https://example.test/v1/responses",
            api_key="TOKEN",
            extra_headers={},
            timeout=5,
            proxy="",
            dash_stream_id=None,
            dash_mod=None,
            scope="private:1",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
            with patch.object(openai_client, "_get_ws_transport", return_value=transport):
                with patch.object(openai_client, "_get_ws_chain_cache", return_value=cache):
                    first = {"model": "test", "messages": [{"role": "user", "content": "hi"}]}
                    await openai_client._stream_responses_ws_attempt(payload=first, **arguments)
                    second = {
                        "model": "test",
                        "messages": first["messages"] + [
                            {"role": "assistant", "content": "ok"},
                            {"role": "user", "content": "next"},
                        ],
                    }
                    result = await asyncio.wait_for(
                        openai_client._stream_responses_ws_attempt(payload=second, **arguments),
                        timeout=2,
                    )
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(self.frames[1]["previous_response_id"], "resp_1")
        self.assertNotIn("previous_response_id", self.frames[2])
        self.assertEqual(len(self.frames[2]["input"]), 3)

    async def test_disconnect_raises_and_error_frame_is_yielded(self):
        async def closing_handler(ws):
            async for _raw in ws:
                await ws.close()
                return

        _, port = await self._serve(closing_handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)
        with self.assertRaises(responses_ws.ResponsesWSDisconnected):
            async for _event in transport.iter_events(
                {"model": "gpt-6.1-terra", "input": []},
                stream_id="drop",
            ):
                pass

        async def error_handler(ws):
            async for raw in ws:
                frame = json.loads(raw)
                lane = frame.get("stream_id")
                extra = {"stream_id": lane} if lane else {}
                await ws.send(json.dumps({
                    **extra,
                    "type": "error",
                    "status": 400,
                    "error": {
                        "code": "previous_response_not_found",
                        "message": "Previous response not found",
                    },
                }))
                self.received.set()

        _, port = await self._serve(error_handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)
        events = [
            event async for event in transport.iter_events(
                {"model": "gpt-6.1-terra", "input": []},
                stream_id="err",
            )
        ]
        await asyncio.wait_for(self.received.wait(), timeout=5)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["error"]["code"], "previous_response_not_found")

    async def test_chain_cache_round_trip_and_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = responses_ws.ResponseChainCache(path, 60)
            cache.put(
                "private:1",
                response_id="resp_1",
                item_hashes=["a", "b"],
                generation=3,
                store=False,
            )
            entry = cache.get("private:1")
            self.assertEqual(entry["response_id"], "resp_1")
            self.assertEqual(entry["item_hashes"], ["a", "b"])
            self.assertFalse(entry["store"])

            loaded = responses_ws.ResponseChainCache(path, 60)
            self.assertEqual(loaded.get("private:1")["response_id"], "resp_1")
            loaded.clear("private:1")
            self.assertIsNone(loaded.get("private:1"))

    def test_ws_url_and_prefix(self):
        self.assertEqual(
            responses_ws.responses_ws_url("https://api.openai.com/v1/responses"),
            "wss://api.openai.com/v1/responses",
        )
        self.assertEqual(
            responses_ws.responses_ws_url("https://api.openai.com/v1/chat/completions"),
            "wss://api.openai.com/v1/responses",
        )
        self.assertEqual(
            responses_ws.responses_ws_url("http://127.0.0.1:11434/v1"),
            "ws://127.0.0.1:11434/v1/responses",
        )
        self.assertEqual(
            responses_ws.common_prefix_len(["a", "b", "c"], ["a", "b"]),
            2,
        )
        self.assertEqual(
            responses_ws.common_prefix_len(["a"], ["b"]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
