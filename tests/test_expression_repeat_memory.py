from pathlib import Path
import importlib.util
import sys
import types
import unittest

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_plugin_repeat_test"


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


def _group_event(user_id: int = 20002, message_id: int = 1) -> GroupMessageEvent:
    message = Message("复读")
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
        raw_message="复读",
        font=0,
        sender=Sender(user_id=user_id, nickname="群友", card="群友"),
        group_id=10001,
    )


class ExpressionRepeatMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        _plugin._recent_conversation_messages.clear()
        _plugin._bot_reply_continuations.clear()

    def test_repeat_memory_does_not_target_the_repeated_user(self) -> None:
        event = _group_event()

        _plugin._remember_bot_repeat_for_event(event, "复读")

        key = _plugin._conversation_queue_key(event)
        self.assertEqual(len(_plugin._recent_conversation_messages[key]), 1)
        remembered = _plugin._recent_conversation_messages[key][0]
        self.assertTrue(remembered.is_bot)
        self.assertEqual(remembered.target_user_id, "")
        self.assertFalse(_plugin._has_bot_reply_continuation(event))

    def test_normal_bot_reply_still_targets_the_replied_user(self) -> None:
        event = _group_event()

        _plugin._remember_bot_reply_for_event(event, "正常回复")

        key = _plugin._conversation_queue_key(event)
        remembered = _plugin._recent_conversation_messages[key][0]
        self.assertEqual(remembered.target_user_id, "20002")
        self.assertTrue(_plugin._has_bot_reply_continuation(event))


if __name__ == "__main__":
    unittest.main()
