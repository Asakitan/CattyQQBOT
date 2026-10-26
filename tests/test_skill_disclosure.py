from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
PKG = "catty_skill_disclosure_test"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT / "src/catty_qq_ai")]
sys.modules[PKG] = package
tools = importlib.import_module(PKG + ".tools")
client = importlib.import_module(PKG + ".openai_client")
intent = importlib.import_module(PKG + ".request_intent")
runtime = importlib.import_module(PKG + ".skill_runtime")
store_module = importlib.import_module(PKG + ".skill_store")


def names(schemas):
    return {s["function"]["name"] for s in schemas}


def response(*calls):
    return {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": f"call-{index}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args, ensure_ascii=False),
        }} for index, (name, args) in enumerate(calls)
    ]}}]}


def final():
    return {"choices": [{"message": {"content": "已完成"}}]}


def definition():
    return {
        "id": "cache-validation", "title": "缓存校验方法",
        "description": "核对HTTP缓存标头与过期行为的可复用步骤",
        "keywords": ["缓存", "HTTP"],
        "body": "ONLY_SELECTED_SKILL_BODY\n" + "检查缓存标头并记录验证结果。" * 280,
        "sources": [{"url": "https://example.org/cache", "title": "Cache reference",
                     "fetched_at": "2026-10-27T00:00:00+00:00", "content_sha256": "a" * 64}],
    }


class SkillDisclosureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = client.Config(
            catty_openai_base_url="https://example.invalid/v1",
            catty_openai_api_key="TOKEN", catty_openai_model="TARGET",
            catty_skills_dir=self.tmp.name, catty_tools_full_exposure=True,
            catty_tools_lazy_schema_enabled=False,
        )
        self.store = store_module.SkillStore(self.tmp.name)
        self.ctx = tools.ToolContext(
            config=self.cfg, memory_store=types.SimpleNamespace(),
            event=types.SimpleNamespace(user_id=123, group_id=456),
            user_text="PRIVATE_CHAT_SENTINEL",
        )
        self.ctx.skill_turn = runtime.SkillTurn(self.cfg)
        self.base = tools.available_tool_schemas(self.cfg, is_private=False, user_text="验证缓存")
        self.allowed = names(self.base)
        self.wire = []
        self.executed = []
        client._reset_cloud_health_for_tests()
        self.addCleanup(client._reset_cloud_health_for_tests)
        self.enterContext(patch.object(client, "_route_native", return_value=False))
        self.enterContext(patch.object(client, "_maybe_distill_reply"))
        self.raw = self.enterContext(patch.object(client, "_post_chat_completion_raw", new_callable=AsyncMock))
        self.raw.side_effect = AssertionError("unexpected network request")
        self.plain = self.enterContext(patch.object(client, "chat_completion", new_callable=AsyncMock))
        self.plain.side_effect = AssertionError("unexpected plain fallback")

    def provider(self):
        schemas = [*self.base, *tools.skill_tool_schemas(self.ctx)]
        self.allowed.clear()
        self.allowed.update(names(schemas))
        return schemas

    async def executor(self, name, args_json):
        self.executed.append(name)
        return await tools.execute_tool_call(name, args_json, self.ctx, allowed_names=self.allowed)

    async def run_loop(self):
        return await client.chat_completion_with_tools(
            self.cfg, [{"role": "user", "content": "验证缓存，PRIVATE_CHAT_SENTINEL"}],
            tools=self.base, tool_executor=self.executor, tools_provider=self.provider,
            max_rounds=8, max_calls_per_round=3, tool_choice="auto",
        )

    async def test_search_then_selection_loads_complete_body_only_after_choice(self):
        self.assertTrue(self.store.save_verified(definition())["ok"])
        original = deepcopy(self.base)

        async def post(**kwargs):
            self.wire.append(deepcopy(kwargs))
            index = len(self.wire)
            if index == 1:
                self.assertEqual({n for n in names(kwargs["tools"]) if n.startswith("catty_skill_")}, {"catty_skill_search"})
                self.assertNotIn("cache-validation", json.dumps(kwargs))
                return response(("catty_skill_search", {"query": "HTTP缓存"}))
            if index == 2:
                self.assertIn("catty_skill_load", names(kwargs["tools"]))
                self.assertNotIn("ONLY_SELECTED_SKILL_BODY", json.dumps(kwargs))
                return response(("catty_skill_load", {"id": "cache-validation"}))
            receipt = next(m for m in reversed(kwargs["messages"]) if m.get("role") == "tool")
            payload = json.loads(receipt["content"])
            self.assertEqual("".join(payload["skill"]["instructions"]), definition()["body"])
            self.assertLessEqual(len(receipt["content"]), client._TOOL_HISTORY_MAX_JSON_CHARS)
            return final()

        self.raw.side_effect = post
        self.assertEqual(await self.run_loop(), "已完成")
        self.assertEqual(self.executed, ["catty_skill_search", "catty_skill_load"])
        self.assertEqual(self.base, original)
        self.assertTrue(all(w["tool_choice"] == "auto" for w in self.wire))

    async def test_same_round_load_is_not_authorized_by_search_in_same_response(self):
        self.assertTrue(self.store.save_verified(definition())["ok"])

        async def post(**kwargs):
            self.wire.append(deepcopy(kwargs))
            if len(self.wire) == 1:
                return response(("catty_skill_search", {"query": "缓存"}),
                                ("catty_skill_load", {"id": "cache-validation"}))
            receipts = [json.loads(m["content"]) for m in kwargs["messages"] if m.get("role") == "tool"]
            self.assertIn("tool_not_allowed", json.dumps(receipts[-1]))
            self.assertNotIn("ONLY_SELECTED_SKILL_BODY", json.dumps(receipts))
            return final()

        self.raw.side_effect = post
        await self.run_loop()
        self.assertEqual(self.executed, ["catty_skill_search"])

    async def test_fetch_learn_then_next_turn_search_without_private_input(self):
        generated = []

        async def generate(config, messages):
            generated.append(deepcopy(messages))
            result = definition()
            result.pop("sources")
            result["shareable"] = True
            return json.dumps(result, ensure_ascii=False)

        self.ctx.skill_turn = runtime.SkillTurn(self.cfg, completion_fn=generate)

        async def fetch(args, ctx):
            return {"ok": True, "url": "https://example.org/cache", "status_code": 200,
                    "title": "Cache reference", "text": "HTTP缓存通过Cache-Control和ETag验证。" * 20}

        async def post(**kwargs):
            self.wire.append(deepcopy(kwargs))
            if len(self.wire) == 1:
                self.assertNotIn("catty_skill_save", names(kwargs["tools"]))
                return response(("catty_fetch_url", {"url": "https://example.org/cache"}))
            if len(self.wire) == 2:
                self.assertIn("catty_skill_save", names(kwargs["tools"]))
                receipts = [json.loads(m["content"]) for m in kwargs["messages"] if m.get("role") == "tool"]
                receipt_id = receipts[-1]["skill_receipt_id"]
                return response(("catty_skill_save", {"receipt_ids": [receipt_id]}))
            saved = json.loads(next(m["content"] for m in reversed(kwargs["messages"]) if m.get("role") == "tool"))
            self.assertTrue(saved["ok"], saved)
            return final()

        self.raw.side_effect = post
        with patch.dict(tools._EXECUTORS, {"catty_fetch_url": fetch}):
            await self.run_loop()
        self.assertEqual(len(generated), 1)
        self.assertNotIn("PRIVATE_CHAT_SENTINEL", json.dumps(generated))
        next_turn = runtime.SkillTurn(self.cfg)
        self.assertEqual(next_turn.schemas(), [])
        found = await next_turn.search({"query": "缓存"})
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["candidates"][0]["id"], "cache-validation")
        self.assertNotIn("ONLY_SELECTED_SKILL_BODY", json.dumps(found))

    async def test_fallback_forwards_schema_provider_and_recovery_state(self):
        state = client.ToolReplyState()
        with patch.object(client, "_check_mc_gate_or_raise", new_callable=AsyncMock), \
             patch.object(client, "_warmup_fallback_if_cold", new_callable=AsyncMock), \
             patch.object(client, "_fallback_should_strip_system", return_value=False), \
             patch.object(client, "chat_completion_with_tools", new_callable=AsyncMock) as loop:
            loop.return_value = "done"
            await client._post_fallback_chat(self.cfg, [], tools=self.base,
                tool_executor=self.executor, tools_provider=self.provider, reply_state=state)
        self.assertEqual(loop.call_args.kwargs["tools_provider"], self.provider)
        self.assertIs(loop.call_args.kwargs["reply_state"], state)

    async def test_degraded_retry_cannot_reopen_dynamic_skill_tools(self):
        tree = ast.parse((ROOT / "src/catty_qq_ai/__init__.py").read_text(encoding="utf-8-sig"))
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_main_reply_with_deadline")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fn], type_ignores=[])
        namespace = {"__package__": PKG, "asyncio": asyncio, "time": time,
                     "logger": types.SimpleNamespace(warning=lambda *a: None)}
        exec(compile(ast.fix_missing_locations(module), str(ROOT / "src/catty_qq_ai/__init__.py"), "exec"), namespace)
        cfg = types.SimpleNamespace(catty_reply_hard_deadline_seconds=0.01,
                                    catty_reply_retry_deadline_seconds=1, catty_reply_deadline_attempts=1)
        messages = [{"role": "user", "content": "查证并记住通用知识"}]
        intent.append_context_request_hint(messages, {"catty_skill_search", "catty_web_search", "catty_fetch_url"})
        original = deepcopy(messages)
        attempts = []

        async def loop(config, current, **kwargs):
            attempts.append((deepcopy(current), kwargs))
            if len(attempts) == 1:
                await asyncio.Event().wait()
            return "暂未查证"

        with patch.object(client, "chat_completion_with_tools", loop):
            result = await namespace["_main_reply_with_deadline"](
                cfg, messages, tools=self.base, tool_executor=self.executor,
                tools_provider=self.provider, max_rounds=8, max_calls_per_round=3)
        self.assertEqual(result, "暂未查证")
        self.assertIn("tools_provider", attempts[0][1])
        self.assertIsNone(attempts[1][1].get("tools_provider"))
        self.assertIsNone(attempts[1][1]["tools"])
        self.assertIsNone(attempts[1][1]["tool_executor"])
        self.assertEqual(messages, original)

    def test_production_and_simulation_both_supply_round_provider(self):
        for name, target in (("__init__.py", "_main_reply_with_deadline"),
                             ("catty_sim_chat.py", "chat_completion_with_tools")):
            tree = ast.parse((ROOT / "src/catty_qq_ai" / name).read_text(encoding="utf-8-sig"))
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == target]
            self.assertTrue(any(any(k.arg == "tools_provider" for k in n.keywords) for n in calls), name)


if __name__ == "__main__":
    unittest.main()