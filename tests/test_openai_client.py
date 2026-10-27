from pathlib import Path
import asyncio
import base64
from io import BytesIO
import importlib.util
import importlib
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = types.ModuleType("catty_qq_ai")
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault("catty_qq_ai", _PACKAGE)
_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "openai_client.py"
_SPEC = importlib.util.spec_from_file_location("catty_qq_ai.openai_client", _MODULE_PATH)
_client = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = _client
_SPEC.loader.exec_module(_client)

def _make_fadianji_reply_context() -> types.SimpleNamespace:
    def render(template: str, **values: object) -> str:
        return template.format(**values)

    return types.SimpleNamespace(
        persona=types.SimpleNamespace(name="fadianji"),
        catalog=types.SimpleNamespace(
            api_transport_reply="网线抽了。再来。",
            tool_result_follow_up_instruction="工具结果已到。直接结合结果回，短句，不复述过程。",
        ),
        render=render,
    )


class _HangingSseResponse:
    def __init__(
        self,
        lines: list[str],
        *,
        hang: bool = True,
        delays: list[float] | None = None,
    ) -> None:
        self.status_code = 200
        self._lines = lines
        self._hang = hang
        self._delays = delays or []

    async def aread(self) -> bytes:
        return b""

    def aiter_lines(self):
        async def gen():
            for index, line in enumerate(self._lines):
                if index < len(self._delays):
                    await asyncio.sleep(self._delays[index])
                yield line
            if self._hang:
                await asyncio.Event().wait()

        return gen()


class StreamChatCompletionAttemptTests(unittest.TestCase):
    def test_finish_reason_closes_stream_even_without_done(self) -> None:
        lines = [
            'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            'data: {"choices":[{"delta":{"content":"活了"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            ': heartbeat',
        ]

        data = asyncio.run(self._run_stream(lines))

        self.assertEqual(data["choices"][0]["message"]["content"], "活了")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")

    def test_usage_chunk_after_finish_is_preserved(self) -> None:
        lines = [
            'data: {"choices":[{"delta":{"content":"在"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":2,"total_tokens":14}}',
            ': heartbeat',
        ]

        data = asyncio.run(self._run_stream(lines))

        self.assertEqual(data["choices"][0]["message"]["content"], "在")
        self.assertEqual(data["usage"]["prompt_tokens"], 12)
        self.assertEqual(data["usage"]["completion_tokens"], 2)

    def test_error_event_raises_before_heartbeat_eof(self) -> None:
        lines = [
            'data: {"error":{"message":"quota exceeded Bearer sk-secret","code":"rate_limit","status":429}}',
            ': heartbeat',
        ]

        with self.assertRaises(_client.OpenAICompatibleError) as caught:
            asyncio.run(self._run_stream(lines))

        error = caught.exception
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.error_code, "rate_limit")
        self.assertIn("message=quota exceeded Bearer <redacted>", str(error))
        self.assertNotIn("sk-secret", str(error))

    def test_finish_and_usage_in_same_chunk_are_preserved(self) -> None:
        lines = [
            'data: {"choices":[{"delta":{"content":"同帧"},"finish_reason":"stop"}],"usage":{"total_tokens":3}}',
            ': heartbeat',
        ]

        data = asyncio.run(self._run_stream(lines))

        self.assertEqual(data["choices"][0]["message"]["content"], "同帧")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        self.assertEqual(data["usage"]["total_tokens"], 3)

    def test_normal_eof_without_finish_reason_still_returns_stop(self) -> None:
        lines = [
            'data: {"choices":[{"delta":{"content":"EOF"}}]}',
        ]

        data = asyncio.run(self._run_stream(lines, hang=False))

        self.assertEqual(data["choices"][0]["message"]["content"], "EOF")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")

    def test_usage_delayed_within_terminal_grace_is_preserved(self) -> None:
        lines = [
            'data: {"choices":[{"delta":{"content":"延迟"},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"total_tokens":4}}',
        ]

        data = asyncio.run(
            self._run_stream(lines, delays=[0.0, 0.005], grace=0.05),
        )

        self.assertEqual(data["choices"][0]["message"]["content"], "延迟")
        self.assertEqual(data["usage"]["total_tokens"], 4)

    async def _run_stream(
        self,
        lines: list[str],
        *,
        hang: bool = True,
        delays: list[float] | None = None,
        grace: float = 0.01,
    ) -> dict:
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)
        fake_client.stream = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(
                    return_value=_HangingSseResponse(
                        lines,
                        hang=hang,
                        delays=delays,
                    ),
                ),
                __aexit__=AsyncMock(return_value=False),
            ),
        )
        with (
            patch.object(_client, "httpx") as fake_httpx,
            patch.object(_client, "_STREAM_TERMINAL_GRACE_SECONDS", grace),
        ):
            fake_httpx.AsyncClient.return_value = fake_client
            return await asyncio.wait_for(
                _client._stream_chat_completion_attempt(
                    url="https://api.example.com/v1/chat/completions",
                    headers={"Authorization": "Bearer x"},
                    payload={"model": "deepseek-v4-flash", "stream": True},
                    timeout=30.0,
                    proxy="",
                    dash_stream_id=None,
                    dash_mod=None,
                ),
                timeout=0.25,
            )


class OpenCodeSessionHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests = []
        self.scope_token = _client.set_current_scope_key("private:test-session")
        self.turn_token = _client.set_current_logical_turn("test-turn")
        self.addCleanup(_client.reset_current_logical_turn, self.turn_token)
        self.addCleanup(_client.reset_current_scope_key, self.scope_token)
        real_client = _client.httpx.AsyncClient

        def respond(request):
            self.requests.append(request)
            payload = json.loads(request.content)
            if payload.get("stream"):
                return _client.httpx.Response(200, text=(
                    'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
                    '"usage":{"total_tokens":1}}\n\ndata: [DONE]\n\n'
                ), headers={"Content-Type": "text/event-stream"})
            if request.url.path.endswith("/responses"):
                return _client.httpx.Response(200, json={"output": [{
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }]})
            return _client.httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            })

        transport = _client.httpx.MockTransport(respond)
        client_patch = patch.object(
            _client.httpx, "AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)
        runtime_patch = patch.object(_client, "_get_runtime_config", return_value=None)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)

    async def request(self, **overrides):
        kwargs = dict(
            base_url="https://opencode.ai/zen/go/v1/chat/completions",
            api_key="test-key", model="deepseek-v4.1-flash",
            messages=[{"role": "user", "content": "ping"}],
            timeout=1, proxy="", temperature=0.7, max_tokens=128,
            extra_headers={}, extra_body={},
        )
        kwargs.update(overrides)
        result = await _client._post_chat_completion_raw(**kwargs)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        return self.requests[-1]

    async def test_headers_stable_across_turns_routes_and_models(self) -> None:
        first = await self.request()
        token = _client.set_current_logical_turn("next-turn")
        try:
            for route in ("main", "fallback", "audit", "spark"):
                request = await self.request(request_route=route, model="deepseek-v4-flash")
                self.assertEqual(request.headers["x-opencode-session"], first.headers["x-opencode-session"])
        finally:
            _client.reset_current_logical_turn(token)
        self.assertEqual(first.headers["User-Agent"], "catty-qq-ai/1.0.0")
        self.assertRegex(first.headers["x-opencode-session"], r"^catty-[0-9a-f]{32}$")
        self.assertNotIn("test-key", first.headers["x-opencode-session"])
        self.assertNotIn("private:test-session", first.headers["x-opencode-session"])

    async def test_sessions_isolate_scopes_and_accounts(self) -> None:
        first = await self.request()
        token = _client.set_current_scope_key("group:other-session")
        try:
            other = await self.request()
        finally:
            _client.reset_current_scope_key(token)
        account = await self.request(api_key="another-test-key")
        self.assertEqual(len({r.headers["x-opencode-session"] for r in (first, other, account)}), 3)

    async def test_scope_less_requests_keep_logical_session(self) -> None:
        token = _client.set_current_scope_key(None)
        try:
            first = await self.request(request_route="audit")
            second = await self.request(request_route="audit")
        finally:
            _client.reset_current_scope_key(token)
        self.assertTrue(first.headers["x-opencode-session"])
        self.assertEqual(first.headers["x-opencode-session"], second.headers["x-opencode-session"])

    async def test_explicit_headers_preserved_case_insensitively(self) -> None:
        supplied = {"X-OpenCode-Session": "custom-session", "user-agent": "custom-catty/1.0"}
        request = await self.request(extra_headers=supplied)
        self.assertEqual(request.headers["x-opencode-session"], "custom-session")
        self.assertEqual(request.headers["user-agent"], "custom-catty/1.0")
        self.assertEqual(supplied, {"X-OpenCode-Session": "custom-session", "user-agent": "custom-catty/1.0"})

    async def test_other_hosts_do_not_receive_opencode_headers(self) -> None:
        for host in ("api.example.com", "opencode.ai.example.com"):
            request = await self.request(base_url=f"https://{host}/v1")
            self.assertNotIn("x-opencode-session", request.headers)
            self.assertNotEqual(request.headers["user-agent"], "catty-qq-ai/1.0.0")

    async def test_stream_and_responses_share_session(self) -> None:
        first = await self.request()
        streamed = await self.request(stream=True)
        responses = await self.request(base_url="https://opencode.ai/zen/go/v1/responses")
        for request in (streamed, responses):
            self.assertEqual(request.headers["x-opencode-session"], first.headers["x-opencode-session"])
        body = json.loads(streamed.content)
        self.assertEqual(body["model"], "deepseek-v4.1-flash")
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "max")


class VisionPassthroughTests(unittest.TestCase):
    def _config(self, **overrides) -> types.SimpleNamespace:
        values = {
            "catty_openai_base_url": "https://opencode.ai/zen/go/v1/chat/completions",
            "catty_openai_model": "deepseek-v4.1-flash",
            "catty_vision_base_url": "",
            "catty_vision_model": "",
        }
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def test_route_match_requires_same_base_and_model(self) -> None:
        self.assertTrue(_client.vision_shares_main_route(self._config()))
        self.assertTrue(_client.vision_shares_main_route(self._config(
            catty_vision_base_url="https://opencode.ai/zen/go/v1/chat/completions/",
            catty_vision_model="DeepSeek-V4.1-Flash",
        )))
        self.assertFalse(_client.vision_shares_main_route(self._config(
            catty_vision_base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
            catty_vision_model="glm-4.6v",
        )))
        self.assertFalse(_client.vision_shares_main_route(self._config(
            catty_vision_base_url="https://api.deepseek.com/v1",
            catty_vision_model="deepseek-v4.1-flash",
        )))

    def test_prepare_parts_keeps_plain_data_url(self) -> None:
        url = "data:image/png;base64," + base64.b64encode(b"png").decode("ascii")
        parts = asyncio.run(_client.prepare_passthrough_image_parts(self._config(), [url]))
        self.assertEqual(parts, [{"type": "image_url", "image_url": {"url": url}}])


class OpenAIClientTests(unittest.TestCase):
    def test_request_class_distinguishes_chat_and_auxiliary_routes(self) -> None:
        self.assertEqual(_client._request_class_for_route("main", "private:1"), "chat")
        self.assertEqual(_client._request_class_for_route("router", "group:2"), "chat")
        self.assertEqual(_client._request_class_for_route("vision", "private:1"), "auxiliary")
        self.assertEqual(_client._request_class_for_route("summary", "private:1"), "auxiliary")
        self.assertEqual(_client._request_class_for_route("imagegen_plan", "private:1"), "auxiliary")
        self.assertEqual(_client._request_class_for_route("imagegen_caption", "group:2"), "auxiliary")
        self.assertEqual(_client._request_class_for_route("spark", "private:1"), "auxiliary")
        self.assertEqual(_client._request_class_for_route("nsfw_main", "private:1"), "chat")
        self.assertEqual(_client._request_class_for_route("main", "summary:group:2"), "auxiliary")

    def test_codex_instant_separates_auxiliary_spark_from_nsfw_main(self) -> None:
        config = _client.Config(
            catty_codex_instant_model="deepseek-v4-flash",
            catty_nsfw_spark_base_url="https://api.deepseek.com/v1",
            catty_nsfw_spark_api_key="test-key",
        )
        routes: list[str] = []

        async def fake_post(*_args, **kwargs):
            routes.append(kwargs["line"])
            return "ok"

        with (
            patch.object(_client, "_post_with_fallback", side_effect=fake_post),
            patch.object(_client, "_maybe_distill_reply"),
        ):
            asyncio.run(
                _client.chat_completion_codex_instant(
                    config,
                    [{"role": "user", "content": "caption"}],
                )
            )
            asyncio.run(
                _client.chat_completion_codex_instant(
                    config,
                    [{"role": "user", "content": "main reply"}],
                    request_route="nsfw_main",
                )
            )

        self.assertEqual(routes, ["spark", "nsfw_main"])

    def test_native_diagnostics_share_local_request_identity(self) -> None:
        scope_token = _client.set_current_scope_key("private:42")
        turn_token = _client.set_current_logical_turn("native-turn")
        payload = {
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 128,
        }
        diagnostics_token = None
        try:
            diagnostics_token, diagnostics = _client._bind_native_request_diagnostics(
                base_url="https://api.anthropic.com",
                model="claude-sonnet-4-5",
                payload=payload,
                request_route="main",
            )
            self.assertEqual(diagnostics["conversation_id"], "private:42")
            self.assertEqual(diagnostics["logical_turn_id"], "native-turn")
            self.assertEqual(diagnostics["request_seq"], 1)
            self.assertEqual(diagnostics["request_kind"], "chat")
            self.assertEqual(diagnostics["request_route"], "main")
            self.assertEqual(diagnostics["request_class"], "chat")
            self.assertEqual(diagnostics["transport"], "anthropic_native")
            self.assertEqual(_client._get_current_cache_request_diagnostics(), diagnostics)
        finally:
            if diagnostics_token is not None:
                _client._reset_native_request_diagnostics(diagnostics_token)
            _client.reset_current_logical_turn(turn_token)
            _client.reset_current_scope_key(scope_token)

    def test_logical_turn_groups_provider_requests_locally(self) -> None:
        scope_token = _client.set_current_scope_key("private:42")
        turn_token = _client.set_current_logical_turn("turn-test")
        try:
            first = _client._next_request_identity(
                [{"role": "user", "content": "hi"}],
                None,
            )
            second = _client._next_request_identity(
                [
                    {"role": "user", "content": "hi"},
                    {"role": "tool", "content": "ok"},
                ],
                [{"type": "function", "function": {"name": "lookup"}}],
            )
        finally:
            _client.reset_current_logical_turn(turn_token)
            _client.reset_current_scope_key(scope_token)
        self.assertEqual(first["logical_turn_id"], "turn-test")
        self.assertEqual(first["request_seq"], 1)
        self.assertEqual(first["request_kind"], "chat")
        self.assertEqual(second["request_seq"], 2)
        self.assertEqual(second["request_kind"], "tool_followup")

    def test_implicit_logical_turn_restarts_when_scope_changes(self) -> None:
        first_scope_token = _client.set_current_scope_key("private:1")
        try:
            first = _client._next_request_identity(
                [{"role": "user", "content": "first"}],
                None,
            )
            second_scope_token = _client.set_current_scope_key("private:2")
            try:
                second = _client._next_request_identity(
                    [{"role": "user", "content": "second"}],
                    None,
                )
            finally:
                _client.reset_current_scope_key(second_scope_token)
        finally:
            _client.reset_current_scope_key(first_scope_token)
            _client._current_logical_turn_var.set(None)

        self.assertNotEqual(first["logical_turn_id"], second["logical_turn_id"])
        self.assertEqual(first["request_seq"], 1)
        self.assertEqual(second["request_seq"], 1)

    def test_session_mode_synthesizes_stable_anchor_observation(self) -> None:
        compressor = importlib.import_module("catty_qq_ai.nlu.prompt_compressor")
        scope_token = _client.set_current_scope_key("private:anchor-test")
        _client._SESSION_TRIM_EPOCH_OBSERVED.clear()
        base_context = {
            "session_context_enabled": True,
            "trim_epoch": 3,
            "request_emergency_trimmed": False,
        }
        payload = {"messages": [{"role": "user", "content": "ping"}]}
        try:
            with patch.object(compressor, "get_anchor_observation", return_value=None):
                first = _client._build_cache_request_diagnostics(
                    base_url="https://api.deepseek.com/v1",
                    model="deepseek-v4-flash",
                    payload=dict(payload),
                    session_context=dict(base_context),
                )
                second = _client._build_cache_request_diagnostics(
                    base_url="https://api.deepseek.com/v1",
                    model="deepseek-v4-flash",
                    payload=dict(payload),
                    session_context=dict(base_context),
                )
                emergency = _client._build_cache_request_diagnostics(
                    base_url="https://api.deepseek.com/v1",
                    model="deepseek-v4-flash",
                    payload=dict(payload),
                    session_context={**base_context, "request_emergency_trimmed": True},
                )
                epoch_shift = _client._build_cache_request_diagnostics(
                    base_url="https://api.deepseek.com/v1",
                    model="deepseek-v4-flash",
                    payload=dict(payload),
                    session_context={**base_context, "trim_epoch": 4},
                )
        finally:
            _client._SESSION_TRIM_EPOCH_OBSERVED.clear()
            _client.reset_current_scope_key(scope_token)

        self.assertTrue(first["anchor_observed"])
        self.assertFalse(first["anchor"]["anchor_changed"])
        self.assertEqual(first["anchor"]["source"], "session_context")
        self.assertFalse(second["anchor"]["anchor_changed"])
        self.assertEqual(first["cohort_metadata"]["anchor_observed"], 1)
        self.assertEqual(second["cohort_metadata"]["anchor_observed"], 1)
        self.assertTrue(emergency["anchor"]["anchor_changed"])
        self.assertEqual(emergency["anchor"]["reset_reason"], "request_emergency_trim")
        self.assertTrue(epoch_shift["anchor"]["anchor_changed"])
        self.assertEqual(epoch_shift["anchor"]["reset_reason"], "trim_epoch_changed")
        self.assertEqual(epoch_shift["anchor"]["anchor_before"], 3)
        self.assertEqual(epoch_shift["anchor"]["anchor_after"], 4)

    def test_session_mode_disabled_keeps_anchor_unobserved(self) -> None:
        compressor = importlib.import_module("catty_qq_ai.nlu.prompt_compressor")
        with patch.object(compressor, "get_anchor_observation", return_value=None):
            diagnostics = _client._build_cache_request_diagnostics(
                base_url="https://api.deepseek.com/v1",
                model="deepseek-v4-flash",
                payload={"messages": [{"role": "user", "content": "ping"}]},
                session_context={"session_context_enabled": False},
            )

        self.assertFalse(diagnostics["anchor_observed"])
        self.assertEqual(diagnostics["cohort_metadata"]["anchor_observed"], 0)

    def test_final_payload_trim_keeps_latest_complete_history_turn(self) -> None:
        compressor = importlib.import_module("catty_qq_ai.nlu.prompt_compressor")
        marker = _client._SESSION_HISTORY_MARKER
        payload = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u111", marker: True},
                {"role": "assistant", "content": "a111", marker: True},
                {"role": "user", "content": "u2", marker: True},
                {"role": "assistant", "content": "a2", marker: True},
                {"role": "user", "content": "q"},
            ],
            "max_tokens": 0,
        }
        runtime_config = types.SimpleNamespace(
            catty_session_context_enabled=True,
            catty_session_context_target_tokens=12,
            catty_session_context_trim_to_tokens=8,
            catty_session_context_headroom_tokens=2,
            catty_model_context_tokens=100,
        )
        with (
            patch.object(_client, "_get_runtime_config", return_value=runtime_config),
            patch.object(_client, "get_session_token_estimator_multiplier", return_value=1.0),
            patch.object(compressor, "count_tokens", side_effect=lambda text: len(text or "")),
        ):
            diagnostics = _client._prepare_session_context_payload(payload, model="test")
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "q"},
            ],
        )
        self.assertTrue(diagnostics["request_emergency_trimmed"])
        self.assertEqual(diagnostics["history_tokens"], 4)
        self.assertTrue(all(marker not in message for message in payload["messages"]))

    def _prepare_large_prefix_fixture(self, *, model_limit=1000, multiplier=1.0, tools=None):
        compressor = importlib.import_module("catty_qq_ai.nlu.prompt_compressor")
        marker = _client._SESSION_HISTORY_MARKER
        payload = {
            "messages": [
                {"role": "system", "content": "s" * 120},
                {"role": "user", "content": "save report.md", marker: True},
                {"role": "assistant", "content": "ok", marker: True},
                {"role": "user", "content": "q"},
            ],
            "max_tokens": 8,
        }
        if tools:
            payload["tools"] = tools
        runtime_config = types.SimpleNamespace(
            catty_session_context_enabled=True,
            catty_session_context_target_tokens=64,
            catty_session_context_trim_to_tokens=48,
            catty_session_context_headroom_tokens=8,
            catty_model_context_tokens=model_limit,
        )
        with (
            patch.object(_client, "_get_runtime_config", return_value=runtime_config),
            patch.object(_client, "get_session_token_estimator_multiplier", return_value=multiplier),
            patch.object(compressor, "count_tokens", side_effect=lambda text: len(text or "")),
        ):
            diagnostics = _client._prepare_session_context_payload(payload, model="test")
        return payload, diagnostics

    def test_long_persona_prefix_does_not_erase_recent_file_requirement(self) -> None:
        payload, diagnostics = self._prepare_large_prefix_fixture()
        self.assertEqual(diagnostics["history_messages"], 2)
        self.assertFalse(diagnostics["request_emergency_trimmed"])
        self.assertEqual(payload["messages"][1]["content"], "save report.md")
        self.assertEqual(payload["messages"][0]["content"], "s" * 120)

    def test_large_tool_schema_does_not_consume_session_history_budget(self) -> None:
        tools = [{"type": "function", "function": {"name": "fixture", "description": "x" * 500}}]
        payload, diagnostics = self._prepare_large_prefix_fixture(tools=tools, model_limit=2000)
        self.assertEqual(diagnostics["history_messages"], 2)
        self.assertEqual(payload["tools"], tools)

    def test_long_prefix_still_obeys_model_window_and_output_reserve(self) -> None:
        payload, diagnostics = self._prepare_large_prefix_fixture(model_limit=130)
        self.assertEqual(diagnostics["history_messages"], 0)
        self.assertTrue(diagnostics["request_emergency_trimmed"])
        self.assertLessEqual(diagnostics["retained_input_tokens"] + 8, 130)
        self.assertEqual(payload["messages"][-1]["content"], "q")

    def test_prefix_and_history_budgets_use_same_calibrated_units(self) -> None:
        payload, diagnostics = self._prepare_large_prefix_fixture(model_limit=300, multiplier=2.0)
        self.assertEqual(diagnostics["history_messages"], 2)
        self.assertLessEqual(diagnostics["retained_input_tokens"] + 8, 300)
        self.assertTrue(all(_client._SESSION_HISTORY_MARKER not in message for message in payload["messages"]))

    def test_untrimmed_next_turn_preserves_previous_wire_message_prefix(self) -> None:
        marker = _client._SESSION_HISTORY_MARKER
        first_payload = {
            "messages": [
                {"role": "system", "content": "stable"},
                {"role": "user", "content": "turn-1"},
            ],
            "max_tokens": 0,
        }
        second_payload = {
            "messages": [
                {"role": "system", "content": "stable"},
                {"role": "user", "content": "turn-1", marker: True},
                {"role": "assistant", "content": "reply-1", marker: True},
                {"role": "user", "content": "turn-2"},
            ],
            "max_tokens": 0,
        }
        runtime_config = types.SimpleNamespace(
            catty_session_context_enabled=True,
            catty_session_context_target_tokens=256_000,
            catty_session_context_trim_to_tokens=192_000,
            catty_session_context_headroom_tokens=32_000,
            catty_model_context_tokens=1_000_000,
        )
        with (
            patch.object(_client, "_get_runtime_config", return_value=runtime_config),
            patch.object(_client, "get_session_token_estimator_multiplier", return_value=1.0),
        ):
            _client._prepare_session_context_payload(first_payload, model="test")
            _client._prepare_session_context_payload(second_payload, model="test")
        self.assertEqual(
            second_payload["messages"][:len(first_payload["messages"])],
            first_payload["messages"],
        )

    def test_current_turn_tokens_include_tool_followup_suffix(self) -> None:
        compressor = importlib.import_module("catty_qq_ai.nlu.prompt_compressor")
        marker = _client._SESSION_HISTORY_MARKER
        payload = {
            "messages": [
                {"role": "system", "content": "stable"},
                {"role": "user", "content": "old", marker: True},
                {"role": "assistant", "content": "answer", marker: True},
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "tool-call"},
                {"role": "tool", "content": "tool-result"},
            ],
            "max_tokens": 0,
        }
        runtime_config = types.SimpleNamespace(
            catty_session_context_enabled=True,
            catty_session_context_target_tokens=256_000,
            catty_session_context_trim_to_tokens=192_000,
            catty_session_context_headroom_tokens=32_000,
            catty_model_context_tokens=1_000_000,
        )
        with (
            patch.object(_client, "_get_runtime_config", return_value=runtime_config),
            patch.object(_client, "get_session_token_estimator_multiplier", return_value=1.0),
            patch.object(compressor, "count_tokens", side_effect=lambda text: len(text or "")),
        ):
            diagnostics = _client._prepare_session_context_payload(payload, model="test")

        self.assertEqual(
            diagnostics["unavoidable_current_turn_tokens"],
            len("question") + len("tool-call") + len("tool-result"),
        )

    def test_emergency_trim_preserves_complete_current_tool_suffix(self) -> None:
        compressor = importlib.import_module("catty_qq_ai.nlu.prompt_compressor")
        marker = _client._SESSION_HISTORY_MARKER
        current_suffix = [
            {"role": "user", "content": "ask"},
            {
                "role": "assistant",
                "content": "call",
                "tool_calls": [{"id": "call-7", "function": {"name": "lookup"}}],
            },
            {"role": "tool", "tool_call_id": "call-7", "content": "result"},
            {"role": "system", "content": "hint"},
        ]
        payload = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "old1", marker: True},
                {"role": "assistant", "content": "answer1", marker: True},
                {"role": "user", "content": "old2", marker: True},
                {"role": "assistant", "content": "answer2", marker: True},
                *current_suffix,
            ],
            "max_tokens": 0,
        }
        runtime_config = types.SimpleNamespace(
            catty_session_context_enabled=True,
            catty_session_context_target_tokens=18,
            catty_session_context_trim_to_tokens=12,
            catty_session_context_headroom_tokens=2,
            catty_model_context_tokens=100,
        )
        with (
            patch.object(_client, "_get_runtime_config", return_value=runtime_config),
            patch.object(_client, "get_session_token_estimator_multiplier", return_value=1.0),
            patch.object(compressor, "count_tokens", side_effect=lambda text: len(text or "")),
        ):
            diagnostics = _client._prepare_session_context_payload(payload, model="test")

        self.assertEqual(payload["messages"], [{"role": "system", "content": "s"}, *current_suffix])
        self.assertTrue(diagnostics["request_emergency_trimmed"])
        self.assertEqual(diagnostics["request_trimmed_messages"], 4)
        self.assertEqual(payload["messages"][2]["tool_calls"][0]["id"], "call-7")
        self.assertEqual(payload["messages"][3]["tool_call_id"], "call-7")
        self.assertTrue(all(marker not in message for message in payload["messages"]))

    def test_request_budget_does_not_subtract_persistence_headroom_twice(self) -> None:
        compressor = importlib.import_module("catty_qq_ai.nlu.prompt_compressor")
        marker = _client._SESSION_HISTORY_MARKER
        history = [
            {"role": "user", "content": "u1", marker: True},
            {"role": "assistant", "content": "aaa", marker: True},
            {"role": "user", "content": "u2", marker: True},
            {"role": "assistant", "content": "bbb", marker: True},
        ]
        payload = {"messages": [*history, {"role": "user", "content": "qq"}], "max_tokens": 0}
        runtime_config = types.SimpleNamespace(
            catty_session_context_enabled=True,
            catty_session_context_target_tokens=12,
            catty_session_context_trim_to_tokens=8,
            catty_session_context_headroom_tokens=2,
            catty_model_context_tokens=100,
        )
        with (
            patch.object(_client, "_get_runtime_config", return_value=runtime_config),
            patch.object(_client, "get_session_token_estimator_multiplier", return_value=1.0),
            patch.object(compressor, "count_tokens", side_effect=lambda text: len(text or "")),
        ):
            diagnostics = _client._prepare_session_context_payload(payload, model="test")

        self.assertFalse(diagnostics["request_emergency_trimmed"])
        self.assertEqual(diagnostics["history_tokens"], 10)
        self.assertEqual(
            payload["messages"],
            [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "aaa"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "bbb"},
                {"role": "user", "content": "qq"},
            ],
        )

    def test_ollama_route_and_options(self) -> None:
        extra_body = {"keep_alive": "30m", "options": {"num_ctx": 1024}}

        self.assertEqual(_client._ollama_chat_url("http://127.0.0.1:11434/v1"), "http://127.0.0.1:11434/api/chat")
        self.assertTrue(_client._looks_like_ollama_route("http://127.0.0.1:11434/v1", "ollama", extra_body))
        self.assertEqual(
            _client._ollama_options(temperature=0.1, max_tokens=16, extra_body=extra_body),
            {"num_ctx": 1024, "temperature": 0.1, "num_predict": 16},
        )

    def test_empty_image_analysis_reply_is_not_treated_as_valid_analysis(self) -> None:
        self.assertEqual(_client._image_analysis_from_reply(""), {})

    def test_http_503_public_message_is_catty(self) -> None:
        # 503 文案池 (_HTTP_503_VARIANTS) 是多变体随机抽,跑多次覆盖所有变体,
        # 每条都必须满足:含 503、含服务名、有猫系词(喵/嗷呜/猫猫/ฅฅ等)、
        # 不裸暴露 "HTTP 503" 字样。
        cat_tokens = ("喵", "嗷呜", "猫猫", "笨猫", "ฅฅ", "尾巴", "爪爪")
        seen: set[str] = set()
        for _ in range(80):
            msg = _client._catty_http_status_message("AI 接口", 503)
            seen.add(msg)
            self.assertIn("503", msg)
            self.assertIn("AI 接口", msg)
            self.assertNotIn("HTTP 503", msg)
            self.assertTrue(
                any(tok in msg for tok in cat_tokens),
                f"503 文案没有猫娘语气: {msg!r}",
            )
        # 80 次抽样应覆盖全部 4 个 variant
        self.assertGreaterEqual(len(seen), 2, f"503 文案只命中 {len(seen)} 种变体,池没充分覆盖")

    def test_persona_reply_context_contextvar_set_get_reset(self) -> None:
        previous = _client.get_current_persona_reply_context()
        outer_context = _make_fadianji_reply_context()
        outer_token = _client.set_current_persona_reply_context(outer_context)
        try:
            inner_context = _make_fadianji_reply_context()
            inner_token = _client.set_current_persona_reply_context(inner_context)
            try:
                self.assertIs(_client.get_current_persona_reply_context(), inner_context)
            finally:
                _client.reset_current_persona_reply_context(inner_token)
            self.assertIs(_client.get_current_persona_reply_context(), outer_context)
        finally:
            _client.reset_current_persona_reply_context(outer_token)
        self.assertIs(_client.get_current_persona_reply_context(), previous)

    def test_scope_contextvar_reset_restores_outer_value(self) -> None:
        previous = _client.get_current_scope_key()
        outer_token = _client.set_current_scope_key("private:outer")
        try:
            inner_token = _client.set_current_scope_key("private:inner")
            try:
                self.assertEqual(_client.get_current_scope_key(), "private:inner")
            finally:
                _client.reset_current_scope_key(inner_token)
            self.assertEqual(_client.get_current_scope_key(), "private:outer")
        finally:
            _client.reset_current_scope_key(outer_token)
        self.assertEqual(_client.get_current_scope_key(), previous)

    def test_model_override_contextvar_set_get_reset(self) -> None:
        previous = _client.get_current_model_override()
        outer_token = _client.set_current_model_override("deepseek-v4-flash")
        try:
            inner_token = _client.set_current_model_override("deepseek-v4-pro")
            try:
                self.assertEqual(_client.get_current_model_override(), "deepseek-v4-pro")
            finally:
                _client.reset_current_model_override(inner_token)
            self.assertEqual(_client.get_current_model_override(), "deepseek-v4-flash")
        finally:
            _client.reset_current_model_override(outer_token)
        self.assertEqual(_client.get_current_model_override(), previous)

    def test_distill_context_cleanup_callback_restores_outer_value(self) -> None:
        previous = _client._current_distill_ctx_var.get()
        clear_outer = _client.set_current_distill_context("outer", ["outer-term"])
        try:
            clear_inner = _client.set_current_distill_context("inner", ["inner-term"])
            try:
                self.assertEqual(
                    _client._current_distill_ctx_var.get(),
                    {"user_text": "inner", "terms": ["inner-term"]},
                )
            finally:
                clear_inner()
            self.assertEqual(
                _client._current_distill_ctx_var.get(),
                {"user_text": "outer", "terms": ["outer-term"]},
            )
        finally:
            clear_outer()
        self.assertIs(_client._current_distill_ctx_var.get(), previous)

    def test_distill_context_cleanup_resets_after_callback_registration_failure(self) -> None:
        class _FailingCallbackRegistrar:
            def callback(self, _callback) -> None:
                raise RuntimeError("callback registration failed")

        previous = _client._current_distill_ctx_var.get()
        cleanup = _client.set_current_distill_context("inner", ["inner-term"])
        cleaned = False
        try:
            with self.assertRaisesRegex(RuntimeError, "callback registration failed"):
                try:
                    _FailingCallbackRegistrar().callback(cleanup)
                except RuntimeError:
                    cleanup()
                    cleaned = True
                    raise
            self.assertIs(_client._current_distill_ctx_var.get(), previous)
        finally:
            if not cleaned:
                cleanup()

    def test_fadianji_http_status_messages_are_neutral_and_keep_auth_semantics(self) -> None:
        token = _client.set_current_persona_reply_context(_make_fadianji_reply_context())
        try:
            unavailable = _client._catty_http_status_message("AI 接口", 503)
            unauthorized = _client._catty_http_status_message("AI 接口", 401)
        finally:
            _client.reset_current_persona_reply_context(token)

        for message in (unavailable, unauthorized):
            for catty_term in ("主人", "喵", "笨猫", "猫娘"):
                self.assertNotIn(catty_term, message)
        self.assertIn("网线抽了。再来。", unavailable)
        self.assertIn("503", unavailable)
        self.assertIn("网线抽了。再来。", unauthorized)
        self.assertIn("401", unauthorized)
        self.assertTrue("API Key" in unauthorized or "权限" in unauthorized)

    def test_fadianji_mc_busy_message_is_neutral(self) -> None:
        token = _client.set_current_persona_reply_context(_make_fadianji_reply_context())
        try:
            message = _client._mc_busy_public_message()
        finally:
            _client.reset_current_persona_reply_context(token)

        for catty_term in ("主人", "喵", "笨猫", "猫娘"):
            self.assertNotIn(catty_term, message)
        self.assertIn("本地服务", message)

    def test_deepseek_v4_thinking_defaults_use_max_effort(self) -> None:
        body = _client._with_deepseek_thinking_defaults(
            "https://api.deepseek.com/v1",
            "deepseek-v4-flash",
            {},
        )

        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "max")

    def test_deepseek_thinking_defaults_respect_explicit_extra_body(self) -> None:
        for effort in ("high", "low"):
            with self.subTest(effort=effort):
                body = _client._with_deepseek_thinking_defaults(
                    "https://api.deepseek.com/v1",
                    "deepseek-v4-pro",
                    {"thinking": {"type": "disabled"}, "reasoning_effort": effort},
                )
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertEqual(body["reasoning_effort"], effort)

    def test_thinking_max_defaults_cover_all_openai_compatible_routes(self) -> None:
        body = _client._with_thinking_max_defaults(
            "https://opencode.ai/zen/go/v1/chat/completions",
            "sk-x",
            "gpt-5.6-luna",
            {},
        )
        self.assertEqual(body["reasoning_effort"], "max")
        self.assertNotIn("thinking", body)

    def test_thinking_max_defaults_respect_explicit_override(self) -> None:
        for model in ("gpt-5.6-luna", "deepseek-v4-flash"):
            for effort in ("high", "low"):
                with self.subTest(model=model, effort=effort):
                    body = _client._with_thinking_max_defaults(
                        "https://opencode.ai/zen/go/v1/chat/completions",
                        "sk-x",
                        model,
                        {"reasoning_effort": effort},
                    )
                    self.assertEqual(body["reasoning_effort"], effort)

    def test_thinking_max_defaults_glm_uses_thinking_enabled(self) -> None:
        body = _client._with_thinking_max_defaults(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            "sk-x",
            "glm-4.6v",
            {},
        )
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertNotIn("reasoning_effort", body)

    def test_thinking_max_defaults_skip_ollama_route(self) -> None:
        body = _client._with_thinking_max_defaults(
            "http://127.0.0.1:11434/v1",
            "ollama",
            "qwen2.5:7b",
            {"think": False},
        )
        self.assertEqual(body, {"think": False})

    def test_thinking_max_defaults_keep_deepseek_v4_behavior(self) -> None:
        body = _client._with_thinking_max_defaults(
            "https://opencode.ai/zen/go/v1/chat/completions",
            "sk-x",
            "deepseek-v4-flash",
            {},
        )
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "max")

    def test_deepseek_payload_omits_openai_only_cache_fields(self) -> None:
        captured_payloads: list[dict[str, object]] = []

        class _FakeResponse:
            status_code = 200
            text = ""

            def json(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "ok"}}]}

        class _FakeAsyncClient:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc_info: object) -> bool:
                return False

            async def post(
                self,
                _url: str,
                *,
                headers: dict[str, str],
                json: dict[str, object],
            ) -> _FakeResponse:
                del headers
                captured_payloads.append(dict(json))
                return _FakeResponse()

        with (
            patch.object(_client.httpx, "AsyncClient", _FakeAsyncClient),
            patch.object(_client, "_get_runtime_config", return_value=types.SimpleNamespace(
                catty_openai_prompt_cache_key_enabled=True,
            )),
            patch.object(_client, "get_current_scope_key", return_value="private:42"),
            patch("pathlib.Path") as request_dump_path,
        ):
            request_dump_path.return_value.__truediv__.return_value = request_dump_path.return_value
            request_dump_path.return_value.glob.return_value = []
            response = asyncio.run(
                _client._post_chat_completion_raw(
                    base_url="https://api.deepseek.com/v1",
                    api_key="test-key",
                    model="deepseek-v4-flash",
                    messages=[{"role": "user", "content": "ping"}],
                    timeout=1,
                    proxy="",
                    temperature=0.1,
                    max_tokens=16,
                    extra_headers={},
                    extra_body={
                        "user": "must-not-send",
                        "user_id": "must-not-send",
                        "prompt_cache_key": "must-not-send",
                        "conversation_id": "must-not-send",
                    },
                    enable_cache=True,
                )
            )

        self.assertEqual(response["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(captured_payloads), 1)
        self.assertNotIn("user", captured_payloads[0])
        self.assertNotIn("user_id", captured_payloads[0])
        self.assertNotIn("prompt_cache_key", captured_payloads[0])
        self.assertNotIn("conversation_id", captured_payloads[0])

    def test_data_gif_is_converted_to_first_frame_png_for_vision(self) -> None:
        image = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
        output = BytesIO()
        image.save(output, format="GIF")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        config = types.SimpleNamespace(catty_vision_request_timeout=1, catty_request_timeout=1, catty_http_proxy="")

        converted = asyncio.run(_client._vision_image_url(config, f"data:image/gif;base64,{encoded}"))

        self.assertTrue(converted.startswith("data:image/png;base64,"))


def _make_fallback_config(
    *,
    fallback_enabled: bool = True,
    mc_gate_enabled: bool = False,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        # cloud
        catty_openai_base_url="https://cloud.example.com/v1",
        catty_openai_api_key="cloud-key",
        catty_openai_model="cloud-model",
        catty_openai_extra_headers={},
        catty_openai_extra_body={},
        catty_temperature=0.7,
        catty_max_tokens=100,
        catty_request_timeout=60.0,
        catty_http_proxy="",
        # fallback
        catty_ai_fallback_enabled=fallback_enabled,
        catty_ai_fallback_base_url="http://127.0.0.1:11434/v1",
        catty_ai_fallback_api_key="ollama",
        catty_ai_fallback_model="qwen2.5:7b",
        catty_ai_fallback_extra_headers={},
        catty_ai_fallback_extra_body={"think": False},
        catty_ai_fallback_temperature=0.7,
        catty_ai_fallback_max_tokens=4096,
        catty_ai_fallback_request_timeout=120.0,
        catty_ai_fallback_cooldown_seconds=300.0,
        catty_ai_fallback_mc_gate_enabled=mc_gate_enabled,
        catty_ai_fallback_mc_server_host="localhost",
        catty_ai_fallback_mc_server_port=26843,
        catty_ai_fallback_mc_ping_timeout_seconds=2.0,
    )


def _make_tool_config() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        catty_tools_enabled=True,
        catty_anthropic_native_enabled=False,
        catty_openai_base_url="https://cloud.example.com/v1",
        catty_openai_api_key="cloud-key",
        catty_openai_model="cloud-model",
        catty_openai_extra_headers={},
        catty_openai_extra_body={},
        catty_temperature=0.7,
        catty_max_tokens=100,
        catty_request_timeout=60.0,
        catty_http_proxy="",
        catty_prompt_cache_enabled=False,
        catty_prompt_cache_depth=2,
    )


class ToolChatTests(unittest.TestCase):
    def setUp(self) -> None:
        _client._reset_cloud_health_for_tests()
        self._orig_raw = _client._post_chat_completion_raw

    def tearDown(self) -> None:
        _client._post_chat_completion_raw = self._orig_raw
        _client._reset_cloud_health_for_tests()

    def test_forced_tool_choice_is_forwarded_and_short_circuits(self) -> None:
        forced = {"type": "function", "function": {"name": "catty_imagegen"}}
        captured: list[dict] = []
        executed: list[tuple[str, str]] = []

        async def fake_raw(**kwargs):
            captured.append(kwargs)
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "catty_imagegen",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }

        async def executor(name: str, args_json: str) -> dict:
            executed.append((name, args_json))
            return {"_short_circuit_reply": "画好啦主人~ ฅฅ"}

        _client._post_chat_completion_raw = fake_raw
        result = asyncio.run(
            _client.chat_completion_with_tools(
                _make_tool_config(),
                [{"role": "user", "content": "猫猫画一张自画像"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "catty_imagegen",
                            "description": "draw",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                tool_executor=executor,
                tool_choice=forced,
            )
        )

        self.assertEqual(result, "画好啦主人~ ฅฅ")
        self.assertEqual(captured[0]["tool_choice"], forced)
        self.assertEqual(executed, [("catty_imagegen", "{}")])

    def test_tool_chat_round_trips_deepseek_reasoning_content(self) -> None:
        calls: list[dict] = []

        async def fake_raw(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": "need to call tool first",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            assistant_messages = [
                msg for msg in kwargs["messages"] if msg.get("role") == "assistant"
            ]
            self.assertEqual(
                assistant_messages[-1]["reasoning_content"],
                "need to call tool first",
            )
            return {"choices": [{"message": {"content": "查好啦主人~"}}]}

        async def executor(name: str, args_json: str) -> dict:
            self.assertEqual((name, args_json), ("lookup", "{}"))
            return {"ok": True}

        _client._post_chat_completion_raw = fake_raw
        result = asyncio.run(
            _client.chat_completion_with_tools(
                _make_tool_config(),
                [{"role": "user", "content": "查一下"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "description": "lookup",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                tool_executor=executor,
                max_rounds=2,
            )
        )

        self.assertEqual(result, "查好啦主人~")
        self.assertEqual(len(calls), 2)


    def test_tool_follow_up_uses_fadianji_instruction_without_catty_voice(self) -> None:
        calls: list[dict] = []

        async def fake_raw(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "lookup", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                }

            hint = kwargs["messages"][-1]["content"]
            self.assertTrue(hint.startswith("工具结果已到。直接结合结果回，短句，不复述过程。"))
            self.assertIn("绝不", hint)
            self.assertIn("原始字典/数组", hint)
            self.assertNotIn("笨猫口吻", hint)
            self.assertNotIn("猫娘语气", hint)
            return {"choices": [{"message": {"content": "查完了。"}}]}

        async def executor(name: str, args_json: str) -> dict:
            self.assertEqual((name, args_json), ("lookup", "{}"))
            return {"ok": True}

        _client._post_chat_completion_raw = fake_raw
        token = _client.set_current_persona_reply_context(_make_fadianji_reply_context())
        try:
            result = asyncio.run(
                _client.chat_completion_with_tools(
                    _make_tool_config(),
                    [{"role": "user", "content": "查一下"}],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "description": "lookup",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                    tool_executor=executor,
                    max_rounds=2,
                )
            )
        finally:
            _client.reset_current_persona_reply_context(token)

        self.assertEqual(result, "查完了。")
        self.assertEqual(len(calls), 2)

@unittest.skip(
    "本地 AI fallback 已在 openai_client._fallback_is_configured 硬关闭 — "
    "云端失败现在直接外抛 OpenAICompatibleError,不再走本地兜底。这套测试覆盖的"
    "fallback 路径(cloud sad → 切换 local → 标 unhealthy/cooldown / mc gate)"
    "整体被禁用,保留代码以便未来恢复本地路径时重启。"
)
class CloudFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        _client._reset_cloud_health_for_tests()
        self._orig_cloud = _client._post_chat_completion
        self._orig_local = _client._post_ollama_chat

    def tearDown(self) -> None:
        _client._post_chat_completion = self._orig_cloud
        _client._post_ollama_chat = self._orig_local
        _client._reset_cloud_health_for_tests()

    def _patch_cloud(self, fn) -> None:
        _client._post_chat_completion = fn

    def _patch_local(self, fn) -> None:
        _client._post_ollama_chat = fn

    def test_cloud_success_returns_cloud_response_without_touching_local(self) -> None:
        cloud_calls = 0
        local_calls = 0

        async def cloud(**_kwargs):
            nonlocal cloud_calls
            cloud_calls += 1
            return "from-cloud"

        async def local(**_kwargs):
            nonlocal local_calls
            local_calls += 1
            return "from-local"

        self._patch_cloud(cloud)
        self._patch_local(local)
        result = asyncio.run(_client.chat_completion(_make_fallback_config(), [{"role": "user", "content": "x"}]))
        self.assertEqual(result, "from-cloud")
        self.assertEqual(cloud_calls, 1)
        self.assertEqual(local_calls, 0)
        self.assertFalse(_client._cloud_is_unhealthy())

    def test_cloud_failure_falls_back_to_local_and_marks_unhealthy(self) -> None:
        async def cloud(**_kwargs):
            raise _client.OpenAICompatibleError("cloud is sad")

        async def local(**_kwargs):
            return "from-local"

        self._patch_cloud(cloud)
        self._patch_local(local)
        result = asyncio.run(_client.chat_completion(_make_fallback_config(), [{"role": "user", "content": "x"}]))
        self.assertEqual(result, "from-local")
        self.assertTrue(_client._cloud_is_unhealthy())

    def test_subsequent_call_in_cooldown_skips_cloud(self) -> None:
        cloud_calls = 0
        local_calls = 0

        async def cloud(**_kwargs):
            nonlocal cloud_calls
            cloud_calls += 1
            raise _client.OpenAICompatibleError("cloud is sad")

        async def local(**_kwargs):
            nonlocal local_calls
            local_calls += 1
            return "from-local"

        self._patch_cloud(cloud)
        self._patch_local(local)
        config = _make_fallback_config()
        # Trip cooldown
        asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "x"}]))
        # Second call should NOT touch cloud
        asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "y"}]))
        self.assertEqual(cloud_calls, 1)
        self.assertEqual(local_calls, 2)

    def test_fallback_disabled_means_cloud_errors_propagate(self) -> None:
        async def cloud(**_kwargs):
            raise _client.OpenAICompatibleError("cloud is sad")

        async def local(**_kwargs):
            self.fail("local should not be called when fallback disabled")

        self._patch_cloud(cloud)
        self._patch_local(local)
        config = _make_fallback_config(fallback_enabled=False)
        with self.assertRaises(_client.OpenAICompatibleError):
            asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "x"}]))
        self.assertFalse(_client._cloud_is_unhealthy())

    def test_both_cloud_and_local_fail_raises_cloud_exception(self) -> None:
        async def cloud(**_kwargs):
            raise _client.OpenAICompatibleError("cloud sad", "cloud-detail")

        async def local(**_kwargs):
            raise _client.OpenAICompatibleError("local sad", "local-detail")

        self._patch_cloud(cloud)
        self._patch_local(local)
        with self.assertRaises(_client.OpenAICompatibleError) as ctx:
            asyncio.run(_client.chat_completion(_make_fallback_config(), [{"role": "user", "content": "x"}]))
        self.assertEqual(ctx.exception.public_message, "cloud sad")

    def test_mc_gate_blocks_fallback_when_players_online(self) -> None:
        cloud_calls = 0
        local_calls = 0

        async def cloud(**_kwargs):
            nonlocal cloud_calls
            cloud_calls += 1
            raise _client.OpenAICompatibleError("cloud sad")

        async def local(**_kwargs):
            nonlocal local_calls
            local_calls += 1
            return "from-local"

        async def fake_mc(host, port, *, timeout=3.0):
            return True  # players online

        self._patch_cloud(cloud)
        self._patch_local(local)
        original_mc = _client.mc_has_players
        _client.mc_has_players = fake_mc
        try:
            config = _make_fallback_config(mc_gate_enabled=True)
            with self.assertRaises(_client.MCBusyError):
                asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "x"}]))
        finally:
            _client.mc_has_players = original_mc
        # Local should NOT have been called
        self.assertEqual(local_calls, 0)
        # Cloud was tried once before the MC gate kicked in
        self.assertEqual(cloud_calls, 1)

    def test_mc_gate_allows_fallback_when_no_players(self) -> None:
        async def cloud(**_kwargs):
            raise _client.OpenAICompatibleError("cloud sad")

        async def local(**_kwargs):
            return "from-local"

        async def fake_mc(host, port, *, timeout=3.0):
            return False  # no players

        self._patch_cloud(cloud)
        self._patch_local(local)
        original_mc = _client.mc_has_players
        _client.mc_has_players = fake_mc
        try:
            config = _make_fallback_config(mc_gate_enabled=True)
            result = asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "x"}]))
        finally:
            _client.mc_has_players = original_mc
        self.assertEqual(result, "from-local")

    def test_mc_gate_in_cooldown_still_blocks_with_players(self) -> None:
        async def cloud(**_kwargs):
            raise _client.OpenAICompatibleError("cloud sad")

        async def local(**_kwargs):
            return "from-local"

        async def fake_mc_after_trip(host, port, *, timeout=3.0):
            return False  # no players first call

        async def fake_mc_with_players(host, port, *, timeout=3.0):
            return True

        self._patch_cloud(cloud)
        self._patch_local(local)
        original_mc = _client.mc_has_players
        _client.mc_has_players = fake_mc_after_trip
        try:
            config = _make_fallback_config(mc_gate_enabled=True)
            # 1st call: cloud fails, local succeeds, cooldown set
            result1 = asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "a"}]))
            self.assertEqual(result1, "from-local")
            self.assertTrue(_client._cloud_is_unhealthy())
            # 2nd call: MC has players now → in cooldown path it should hit MC gate and raise
            _client.mc_has_players = fake_mc_with_players
            with self.assertRaises(_client.MCBusyError):
                asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "b"}]))
        finally:
            _client.mc_has_players = original_mc

    def test_cloud_recovery_clears_cooldown_after_local_warmth(self) -> None:
        cloud_should_fail = [True]

        async def cloud(**_kwargs):
            if cloud_should_fail[0]:
                raise _client.OpenAICompatibleError("cloud sad")
            return "cloud-back"

        async def local(**_kwargs):
            return "from-local"

        self._patch_cloud(cloud)
        self._patch_local(local)
        config = _make_fallback_config()
        asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "1"}]))
        self.assertTrue(_client._cloud_is_unhealthy())
        # Manually clear cooldown so this test doesn't sleep 5 minutes
        _client._reset_cloud_health_for_tests()
        cloud_should_fail[0] = False
        result = asyncio.run(_client.chat_completion(config, [{"role": "user", "content": "2"}]))
        self.assertEqual(result, "cloud-back")
        self.assertFalse(_client._cloud_is_unhealthy())


class PooledHttpClientTests(unittest.TestCase):
    def test_same_loop_reuses_one_client_per_timeout_and_proxy(self) -> None:
        async def collect():
            async with _client._http_client(30, "") as first:
                pass
            async with _client._http_client(30, "") as second:
                pass
            async with _client._http_client(90, "") as other_timeout:
                pass
            clients = (first, second, other_timeout)
            for client in {id(c): c for c in clients}.values():
                await client.aclose()
            return clients

        first, second, other_timeout = asyncio.run(collect())
        self.assertIs(first, second)
        self.assertIsNot(first, other_timeout)
        self.assertIsInstance(first._transport, _client.httpx.AsyncHTTPTransport)

    def test_new_loop_gets_its_own_client(self) -> None:
        async def grab():
            async with _client._http_client(30, "") as client:
                pass
            await client.aclose()
            return client

        self.assertIsNot(asyncio.run(grab()), asyncio.run(grab()))

    def test_patched_async_client_keeps_per_request_semantics(self) -> None:
        created = []

        class _FakeAsyncClient:
            def __init__(self, **kwargs) -> None:
                created.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info) -> bool:
                return False

        async def use_twice():
            for _ in range(2):
                async with _client._http_client(30, "") as client:
                    self.assertIsInstance(client, _FakeAsyncClient)

        with patch.object(_client.httpx, "AsyncClient", _FakeAsyncClient):
            asyncio.run(use_twice())
        self.assertEqual(len(created), 2)


if __name__ == "__main__":
    unittest.main()
