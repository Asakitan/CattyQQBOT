from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import importlib.util
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import patch

import nonebot


try:
    nonebot.get_driver()
except Exception:
    nonebot.init()


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_persona_reply_fallbacks_plugin"
_PACKAGE_PATH = _ROOT / "src" / "catty_qq_ai"
_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    _PACKAGE_PATH / "__init__.py",
    submodule_search_locations=[str(_PACKAGE_PATH)],
)
assert _SPEC is not None and _SPEC.loader is not None
_plugin = importlib.util.module_from_spec(_SPEC)
sys.modules[_PACKAGE_NAME] = _plugin
_SPEC.loader.exec_module(_plugin)
_personas = importlib.import_module(f"{_PACKAGE_NAME}.personas")


@dataclass
class _Event:
    user_id: str


@dataclass
class _Incoming:
    has_image: bool = False
    replied_to_self: bool = False
    mentioned: bool = False
    text: str = ""


@contextmanager
def _persona_event(persona, user_id: str):
    event = _Event(user_id)
    with (
        patch.object(_plugin, "config", types.SimpleNamespace(catty_owner_qq="10001")),
        patch.object(_plugin, "_persona_for_event", return_value=persona),
    ):
        yield event


class PersonaReplyFallbackTests(unittest.TestCase):
    def test_catty_owner_and_guest_addresses(self) -> None:
        catty = _personas.get_persona("catty")

        with _persona_event(catty, "10001") as event:
            context = _plugin._persona_reply_context_for_event(event)
            self.assertTrue(context.owner_address_allowed)
            self.assertEqual(_plugin._addr_user(event), "主人")
            self.assertEqual(
                _plugin._fallback_required_reply(event, _Incoming()),
                context.render(context.catalog.no_reply_default_fallback),
            )

        with _persona_event(catty, "20002") as event:
            context = _plugin._persona_reply_context_for_event(event)
            self.assertFalse(context.owner_address_allowed)
            self.assertEqual(_plugin._addr_user(event), "你")
            self.assertEqual(
                _plugin._fallback_required_reply(event, _Incoming()),
                context.render(context.catalog.no_reply_default_fallback),
            )

    def test_fadianji_real_owner_stays_neutral(self) -> None:
        fadianji = _personas.get_persona("fadianji")

        with _persona_event(fadianji, "10001") as event:
            context = _plugin._persona_reply_context_for_event(event)
            self.assertTrue(context.is_owner)
            self.assertFalse(context.owner_address_allowed)
            self.assertEqual(_plugin._addr_user(event), "你")
            self.assertEqual(context.render("{char}/{owner_address}"), "机机/你")

    def test_no_reply_four_branches_use_fadianji_catalog(self) -> None:
        fadianji = _personas.get_persona("fadianji")
        cases = (
            (_Incoming(has_image=True), "no_reply_image_fallback"),
            (_Incoming(replied_to_self=True), "no_reply_reply_fallback"),
            (_Incoming(mentioned=True), "no_reply_mention_fallback"),
            (_Incoming(), "no_reply_default_fallback"),
        )

        with _persona_event(fadianji, "10001") as event:
            context = _plugin._persona_reply_context_for_event(event)
            for incoming, catalog_field in cases:
                with self.subTest(catalog_field=catalog_field):
                    expected = context.render(getattr(context.catalog, catalog_field))
                    self.assertEqual(
                        _plugin._fallback_required_reply(event, incoming),
                        expected,
                    )

            forced = _plugin._force_reply_messages(
                event,
                [{"role": "user", "content": "在吗"}],
                {},
            )
            self.assertEqual(
                forced[-1]["content"],
                context.render(context.catalog.force_reply_instruction),
            )

    def test_fadianji_finalizer_falls_back_after_catty_tic_strip(self) -> None:
        fadianji = _personas.get_persona("fadianji")

        with _persona_event(fadianji, "20002") as event:
            context = _plugin._persona_reply_context_for_event(event)
            self.assertEqual(
                _plugin._finalize_fadianji_reply(event, "喵，喵呜"),
                context.render(context.catalog.no_reply_reply_fallback),
            )
            self.assertEqual(
                _plugin._finalize_fadianji_reply(event, "（小声）喵，别闹喵"),
                "别闹",
            )

    def test_placeholder_pool_keeps_catty_full_pool_and_owner_gate(self) -> None:
        catty = _personas.get_persona("catty")
        fadianji = _personas.get_persona("fadianji")
        catty_catalog = _personas.get_reply_catalog(catty)

        self.assertEqual(len(catty_catalog.slow_reply_placeholders), 22)
        self.assertEqual(len(catty_catalog.slow_reply_owner_placeholders), 6)
        self.assertIn("等等~~人家在翻记忆库喵 ฅฅ", catty_catalog.slow_reply_placeholders)
        self.assertIn("奴马上把答案端到主人面前嗷呜~", catty_catalog.slow_reply_owner_placeholders)

        with _persona_event(catty, "10001") as event:
            context = _plugin._persona_reply_context_for_event(event)
            self.assertEqual(
                context.placeholder_pool,
                catty_catalog.slow_reply_placeholders
                + catty_catalog.slow_reply_owner_placeholders,
            )

        with _persona_event(catty, "20002") as event:
            context = _plugin._persona_reply_context_for_event(event)
            self.assertEqual(context.placeholder_pool, catty_catalog.slow_reply_placeholders)

        with _persona_event(fadianji, "10001") as event:
            context = _plugin._persona_reply_context_for_event(event)
            self.assertEqual(context.placeholder_pool, context.catalog.slow_reply_placeholders)
            self.assertEqual(context.catalog.slow_reply_owner_placeholders, ())

    def test_timeout_transport_and_image_failure_render_by_persona(self) -> None:
        catty = _personas.get_persona("catty")
        fadianji = _personas.get_persona("fadianji")

        with _persona_event(catty, "20002") as event:
            context = _plugin._persona_reply_context_for_event(event)
            rendered = (
                context.render(context.catalog.api_timeout_reply),
                context.render(context.catalog.api_transport_reply),
                context.render(context.catalog.image_send_failure_reply),
            )
            self.assertTrue(all("你" in line for line in rendered))
            self.assertTrue(all("主人" not in line for line in rendered))

        with _persona_event(fadianji, "10001") as event:
            context = _plugin._persona_reply_context_for_event(event)
            self.assertEqual(context.render(context.catalog.api_timeout_reply), "刚才卡住了。再发一次。")
            self.assertEqual(context.render(context.catalog.api_transport_reply), "网线抽了。再来。")
            self.assertEqual(context.render(context.catalog.image_send_failure_reply), "图没发出去。等会再试。")

        self.assertEqual(
            _personas.NEUTRAL_REPLY_CATALOG.image_send_failure_reply,
            "图片发送失败了，请稍后再试。",
        )

    def test_fadianji_catalog_has_no_catty_leakage(self) -> None:
        catalog = _personas.FADIANJI_REPLY_CATALOG
        texts = (
            *catalog.slow_reply_placeholders,
            *catalog.slow_reply_owner_placeholders,
            catalog.force_reply_instruction,
            *catalog.no_reply_fallbacks,
            catalog.api_timeout_reply,
            catalog.api_transport_reply,
            catalog.image_send_failure_reply,
            catalog.tool_result_follow_up_instruction,
            catalog.turtle_soup_cooldown_reply,
            catalog.turtle_soup_rule_line,
            catalog.api_key_missing_reply,
            catalog.web_search_cooldown_reply,
            catalog.web_search_failure_instruction,
            catalog.web_search_disabled_instruction,
            catalog.busy_fallback_reply,
        )
        blob = "\n".join(texts)
        for forbidden in ("主人", "喵", "笨猫", "猫猫", "~", "～", "〜", "〰"):
            self.assertNotIn(forbidden, blob)
        self.assertIsNone(re.search(r"[\U0001F300-\U0001FAFF]", blob))

    def test_recent_image_reference_hint_is_persona_aware(self) -> None:
        catty = _personas.get_persona("catty")
        fadianji = _personas.get_persona("fadianji")
        incoming = _Incoming(text="刚才那张图是什么")
        recent_images = [{
            "time": "2026-08-03T12:34:56.000000",
            "display_name": "测试用户",
            "text": "图片描述",
        }]

        with patch.object(
            _plugin.memory_store,
            "collect_recent_image_descriptions",
            return_value=recent_images,
        ):
            with _persona_event(catty, "10001") as event:
                catty_hint = _plugin._build_recent_image_reference_hint(
                    event,
                    incoming,
                    persona=catty,
                )
                none_hint = _plugin._build_recent_image_reference_hint(event, incoming)
            with _persona_event(fadianji, "10001") as event:
                fadianji_hint = _plugin._build_recent_image_reference_hint(
                    event,
                    incoming,
                    persona=fadianji,
                )

        expected_catty_hint = (
            "用户这一句明显在回指**之前出现过的图片**(『那张/刚才那张/认得这张』之类)。"
            "下面给出当前会话最近的图片记忆(corpus 里 has_image 条目,时间倒序),"
            "请你**优先在这几条里找用户指代的那张**,认出来就基于 vision 描述回答;"
            "如果都对不上,可以用一句猫娘口吻说『人家也没看清这张是啥喵～你形容下』,"
            "不要硬猜。\n"
            "1. [2026-08-03T12:34:56] 测试用户（QQ 未知）: 图片描述\n"
            "如果需要更早的图(超出本会话最近 3 张),再考虑调 catty_recall(keywords=『图片内容 关键词』)查 corpus。"
        )
        self.assertEqual(catty_hint.encode("utf-8"), expected_catty_hint.encode("utf-8"))
        self.assertEqual(none_hint.encode("utf-8"), expected_catty_hint.encode("utf-8"))
        self.assertIn("保持当前人格既有的表达方式", fadianji_hint)
        self.assertNotIn("catty_recall", fadianji_hint)
        for forbidden in ("主人", "喵", "笨猫", "猫猫", "猫娘", "~", "～", "〜", "〰"):
            self.assertNotIn(forbidden, fadianji_hint)
        self.assertIsNone(re.search(r"[\U0001F300-\U0001FAFF]", fadianji_hint))

    def test_local_critic_rewrite_prompt_is_persona_aware(self) -> None:
        catty = _personas.get_persona("catty")
        fadianji = _personas.get_persona("fadianji")
        messages = [{"role": "system", "content": "base"}]
        critic_result = {"rewrite_hint": "保留原意"}

        with _persona_event(catty, "10001") as event:
            catty_messages = _plugin._local_critic_rewrite_messages(
                event,
                messages,
                "草稿回复",
                critic_result,
            )
        with _persona_event(fadianji, "10001") as event:
            fadianji_messages = _plugin._local_critic_rewrite_messages(
                event,
                messages,
                "草稿回复",
                critic_result,
            )

        expected_catty_prompt = (
            "刚才那条回复需要重写一下: 保持原意和事实, 用笨猫 QQ 聊天口吻 "
            "(短句、自然、可爱但有用)。\n"
            f"如果确实不该回复就输出 {_plugin.NO_REPLY_MARKER}。\n"
            "重写方向: 保留原意"
        )
        catty_prompt = catty_messages[-1]["content"]
        fadianji_prompt = fadianji_messages[-1]["content"]
        self.assertEqual(catty_prompt.encode("utf-8"), expected_catty_prompt.encode("utf-8"))
        self.assertIn("使用当前人格既有的聊天口吻", fadianji_prompt)
        for forbidden in ("主人", "喵", "笨猫", "猫猫", "猫娘", "~", "～", "〜", "〰"):
            self.assertNotIn(forbidden, fadianji_prompt)
        self.assertIsNone(re.search(r"[\U0001F300-\U0001FAFF]", fadianji_prompt))

    def test_business_early_fallbacks_follow_fadianji_catalog(self) -> None:
        fadianji = _personas.get_persona("fadianji")

        with _persona_event(fadianji, "10001") as event:
            context = _plugin._persona_reply_context_for_event(event)
            cooldown = _plugin._persona_search_cooldown_message(event, 90.0)
            soup = _plugin.choose_turtle_soup(
                "persona-test",
                persona_rule_line=context.catalog.turtle_soup_rule_line,
            )

        self.assertEqual(cooldown, "刚搜过。还剩 2分钟，冷却完再搜。")
        self.assertIn(context.catalog.turtle_soup_rule_line, soup)
        for text in (
            cooldown,
            soup,
            context.catalog.api_key_missing_reply,
            context.catalog.web_search_disabled_instruction,
            context.catalog.busy_fallback_reply,
        ):
            for forbidden in ("主人", "喵", "笨猫", "猫猫", "猫娘"):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
