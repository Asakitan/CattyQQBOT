from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nonebot

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    nonebot.init()
except Exception:
    pass

_existing_package = sys.modules.get("catty_qq_ai")
if _existing_package is not None and getattr(_existing_package, "__file__", None) is None:
    sys.modules.pop("catty_qq_ai", None)
    for _name in [k for k in sys.modules if k.startswith("catty_qq_ai.")]:
        sys.modules.pop(_name, None)

import catty_qq_ai as plugin
from catty_qq_ai import openai_client


def _cfg(**overrides):
    base = {
        "catty_reply_hard_deadline_seconds": 0.05,
        "catty_reply_retry_deadline_seconds": 0.05,
        "catty_reply_deadline_attempts": 2,
        "catty_request_timeout": 120.0,
        "catty_tools_enabled": True,
        "catty_max_tokens": 100,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class ReplyDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_retry_keeps_schemas_and_effort_but_never_executes_tools(self) -> None:
        config = plugin.Config(
            catty_openai_base_url="https://api.openai.com/v1/responses",
            catty_openai_api_key="TOKEN", catty_openai_model="gpt-6.1-terra",
            catty_reply_hard_deadline_seconds=0.01, catty_reply_retry_deadline_seconds=1,
            catty_reply_deadline_attempts=1,
        )
        initial = [{"type": "function", "function": {"name": "lookup"}}]
        final = initial + [{"type": "function", "function": {"name": "loaded"}}]
        calls = []

        async def fake(config, messages, **kwargs):
            calls.append((kwargs, openai_client.get_current_reasoning_effort()))
            if len(calls) == 1:
                kwargs["reply_state"]._remember_request(messages, final)
                await asyncio.Event().wait()
            return "done"

        with patch.object(openai_client, "chat_completion_with_tools", fake):
            reply = await plugin._main_reply_with_deadline(
                config, [{"role": "user", "content": "TARGET"}], tools=initial,
                tool_executor=object(), max_rounds=2, max_calls_per_round=1,
            )
        self.assertEqual(reply, "done")
        self.assertEqual(calls[1][0]["tools"], final)
        self.assertEqual(calls[1][0]["tool_choice"], "none")
        self.assertIsNone(calls[1][0]["tool_executor"])
        self.assertIsNone(calls[1][1])

    async def test_degraded_retry_withdraws_file_capability_without_mutating_original(self) -> None:
        from catty_qq_ai.request_intent import append_context_request_hint

        original = [{"role": "user", "content": "把示例保存为文件"}]
        append_context_request_hint(original, {"catty_write_file"})
        before = copy.deepcopy(original)
        attempts = []

        async def fake(config, messages, **kwargs):
            attempts.append((copy.deepcopy(messages), kwargs))
            if len(attempts) == 1:
                await asyncio.Event().wait()
            return "尚未生成文件"

        with patch.object(openai_client, "chat_completion_with_tools", fake):
            reply = await plugin._main_reply_with_deadline(
                _cfg(), original,
                tools=[{"type": "function", "function": {"name": "catty_write_file"}}],
                tool_executor=object(), max_rounds=3, max_calls_per_round=1,
            )
        self.assertEqual(reply, "尚未生成文件")
        self.assertEqual(len(attempts), 2)
        self.assertIn("【本轮文件交付能力】", str(attempts[0][0]))
        self.assertNotIn("【本轮文件交付能力】", str(attempts[1][0]))
        self.assertIsNone(attempts[1][1]["tools"])
        self.assertEqual(attempts[0][1]["tool_choice"], "auto")
        self.assertEqual(original, before)

    async def test_degraded_retry_withdraws_harness_tool_guide(self) -> None:
        from catty_qq_ai.harness_prompt import append_harness_agent_hint

        original = [{"role": "system", "content": "【机机·evidence】\n合成证据"}, {"role": "user", "content": "测试"}]
        append_harness_agent_hint(original, ["catty_web_search"])
        before = copy.deepcopy(original)
        attempts = []

        async def fake(config, messages, **kwargs):
            attempts.append((copy.deepcopy(messages), kwargs))
            if len(attempts) == 1:
                await asyncio.Event().wait()
            return "完成"

        with patch.object(openai_client, "chat_completion_with_tools", fake):
            reply = await plugin._main_reply_with_deadline(_cfg(), original, tools=[{"type": "function"}], tool_executor=object(), max_rounds=1, max_calls_per_round=1)
        self.assertEqual(reply, "完成")
        self.assertIn("catty_web_search", str(attempts[0][0]))
        self.assertNotIn("catty_web_search", str(attempts[1][0]))
        self.assertIn("本轮没有可用的上述补查工具", str(attempts[1][0]))
        self.assertIsNone(attempts[1][1]["tools"])
        self.assertEqual(original, before)

    async def test_timeout_cancels_then_resends_without_tools(self) -> None:
        calls: list[dict] = []

        async def fake(config, messages, *, tools, tool_executor, max_rounds,
                       max_calls_per_round, router_base_url="", router_api_key="",
                       router_model="", router_label="", tool_choice="auto", reply_state=None):
            calls.append({
                "tools": tools,
                "effort": openai_client.get_current_reasoning_effort(),
                "payload_effort": openai_client._with_thinking_max_defaults(
                    "https://example.invalid/v1", "TOKEN", "deepseek-v4.1-flash", {}
                )["reasoning_effort"],
            })
            if len(calls) == 1:
                await asyncio.sleep(5)
            return "模型自己的回答"

        with patch.object(openai_client, "chat_completion_with_tools", fake):
            result = await plugin._main_reply_with_deadline(
                _cfg(),
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function"}],
                tool_executor=object(),
                max_rounds=3,
                max_calls_per_round=3,
            )

        self.assertEqual(result, "模型自己的回答")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["tools"], [{"type": "function"}])
        self.assertIsNone(calls[0]["effort"])
        self.assertEqual(calls[0]["payload_effort"], "max")
        self.assertIsNone(calls[1]["tools"])
        self.assertEqual(calls[1]["effort"], "low")
        self.assertEqual(calls[1]["payload_effort"], "low")

    async def test_every_attempt_times_out_raises(self) -> None:
        attempts = []
        async def fake(config, messages, **kwargs):
            attempts.append(kwargs)
            await asyncio.sleep(5)

        with patch.object(openai_client, "chat_completion_with_tools", fake):
            with self.assertRaises(asyncio.TimeoutError):
                await plugin._main_reply_with_deadline(
                    _cfg(catty_reply_deadline_attempts=1),
                    [{"role": "user", "content": "hi"}],
                    tools=None,
                    tool_executor=None,
                    max_rounds=1,
                    max_calls_per_round=1,
                )
        self.assertEqual(len(attempts), 2)

    async def test_completed_tool_receipt_survives_real_loop_deadline(self) -> None:
        config = plugin.Config(
            catty_openai_base_url="https://example.invalid/v1",
            catty_openai_api_key="TOKEN",
            catty_openai_model="TARGET",
            catty_reply_hard_deadline_seconds=0.05,
            catty_reply_retry_deadline_seconds=0.1,
            catty_reply_deadline_attempts=1,
        )
        dispatches = []
        executions = []
        retries = []

        async def post(**kwargs):
            dispatches.append(kwargs)
            if len(dispatches) == 1:
                return {"choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "fixture-call", "type": "function",
                    "function": {"name": "catty_write_file", "arguments": "{}"},
                }]}}]}
            await asyncio.Event().wait()

        async def execute(name, args):
            executions.append(name)
            return {"ok": True, "receipt": "fixture-committed"}

        async def plain(config, messages):
            retries.append(messages)
            return "done"

        original = [{"role": "user", "content": "make fixture"}]
        with patch.object(openai_client, "_post_chat_completion_raw", post), \
             patch.object(openai_client, "chat_completion", plain), \
             patch.object(openai_client, "_cloud_is_unhealthy", return_value=False), \
             patch.object(openai_client, "_route_native", return_value=False), \
             patch.object(openai_client, "_maybe_distill_reply"):
            reply = await plugin._main_reply_with_deadline(
                config, original,
                tools=[{"type": "function", "function": {"name": "catty_write_file"}}],
                tool_executor=execute, max_rounds=3, max_calls_per_round=1,
            )
        self.assertEqual(reply, "done")
        self.assertEqual(executions, ["catty_write_file"])
        self.assertIn("fixture-committed", str(retries))
        self.assertEqual(sum(m.get("role") == "tool" for m in retries[0]), 1)
        self.assertEqual(original, [{"role": "user", "content": "make fixture"}])

    async def test_zero_retry_attempts_runs_once(self) -> None:
        calls = []

        async def fake(config, messages, **kwargs):
            calls.append(kwargs)
            await asyncio.Event().wait()

        with patch.object(openai_client, "chat_completion_with_tools", fake):
            with self.assertRaises(asyncio.TimeoutError):
                await plugin._main_reply_with_deadline(
                    _cfg(catty_reply_deadline_attempts=0), [], tools=None,
                    tool_executor=None, max_rounds=1, max_calls_per_round=1,
                )
        self.assertEqual(len(calls), 1)


class ReasoningOverrideTests(unittest.TestCase):
    def test_override_lowers_and_resets(self) -> None:
        url = "https://opencode.ai/zen/go/v1/chat/completions"
        token = openai_client.set_current_reasoning_effort("low")
        try:
            body = openai_client._with_thinking_max_defaults(
                url, "key", "deepseek-v4.1-flash", {}
            )
            self.assertEqual(body["reasoning_effort"], "low")
        finally:
            openai_client.reset_current_reasoning_effort(token)
        body = openai_client._with_thinking_max_defaults(url, "key", "deepseek-v4.1-flash", {})
        self.assertEqual(body["reasoning_effort"], "max")

    def test_explicit_effort_takes_precedence_over_retry_override(self) -> None:
        url = "https://opencode.ai/zen/go/v1/chat/completions"
        token = openai_client.set_current_reasoning_effort("low")
        try:
            for model in ("deepseek-v4.1-flash", "gpt-5.6-luna"):
                for effort in ("high", "low"):
                    with self.subTest(model=model, effort=effort):
                        body = openai_client._with_thinking_max_defaults(
                            url, "key", model, {"reasoning_effort": effort}
                        )
                        self.assertEqual(body["reasoning_effort"], effort)
        finally:
            openai_client.reset_current_reasoning_effort(token)

    def test_stream_deadline_round_trip(self) -> None:
        token = openai_client.set_current_stream_deadline(42.0)
        try:
            self.assertEqual(openai_client.get_current_stream_deadline(), 42.0)
        finally:
            openai_client.reset_current_stream_deadline(token)
        self.assertEqual(openai_client.get_current_stream_deadline(), 0.0)


if __name__ == "__main__":
    unittest.main()
