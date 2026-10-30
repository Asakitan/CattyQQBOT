"""Offline host-contract tests; never import/initialize the NoneBot plugin."""
from __future__ import annotations

import ast
import asyncio
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import replace
import copy
import importlib
import json
from pathlib import Path
import random
import re
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
_TREE = ast.parse(_SOURCE.read_text(encoding="utf-8"))
_PKG = "catty_fdj_integration_test"
_package = types.ModuleType(_PKG)
_package.__path__ = [str(_SOURCE.parent)]
sys.modules.setdefault(_PKG, _package)
runtime = importlib.import_module(f"{_PKG}.fdj_proactive_runtime")
legacy = importlib.import_module(f"{_PKG}.proactive_legacy")
markers = importlib.import_module(f"{_PKG}.reply_markers")
parsers = importlib.import_module(f"{_PKG}.parsers")
message_utils = importlib.import_module(f"{_PKG}.message_utils")
Config = importlib.import_module(f"{_PKG}.config").Config

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender


def _extract(names: set[str], namespace: dict) -> dict:
    nodes = []
    for node in _TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node = copy.deepcopy(node)
            node.decorator_list = []
            nodes.append(node)
    assert len(nodes) == len(names), names - {node.name for node in nodes}
    unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), str(_SOURCE), "exec"), namespace)
    return namespace


def _clean_namespace() -> dict:
    return _extract({"_clean_fdj_proactive_reply"}, {
        "re": re, "NO_REPLY_MARKER": markers.NO_REPLY_MARKER,
        "REPLY_SPLIT_MARKER": markers.REPLY_SPLIT_MARKER,
        "_strip_catty_markers": parsers.strip_catty_markers,
        "_strip_tone_parenthetical": markers.strip_tone_parenthetical,
        "_strip_fadianji_catty_tics": markers.strip_fadianji_catty_tics,
    })


def _event(text="今天吃什么", *, at=False, at_all=False, uid=20001):
    message = Message(text)
    if at:
        message = Message(MessageSegment.at(999)) + message
    if at_all:
        message = Message(MessageSegment.at("all")) + message
    return GroupMessageEvent(
        time=0, self_id=999, post_type="message", sub_type="normal", user_id=uid,
        message_type="group", message_id=17, message=message, original_message=message,
        raw_message=str(message), font=0, sender=Sender(user_id=uid, nickname="群友"), group_id=10001,
    )


class ProactiveRouteIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_host_uses_dedicated_proactive_completion(self):
        generate = AsyncMock(return_value="reply")
        configured = Mock(return_value=True)
        namespace = _extract({"_get_fdj_proactive_runtime"}, {
            "__package__": _PKG,
            "_fdj_proactive_runtime": None,
            "config": object(),
            "_persona_for_scope": Mock(), "get_bots": Mock(return_value={}),
            "_recent_conversation_for_scope": Mock(), "_fdj_group_reply_busy": Mock(),
            "proactive_model_configured": configured,
            "chat_completion_proactive": generate,
            "_send_fdj_proactive_text": AsyncMock(), "_remember_fdj_proactive_text": Mock(),
            "_clean_fdj_proactive_reply": Mock(), "_proactive_purpose_candidates": AsyncMock(),
            "_proactive_purpose_delivered": AsyncMock(),
        })
        host = namespace["_get_fdj_proactive_runtime"]().host
        self.assertIs(host.generate, generate)
        self.assertTrue(host.has_api_key())
        configured.assert_called_once_with(namespace["config"])

    async def test_legacy_bubble_uses_dedicated_proactive_completion(self):
        generate = AsyncMock(return_value="NO_REPLY")
        namespace = _extract({"_send_proactive_bubble"}, {
            "_locks": {"group:42": asyncio.Lock()}, "config": object(),
            "_build_proactive_messages": Mock(return_value=[]),
            "chat_completion_proactive": generate,
            "_is_no_reply": lambda value: True,
        })
        self.assertFalse(await namespace["_send_proactive_bubble"](object(), "42"))
        generate.assert_awaited_once_with(namespace["config"], [])


class TriggerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.persona = types.SimpleNamespace(
            name="fadianji", mention_only_trigger=False, followup_reply_budget=2,
            trigger_prefixes=("机机", "小机", "发电机"), directed_keywords=("机机", "发电机", "机器人"),
            feature_disabled=lambda name: False,
        )
        self.continuation = False
        self.references = []
        self.config = Config(
            catty_filter_enabled=True, catty_allowed_group_ids=set(), catty_allowed_user_ids=set(),
            catty_enable_group=True, catty_ignore_bot_self_intro_enabled=True,
        )
        self.ns = {
            "GroupMessageEvent": GroupMessageEvent, "PrivateMessageEvent": PrivateMessageEvent,
            "config": self.config, "logger": Mock(),
            "_persona_for_event": lambda event: self.persona,
            "event_plain_text": message_utils.event_plain_text,
            "has_real_at_self": message_utils.has_real_at_self,
            "extract_incoming_message": message_utils.extract_incoming_message,
            "_resolve_reply_sources": AsyncMock(side_effect=lambda *args: self.references),
            "_reply_targets_self": AsyncMock(side_effect=lambda *args: bool(self.references)),
            "_recent_bot_prompted_user": lambda event: self.continuation,
            "_remember_recent_conversation_event": Mock(),
            "_model_should_reply_to_group": AsyncMock(return_value=True),
            "_close_bot_reply_continuation": Mock(),
            "_is_session_closing": lambda text: False,
            "_reset_followup_idle": Mock(), "_bot_reply_continuation_remaining": lambda event: 2 if self.continuation else 0,
            "_has_consumed_reply_source": lambda event: False,
            "_force_direct_reply_enabled": lambda event, incoming: False,
            "_take_due_group_filter_batch": AsyncMock(return_value=None),
            "memory_store": types.SimpleNamespace(is_special_care_user=lambda event: False),
            "_expression_repeat_message": lambda bot, event: Message("复读"),
            "_keyword_reply_event_allowed": lambda event: True,
            "_is_signin_request": lambda text: "签到" in text,
            "_is_points_query_request": lambda text: "积分" in text,
        }
        _extract({
            "_fdj_explicit_wake", "_mention_only_real_at_allowed", "_fresh_mention_for_event",
            "_group_persona_trigger_allowed", "_extract_trigger_incoming", "_extract_incoming_with_voice",
            "_rule", "_expression_repeat_rule", "_affection_command_rule",
        }, self.ns)
        message_utils.set_persona_resolver(lambda event: self.persona)
        self.addCleanup(message_utils.set_persona_resolver, None)
        self.bot = types.SimpleNamespace(self_id=999)

    async def test_ordinary_and_feature_keywords_do_not_enter_llm(self):
        for text in ("今天吃什么", "帮我搜索这个", "画一张风景图", "我的积分"):
            with self.subTest(text=text):
                state = {}
                allowed = await self.ns["_rule"](self.bot, _event(text), state)
                llm = AsyncMock()
                if allowed:
                    await llm()
                llm.assert_not_awaited()
                self.assertNotIn("catty_incoming", state)
        self.ns["_take_due_group_filter_batch"].assert_not_awaited()

    async def test_special_care_cannot_open_ordinary_fdj_chat(self):
        incoming = types.SimpleNamespace(
            text="闲聊", directly_requested=True, mentioned=False, replied_to_self=False,
            used_prefix=False, needs_filter=False, opportunistic=True,
        )
        self.ns["extract_incoming_message"] = lambda *args, **kwargs: incoming
        self.assertFalse(await self.ns["_rule"](self.bot, _event(), {}))

    async def test_keywords_real_at_reference_and_existing_continuation(self):
        for text, at, reply, continuation in (
            ("机机", False, False, False), ("机机：", False, False, False),
            ("机机 在吗", False, False, False), ("发电机怎么样", False, False, False),
            ("在吗", True, False, False), ("接着说", False, True, False),
            ("接着说", False, False, True),
        ):
            with self.subTest(text=text, at=at, reply=reply, continuation=continuation):
                self.references = [types.SimpleNamespace(is_current_bot=True)] if reply else []
                self.continuation = continuation
                event, state = _event(text, at=at), {}
                self.assertTrue(await self.ns["_rule"](self.bot, event, state))
                self.assertEqual(self.ns["_fresh_mention_for_event"](event, state["catty_incoming"]), not continuation)

    async def test_broadcast_permissions_and_self_intro_guards_still_apply(self):
        self.assertFalse(await self.ns["_rule"](self.bot, _event("机机 在吗", at_all=True), {}))
        self.config.catty_allowed_group_ids = {123}
        self.assertFalse(await self.ns["_rule"](self.bot, _event("机机 在吗", at=True), {}))
        self.config.catty_allowed_group_ids = set()
        with patch.object(message_utils, "_looks_like_bot_self_intro", return_value=True):
            self.assertFalse(await self.ns["_rule"](self.bot, _event("机机 机器人自介", at=True), {}))

    async def test_weak_wake_and_continuation_require_model_admission(self):
        gate = self.ns["_model_should_reply_to_group"]
        gate.return_value = False
        self.assertFalse(await self.ns["_rule"](self.bot, _event("这个机器人挺有意思"), {}))
        gate.assert_awaited_once()
        gate.reset_mock()
        self.continuation = True
        self.assertFalse(await self.ns["_rule"](self.bot, _event("接着说"), {}))
        self.assertTrue(gate.await_args.kwargs["continuation"])
        self.ns["_close_bot_reply_continuation"].assert_called_once()
        gate.return_value = True
        state = {}
        self.assertTrue(await self.ns["_rule"](self.bot, _event("接着说"), state))
        self.assertEqual(state["catty_reply_gate_result"], {"should_reply": True, "source": "model"})

    async def test_direct_wake_and_reply_bypass_model_admission(self):
        self.ns["_model_should_reply_to_group"].return_value = False
        for event, referenced in ((_event("机机 在吗"), False), (_event("@机机 在吗"), False), (_event("在吗", at=True), False), (_event("接着说"), True)):
            with self.subTest(event=event.raw_message, referenced=referenced):
                self.references = [types.SimpleNamespace(is_current_bot=True)] if referenced else []
                self.assertTrue(await self.ns["_rule"](self.bot, event, {}))
        self.ns["_model_should_reply_to_group"].assert_not_awaited()

    async def test_existing_nsfw_context_bypasses_admission(self):
        namespace = _extract({"_model_should_reply_to_group"}, {
            "__name__": f"{_PKG}.test_runtime", "__package__": _PKG,
            "_conversation_queue_key": lambda event: "group:10001",
            "_persona_for_event": lambda event: types.SimpleNamespace(nsfw_allowed=lambda **kwargs: True),
            "_phase_scope_for": lambda scope, persona: scope,
            "_active_nsfw_group_occupant": lambda scope: ("20001", 1.0),
        })
        self.assertTrue(await namespace["_model_should_reply_to_group"](_event(), types.SimpleNamespace(text="测试"), continuation=True))
        namespace["_active_nsfw_group_occupant"] = lambda scope: None
        namespace["_is_nsfw_context"] = lambda scope, user_id, text: True
        self.assertTrue(await namespace["_model_should_reply_to_group"](_event(), types.SimpleNamespace(text="测试"), continuation=True))

    async def test_disabled_group_nsfw_cannot_bypass_admission(self):
        persona = types.SimpleNamespace(char_name="机机", nsfw_allowed=lambda **kwargs: False)
        occupant = Mock(return_value=("20001", 1.0))
        namespace = _extract({"_model_should_reply_to_group"}, {
            "__name__": f"{_PKG}.test_runtime", "__package__": _PKG,
            "_conversation_queue_key": lambda event: "group:10001",
            "_persona_for_event": lambda event: persona,
            "_phase_scope_for": lambda scope, current: scope,
            "_active_nsfw_group_occupant": occupant,
            "_is_nsfw_context": Mock(return_value=True),
            "_recent_messages_snapshot_for_scope": lambda scope, limit: [],
            "_event_message_id": lambda event: "",
            "_display_name": lambda event: "群友",
        })
        admission = importlib.import_module(f"{_PKG}.reply_admission")
        with patch.object(admission, "decide_reply", new_callable=AsyncMock, return_value=False) as decide:
            self.assertFalse(await namespace["_model_should_reply_to_group"](
                _event(), types.SimpleNamespace(text="测试"), continuation=True,
            ))
        decide.assert_awaited_once()
        occupant.assert_not_called()

    async def test_existing_followup_survives_filter_disabled_without_opening_new_windows(self):
        self.config.catty_filter_enabled = False
        self.config.catty_group_require_mention_or_prefix = True
        self.assertFalse(await self.ns["_rule"](self.bot, _event("接着说"), {}))
        self.continuation = True
        self.assertTrue(await self.ns["_rule"](self.bot, _event("接着说"), {}))
        self.assertFalse(await self.ns["_rule"](self.bot, _event("接着说", at_all=True), {}))
        self.config.catty_allowed_group_ids = {12}
        self.assertFalse(await self.ns["_rule"](self.bot, _event("接着说"), {}))
        self.config.catty_allowed_group_ids = set()
        with patch.object(message_utils, "_looks_like_bot_self_intro", return_value=True):
            self.assertFalse(await self.ns["_rule"](self.bot, _event("接着说"), {}))
        self.continuation = False
        self.assertFalse(await self.ns["_rule"](self.bot, _event("接着说"), {}))

    async def test_other_mention_only_persona_keeps_true_at_gate(self):
        self.persona.name, self.persona.mention_only_trigger = "other", True
        self.assertFalse(await self.ns["_rule"](self.bot, _event("机机 在吗"), {}))
        self.references = [types.SimpleNamespace(is_current_bot=True)]
        self.assertFalse(await self.ns["_rule"](self.bot, _event("在吗"), {}))
        self.assertTrue(await self.ns["_rule"](self.bot, _event("在吗", at=True), {}))

    async def test_catty_shares_explicit_wake_gate(self):
        self.persona.name = "catty"
        self.persona.trigger_prefixes = ("猫猫",)
        self.persona.directed_keywords = ("猫猫",)
        incoming = types.SimpleNamespace(text="搜索天气", directly_requested=True, mentioned=False,
            replied_to_self=False, used_prefix=False, needs_filter=False, opportunistic=False, has_image=False)
        self.ns["extract_incoming_message"] = lambda *a, **k: incoming
        self.assertFalse(await self.ns["_rule"](self.bot, _event("搜索天气"), {}))
        self.assertTrue(await self.ns["_rule"](self.bot, _event("猫猫 搜索天气"), {}))

    async def test_repeat_yields_to_wake_reference_and_continuation(self):
        for event, reply, continuation in (
            (_event("机机"), False, False),
            (_event("机机 在吗"), False, False), (_event("在吗", at=True), False, False),
            (_event("接着说"), True, False), (_event("接着说"), False, True),
        ):
            self.references = [types.SimpleNamespace(is_current_bot=True)] if reply else []
            self.continuation = continuation
            self.assertFalse(await self.ns["_expression_repeat_rule"](self.bot, event, {}))
        self.references, self.continuation = [], False
        self.assertTrue(await self.ns["_expression_repeat_rule"](self.bot, _event("哈哈哈"), {}))
        self.persona.name, self.persona.mention_only_trigger = "other", True
        self.assertFalse(await self.ns["_expression_repeat_rule"](self.bot, _event("机机 在吗"), {}))
        self.assertTrue(await self.ns["_expression_repeat_rule"](self.bot, _event("哈哈哈"), {}))

    async def test_points_commands_share_wake_gate(self):
        self.assertFalse(await self.ns["_affection_command_rule"](self.bot, _event("我的积分"), {}))
        for event in (_event("机机 我的积分"), _event("我的积分", at=True)):
            self.assertTrue(await self.ns["_affection_command_rule"](self.bot, event, {}))
        self.references = [types.SimpleNamespace(is_current_bot=True)]
        self.assertTrue(await self.ns["_affection_command_rule"](self.bot, _event("我的积分"), {}))


class FakeClock:
    def __init__(self):
        self.wall = 1793073600.0
        self.mono = 5000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class FakeScheduler:
    def __init__(self, root, policy):
        self.root, self.policy = root, policy
        self.done = set()
        self.claims, self.finishes, self.skips, self.human_times = [], [], [], []
        self.claim_ok = True
        self.on_claim = None
        self.window = None

    def due(self, group_id, now, *, human_times):
        self.human_times.append(list(human_times))
        if group_id in self.done or not human_times:
            return None
        if self.window is None:
            self.window = (now - 1, now + 1800)
        day = runtime.datetime.fromtimestamp(now, runtime._LOCAL_TIME).date().isoformat()
        return types.SimpleNamespace(group_id=group_id, day=day, slot_id="slot1", mode="join", night=False)

    def status(self, group_id, now):
        return {"ok": True, "slots": [{"slot_id": "slot1", "at": self.window[0], "expires_at": self.window[1]}]}

    def claim(self, candidate, now):
        self.claims.append(candidate)
        if self.on_claim:
            self.on_claim()
        if not self.claim_ok or candidate.group_id in self.done:
            return None
        self.done.add(candidate.group_id)
        return "attempt1"

    def finish(self, group_id, attempt, outcome, now):
        self.finishes.append((group_id, attempt, outcome))
        return True

    def skip(self, candidate, now, reason="unsuitable"):
        self.skips.append(reason)
        self.done.add(candidate.group_id)
        return True


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cfg = types.SimpleNamespace(catty_proactive_enabled=False)
        self.persona = types.SimpleNamespace(name="fadianji")
        self.busy = False
        self.key = True
        self.bot = types.SimpleNamespace(self_id="999", get_group_list=AsyncMock(return_value=[{"group_id": 10001}]))
        self.recent = {"group:10001": [self.row(i) for i in range(5)]}
        self.generate = AsyncMock(return_value="这话题还挺有意思")
        self.send = AsyncMock(return_value={"message_id": 88})
        self.remember = Mock()
        self.clean = _clean_namespace()["_clean_fdj_proactive_reply"]
        self.factories = []

        def factory(root, policy):
            scheduler = FakeScheduler(root, policy)
            self.factories.append(scheduler)
            return scheduler

        self.runtime = runtime.FdjProactiveRuntime(runtime.BubbleHost(
            config=lambda: self.cfg, persona=lambda scope: self.persona,
            bots=lambda: [self.bot], recent=lambda scope: self.recent.get(scope, []),
            busy=lambda scope: self.busy, has_api_key=lambda: self.key,
            generate=self.generate, send_text=self.send, remember=self.remember, clean_text=self.clean,
        ), wall=lambda: self.clock.wall, monotonic=lambda: self.clock.mono,
            scheduler_factory=factory, rng=random.Random(4))
        self.prompt = Mock(return_value=[{"role": "user", "content": "主动闲聊"}])
        self.prompt_patch = patch.object(runtime, "build_fdj_proactive_messages", self.prompt)
        self.prompt_patch.start()
        self.addCleanup(self.prompt_patch.stop)

    def row(self, index=1, *, bot=False, age=10, uid="123"):
        return types.SimpleNamespace(seq=index, created_at=self.clock.mono-age,
            is_bot=bot, user_id=uid, display_name="群友", text=f"话题{index}")

    async def test_switch_independent_defaults_ack_ledger_and_clock_conversion(self):
        self.assertTrue(await self.runtime.tick())
        self.send.assert_awaited_once()
        self.remember.assert_called_once_with(self.bot, "10001", "这话题还挺有意思", "88")
        scheduler = self.factories[0]
        self.assertEqual(scheduler.finishes[-1][-1], "sent")
        self.assertTrue(all(t == self.clock.wall-10 for t in scheduler.human_times[0]))
        self.assertEqual(self.prompt.call_args.kwargs["now"].utcoffset().total_seconds(), 28800)
        self.assertEqual(scheduler.policy.min_gap_seconds, 3600)
        self.assertEqual(scheduler.policy.failure_backoff_seconds, 3600)
        self.assertFalse(await self.runtime.tick())
        self.send.assert_awaited_once()

    async def test_purpose_delivery_requires_matching_text_and_ack(self):
        goal = {"id": 12, "owner": "member", "owner_id": "123", "text": "我想去跑步",
                "confidence": .5, "delivery_count": 0, "last_delivery_at": None}
        candidates = AsyncMock(return_value=[goal])
        receipt = AsyncMock(return_value=True)
        self.runtime.host = replace(self.runtime.host, purpose_candidates=candidates, purpose_delivered=receipt)
        self.generate.return_value = "跑步计划怎么样了"
        self.assertTrue(await self.runtime.tick())
        candidates.assert_awaited_once_with("group:10001", "fadianji")
        receipt.assert_awaited_once_with("group:10001", "fadianji", goal, "88")
        background = self.prompt.call_args.kwargs["summary"]
        self.assertIn("我想去跑步", background)
        self.assertNotIn('"owner_id"', background)

    async def test_purpose_is_not_delivered_on_unconfirmed_or_unrelated_reply(self):
        goal = {"id": 12, "owner": "member", "owner_id": "123", "text": "我想去跑步",
                "confidence": .5, "delivery_count": 0, "last_delivery_at": None}
        receipt = AsyncMock(return_value=True)
        self.runtime.host = replace(self.runtime.host, purpose_candidates=AsyncMock(return_value=[goal]), purpose_delivered=receipt)
        self.generate.return_value = "跑步计划怎么样了"
        self.send.return_value = {}
        self.assertFalse(await self.runtime.tick())
        receipt.assert_not_awaited()
        self.runtime._scheduler = None
        self.send.return_value = {"message_id": 89}
        self.generate.return_value = "今天好热"
        self.assertTrue(await self.runtime.tick())
        receipt.assert_not_awaited()

    async def test_disabled_or_missing_key_no_list_or_llm(self):
        self.cfg.catty_fdj_proactive_enabled = False
        self.assertFalse(await self.runtime.tick())
        self.cfg.catty_fdj_proactive_enabled = True
        self.key = False
        self.assertFalse(await self.runtime.tick())
        self.bot.get_group_list.assert_not_awaited()
        self.generate.assert_not_awaited()

    async def test_other_persona_or_disallowed_group_no_generation(self):
        self.persona.name = "catty"
        self.assertFalse(await self.runtime.tick())
        self.persona.name = "fadianji"
        self.cfg.catty_allowed_group_ids = {42}
        self.assertFalse(await self.runtime.tick())
        self.generate.assert_not_awaited()

    async def test_bot_only_self_and_cold_groups_do_not_generate(self):
        for rows in ([self.row(bot=True)], [self.row(uid="999")], [self.row(age=21601)], []):
            self.recent["group:10001"] = rows
            self.assertFalse(await self.runtime.tick())
        self.generate.assert_not_awaited()

    async def test_busy_before_generation_defers_without_consuming_opportunity(self):
        self.busy = True
        self.assertFalse(await self.runtime.tick())
        self.assertFalse(await self.runtime.tick())
        self.generate.assert_not_awaited()
        self.assertEqual(self.factories[0].skips, [])
        self.busy = False
        self.assertTrue(await self.runtime.tick())
        self.generate.assert_awaited_once()

    async def test_no_reply_and_long_reply_are_skipped_once(self):
        for reply in (markers.NO_REPLY_MARKER, "长" * 51):
            with self.subTest(reply=reply):
                self.factories.clear()
                self.runtime._scheduler = None
                self.generate.reset_mock()
                self.generate.return_value = reply
                self.assertFalse(await self.runtime.tick())
                self.assertFalse(await self.runtime.tick())
                self.generate.assert_awaited_once()
                self.assertEqual(self.factories[0].skips, ["no_reply"])
        self.send.assert_not_awaited()

    async def test_generation_timeout_skips_without_send(self):
        self.cfg.catty_fdj_proactive_timeout_seconds = .01

        async def block(*args):
            await asyncio.Event().wait()

        self.generate.side_effect = block
        self.assertFalse(await self.runtime.tick())
        self.assertFalse(await self.runtime.tick())
        self.assertEqual(self.factories[0].skips, ["generation_failed"])
        self.generate.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_generation_context_bound_then_reset(self):
        personas = importlib.import_module(f"{_PKG}.persona_runtime")

        async def generate(*args):
            self.assertIs(personas.active_persona_for_scope("group:10001"), self.persona)
            return "好家伙"

        self.generate.side_effect = generate
        self.assertTrue(await self.runtime.tick())
        self.assertIsNone(personas.active_persona_for_scope("group:10001"))

    async def test_persona_change_during_generation_skips(self):
        async def generate(*args):
            self.persona = types.SimpleNamespace(name="catty")
            return "好家伙"

        self.generate.side_effect = generate
        self.assertFalse(await self.runtime.tick())
        self.send.assert_not_awaited()
        self.assertEqual(self.factories[0].skips, ["changed_after_generation"])

    async def test_config_change_during_generation_skips(self):
        async def generate(*args):
            self.cfg.catty_allowed_group_ids = {99}
            return "好家伙"

        self.generate.side_effect = generate
        self.assertFalse(await self.runtime.tick())
        self.send.assert_not_awaited()

    async def test_new_human_message_during_generation_still_allows_active_group(self):
        async def generate(*args):
            self.recent["group:10001"].append(self.row(100, age=0))
            return "好家伙"

        self.generate.side_effect = generate
        self.assertTrue(await self.runtime.tick())
        self.send.assert_awaited_once()

    async def test_new_bot_message_during_generation_skips(self):
        async def generate(*args):
            self.recent["group:10001"].append(self.row(100, age=0, bot=True, uid="999"))
            return "好家伙"

        self.generate.side_effect = generate
        self.assertFalse(await self.runtime.tick())
        self.assertEqual(self.factories[0].skips, ["stale_context"])
        self.send.assert_not_awaited()

    async def test_generation_then_main_reply_busy_skips_once(self):
        async def generate(*args):
            self.busy = True
            return "好家伙"

        self.generate.side_effect = generate
        self.assertFalse(await self.runtime.tick())
        self.busy = False
        self.assertFalse(await self.runtime.tick())
        self.generate.assert_awaited_once()
        self.assertEqual(self.factories[0].skips, ["changed_after_generation"])

    async def test_claim_failure_zero_send(self):
        scheduler = await self.runtime._get_scheduler(self.cfg)
        scheduler.claim_ok = False
        self.assertFalse(await self.runtime.tick())
        self.send.assert_not_awaited()
        self.remember.assert_not_called()

    async def test_busy_change_during_claim_finishes_failed(self):
        scheduler = await self.runtime._get_scheduler(self.cfg)
        scheduler.on_claim = lambda: setattr(self, "busy", True)
        self.assertFalse(await self.runtime.tick())
        self.send.assert_not_awaited()
        self.assertEqual(scheduler.finishes[-1][-1], "failed")

    async def test_slot_expiry_during_claim_finishes_failed_without_sending(self):
        self.cfg.catty_fdj_proactive_timeout_seconds = 3600
        scheduler = await self.runtime._get_scheduler(self.cfg)
        scheduler.on_claim = lambda: self.clock.advance(1801)
        self.assertFalse(await self.runtime.tick())
        self.send.assert_not_awaited()
        self.assertEqual(scheduler.finishes[-1][-1], "failed")

    async def test_midnight_during_claim_finishes_failed_without_sending(self):
        self.clock.wall = runtime.datetime(2026, 10, 27, 23, 59, 59, tzinfo=runtime._LOCAL_TIME).timestamp()
        scheduler = await self.runtime._get_scheduler(self.cfg)
        scheduler.on_claim = lambda: self.clock.advance(2)
        self.assertFalse(await self.runtime.tick())
        self.send.assert_not_awaited()
        self.assertEqual(scheduler.finishes[-1][-1], "failed")

    async def test_send_exception_unknown_never_retried(self):
        self.send.side_effect = OSError("transport")
        self.assertFalse(await self.runtime.tick())
        self.assertFalse(await self.runtime.tick())
        self.send.assert_awaited_once()
        self.remember.assert_not_called()
        self.assertEqual(self.factories[0].finishes[-1][-1], "unknown")

    async def test_send_timeout_unknown_and_never_retried(self):
        self.cfg.catty_fdj_proactive_timeout_seconds = .01

        async def send(*args):
            await asyncio.Event().wait()

        self.send.side_effect = send
        self.assertFalse(await self.runtime.tick())
        self.assertFalse(await self.runtime.tick())
        self.send.assert_awaited_once()
        self.remember.assert_not_called()
        self.assertEqual(self.factories[0].finishes[-1][-1], "unknown")

    async def test_missing_or_malformed_ack_unknown(self):
        for ack in (None, {}, {"message_id": False}, {"message_id": []}, {"message_id": 0}):
            self.runtime._scheduler = None
            self.send.return_value = ack
            self.assertFalse(await self.runtime.tick())
            self.assertEqual(self.factories[-1].finishes[-1][-1], "unknown")
        self.remember.assert_not_called()

    async def test_cancel_send_unknown_drained(self):
        entered = asyncio.Event()

        async def send(*args):
            entered.set()
            await asyncio.Event().wait()

        self.send.side_effect = send
        task = asyncio.create_task(self.runtime.tick())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.factories[0].finishes[-1][-1], "unknown")
        self.assertFalse(await self.runtime.tick())
        self.send.assert_awaited_once()
        self.remember.assert_not_called()

    async def test_cancel_claim_drains_and_finishes_failed(self):
        entered, release = threading.Event(), threading.Event()
        scheduler = await self.runtime._get_scheduler(self.cfg)

        def hold():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")

        scheduler.on_claim = hold
        task = asyncio.create_task(self.runtime.tick())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        self.assertEqual(scheduler.finishes[-1][-1], "failed")
        self.send.assert_not_awaited()

    async def test_cancel_generation_skips_and_resets_persona_context(self):
        entered = asyncio.Event()

        async def generate(*args):
            entered.set()
            await asyncio.Event().wait()

        self.generate.side_effect = generate
        task = asyncio.create_task(self.runtime.tick())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.factories[0].skips, ["generation_cancelled"])
        self.assertFalse(await self.runtime.tick())
        self.send.assert_not_awaited()

    async def test_cancel_finish_drains_successful_ack_commit(self):
        entered, release = threading.Event(), threading.Event()
        scheduler = await self.runtime._get_scheduler(self.cfg)
        original = scheduler.finish

        def finish(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")
            return original(*args)

        scheduler.finish = finish
        task = asyncio.create_task(self.runtime.tick())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        self.assertEqual(scheduler.finishes[-1][-1], "sent")
        self.send.assert_awaited_once()
        self.remember.assert_called_once()

    async def test_group_cache_expiry_failure_is_fail_closed(self):
        self.assertEqual(await self.runtime._group_ids(self.bot), ("10001",))
        self.clock.advance(299)
        self.assertEqual(await self.runtime._group_ids(self.bot), ("10001",))
        self.bot.get_group_list.assert_awaited_once()
        self.clock.advance(2)
        self.bot.get_group_list.side_effect = OSError("offline")
        self.assertEqual(await self.runtime._group_ids(self.bot), ())
        self.assertEqual(await self.runtime._group_ids(self.bot), ())
        self.assertEqual(self.bot.get_group_list.await_count, 2)

    async def test_only_one_attempt_per_tick_and_fair_random_order(self):
        self.bot.get_group_list.return_value = [{"group_id": i} for i in range(10001, 10006)]
        for i in range(10001, 10006):
            self.recent[f"group:{i}"] = [self.row(j) for j in range(5)]
        for count in range(1, 6):
            self.assertTrue(await self.runtime.tick())
            self.assertEqual(self.send.await_count, count)
        groups = [call.args[1] for call in self.send.await_args_list]
        self.assertEqual(len(set(groups)), 5)
        self.assertNotEqual(groups, sorted(groups))

    async def test_dir_or_policy_rebuilds_scheduler_not_other_config(self):
        first = await self.runtime._get_scheduler(self.cfg)
        self.cfg.catty_fdj_proactive_timeout_seconds = 20
        self.assertIs(await self.runtime._get_scheduler(self.cfg), first)
        self.cfg.catty_fdj_proactive_dir = "data/other"
        second = await self.runtime._get_scheduler(self.cfg)
        self.assertIsNot(first, second)
        self.cfg.catty_fdj_proactive_daily_min = 6
        self.assertIsNot(second, await self.runtime._get_scheduler(self.cfg))

    async def test_real_scheduler_runtime_disk_roundtrip_survives_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            self.cfg.catty_fdj_proactive_dir = directory
            self.cfg.catty_fdj_proactive_daily_min = 5
            self.cfg.catty_fdj_proactive_daily_max = 5
            self.clock.wall = runtime.datetime(2026, 10, 27, 7, tzinfo=runtime._LOCAL_TIME).timestamp()
            self.runtime.scheduler_factory = runtime.BubbleScheduler
            scheduler = await self.runtime._get_scheduler(self.cfg)
            status = await runtime.store_call(scheduler.status, "10001", self.clock.wall)
            self.assertTrue(status["ok"])
            slot = next(item for item in status["slots"] if not item["night"])
            self.clock.advance(slot["at"] + 1 - self.clock.wall)
            self.recent["group:10001"] = [self.row(i) for i in range(5)]
            self.assertTrue(await self.runtime.tick())
            status = await runtime.store_call(scheduler.status, "10001", self.clock.wall)
            self.assertEqual(status["sent"], 1)
            state_file = next(Path(directory).glob("*.json"))
            disk = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(sum(item["state"] == "sent" for item in disk["plan"]["slots"]), 1)
            self.assertFalse(await self.runtime.tick())
            rebuilt = runtime.FdjProactiveRuntime(
                self.runtime.host, wall=lambda: self.clock.wall, monotonic=lambda: self.clock.mono,
            )
            self.assertFalse(await rebuilt.tick())
            reloaded = await runtime.store_call(rebuilt._scheduler.status, "10001", self.clock.wall)
            self.assertEqual(reloaded["sent"], 1)
            self.send.assert_awaited_once()
            self.remember.assert_called_once()


class HostWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_cleaning_single_short_text_without_fake_reply(self):
        clean = _clean_namespace()["_clean_fdj_proactive_reply"]
        self.assertEqual(clean("（小声）好家伙\n" + markers.REPLY_SPLIT_MARKER + "真行"), "好家伙 真行")
        self.assertEqual(clean(markers.NO_REPLY_MARKER), "")
        self.assertEqual(clean("\n"), "")
        self.assertNotIn("CATTY_", clean("好家伙<<<CATTY_FD_MOOD:happy>>>"))

    async def test_text_segment_does_not_parse_cq(self):
        ns = _extract({"_send_fdj_proactive_text"}, {
            "Message": Message, "MessageSegment": MessageSegment, "_coerce_group_id": int,
        })
        bot = types.SimpleNamespace(send_group_msg=AsyncMock(return_value={"message_id": 1}))
        await ns["_send_fdj_proactive_text"](bot, "10001", "[CQ:at,qq=all]")
        message = bot.send_group_msg.call_args.kwargs["message"]
        self.assertEqual(len(message), 1)
        self.assertEqual(message[0].type, "text")
        self.assertEqual(message[0].data["text"], "[CQ:at,qq=all]")

    def test_ack_ledger_does_not_open_continuation_or_fabricate_turn(self):
        remember = Mock()
        ns = _extract({"_remember_fdj_proactive_text"}, {"_remember_bot_conversation_message": remember})
        ns["_remember_fdj_proactive_text"](types.SimpleNamespace(self_id=999), "10001", "好家伙", "88")
        self.assertFalse(remember.call_args.kwargs["update_continuation"])
        self.assertNotIn("target_user_id", remember.call_args.kwargs)

    def test_busy_count_preserves_concurrency_and_releases(self):
        counts = defaultdict(int, {"group:10001": 2})
        ns = _extract({"_release_active_group_reply", "_fdj_group_reply_busy"}, {
            "_active_group_replies": counts, "_persona_transition_locks": {},
            "_persona_config_transition_lock": asyncio.Lock(),
        })
        self.assertTrue(ns["_fdj_group_reply_busy"]("group:10001"))
        ns["_release_active_group_reply"]("group:10001")
        self.assertEqual(counts["group:10001"], 1)
        ns["_release_active_group_reply"]("group:10001")
        self.assertFalse(ns["_fdj_group_reply_busy"]("group:10001"))

    def test_startup_unconditionally_registers_independent_loop_and_busy_cleanup(self):
        start = next(n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_memory_summary_loop")
        top_level_calls = [ast.unparse(n) for n in start.body if isinstance(n, ast.Expr)]
        self.assertIn("asyncio.create_task(_fdj_proactive_bubble_loop())", top_level_calls)
        self.assertNotIn("asyncio.create_task(_proactive_bubble_loop())", top_level_calls)
        handler = next(n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_chat")
        source = ast.unparse(handler)
        self.assertIn("_active_group_replies[queue_key] += 1", source)
        self.assertIn("_lock_stack.callback(_release_active_group_reply, queue_key)", source)
        self.assertIn("_conversation_locks[history_key]", source)
        self.assertIn("_group_concurrency_sema_for(event)", source)


class LegacyIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_legacy_has_zero_candidates_and_sends(self):
        cfg = types.SimpleNamespace(catty_proactive_enabled=False)
        candidate, send = AsyncMock(return_value=["10001"]), AsyncMock()
        host = legacy.LegacyBubbleHost(lambda: cfg, lambda: True, lambda: [object()], candidate,
            lambda: None, lambda scope: None, send, Mock())
        await legacy.legacy_proactive_tick(host, sleep=AsyncMock())
        candidate.assert_not_awaited()
        send.assert_not_awaited()

    async def test_enabled_legacy_retains_memory_policy_persona_filter_and_error_contract(self):
        cfg = types.SimpleNamespace(catty_proactive_enabled=True, catty_proactive_max_daily_per_group=3,
            catty_proactive_min_interval_minutes=20, catty_proactive_active_window_minutes=30,
            catty_proactive_active_min_messages=4)
        bot = object()
        memory = types.SimpleNamespace(due_proactive_group_ids=Mock(return_value=["10001", "10002"]))
        send, on_error, sleep = AsyncMock(side_effect=OSError("offline")), Mock(), AsyncMock()
        host = legacy.LegacyBubbleHost(lambda: cfg, lambda: True, lambda: [bot], AsyncMock(return_value=["10001", "10002"]),
            lambda: memory, lambda scope: types.SimpleNamespace(feature_disabled=lambda feature: scope == "group:10002"), send, on_error)
        await legacy.legacy_proactive_tick(host, sleep=sleep)
        memory.due_proactive_group_ids.assert_called_once_with(["10001", "10002"], max_daily=3,
            min_interval_minutes=20, active_window_minutes=30, active_min_messages=4)
        send.assert_awaited_once_with(bot, "10001")
        on_error.assert_called_once()
        sleep.assert_awaited_once_with(2)


if __name__ == "__main__":
    unittest.main()