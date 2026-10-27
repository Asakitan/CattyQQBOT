from pathlib import Path
import importlib.util
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_owner_forward_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)


def _load_module(name: str):
    path = _ROOT / "src" / "catty_qq_ai" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_config = _load_module("config")
_owner_forward = _load_module("owner_forward")

Config = _config.Config


def _private_event(sub_type: str) -> PrivateMessageEvent:
    message = Message("hello")
    return PrivateMessageEvent(
        time=0,
        self_id=999,
        post_type="message",
        sub_type=sub_type,
        user_id=123456,
        message_type="private",
        message_id=1,
        message=message,
        original_message=message,
        raw_message="hello",
        font=0,
        sender=Sender(user_id=123456, nickname="私聊用户"),
    )


class OwnerForwardTests(unittest.TestCase):
    def setUp(self) -> None:
        _owner_forward.init(
            Config(
                catty_owner_forward_enabled=True,
                catty_owner_qq=993255714,
                catty_owner_forward_private_messages=True,
                catty_owner_forward_block_ai_reply=True,
            )
        )

    def test_friend_private_message_is_not_forwarded(self) -> None:
        self.assertFalse(_owner_forward._should_forward_private_message(_private_event("friend")))

    def test_non_friend_private_messages_are_forwarded(self) -> None:
        for sub_type in ("group", "other", ""):
            with self.subTest(sub_type=sub_type):
                self.assertTrue(_owner_forward._should_forward_private_message(_private_event(sub_type)))

    def test_private_forwarding_flag_disables_forwarding(self) -> None:
        _owner_forward.init(
            Config(
                catty_owner_forward_enabled=True,
                catty_owner_qq=993255714,
                catty_owner_forward_private_messages=False,
            )
        )

        self.assertFalse(_owner_forward._should_forward_private_message(_private_event("other")))

    def test_stranger_hints_are_neutral_friend_requests(self) -> None:
        for template in _owner_forward._STRANGER_HINT_TEMPLATES:
            with self.subTest(template=template):
                self.assertNotIn("主人", template)
                self.assertIn("加好友", template)
                self.assertIn("临时会话", template)


class PaidPersonaForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_paid_window_uses_selected_persona_and_preserves_catty_key(self):
        for name, expected in (("catty", "private:123:123"),
                               ("fadianji", "private:123#nsfw=fadianji:123"),
                               ("atlas", "private:123#nsfw=atlas:123")):
            with self.subTest(persona=name):
                persona = types.SimpleNamespace(name=name, definition_revision=None,
                    nsfw_allowed=lambda **kwargs: True)
                scorer = types.ModuleType(f"{_PACKAGE_NAME}.affection_scorer")
                scorer.pick_paid_nsfw_scene = Mock(return_value=("trope", "scene"))
                scorer.open_paid_sticky = Mock()
                scorer.PAID_NSFW_COST = 100
                scorer.BREAKTHROUGH_PREFILLS = {"paid": ""}
                store = types.SimpleNamespace(get_points=Mock(return_value=200),
                    consume_points=Mock(return_value={"ok": True, "balance_before": 200, "balance_after": 100}))
                bot = types.SimpleNamespace(set_friend_add_request=AsyncMock(), send_private_msg=AsyncMock())
                scope_for = lambda scope, p: scope if p.name == "catty" else f"{scope}#nsfw={p.name}"
                with patch.dict(sys.modules, {scorer.__name__: scorer}), \
                     patch.object(_PACKAGE, "affection_store", store, create=True), \
                     patch.object(_PACKAGE, "_persona_for_scope", return_value=persona, create=True), \
                     patch.object(_PACKAGE, "_phase_scope_for", side_effect=scope_for, create=True), \
                     patch.object(_owner_forward, "_send_to_owner", new_callable=AsyncMock):
                    await _owner_forward._try_paid_friend_accept(bot, 123, "flag", source="friend_request")
                self.assertEqual(scorer.open_paid_sticky.call_args.args[0], expected)
                store.consume_points.assert_called_once_with("123", 100)
                sent = bot.send_private_msg.await_args.kwargs["message"]
                if name == "catty":
                    self.assertIn("笨猫", sent)
                else:
                    self.assertNotIn("笨猫", sent)
                    self.assertNotIn("喵", sent)

    async def test_disabled_or_changed_persona_never_charges(self):
        allowed = types.SimpleNamespace(name="fadianji", nsfw_allowed=lambda **kwargs: True)
        blocked = types.SimpleNamespace(name="atlas", nsfw_allowed=lambda **kwargs: False)
        scorer = types.ModuleType(f"{_PACKAGE_NAME}.affection_scorer")
        scorer.pick_paid_nsfw_scene = Mock(return_value=("trope", "scene"))
        scorer.open_paid_sticky = Mock()
        scorer.PAID_NSFW_COST = 100
        scorer.BREAKTHROUGH_PREFILLS = {"paid": ""}
        for personae in ((blocked,), (allowed, blocked)):
            with self.subTest(personae=len(personae)):
                store = types.SimpleNamespace(get_points=Mock(return_value=200), consume_points=Mock())
                bot = types.SimpleNamespace(set_friend_add_request=AsyncMock(), send_private_msg=AsyncMock())
                resolver = Mock(side_effect=personae)
                with patch.dict(sys.modules, {scorer.__name__: scorer}), \
                     patch.object(_PACKAGE, "affection_store", store, create=True), \
                     patch.object(_PACKAGE, "_persona_for_scope", resolver, create=True), \
                     patch.object(_PACKAGE, "_phase_scope_for", create=True), \
                     patch.object(_owner_forward, "_send_to_owner", new_callable=AsyncMock):
                    await _owner_forward._try_paid_friend_accept(bot, 123, "flag", source="friend_request")
                store.consume_points.assert_not_called()
                scorer.open_paid_sticky.assert_not_called()


if __name__ == "__main__":
    unittest.main()
