"""联系上下文识图: 最近 N 条消息里的图要一起带给主 AI (2026-10-26)。"""
from __future__ import annotations

import importlib.util
import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_vision_context_test"


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
_message_utils = sys.modules[f"{_PACKAGE_NAME}.message_utils"]


def _group_event(
    *,
    user_id: int = 20002,
    message_id: int = 1,
    text: str = "",
    image_url: str = "",
    image_file: str = "",
    group_id: int = 10001,
    mention_self: bool = False,
) -> GroupMessageEvent:
    segments: list[MessageSegment] = []
    if mention_self:
        segments.append(MessageSegment.at(999))
    if image_url:
        segments.append(
            MessageSegment(
                type="image", data={"url": image_url, "file": image_file or image_url}
            )
        )
    if text:
        segments.append(MessageSegment.text(text))
    message = Message(segments)
    return GroupMessageEvent(
        time=0,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=text or "[图片]",
        font=0,
        sender=Sender(user_id=user_id, nickname="拉粑粑", card="我会自己拉粑粑"),
        group_id=group_id,
    )


def _incoming(*, text: str = "你能搓这个效果吗", image_urls=None, image_keys=None):
    return _message_utils.ExtractedMessage(
        text=text,
        history_content=text,
        mentioned=True,
        replied_to_self=False,
        used_prefix=False,
        image_urls=list(image_urls or []),
        image_keys=list(image_keys or []),
        has_image=bool(image_urls),
        directed=True,
        directed_strength="strong",
        directly_requested=True,
        needs_filter=False,
        opportunistic=False,
    )


class RecentContextImageTests(unittest.TestCase):
    def setUp(self) -> None:
        _plugin._recent_conversation_messages.clear()
        _plugin._recent_conversation_seq.clear()

    def _scope(self, group_id: int = 10001) -> str:
        return f"group:{group_id}"

    def test_records_image_urls_and_keys_per_message(self) -> None:
        event = _group_event(message_id=11, text="这是ds1017搓的", image_url="https://img/a.jpg")

        _plugin._remember_recent_conversation_event(event)

        remembered = _plugin._recent_conversation_messages[self._scope()][-1]
        self.assertEqual(remembered.image_urls, ("https://img/a.jpg",))
        self.assertEqual(len(remembered.image_keys), 1)
        self.assertTrue(remembered.image_keys[0].startswith("image:"))
        self.assertEqual(remembered.text, "这是ds1017搓的")

    def test_records_images_from_incoming_without_reparsing_event(self) -> None:
        event = _group_event(message_id=12, text="看这张")
        incoming = _incoming(
            text="看这张",
            image_urls=["https://img/from-incoming.jpg"],
            image_keys=["image:file-from-incoming"],
        )

        _plugin._remember_recent_conversation_event(event, incoming)

        remembered = _plugin._recent_conversation_messages[self._scope()][-1]
        self.assertEqual(remembered.image_urls, ("https://img/from-incoming.jpg",))
        self.assertEqual(remembered.image_keys, ("image:file-from-incoming",))

    def test_recent_messages_keep_native_face_context_without_cq_code(self) -> None:
        event = _group_event(message_id=14)
        event.message.append(MessageSegment(type="face", data={"id": "182"}))
        event.raw_message = str(event.message)

        _plugin._remember_recent_conversation_event(event)

        remembered = _plugin._recent_conversation_messages[self._scope()][-1]
        self.assertIn("QQ表情：笑哭", remembered.text)
        self.assertNotIn("CQ:face", remembered.text)

    def test_self_echo_before_receipt_is_attributed_to_bound_persona(self) -> None:
        scope = self._scope()
        catty = types.SimpleNamespace(name="catty", char_name="猫猫", definition_revision=None)
        fadianji = types.SimpleNamespace(name="fadianji", char_name="机机", definition_revision=None)
        selected = [catty]

        def resolve(key):
            return _plugin.active_persona_for_scope(key) or selected[0]

        with patch.object(_plugin, "_persona_for_scope", side_effect=resolve), \
             patch.object(_plugin._message_utils_mod, "_looks_like_marked_bot", side_effect=lambda event: event.user_id == 888):
            _plugin._remember_recent_conversation_event(_group_event(user_id=200, message_id=1, text="群友"))
            _plugin._remember_recent_conversation_event(_group_event(user_id=888, message_id=2, text="其他机器人"))
            _plugin._remember_recent_conversation_event(_group_event(user_id=999, message_id=3, text="猫猫回复"))
            self.assertEqual([row.text for row in _plugin._recent_conversation_for_scope(scope)], ["群友", "其他机器人"])
            selected[0] = fadianji
            token = _plugin.bind_turn_persona(scope, catty)
            try:
                _plugin._remember_bot_conversation_message(scope, bot_id="999", text="猫猫回复", message_id="3", update_continuation=False)
            finally:
                _plugin.reset_turn_persona(token)
            self.assertEqual(len(_plugin._recent_conversation_messages[scope]), 3)
            self.assertEqual(_plugin._recent_conversation_messages[scope][-1].persona_identity, "catty")
            self.assertEqual([row.text for row in _plugin._recent_conversation_for_scope(scope)], ["群友", "其他机器人"])
            selected[0] = catty
            self.assertEqual([row.text for row in _plugin._recent_conversation_for_scope(scope)], ["群友", "其他机器人", "猫猫回复"])
            self.assertEqual(_plugin._recent_conversation_for_scope("group:555"), ())

    def test_receipt_before_self_echo_and_custom_revision_remain_isolated(self) -> None:
        scope = self._scope()
        role = types.SimpleNamespace(name="custom", char_name="小青", definition_revision="v1")
        with patch.object(_plugin, "_persona_for_scope", return_value=role):
            _plugin._remember_bot_conversation_message(scope, bot_id="999", text="小青回复", message_id="9", update_continuation=False)
            _plugin._remember_recent_conversation_event(_group_event(user_id=999, message_id=9, text="小青回复"))
            self.assertEqual(len(_plugin._recent_conversation_messages[scope]), 1)
            self.assertEqual(_plugin._recent_conversation_for_scope(scope)[0].persona_identity, "custom@v1")
            role.definition_revision = "v2"
            self.assertEqual(_plugin._recent_conversation_for_scope(scope), ())

    def test_missing_image_key_does_not_drop_image(self) -> None:
        event = _group_event(message_id=13, text="看这两张")
        incoming = _incoming(image_urls=["https://img/a", "https://img/b"], image_keys=["first"])
        _plugin._remember_recent_conversation_event(event, incoming)
        remembered = _plugin._recent_conversation_messages[self._scope()][-1]
        self.assertEqual(remembered.image_urls, ("https://img/a", "https://img/b"))
        self.assertEqual(remembered.image_keys, ("first", "https://img/b"))

    def test_collects_images_inside_lookback_window(self) -> None:
        _plugin._remember_recent_conversation_event(
            _group_event(message_id=1, text="效果图", image_url="https://img/old.jpg")
        )
        for index in range(10):
            _plugin._remember_recent_conversation_event(
                _group_event(message_id=100 + index, text=f"闲聊{index}")
            )

        entries = _plugin._recent_context_images_for_scope(
            self._scope(), lookback_messages=16, max_images=4
        )

        self.assertEqual([entry[1] for entry in entries], ["https://img/old.jpg"])
        self.assertEqual(entries[0][3], 10)  # 距今 10 条消息
        self.assertEqual(entries[0][0].display_name, "我会自己拉粑粑")

    def test_lookback_window_drops_older_images(self) -> None:
        _plugin._remember_recent_conversation_event(
            _group_event(message_id=1, text="旧图", image_url="https://img/old.jpg")
        )
        for index in range(16):
            _plugin._remember_recent_conversation_event(
                _group_event(message_id=100 + index, text=f"闲聊{index}")
            )

        self.assertEqual(
            _plugin._recent_context_images_for_scope(
                self._scope(), lookback_messages=16, max_images=4
            ),
            [],
        )
        older = _plugin._recent_context_images_for_scope(
            self._scope(), lookback_messages=17, max_images=4
        )
        self.assertEqual([entry[1] for entry in older], ["https://img/old.jpg"])

    def test_exclude_urls_and_max_images_keep_newest(self) -> None:
        for index, url in enumerate(
            ["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"], start=1
        ):
            _plugin._remember_recent_conversation_event(
                _group_event(message_id=index, text=f"图{index}", image_url=url)
            )

        entries = _plugin._recent_context_images_for_scope(
            self._scope(),
            lookback_messages=16,
            max_images=1,
            exclude_urls={"https://img/3.jpg"},
        )

        self.assertEqual([entry[1] for entry in entries], ["https://img/2.jpg"])

    def test_stale_images_are_filtered_by_age(self) -> None:
        _plugin._remember_recent_conversation_event(
            _group_event(message_id=1, text="老图", image_url="https://img/stale.jpg")
        )
        _plugin._recent_conversation_messages[self._scope()][-1].created_at -= 3600.0

        self.assertEqual(
            _plugin._recent_context_images_for_scope(
                self._scope(), lookback_messages=16, max_images=4, max_age_seconds=60.0
            ),
            [],
        )

    def test_bare_mention_selects_previous_image_from_same_sender(self) -> None:
        _plugin._remember_recent_conversation_event(
            _group_event(message_id=1, image_url="https://img/scores.png")
        )
        _plugin._remember_recent_conversation_event(
            _group_event(user_id=30003, message_id=2, image_url="https://img/other.png")
        )
        event = _group_event(message_id=3, mention_self=True)
        incoming = _message_utils.extract_incoming_message("999", event, _plugin.config)
        self.assertIsNotNone(incoming)
        _plugin._remember_recent_conversation_event(event, incoming)

        self.assertTrue(_plugin._is_bare_image_wakeup(event, incoming))
        entries = _plugin._recent_context_images_for_scope(
            self._scope(), lookback_messages=16, max_images=4,
            reference_seq=_plugin._recent_conversation_messages[self._scope()][-1].seq,
            preceding_user_id=str(event.user_id),
        )

        self.assertEqual([entry[1] for entry in entries], ["https://img/scores.png"])
        self.assertEqual(entries[0][0].user_id, str(event.user_id))
        self.assertEqual(entries[0][3], 2)

    def test_bare_mention_does_not_skip_same_sender_text_to_reuse_old_image(self) -> None:
        _plugin._remember_recent_conversation_event(
            _group_event(message_id=1, image_url="https://img/old.png")
        )
        _plugin._remember_recent_conversation_event(
            _group_event(message_id=2, text="今天几点下班")
        )
        event = _group_event(message_id=3, mention_self=True)
        _plugin._remember_recent_conversation_event(event)

        entries = _plugin._recent_context_images_for_scope(
            self._scope(), lookback_messages=16, max_images=4,
            reference_seq=_plugin._recent_conversation_messages[self._scope()][-1].seq,
            preceding_user_id=str(event.user_id),
        )

        self.assertEqual(entries, [])

    def test_bare_mention_does_not_borrow_other_group_or_expired_image(self) -> None:
        _plugin._remember_recent_conversation_event(
            _group_event(message_id=1, image_url="https://img/expired.png")
        )
        _plugin._recent_conversation_messages[self._scope()][-1].created_at -= 3600.0
        _plugin._remember_recent_conversation_event(
            _group_event(message_id=2, image_url="https://img/other-group.png", group_id=555)
        )
        event = _group_event(message_id=3, mention_self=True)
        _plugin._remember_recent_conversation_event(event)

        entries = _plugin._recent_context_images_for_scope(
            self._scope(), lookback_messages=16, max_images=4, max_age_seconds=1800.0,
            reference_seq=_plugin._recent_conversation_messages[self._scope()][-1].seq,
            preceding_user_id=str(event.user_id),
        )

        self.assertEqual(entries, [])

    def test_mention_with_words_expression_or_image_is_not_bare_wakeup(self) -> None:
        events = [
            _group_event(mention_self=True, text="今天几点下班"),
            _group_event(mention_self=True, image_url="https://img/current.png"),
            _group_event(),
        ]
        expression = _group_event(mention_self=True)
        expression.message.append(MessageSegment(type="face", data={"id": "182"}))
        events.append(expression)
        for event in events:
            with self.subTest(message=str(event.message)):
                incoming = _message_utils.extract_incoming_message("999", event, _plugin.config)
                if incoming is not None:
                    self.assertFalse(_plugin._is_bare_image_wakeup(event, incoming))

    def test_same_url_from_different_senders_retains_both_origins(self) -> None:
        for uid, mid in ((100, 1), (200, 2)):
            _plugin._remember_recent_conversation_event(_group_event(user_id=uid, message_id=mid, image_url="https://img/shared"))
        entries = _plugin._recent_context_images_for_scope(self._scope(), lookback_messages=16, max_images=4)
        self.assertEqual([row[0].user_id for row in entries], ["100", "200"])

    def test_following_images_exclude_other_users_and_other_groups(self) -> None:
        _plugin._remember_recent_conversation_event(_group_event(user_id=200, message_id=1, image_url="https://img/old"))
        _plugin._remember_recent_conversation_event(_group_event(user_id=100, message_id=2, text="新衣服好看吗"))
        request_seq = _plugin._recent_conversation_messages[self._scope()][-1].seq
        _plugin._remember_recent_conversation_event(_group_event(user_id=200, message_id=3, image_url="https://img/other"))
        _plugin._remember_recent_conversation_event(_group_event(user_id=100, message_id=4, image_url="https://img/right"))
        _plugin._remember_recent_conversation_event(_group_event(user_id=100, message_id=5, image_url="https://img/other-group", group_id=555))
        entries = _plugin._recent_context_images_for_scope(self._scope(), lookback_messages=16, max_images=4,
                                                          reference_seq=request_seq, following_user_id="100")
        self.assertEqual([row[1] for row in entries], ["https://img/old", "https://img/right"])
        self.assertGreater(entries[0][3], 0)
        self.assertLess(entries[1][3], 0)
        self.assertFalse(_plugin._user_requests_context_images("新衣服好看吗"))
        self.assertTrue(_plugin._user_might_send_following_image("新衣服好看吗"))
        self.assertFalse(_plugin._user_might_send_following_image("今天几点下班"))


class AsyncImageContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _plugin._recent_conversation_messages.clear()
        _plugin._recent_conversation_seq.clear()

    async def test_wait_only_ends_early_for_same_sender_in_same_group(self):
        scope = "group:10001"
        _plugin._remember_recent_conversation_event(_group_event(user_id=100, message_id=1, text="看新衣服"))
        seq = _plugin._recent_conversation_messages[scope][-1].seq
        waiter = asyncio.create_task(_plugin._wait_for_following_sender_image(scope, "100", seq, timeout_seconds=0.5))
        await asyncio.sleep(0)
        _plugin._remember_recent_conversation_event(_group_event(user_id=200, message_id=2, image_url="https://img/other"))
        _plugin._remember_recent_conversation_event(_group_event(user_id=100, message_id=3, image_url="https://img/wrong-group", group_id=555))
        await asyncio.sleep(0.02)
        self.assertFalse(waiter.done())
        _plugin._remember_recent_conversation_event(_group_event(user_id=100, message_id=4, image_url="https://img/right"))
        await waiter

    async def test_single_failed_image_does_not_drop_other_labeled_parts(self):
        async def prepare(_config, urls):
            if urls[0].endswith("bad"):
                raise ValueError("unavailable")
            return [{"type": "image_url", "image_url": {"url": urls[0]}}]
        with patch.object(_plugin, "prepare_passthrough_image_parts", side_effect=prepare):
            parts, count = await _plugin._prepare_labeled_image_parts([
                ("https://img/bad", "甲 QQ 100"), ("https://img/good", "乙 QQ 200"),
            ])
        self.assertEqual(count, 1)
        self.assertIn("甲 QQ 100；图片未能读取", parts[0]["text"])
        self.assertEqual(parts[1], {"type": "text", "text": "乙 QQ 200"})
        self.assertEqual(parts[2]["image_url"]["url"], "https://img/good")


class UserContentTests(unittest.TestCase):
    def test_unreadable_image_does_not_imply_seen_content(self) -> None:
        content = _plugin._build_user_content(
            _incoming(image_urls=["https://img/missing"]),
            image_parts=[{"type": "text", "text": "甲 QQ 100；图片未能读取，内容未知"}],
        )
        self.assertIn("无法确认图片内容", content[0]["text"])
        self.assertIn("内容未知", content[1]["text"])

    def test_context_image_parts_are_appended_after_quoted_images(self) -> None:
        content = _plugin._build_user_content(
            _incoming(),
            quoted_image_parts=[{"type": "image_url", "image_url": {"url": "https://img/quoted.jpg"}}],
            context_image_parts=[
                {"type": "text", "text": "【前文图1】3 条消息前 我会自己拉粑粑 发的图"},
                {"type": "image_url", "image_url": {"url": "https://img/old.jpg"}},
            ],
        )

        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(
            [block["type"] for block in content], ["text", "image_url", "text", "image_url"]
        )
        self.assertIn("前文图1", content[2]["text"])

    def test_context_image_notes_are_inlined_into_text(self) -> None:
        content = _plugin._build_user_content(
            _incoming(),
            context_image_notes=["- 前文图1（3 条消息前，我会自己拉粑粑）: 一张机械装置截图"],
        )

        self.assertIsInstance(content, str)
        self.assertIn("最近几条消息里的图片来源参考", content)
        self.assertNotIn("不要说没看到图", content)
        self.assertIn("机械装置截图", content)


class VisionContextConfigTests(unittest.TestCase):
    def test_config_defaults_cover_16_messages(self) -> None:
        config_module = sys.modules[f"{_PACKAGE_NAME}.config"]

        self.assertEqual(
            config_module.Config.model_fields["catty_vision_context_messages"].default, 16
        )
        self.assertEqual(
            config_module.Config.model_fields["catty_vision_context_max_images"].default, 4
        )
        self.assertEqual(
            config_module.Config.model_fields["catty_vision_context_max_age_seconds"].default,
            1800.0,
        )


class VisualConversationIntegrationTests(unittest.TestCase):
    def test_real_builder_enables_visual_guidance_for_each_input_route(self):
        script = textwrap.dedent('''
            import asyncio, os, sys
            root = sys.argv[1]
            sys.path[:0] = [root, os.path.join(root, 'src')]
            sys.modules['chromadb'] = None
            import nonebot
            nonebot.init(log_level='ERROR')
            assert nonebot.load_plugin('catty_qq_ai') is not None
            import catty_qq_ai as app
            from catty_qq_ai.personas import get_persona
            from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent
            from nonebot.adapters.onebot.v11.event import Sender

            class Rag:
                enabled = False
                def add(self, *args, **kwargs): pass
            app.catty_rag_store = Rag()
            app._build_cognitive_turn_context_sync = lambda **kwargs: ('', '')
            image = MessageSegment(type='image', data={'url': 'https://example.test/image'})
            part = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
            cases = [
                ('direct image', Message([image]), {'image_parts': part}, True, True),
                ('image description', Message([image]), {'image_description': '合成截图'}, True, True),
                ('quoted image', Message('看看引用'), {'quoted_image_parts': part}, True, True),
                ('context image', Message('看看前文'), {'context_image_parts': part}, True, True),
                ('web image', Message('查找资料'), {'web_image_parts': part}, True, True),
                ('native face', Message([MessageSegment(type='face', data={'id': '182'})]), {}, True, False),
                ('unicode emoji', Message('好好笑😂'), {}, True, False),
                ('plain text', Message('今天过得怎么样'), {}, False, False),
            ]

            async def check():
                for role in ('catty', 'fadianji'):
                    token = app.bind_turn_persona('private:7', get_persona(role))
                    try:
                        for number, (label, msg, kwargs, has_visual, has_image) in enumerate(cases, 1):
                            event = PrivateMessageEvent(
                                time=0, self_id=99, post_type='message', sub_type='friend', user_id=7,
                                message_type='private', message_id=number, message=msg, original_message=msg,
                                raw_message=str(msg), font=0, sender=Sender(user_id=7, nickname='user'),
                            )
                            incoming = app.extract_incoming_message('99', event, app.config)
                            assert incoming is not None, (role, label, 'not admitted')
                            messages, _ = await app._build_messages(event, 'private:7', incoming, **kwargs)
                            system = '\\n'.join(
                                item['content'] for item in messages
                                if item.get('role') == 'system' and isinstance(item.get('content'), str)
                            )
                            assert ('【图片与表情的接话】' in system) == has_visual, (role, label)
                            assert ('vision 给的图像描述' in system) == has_image, (role, label)
                            assert '【笨猫·看图反应' not in system, (role, label)
                            if label == 'native face':
                                user = next(item['content'] for item in messages if item.get('role') == 'user')
                                assert 'QQ表情：笑哭' in str(user), (role, label, user)
                                assert 'CQ:face' not in str(user), (role, label, user)
                    finally:
                        app.reset_turn_persona(token)
                print('VISUAL_CONVERSATION_INTEGRATION_OK')
            asyncio.run(check())
        ''')
        allowed_env = (
            "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
            "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA",
        )
        env = {key: os.environ[key] for key in allowed_env if key in os.environ}
        env["PYTHONUTF8"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-c", script, str(_ROOT)],
                cwd=directory, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VISUAL_CONVERSATION_INTEGRATION_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
