from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1] / "src/catty_qq_ai"
PACKAGE = "task_tool_fixture"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
for name in ("task_contracts", "task_tools"):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
tools = sys.modules[f"{PACKAGE}.task_tools"]


class Service:
    async def handle_tool(self, name, arguments, identity, context):
        return {"ok": True, "name": name, "owner": identity.is_owner, "user_id": identity.user_id}


class TaskToolTests(unittest.IsolatedAsyncioTestCase):
    def context(self, **changes):
        value = SimpleNamespace(
            config=SimpleNamespace(catty_agent_tasks={}), user_id="123", scope_key="group:456",
            persona=SimpleNamespace(name="fadianji"), affection_store=SimpleNamespace(is_owner=lambda user: False),
            task_service=Service(), task_root_id="", task_turn_id="", user_text="",
        )
        value.__dict__.update(changes)
        return value

    async def test_identity_comes_from_context(self):
        result = await tools.execute_task_tool("catty_delegate", {"action": "list"}, self.context())
        self.assertEqual(result["user_id"], "123")
        self.assertFalse(result["owner"])

    async def test_model_cannot_override_authority(self):
        result = await tools.execute_task_tool("catty_delegate", {"action": "create", "is_owner": True}, self.context())
        self.assertEqual(result["error"], "reserved_task_arguments")

    async def test_unknown_parameters_never_reach_service(self):
        result = await tools.execute_task_tool("catty_delegate", {"action": "create", "unknown": 1}, self.context())
        self.assertEqual(result["error"], "unknown_task_arguments")

    async def test_confirmation_requires_actual_user_text(self):
        arguments = {"action": "confirm", "task_id": "abc123"}
        result = await tools.execute_task_tool("catty_delegate", arguments, self.context(user_text="please do a report"))
        self.assertEqual(result["error"], "user_confirmation_required")
        result = await tools.execute_task_tool("catty_delegate", arguments, self.context(user_text="/任务确认 abc123"))
        self.assertTrue(result["ok"])

    async def test_nested_delegation_is_rejected(self):
        result = await tools.execute_task_tool("catty_delegate", {"action": "create"}, self.context(task_root_id="root"))
        self.assertEqual(result["error"], "nested_delegation_forbidden")

    async def test_owner_cannot_fabricate_user_confirmation(self):
        context = self.context(affection_store=SimpleNamespace(is_owner=lambda user: True))
        result = await tools.execute_task_tool("catty_delegate", {"action": "confirm", "task_id": "abc"}, context)
        self.assertEqual(result["error"], "user_confirmation_required")

    async def test_disabled_service_does_not_execute(self):
        result = await tools.execute_task_tool("catty_knowledge", {"action": "list"}, self.context(config=SimpleNamespace(catty_agent_tasks={"enabled": False})))
        self.assertEqual(result["error"], "task_service_disabled")

    def test_schemas_have_no_authority_inputs(self):
        for schema in tools.TASK_TOOL_SCHEMAS.values():
            parameters = schema["function"]["parameters"]
            self.assertFalse(parameters["additionalProperties"])
            self.assertNotIn("is_owner", parameters["properties"])


if __name__ == "__main__":
    unittest.main()