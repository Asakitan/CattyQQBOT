import importlib.util
from pathlib import Path
import unittest


_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_features = _load_module("features", "src/catty_qq_ai/features.py")
_star_memory = _load_module("star_resonance_memory", "src/catty_qq_ai/star_resonance_memory.py")
_strinova_memory = _load_module("strinova_memory", "src/catty_qq_ai/strinova_memory.py")
extract_web_search_query = _features.extract_web_search_query
format_duration_cn = _features.format_duration_cn
is_turtle_soup_request = _features.is_turtle_soup_request
turtle_soup_remaining = _features.turtle_soup_remaining
build_star_resonance_context = _star_memory.build_star_resonance_context
is_star_resonance_related = _star_memory.is_star_resonance_related
build_strinova_context = _strinova_memory.build_strinova_context
is_strinova_related = _strinova_memory.is_strinova_related


class FeatureTests(unittest.TestCase):
    def test_extract_web_search_query(self) -> None:
        self.assertEqual(extract_web_search_query("联网搜索 星痕共鸣 职业"), "星痕共鸣 职业")
        self.assertEqual(extract_web_search_query("帮我查一下蓝色协议"), "蓝色协议")
        self.assertEqual(extract_web_search_query("猫猫帮我搜一点色图"), "色图")
        self.assertEqual(extract_web_search_query("帮我搜点猫图"), "猫图")
        self.assertEqual(extract_web_search_query("普通聊天"), "")

    def test_turtle_soup_request_and_cooldown(self) -> None:
        self.assertTrue(is_turtle_soup_request("来个海龟汤"))
        self.assertFalse(is_turtle_soup_request("来个普通谜语"))
        self.assertEqual(turtle_soup_remaining({"group:1": 100.0}, "group:1", now=150.0, cooldown_seconds=300), 250.0)
        self.assertEqual(turtle_soup_remaining({"group:1": 100.0}, "group:1", now=450.0, cooldown_seconds=300), 0.0)

    def test_format_duration_cn(self) -> None:
        self.assertEqual(format_duration_cn(61), "2分钟")
        self.assertEqual(format_duration_cn(3600), "1小时")

    def test_star_resonance_context(self) -> None:
        self.assertTrue(is_star_resonance_related("星痕共鸣职业怎么选"))
        context = build_star_resonance_context("Blue Protocol Star Resonance")
        self.assertIn("本地记忆", context)
        self.assertIn("Regnas", context)
        group_context = build_star_resonance_context(
            "今晚有人带副本吗",
            group_id=477970838,
            group_ids={477970838, 578305908},
        )
        self.assertIn("主题群", group_context)
        self.assertIn("职业、副本、装备", group_context)
        self.assertEqual(build_star_resonance_context("普通聊天"), "")

    def test_strinova_context(self) -> None:
        self.assertTrue(is_strinova_related("卡拉彼丘弦化怎么练"))
        self.assertTrue(is_strinova_related("Strinova Superstrings"))
        context = build_strinova_context("米雪儿是哪个阵营")
        self.assertIn("弦化", context)
        self.assertIn("Superstrings", context)
        self.assertIn("欧泊阵营", context)
        group_context = build_strinova_context("今天爆破排位吗", group_id="100", group_ids={100})
        self.assertIn("卡拉彼丘", group_context)
        self.assertEqual(build_strinova_context("普通聊天"), "")


if __name__ == "__main__":
    unittest.main()
