from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx


PACKAGE = "main_dispatch_fixture"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1] / "src/catty_qq_ai")]
sys.modules[PACKAGE] = package
client = importlib.import_module(f"{PACKAGE}.openai_client")
dispatch = importlib.import_module(f"{PACKAGE}.dispatch_context")
contracts = importlib.import_module(f"{PACKAGE}.task_contracts")
switches = importlib.import_module(f"{PACKAGE}.tool_switches")


class MainDispatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_catalog_contains_disabled_tools_but_permissions_stay_separate(self):
        tool_module = importlib.import_module(f"{PACKAGE}.tools")
        config = client.Config(catty_tools_enabled=True)
        catalog = tool_module.complete_tool_catalog(config)
        names = {item["function"]["name"] for item in catalog}
        self.assertEqual(names, set(tool_module.ALL_TOOL_SCHEMAS))
        allowed = {item["function"]["name"] for item in tool_module.available_tool_schemas(config, is_private=False)}
        self.assertIn("catty_nsfw_search", names)
        self.assertNotIn("catty_nsfw_search", allowed)
        state = switches.ToolSwitches("main", permitted=allowed)
        self.assertEqual(state.refresh(catalog)[:-1], catalog)
        self.assertFalse(state.apply('{"enable":["catty_nsfw_search"]}')["ok"])
        self.assertEqual(state.denial("catty_nsfw_search")["error"], "tool_not_allowed")

    async def test_role_switches_support_multiround_history_and_vision(self):
        names = ["catty_scan_messages", "catty_look_image", "catty_run_code"]
        tools = [{"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}} for name in names]
        state = switches.ToolSwitches("research", permitted=names[:2], enabled=set())
        messages = [{"role": "system", "content": "unchanged persona and cached history " * 5000}, {"role": "user", "content": [{"type": "text", "text": "Read the previous group message and inspect its image."}, {"type": "image_url", "image_url": {"url": "https://example.test/original.png"}}]}]
        captured = []
        executed = []
        responses = [
            [("blocked", "catty_run_code", "{}"), ("switch-scan", "catty_tool_switch", '{"enable":["catty_scan_messages"]}')],
            [("scan", "catty_scan_messages", "{}")],
            [("switch-image", "catty_tool_switch", '{"enable":["catty_look_image"]}')],
            [("look", "catty_look_image", '{"ref":"m1#0"}')],
        ]

        async def raw(**kwargs):
            captured.append(copy.deepcopy(kwargs))
            index = len(captured) - 1
            if index == len(responses):
                message = {"role": "assistant", "content": "The group image contains a chart."}
            else:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}} for identifier, name, arguments in responses[index]]}
            return {"choices": [{"message": message, "finish_reason": "stop"}]}

        async def execute(name, arguments):
            executed.append(name)
            return {"ok": True, "image_ref": "m1#0", "text": "chart"}

        config = client.Config(catty_openai_base_url="https://offline.example/v1", catty_openai_api_key="TOKEN", catty_openai_model="fixture", catty_prompt_cache_enabled=False)
        with patch.object(client, "_post_chat_completion_raw", side_effect=raw), patch.object(client, "_cloud_is_unhealthy", return_value=False), patch.object(client, "_maybe_distill_reply"):
            result = await client.chat_completion_with_tools(config, messages, tools=tools, tool_executor=execute, tools_provider=lambda: tools, tool_switches=state, max_rounds=6)
        self.assertEqual(result, "The group image contains a chart.")
        self.assertEqual(executed, ["catty_scan_messages", "catty_look_image"])
        self.assertEqual(len(captured), 5)
        for request in captured:
            self.assertEqual(request["messages"][:2], messages)
            self.assertEqual(request["tools"], captured[0]["tools"])
            self.assertIn("catty_run_code", [item["function"]["name"] for item in request["tools"]])
            self.assertEqual(sum(item.get("name") == switches.SWITCH_MESSAGE_NAME for item in request["messages"]), 1)
        receipts = [item for item in captured[-1]["messages"] if item.get("role") == "tool"]
        self.assertEqual({item["tool_call_id"] for item in receipts}, {"blocked", "switch-scan", "scan", "switch-image", "look"})
        self.assertIn("tool_not_allowed", next(item["content"] for item in receipts if item["tool_call_id"] == "blocked"))

    async def test_wire_request_preserves_full_context_and_original_is_retrievable(self):
        captured = []

        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 1}})

        messages = [{"role": "system", "content": "Long persona and reference. " * 30000}]
        messages.extend({"role": "user" if index % 2 == 0 else "assistant", "content": f"Past exchange {index}. " * 1000} for index in range(20))
        messages.append({"role": "user", "content": [{"type": "text", "text": "Please research the current task and inspect the picture."}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJDREVGRw=="}}]})
        original = copy.deepcopy(messages)
        with tempfile.TemporaryDirectory() as directory:
            identity = contracts.TaskIdentity("123", "group:456", "fadianji")
            archive = dispatch.ContextArchive(Path(directory))
            token = dispatch.bind_dispatch(archive, identity, "wire-turn", "Use the current persona's concise voice.")
            try:
                with patch.object(client, "_http_client", side_effect=lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond))), patch.object(client, "_log_cache_stats"):
                    result = await client._post_chat_completion_raw(
                        base_url="https://offline.example/v1", api_key="TOKEN", model="main-model",
                        messages=messages, timeout=5, proxy="", temperature=0.2, max_tokens=128,
                        extra_headers={}, extra_body={}, tools=[dispatch.CONTEXT_TOOL_SCHEMA],
                    )
                self.assertEqual(result["choices"][0]["message"]["content"], "ok")
                self.assertEqual(len(captured), 1)
                self.assertGreater(dispatch.get_dispatch_stats()["projected_tokens"], 25000)
                self.assertEqual(dispatch.get_dispatch_stats()["hard_tokens"], 0)
                self.assertEqual(captured[0]["messages"], original)
                self.assertEqual(captured[0]["tools"], [dispatch.CONTEXT_TOOL_SCHEMA])
                self.assertEqual(messages, original)
                first = archive.read(identity, "wire-turn", "m00000", max_chars=6000)
                self.assertEqual(first["text"], original[0]["content"][:6000])
                self.assertTrue(first["truncated"])
                with self.assertRaises(contracts.TaskError):
                    archive.read(contracts.TaskIdentity("other", "group:456"), "wire-turn", "m00000")
            finally:
                dispatch.reset_dispatch(token)


if __name__ == "__main__":
    unittest.main()