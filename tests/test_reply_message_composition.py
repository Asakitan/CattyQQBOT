from pathlib import Path
import importlib.util
import sys
import tempfile
import types
import unittest

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
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


def _group_event(message_id: int = 123) -> GroupMessageEvent:
    message = Message("猫猫看这个")
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
        raw_message="猫猫看这个",
        font=0,
        sender=Sender(user_id=20002, nickname="群友", card="群友"),
        group_id=10001,
    )


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
        _plugin.config.catty_reply_quote_enabled = True
        _plugin.config.catty_reply_quote_private_enabled = False
        _plugin.config.catty_reply_mix_emoji_with_text = True

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
        _plugin._recent_emoji_paths.clear()

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


if __name__ == "__main__":
    unittest.main()
