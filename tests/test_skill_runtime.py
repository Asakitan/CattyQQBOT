"""Offline runtime tests; run in the parent's terminal, never against cloud APIs."""
from __future__ import annotations

import asyncio
import contextvars
from copy import deepcopy
from datetime import datetime
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_skill_runtime_tests"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)
_SPEC = importlib.util.spec_from_file_location(
    f"{_PACKAGE_NAME}.skill_runtime", _ROOT / "src" / "catty_qq_ai" / "skill_runtime.py",
)
assert _SPEC is not None and _SPEC.loader is not None
runtime = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runtime
_SPEC.loader.exec_module(runtime)


def _config(**overrides):
    values = {
        "catty_skills_enabled": True,
        "catty_skills_auto_learn_enabled": True,
        "catty_skills_search_limit": 3,
        "catty_skills_max_body_chars": 6000,
        "catty_skills_learning_timeout_seconds": 20.0,
        "catty_skills_learning_max_tokens": 2400,
        "catty_max_tokens": 8000,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _definition(**overrides):
    values = {
        "id": "python-venv", "title": "Python 虚拟环境", "description": "隔离 Python 依赖的通用步骤",
        "keywords": ["Python", "虚拟环境"],
        "body": "适用：Python 3 项目。步骤：python -m venv .venv。验证：检查 sys.prefix。时效：按所用 Python 版本复核。",
        "shareable": True,
    }
    values.update(overrides)
    return values


def _fetch(**overrides):
    values = {
        "ok": True, "status_code": 200, "url": "https://docs.python.org/3/library/venv.html",
        "title": "venv 文档", "text": "公共资料：python -m venv 创建独立环境；用 sys.prefix 验证环境。",
    }
    values.update(overrides)
    return values


class _Store:
    """Small contract double; real-store compatibility has its own test below."""

    def __init__(self):
        self.records = {}
        self.search_calls = []
        self.load_calls = []
        self.save_calls = []

    def put(self, definition=None):
        skill = deepcopy(definition or _definition())
        skill.pop("shareable", None)
        skill.setdefault("sources", [])
        skill["updated_at"] = "2026-10-27T00:00:00+00:00"
        skill["body_chars"] = len(skill["body"])
        sha = hashlib.sha256(json.dumps(skill, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.records[skill["id"]] = (skill, sha)
        return sha

    def search(self, query, limit=3):
        self.search_calls.append((query, limit))
        if query == "no-match":
            return {"ok": True, "candidates": []}
        # Deliberately return body/extras to verify runtime projects a whitelist.
        return {"ok": True, "candidates": [
            {**deepcopy(skill), "sha256": sha, "private_extra": "NEVER_EXPOSE"}
            for skill, sha in list(self.records.values())[:limit]
        ]}

    def load(self, skill_id, expected_sha256):
        self.load_calls.append((skill_id, expected_sha256))
        if skill_id not in self.records:
            return {"ok": False, "error": "not_found"}
        skill, sha = self.records[skill_id]
        if sha != expected_sha256:
            return {"ok": False, "error": "sha_conflict"}
        return {"ok": True, "skill": deepcopy(skill), "sha256": sha}

    def save_verified(self, definition, overwrite=False, expected_sha256=None):
        self.save_calls.append((deepcopy(definition), overwrite, expected_sha256))
        skill_id = definition["id"]
        if skill_id in self.records:
            if not overwrite or expected_sha256 != self.records[skill_id][1]:
                return {"ok": False, "error": "sha_conflict"}
        elif overwrite:
            return {"ok": False, "error": "not_found"}
        sha = self.put(definition)
        return {"ok": True, "status": "updated" if overwrite else "created", "id": skill_id, "sha256": sha}


class SkillTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = _Store()
        self.calls = []

        async def complete(config, messages):
            self.calls.append((config, deepcopy(messages)))
            return json.dumps(_definition(), ensure_ascii=False)

        self.complete = complete
        self.turn = self.make_turn()

    def make_turn(self, config=None, completion=None):
        turn = runtime.SkillTurn(config or _config(), completion_fn=completion or self.complete)
        turn._store = self.store
        return turn

    def receipt(self, turn=None, **overrides):
        result = (turn or self.turn).observe_tool_result("catty_fetch_url", _fetch(**overrides))
        return result["skill_receipt_id"]

    async def test_constructor_and_schemas_do_not_open_or_scan_store(self):
        turn = runtime.SkillTurn(_config(catty_skills_dir="DO_NOT_TOUCH"))
        self.assertIsNone(turn._store)
        self.assertEqual(turn.schemas(), [])
        self.assertIsNone(turn._store)
        self.assertFalse((await turn.load({"id": "python-venv"}))["ok"])
        self.assertIsNone(turn._store)
        self.assertFalse((await turn.save({"receipt_ids": ["forged"]}))["ok"])
        self.assertIsNone(turn._store)

    async def test_only_search_candidates_unlock_load_and_never_expose_body(self):
        sha = self.store.put()
        result = await self.turn.search({"query": "Python"})
        self.assertTrue(result["ok"])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(_definition()["body"], encoded)
        self.assertNotIn('"body":', encoded)
        self.assertNotIn("private_extra", encoded)
        schemas = self.turn.schemas()
        self.assertEqual([item["function"]["name"] for item in schemas], ["catty_skill_load"])
        self.assertEqual(schemas[0]["function"]["parameters"]["properties"]["id"]["enum"], ["python-venv"])
        loaded = await self.turn.load({"id": "python-venv"})
        self.assertEqual(loaded["sha256"], sha)
        self.assertEqual("".join(loaded["skill"]["instructions"]), _definition()["body"])
        self.assertNotIn("body", loaded["skill"])

    async def test_empty_search_only_advises_fetch_and_does_not_generate(self):
        result = await self.turn.search({"query": "no-match"})
        self.assertEqual(result["candidates"], [])
        self.assertIn("catty_fetch_url", result["hint"])
        self.assertEqual(self.turn.schemas(), [])
        self.assertFalse((await self.turn.save({"receipt_ids": ["search-summary"]}))["ok"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.save_calls, [])

    async def test_load_rejects_unsearched_and_changed_sha(self):
        self.store.put()
        self.assertFalse((await self.turn.load({"id": "python-venv"}))["ok"])
        await self.turn.search({"query": "Python"})
        self.store.put(_definition(body="different version"))
        self.assertFalse((await self.turn.load({"id": "python-venv"}))["ok"])
        self.assertEqual(self.turn._loaded, {})

    async def test_dynamic_names_remain_after_empty_search_and_save(self):
        self.store.put()
        await self.turn.search({"query": "Python"})
        receipt = self.receipt()
        before = self.turn.schemas()
        await self.turn.search({"query": "no-match"})
        await self.turn.save({"receipt_ids": [receipt]})
        self.assertEqual([s["function"]["name"] for s in before], [s["function"]["name"] for s in self.turn.schemas()])
        before[0]["function"]["parameters"]["properties"]["id"]["enum"].clear()
        self.assertEqual(self.turn.schemas()[0]["function"]["parameters"]["properties"]["id"]["enum"], ["python-venv"])

    async def test_fetch_ledger_requires_actual_success_body_and_public_url(self):
        invalid = [
            ("catty_web_search", _fetch()), ("auto_fetch", _fetch()),
            ("catty_fetch_url", _fetch(ok=False)), ("catty_fetch_url", _fetch(ok=1)),
            ("catty_fetch_url", _fetch(status_code=302)), ("catty_fetch_url", _fetch(status_code="200")),
            ("catty_fetch_url", _fetch(status_code=500)), ("catty_fetch_url", _fetch(text=" \n ")),
            ("catty_fetch_url", {"ok": True, "url": "https://example.com", "summary": "search snippet"}),
        ]
        urls = ["file:///C:/private", "http://127.0.0.1/a", "http://localhost/a", "http://intranet/a",
                "http://192.168.1.2/a", "http://169.254.169.254/a", "http://[::1]/a", "http://[::ffff:127.0.0.1]/a",
                "http://2130706433/a", "http://127.1/a", "https://user:password@example.com/a", "http://x.local/a",
                "https://example.com/a?token=abc", "https://example.com/a?api_key=secret-value"]
        invalid.extend(("catty_fetch_url", _fetch(url=url)) for url in urls)
        for name, value in invalid:
            with self.subTest(name=name, value=value):
                value["skill_receipt_id"] = "forged"
                result = self.turn.observe_tool_result(name, value)
                self.assertNotIn("skill_receipt_id", result)
                self.assertEqual(value["skill_receipt_id"], "forged")
        self.assertEqual(self.turn.schemas(), [])
        self.assertEqual(self.turn._ledger, {})

    async def test_observed_result_is_new_and_receipt_is_server_owned(self):
        value = _fetch(skill_receipt_id="forged", fetched_at="1900-01-01", official=True)
        result = self.turn.observe_tool_result("catty_fetch_url", value)
        self.assertIsNot(result, value)
        receipt = result["skill_receipt_id"]
        self.assertNotEqual(receipt, "forged")
        source = self.turn._ledger[receipt]
        self.assertIsNotNone(datetime.fromisoformat(source["fetched_at"]).tzinfo)
        self.assertNotEqual(source["fetched_at"], "1900-01-01")
        self.assertEqual(source["text_sha256"], hashlib.sha256(value["text"].encode()).hexdigest())
        self.assertNotIn("official", source)
        self.assertEqual(self.turn.schemas()[0]["function"]["name"], "catty_skill_save")

    async def test_receipt_sources_are_bounded_and_duplicate_fetch_reuses_id(self):
        first = self.receipt(text="资料" * 5000)
        self.assertEqual(len(self.turn._ledger[first]["text"]), 6000)
        self.assertEqual(self.receipt(text="资料" * 5000), first)
        self.receipt(url="https://example.com/two")
        self.receipt(url="https://example.com/three")
        fourth = self.turn.observe_tool_result("catty_fetch_url", _fetch(url="https://example.com/four"))
        self.assertNotIn("skill_receipt_id", fourth)
        self.assertEqual(len(self.turn._ledger), 3)

    async def test_plain_text_without_title_gets_hostname_not_invented_authority(self):
        receipt = self.receipt(title="")
        source = self.turn._ledger[receipt]
        self.assertEqual(source["title"], "docs.python.org")
        result = await self.turn.save({"receipt_ids": [receipt]})
        self.assertTrue(result["ok"], result)
        self.assertNotIn("official", self.store.save_calls[0][0]["sources"][0])

    async def test_save_accepts_only_current_receipts_not_body_urls_or_times(self):
        receipt = self.receipt()
        for extra in ({"body": "arbitrary"}, {"url": "https://example.com"}, {"sources": []}, {"fetched_at": "now"}, {"shareable": True}):
            self.assertFalse((await self.turn.save({"receipt_ids": [receipt], **extra}))["ok"])
        for values in ([], ["forged"], [receipt, receipt], "not-a-list", [None]):
            self.assertFalse((await self.turn.save({"receipt_ids": values}))["ok"])
        self.assertFalse((await self.make_turn().save({"receipt_ids": [receipt]}))["ok"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.save_calls, [])

    async def test_generation_uses_only_public_fields_and_clean_context(self):
        private_context = contextvars.ContextVar("private_chat_fixture", default=None)
        token = private_context.set("PRIVATE-RAW-CHAT-AND-USER-ID")
        configs = []

        async def complete(config, messages):
            self.assertIsNone(private_context.get())
            configs.append(config)
            self.assertEqual(set(json.loads(messages[1]["content"])), {"public_sources"})
            source = json.loads(messages[1]["content"])["public_sources"][0]
            self.assertEqual(set(source), {"title", "url", "text"})
            self.assertNotIn("PRIVATE-RAW", json.dumps(messages))
            self.assertEqual(messages[0]["content"], runtime.SKILL_LEARNING_SYSTEM_PROMPT)
            return json.dumps(_definition(sources=[{"url": "https://forged.example.com"}], fetched_at="1900", official=True), ensure_ascii=False)

        config = _config(catty_skills_learning_max_tokens=1234, catty_openai_extra_body={"max_tokens": 9999, "metadata": {"user_id": "PRIVATE-RAW"}, "reasoning_effort": "low"})
        turn = self.make_turn(config=config, completion=complete)
        try:
            receipt = self.receipt(turn, chat="PRIVATE-RAW-CHAT", user_id="PRIVATE-RAW-USER", profile="PRIVATE-RAW-PROFILE")
            result = await turn.save({"receipt_ids": [receipt]})
            self.assertTrue(result["ok"], result)
            self.assertEqual(private_context.get(), "PRIVATE-RAW-CHAT-AND-USER-ID")
        finally:
            private_context.reset(token)
        self.assertIsNot(configs[0], config)
        self.assertEqual(configs[0].catty_max_tokens, 1234)
        self.assertEqual(config.catty_max_tokens, 8000)
        self.assertEqual(configs[0].catty_openai_extra_body, {"reasoning_effort": "low"})
        self.assertEqual(config.catty_openai_extra_body["max_tokens"], 9999)
        saved = self.store.save_calls[0][0]
        self.assertNotIn("shareable", saved)
        self.assertNotIn("official", saved)
        self.assertEqual(saved["sources"][0]["url"], _fetch()["url"])
        self.assertIn("content_sha256", saved["sources"][0])
        self.assertNotIn("text", saved["sources"][0])

    async def test_private_or_credential_sources_do_not_reach_generation(self):
        for text in ("联系 alice@example.com", "电话 13812345678", "密码: realpassword", "Bearer abcdefghijklmnopqrstuvwxyz", "-----BEGIN PRIVATE KEY-----", "QQ号：123456789"):
            with self.subTest(text=text):
                result = self.turn.observe_tool_result("catty_fetch_url", _fetch(text=text))
                self.assertNotIn("skill_receipt_id", result)
        self.assertEqual(self.calls, [])

    async def test_unshareable_invalid_and_sensitive_generation_never_save(self):
        invalid = [
            '{"shareable":false}', "not-json", '```json\n{"shareable":true}\n```',
            json.dumps(_definition(shareable="true")), json.dumps(_definition(body="联系 alice@example.com")),
            json.dumps(_definition(body="api_key=abcDEF123456789")), json.dumps(_definition(body="")),
            json.dumps(_definition(body="x" * 6001)), json.dumps(_definition(id="../escape")),
            json.dumps(_definition(id="con")), json.dumps(_definition(keywords="not-list")),
            '{"shareable":false,"shareable":true}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:70]):
                async def complete(config, messages):
                    return raw
                turn = self.make_turn(completion=complete)
                result = await turn.save({"receipt_ids": [self.receipt(turn)]})
                self.assertFalse(result["ok"])
        self.assertEqual(self.store.save_calls, [])

    async def test_concurrent_duplicate_save_generates_and_commits_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def complete(config, messages):
            calls.append(messages)
            started.set()
            await release.wait()
            return json.dumps(_definition())

        turn = self.make_turn(completion=complete)
        first = self.receipt(turn)
        second = self.receipt(turn, url="https://example.com/second")
        task1 = asyncio.create_task(turn.save({"receipt_ids": [first, second]}))
        await started.wait()
        task2 = asyncio.create_task(turn.save({"receipt_ids": [second, first]}))
        release.set()
        result1, result2 = await asyncio.gather(task1, task2)
        self.assertTrue(result1["ok"], result1)
        self.assertEqual(result1, result2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.store.save_calls), 1)
        result1["ok"] = False
        self.assertTrue((await turn.save({"receipt_ids": [first, second]}))["ok"])

    async def test_cancellation_during_generation_does_not_write(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def complete(config, messages):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        turn = self.make_turn(completion=complete)
        task = asyncio.create_task(turn.save({"receipt_ids": [self.receipt(turn)]}))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.store.save_calls, [])
        self.assertEqual(turn._save_cache, {})

    async def test_commit_stays_off_loop_and_cancellation_drains_existing_write(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        owner_thread = threading.get_ident()
        worker_threads = []
        original = self.store.save_verified

        def blocked_commit(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("fixture release missing")
            return original(*args, **kwargs)

        with patch.object(self.store, "save_verified", blocked_commit):
            task = asyncio.create_task(self.turn.save({"receipt_ids": [self.receipt()]}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertNotEqual(worker_threads, [owner_thread])
                self.assertFalse(task.done())
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(len(self.store.save_calls), 1)
        self.assertIn("python-venv", self.store.records)
        self.assertEqual(self.turn._save_cache, {})

    async def test_configured_search_limit_can_use_five_candidates(self):
        turn = self.make_turn(_config(catty_skills_search_limit=5))
        for index in range(5):
            self.store.put(_definition(id=f"python-venv-{index}"))
        result = await turn.search({"query": "Python", "limit": 5})
        self.assertEqual(len(result["candidates"]), 5)

    async def test_timeout_and_generator_exception_do_not_write(self):
        async def slow(config, messages):
            await asyncio.Event().wait()
        turn = self.make_turn(_config(catty_skills_learning_timeout_seconds=0.01), slow)
        result = await turn.save({"receipt_ids": [self.receipt(turn)]})
        self.assertEqual(result["error"], "skill_learning_timeout")

        async def broken(config, messages):
            raise RuntimeError("do not expose exception or credentials")
        turn = self.make_turn(completion=broken)
        result = await turn.save({"receipt_ids": [self.receipt(turn)]})
        self.assertEqual(result["error"], "skill_generation_failed")
        self.assertEqual(self.store.save_calls, [])

    async def test_update_requires_load_and_uses_loaded_sha(self):
        sha = self.store.put()
        receipt = self.receipt()
        args = {"receipt_ids": [receipt], "existing_id": "python-venv"}
        self.assertFalse((await self.turn.save(args))["ok"])
        await self.turn.search({"query": "Python"})
        self.assertFalse((await self.turn.save(args))["ok"])
        await self.turn.load({"id": "python-venv"})
        self.assertIn("existing_id", self.turn.schemas()[1]["function"]["parameters"]["properties"])
        self.assertTrue((await self.turn.save(args))["ok"])
        self.assertEqual(self.store.save_calls[0][1:], (True, sha))

    async def test_update_race_conflict_keeps_newer_record(self):
        self.store.put()
        await self.turn.search({"query": "Python"})
        await self.turn.load({"id": "python-venv"})

        async def concurrent_update(config, messages):
            self.store.put(_definition(body="other writer's committed revision"))
            return json.dumps(_definition(body="our obsolete generated revision"))

        self.turn._completion_fn = concurrent_update
        result = await self.turn.save({"receipt_ids": [self.receipt()], "existing_id": "python-venv"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.records["python-venv"][0]["body"], "other writer's committed revision")

    async def test_new_turn_must_search_again_but_can_reuse_global_skill(self):
        saved = await self.turn.save({"receipt_ids": [self.receipt()]})
        self.assertTrue(saved["ok"], saved)
        next_turn = self.make_turn()
        self.assertEqual(next_turn.schemas(), [])
        self.assertFalse((await next_turn.load({"id": saved["id"]}))["ok"])
        self.assertTrue((await next_turn.search({"query": "Python"}))["candidates"])
        self.assertTrue((await next_turn.load({"id": saved["id"]}))["ok"])

    async def test_flags_close_all_or_only_save(self):
        self.store.put()
        disabled = self.make_turn(_config(catty_skills_enabled=False))
        self.assertFalse((await disabled.search({"query": "Python"}))["ok"])
        self.assertFalse((await disabled.load({"id": "python-venv"}))["ok"])
        self.assertNotIn("skill_receipt_id", disabled.observe_tool_result("catty_fetch_url", _fetch()))
        self.assertEqual(disabled.schemas(), [])
        readonly = self.make_turn(_config(catty_skills_auto_learn_enabled=False))
        await readonly.search({"query": "Python"})
        self.assertTrue((await readonly.load({"id": "python-venv"}))["ok"])
        self.assertNotIn("skill_receipt_id", readonly.observe_tool_result("catty_fetch_url", _fetch()))
        self.assertFalse((await readonly.save({"receipt_ids": []}))["ok"])
        self.assertEqual([s["function"]["name"] for s in readonly.schemas()], ["catty_skill_load"])

    async def test_load_survives_real_history_serializer_without_loss(self):
        client = importlib.import_module(f"{_PACKAGE_NAME}.openai_client")
        for body in ("技" * 6000, "data:" + "a" * 5995, "base64://" + "b" * 5991):
            with self.subTest(prefix=body[:10]):
                turn = self.make_turn()
                self.store.put(_definition(body=body))
                await turn.search({"query": "Python"})
                loaded = await turn.load({"id": "python-venv"})
                self.assertTrue(loaded["ok"], loaded)
                self.assertTrue(all(len(chunk) <= 1500 for chunk in loaded["skill"]["instructions"]))
                serialized = client._serialize_tool_result_for_history(loaded)
                self.assertLess(len(serialized), 12000)
                self.assertEqual("".join(json.loads(serialized)["skill"]["instructions"]), body)

    async def test_escaped_oversize_load_fails_instead_of_truncating(self):
        self.store.put(_definition(body="\x01" * 6000))
        await self.turn.search({"query": "Python"})
        result = await self.turn.load({"id": "python-venv"})
        self.assertEqual(result["error"], "skill_receipt_too_large")
        self.assertEqual(self.turn._loaded, {})

    async def test_default_completion_restores_distill_suppression_on_failure(self):
        flag = contextvars.ContextVar("test_distill_suppressed", default=False)
        fake = types.ModuleType(f"{_PACKAGE_NAME}.openai_client")
        fake.set_distill_suppressed = flag.set
        fake.reset_distill_suppressed = flag.reset

        async def broken(config, messages, **kwargs):
            self.assertTrue(flag.get())
            raise RuntimeError("failed")

        fake.chat_completion_aux = broken
        with patch.dict(sys.modules, {fake.__name__: fake}), patch.object(_PACKAGE, "openai_client", fake, create=True):
            with self.assertRaises(RuntimeError):
                await runtime._default_completion(_config(), [])
        self.assertFalse(flag.get())

    async def test_real_store_roundtrip_is_reusable_across_turns(self):
        with tempfile.TemporaryDirectory(prefix="catty-skill-runtime-") as directory:
            config = _config(catty_skills_dir=directory)
            turn = runtime.SkillTurn(config, completion_fn=self.complete)
            result = await turn.save({"receipt_ids": [self.receipt(turn)]})
            self.assertTrue(result["ok"], result)
            next_turn = runtime.SkillTurn(config, completion_fn=self.complete)
            found = await next_turn.search({"query": "Python"})
            self.assertTrue(found["candidates"], found)
            loaded = await next_turn.load({"id": result["id"]})
            self.assertTrue(loaded["ok"], loaded)
            self.assertEqual("".join(loaded["skill"]["instructions"]), _definition()["body"])


if __name__ == "__main__":
    unittest.main()