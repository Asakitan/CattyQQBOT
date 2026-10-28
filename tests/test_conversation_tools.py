from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_conversation_tools_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)
_SPEC = importlib.util.spec_from_file_location(
    f"{_PACKAGE_NAME}.tools", _ROOT / "src" / "catty_qq_ai" / "tools.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_tools = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _tools
_SPEC.loader.exec_module(_tools)

_NAMES = {"catty_session_messages", "catty_wait_messages", "catty_close_conversation"}


def _config(**overrides):
    values = {
        "catty_tools_enabled": True,
        "catty_tools_full_exposure": True,
        "catty_tools_lazy_schema_enabled": False,
        "catty_tools_disabled_in_private": [],
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _row(seq, text, *, is_self=False, identity="", is_bot=False):
    return {
        "seq": seq,
        "text": text,
        "user_id": "bot" if is_self else "user",
        "display_name": "test",
        "is_self": is_self,
        "is_bot": is_bot or is_self,
        "persona_identity": identity,
        "age_seconds": 0.0,
        "images": [],
    }


def _ctx(**overrides):
    values = {"config": _config(), "memory_store": None, "event": None}
    values.update(overrides)
    return _tools.ToolContext(**values)


async def _call(name, args, ctx, *, allowed_names=None):
    return await _tools.execute_tool_call(
        name, json.dumps(args, ensure_ascii=False), ctx, allowed_names=allowed_names
    )


class ConversationSchemaTests(unittest.TestCase):
    def test_full_lazy_intent_gate_and_serial_dispatch(self):
        for full_exposure in (False, True):
            for lazy in (False, True):
                for is_private in (False, True):
                    with self.subTest(full=full_exposure, lazy=lazy, private=is_private):
                        cfg = _config(
                            catty_tools_full_exposure=full_exposure,
                            catty_tools_lazy_schema_enabled=lazy,
                        )
                        schemas = {
                            item["function"]["name"]: item
                            for item in _tools.available_tool_schemas(cfg, is_private=is_private)
                        }
                        self.assertTrue(_NAMES <= schemas.keys())
                        registry = _tools._LAZY_TOOL_SCHEMAS if lazy else _tools.ALL_TOOL_SCHEMAS
                        for name in _NAMES:
                            self.assertEqual(schemas[name], registry[name])
                            params = schemas[name]["function"]["parameters"]
                            self.assertFalse(params["additionalProperties"])
                            self.assertNotIn("scope", params["properties"])
                            self.assertNotIn("user_id", params["properties"])
                            self.assertEqual(_tools.tool_execution_mode(name), "write")

    def test_individual_global_and_private_gates(self):
        for name in _NAMES:
            with self.subTest(name=name):
                flag = f"{name}_enabled"
                cfg = _config(**{flag: False})
                names = {s["function"]["name"] for s in _tools.available_tool_schemas(cfg, is_private=False)}
                self.assertNotIn(name, names)
                private = _config(catty_tools_disabled_in_private=[name])
                names = {s["function"]["name"] for s in _tools.available_tool_schemas(private, is_private=True)}
                self.assertNotIn(name, names)
        self.assertEqual(_tools.available_tool_schemas(_config(catty_tools_enabled=False), is_private=False), [])
        self.assertIn("pending 不代表已关闭", _tools.tools_system_hint())


class ConversationExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_recall_is_scoped_to_the_current_user(self):
        async def archive(query, limit):
            self.assertEqual(query, "咖啡")
            return [
                {"record_id": 1, "speaker_id": "7", "speaker_name": "甲", "text": "喜欢咖啡", "active": True},
                {"record_id": 2, "speaker_id": "8", "speaker_name": "乙", "text": "别人的咖啡", "active": True},
            ]
        ctx = _ctx(
            config=_config(catty_tools_cache_ttl_seconds=0),
            memory_store=types.SimpleNamespace(recall=lambda **kwargs: {"matches": []}),
            event=types.SimpleNamespace(user_id=7), insight_archive_fn=archive,
        )
        current = await _call("catty_recall", {"scope": "current_user", "keywords": "咖啡"}, ctx)
        self.assertEqual([item["record_id"] for item in current["archive_matches"]], [1])
        other = await _call("catty_recall", {"scope": "specific_user", "user_id": "8", "keywords": "咖啡"}, ctx)
        self.assertNotIn("archive_matches", other)

    async def test_real_dispatch_rejects_untrusted_addressing_and_allowlist(self):
        ctx = _ctx(recent_messages=[_row(1, "only current")])
        for name, args in (
            ("catty_session_messages", {"action": "recent", "scope": "group:other"}),
            ("catty_session_messages", {"action": "own", "user_id": "other"}),
            ("catty_wait_messages", {"after_seq": 0, "scope": "group:other"}),
            ("catty_close_conversation", {"reason": "done", "user_id": "other"}),
        ):
            with self.subTest(name=name, args=args):
                result = await _call(name, args, ctx)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "unexpected_argument")
        self.assertIsNone(ctx.pending_closure)
        denied = await _call("catty_session_messages", {"action": "recent"}, ctx, allowed_names=set())
        self.assertEqual(denied["reason"], "not_in_allowlist")
        ctx.config.catty_session_messages_enabled = False
        disabled = await _call("catty_session_messages", {"action": "recent"}, ctx)
        self.assertEqual(disabled["reason"], "config_disabled")

    async def test_recent_unread_own_revision_and_dynamic_snapshot(self):
        rows = [
            _row(1, "incoming"),
            _row(2, "old persona", is_self=True, identity="catty"),
            _row(3, "other bot", is_bot=True, identity="catty@r2"),
            _row(4, "missing identity", is_self=True),
            _row(5, "my reply", is_self=True, identity="catty@r2"),
        ]
        ctx = _ctx(
            recent_messages=[_row(999, "stale snapshot")],
            message_snapshot_fn=lambda: list(rows),
            persona=types.SimpleNamespace(name="catty", definition_revision="r2"),
        )
        recent = await _call("catty_session_messages", {"action": "recent", "limit": 2}, ctx)
        self.assertEqual([r["seq"] for r in recent["messages"]], [4, 5])
        own = await _call("catty_session_messages", {"action": "own"}, ctx)
        self.assertEqual([r["seq"] for r in own["messages"]], [5])
        rows.append(_row(6, "new"))
        unread = await _call("catty_session_messages", {"action": "unread", "limit": 2}, ctx)
        self.assertEqual([r["seq"] for r in unread["messages"]], [1, 2])
        self.assertEqual(unread["latest_seq"], 6)
        self.assertEqual(unread["cursor"], 0)
        self.assertFalse(unread["buffer_may_be_incomplete"])

    async def test_read_watermark_only_advances_to_existing_sequences(self):
        rows = [_row(2, "a"), _row(5, "b"), _row(10, "c")]
        calls = []
        state = {"cursor": 0}

        def cursor(operation, seq):
            calls.append((operation, seq))
            if operation == "set":
                state["cursor"] = max(state["cursor"], seq)
            return state["cursor"]

        ctx = _ctx(message_snapshot_fn=lambda: list(rows), message_cursor_fn=cursor)
        initial = await _call("catty_session_messages", {"action": "unread"}, ctx)
        self.assertTrue(initial["buffer_may_be_incomplete"])
        marked = await _call("catty_session_messages", {"action": "mark_read", "through_seq": 4}, ctx)
        self.assertEqual(marked["cursor"], 2)
        self.assertIn(("set", 2), calls)
        marked = await _call("catty_session_messages", {"action": "mark_read", "through_seq": 9}, ctx)
        self.assertEqual(marked["cursor"], 5)
        unread = await _call("catty_session_messages", {"action": "unread"}, ctx)
        self.assertEqual([r["seq"] for r in unread["messages"]], [10])
        self.assertEqual(unread["oldest_seq"], 2)
        self.assertFalse(unread["buffer_may_be_incomplete"])
        rows.clear()
        marked = await _call("catty_session_messages", {"action": "mark_read", "through_seq": 999}, ctx)
        self.assertEqual(marked["cursor"], 5)
        self.assertEqual([seq for operation, seq in calls if operation == "set"], [2, 5])

    async def test_fallback_cursor_is_local_and_does_not_regress(self):
        first = _ctx(recent_messages=[_row(2, "a"), _row(7, "b")])
        second = _ctx(recent_messages=[_row(2, "other session")])
        marked = await _call("catty_session_messages", {"action": "mark_read"}, first)
        self.assertEqual(marked["cursor"], 7)
        unchanged = await _call("catty_session_messages", {"action": "mark_read", "through_seq": 2}, first)
        self.assertEqual(unchanged["cursor"], 7)
        other = await _call("catty_session_messages", {"action": "unread"}, second)
        self.assertEqual(other["cursor"], 0)
        self.assertEqual([r["seq"] for r in other["messages"]], [2])

    async def test_wait_immediate_and_callback_results_use_after_seq_and_cap_timeout(self):
        rows = [_row(3, "old"), _row(5, "existing")]
        captured = []

        async def waiter(after_seq, timeout):
            captured.append((after_seq, timeout))
            return [_row(5, "old again"), _row(8, "new")]

        ctx = _ctx(message_snapshot_fn=lambda: list(rows), message_wait_fn=waiter)
        immediate = await _call("catty_wait_messages", {"after_seq": 3}, ctx)
        self.assertEqual([r["seq"] for r in immediate["messages"]], [5])
        self.assertEqual(captured, [])
        result = await _call("catty_wait_messages", {"after_seq": 5, "timeout_seconds": 100}, ctx)
        self.assertEqual(captured, [(5, 3.0)])
        self.assertEqual(result["status"], "messages")
        self.assertEqual(result["after_seq"], 5)
        self.assertEqual(result["latest_seq"], 8)
        self.assertEqual([r["seq"] for r in result["messages"]], [8])

    async def test_wait_polls_dynamic_snapshot_without_wait_callback(self):
        rows = [_row(1, "old")]
        ctx = _ctx(message_snapshot_fn=lambda: list(rows))
        task = asyncio.create_task(_call("catty_wait_messages", {"timeout_seconds": 0.5}, ctx))
        await asyncio.sleep(0)
        rows.append(_row(2, "new"))
        result = await asyncio.wait_for(task, 1.0)
        self.assertEqual(result["status"], "messages")
        self.assertEqual([r["seq"] for r in result["messages"]], [2])

    async def test_wait_timeout_cancels_callback(self):
        cleaned = asyncio.Event()

        async def waiter(_after_seq, _timeout):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        ctx = _ctx(message_wait_fn=waiter)
        result = await asyncio.wait_for(
            _call("catty_wait_messages", {"timeout_seconds": 0.02}, ctx), 0.5
        )
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["messages"], [])
        await asyncio.wait_for(cleaned.wait(), 0.5)

    async def test_wait_timeout_does_not_block_on_slow_cancellation(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def waiter(_after_seq, _timeout):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished.set()
            return []

        ctx = _ctx(message_wait_fn=waiter)
        task = asyncio.create_task(_call("catty_wait_messages", {"timeout_seconds": 0.02}, ctx))
        await asyncio.wait_for(entered.wait(), 0.5)
        result = await asyncio.wait_for(task, 0.5)
        self.assertEqual(result["status"], "timeout")
        release.set()
        await asyncio.wait_for(finished.wait(), 0.5)

    async def test_outer_cancellation_cancels_waiter(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def waiter(_after_seq, _timeout):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        task = asyncio.create_task(_call("catty_wait_messages", {}, _ctx(message_wait_fn=waiter)))
        await asyncio.wait_for(started.wait(), 0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cleaned.wait(), 0.5)

    async def test_close_is_only_a_pending_proposal_and_uses_current_snapshot(self):
        rows = [_row(3, "hi"), _row(8, "bye")]
        ctx = _ctx(recent_messages=[_row(99, "stale")], message_snapshot_fn=lambda: list(rows))
        result = await _call("catty_close_conversation", {"reason": " done "}, ctx)
        self.assertEqual(result, {
            "ok": True,
            "status": "pending",
            "closed": False,
            "pending_closure": {"reason": "done", "observed_seq": 8},
        })
        self.assertEqual(ctx.pending_closure, {"reason": "done", "observed_seq": 8})
        result["pending_closure"]["reason"] = "changed"
        self.assertEqual(ctx.pending_closure["reason"], "done")

    async def test_old_context_and_invalid_arguments(self):
        ctx = _tools.ToolContext(_config(), None, None)
        result = await _call("catty_session_messages", {"action": "recent"}, ctx)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["cursor"], 0)
        unavailable = await _call("catty_wait_messages", {}, ctx)
        self.assertEqual(unavailable["error"], "message_wait_unavailable")
        immediate = await _call("catty_wait_messages", {"timeout_seconds": 0}, ctx)
        self.assertEqual(immediate["status"], "timeout")
        for name, args in (
            ("catty_session_messages", {"action": "other"}),
            ("catty_session_messages", {"action": "recent", "through_seq": 1}),
            ("catty_session_messages", {"action": "mark_read", "through_seq": -1}),
            ("catty_wait_messages", {"after_seq": -1}),
            ("catty_wait_messages", {"timeout_seconds": -1}),
            ("catty_close_conversation", {"reason": "x" * 201}),
        ):
            with self.subTest(name=name, args=args):
                invalid = await _call(name, args, ctx)
                self.assertFalse(invalid["ok"])
        self.assertIsNone(ctx.pending_closure)
        pending = await _call("catty_close_conversation", {}, ctx)
        self.assertEqual(pending["pending_closure"]["observed_seq"], 0)


if __name__ == "__main__":
    unittest.main()
