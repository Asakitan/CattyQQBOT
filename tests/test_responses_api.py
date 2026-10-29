"""Responses API (OpenAI /v1/responses) 转换层单元测试。

覆盖: endpoint 判别 / URL 构造 / chat-messages→Responses input / payload→body /
Responses response→chat-completions shape / 流式 SSE 事件拼装 (mock httpx 流)。
"""
from pathlib import Path
import asyncio
import importlib.util
import importlib
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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

_RESPONSES_URL = "https://opencode.ai/zen/go/v1/responses"


class ResponsesEndpointDetectionTests(unittest.TestCase):
    def test_recognizes_responses_path(self) -> None:
        self.assertTrue(_client._is_responses_endpoint(_RESPONSES_URL))
        self.assertTrue(_client._is_responses_endpoint(_RESPONSES_URL + "/"))
        self.assertTrue(_client._is_responses_endpoint("https://x.example/v1/responses"))

    def test_rejects_chat_completions_and_plain_urls(self) -> None:
        self.assertFalse(_client._is_responses_endpoint("https://api.deepseek.com/v1"))
        self.assertFalse(
            _client._is_responses_endpoint(
                "https://opencode.ai/zen/go/v1/chat/completions",
            ),
        )
        self.assertFalse(_client._is_responses_endpoint(""))
        self.assertFalse(_client._is_responses_endpoint("not a url"))

    def test_responses_url_keeps_or_appends(self) -> None:
        self.assertEqual(_client._responses_url(_RESPONSES_URL), _RESPONSES_URL)
        self.assertEqual(
            _client._responses_url("https://api.openai.com/v1"),
            "https://api.openai.com/v1/responses",
        )


class MessagesToResponsesInputTests(unittest.TestCase):
    def test_simple_history_conversion(self) -> None:
        messages = [
            {"role": "system", "content": "你是猫娘。"},
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "在的喵～"},
            {"role": "user", "content": "帮我看看"},
        ]
        items = _client._messages_to_responses_input(messages)
        self.assertEqual(
            [item["type"] for item in items],
            ["message", "message", "message", "message"],
        )
        self.assertEqual(items[0]["role"], "system")
        self.assertEqual(items[0]["content"][0]["type"], "input_text")
        self.assertEqual(items[0]["content"][0]["text"], "你是猫娘。")
        self.assertEqual(items[2]["content"][0]["type"], "output_text")
        self.assertEqual(items[2]["content"][0]["text"], "在的喵～")

    def test_assistant_tool_calls_become_function_call_items_after_message(self) -> None:
        messages = [
            {"role": "user", "content": "画一张图"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "catty_imagegen", "arguments": "{\"prompt\":\"猫\"}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "catty_imagegen", "content": "{\"ok\":true}"},
        ]
        items = _client._messages_to_responses_input(messages)
        self.assertEqual(
            [item["type"] for item in items],
            ["message", "function_call", "function_call_output"],
        )
        call_item = items[1]
        self.assertEqual(call_item["name"], "catty_imagegen")
        self.assertEqual(call_item["call_id"], "call_1")
        self.assertEqual(call_item["arguments"], "{\"prompt\":\"猫\"}")
        self.assertEqual(items[2]["call_id"], "call_1")
        self.assertEqual(items[2]["output"], "{\"ok\":true}")

    def test_assistant_text_plus_tool_call_keeps_message_first(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "我看看",
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    },
                ],
            },
        ]
        items = _client._messages_to_responses_input(messages)
        self.assertEqual(
            [item["type"] for item in items],
            ["message", "function_call"],
        )
        self.assertEqual(items[0]["content"][0]["text"], "我看看")

    def test_multimodal_user_content(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                ],
            },
        ]
        items = _client._messages_to_responses_input(messages)
        self.assertEqual(items[0]["content"][0]["type"], "input_text")
        self.assertEqual(items[0]["content"][1]["type"], "input_image")
        self.assertEqual(items[0]["content"][1]["image_url"], "https://x/y.png")


class PayloadToResponsesBodyTests(unittest.TestCase):
    def test_hoists_system_into_instructions(self) -> None:
        payload = {
            "model": "gpt-5.6-luna",
            "messages": [
                {"role": "system", "content": "你是机机。"},
                {"role": "user", "content": "hi"},
            ],
            "temperature": 0.7,
            "max_tokens": 8000,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        body = _client._payload_to_responses_body(payload, stream=True)
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertEqual(body["instructions"], "你是机机。")
        self.assertEqual(len(body["input"]), 1)
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertEqual(body["max_output_tokens"], 8000)
        self.assertNotIn("temperature", body)
        self.assertTrue(body["stream"])
        self.assertNotIn("messages", body)
        self.assertNotIn("stream_options", body)
        self.assertNotIn("max_tokens", body)

    def test_tools_flat_and_tool_choice_mapping(self) -> None:
        payload = {
            "model": "gpt-5.6-luna",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "catty_imagegen",
                        "description": "生成图片",
                        "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
                    },
                },
            ],
            "tool_choice": {"type": "function", "function": {"name": "catty_imagegen"}},
        }
        body = _client._payload_to_responses_body(payload, stream=False)
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["name"], "catty_imagegen")
        self.assertEqual(body["tools"][0]["description"], "生成图片")
        self.assertNotIn("function", body["tools"][0])
        self.assertEqual(body["tool_choice"], {"type": "function", "name": "catty_imagegen"})

    def test_tool_choice_auto_and_required(self) -> None:
        self.assertEqual(_client._tool_choice_to_responses("auto"), "auto")
        self.assertEqual(_client._tool_choice_to_responses(None), "auto")
        self.assertEqual(_client._tool_choice_to_responses("required"), "required")
        self.assertEqual(_client._tool_choice_to_responses("none"), "none")


class ConvertResponsesDataTests(unittest.TestCase):
    def test_text_and_usage_mapping(self) -> None:
        raw = {
            "id": "resp_abc",
            "model": "gpt-5.6-luna",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "在的喵～"}],
                },
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 60},
            },
        }
        data = _client._convert_responses_data(raw)
        choice = data["choices"][0]
        self.assertEqual(choice["message"]["content"], "在的喵～")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(data["usage"]["prompt_tokens"], 100)
        self.assertEqual(data["usage"]["completion_tokens"], 20)
        self.assertEqual(data["usage"]["prompt_tokens_details"]["cached_tokens"], 60)

    def test_tool_calls_and_reasoning(self) -> None:
        raw = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "思考中"}]},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "catty_imagegen",
                    "arguments": "{\"prompt\":\"猫\"}",
                },
            ],
            "usage": {},
        }
        data = _client._convert_responses_data(raw)
        message = data["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "思考中")
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(
            message["tool_calls"][0]["function"]["name"],
            "catty_imagegen",
        )
        self.assertEqual(
            message["tool_calls"][0]["function"]["arguments"],
            "{\"prompt\":\"猫\"}",
        )

    def test_incomplete_status_maps_to_length(self) -> None:
        raw = {"status": "incomplete", "output": [], "usage": {}}
        data = _client._convert_responses_data(raw)
        self.assertEqual(data["choices"][0]["finish_reason"], "length")

    def test_extract_content_round_trip(self) -> None:
        raw = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "喵呜～"}],
                },
            ],
            "usage": {},
        }
        data = _client._convert_responses_data(raw)
        self.assertEqual(_client._extract_content(data), "喵呜～")

    def test_idempotent_on_already_converted(self) -> None:
        raw = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "x"}],
                },
            ],
            "usage": {},
        }
        data = _client._convert_responses_data(raw)
        again = _client._convert_responses_data(data)
        self.assertEqual(again["choices"][0]["message"]["content"], "x")


class _FakeSseResponse:
    def __init__(
        self,
        status_code: int,
        lines: list[str],
        body: str = "",
        *,
        fail_if_read_past: bool = False,
    ) -> None:
        self.status_code = status_code
        self._lines = lines
        self._body = body
        self._fail_if_read_past = fail_if_read_past

    async def aread(self):
        return self._body.encode("utf-8")

    def aiter_lines(self):
        async def gen():
            for line in self._lines:
                yield line
            if self._fail_if_read_past:
                raise AssertionError("stream read continued past terminal SSE event")
        return gen()


class StreamResponsesAttemptTests(unittest.TestCase):
    def test_completed_event_used_as_authoritative(self) -> None:
        sse_events = [
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "在"},
            {"type": "response.output_text.delta", "delta": "的喵"},
            {"type": "response.completed", "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "在的喵"}],
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            }},
        ]
        data = asyncio.run(self._run_stream(sse_events))
        self.assertEqual(data["choices"][0]["message"]["content"], "在的喵")
        self.assertEqual(data["usage"]["prompt_tokens"], 5)

    def test_completed_event_stops_before_transport_eof(self) -> None:
        data = asyncio.run(self._run_stream([
            {"type": "response.output_text.delta", "delta": "在"},
            {"type": "response.completed", "response": {
                "status": "completed",
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "在"}],
                }],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }},
        ], fail_if_read_past=True))

        self.assertEqual(data["choices"][0]["message"]["content"], "在")
        self.assertEqual(data["usage"]["prompt_tokens"], 3)

    def test_done_sentinel_stops_before_transport_eof(self) -> None:
        data = asyncio.run(self._run_stream(
            [{"type": "response.output_text.delta", "delta": "好"}],
            append_done=True,
            fail_if_read_past=True,
        ))

        self.assertEqual(data["choices"][0]["message"]["content"], "好")

    def test_no_completed_event_falls_back_to_deltas(self) -> None:
        sse_events = [
            {"type": "response.output_text.delta", "delta": "喵"},
            {"type": "response.output_text.delta", "delta": "呜"},
            {"type": "response.output_item.added", "item": {
                "id": "fc_1", "type": "function_call", "name": "lookup", "call_id": "call_1",
            }},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{\"q\""},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": ":\"x\"}"},
            {"type": "response.output_item.done", "item": {
                "id": "fc_1", "type": "function_call", "name": "lookup", "call_id": "call_1",
                "arguments": "{\"q\":\"x\"}",
            }},
        ]
        data = asyncio.run(self._run_stream(sse_events))
        message = data["choices"][0]["message"]
        self.assertEqual(message["content"], "喵呜")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], "{\"q\":\"x\"}")

    def test_http_error_raises_with_status_code(self) -> None:
        with self.assertRaises(_client.OpenAICompatibleError) as ctx:
            asyncio.run(self._run_stream([], status_code=404, body="<!DOCTYPE html>"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_sse_error_event_raises_with_502(self) -> None:
        with self.assertRaises(_client.OpenAICompatibleError) as ctx:
            asyncio.run(self._run_stream([
                {"type": "error", "error": {"code": "server_error", "message": "boom"}},
            ]))
        self.assertEqual(ctx.exception.status_code, 502)

    async def _run_stream(
        self,
        events: list[dict],
        *,
        status_code: int = 200,
        body: str = "",
        append_done: bool = False,
        fail_if_read_past: bool = False,
    ) -> dict:
        if status_code >= 400:
            lines = [body]
        else:
            lines = [
                f"data: {json.dumps(event, ensure_ascii=False)}" for event in events
            ]
            if append_done:
                lines.append("data: [DONE]")

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)
        fake_client.stream = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=_FakeSseResponse(
                    status_code,
                    lines,
                    body,
                    fail_if_read_past=fail_if_read_past,
                )),
                __aexit__=AsyncMock(return_value=False),
            ),
        )
        payload = {
            "model": "gpt-5.6-luna",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        with patch.object(_client, "httpx") as fake_httpx:
            fake_httpx.AsyncClient.return_value = fake_client
            return await _client._stream_responses_attempt(
                url=_RESPONSES_URL,
                headers={"Authorization": "Bearer x"},
                payload=payload,
                timeout=30.0,
                proxy="",
                dash_stream_id=None,
                dash_mod=None,
            )


if __name__ == "__main__":
    unittest.main()
