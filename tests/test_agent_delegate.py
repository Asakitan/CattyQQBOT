from pathlib import Path
import asyncio
import gc
import importlib
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = types.ModuleType("catty_qq_ai")
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault("catty_qq_ai", _PACKAGE)
delegate = importlib.import_module("catty_qq_ai.agent_delegate")
accounting = importlib.import_module("catty_qq_ai.task_accounting")
contracts = importlib.import_module("catty_qq_ai.task_contracts")


def schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


class FakeBudget:
    def __init__(self, max_tokens=32_000):
        self.max_tokens = max_tokens
        self.reservations = {}
        self.receipts = {}

    def reserve_request(self, input_tokens, max_output_tokens):
        if input_tokens + max_output_tokens > self.snapshot()["remaining_tokens"]:
            raise contracts.TaskError("budget_exhausted", "no budget")
        request_id = str(len(self.reservations))
        self.reservations[request_id] = input_tokens + max_output_tokens
        return request_id

    def settle_request(self, request_id, prompt_tokens, completion_tokens, *, receipt_id=None):
        self.receipts[request_id] = prompt_tokens + completion_tokens
        return True

    def snapshot(self):
        used = sum(self.receipts.values())
        reserved = sum(value for key, value in self.reservations.items() if key not in self.receipts)
        return {"max_tokens": self.max_tokens, "used_tokens": used, "reserved_tokens": reserved,
                "remaining_tokens": max(0, self.max_tokens - used - reserved)}


class PureTests(unittest.TestCase):
    def test_roles_share_the_same_static_worker_prefix(self):
        research = delegate.build_worker_messages(delegate.WorkerTask("research", "read the evidence"))
        document = delegate.build_worker_messages(delegate.WorkerTask("document", "write the evidence"))
        self.assertEqual(research[0], document[0])
        self.assertIn("[Current tool switches]", research[0]["content"])

    def test_role_permissions_are_intersected_with_actual_parent_permissions(self):
        tools = [schema(name) for name in (
            "catty_web_search", "catty_fetch_url", "catty_write_file", "catty_delegate", "catty_task_create", "catty_persona",
        )]
        selected = delegate.filter_worker_tools("research", tools, {"catty_web_search", "catty_write_file", "catty_delegate"})
        self.assertEqual([item["function"]["name"] for item in selected], ["catty_web_search"])
        self.assertEqual(len(tools), 6)

    def test_privileged_arguments_cannot_be_hidden_in_nested_objects(self):
        for key in ("user_id", "is_owner", "model", "points", "artifact_root"):
            with self.subTest(key=key), self.assertRaises(contracts.TaskError):
                delegate.validate_tool_arguments(json.dumps({"query": {key: "override"}}))

    def test_scoped_knowledge_and_external_tools_are_available(self):
        names = {"catty_knowledge", "catty_task_artifact", "mcp__approved__lookup", "ext__approved__lookup"}
        selected = delegate.filter_worker_tools("research", [schema(name) for name in names | {"mcp__other__lookup"}], names)
        self.assertEqual({item["function"]["name"] for item in selected}, names)

    def test_sources_exclude_local_paths_and_keep_provider_urls(self):
        sources = delegate.collect_sources({"results": [{"url": "file:///private"}, {"url": "https://example.org", "title": "source"}]})
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["url"], "https://example.org")


class DelegatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy = contracts.TaskPolicy(data_dir=self.directory.name, max_output_tokens=64, summary_max_chars=128)
        self.identity = contracts.TaskIdentity("42", "group:100")
        self.config = types.SimpleNamespace(
            catty_audit_ai_base_url="https://worker.invalid/v1", catty_audit_ai_api_key="TOKEN",
            catty_audit_ai_model="deepseek-other", catty_audit_ai_request_timeout=30,
            catty_openai_base_url="https://main.invalid/v1", catty_openai_api_key="MAIN_TOKEN",
            catty_openai_model="main-model", catty_max_tokens=4096,
            catty_request_timeout=60, catty_http_proxy="", catty_temperature=0.2,
        )
        self.record = {"task_id": "task_1", "user_id": "42", "scope": "group:100", "persona": "fadianji",
                       "role": "research", "description": "Research the supplied material", "tier": "light", "payload": {}}
        self.executor = AsyncMock(return_value={"ok": True})
        self.provider = lambda identity: [schema("catty_web_search"), schema("catty_write_file"), schema("catty_delegate")]
        self.worker = delegate.AgentDelegator(self.config, self.policy, self.provider, self.executor)
        self.budget = FakeBudget()

    def account(self, messages):
        request = accounting.begin_worker_request("deepseek-v4.1-flash", {"messages": messages, "max_tokens": 64})
        accounting.settle_worker_request(request, {"usage": {"prompt_tokens": 8, "completion_tokens": 2}})

    async def test_complete_output_externalized_and_route_does_not_mutate_main(self):
        complete_text = "complete-output\n" * 200
        captured = []

        async def completion(config, messages, **kwargs):
            captured.append((config, messages, kwargs))
            self.assertIs(accounting.current_worker_accounting().budget, self.budget)
            self.account(messages)
            return complete_text

        self.record["payload"] = {"materials": ["explicit-material"], "references": [{"source_id": "doc:1", "text": "quoted"}]}
        with patch.object(delegate.openai_client, "chat_completion_with_tools", side_effect=completion):
            result = await self.worker.execute(self.record, self.identity, self.budget)
        self.assertEqual(len(result.summary), 128)
        self.assertEqual(Path(result.artifacts[0]["path"]).read_text(encoding="utf-8"), complete_text)
        self.assertTrue(Path(result.artifacts[0]["path"]).is_relative_to(self.policy.root / "task_1" / "artifacts"))
        self.assertNotIn("content", result.artifacts[0])
        self.assertEqual(result.metadata["usage"]["used_tokens"], 10)
        self.assertEqual(captured[0][2]["router_model"], "deepseek-v4.1-flash")
        self.assertEqual(captured[0][2]["router_base_url"], "https://worker.invalid/v1")
        self.assertEqual(self.config.catty_openai_model, "main-model")
        self.assertEqual(self.config.catty_max_tokens, 4096)
        self.assertEqual(len(captured[0][1]), 3)
        self.assertEqual(captured[0][1][-1]["name"], "catty_tool_switches")
        self.assertIn("explicit-material", captured[0][1][1]["content"])
        self.assertIsNone(accounting.current_worker_accounting())

    async def test_three_workers_two_parallel_share_budget_and_isolated_history(self):
        self.record["payload"] = {"subtasks": [{"role": "research", "description": str(index)} for index in range(3)]}
        active, peak, entered = 0, 0, 0
        ready = asyncio.Event()
        histories = []

        async def completion(config, messages, **kwargs):
            nonlocal active, peak, entered
            active += 1
            entered += 1
            peak = max(peak, active)
            if entered == 2:
                ready.set()
            await ready.wait()
            self.assertIs(accounting.current_worker_accounting().budget, self.budget)
            histories.append(messages)
            self.assertEqual(len(messages), 3)
            self.account(messages)
            messages.append({"role": "assistant", "content": "private child history"})
            await asyncio.sleep(0)
            active -= 1
            return "done"

        with patch.object(delegate.openai_client, "chat_completion_with_tools", side_effect=completion):
            result = await self.worker.execute(self.record, self.identity, self.budget)
        self.assertEqual(peak, 2)
        self.assertEqual(len({id(history) for history in histories}), 3)
        self.assertEqual(result.metadata["usage"]["used_tokens"], 30)
        self.assertEqual(len(result.artifacts), 3)

    async def test_subtask_count_nested_depth_and_untrusted_fields_rejected(self):
        invalid_payloads = [
            {"subtasks": [{"role": "research", "description": "task"}] * 4},
            {"subtasks": [{"role": "research", "description": "task", "subtasks": []}]},
            {"subtasks": [{"role": "research", "description": "task", "model": "main-model"}]},
            {"allowed_names": ["catty_delegate"]}, {"history": []}, {"artifact_root": "C:/"},
        ]
        with patch.object(delegate.openai_client, "chat_completion_with_tools", new=AsyncMock()) as completion:
            for payload in invalid_payloads:
                with self.subTest(payload=payload), self.assertRaises(contracts.TaskError):
                    await self.worker.execute({**self.record, "payload": payload}, self.identity, self.budget)
            with self.assertRaises(contracts.TaskError):
                await self.worker.execute({**self.record, "depth": 1}, self.identity, self.budget)
        completion.assert_not_awaited()

    async def test_worker_context_rejects_recursive_execution(self):
        with accounting.worker_accounting(self.budget, self.policy.worker_model):
            with self.assertRaises(contracts.TaskError):
                await self.worker.execute(self.record, self.identity, self.budget)

    async def test_identity_mismatch_and_shared_budget_preflight(self):
        for record, budget in (({**self.record, "user_id": "other"}, self.budget), (self.record, FakeBudget(100))):
            with self.assertRaises(contracts.TaskError):
                await self.worker.execute(record, self.identity, budget)
        self.assertFalse(self.budget.reservations)

    async def test_executor_inherits_identity_and_blocks_hidden_tool(self):
        self.executor.return_value = {"ok": True, "sources": [{"url": "https://example.org/evidence"}]}

        async def completion(config, messages, **kwargs):
            with self.assertRaises(contracts.TaskError):
                await kwargs["tool_executor"]("catty_delegate", "{}")
            with self.assertRaises(contracts.TaskError):
                await kwargs["tool_executor"]("catty_web_search", '{"query":"task","is_owner":true}')
            await kwargs["tool_executor"]("catty_web_search", '{"query":"task"}')
            self.account(messages)
            return "done"

        with patch.object(delegate.openai_client, "chat_completion_with_tools", side_effect=completion):
            result = await self.worker.execute(self.record, self.identity, self.budget)
        self.executor.assert_awaited_once_with(self.identity, "catty_web_search", '{"query":"task"}')
        self.assertEqual(result.sources[0]["url"], "https://example.org/evidence")
        self.assertEqual(len(result.artifacts), 2)

    async def test_document_tool_writes_only_inside_task_root(self):
        self.record["role"] = "document"

        async def completion(config, messages, **kwargs):
            with self.assertRaises(contracts.TaskError):
                await kwargs["tool_executor"]("catty_write_file", '{"filename":"../outside.md","content":"bad"}')
            await kwargs["tool_executor"]("catty_write_file", '{"filename":"report.md","content":"full source"}')
            self.account(messages)
            return "document written"

        with patch.object(delegate.openai_client, "chat_completion_with_tools", side_effect=completion):
            result = await self.worker.execute(self.record, self.identity, self.budget)
        self.executor.assert_not_awaited()
        self.assertEqual(Path(result.artifacts[0]["path"]).read_text(encoding="utf-8"), "full source")
        self.assertFalse((self.policy.root / "outside.md").exists())

    async def test_research_catalog_includes_off_tools_and_can_enable_them(self):
        async def completion(config, messages, **kwargs):
            names = {item["function"]["name"] for item in kwargs["tools"]}
            self.assertIn("catty_write_file", names)
            self.assertNotIn("catty_delegate", names)
            state = kwargs["tool_switches"]
            self.assertEqual(state.denial("catty_write_file")["error"], "tool_switched_off")
            with self.assertRaises(contracts.TaskError):
                await kwargs["tool_executor"]("catty_write_file", '{"filename":"off.md","content":"not written"}')
            self.assertTrue(state.apply('{"enable":["catty_write_file"]}')["ok"])
            await kwargs["tool_executor"]("catty_write_file", '{"filename":"enabled.md","content":"research result"}')
            self.account(messages)
            return "done"

        with patch.object(delegate.openai_client, "chat_completion_with_tools", side_effect=completion):
            result = await self.worker.execute(self.record, self.identity, self.budget)
        self.assertEqual(Path(result.artifacts[0]["path"]).read_text(encoding="utf-8"), "research result")
        self.executor.assert_not_awaited()

    async def test_cancellation_reaches_all_running_children_without_refund(self):
        self.record["payload"] = {"subtasks": [{"role": "research", "description": str(index)} for index in range(3)]}
        ready = asyncio.Event()
        entered, cancelled = 0, 0

        async def completion(config, messages, **kwargs):
            nonlocal entered, cancelled
            entered += 1
            accounting.begin_worker_request(self.policy.worker_model, {"messages": messages, "max_tokens": 64})
            if entered == 2:
                ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        failures = []
        loop.set_exception_handler(lambda current_loop, context: failures.append(context))
        try:
            with patch.object(delegate.openai_client, "chat_completion_with_tools", side_effect=completion):
                operation = asyncio.create_task(self.worker.execute(self.record, self.identity, self.budget))
                await asyncio.wait_for(ready.wait(), 5)
                operation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                del operation
                gc.collect()
                await asyncio.sleep(0)
            self.assertEqual(failures, [])
        finally:
            loop.set_exception_handler(previous_handler)
        self.assertEqual(cancelled, 2)
        self.assertEqual(len(self.budget.reservations), 2)
        self.assertGreater(self.budget.snapshot()["reserved_tokens"], 0)
        self.assertFalse(self.budget.receipts)
        self.assertFalse(self.worker._active_roots)

    async def test_task_specific_root_rejects_traversal(self):
        for task_id in ("../escape", "a/b", "a\\b", "C:escape", ""):
            with self.subTest(task_id=task_id), self.assertRaises(contracts.TaskError):
                await self.worker.execute({**self.record, "task_id": task_id}, self.identity, self.budget)


if __name__ == "__main__":
    unittest.main()