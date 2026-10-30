from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender


_ROOT = Path(__file__).resolve().parents[1]
_PKG = "catty_mention_followup_tests"
_package = types.ModuleType(_PKG)
_package.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PKG, _package)
module = importlib.import_module(f"{_PKG}.mention_followup")


def event(message_id, text="", *, user=10, group=20, bot=30, mention=False):
    message = Message(text)
    if mention:
        message = Message(MessageSegment.at(bot)) + message
    return GroupMessageEvent(
        time=0, self_id=bot, post_type="message", sub_type="normal", user_id=user,
        message_type="group", message_id=message_id, message=message,
        original_message=message, raw_message=str(message), font=0,
        sender=Sender(user_id=user, nickname=f"user-{user}"), group_id=group,
    )


class MentionFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_second_window_collects_same_sender_once(self):
        now = [100.0]
        waited = []

        async def sleep(seconds):
            waited.append(seconds)
            now[0] += seconds

        collector = module.MentionFollowups(clock=lambda: now[0], sleep=sleep)
        wake = event(1, mention=True)
        collector.observe(wake, "fadianji")
        pending = collector.begin(wake, "fadianji")
        now[0] = 102.0
        following = event(2, "和我相亲")
        collector.observe(following, "fadianji")
        collector.observe(following, "fadianji")
        self.assertTrue(collector.is_consumed(following, "fadianji"))
        collected = await collector.collect(pending)
        self.assertEqual(waited, [8.0])
        self.assertEqual([item.message_id for item in collected], [2])
        merged = module.merge_mention_events(wake, collected)
        self.assertEqual((merged.user_id, merged.group_id, merged.message_id), (10, 20, 1))
        self.assertIn("和我相亲", merged.get_plaintext())
        self.assertNotIn("和我相亲", wake.get_plaintext())
        self.assertFalse(collector.pending)

    async def test_empty_window_waits_ten_seconds(self):
        waited = []

        async def sleep(seconds):
            waited.append(seconds)

        collector = module.MentionFollowups(clock=lambda: 100.0, sleep=sleep)
        pending = collector.begin(event(1, mention=True), "fadianji")
        self.assertEqual(await collector.collect(pending), [])
        self.assertEqual(waited, [10.0])

    async def test_other_people_groups_bots_personas_and_late_messages_are_not_consumed(self):
        now = [100.0]

        async def sleep(seconds):
            now[0] += seconds

        collector = module.MentionFollowups(clock=lambda: now[0], sleep=sleep)
        pending = collector.begin(event(1, mention=True), "fadianji")
        cases = [(event(2, "other", user=11), "fadianji"),
                 (event(3, "other", group=21), "fadianji"),
                 (event(4, "other", bot=31), "fadianji"),
                 (event(5, "other"), "catty")]
        for other, persona in cases:
            collector.observe(other, persona)
            self.assertFalse(collector.is_consumed(other, persona))
        now[0] = 111.0
        collector.observe(event(6, "late"), "fadianji")
        self.assertEqual(await collector.collect(pending), [])

    async def test_images_are_merged_but_other_user_mentions_replies_and_commands_are_not(self):
        collector = module.MentionFollowups(clock=lambda: 100.0, sleep=lambda _: asyncio.sleep(0))
        wake = event(1, mention=True)
        pending = collector.begin(wake, "fadianji")
        image = event(2)
        image.message.append(MessageSegment.image("https://images.example/test.png"))
        collector.observe(image, "fadianji")
        other = event(3, "跟别人说")
        other.message.append(MessageSegment.at(99))
        reply = event(4, "回复别人")
        reply.message.append(MessageSegment.reply(123))
        for item in (other, reply, event(5, "/独立命令")):
            collector.observe(item, "fadianji")
            self.assertFalse(collector.is_consumed(item, "fadianji"))
        collected = await collector.collect(pending)
        self.assertEqual([item.message_id for item in collected], [2])
        self.assertEqual(module.merge_mention_events(wake, collected).message[-1].type, "image")

    async def test_arrived_before_registration_is_still_collected(self):
        now = [100.0]
        collector = module.MentionFollowups(clock=lambda: now[0], sleep=lambda _: asyncio.sleep(0))
        wake = event(1, mention=True)
        collector.observe(wake, "fadianji")
        now[0] = 101.0
        collector.observe(event(2, "已经发来的正文"), "fadianji")
        pending = collector.begin(wake, "fadianji")
        self.assertEqual(pending.deadline, 110.0)
        self.assertEqual([item.message_id for item in await collector.collect(pending)], [2])

    async def test_cancellation_releases_pending_window(self):
        started = asyncio.Event()

        async def sleep(seconds):
            started.set()
            await asyncio.Event().wait()

        collector = module.MentionFollowups(clock=lambda: 100.0, sleep=sleep)
        pending = collector.begin(event(1, mention=True), "fadianji")
        task = asyncio.create_task(collector.collect(pending))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(collector.pending)