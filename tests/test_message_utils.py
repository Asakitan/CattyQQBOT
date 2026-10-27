import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_message_utils_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)

_CONFIG_MODULE = types.ModuleType(f"{_PACKAGE_NAME}.config")
_CONFIG_MODULE.Config = object
sys.modules.setdefault(f"{_PACKAGE_NAME}.config", _CONFIG_MODULE)

for _module_name in ("features", "message_utils"):
    _path = _ROOT / "src" / "catty_qq_ai" / f"{_module_name}.py"
    _spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{_module_name}", _path)
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)

_message_utils = sys.modules[f"{_PACKAGE_NAME}.message_utils"]


class HistoryKeyTests(unittest.TestCase):
    class GroupEvent:
        def __init__(self, group_id: int, user_id: int) -> None:
            self.group_id = group_id
            self.user_id = user_id

    def test_group_scope_shares_one_key_across_users(self) -> None:
        config = types.SimpleNamespace(catty_group_history_scope="group")
        with patch.object(_message_utils, "GroupMessageEvent", self.GroupEvent):
            self.assertEqual(
                _message_utils.build_history_key(self.GroupEvent(42, 7), config),
                "group:42",
            )
            self.assertEqual(
                _message_utils.build_history_key(self.GroupEvent(42, 8), config),
                "group:42",
            )

    def test_user_scope_splits_group_history_by_user(self) -> None:
        config = types.SimpleNamespace(catty_group_history_scope="user")
        with patch.object(_message_utils, "GroupMessageEvent", self.GroupEvent):
            self.assertEqual(
                _message_utils.build_history_key(self.GroupEvent(42, 7), config),
                "group:42:user:7",
            )
            self.assertEqual(
                _message_utils.build_history_key(self.GroupEvent(42, 8), config),
                "group:42:user:8",
            )

    def test_private_history_is_always_per_user(self) -> None:
        event = types.SimpleNamespace(user_id=7)
        for scope in ("group", "user"):
            with self.subTest(scope=scope), patch.object(
                _message_utils,
                "GroupMessageEvent",
                self.GroupEvent,
            ):
                config = types.SimpleNamespace(catty_group_history_scope=scope)
                self.assertEqual(
                    _message_utils.build_history_key(event, config),
                    "private:7",
                )


class DirectedKeywordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = types.SimpleNamespace(
            catty_trigger_prefixes=["猫猫", "笨猫", "喵"],
            catty_directed_keywords=["你", "猫猫", "猫娘", "看看", "图片"],
        )

    def test_inline_name_mentions_are_sent_to_ai_for_judgment(self) -> None:
        self.assertTrue(_message_utils._has_directed_keyword("这个猫猫表情好好笑", self.config))
        self.assertTrue(_message_utils._has_directed_keyword("我叫猫猫也可以吗", self.config))

    def test_inline_name_address_is_also_sent_to_ai(self) -> None:
        self.assertTrue(_message_utils._has_directed_keyword("猫猫你看看这个", self.config))
        self.assertTrue(_message_utils._has_directed_keyword("猫猫，帮我想一下", self.config))
        self.assertTrue(_message_utils._has_directed_keyword("问猫猫这个怎么弄", self.config))

    def test_direct_address_strength_is_higher_for_obvious_calls(self) -> None:
        self.assertEqual(_message_utils._directed_keyword_strength("猫猫你看看这个", self.config), "direct_address")
        self.assertEqual(_message_utils._directed_keyword_strength("猫猫，帮我想一下", self.config), "direct_address")
        self.assertEqual(_message_utils._directed_keyword_strength("问猫猫这个怎么弄", self.config), "direct_address")

    def test_inline_name_mentions_remain_keyword_strength(self) -> None:
        self.assertEqual(_message_utils._directed_keyword_strength("这个猫猫表情好好笑", self.config), "keyword")
        self.assertEqual(_message_utils._directed_keyword_strength("我叫猫猫也可以吗", self.config), "keyword")

    def test_feature_keywords_still_wake_bot(self) -> None:
        self.assertTrue(_message_utils._has_directed_keyword("联网搜索 星痕共鸣职业", self.config))
        self.assertTrue(_message_utils._has_directed_keyword("卡拉彼丘弦化怎么练", self.config))
        self.assertTrue(_message_utils._has_directed_keyword("来个海龟汤", self.config))

    def test_second_person_mentions_are_sent_to_ai_for_subject_judgment(self) -> None:
        # "你觉得呢": "你"+尾巴"呢" → 直接对话语气,应放行
        self.assertTrue(_message_utils._has_directed_keyword("你觉得呢", self.config))
        # "他说你昨天很忙": 第三人称转述里出现的"你",前置"说"不是请求引导字、
        # 后缀"昨"也不是语气助词 → 现行实现刻意拦掉,避免猫猫被第三方对话抢答。
        # 这是产品决策(参考 _bot_continuation_judgement_prompt 的『默认 NO_REPLY』
        # 路线):宁可漏几条,也别让猫猫在群友互相聊天时插嘴。
        self.assertFalse(_message_utils._has_directed_keyword("他说你昨天很忙", self.config))

    def test_plain_messages_without_markers_stay_unhandled(self) -> None:
        self.assertFalse(_message_utils._has_directed_keyword("今天午饭吃什么", self.config))


class ReplySplitTests(unittest.TestCase):
    def test_long_reply_is_split_into_two_chunks(self) -> None:
        reply = "第一句很长很长。" * 20

        chunks = _message_utils.split_reply(reply, 30)

        self.assertEqual(len(chunks), 2)
        self.assertEqual("".join(chunks), reply)

    def test_split_can_be_disabled_by_max_chunks(self) -> None:
        reply = "猫猫要说很多很多话" * 10

        self.assertEqual(_message_utils.split_reply(reply, 20, max_chunks=1), [reply])

    def test_long_reply_can_use_more_than_two_chunks(self) -> None:
        reply = "第一句很长很长。" * 30

        chunks = _message_utils.split_reply(reply, 30, max_chunks=4)

        self.assertGreater(len(chunks), 2)
        self.assertLessEqual(len(chunks), 4)
        self.assertEqual("".join(chunks), reply)


class BotLoopGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个测试前重置一次性 dump 标记，避免互相干扰
        _message_utils._marked_bot_dump_done = False

    def test_marked_bot_via_is_bot_attr(self) -> None:
        sender = types.SimpleNamespace(is_bot=True, role="member")
        event = types.SimpleNamespace(user_id=12345, sender=sender)
        self.assertTrue(_message_utils._looks_like_marked_bot(event))

    def test_marked_bot_via_role_field(self) -> None:
        sender = types.SimpleNamespace(role="bot", is_bot=False)
        event = types.SimpleNamespace(user_id=12345, sender=sender)
        self.assertTrue(_message_utils._looks_like_marked_bot(event))

    def test_marked_bot_via_category_field(self) -> None:
        sender = types.SimpleNamespace(category="official_bot")
        event = types.SimpleNamespace(user_id=12345, sender=sender)
        self.assertTrue(_message_utils._looks_like_marked_bot(event))

    def test_marked_bot_via_raw_event_dict(self) -> None:
        sender = types.SimpleNamespace(role="member")
        raw_event = {"sender": {"is_bot": True}}
        event = types.SimpleNamespace(user_id=12345, sender=sender, raw_event=raw_event)
        self.assertTrue(_message_utils._looks_like_marked_bot(event))

    def test_normal_user_is_not_marked(self) -> None:
        sender = types.SimpleNamespace(is_bot=False, role="member", nickname="张三")
        event = types.SimpleNamespace(user_id=12345, sender=sender)
        self.assertFalse(_message_utils._looks_like_marked_bot(event))

    def test_bot_intro_self_intro_temporary(self) -> None:
        text = "@@笨猫猫 嗨~，我是Q群管家，可以发送入群欢迎和定时消息，暂时还不能和你对话哦。"
        self.assertTrue(_message_utils._looks_like_bot_self_intro(text))

    def test_bot_intro_assistant_template(self) -> None:
        text = "嗨~，我是XX小助手，有任何问题都可以问我！"
        self.assertTrue(_message_utils._looks_like_bot_self_intro(text))

    def test_bot_intro_signin_helper(self) -> None:
        text = "Q群管家签到小助手提醒您按时签到"
        self.assertTrue(_message_utils._looks_like_bot_self_intro(text))

    def test_bot_intro_does_not_match_casual_user(self) -> None:
        self.assertFalse(_message_utils._looks_like_bot_self_intro("我是张三啊"))
        self.assertFalse(_message_utils._looks_like_bot_self_intro("猫猫今天又萌了一波"))
        self.assertFalse(_message_utils._looks_like_bot_self_intro(""))


class ReplySourceTests(unittest.TestCase):
    def test_current_message_images_are_deduplicated_by_url(self) -> None:
        event = types.SimpleNamespace(
            message=[
                types.SimpleNamespace(type="image", data={"url": "https://img/a.jpg", "file": "a"}),
                types.SimpleNamespace(type="mface", data={"url": "https://img/a.jpg", "emoji_id": "e"}),
            ]
        )

        self.assertEqual(
            _message_utils.extract_images(event),
            [("https://img/a.jpg", "image:a")],
        )

    def test_dict_reply_source_extracts_sender_text_and_images(self) -> None:
        source = _message_utils.reply_source_from_message(
            {
                "message_id": 88,
                "sender": {"user_id": 999, "card": "机机bot", "nickname": "机器人"},
                "message": [
                    {"type": "text", "data": {"text": "看这张"}},
                    {"type": "at", "data": {"qq": "123", "name": "桃子"}},
                    {"type": "image", "data": {"url": "https://img/a.jpg", "file": "a-file"}},
                    {
                        "type": "mface",
                        "data": {"url": "https://img/b.gif", "package_id": "p", "emoji_id": "e"},
                    },
                    {"type": "image", "data": {"url": "https://img/a.jpg", "file": "duplicate"}},
                ],
            },
            self_id="999",
        )

        self.assertEqual(source.message_id, "88")
        self.assertEqual(source.sender_id, "999")
        self.assertEqual(source.sender_name, "机机bot")
        self.assertEqual(source.text, "看这张@桃子(QQ:123)")
        self.assertTrue(source.is_current_bot)
        self.assertEqual([url for url, _key in source.images], ["https://img/a.jpg", "https://img/b.gif"])
        self.assertTrue(source.images[0][1].startswith("image:"))
        self.assertTrue(source.images[1][1].startswith("mface:"))

    def test_string_reply_source_keeps_text_and_parses_media_control_codes(self) -> None:
        source = _message_utils.reply_source_from_message(
            {
                "message_id": 100,
                "sender": {"user_id": 123, "nickname": "桃桃"},
                "message": "引用文字[CQ:image,file=a.jpg,url=https://img/a.jpg]",
            },
            self_id="999",
        )

        self.assertEqual(source.text, "引用文字")
        self.assertEqual(source.images[0][0], "https://img/a.jpg")
        self.assertTrue(source.images[0][1].startswith("image:"))

    def test_object_reply_source_ignores_face_and_images_without_url(self) -> None:
        message = types.SimpleNamespace(
            id="99",
            sender=types.SimpleNamespace(user_id=123, card="", nickname="桃桃"),
            message=[
                types.SimpleNamespace(type="text", data={"text": "原文"}),
                types.SimpleNamespace(type="face", data={"id": "14"}),
                types.SimpleNamespace(type="image", data={"file": "missing-url"}),
            ],
            raw_message="",
        )

        source = _message_utils.reply_source_from_message(message, self_id="999")

        self.assertEqual(source.message_id, "99")
        self.assertEqual(source.sender_id, "123")
        self.assertEqual(source.sender_name, "桃桃")
        self.assertEqual(source.text, "原文")
        self.assertFalse(source.is_current_bot)
        self.assertEqual(source.images, [])


class IncomingExpressionTests(unittest.TestCase):
    def setUp(self):
        self.config = types.SimpleNamespace(
            catty_allowed_user_ids=set(), catty_enable_private=True,
            catty_trigger_prefixes=["猫猫"], catty_directed_keywords=["猫猫", "你"],
            catty_private_require_prefix=False,
        )

    def _event(self, *segments):
        message = Message(list(segments))
        return PrivateMessageEvent(
            time=0, self_id=999, post_type="message", sub_type="friend", user_id=7,
            message_type="private", message_id=1, message=message, original_message=message,
            raw_message=str(message), font=0, sender=Sender(user_id=7, nickname="用户"),
        )

    def test_native_face_only_is_recognized_without_cq_code(self):
        incoming = _message_utils.extract_incoming_message(
            "999", self._event(MessageSegment(type="face", data={"id": "182"})), self.config,
        )
        self.assertIsNotNone(incoming)
        self.assertTrue(incoming.has_expression)
        self.assertFalse(incoming.has_image)
        self.assertIn("QQ表情：笑哭", incoming.history_content)
        self.assertNotIn("CQ:face", incoming.text + incoming.history_content)

    def test_text_and_face_keep_text_and_expression(self):
        incoming = _message_utils.extract_incoming_message(
            "999", self._event(MessageSegment.text("真的绷不住"), MessageSegment(type="face", data={"id": "264"})), self.config,
        )
        self.assertEqual(incoming.text, "真的绷不住")
        self.assertIn("QQ表情：捂脸", incoming.history_content)
        self.assertTrue(incoming.has_expression)

    def test_unknown_face_is_not_assigned_a_guessed_meaning(self):
        incoming = _message_utils.extract_incoming_message(
            "999", self._event(MessageSegment(type="face", data={"id": "99999"})), self.config,
        )
        self.assertIn("QQ表情（含义未知）", incoming.history_content)
        self.assertNotIn("99999", incoming.history_content)

    def test_raw_face_name_and_market_sticker_are_preserved(self):
        incoming = _message_utils.extract_incoming_message(
            "999", self._event(
                MessageSegment(type="face", data={"id": "99999", "raw": {"faceText": "/摸鱼"}}),
                MessageSegment(type="mface", data={"summary": "[难绷]"}),
            ), self.config,
        )
        self.assertIn("QQ表情：摸鱼", incoming.history_content)
        self.assertIn("表情包：难绷", incoming.history_content)
        self.assertFalse(incoming.has_image)

    def test_unicode_emoji_and_plain_text_are_distinguished(self):
        for text, expected in (("真的绷不住😂", True), ("只是普通文字", False)):
            with self.subTest(text=text):
                incoming = _message_utils.extract_incoming_message(
                    "999", self._event(MessageSegment.text(text)), self.config,
                )
                self.assertEqual(incoming.text, text)
                self.assertEqual(incoming.has_expression, expected)

    def test_mface_with_url_remains_an_image(self):
        incoming = _message_utils.extract_incoming_message(
            "999", self._event(MessageSegment(type="mface", data={
                "url": "https://example.test/sticker", "summary": "[动画表情]",
            })), self.config,
        )
        self.assertTrue(incoming.has_image)
        self.assertTrue(incoming.has_expression)
        self.assertEqual(incoming.image_urls, ["https://example.test/sticker"])
        self.assertIn("表情包", incoming.history_content)

    def test_private_prefix_requirement_is_preserved(self):
        self.config.catty_private_require_prefix = True
        incoming = _message_utils.extract_incoming_message(
            "999", self._event(MessageSegment(type="face", data={"id": "182"})), self.config,
        )
        self.assertIsNone(incoming)


if __name__ == "__main__":
    unittest.main()
