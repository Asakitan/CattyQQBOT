"""Phase 2 prompt_compressor 单元测试.

覆盖:
- detect_anaphora 各种 query 形态
- compress_history fallback chain (cosine → recency → keep_recent)
- select_user_details 容错降级
- select_summary_paragraphs 段落选取
- dynamic_tool_picker 关键词触发
- get_protected_identifiers 不空 (生产环境)

不依赖 NLU 实模型 (text2vec/jieba/HanLP 不可用时也能跑).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest.mock import patch


def _load_compressor():
    """独立加载模块, 跳过 nonebot 初始化."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "src", "catty_qq_ai", "nlu", "prompt_compressor.py")
    spec = importlib.util.spec_from_file_location("_pc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pc = _load_compressor()


class TestDetectAnaphora(unittest.TestCase):
    def test_short_pronoun(self) -> None:
        for q in ["嗯", "嗯嗯", "对", "继续", "?", "好"]:
            self.assertTrue(pc.detect_anaphora(q), f"{q!r} 应判为指代")

    def test_anaphora_pattern(self) -> None:
        for q in ["这个咋办", "那是什么", "刚才说啥", "对吧"]:
            self.assertTrue(pc.detect_anaphora(q), f"{q!r} 应判为指代")

    def test_independent_query(self) -> None:
        for q in ["我想吃饭", "帮我画一张猫娘图", "今天天气怎样"]:
            self.assertFalse(pc.detect_anaphora(q), f"{q!r} 应判为独立 query")

    def test_empty_or_none(self) -> None:
        self.assertFalse(pc.detect_anaphora(""))
        self.assertFalse(pc.detect_anaphora(None))
        self.assertFalse(pc.detect_anaphora("   "))


class TestCompressHistory(unittest.TestCase):
    def _make_hist(self, n: int) -> list[dict]:
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"}
            for i in range(n)
        ]

    def test_short_history_pass_through(self) -> None:
        hist = self._make_hist(3)
        got = pc.compress_history(hist, query=None, target_tokens=1000, keep_recent=2)
        self.assertEqual(got, hist)

    def test_keep_recent_preserved(self) -> None:
        hist = self._make_hist(10)
        got = pc.compress_history(hist, query=None, target_tokens=20, keep_recent=2)
        self.assertEqual(got[-2:], hist[-2:], "最后 2 条必须保留")

    def test_keep_recent_when_budget_zero(self) -> None:
        """budget 已被 keep_recent 占满 → 只返回 keep_recent."""
        hist = self._make_hist(10)
        got = pc.compress_history(hist, query=None, target_tokens=1, keep_recent=2)
        # 至少返回了 keep_recent 末尾 2 条
        self.assertGreaterEqual(len(got), 2)
        self.assertEqual(got[-2:], hist[-2:])

    def test_empty_history(self) -> None:
        self.assertEqual(pc.compress_history([], "any", 100, keep_recent=2), [])


class TestSelectUserDetails(unittest.TestCase):
    def test_short_list_passthrough(self) -> None:
        details = [{"text": "a"}, {"text": "b"}]
        got = pc.select_user_details(details, "any", top_k=5)
        self.assertEqual(got, details)

    def test_no_query_keeps_first_k(self) -> None:
        details = [{"text": f"item{i}"} for i in range(10)]
        got = pc.select_user_details(details, query=None, top_k=3)
        self.assertEqual(got, details[:3])

    def test_empty_input(self) -> None:
        self.assertEqual(pc.select_user_details([], "q", 5), [])

    def test_str_items(self) -> None:
        details = ["纯字符串1", "纯字符串2", "纯字符串3", "纯字符串4"]
        got = pc.select_user_details(details, query=None, top_k=2)
        self.assertEqual(got, details[:2])


class TestSelectSummaryParagraphs(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(pc.select_summary_paragraphs("", "q", 100), "")
        self.assertEqual(pc.select_summary_paragraphs(None, "q", 100), "")

    def test_single_paragraph(self) -> None:
        text = "只有一段没换行"
        # 单段 + 内容很小 ≤ budget → 原样
        got = pc.select_summary_paragraphs(text, query=None, target_tokens=100)
        self.assertEqual(got, text)

    def test_no_query_takes_first(self) -> None:
        text = "段A\n\n段B\n\n段C"
        got = pc.select_summary_paragraphs(text, query=None, target_tokens=1000)
        # budget 大, 应该全留
        self.assertIn("段A", got)


class TestDynamicToolPicker(unittest.TestCase):
    def _tools(self) -> list[dict]:
        return [
            {"name": "catty_imagegen"},
            {"name": "catty_web_search"},
            {"name": "catty_meme_query"},
            {"name": "catty_recall"},
            {"name": "catty_other_uninteresting"},
        ]

    def test_imagegen_trigger(self) -> None:
        got = pc.dynamic_tool_picker(self._tools(), "帮我画一张图")
        names = {t["name"] for t in got}
        self.assertIn("catty_imagegen", names)

    def test_web_search_trigger(self) -> None:
        got = pc.dynamic_tool_picker(self._tools(), "查一下今天新闻")
        names = {t["name"] for t in got}
        self.assertIn("catty_web_search", names)

    def test_no_query_returns_all(self) -> None:
        all_t = self._tools()
        got = pc.dynamic_tool_picker(all_t, "")
        self.assertEqual(got, all_t)
        got2 = pc.dynamic_tool_picker(all_t, None)
        self.assertEqual(got2, all_t)

    def test_no_match_returns_empty(self) -> None:
        got = pc.dynamic_tool_picker(self._tools(), "hello 闲聊")
        self.assertEqual(got, [])

    def test_allowed_force_include(self) -> None:
        """with_tools loop 已发生 tool_calls 的 tool 必须强制保留, 即使本轮 query 没触发."""
        got = pc.dynamic_tool_picker(
            self._tools(),
            "hello 闲聊",  # 不触发任何关键词
            allowed={"catty_recall"},
        )
        names = {t["name"] for t in got}
        self.assertIn("catty_recall", names)


class TestPersonaProtection(unittest.TestCase):
    """plan step 2.5: persona 段任何情况都不进 NLU 通道.

    在测试环境 (无 nonebot init), get_protected_identifiers 返回 frozenset()
    (生产环境会返回 prompt_manager._PROTECTED_IDENTIFIERS).
    所以这里只验证 API 存在 + 不抛异常.
    """
    def test_get_protected_returns_frozenset(self) -> None:
        got = pc.get_protected_identifiers()
        self.assertIsInstance(got, frozenset)


class TestMonotonicHistoryTrim(unittest.TestCase):
    """Phase 3: anchor checkpoint, cache-safe 行为验证."""

    def setUp(self) -> None:
        pc.reset_scope_anchor()  # 清所有 anchor

    def _hist(self, n: int, size: int = 5) -> list[dict]:
        """生成 n 条历史, 每条文本 size 字符 (粗略 token 数)."""
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * size}
            for i in range(n)
        ]

    def test_under_budget_anchor_zero(self) -> None:
        """history 不超 budget → 全返回, anchor 保持 0."""
        hist = self._hist(5, size=4)
        got = pc.monotonic_history_trim(hist, scope_id="s1", target_tokens=100, keep_recent=2)
        self.assertEqual(got, hist)
        self.assertEqual(pc.get_scope_anchor("s1"), 0)

    def test_anchor_stable_across_rounds_under_budget(self) -> None:
        """Cache 关键: 多轮间没超 budget, anchor 不动 → 前缀字节稳定."""
        hist1 = self._hist(5, size=4)
        pc.monotonic_history_trim(hist1, scope_id="s2", target_tokens=200, keep_recent=2)
        anchor_after_r1 = pc.get_scope_anchor("s2")

        # 第 2 轮: append 2 新条
        hist2 = hist1 + self._hist(2, size=4)
        out2 = pc.monotonic_history_trim(hist2, scope_id="s2", target_tokens=200, keep_recent=2)
        anchor_after_r2 = pc.get_scope_anchor("s2")

        self.assertEqual(anchor_after_r1, anchor_after_r2, "未超 budget 时 anchor 必须不动")
        # 前 5 条字节必须 = 第 1 轮的 5 条 (cache prefix 字节稳定)
        self.assertEqual(out2[:5], hist1)

    def test_anchor_jumps_when_over_budget(self) -> None:
        """超 budget → anchor 大跳, 剩 70% budget 留余量."""
        # 30 条 × 10 字符 ≈ 300 字符 → ~150 token 估算
        hist = self._hist(30, size=10)
        # target 20 tokens 远小于 history 总和 → 应该 anchor 跳
        out = pc.monotonic_history_trim(hist, scope_id="s3", target_tokens=20, keep_recent=2)
        anchor = pc.get_scope_anchor("s3")
        self.assertGreater(anchor, 0, "超 budget 应该让 anchor 跳到 >0")
        self.assertEqual(out, hist[anchor:])
        # 跳后再调一次, anchor 不应再变 (没新内容触发再跳)
        out2 = pc.monotonic_history_trim(hist, scope_id="s3", target_tokens=20, keep_recent=2)
        anchor2 = pc.get_scope_anchor("s3")
        self.assertEqual(anchor, anchor2, "同 history 同 budget 再调 anchor 应稳定")

    def test_keep_recent_floor(self) -> None:
        """budget 极小, anchor 不能让末尾 < keep_recent 条."""
        hist = self._hist(20, size=50)
        out = pc.monotonic_history_trim(hist, scope_id="s4", target_tokens=10, keep_recent=4)
        # 末尾必须 >= keep_recent
        self.assertGreaterEqual(len(out), 4, f"应至少保 keep_recent=4 条, 实际 {len(out)}")
        # 末尾 N 条必须是 hist 最后 N 条
        self.assertEqual(out[-4:], hist[-4:])

    def test_empty_history(self) -> None:
        out = pc.monotonic_history_trim([], scope_id="s5", target_tokens=100, keep_recent=2)
        self.assertEqual(out, [])

    def test_reset_anchor(self) -> None:
        hist = self._hist(20, size=20)
        pc.monotonic_history_trim(hist, scope_id="s6", target_tokens=20, keep_recent=2)
        self.assertGreater(pc.get_scope_anchor("s6"), 0)
        pc.reset_scope_anchor("s6")
        self.assertEqual(pc.get_scope_anchor("s6"), 0)

    def test_anchor_overshoot_protection(self) -> None:
        """history 被外部 prune 致使 anchor 超界 → 自动重锚."""
        hist_long = self._hist(30, size=20)
        pc.monotonic_history_trim(hist_long, scope_id="s7", target_tokens=20, keep_recent=2)
        anchor_set = pc.get_scope_anchor("s7")
        self.assertGreater(anchor_set, 0)

        # 模拟 history 被外部削短到 anchor 之前
        hist_short = hist_long[: max(anchor_set - 5, 1)]
        out = pc.monotonic_history_trim(hist_short, scope_id="s7", target_tokens=20, keep_recent=2)
        # 应该不崩, 自动 reset 到合理位置
        self.assertGreater(len(out), 0)

    def test_scope_isolation(self) -> None:
        """不同 scope_id anchor 独立."""
        hist = self._hist(20, size=20)
        pc.monotonic_history_trim(hist, scope_id="sA", target_tokens=20, keep_recent=2)
        pc.monotonic_history_trim(hist, scope_id="sB", target_tokens=10000, keep_recent=2)  # 不超 budget
        self.assertGreater(pc.get_scope_anchor("sA"), 0)
        self.assertEqual(pc.get_scope_anchor("sB"), 0)


class TestSessionHistoryTokenWindow(unittest.TestCase):
    def _history(self) -> list[dict]:
        return [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a111"},
            {"role": "user", "content": "u22"},
            {"role": "assistant", "content": "a22"},
            {"role": "user", "content": "u333"},
            {"role": "assistant", "content": "a333"},
        ]

    def test_count_history_tokens_uses_message_text(self) -> None:
        with patch.object(pc, "count_tokens", side_effect=lambda text: len(text or "")):
            self.assertEqual(pc.count_history_tokens(self._history()), 20)

    def test_appended_tool_definitions_are_counted_without_message_content(self) -> None:
        import json
        definitions = [{"type": "function", "name": "load", "description": "definition text" * 20}]
        message = {"type": "additional_tools", "role": "developer", "tools": definitions}
        expected = json.dumps(definitions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with patch.object(pc, "count_tokens", side_effect=lambda text: len(text or "")):
            self.assertEqual(pc.count_message_tokens(message), len(expected))
            self.assertEqual(pc.count_history_tokens([message, {"role": "user", "content": "TARGET"}]), len(expected) + 6)

    def test_trim_keeps_newest_contiguous_complete_turns(self) -> None:
        history = self._history()
        with patch.object(pc, "count_tokens", side_effect=lambda text: len(text or "")):
            trimmed = pc.trim_history_to_token_budget(history, 15)
        self.assertEqual(trimmed, history[2:])
        self.assertEqual(trimmed[0]["role"], "user")

    def test_trim_keeps_oversized_latest_turn_intact(self) -> None:
        history = self._history()
        with patch.object(pc, "count_tokens", side_effect=lambda text: len(text or "")):
            trimmed = pc.trim_history_to_token_budget(history, 2)
        self.assertEqual(trimmed, history[-2:])

    def test_trim_discards_leading_orphans(self) -> None:
        history = [{"role": "assistant", "content": "orphan"}] + self._history()[-2:]
        with patch.object(pc, "count_tokens", side_effect=lambda text: len(text or "")):
            trimmed = pc.trim_history_to_token_budget(history, 100)
        self.assertEqual(trimmed, history[1:])

    def test_trim_keeps_newest_tool_turn_intact_without_orphans(self) -> None:
        history = [
            {"role": "user", "content": "ou"},
            {"role": "assistant", "content": "oa"},
            {"role": "user", "content": "new"},
            {
                "role": "assistant",
                "content": "call",
                "tool_calls": [{"id": "call-1", "function": {"name": "lookup"}}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]
        newest_turn = history[2:]
        with patch.object(pc, "count_tokens", side_effect=lambda text: len(text or "")):
            trimmed = pc.trim_history_to_token_budget(history, 17)

        self.assertEqual(trimmed, newest_turn)
        self.assertEqual(trimmed[0]["role"], "user")
        self.assertEqual(trimmed[1]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(trimmed[2]["tool_call_id"], "call-1")

    def test_trim_budget_boundary_drops_whole_older_turn(self) -> None:
        history = [
            {"role": "user", "content": "ou"},
            {"role": "assistant", "content": "oa"},
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "call"},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]
        with patch.object(pc, "count_tokens", side_effect=lambda text: len(text or "")):
            exact = pc.trim_history_to_token_budget(history, 21)
            one_under = pc.trim_history_to_token_budget(history, 20)

        self.assertEqual(exact, history)
        self.assertEqual(one_under, history[2:])

    def test_scaled_224k_high_watermark_trims_once_to_192k_tail(self) -> None:
        history: list[dict] = []
        for turn in range(23):
            history.extend([
                {"role": "user", "content": f"{turn:02d}" + "u" * 3},
                {"role": "assistant", "content": f"{turn:02d}" + "a" * 3},
            ])
        with patch.object(pc, "count_tokens", side_effect=lambda text: len(text or "")):
            self.assertEqual(pc.count_history_tokens(history), 230)
            trimmed = pc.trim_history_to_token_budget(history, 192)
            self.assertEqual(pc.count_history_tokens(trimmed), 190)
        self.assertEqual(len(trimmed), 38)
        self.assertEqual(trimmed, history[-38:])
        self.assertEqual(trimmed[0]["role"], "user")


if __name__ == "__main__":
    unittest.main()
