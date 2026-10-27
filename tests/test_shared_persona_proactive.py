"""Offline contracts for shared-persona proactive bubbles."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import random
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_shared_persona_proactive_tests"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)
runtime = importlib.import_module(f"{_PACKAGE_NAME}.fdj_proactive_runtime")
prompt = importlib.import_module(f"{_PACKAGE_NAME}.fdj_proactive_prompt")
personas = importlib.import_module(f"{_PACKAGE_NAME}.personas")
catty_core = importlib.import_module(f"{_PACKAGE_NAME}.catty_core_persona")
catty_prompts = importlib.import_module(f"{_PACKAGE_NAME}.persona_prompts")


def make_persona(name="custom", *, source="legacy", enabled=True, revision="revision-1"):
    return types.SimpleNamespace(
        name=name,
        char_name=name,
        core_persona=f"CORE_{name}",
        chat_rhythm=f"RHYTHM_{name} {{split_marker}}",
        conversation_prompt_for=lambda scope: f"CURRENT_{name}_{scope}",
        definition_revision=revision,
        proactive_enabled=enabled,
        proactive_config_source=source,
    )


class PromptTests(unittest.TestCase):
    def build(self, persona):
        return prompt.build_fdj_proactive_messages(
            persona, scope="group:10001", mode="join",
            recent_messages=[{"speaker": "群友", "text": "真正的群话题", "is_bot": False}],
            now=datetime(2026, 10, 27, 10, tzinfo=timezone.utc),
        )

    def test_builtin_catty_none_uses_catty_core_and_rhythm(self):
        persona = personas.CATTY_PERSONA
        self.assertIsNone(persona.core_persona)
        self.assertIsNone(persona.chat_rhythm)
        messages = self.build(persona)
        first = messages[0]["content"]
        self.assertIn(catty_core.CATTY_CORE_PERSONA, first)
        self.assertIn(catty_prompts.build_qq_chat_rhythm_prompt(prompt.REPLY_SPLIT_MARKER, persona=persona), first)
        self.assertNotIn(personas.FADIANJI_PERSONA.core_persona, first)
        self.assertNotIn(personas.FADIANJI_PERSONA.chat_rhythm, first)
        self.assertEqual(json.loads(messages[-1]["content"])["recent_messages"][0]["text"], "真正的群话题")

    def test_fdj_uses_only_its_actual_identity_and_rhythm(self):
        persona = personas.FADIANJI_PERSONA
        messages = self.build(persona)
        expected = (
            persona.core_persona,
            persona.conversation_prompt_for("group:10001"),
            persona.chat_rhythm.replace("{split_marker}", prompt.REPLY_SPLIT_MARKER),
        )
        self.assertEqual(messages[0]["content"], "\n\n".join(x for x in expected if x and x.strip()))
        self.assertNotIn(catty_core.CATTY_CORE_PERSONA, messages[0]["content"])

    def test_custom_uses_its_own_fields_and_only_requested_scope(self):
        persona = make_persona("custom")
        messages = self.build(persona)
        text = "\n".join(item["content"] for item in messages if item["role"] == "system")
        self.assertIn("CORE_custom", text)
        self.assertIn("RHYTHM_custom", text)
        self.assertIn("CURRENT_custom_group:10001", text)
        self.assertNotIn("CURRENT_custom_group:20002", text)
        self.assertNotIn("机机", text)
        self.assertNotIn(catty_core.CATTY_CORE_PERSONA, text)
        self.assertNotIn(personas.FADIANJI_PERSONA.core_persona, text)
        self.assertIn(prompt.REPLY_SPLIT_MARKER, text)

    def test_custom_none_does_not_inherit_either_builtin(self):
        persona = make_persona("custom")
        persona.core_persona = persona.chat_rhythm = None
        messages = self.build(persona)
        self.assertEqual(messages[0]["content"], "CURRENT_custom_group:10001")
        self.assertNotIn("机机", messages[0]["content"])
        self.assertNotIn("笨猫", messages[0]["content"])

    def test_custom_revision_named_catty_does_not_inherit_builtin(self):
        persona = make_persona("catty", revision="custom-revision")
        persona.core_persona = persona.chat_rhythm = None
        self.assertEqual(self.build(persona)[0]["content"], "CURRENT_catty_group:10001")


class FakeClock:
    def __init__(self):
        self.wall = datetime(2026, 10, 27, 10, tzinfo=runtime._LOCAL_TIME).timestamp()
        self.mono = 5000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class FakeScheduler:
    def __init__(self, root, policy):
        self.root, self.policy = root, policy
        self.window = None
        self.done = False
        self.on_due = None
        self.on_claim = None
        self.due_calls = 0
        self.skips = []
        self.finishes = []

    def due(self, group_id, now, *, human_times):
        self.due_calls += 1
        if self.on_due is not None:
            self.on_due()
        if self.done or not human_times:
            return None
        if self.window is None:
            self.window = (now - 1, now + 1800)
        day = datetime.fromtimestamp(now, runtime._LOCAL_TIME).date().isoformat()
        return types.SimpleNamespace(group_id=group_id, day=day, slot_id="slot1", mode="join", night=False)

    def status(self, group_id, now):
        return {"ok": True, "slots": [{"slot_id": "slot1", "at": self.window[0], "expires_at": self.window[1]}]}

    def claim(self, candidate, now):
        if self.on_claim is not None:
            self.on_claim()
        if self.done:
            return None
        self.done = True
        return "attempt1"

    def finish(self, group_id, attempt, outcome, now):
        self.finishes.append(outcome)
        return True

    def skip(self, candidate, now, reason="unsuitable"):
        self.skips.append(reason)
        self.done = True
        return True


class SharedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cfg = types.SimpleNamespace(
            catty_proactive_enabled=True, catty_fdj_proactive_enabled=True,
            catty_enable_group=True, catty_allowed_group_ids=set(),
        )
        self.persona = make_persona()
        self.busy = False
        self.key = True
        self.bot = types.SimpleNamespace(
            self_id="999", get_group_list=AsyncMock(return_value=[{"group_id": 10001}]),
        )
        self.rows = [self.row(index) for index in range(5)]
        self.generate = AsyncMock(return_value="接着聊吧")
        self.send = AsyncMock(return_value={"message_id": 88})
        self.remember = Mock()
        self.clean = Mock(side_effect=lambda text, persona: text)
        self.schedulers = []

        def factory(root, policy):
            scheduler = FakeScheduler(root, policy)
            self.schedulers.append(scheduler)
            return scheduler

        self.host = runtime.BubbleHost(
            config=lambda: self.cfg, persona=lambda scope: self.persona,
            bots=lambda: [self.bot], recent=lambda scope: tuple(self.rows),
            busy=lambda scope: self.busy, has_api_key=lambda: self.key,
            generate=self.generate, send_text=self.send, remember=self.remember,
            clean_text=self.clean,
        )
        self.engine = runtime.FdjProactiveRuntime(
            self.host, wall=lambda: self.clock.wall, monotonic=lambda: self.clock.mono,
            scheduler_factory=factory, rng=random.Random(4),
        )

    def row(self, seq, *, bot=False):
        return types.SimpleNamespace(
            seq=seq, created_at=self.clock.mono - 10, is_bot=bot,
            user_id="999" if bot else "123", display_name="群友", text=f"话题{seq}",
        )

    async def test_per_persona_switches_and_sources_are_independent(self):
        cases = (
            ("fdj", True, False, True, True),
            ("fdj", True, True, False, False),
            ("legacy", True, True, False, True),
            ("legacy", True, False, True, False),
            ("legacy", False, True, True, False),
            ("fdj", False, True, True, False),
            ("invalid", True, True, True, False),
        )
        for source, enabled, legacy, fdj, expected in cases:
            with self.subTest(source=source, enabled=enabled, legacy=legacy, fdj=fdj):
                self.setUp()
                self.persona.proactive_config_source = source
                self.persona.proactive_enabled = enabled
                self.cfg.catty_proactive_enabled = legacy
                self.cfg.catty_fdj_proactive_enabled = fdj
                self.assertEqual(await self.engine.tick(), expected)
                self.assertEqual(self.generate.await_count, int(expected))
                self.assertEqual(self.send.await_count, int(expected))
                if expected:
                    self.clean.assert_called_once_with("接着聊吧", self.persona)
                    self.assertEqual(self.schedulers[0].finishes, ["sent"])

    async def test_no_global_switch_or_key_does_not_read_groups(self):
        self.cfg.catty_proactive_enabled = False
        self.cfg.catty_fdj_proactive_enabled = False
        self.assertFalse(await self.engine.tick())
        self.bot.get_group_list.assert_not_awaited()
        self.cfg.catty_fdj_proactive_enabled = True
        self.key = False
        self.assertFalse(await self.engine.tick())
        self.bot.get_group_list.assert_not_awaited()

    async def test_unrelated_global_switch_change_does_not_abort(self):
        async def generate(*args):
            self.cfg.catty_fdj_proactive_enabled = False
            return "接着聊吧"

        self.generate.side_effect = generate
        self.assertTrue(await self.engine.tick())
        self.send.assert_awaited_once()

    async def test_switch_after_eligibility_before_generation_is_not_delivered(self):
        scheduler = await self.engine._get_scheduler(self.cfg)
        scheduler.on_due = lambda: setattr(self, "persona", make_persona("other"))
        self.assertFalse(await self.engine.tick())
        self.generate.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_context_after_eligibility_before_generation_is_rechecked(self):
        scheduler = await self.engine._get_scheduler(self.cfg)
        scheduler.on_due = lambda: self.rows.append(self.row(99, bot=True))
        self.assertFalse(await self.engine.tick())
        self.generate.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_changes_during_generation_never_deliver(self):
        def switch_persona():
            self.persona = make_persona("other")

        changes = (
            switch_persona,
            lambda: setattr(self.persona, "definition_revision", "revision-2"),
            lambda: setattr(self.persona, "proactive_enabled", False),
            lambda: setattr(self.persona, "proactive_config_source", "fdj"),
            lambda: setattr(self.cfg, "catty_proactive_enabled", False),
            lambda: setattr(self.cfg, "catty_allowed_group_ids", {99999}),
            lambda: setattr(self.cfg, "catty_enable_group", False),
            lambda: setattr(self, "busy", True),
            lambda: self.rows.append(self.row(99, bot=True)),
            lambda: self.rows.clear(),
            lambda: setattr(self.bot.get_group_list, "return_value", []),
        )
        for change in changes:
            with self.subTest(change=change):
                self.setUp()

                async def generate(*args):
                    change()
                    return "接着聊吧"

                self.generate.side_effect = generate
                self.assertFalse(await self.engine.tick())
                self.send.assert_not_awaited()
                self.remember.assert_not_called()

    async def test_switch_revision_or_config_during_claim_never_delivers(self):
        changes = (
            lambda: setattr(self, "persona", make_persona("other")),
            lambda: setattr(self.persona, "definition_revision", "revision-2"),
            lambda: setattr(self.persona, "proactive_enabled", False),
            lambda: setattr(self.cfg, "catty_proactive_enabled", False),
            lambda: setattr(self.cfg, "catty_allowed_group_ids", {99999}),
            lambda: self.rows.append(self.row(99, bot=True)),
            lambda: setattr(self.bot.get_group_list, "return_value", []),
        )
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                scheduler = await self.engine._get_scheduler(self.cfg)
                scheduler.on_claim = change
                self.assertFalse(await self.engine.tick())
                self.send.assert_not_awaited()
                self.assertEqual(scheduler.finishes, ["failed"])

    async def test_live_membership_is_rechecked_after_generation(self):
        self.bot.get_group_list.side_effect = [
            [{"group_id": 10001}], [{"group_id": 10001}], [],
        ]
        self.assertFalse(await self.engine.tick())
        self.generate.assert_awaited_once()
        self.send.assert_not_awaited()
        self.assertEqual(self.schedulers[0].skips, ["membership_changed"])

    async def test_cleaner_accepts_legacy_single_argument(self):
        calls = Mock()

        def cleaner(text):
            calls(text)
            return text

        self.engine.host = replace(self.host, clean_text=cleaner)
        self.assertTrue(await self.engine.tick())
        calls.assert_called_once_with("接着聊吧")

    async def test_cleaner_internal_type_error_is_not_retried_as_single_argument(self):
        cleaner = Mock(side_effect=TypeError("cleaner failed"))
        self.engine.host = replace(self.host, clean_text=cleaner)
        self.assertFalse(await self.engine.tick())
        cleaner.assert_called_once_with("接着聊吧", self.persona)
        self.send.assert_not_awaited()
        self.assertEqual(self.schedulers[0].skips, ["invalid_reply"])

    async def test_delivery_history_binds_the_generation_persona(self):
        original = self.persona
        persona_runtime = importlib.import_module(f"{_PACKAGE_NAME}.persona_runtime")
        recorded = []

        async def send(*args):
            self.persona = make_persona("other")
            return {"message_id": 88}

        self.send.side_effect = send
        self.remember.side_effect = lambda *args: recorded.append(
            persona_runtime.active_persona_for_scope("group:10001")
        )
        self.assertTrue(await self.engine.tick())
        self.assertEqual(recorded, [original])
        self.assertIsNone(persona_runtime.active_persona_for_scope("group:10001"))

    async def test_real_plan_is_shared_across_persona_switch_and_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            self.cfg.catty_fdj_proactive_dir = directory
            self.cfg.catty_fdj_proactive_daily_min = 5
            self.cfg.catty_fdj_proactive_daily_max = 5
            self.clock.wall = datetime(2026, 10, 27, 7, tzinfo=runtime._LOCAL_TIME).timestamp()
            self.engine.scheduler_factory = runtime.BubbleScheduler
            scheduler = await self.engine._get_scheduler(self.cfg)
            status = await runtime.store_call(scheduler.status, "10001", self.clock.wall)
            slot = next(item for item in status["slots"] if not item["night"])
            self.clock.advance(slot["at"] + 1 - self.clock.wall)
            self.rows = [self.row(index) for index in range(5)]
            self.persona = make_persona("fadianji", source="fdj", revision=None)
            self.assertTrue(await self.engine.tick())
            self.persona = make_persona("catty", source="legacy", revision=None)
            self.assertFalse(await self.engine.tick())
            restored = runtime.FdjProactiveRuntime(
                self.host, wall=lambda: self.clock.wall, monotonic=lambda: self.clock.mono,
            )
            self.assertFalse(await restored.tick())
            status = await runtime.store_call(restored._scheduler.status, "10001", self.clock.wall)
            self.assertEqual(status["sent"], 1)
            self.send.assert_awaited_once()
            self.remember.assert_called_once()


if __name__ == "__main__":
    unittest.main()
