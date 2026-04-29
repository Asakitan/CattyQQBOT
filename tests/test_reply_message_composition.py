from pathlib import Path
import asyncio
import importlib.util
import re
import sys
import tempfile
import types
import unittest

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
    return [line for line in prompt.splitlines() if re.match(r"^[+-]\d+\. ", line)]


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

    def test_web_search_context_delegates_to_main_ai(self) -> None:
        context = asyncio.run(_plugin._build_web_search_context("星痕共鸣"))

        self.assertIn("由主 AI 自己", context)
        self.assertIn("原生联网搜索能力", context)
        self.assertIn("同一条最终回复", context)
        self.assertIn("图片、壁纸、表情包或图包", context)
        self.assertIn("不要编造搜索结果", context)
        self.assertNotIn("DuckDuckGo", context)

    def test_invalid_hot_reload_config_does_not_replace_current_config(self) -> None:
        old_model = _plugin.config.catty_openai_model
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text('{"ai": ', encoding="utf-8")

            loaded = _plugin._load_runtime_config_from_path(config_path)

        self.assertIsNone(loaded)
        self.assertEqual(_plugin.config.catty_openai_model, old_model)

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

        self.assertIn("你看看", prompt)
        self.assertIn("技术求助", prompt)
        self.assertIn("可执行技术结论", prompt)
        self.assertIn("句尾的请求目标", prompt)
        self.assertIn("先回答 B", prompt)
        self.assertIn("冒号后就是用户完整原文", prompt)
        self.assertIn("不要只吐槽、玩梗或空泛追问", prompt)

    def test_wake_context_uses_hard_trigger_window_up_to_fifty_messages(self) -> None:
        key = "group:10001"
        for index in range(1, 61):
            _plugin._recent_conversation_messages[key].append(
                _plugin.RecentConversationMessage(
                    message_id=str(index),
                    user_id="20002",
                    display_name="群友",
                    text="猫猫看这个" if index == 60 else f"第{index}条",
                    has_image=False,
                    created_at=float(index),
                )
            )
        event = _group_event(60, mention_self=True)
        incoming = _plugin.extract_incoming_message(str(event.self_id), event, _plugin.config)
        assert incoming is not None

        prompt = _plugin._wake_context_prompt(event, incoming)
        rows = _wake_context_rows(prompt)

        self.assertEqual(len(rows), 50)
        self.assertIn("最多 50 条", prompt)
        self.assertIn("按时间顺序整理并去重", prompt)
        self.assertIn("群聊按群号隔离", prompt)
        self.assertIn("第11条", rows[0])
        self.assertNotIn("第10条", prompt)
        self.assertIn("猫猫看这个", rows[-1])
        self.assertIn("<- 当前唤起消息", rows[-1])

    def test_wake_context_uses_min_window_for_loose_group_filter_messages(self) -> None:
        key = "group:10001"
        for index in range(1, 31):
            _plugin._recent_conversation_messages[key].append(
                _plugin.RecentConversationMessage(
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

        self.assertEqual(len(rows), 16)
        self.assertIn("最多 16 条", prompt)
        self.assertIn("第15条", rows[0])
        self.assertIn("第30条", rows[-1])
        self.assertIn("<- 当前唤起消息", rows[-1])

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
        self.assertEqual(row_texts, ["早", "重复", "当前"])
        self.assertEqual(row_texts.count("重复"), 1)

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

        self.assertEqual(reply, "来啦主人～这个人家接住了喵")


if __name__ == "__main__":
    unittest.main()
