from pathlib import Path
import importlib.util
import sys
import tempfile
import types
import unittest

from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_memory_test"
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
_load_module("reply_markers")
_memory = _load_module("memory")

Config = _config.Config
MemoryStore = _memory.MemoryStore


def _sender(name: str) -> Sender:
    return Sender(user_id=0, nickname=name, card=name)


def _group_event(group_id: int, user_id: int, name: str = "群友") -> GroupMessageEvent:
    message = Message("hello")
    return GroupMessageEvent(
        time=0,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=1,
        message=message,
        original_message=message,
        raw_message="hello",
        font=0,
        sender=_sender(name),
        group_id=group_id,
    )


def _private_event(user_id: int, name: str = "私聊用户") -> PrivateMessageEvent:
    message = Message("hello")
    return PrivateMessageEvent(
        time=0,
        self_id=999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=1,
        message=message,
        original_message=message,
        raw_message="hello",
        font=0,
        sender=_sender(name),
    )


def _store(directory: str) -> MemoryStore:
    return MemoryStore(
        Config(
            catty_memory_enabled=True,
            catty_memory_path=str(Path(directory) / "memory.json"),
            catty_memory_group_storage_dir=str(Path(directory) / "groups"),
            catty_memory_user_storage_dir=str(Path(directory) / "users"),
            catty_memory_max_known_members=20,
        )
    )


class MemoryIsolationTests(unittest.TestCase):
    def test_group_observation_records_same_user_identity_without_private_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)

            store.remember_event(_group_event(10001, 20002, "群名片"))

            self.assertEqual(store._data["users"]["20002"]["display_name"], "群名片")
            self.assertNotIn("private_summary", store._data["users"]["20002"])
            self.assertNotIn("private_profile", store._data["users"]["20002"])
            self.assertEqual(store._data["groups"]["10001"]["members"]["20002"]["display_name"], "群名片")

    def test_group_reply_context_can_use_same_user_memory_but_not_other_group_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            store.save_private_summary(
                "20002",
                '{"summary":"私聊秘密摘要","profile":{"gender":"女","title":"私聊称呼","impression":"私聊秘密印象","confidence":"高"}}',
            )
            store.save_group_summary(
                "30003",
                '{"summary":"三群摘要","members":[{"user_id":"20002","display_name":"同一人","gender":"男","title":"三群称呼","impression":"三群印象","confidence":"高"}]}',
            )
            store.remember_event(_group_event(10001, 20002, "一群名片"))

            context = store.build_context(_group_event(10001, 20002, "一群名片"))
            proactive = store.build_proactive_context("10001", recent_limit=5)

            self.assertIn("私聊称呼", context)
            self.assertIn("私聊秘密摘要", context)
            self.assertIn("私聊秘密印象", context)
            for leaked in ("三群称呼", "三群印象"):
                self.assertNotIn(leaked, context)
            for leaked in ("私聊称呼", "私聊秘密摘要", "私聊秘密印象", "三群称呼", "三群印象"):
                self.assertNotIn(leaked, proactive)
            self.assertIn("当前群：10001", context)
            self.assertIn("一群名片(20002)=>群友/", context)

    def test_proactive_context_uses_only_the_selected_group_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            store.save_group_summary(
                "10001",
                '{"summary":"一群只聊游戏","members":[{"user_id":"20002","display_name":"同一人","gender":"未知","title":"一群称呼","impression":"一群印象","confidence":"高"}]}',
            )
            store.save_group_summary(
                "30003",
                '{"summary":"三群只聊代码","members":[{"user_id":"20002","display_name":"同一人","gender":"未知","title":"三群称呼","impression":"三群印象","confidence":"高"}]}',
            )

            group_one = store.build_proactive_context("10001", recent_limit=5)
            group_three = store.build_proactive_context("30003", recent_limit=5)

            self.assertIn("一群只聊游戏", group_one)
            self.assertIn("一群称呼/一群印象", group_one)
            self.assertNotIn("三群称呼", group_one)
            self.assertNotIn("三群印象", group_one)
            self.assertIn("三群只聊代码", group_three)
            self.assertIn("三群称呼/三群印象", group_three)
            self.assertNotIn("一群称呼", group_three)
            self.assertNotIn("一群印象", group_three)

    def test_proactive_due_skips_groups_without_group_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)

            self.assertEqual(
                store.due_proactive_group_ids(["10001"], max_daily=5, min_interval_minutes=1),
                [],
            )
            self.assertNotIn("10001", store._data["groups"])

            store.remember_event(_group_event(10001, 20002, "一群名片"))

            self.assertEqual(
                store.due_proactive_group_ids(["10001"], max_daily=5, min_interval_minutes=1),
                ["10001"],
            )

    def test_private_context_does_not_use_group_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            store.save_group_summary(
                "10001",
                '{"summary":"群摘要","members":[{"user_id":"20002","display_name":"群名片","gender":"男","title":"群内称呼","impression":"群内印象","confidence":"高"}]}',
            )
            store.remember_event(_private_event(20002, "私聊昵称"))

            context = store.build_context(_private_event(20002, "私聊昵称"))

            self.assertIn("当前是私聊", context)
            self.assertNotIn("群内称呼", context)
            self.assertNotIn("群内印象", context)


if __name__ == "__main__":
    unittest.main()
