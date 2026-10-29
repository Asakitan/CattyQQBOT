from __future__ import annotations

import asyncio
import ast
from collections import defaultdict, deque
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_persona_evolution_runtime_fixture"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules[PACKAGE] = package
client = types.ModuleType(f"{PACKAGE}.openai_client")
client._extract_content = lambda response: response["choices"][0]["message"]["content"]
client._post_chat_completion_raw = AsyncMock()


def _fixture_aux_route(config):
    return types.SimpleNamespace(
        base_url=str(getattr(config, "catty_audit_ai_base_url", "") or "").strip()
        or str(getattr(config, "catty_ai_fallback_base_url", "") or "").strip()
        or str(getattr(config, "catty_openai_base_url", "") or "").strip(),
        api_key=str(getattr(config, "catty_audit_ai_api_key", "") or "").strip()
        or str(getattr(config, "catty_ai_fallback_api_key", "") or "").strip()
        or str(getattr(config, "catty_openai_api_key", "") or "").strip(),
        model=str(getattr(config, "catty_audit_ai_model", "") or "").strip()
        or str(getattr(config, "catty_ai_fallback_model", "") or "").strip()
        or str(getattr(config, "catty_openai_model", "") or "").strip(),
        timeout=getattr(config, "catty_request_timeout", None),
        temperature=getattr(config, "catty_temperature", None),
        max_tokens=None,
        extra_headers={},
        extra_body=dict(getattr(config, "catty_openai_extra_body", {}) or {}),
        context_tokens=int(getattr(config, "catty_model_context_tokens", 0) or 128_000),
    )


client.aux_route = _fixture_aux_route
sys.modules[client.__name__] = client
runtime = importlib.import_module(f"{PACKAGE}.persona_evolution_runtime")
store_module = importlib.import_module(f"{PACKAGE}.persona_evolution_store")
budget_module = importlib.import_module(f"{PACKAGE}.persona_evolution_budget")


class PersonaEvolutionConfigTests(unittest.TestCase):
    def test_defaults_and_maximum(self):
        config_module = importlib.import_module(f"{PACKAGE}.config")
        config = config_module.Config()
        self.assertTrue(config.catty_persona_evolution_enabled)
        self.assertEqual(config.catty_persona_evolution_daily_token_budget, 30_000_000)
        self.assertEqual(config.catty_persona_evolution_interval_minutes, 120)
        with self.assertRaises(ValidationError):
            config_module.Config(catty_persona_evolution_daily_token_budget=30_000_001)
        with self.assertRaises(ValidationError):
            config_module.Config(catty_persona_evolution_daily_token_budget=-1)

    def test_config_loader_exports_evolution_values(self):
        spec = importlib.util.spec_from_file_location("persona_evolution_config_loader", ROOT / "catty_config_loader.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            module._apply_config({"persona": {
                "evolution_enabled": False, "evolution_daily_token_budget": 123,
                "evolution_interval_minutes": 30,
            }}, Path(root))
            self.assertEqual(os.environ["CATTY_PERSONA_EVOLUTION_ENABLED"], "false")
            self.assertEqual(os.environ["CATTY_PERSONA_EVOLUTION_DAILY_TOKEN_BUDGET"], "123")
            self.assertEqual(os.environ["CATTY_PERSONA_EVOLUTION_INTERVAL_MINUTES"], "30")


def response(style="更自然地接话。", usage=None):
    return {
        "choices": [{"message": {"content": json.dumps({"style": style}, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10} if usage is None else usage,
    }


def persona(name):
    return types.SimpleNamespace(name=name, char_name=name, core_persona=f"{name} 的原角色卡", definition_revision=None)


def message(text, *, who="user", identity="", timestamp=None):
    return types.SimpleNamespace(
        text=text, is_self=who == "self", is_bot=who == "bot", persona_identity=identity,
        created_at=time.monotonic() if timestamp is None else timestamp,
    )


class PersonaEvolutionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = store_module.PersonaEvolutionStore(root / "personas")
        self.ledger = budget_module.BudgetLedger(root / "budget.sqlite3")
        self.config = types.SimpleNamespace(
            catty_persona_evolution_enabled=True, catty_openai_api_key="fixture-token",
            catty_openai_model="fixture-model", catty_openai_base_url="https://example.test/v1",
            catty_model_context_tokens=100, catty_request_timeout=5, catty_http_proxy="",
            catty_openai_extra_headers={}, catty_temperature=0.3,
            catty_openai_extra_body={"reasoning_effort": "high", "n": 9, "max_tokens": 99999},
        )
        self.examples = [{"role": "assistant", "content": "原角色的消息"}]
        self.commit = AsyncMock(side_effect=lambda revision, style: self.store.save("catty", "card", revision, style))

    async def call(self, *, current=lambda: True):
        return await runtime.evolve_once(
            config=self.config, name="catty", base_text="card", examples=self.examples,
            store=self.store, ledger=self.ledger, still_current=current, commit=self.commit,
        )

    async def test_success_uses_one_request_and_reported_usage(self):
        fake = AsyncMock(return_value=response())
        with patch.object(runtime, "_post_chat_completion_raw", fake):
            self.assertEqual(await self.call(), "updated")
        self.assertEqual(self.store.load("catty", "card")[0], "更自然地接话。")
        self.assertEqual(self.ledger.used_today(), 10)
        args = fake.await_args.kwargs
        self.assertEqual(args["max_attempts"], 1)
        self.assertEqual(args["max_tokens"], 4096)
        self.assertEqual(args["extra_body"], {"reasoning_effort": "high"})
        self.assertEqual(args["tools"], None)
        self.assertFalse(args["stream"])

    async def test_timeout_keeps_full_reservation(self):
        with patch.object(runtime, "_post_chat_completion_raw", AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(TimeoutError):
                await self.call()
        self.assertEqual(self.ledger.used_today(), 4196)
        self.commit.assert_not_awaited()
        self.assertEqual(self.store.load("catty", "card"), ("", None))

    async def test_missing_or_excess_usage_does_not_release_or_commit(self):
        for usage in ({}, {"total_tokens": 5000}):
            with self.subTest(usage=usage):
                with patch.object(runtime, "_post_chat_completion_raw", AsyncMock(return_value=response(usage=usage))):
                    self.assertEqual(await self.call(), "usage_unconfirmed")
        self.assertEqual(self.ledger.used_today(), 2 * 4196)
        self.commit.assert_not_awaited()

    async def test_bad_model_response_still_settles_usage(self):
        bad = response()
        bad["choices"][0]["message"]["content"] = "not JSON"
        with patch.object(runtime, "_post_chat_completion_raw", AsyncMock(return_value=bad)):
            self.assertEqual(await self.call(), "invalid_response")
        self.assertEqual(self.ledger.used_today(), 10)
        self.commit.assert_not_awaited()

    async def test_switch_before_request_is_released_without_call(self):
        fake = AsyncMock(return_value=response())
        with patch.object(runtime, "_post_chat_completion_raw", fake):
            self.assertEqual(await self.call(current=lambda: False), "stale")
        fake.assert_not_awaited()
        self.commit.assert_not_awaited()
        self.assertEqual(self.ledger.used_today(), 0)

    async def test_scope_switch_during_request_does_not_update_old_or_new_card(self):
        started = asyncio.Event()
        finish = asyncio.Event()
        selected = {"name": "catty"}

        async def request(**kwargs):
            started.set()
            await finish.wait()
            return response()

        async def commit(revision, style):
            if selected["name"] != "catty":
                return False
            return self.store.save("catty", "card", revision, style)

        self.commit = AsyncMock(side_effect=commit)
        with patch.object(runtime, "_post_chat_completion_raw", request):
            task = asyncio.create_task(self.call(current=lambda: selected["name"] == "catty"))
            await asyncio.wait_for(started.wait(), 5)
            selected["name"] = "fadianji"
            finish.set()
            self.assertEqual(await task, "stale")
        self.assertEqual(self.ledger.used_today(), 10)
        self.assertEqual(self.store.load("catty", "card"), ("", None))
        self.assertEqual(self.store.load("fadianji", "card"), ("", None))

    def test_only_current_persona_messages_after_last_switch_are_sampled(self):
        items = [
            message("旧角色", who="self", identity="other", timestamp=1),
            message("旧对话", timestamp=2),
            message("旧角色2", who="self", identity="other", timestamp=3),
            message("新用户", timestamp=4),
            message("新角色1", who="self", identity="catty", timestamp=5),
            message("另一个机器人", who="bot", timestamp=6),
            message("新角色2", who="self", identity="catty", timestamp=7),
            message("新角色3", who="self", identity="catty", timestamp=8),
        ]
        self.assertEqual(runtime.scoped_examples(items, "catty", now=10), [
            {"role": "user", "content": "新用户"},
            {"role": "assistant", "content": "新角色1"},
            {"role": "assistant", "content": "新角色2"},
            {"role": "assistant", "content": "新角色3"},
        ])
        self.assertEqual(runtime.scoped_examples(items, "other", now=10), [])
        self.assertEqual(runtime.scoped_examples(items, "catty", now=100000), [])

    def test_usage_prefers_larger_consistent_count_and_never_estimates(self):
        self.assertEqual(runtime._reported_total({"usage": {"total_tokens": 5, "prompt_tokens": 7, "completion_tokens": 3}}), 10)
        self.assertEqual(runtime._reported_total({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}), 10)
        self.assertIsNone(runtime._reported_total({"usage": {"prompt_tokens": 7}}))
        self.assertIsNone(runtime._reported_total({"usage": {}}))


def load_tick(namespace):
    source = ROOT / "src" / "catty_qq_ai" / "__init__.py"
    parsed = ast.parse(source.read_text(encoding="utf-8"))
    node = next(item for item in parsed.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "_persona_evolution_tick")
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    values = {"__package__": PACKAGE, "asyncio": asyncio, "Path": Path, **namespace}
    exec(compile(module, str(source), "exec"), values)
    return values["_persona_evolution_tick"]


class PersonaEvolutionTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tick_shares_card_by_persona_without_touching_other_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = types.SimpleNamespace(
                catty_persona_evolution_enabled=True, catty_persona_evolution_daily_token_budget=30000000,
                catty_memory_path=str(root / "memory.json"), catty_openai_api_key="fixture",
                catty_openai_model="fixture", catty_openai_base_url="https://example.test/v1",
                catty_model_context_tokens=100, catty_request_timeout=5, catty_http_proxy="",
                catty_openai_extra_headers={}, catty_openai_extra_body={}, catty_temperature=0.3,
            )
            active = {"group:a": persona("catty"), "private:b": persona("fadianji")}
            recent = {
                scope: deque([message("hi"), *(message(f"reply {i}", who="self", identity=p.name) for i in range(3))])
                for scope, p in active.items()
            }
            manager = types.SimpleNamespace(root=root / "personas")
            namespace = {
                "config": config, "persona_runtime": manager, "_recent_conversation_messages": recent,
                "_persona_for_scope": active.__getitem__, "_persona_identity": lambda p: p.name,
                "_base_persona_scope": lambda scope: scope, "_persona_config_transition_lock": asyncio.Lock(),
                "_persona_transition_locks": defaultdict(asyncio.Lock),
                "logger": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
            }
            async def request(**kwargs):
                data = json.loads(kwargs["messages"][1]["content"])
                return response(style=f"{data['name']} 的自然语气")
            with patch.object(runtime, "_post_chat_completion_raw", request):
                await load_tick(namespace)()
            store = store_module.PersonaEvolutionStore(manager.root)
            self.assertEqual(store.load("catty", runtime.persona_base_text(active["group:a"]))[0], "catty 的自然语气")
            self.assertEqual(store.load("fadianji", runtime.persona_base_text(active["private:b"]))[0], "fadianji 的自然语气")
            self.assertFalse((manager.root / "evolution" / "other.json").exists())
            self.assertEqual(budget_module.BudgetLedger(root / "persona_evolution_budget.sqlite3").used_today(), 20)

    async def test_real_tick_checks_current_role_after_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = types.SimpleNamespace(
                catty_persona_evolution_enabled=True, catty_persona_evolution_daily_token_budget=30000000,
                catty_memory_path=str(root / "memory.json"), catty_openai_api_key="fixture",
                catty_openai_model="fixture", catty_openai_base_url="https://example.test/v1",
                catty_model_context_tokens=100, catty_request_timeout=5, catty_http_proxy="",
                catty_openai_extra_headers={}, catty_openai_extra_body={}, catty_temperature=0.3,
            )
            active = {"group:a": persona("catty")}
            recent = {"group:a": deque([message("hi"), *(message(f"reply {i}", who="self", identity="catty") for i in range(3))])}
            manager = types.SimpleNamespace(root=root / "personas")
            namespace = {
                "config": config, "persona_runtime": manager, "_recent_conversation_messages": recent,
                "_persona_for_scope": active.__getitem__, "_persona_identity": lambda p: p.name,
                "_base_persona_scope": lambda scope: scope, "_persona_config_transition_lock": asyncio.Lock(),
                "_persona_transition_locks": defaultdict(asyncio.Lock),
                "logger": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
            }
            started, finish = asyncio.Event(), asyncio.Event()

            async def request(**kwargs):
                started.set()
                await finish.wait()
                return response()

            with patch.object(runtime, "_post_chat_completion_raw", request):
                task = asyncio.create_task(load_tick(namespace)())
                await asyncio.wait_for(started.wait(), 5)
                active["group:a"] = persona("fadianji")
                finish.set()
                await task
            self.assertFalse((manager.root / "evolution" / "catty.json").exists())
            self.assertFalse((manager.root / "evolution" / "fadianji.json").exists())
            self.assertEqual(budget_module.BudgetLedger(root / "persona_evolution_budget.sqlite3").used_today(), 10)

    async def test_cancelled_write_keeps_switch_lock_until_disk_write_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = types.SimpleNamespace(
                catty_persona_evolution_enabled=True, catty_persona_evolution_daily_token_budget=30000000,
                catty_memory_path=str(root / "memory.json"), catty_openai_api_key="fixture",
                catty_openai_model="fixture", catty_openai_base_url="https://example.test/v1",
                catty_model_context_tokens=100, catty_request_timeout=5, catty_http_proxy="",
                catty_openai_extra_headers={}, catty_openai_extra_body={}, catty_temperature=0.3,
            )
            active = {"group:a": persona("catty")}
            recent = {"group:a": deque([message("hi"), *(message(f"reply {i}", who="self", identity="catty") for i in range(3))])}
            manager = types.SimpleNamespace(root=root / "personas")
            locks = defaultdict(asyncio.Lock)
            namespace = {
                "config": config, "persona_runtime": manager, "_recent_conversation_messages": recent,
                "_persona_for_scope": active.__getitem__, "_persona_identity": lambda p: p.name,
                "_base_persona_scope": lambda scope: scope, "_persona_config_transition_lock": asyncio.Lock(),
                "_persona_transition_locks": locks,
                "logger": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
            }
            entered, release = threading.Event(), threading.Event()
            original_save = store_module.PersonaEvolutionStore.save

            def delayed_save(store, *args):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("fixture write was not released")
                return original_save(store, *args)

            with patch.object(runtime, "_post_chat_completion_raw", AsyncMock(return_value=response())), \
                 patch.object(store_module.PersonaEvolutionStore, "save", delayed_save):
                task = asyncio.create_task(load_tick(namespace)())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(locks["group:a"].locked())
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(locks["group:a"].locked())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(locks["group:a"].locked())
            store = store_module.PersonaEvolutionStore(manager.root)
            self.assertEqual(store.load("catty", runtime.persona_base_text(active["group:a"]))[0], "更自然地接话。")


if __name__ == "__main__":
    unittest.main()
