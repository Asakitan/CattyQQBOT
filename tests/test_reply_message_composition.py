from pathlib import Path
import asyncio
import importlib.util
import re
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_plugin_reply_composition_test"


def _load_plugin_module():
    nonebot.init()
    package = types.ModuleType(_PACKAGE_NAME)
    package.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
    sys.modules[_PACKAGE_NAME] = package
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME,
        _ROOT / "src" / "catty_qq_ai" / "__init__.py",
        submodule_search_locations=[str(_ROOT / "src" / "catty_qq_ai")],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


_plugin = _load_plugin_module()


def _group_event(message_id: int = 123, text: str = "猫猫看这个", *, mention_self: bool = False) -> GroupMessageEvent:
    if mention_self:
        message = Message([MessageSegment.at(999), MessageSegment.text(f" {text}")])
        raw_message = f"[CQ:at,qq=999] {text}"
    else:
        message = Message(text)
        raw_message = text
    return GroupMessageEvent(
        time=0,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=20002,
        message_type="group",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=raw_message,
        font=0,
        sender=Sender(user_id=20002, nickname="群友", card="群友"),
        group_id=10001,
    )


def _wake_context_rows(prompt: str) -> list[str]:
    return [line for line in prompt.splitlines() if re.match(r"^#\d{6} ", line)]


class ReplyMessageCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_quote_enabled = _plugin.config.catty_reply_quote_enabled
        self._old_private_quote_enabled = _plugin.config.catty_reply_quote_private_enabled
        self._old_mix_enabled = _plugin.config.catty_reply_mix_emoji_with_text
        self._old_diversity_enabled = _plugin.config.catty_emoji_diversity_enabled
        self._old_diversity_recent_window = _plugin.config.catty_emoji_diversity_recent_window
        self._old_diversity_candidate_pool = _plugin.config.catty_emoji_diversity_candidate_pool
        self._old_auto_fallback_enabled = _plugin.config.catty_emoji_auto_fallback_enabled
        self._old_emoji_reply_enabled = _plugin.config.catty_emoji_reply_enabled
        self._old_emoji_reply_probability = _plugin.config.catty_emoji_reply_probability
        self._old_local_critic_enabled = _plugin.config.catty_local_critic_enabled
        self._old_force_direct_reply = _plugin.config.catty_local_critic_force_direct_reply
        self._old_keyword_replies = list(_plugin.config.catty_keyword_replies)
        _plugin.config.catty_reply_quote_enabled = True
        _plugin.config.catty_reply_quote_private_enabled = False
        _plugin.config.catty_reply_mix_emoji_with_text = True
        _plugin._recent_conversation_messages.clear()
        _plugin._bot_reply_continuations.clear()
        _plugin._continuation_updated_turn_ids.clear()

    def tearDown(self) -> None:
        _plugin.config.catty_reply_quote_enabled = self._old_quote_enabled
        _plugin.config.catty_reply_quote_private_enabled = self._old_private_quote_enabled
        _plugin.config.catty_reply_mix_emoji_with_text = self._old_mix_enabled
        _plugin.config.catty_emoji_diversity_enabled = self._old_diversity_enabled
        _plugin.config.catty_emoji_diversity_recent_window = self._old_diversity_recent_window
        _plugin.config.catty_emoji_diversity_candidate_pool = self._old_diversity_candidate_pool
        _plugin.config.catty_emoji_auto_fallback_enabled = self._old_auto_fallback_enabled
        _plugin.config.catty_emoji_reply_enabled = self._old_emoji_reply_enabled
        _plugin.config.catty_emoji_reply_probability = self._old_emoji_reply_probability
        _plugin.config.catty_local_critic_enabled = self._old_local_critic_enabled
        _plugin.config.catty_local_critic_force_direct_reply = self._old_force_direct_reply
        _plugin.config.catty_keyword_replies = self._old_keyword_replies
        _plugin._recent_emoji_paths.clear()
        _plugin._recent_conversation_messages.clear()
        _plugin._bot_reply_continuations.clear()
        _plugin._continuation_updated_turn_ids.clear()

    def test_reply_message_can_quote_text_and_image_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "cat.png"
            image_path.write_bytes(b"fake")
            entry = _plugin.EmojiEntry(
                path=image_path,
                meaning="开心",
                tags=[],
                source="test",
                priority=1,
            )

            message = _plugin._compose_reply_message(
                _group_event(456),
                text="收到啦主人",
                emoji_entry=entry,
                quote=True,
            )

        self.assertEqual(message[0].type, "reply")
        self.assertEqual(message[0].data["id"], "456")
        self.assertEqual(message[1].type, "text")
        self.assertEqual(message[2].type, "image")

    def test_complete_event_reply_snapshot_skips_get_msg(self) -> None:
        class FakeBot:
            self_id = "999"
            calls = 0

            async def get_msg(self, **_kwargs):
                self.calls += 1
                raise AssertionError("complete event.reply should not call get_msg")

        event = types.SimpleNamespace(
            message=Message([MessageSegment.reply(88), MessageSegment.text(" 再说一次")]),
            raw_message="[CQ:reply,id=88] 再说一次",
            reply={
                "message_id": 88,
                "sender": {"user_id": 999, "card": "机机bot"},
                "message": [
                    {"type": "text", "data": {"text": "之前的回复"}},
                    {"type": "image", "data": {"url": "https://img/reply.jpg", "file": "reply-file"}},
                ],
            },
        )
        bot = FakeBot()

        sources = asyncio.run(_plugin._resolve_reply_sources(bot, event))

        self.assertEqual(bot.calls, 0)
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0].is_current_bot)
        self.assertEqual(sources[0].text, "之前的回复")
        self.assertEqual(sources[0].images[0][0], "https://img/reply.jpg")

    def test_incomplete_event_reply_fetches_each_message_once(self) -> None:
        class FakeBot:
            self_id = "999"

            def __init__(self) -> None:
                self.calls = []

            async def get_msg(self, *, message_id):
                self.calls.append(message_id)
                return {
                    "message_id": message_id,
                    "sender": {"user_id": 123, "nickname": "旧群友名"},
                    "message": [
                        {"type": "text", "data": {"text": "引用原文"}},
                        {"type": "mface", "data": {"url": "https://img/mface.gif", "emoji_id": "e"}},
                    ],
                }

        event = types.SimpleNamespace(
            message=Message([MessageSegment.reply(89), MessageSegment.text(" 这是什么图")]),
            raw_message="[CQ:reply,id=89] 这是什么图",
            reply={"message_id": 89, "sender": {"user_id": 123}},
        )
        bot = FakeBot()

        sources = asyncio.run(_plugin._resolve_reply_sources(bot, event))
        context = _plugin._build_reply_source_context(
            sources,
            group_id="10001",
            image_description="一张测试表情包",
        )

        self.assertEqual(bot.calls, [89])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].sender_name, "旧群友名")
        self.assertEqual(sources[0].images[0][0], "https://img/mface.gif")
        self.assertIn("旧群友名", context)
        self.assertIn("引用图片识别结果: 一张测试表情包", context)
        self.assertNotIn("https://img/mface.gif", context)

    def test_string_event_reply_fetches_once_for_complete_image_segments(self) -> None:
        class FakeBot:
            self_id = "999"

            def __init__(self) -> None:
                self.calls = []

            async def get_msg(self, *, message_id):
                self.calls.append(message_id)
                return {
                    "message_id": message_id,
                    "sender": {"user_id": 123, "nickname": "桃桃"},
                    "message": [
                        {"type": "text", "data": {"text": "引用文字"}},
                        {"type": "image", "data": {"url": "https://img/full.jpg", "file": "full"}},
                    ],
                }

        event = types.SimpleNamespace(
            message=Message([MessageSegment.reply(90), MessageSegment.text(" 看图")]),
            raw_message="[CQ:reply,id=90] 看图",
            reply={
                "message_id": 90,
                "sender": {"user_id": 123, "nickname": "桃桃"},
                "message": "引用文字[CQ:image,file=preview.jpg]",
            },
        )
        bot = FakeBot()

        sources = asyncio.run(_plugin._resolve_reply_sources(bot, event))

        self.assertEqual(bot.calls, [90])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].text, "引用文字")
        self.assertEqual(sources[0].images[0][0], "https://img/full.jpg")

    def test_complete_raw_cq_snapshot_with_url_skips_get_msg(self) -> None:
        class FakeBot:
            self_id = "999"
            calls = 0

            async def get_msg(self, **_kwargs):
                self.calls += 1
                raise AssertionError("URL-bearing raw CQ snapshot should be complete")

        event = types.SimpleNamespace(
            message=Message([MessageSegment.reply(93), MessageSegment.text(" 看图")]),
            raw_message="[CQ:reply,id=93] 看图",
            reply={
                "message_id": 93,
                "sender": {"user_id": 123, "nickname": "桃桃"},
                "message": "引用文字[CQ:image,file=preview.jpg,url=https://img/raw.jpg]",
            },
        )
        bot = FakeBot()

        sources = asyncio.run(_plugin._resolve_reply_sources(bot, event))

        self.assertEqual(bot.calls, 0)
        self.assertEqual(sources[0].text, "引用文字")
        self.assertEqual(sources[0].images[0][0], "https://img/raw.jpg")

    def test_string_preview_with_structured_original_skips_get_msg(self) -> None:
        class FakeBot:
            self_id = "999"
            calls = 0

            async def get_msg(self, **_kwargs):
                self.calls += 1
                raise AssertionError("structured original_message should be a complete snapshot")

        event = types.SimpleNamespace(
            message=Message([MessageSegment.reply(91), MessageSegment.text(" 看图")]),
            raw_message="[CQ:reply,id=91] 看图",
            reply={
                "message_id": 91,
                "sender": {"user_id": 123, "nickname": "桃桃"},
                "message": "引用文字[CQ:image,file=preview.jpg]",
                "original_message": [
                    {"type": "text", "data": {"text": "引用文字"}},
                    {"type": "mface", "data": {"url": "https://img/original.gif", "emoji_id": "e"}},
                ],
            },
        )
        bot = FakeBot()

        sources = asyncio.run(_plugin._resolve_reply_sources(bot, event))

        self.assertEqual(bot.calls, 0)
        self.assertEqual(sources[0].text, "引用文字")
        self.assertEqual(sources[0].images[0][0], "https://img/original.gif")

    def test_empty_message_with_structured_original_skips_get_msg(self) -> None:
        class FakeBot:
            self_id = "999"
            calls = 0

            async def get_msg(self, **_kwargs):
                self.calls += 1
                raise AssertionError("non-empty original_message should be a complete snapshot")

        event = types.SimpleNamespace(
            message=Message([MessageSegment.reply(92), MessageSegment.text(" 原文")]),
            raw_message="[CQ:reply,id=92] 原文",
            reply={
                "message_id": 92,
                "sender": {"user_id": 123, "nickname": "桃桃"},
                "message": [],
                "original_message": [
                    {"type": "text", "data": {"text": "结构化原文"}},
                ],
            },
        )
        bot = FakeBot()

        sources = asyncio.run(_plugin._resolve_reply_sources(bot, event))

        self.assertEqual(bot.calls, 0)
        self.assertEqual(sources[0].text, "结构化原文")

    def test_current_and_reply_images_are_deduplicated_before_vision(self) -> None:
        sources = [
            _plugin.ReplySource(
                images=[
                    ("https://img/same.jpg", "mface:e"),
                    ("https://img/reply.jpg", "image:reply"),
                    ("https://img/reply.jpg", "mface:duplicate"),
                ]
            )
        ]

        images = _plugin._unique_reply_image_pairs(
            sources,
            excluded_urls=["https://img/same.jpg"],
        )

        self.assertEqual(images, [("https://img/reply.jpg", "image:reply")])

    # 旧 [[CATTY_WEB_SEARCH:xxx]] 文本 marker 教学已废弃,web_search 现在走 OpenAI
    # function calling 标准协议(tools 字段, catty_web_search tool schema)。
    # _web_search_plugin_prompt / _extract_model_web_search_query 已删,
    # 相关测试 (test_web_search_prompt_lets_main_ai_request_plugin_search,
    # test_extract_model_web_search_query) 一并清掉。

    def test_invalid_hot_reload_config_does_not_replace_current_config(self) -> None:
        old_model = _plugin.config.catty_openai_model
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text('{"ai": ', encoding="utf-8")

            loaded = _plugin._load_runtime_config_from_path(config_path)

        self.assertIsNone(loaded)
        self.assertEqual(_plugin.config.catty_openai_model, old_model)

    def test_disabled_local_training_never_writes_assistant_samples(self) -> None:
        old_enabled = _plugin.config.catty_local_training_enabled
        old_collect = _plugin.config.catty_local_training_collect_assistant_samples
        old_path = _plugin.config.catty_local_training_assistant_samples_path
        try:
            with tempfile.TemporaryDirectory() as directory:
                sample_path = Path(directory) / "assistant_reply_samples.jsonl"
                _plugin.config.catty_local_training_enabled = False
                _plugin.config.catty_local_training_collect_assistant_samples = True
                _plugin.config.catty_local_training_assistant_samples_path = str(sample_path)

                _plugin._save_assistant_training_sample(
                    _group_event(),
                    types.SimpleNamespace(),
                    [],
                    "这条不该落盘",
                )

                self.assertFalse(sample_path.exists())
        finally:
            _plugin.config.catty_local_training_enabled = old_enabled
            _plugin.config.catty_local_training_collect_assistant_samples = old_collect
            _plugin.config.catty_local_training_assistant_samples_path = old_path

    def test_candidate_group_ids_prunes_removed_memory_groups(self) -> None:
        class FakeBot:
            async def get_group_list(self):
                return [{"group_id": 10001}]

        old_memory_store = _plugin.memory_store
        old_allowed_group_ids = _plugin.config.catty_allowed_group_ids
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = _plugin.MemoryStore(
                    _plugin.Config(
                        catty_memory_enabled=True,
                        catty_memory_path=str(Path(directory) / "memory.json"),
                        catty_memory_group_storage_dir=str(Path(directory) / "groups"),
                        catty_memory_user_storage_dir=str(Path(directory) / "users"),
                    )
                )
                store._data["groups"] = {
                    "10001": {"summary": "live group"},
                    "20002": {"summary": "stale group"},
                }
                store._save()
                _plugin.memory_store = store
                _plugin.config.catty_allowed_group_ids = set()

                group_ids = asyncio.run(_plugin._candidate_group_ids(FakeBot()))

                self.assertEqual(group_ids, ["10001"])
                self.assertEqual(store.group_ids(), ["10001"])
        finally:
            _plugin.memory_store = old_memory_store
            _plugin.config.catty_allowed_group_ids = old_allowed_group_ids

    def test_removed_group_send_error_is_detected(self) -> None:
        exc = RuntimeError("发送失败，你已被移出该群，请重新加群。")

        self.assertTrue(_plugin._is_removed_from_group_error(exc))

    def test_quote_can_be_disabled(self) -> None:
        _plugin.config.catty_reply_quote_enabled = False

        message = _plugin._compose_reply_message(_group_event(456), text="不引用", quote=True)

        self.assertEqual(message[0].type, "text")
        self.assertNotIn("[CQ:reply", str(message))

    def test_diverse_emoji_selection_avoids_recent_entries_when_possible(self) -> None:
        event = _group_event(456)
        entries = [
            _plugin.EmojiEntry(path=Path("a.jpg"), meaning="A", tags=[], source="test", priority=100),
            _plugin.EmojiEntry(path=Path("b.jpg"), meaning="B", tags=[], source="test", priority=90),
            _plugin.EmojiEntry(path=Path("c.jpg"), meaning="C", tags=[], source="test", priority=80),
        ]
        _plugin.config.catty_emoji_diversity_enabled = True
        _plugin.config.catty_emoji_diversity_recent_window = 2
        _plugin.config.catty_emoji_diversity_candidate_pool = 3
        _plugin._recent_emoji_paths.clear()
        _plugin._recent_emoji_paths[_plugin._conversation_queue_key(event)].extend(
            [_plugin._emoji_entry_key(entries[0]), _plugin._emoji_entry_key(entries[1])]
        )

        chosen = _plugin._select_diverse_emoji(event, entries)

        self.assertEqual(chosen.path.name, "c.jpg")

    def test_auto_emoji_fallback_is_disabled_by_default(self) -> None:
        incoming = types.SimpleNamespace(opportunistic=False)
        _plugin.config.catty_emoji_auto_fallback_enabled = False
        _plugin.config.catty_emoji_reply_enabled = True
        _plugin.config.catty_emoji_reply_probability = 1.0

        self.assertFalse(_plugin._should_auto_emoji_reply(incoming, "普通回复喵"))

    def test_auto_emoji_fallback_can_be_enabled_explicitly(self) -> None:
        incoming = types.SimpleNamespace(opportunistic=False)
        _plugin.config.catty_emoji_auto_fallback_enabled = True
        _plugin.config.catty_emoji_reply_enabled = True
        _plugin.config.catty_emoji_reply_probability = 1.0

        self.assertTrue(_plugin._should_auto_emoji_reply(incoming, "普通回复喵"))

    def test_keyword_reply_matches_mc_terms_without_mcp_false_positive(self) -> None:
        _plugin.config.catty_keyword_replies = [
            _plugin.KeywordReplyRule(
                keywords=["MC", "我的世界", "Minecraft"],
                reply="莎国方可梦2.0开服啦喵",
            )
        ]

        self.assertEqual(_plugin._keyword_reply_for_text("有人玩 MC 吗"), "莎国方可梦2.0开服啦喵")
        self.assertEqual(_plugin._keyword_reply_for_text("我的世界开不开心"), "莎国方可梦2.0开服啦喵")
        self.assertEqual(_plugin._keyword_reply_for_text("minecraft 服务器"), "莎国方可梦2.0开服啦喵")
        self.assertEqual(_plugin._keyword_reply_for_text("MCP 工具怎么配"), "")

    def test_bot_continuation_prompt_keeps_technical_followups_actionable(self) -> None:
        event = _group_event(456)

        prompt = _plugin._bot_continuation_judgement_prompt(event)

        self.assertIn("【BOT_CONT】", prompt)
        self.assertIn("r≈", prompt)
        self.assertIn("no=<<<CATTY_NO_REPLY>>>", prompt)
        self.assertIn("rules=catty_bot_continuation_skeleton", prompt)

    def test_mention_only_helpers_require_structured_at_and_freshness(self) -> None:
        textual_event = _group_event(11, text="@机机 看这个")
        real_at_event = _group_event(12, text="看这个", mention_self=True)
        incoming = types.SimpleNamespace(mentioned=True)
        mention_only_persona = types.SimpleNamespace(mention_only_trigger=True)

        with patch.object(_plugin, "_persona_for_event", return_value=mention_only_persona):
            self.assertFalse(_plugin._mention_only_real_at_allowed(textual_event))
            self.assertTrue(_plugin._mention_only_real_at_allowed(real_at_event))
            self.assertFalse(_plugin._fresh_mention_for_event(textual_event, incoming))
            self.assertTrue(_plugin._fresh_mention_for_event(real_at_event, incoming))

    def test_preliminary_reply_chunks_consume_followup_budget_only_once(self) -> None:
        scope = "group:10001"
        user_id = "20002"
        continuation_key = _plugin._bot_reply_continuation_key(scope, user_id)
        _plugin._bot_reply_continuations[continuation_key] = _plugin.BotReplyContinuationState(
            expires_at=time.monotonic() + 60,
            remaining_messages=2,
        )
        persona = types.SimpleNamespace(char_name="机机", followup_reply_budget=2)

        try:
            with patch.object(_plugin, "_persona_for_scope", return_value=persona):
                _plugin._remember_bot_conversation_message(
                    scope,
                    bot_id="999",
                    text="前段",
                    target_user_id=user_id,
                    update_continuation=False,
                )
                self.assertEqual(
                    _plugin._bot_reply_continuations[continuation_key].remaining_messages,
                    2,
                )
                _plugin._remember_bot_conversation_message(
                    scope,
                    bot_id="999",
                    text="尾段",
                    target_user_id=user_id,
                )
            self.assertEqual(
                _plugin._bot_reply_continuations[continuation_key].remaining_messages,
                1,
            )
        finally:
            _plugin._bot_reply_continuations.pop(continuation_key, None)

    def test_fresh_real_at_initializes_persona_followup_budget(self) -> None:
        scope = "group:10001"
        user_id = "20002"
        continuation_key = _plugin._bot_reply_continuation_key(scope, user_id)
        persona = types.SimpleNamespace(followup_reply_budget=2)

        with patch.object(_plugin, "_persona_for_scope", return_value=persona):
            _plugin._mark_bot_reply_continuation(
                scope,
                user_id,
                fresh_mention=True,
            )

        self.assertEqual(
            _plugin._bot_reply_continuations[continuation_key].remaining_messages,
            2,
        )

    def test_fdj_keyword_refreshes_budget_but_plain_followup_consumes_it(self) -> None:
        scope, user_id = "group:10001", "20002"
        key = _plugin._bot_reply_continuation_key(scope, user_id)
        persona = types.SimpleNamespace(
            name="fadianji", mention_only_trigger=False, followup_reply_budget=2,
            trigger_prefixes=("机机",), directed_keywords=("机机", "发电机"),
        )
        incoming = types.SimpleNamespace(mentioned=False, replied_to_self=False)
        _plugin._bot_reply_continuations[key] = _plugin.BotReplyContinuationState(
            expires_at=time.monotonic() + 60, remaining_messages=1,
        )
        try:
            with (
                patch.object(_plugin, "_persona_for_event", return_value=persona),
                patch.object(_plugin, "_persona_for_scope", return_value=persona),
            ):
                fresh = _plugin._fresh_mention_for_event(_group_event(20, text="机机 接着说"), incoming)
                self.assertTrue(fresh)
                _plugin._mark_bot_reply_continuation(scope, user_id, fresh_mention=fresh)
                self.assertEqual(_plugin._bot_reply_continuations[key].remaining_messages, 2)
                fresh = _plugin._fresh_mention_for_event(_group_event(21, text="接着说"), incoming)
                self.assertFalse(fresh)
                _plugin._mark_bot_reply_continuation(scope, user_id, fresh_mention=fresh)
                self.assertEqual(_plugin._bot_reply_continuations[key].remaining_messages, 1)
        finally:
            _plugin._bot_reply_continuations.pop(key, None)

    def test_duplicate_recent_text_still_updates_each_distinct_turn_once(self) -> None:
        scope = "group:10001"
        user_id = "20002"
        continuation_key = _plugin._bot_reply_continuation_key(scope, user_id)
        _plugin._bot_reply_continuations[continuation_key] = _plugin.BotReplyContinuationState(
            expires_at=time.monotonic() + 60,
            remaining_messages=2,
        )
        persona = types.SimpleNamespace(char_name="机机", followup_reply_budget=2)

        with patch.object(_plugin, "_persona_for_scope", return_value=persona):
            _plugin._remember_bot_conversation_message(
                scope,
                bot_id="999",
                text="重复回复",
                target_user_id=user_id,
                continuation_turn_id="turn-1",
            )
            self.assertEqual(
                _plugin._bot_reply_continuations[continuation_key].remaining_messages,
                1,
            )
            _plugin._remember_bot_conversation_message(
                scope,
                bot_id="999",
                text="重复回复",
                target_user_id=user_id,
                continuation_turn_id="turn-1",
            )
            self.assertEqual(
                _plugin._bot_reply_continuations[continuation_key].remaining_messages,
                1,
            )
            _plugin._remember_bot_conversation_message(
                scope,
                bot_id="999",
                text="重复回复",
                target_user_id=user_id,
                continuation_turn_id="turn-2",
            )

        self.assertNotIn(continuation_key, _plugin._bot_reply_continuations)
        self.assertEqual(len(_plugin._recent_conversation_messages[scope]), 1)

    def test_main_rule_rejects_textual_alias_for_mention_only_persona(self) -> None:
        event = _group_event(13, text="@机机 看这个")
        bot = types.SimpleNamespace(self_id="999")
        incoming = types.SimpleNamespace(text="看这个", mentioned=True)
        mention_only_persona = types.SimpleNamespace(mention_only_trigger=True)

        with (
            patch.object(_plugin, "_resolve_reply_sources", new=AsyncMock(return_value=[])),
            patch.object(_plugin, "extract_incoming_message", return_value=incoming),
            patch.object(_plugin, "_recent_bot_prompted_user", return_value=False),
            patch.object(_plugin, "_persona_for_event", return_value=mention_only_persona),
        ):
            allowed = asyncio.run(_plugin._rule(bot, event, {}))

        self.assertFalse(allowed)

    def test_affection_command_rejects_textual_alias_for_mention_only_persona(self) -> None:
        event = _group_event(14, text="@机机 签到")
        bot = types.SimpleNamespace(self_id="999")
        state: dict[str, object] = {}
        incoming = types.SimpleNamespace(mentioned=True, directly_requested=True)
        mention_only_persona = types.SimpleNamespace(
            mention_only_trigger=True,
            feature_disabled=lambda _feature: False,
        )

        with (
            patch.object(_plugin, "_keyword_reply_event_allowed", return_value=True),
            patch.object(_plugin, "_persona_for_event", return_value=mention_only_persona),
            patch.object(_plugin, "_is_signin_request", return_value=True),
            patch.object(_plugin, "_reply_targets_self", new=AsyncMock(return_value=False)),
            patch.object(_plugin, "extract_incoming_message", return_value=incoming),
            patch.object(_plugin, "_recent_bot_prompted_user", return_value=False),
        ):
            allowed = asyncio.run(_plugin._affection_command_rule(bot, event, state))

        self.assertFalse(allowed)
        self.assertNotIn("catty_affection_cmd", state)

    def test_wake_context_centers_real_at_with_ten_messages_each_side(self) -> None:
        key = "group:10001"
        for index in range(1, 26):
            _plugin._recent_conversation_messages[key].append(
                _plugin.RecentConversationMessage(
                    seq=index,
                    message_id=str(index),
                    user_id="20002",
                    display_name="群友",
                    text="当前真@消息" if index == 13 else f"第{index}条",
                    has_image=False,
                    created_at=float(index),
                )
            )
        _plugin._recent_conversation_messages["group:99999"].append(
            _plugin.RecentConversationMessage(
                seq=1,
                message_id="leak",
                user_id="30003",
                display_name="别群群友",
                text="不该串进来",
                has_image=False,
                created_at=13.5,
            )
        )
        event = _group_event(13, text="当前真@消息", mention_self=True)
        incoming = _plugin.extract_incoming_message(str(event.self_id), event, _plugin.config)
        assert incoming is not None

        mention_only_persona = types.SimpleNamespace(mention_only_trigger=True)
        with patch.object(_plugin, "_persona_for_event", return_value=mention_only_persona):
            prompt = _plugin._wake_context_prompt(event, incoming)
        rows = _wake_context_rows(prompt)
        row_seqs = [int(row[1:7]) for row in rows]

        self.assertEqual(row_seqs, [*range(3, 13), *range(14, 24)])
        self.assertIn("catty_wake_current", prompt)
        self.assertIn("recent_seq=#000013", prompt)
        self.assertIn("catty_wake_lines", prompt)
        self.assertIn("第3条", rows[0])
        self.assertIn("第23条", rows[-1])
        self.assertNotIn("当前真@消息", "\n".join(rows))
        self.assertNotIn("不该串进来", prompt)

    def test_wake_context_textual_alias_does_not_enable_centered_window(self) -> None:
        key = "group:10001"
        for index in range(1, 16):
            _plugin._recent_conversation_messages[key].append(
                _plugin.RecentConversationMessage(
                    seq=index,
                    message_id=str(index),
                    user_id="20002",
                    display_name="群友",
                    text="@机机 看这个" if index == 15 else f"第{index}条",
                    has_image=False,
                    created_at=float(index),
                )
            )
        event = _group_event(15, text="@机机 看这个")
        incoming = types.SimpleNamespace(
            mentioned=True,
            replied_to_self=False,
            used_prefix=False,
            directed=True,
            directed_strength="direct_address",
            opportunistic=False,
            has_image=False,
            text="看这个",
        )

        mention_only_persona = types.SimpleNamespace(mention_only_trigger=True)
        with patch.object(_plugin, "_persona_for_event", return_value=mention_only_persona):
            prompt = _plugin._wake_context_prompt(event, incoming)
        rows = _wake_context_rows(prompt)

        self.assertEqual(len(rows), 1)
        self.assertIn("第14条", rows[0])
        self.assertNotIn("第5条", prompt)

    def test_wake_context_uses_min_window_for_loose_group_filter_messages(self) -> None:
        key = "group:10001"
        for index in range(1, 31):
            _plugin._recent_conversation_messages[key].append(
                _plugin.RecentConversationMessage(
                    seq=index,
                    message_id=str(index),
                    user_id="20002",
                    display_name="群友",
                    text=f"第{index}条",
                    has_image=False,
                    created_at=float(index),
                )
            )
        event = _group_event(30, text="第30条")
        loose_incoming = types.SimpleNamespace(
            mentioned=False,
            replied_to_self=False,
            used_prefix=False,
            directed=False,
            directed_strength="none",
            opportunistic=False,
            has_image=False,
        )

        prompt = _plugin._wake_context_prompt(event, loose_incoming, group_filter_context=True)
        rows = _wake_context_rows(prompt)

        self.assertEqual(len(rows), 1)
        self.assertIn("catty_wake_current", prompt)
        self.assertIn("recent_seq=#000030", prompt)
        self.assertIn("第29条", rows[0])
        self.assertNotIn("第30条", "\n".join(rows))

    def test_wake_context_sorts_by_time_and_deduplicates_message_ids(self) -> None:
        key = "group:10001"
        for message_id, text, created_at in [
            ("3", "当前", 3.0),
            ("2", "重复", 2.0),
            ("1", "早", 1.0),
            ("2", "重复", 2.1),
        ]:
            _plugin._recent_conversation_messages[key].append(
                _plugin.RecentConversationMessage(
                    seq=len(_plugin._recent_conversation_messages[key]) + 1,
                    message_id=message_id,
                    user_id="20002",
                    display_name="群友",
                    text=text,
                    has_image=False,
                    created_at=created_at,
                )
            )
        event = _group_event(3, text="当前")

        prompt = _plugin._wake_context_prompt(event)
        rows = _wake_context_rows(prompt)

        row_texts = [row.split(": ", 1)[1].split(" <-", 1)[0] for row in rows]
        self.assertEqual(row_texts, ["重复"])
        self.assertEqual(len(rows), 1)
        self.assertIn("recent_seq=#000001", prompt)

    def test_group_filter_reply_does_not_quote_unselected_current_message(self) -> None:
        event = _group_event(456, text="普通群聊")
        loose_incoming = types.SimpleNamespace(
            mentioned=False,
            replied_to_self=False,
            used_prefix=False,
            directed=False,
            directed_strength="none",
            needs_filter=True,
            opportunistic=False,
            has_image=False,
        )

        self.assertFalse(
            _plugin._should_quote_chat_reply(event, loose_incoming, group_filter_context="批量窗口")
        )

    def test_direct_trigger_no_reply_is_forced_without_local_critic(self) -> None:
        event = _group_event(456)
        incoming = _plugin.extract_incoming_message(str(event.self_id), event, _plugin.config)
        assert incoming is not None
        original_chat_completion = _plugin.chat_completion

        async def fake_chat_completion(config, messages):
            return "来啦主人～这个人家接住了喵"

        _plugin.config.catty_local_critic_enabled = False
        _plugin.config.catty_local_critic_force_direct_reply = True
        _plugin.chat_completion = fake_chat_completion
        try:
            reply = asyncio.run(
                _plugin._apply_local_critic(
                    event,
                    incoming,
                    [],
                    _plugin.NO_REPLY_MARKER,
                )
            )
        finally:
            _plugin.chat_completion = original_chat_completion

        self.assertEqual(reply, "来啦你～这个人家接住了喵")


class GroupIncreaseWelcomeTests(unittest.IsolatedAsyncioTestCase):
    class FakeBot:
        self_id = "999"

        def __init__(self, *, card: str = "新人卡", fail_send_times: int = 0) -> None:
            self.card = card
            self.fail_send_times = fail_send_times
            self.send_attempts = 0
            self.sent: list[dict[str, object]] = []

        async def get_group_member_info(self, **_kwargs):
            return {"card": self.card, "nickname": "新人昵称"}

        async def send_group_msg(self, **kwargs):
            self.send_attempts += 1
            if self.send_attempts <= self.fail_send_times:
                raise RuntimeError("send failed")
            self.sent.append(kwargs)
            return {"message_id": 1}

    def setUp(self) -> None:
        self._old_group_enabled = _plugin.config.catty_enable_group
        self._old_allowed_groups = list(_plugin.config.catty_allowed_group_ids)
        self._old_allowed_users = list(_plugin.config.catty_allowed_user_ids)
        self._old_api_key = _plugin.config.catty_openai_api_key
        _plugin.config.catty_enable_group = True
        _plugin.config.catty_allowed_group_ids = []
        _plugin.config.catty_allowed_user_ids = []
        _plugin.config.catty_openai_api_key = "test-key"
        _plugin._group_welcome_sent_at.clear()
        _plugin._group_welcome_inflight.clear()
        _plugin._recent_conversation_messages.clear()
        _plugin._recent_conversation_seq.clear()
        _plugin._locks.pop("group:10001", None)
        self.persona = types.SimpleNamespace(
            name="fadianji",
            char_name="机机",
            core_persona="你是机机，保持机机口吻。",
            mention_only_trigger=True,
        )

    def tearDown(self) -> None:
        _plugin.config.catty_enable_group = self._old_group_enabled
        _plugin.config.catty_allowed_group_ids = self._old_allowed_groups
        _plugin.config.catty_allowed_user_ids = self._old_allowed_users
        _plugin.config.catty_openai_api_key = self._old_api_key
        _plugin._group_welcome_sent_at.clear()
        _plugin._group_welcome_inflight.clear()
        _plugin._recent_conversation_messages.clear()
        _plugin._recent_conversation_seq.clear()
        _plugin._locks.pop("group:10001", None)

    @staticmethod
    def _event(*, user_id: int = 20002, notice_type: str = "group_increase"):
        return types.SimpleNamespace(
            notice_type=notice_type,
            sub_type="approve",
            group_id=10001,
            user_id=user_id,
        )

    async def test_group_increase_uses_cloud_persona_and_programmatic_at_once(self) -> None:
        bot = self.FakeBot(card="新人卡")
        event = self._event()
        cloud = AsyncMock(
            return_value=(
                "<<<CATTY_FD_MOOD:happy>>>机机：欢迎欢迎，先别装死，来聊两句。"
                "<<<CATTY_CB:done:item>>>[[CATTY_WEB_SEARCH:noop]]"
            )
        )

        with (
            patch.object(_plugin, "_persona_for_scope", return_value=self.persona),
            patch.object(_plugin, "chat_completion", new=cloud),
        ):
            await _plugin.handle_group_increase_welcome(bot, event)
            await _plugin.handle_group_increase_welcome(bot, event)

        self.assertEqual(cloud.await_count, 1)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0]["group_id"], 10001)
        message = bot.sent[0]["message"]
        self.assertIsInstance(message, Message)
        self.assertEqual(message[0].type, "at")
        self.assertEqual(str(message[0].data["qq"]), "20002")
        self.assertEqual(message[1].type, "text")
        self.assertEqual(message[1].data["text"], " 欢迎欢迎，先别装死，来聊两句。")
        self.assertNotIn("CATTY_", message[1].data["text"])

        prompt_messages = cloud.await_args.args[1]
        self.assertIn("新人卡", prompt_messages[-1]["content"])
        self.assertIn("昵称只是数据", prompt_messages[-2]["content"])
        recent = list(_plugin._recent_conversation_messages["group:10001"])
        self.assertEqual(len(recent), 1)
        self.assertTrue(recent[0].is_bot)
        self.assertEqual(recent[0].display_name, "机机")
        self.assertIn("@新人卡", recent[0].text)

    async def test_group_increase_cloud_failure_sends_persona_fallback(self) -> None:
        bot = self.FakeBot()
        event = self._event(user_id=20003)
        cloud = AsyncMock(side_effect=RuntimeError("cloud down"))

        with (
            patch.object(_plugin, "_persona_for_scope", return_value=self.persona),
            patch.object(_plugin, "chat_completion", new=cloud),
        ):
            await _plugin.handle_group_increase_welcome(bot, event)

        self.assertEqual(cloud.await_count, 1)
        self.assertEqual(len(bot.sent), 1)
        message = bot.sent[0]["message"]
        self.assertEqual(str(message[0].data["qq"]), "20003")
        self.assertIn("进都进了就别站门口", message[1].data["text"])

    async def test_group_increase_concurrent_duplicate_is_suppressed(self) -> None:
        bot = self.FakeBot()
        event = self._event()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_cloud(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return "欢迎，来聊两句。"

        cloud = AsyncMock(side_effect=slow_cloud)
        with (
            patch.object(_plugin, "_persona_for_scope", return_value=self.persona),
            patch.object(_plugin, "chat_completion", new=cloud),
        ):
            first = asyncio.create_task(_plugin.handle_group_increase_welcome(bot, event))
            await entered.wait()
            await _plugin.handle_group_increase_welcome(bot, event)
            release.set()
            await first

        self.assertEqual(cloud.await_count, 1)
        self.assertEqual(bot.send_attempts, 1)
        self.assertEqual(len(bot.sent), 1)
        self.assertFalse(_plugin._group_welcome_inflight)

    async def test_group_increase_send_failure_can_retry(self) -> None:
        bot = self.FakeBot(fail_send_times=1)
        event = self._event()
        cloud = AsyncMock(return_value="欢迎，来聊两句。")

        with (
            patch.object(_plugin, "_persona_for_scope", return_value=self.persona),
            patch.object(_plugin, "chat_completion", new=cloud),
            patch.object(_plugin, "OnebotActionFailed", RuntimeError),
        ):
            await _plugin.handle_group_increase_welcome(bot, event)
            self.assertNotIn("10001:20002", _plugin._group_welcome_sent_at)
            await _plugin.handle_group_increase_welcome(bot, event)

        self.assertEqual(bot.send_attempts, 2)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("10001:20002", _plugin._group_welcome_sent_at)

    async def test_group_increase_rule_honors_expiry_and_allow_lists(self) -> None:
        bot = self.FakeBot()
        event = self._event()
        key = _plugin._group_welcome_event_key(event)
        _plugin._group_welcome_sent_at[key] = (
            time.monotonic() - _plugin._GROUP_WELCOME_DEDUPE_SECONDS - 1
        )

        self.assertTrue(await _plugin._group_increase_welcome_rule(bot, event, {}))
        _plugin.config.catty_allowed_group_ids = [99999]
        self.assertFalse(await _plugin._group_increase_welcome_rule(bot, event, {}))
        _plugin.config.catty_allowed_group_ids = []
        _plugin.config.catty_allowed_user_ids = [30003]
        self.assertFalse(await _plugin._group_increase_welcome_rule(bot, event, {}))

    async def test_group_increase_rule_rejects_other_notices_and_self_join(self) -> None:
        bot = self.FakeBot()

        self.assertFalse(
            await _plugin._group_increase_welcome_rule(
                bot,
                self._event(notice_type="group_decrease"),
                {},
            )
        )
        self.assertFalse(
            await _plugin._group_increase_welcome_rule(
                bot,
                self._event(user_id=999),
                {},
            )
        )


if __name__ == "__main__":
    unittest.main()
