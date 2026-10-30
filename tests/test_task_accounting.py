from pathlib import Path
import asyncio
from contextlib import asynccontextmanager
import importlib
import sys
import threading
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = types.ModuleType("catty_qq_ai")
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault("catty_qq_ai", _PACKAGE)
accounting = importlib.import_module("catty_qq_ai.task_accounting")
TaskError = importlib.import_module("catty_qq_ai.task_contracts").TaskError
client = importlib.import_module("catty_qq_ai.openai_client")


class FakeBudget:
    def __init__(self, max_tokens=10_000):
        self.max_tokens = max_tokens
        self.holds = {}
        self.receipts = {}
        self.lock = threading.Lock()

    def reserve_request(self, input_tokens, max_output_tokens):
        with self.lock:
            if self.snapshot()["remaining_tokens"] < input_tokens + max_output_tokens:
                raise TaskError("budget_exhausted", "budget exhausted")
            request_id = str(len(self.holds))
            self.holds[request_id] = input_tokens + max_output_tokens
            return request_id

    def settle_request(self, request_id, prompt_tokens, completion_tokens, *, receipt_id=None):
        self.receipts[request_id] = prompt_tokens + completion_tokens
        return True

    def snapshot(self):
        used = sum(self.receipts.values())
        reserved = sum(value for key, value in self.holds.items() if key not in self.receipts)
        return {
            "max_tokens": self.max_tokens, "used_tokens": used, "reserved_tokens": reserved,
            "remaining_tokens": max(0, self.max_tokens - used - reserved),
        }


class UsageTests(unittest.TestCase):
    def test_cache_and_reasoning_are_subsets(self):
        usage = accounting.normalize_usage({
            "prompt_tokens": 100, "completion_tokens": 20,
            "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 12},
        })
        self.assertEqual(usage.total_tokens, 120)
        self.assertEqual(usage.cached_input_tokens, 80)
        self.assertEqual(usage.reasoning_output_tokens, 12)

    def test_responses_and_anthropic_usage(self):
        responses = accounting.normalize_usage({
            "input_tokens": 100, "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 80},
            "output_tokens_details": {"reasoning_tokens": 10},
        })
        anthropic = accounting.normalize_usage({
            "input_tokens": 20, "output_tokens": 20,
            "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10,
        })
        self.assertEqual(responses.total_tokens, 120)
        self.assertEqual(anthropic.total_tokens, 130)

    def test_unknown_is_not_zero(self):
        for usage in (None, {}, {"total_tokens": 100}, {"prompt_tokens": 1}, {"input_tokens": True}):
            with self.subTest(usage=usage):
                self.assertIsNone(accounting.normalize_usage(usage))

    def test_zero_complete_receipt_is_known(self):
        self.assertEqual(accounting.normalize_usage({"prompt_tokens": 0, "completion_tokens": 0}).total_tokens, 0)


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_requests_share_one_budget(self):
        budget = FakeBudget(150)

        async def reserve():
            await asyncio.sleep(0)
            try:
                return budget.reserve_request(60, 40)
            except TaskError:
                return None

        results = await asyncio.gather(reserve(), reserve())
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(budget.snapshot()["reserved_tokens"], 100)

    async def test_missing_usage_and_retry_keep_reservations(self):
        budget = FakeBudget(1000)
        first = budget.reserve_request(100, 50)
        accounting.settle_worker_request((budget, first), {})
        second = budget.reserve_request(100, 50)
        self.assertEqual(budget.snapshot()["reserved_tokens"], 300)
        accounting.settle_worker_request((budget, second), {"usage": {"prompt_tokens": 25, "completion_tokens": 5}})
        self.assertEqual(budget.snapshot()["used_tokens"], 30)
        self.assertEqual(budget.snapshot()["reserved_tokens"], 150)

    async def test_request_limits_enforced_before_reservation(self):
        budget = FakeBudget()
        with accounting.worker_accounting(budget, "deepseek-v4.1-flash", max_input_tokens=512, max_output_tokens=40):
            with self.assertRaises(TaskError):
                accounting.begin_worker_request("deepseek-v4.1-flash", {"max_tokens": 10, "messages": ["a" * 600]})
            with self.assertRaises(TaskError):
                accounting.begin_worker_request("deepseek-v4.1-flash", {"max_tokens": 41})
        self.assertEqual(len(budget.holds), 0)

    async def test_worker_context_is_scoped_and_rejects_main_model(self):
        budget = FakeBudget()
        self.assertIsNone(accounting.begin_worker_request("main-model", {}))
        with accounting.worker_accounting(budget, "deepseek-v4.1-flash"):
            with self.assertRaises(TaskError):
                accounting.begin_worker_request("main-model", {"max_tokens": 10})
            request = accounting.begin_worker_request("deepseek-v4.1-flash", {"max_tokens": 10, "messages": []})
            accounting.settle_worker_request(request, {"usage": {"prompt_tokens": 4, "completion_tokens": 2}})
        self.assertIsNone(accounting.current_worker_accounting())
        self.assertEqual(budget.snapshot()["used_tokens"], 6)


class RequestHookTests(unittest.IsolatedAsyncioTestCase):
    def request(self, **overrides):
        return {
            "base_url": "https://worker.invalid/v1", "api_key": "TOKEN",
            "model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "task"}],
            "timeout": 1.0, "proxy": "", "temperature": 0.1, "max_tokens": 32,
            "extra_headers": {}, "extra_body": {}, "max_attempts": 2, **overrides,
        }

    async def test_each_nonstream_retry_reserves_and_known_usage_settles(self):
        budget = FakeBudget(10_000)
        post = AsyncMock(side_effect=[
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json={"choices": [{"message": {"content": "done"}}],
                                      "usage": {"prompt_tokens": 7, "completion_tokens": 3}}),
        ])

        @asynccontextmanager
        async def transport(*args):
            yield types.SimpleNamespace(post=post)

        with patch.object(client, "_http_client", transport), patch.object(client, "_get_runtime_config", return_value=None), \
                patch.object(client, "_log_cache_stats"), patch.object(client, "_cache_request_dump_enabled", return_value=False), \
                patch.object(client.asyncio, "sleep", new=AsyncMock()):
            with accounting.worker_accounting(budget, "deepseek-v4.1-flash"):
                await client._post_chat_completion_raw(**self.request())
        self.assertEqual(post.await_count, 2)
        self.assertEqual(len(budget.holds), 2)
        self.assertEqual(budget.snapshot()["used_tokens"], 10)
        self.assertGreater(budget.snapshot()["reserved_tokens"], 0)

    async def test_stream_missing_usage_remains_reserved(self):
        budget = FakeBudget()
        stream = AsyncMock(return_value={"choices": [{"message": {"content": "done"}}], "usage": {}})
        with patch.object(client, "_stream_chat_completion_attempt", stream), \
                patch.object(client, "_get_runtime_config", return_value=None), patch.object(client, "_log_cache_stats"), \
                patch.object(client, "_cache_request_dump_enabled", return_value=False):
            with accounting.worker_accounting(budget, "deepseek-v4.1-flash"):
                await client._post_chat_completion_raw(**self.request(stream=True))
        self.assertEqual(len(budget.holds), 1)
        self.assertGreater(budget.snapshot()["reserved_tokens"], 0)
        self.assertEqual(budget.snapshot()["used_tokens"], 0)

    async def test_stream_cancellation_keeps_reservation(self):
        budget = FakeBudget()
        with patch.object(client, "_stream_chat_completion_attempt", new=AsyncMock(side_effect=asyncio.CancelledError)), \
                patch.object(client, "_get_runtime_config", return_value=None), \
                patch.object(client, "_cache_request_dump_enabled", return_value=False):
            with accounting.worker_accounting(budget, "deepseek-v4.1-flash"):
                with self.assertRaises(asyncio.CancelledError):
                    await client._post_chat_completion_raw(**self.request(stream=True))
        self.assertGreater(budget.snapshot()["reserved_tokens"], 0)
        self.assertIsNone(accounting.current_worker_accounting())

    async def test_main_model_and_payload_override_rejected_before_network(self):
        budget = FakeBudget()
        with patch.object(client, "_http_client") as transport:
            with accounting.worker_accounting(budget, "deepseek-v4.1-flash"):
                with self.assertRaises(TaskError):
                    await client._post_chat_completion_raw(**self.request(model="main-model"))
                with self.assertRaises(TaskError):
                    await client._post_chat_completion_raw(**self.request(extra_body={"model": "main-model"}, stream=True))
        transport.assert_not_called()
        self.assertFalse(budget.holds)

    async def test_same_model_on_main_endpoint_cannot_borrow_worker_scope(self):
        budget = FakeBudget()
        with patch.object(client, "_http_client") as transport:
            with accounting.worker_accounting(
                budget, "deepseek-v4.1-flash", base_url="https://worker.invalid/v1", api_key="TOKEN",
            ):
                with self.assertRaises(TaskError):
                    await client._post_chat_completion_raw(**self.request(base_url="https://main.invalid/v1"))
                with self.assertRaises(TaskError):
                    await client._post_chat_completion_raw(**self.request(api_key="MAIN_TOKEN"))
        transport.assert_not_called()
        self.assertFalse(budget.holds)

    async def test_ordinary_request_does_not_activate_budget_or_limits(self):
        response = {"choices": [{"message": {"content": "normal"}}]}
        with patch.object(client, "_stream_chat_completion_attempt", new=AsyncMock(return_value=response)), \
                patch.object(client, "_get_runtime_config", return_value=None), patch.object(client, "_log_cache_stats"), \
                patch.object(client, "_cache_request_dump_enabled", return_value=False):
            data = await client._post_chat_completion_raw(**self.request(model="main-model", max_tokens=99999, stream=True))
        self.assertEqual(data, response)

    async def test_tool_loop_fallback_and_final_request_keep_worker_route(self):
        budget = FakeBudget(50_000)
        config = client.Config(
            catty_openai_base_url="https://main.invalid/v1", catty_openai_api_key="MAIN_TOKEN",
            catty_openai_model="main-model", catty_max_tokens=32,
        )
        seen = []

        async def stream(**kwargs):
            seen.append(kwargs["payload"]["model"])
            if len(seen) <= 4:
                error = client.OpenAICompatibleError("unavailable")
                error.status_code = 503
                raise error
            return {"choices": [{"message": {"content": "recovered"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2}}

        tools = [{"type": "function", "function": {"name": "catty_web_search", "parameters": {"type": "object"}}}]
        with patch.object(client, "_stream_chat_completion_attempt", side_effect=stream), \
                patch.object(client, "chat_completion", new=AsyncMock()) as main, \
                patch.object(client, "_get_runtime_config", return_value=None), patch.object(client, "_log_cache_stats"), \
                patch.object(client, "_cache_request_dump_enabled", return_value=False), \
                patch.object(client.asyncio, "sleep", new=AsyncMock()):
            with accounting.worker_accounting(budget, "deepseek-v4.1-flash"):
                result = await client.chat_completion_with_tools(
                    config, [{"role": "user", "content": "task"}], tools=tools,
                    tool_executor=AsyncMock(), router_base_url="https://worker.invalid/v1",
                    router_api_key="TOKEN", router_model="deepseek-v4.1-flash",
                )
        self.assertEqual(result, "recovered")
        self.assertEqual(seen, ["deepseek-v4.1-flash"] * 5)
        self.assertEqual(len(budget.holds), 5)
        self.assertEqual(budget.snapshot()["used_tokens"], 12)
        self.assertEqual(len(budget.receipts), 1)
        main.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()