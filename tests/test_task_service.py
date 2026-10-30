from __future__ import annotations

import asyncio
import importlib.util
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / "src/catty_qq_ai"
PACKAGE = "task_service_fixture"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
for name in ("task_contracts", "task_tools", "task_store", "task_runtime", "knowledge_base", "mcp_runtime", "extension_runtime", "task_service"):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
contracts = sys.modules[f"{PACKAGE}.task_contracts"]
TaskService = sys.modules[f"{PACKAGE}.task_service"].TaskService
task_tools = sys.modules[f"{PACKAGE}.task_tools"]


class Account:
    def __init__(self):
        self.held = {}
        self.balance = 1000

    def is_owner(self, user_id):
        return user_id == "owner"

    def reserve_task_points(self, user_id, task_id, cost):
        self.held.setdefault(task_id, cost)
        return {"ok": True}

    def settle_task_points(self, user_id, task_id, charge):
        amount = self.held.pop(task_id, 0)
        if charge:
            self.balance -= amount
        return {"ok": True}


class Delegator:
    async def execute(self, record, identity, budget):
        reservation = budget.reserve_request(10, 20)
        budget.settle_request(reservation, 10, 5)
        return contracts.TaskResult("Finished.")


class TaskServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.account = Account()
        self.delivered = asyncio.Event()
        self.config = SimpleNamespace(catty_agent_tasks={"data_dir": self.tmp.name}, catty_owner_qq="owner")
        async def deliver(record, identity, result):
            self.delivered.set()
            return {"status": "delivered", "receipt_id": record["task_id"]}
        self.service = TaskService(self.config, self.account, deliver=deliver, delegator=Delegator())
        self.context = SimpleNamespace(config=self.config, affection_store=self.account, user_id="123", scope_key="private:123", persona=SimpleNamespace(name="fadianji"), task_turn_id="turn", task_root_id="", task_service=self.service, user_text="", event=None, pending_file_deliveries=[], pending_image_segments=[], pending_voice_deliveries=[])
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def test_free_task_reaches_confirmed_delivery(self):
        receipt = await task_tools.execute_task_tool("catty_delegate", {"action": "create", "description": "Summarize this document"}, self.context)
        self.assertEqual(receipt["state"], "queued")
        await asyncio.wait_for(self.delivered.wait(), 3)
        for _ in range(100):
            record = self.service.store.get_task(receipt["task_id"], task_tools.identity_from_context(self.context))
            if record["state"] == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(self.account.balance, 1000)

    async def test_existing_request_is_idempotent(self):
        arguments = {"action": "create", "description": "One task"}
        first = await task_tools.execute_task_tool("catty_delegate", arguments, self.context)
        second = await task_tools.execute_task_tool("catty_delegate", arguments, self.context)
        self.assertEqual(first["task_id"], second["task_id"])

    async def test_knowledge_round_trip_preserves_identity(self):
        await task_tools.execute_task_tool("catty_knowledge", {"action": "ingest", "name": "notes.txt", "text": "Telescope calibration requires a flat field."}, self.context)
        result = await task_tools.execute_task_tool("catty_knowledge", {"action": "search", "query": "telescope"}, self.context)
        self.assertTrue(result["items"])
        other = SimpleNamespace(**{**self.context.__dict__, "user_id": "456", "scope_key": "private:456"})
        result = await task_tools.execute_task_tool("catty_knowledge", {"action": "search", "query": "telescope"}, other)
        self.assertFalse(result["items"])

    async def test_admin_revision_protects_upload(self):
        revision = self.service._current_revision()
        await self.service.upload_knowledge(filename="admin.txt", content=b"admin document", expected_revision=revision)
        with self.assertRaises(contracts.TaskError):
            await self.service.upload_knowledge(filename="stale.txt", content=b"stale", expected_revision=revision)

    async def test_external_failure_cannot_become_success(self):
        with self.assertRaises(contracts.TaskError) as caught:
            await self.service._external_result("root", {"ok": False, "error": "unavailable"})
        self.assertEqual(caught.exception.code, "external_tool_failed")

    async def test_fresh_context_denial_never_uses_cached_authority(self):
        self.service._contexts["task"] = self.context
        self.service.context_factory = lambda identity, record: None
        with self.assertRaises(contracts.TaskError):
            await self.service._context({"task_id": "task"}, task_tools.identity_from_context(self.context), None)

    async def test_admin_pagination_reports_full_count(self):
        identity = task_tools.identity_from_context(self.context)
        for index in range(3):
            self.service.store.quote(identity, f"page-{index}", f"Task {index}", "research", "A task")
        page = await self.service.list_tasks(limit=2)
        self.assertEqual(page["total"], 3)
        self.assertTrue(page["has_more"])
        page = await self.service.list_tasks(limit=2, offset=2)
        self.assertEqual(len(page["items"]), 1)
        self.assertFalse(page["has_more"])

    async def test_real_http_projection_keeps_task_and_document_identifiers(self):
        import httpx
        from fastapi import FastAPI
        dashboard = importlib.import_module(f"{PACKAGE}.task_dashboard")
        app = FastAPI()
        dashboard.mount_task_dashboard(app, lambda: self.service, lambda request: True)
        receipt = await task_tools.execute_task_tool("catty_delegate", {"action": "create", "title": "Projected task", "description": "A report"}, self.context)
        await self.service.upload_knowledge(filename="admin.txt", content=b"HTTP projection", expected_revision=self.service._current_revision())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/dashboard/api/tasks")
            self.assertEqual(response.status_code, 200)
            task = next(item for item in response.json()["data"]["items"] if item["task_id"] == receipt["task_id"])
            self.assertEqual(task["title"], "Projected task")
            self.assertIn("points", task)
            response = await client.get("/dashboard/api/knowledge")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["data"]["items"][0]["document_id"])

    async def test_large_external_result_keeps_complete_artifact(self):
        token = self.service._artifact_refs.set([])
        try:
            text = "complete data " * 1000
            result = await self.service._external_result("root", {"ok": True, "text": text})
            self.assertLessEqual(len(result.summary), self.service.policy.summary_max_chars)
            self.assertEqual(len(result.artifacts), 1)
            import json
            saved = json.loads(Path(result.artifacts[0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["text"], text)
        finally:
            self.service._artifact_refs.reset(token)

    async def test_real_delegate_accepts_persisted_delivery_metadata(self):
        module = importlib.import_module(f"{PACKAGE}.agent_delegate")
        config_module = importlib.import_module(f"{PACKAGE}.config")
        self.service.config = config_module.Config(
            catty_agent_tasks={"data_dir": self.tmp.name},
            catty_audit_ai_base_url="https://worker.invalid/v1",
            catty_audit_ai_api_key="TOKEN", catty_audit_ai_model="deepseek-v4.1-flash",
            catty_voice_enabled=False,
        )
        self.context.config = self.service.config
        self.service.delegator = module.AgentDelegator(self.service.config, self.service.policy, lambda identity: [], lambda *args: None)
        with patch.object(module.openai_client, "chat_completion_with_tools", new=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value="A complete research result.")):
            receipt = await task_tools.execute_task_tool("catty_delegate", {"action": "create", "description": "Read the supplied notes"}, self.context)
            await asyncio.wait_for(self.delivered.wait(), 3)
        record = self.service.store.get_task(receipt["task_id"], task_tools.identity_from_context(self.context))
        self.assertIn(record["state"], {"completed", "delivering", "delivery_pending"})
        self.assertTrue(record["result"]["artifacts"])


if __name__ == "__main__":
    unittest.main()