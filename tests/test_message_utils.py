import importlib.util
from pathlib import Path
import sys
import types
import unittest


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
        self.assertTrue(_message_utils._has_directed_keyword("来个海龟汤", self.config))

    def test_second_person_mentions_are_sent_to_ai_for_subject_judgment(self) -> None:
        self.assertTrue(_message_utils._has_directed_keyword("你觉得呢", self.config))
        self.assertTrue(_message_utils._has_directed_keyword("他说你昨天很忙", self.config))

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


if __name__ == "__main__":
    unittest.main()
