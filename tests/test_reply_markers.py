import importlib.util
from pathlib import Path
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "reply_markers.py"
_SPEC = importlib.util.spec_from_file_location("reply_markers", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_reply_markers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_reply_markers)
extract_emoji_query = _reply_markers.extract_emoji_query
extract_fadianji_mood = _reply_markers.extract_fadianji_mood
strip_tone_parenthetical = _reply_markers.strip_tone_parenthetical
strip_fadianji_catty_tics = _reply_markers.strip_fadianji_catty_tics
strip_voice_generation_narration = _reply_markers.strip_voice_generation_narration


class VoiceNarrationTests(unittest.TestCase):
    def test_drops_generation_sentences(self) -> None:
        self.assertEqual(
            strip_voice_generation_narration("语音正在生成，稍后就会发出来。看的时候别乱想啊"),
            "看的时候别乱想啊",
        )
        self.assertEqual(
            strip_voice_generation_narration("哼，等我一下。语音马上就发给你。"),
            "哼，等我一下。",
        )
        self.assertEqual(
            strip_voice_generation_narration("语音正在合成中。"),
            "",
        )

    def test_keeps_normal_talk(self) -> None:
        for text in (
            "喜欢你啊，笨蛋。",
            "我给你发过语音吗？",
            "刚刚那句语音好听吗",
            "生成一张图给你",
        ):
            with self.subTest(text=text):
                self.assertEqual(strip_voice_generation_narration(text), text)


class ExtractEmojiQueryTests(unittest.TestCase):
    def test_extracts_closed_marker(self) -> None:
        reply, query = extract_emoji_query(
            "杂鱼就这也能爽到喵？\n<<<CATTY_EMOJI_QUERY:唐猫不屑>>>"
        )

        self.assertEqual(reply, "杂鱼就这也能爽到喵？")
        self.assertEqual(query, "唐猫不屑")

    def test_strips_unclosed_marker_at_end(self) -> None:
        reply, query = extract_emoji_query(
            "杂鱼就这也能爽到喵？\n<<<CATTY_EMOJI_QUERY:唐猫不屑，图片也是"
        )

        self.assertEqual(reply, "杂鱼就这也能爽到喵？")
        self.assertEqual(query, "唐猫不屑，图片也是")

    def test_keeps_text_after_unclosed_marker_line(self) -> None:
        # _EMOJI_QUERY_RE 用 (?=\n) lookahead 不消费换行,所以未闭合 marker 被替成空
        # 串后会留下一个空行(『第一句\n[空]\n第二句』);.strip() 只清首尾。
        # 这是有意行为:保留行结构方便后续段落渲染,不强行合并空行。下游 reply chunker
        # 已在拆段时再做空行合并,这里不要重复 normalize。
        reply, query = extract_emoji_query(
            "第一句\n<<<CATTY_EMOJI_QUERY:哭哭\n第二句"
        )

        self.assertEqual(reply, "第一句\n\n第二句")
        self.assertEqual(query, "哭哭")

    def test_returns_original_without_marker(self) -> None:
        reply, query = extract_emoji_query("普通回复喵")

        self.assertEqual(reply, "普通回复喵")
        self.assertEqual(query, "")

    def test_strips_multiple_markers(self) -> None:
        reply, query = extract_emoji_query(
            "正文\n<<<CATTY_EMOJI_QUERY:不屑>>>\n<<<CATTY_EMOJI_QUERY:漏出来"
        )

        self.assertEqual(reply, "正文")
        self.assertEqual(query, "不屑")


class ExtractFadianjiMoodTests(unittest.TestCase):
    """2026-08-13 二轮: 机机情绪自报 <<<CATTY_FD_MOOD:tag>>> 提取与剥除。"""

    def test_extracts_closed_marker(self) -> None:
        reply, tag = extract_fadianji_mood(
            "不知道啊, 我瞎编的\n<<<CATTY_FD_MOOD:exposed>>>"
        )

        self.assertEqual(reply, "不知道啊, 我瞎编的")
        self.assertEqual(tag, "exposed")

    def test_tolerates_double_bracket_variant(self) -> None:
        reply, tag = extract_fadianji_mood("滚啊\n<<CATTY_FD_MOOD:teased_streak>>")

        self.assertEqual(reply, "滚啊")
        self.assertEqual(tag, "teased_streak")

    def test_strips_unclosed_marker_at_end(self) -> None:
        reply, tag = extract_fadianji_mood("嘿嘿\n<<<CATTY_FD_MOOD:praised")

        self.assertEqual(reply, "嘿嘿")
        self.assertEqual(tag, "praised")

    def test_returns_original_without_marker(self) -> None:
        reply, tag = extract_fadianji_mood("好好好")

        self.assertEqual(reply, "好好好")
        self.assertEqual(tag, "")

    def test_multiple_markers_first_tag_wins(self) -> None:
        reply, tag = extract_fadianji_mood(
            "正文\n<<<CATTY_FD_MOOD:broken_wrong>>>\n<<<CATTY_FD_MOOD:praised>>>"
        )

        self.assertEqual(reply, "正文")
        self.assertEqual(tag, "broken_wrong")
        self.assertNotIn("CATTY_FD_MOOD", reply)


class StripToneParentheticalTests(unittest.TestCase):
    """2026-08-12 主人: 机机回复里的（小声）式语气/动作注解括号, 出站程序硬剥。"""

    def test_strips_trailing_xiaosheng_observed_form(self) -> None:
        # 服务器日志实证形态 (2026-08-12 group:922298923)
        self.assertEqual(
            strip_tone_parenthetical("会啊 怎么 你要给我递纸（小声）"),
            "会啊 怎么 你要给我递纸",
        )
        self.assertEqual(
            strip_tone_parenthetical("知道啊 你叫得那么欢 想装听不见都难（小声）"),
            "知道啊 你叫得那么欢 想装听不见都难",
        )

    def test_strips_halfwidth_leading_and_mid_text(self) -> None:
        self.assertEqual(strip_tone_parenthetical("(小声)才不会呢"), "才不会呢")
        self.assertEqual(strip_tone_parenthetical("我（小声）说完了"), "我说完了")

    def test_strips_annotation_variants(self) -> None:
        self.assertEqual(strip_tone_parenthetical("才不要（超小声）"), "才不要")
        self.assertEqual(strip_tone_parenthetical("哦（轻笑）"), "哦")
        self.assertEqual(strip_tone_parenthetical("唉（叹气）"), "唉")
        self.assertEqual(strip_tone_parenthetical("滚啊（小声嘀咕）"), "滚啊")
        self.assertEqual(strip_tone_parenthetical("好吧（小声说）"), "好吧")
        self.assertEqual(strip_tone_parenthetical("（捂脸）"), "")

    def test_drops_empty_line_after_strip(self) -> None:
        self.assertEqual(strip_tone_parenthetical("在\n（小声）"), "在")

    def test_keeps_meme_parentheticals(self) -> None:
        # 语料实证玩梗括号必须原样保留
        self.assertEqual(strip_tone_parenthetical("想起号就现在给我哭丧（？）"), "想起号就现在给我哭丧（？）")
        self.assertEqual(strip_tone_parenthetical("我在布局（不是）"), "我在布局（不是）")
        self.assertEqual(strip_tone_parenthetical("（悲）"), "（悲）")
        self.assertEqual(strip_tone_parenthetical("睡了（去🦌了）"), "睡了（去🦌了）")
        self.assertEqual(strip_tone_parenthetical("有点点（龟速做视频中）"), "有点点（龟速做视频中）")

    def test_keeps_ban_word_plus_non_suffix_content(self) -> None:
        # 「名单词 + 非后缀字」= 内容, 不是注解 — 不误伤
        self.assertEqual(strip_tone_parenthetical("（笑死）"), "（笑死）")
        self.assertEqual(strip_tone_parenthetical("（笑一个）"), "（笑一个）")

    def test_strips_particle_tail_forms(self) -> None:
        # 主人 2026-08-14: 语气尾词(喵/呜/呐/嘤等)跟在注解词后也要剥, 修（小声喵）漏过滤;
        # 死/惨/爆/裂 等强调后缀不进尾词表,（笑死）仍保留。
        self.assertEqual(strip_tone_parenthetical("哼（小声喵）"), "哼")
        self.assertEqual(strip_tone_parenthetical("（小声地喵）"), "")
        self.assertEqual(strip_tone_parenthetical("哦（轻笑呜）"), "哦")
        self.assertEqual(strip_tone_parenthetical("别说了（捂脸呐）"), "别说了")
        self.assertEqual(strip_tone_parenthetical("呜呜（哭嘤）"), "呜呜")

    def test_no_paren_byte_identical(self) -> None:
        self.assertEqual(strip_tone_parenthetical("分机, 本体忙去了"), "分机, 本体忙去了")
        self.assertEqual(strip_tone_parenthetical(""), "")


class StripFadianjiCattyTicsTests(unittest.TestCase):
    def test_strips_catty_tic_and_cleans_spacing(self) -> None:
        self.assertEqual(
            strip_fadianji_catty_tics("活了 活蹦乱跳的 咋 刚才吓着你了喵"),
            "活了 活蹦乱跳的 咋 刚才吓着你了",
        )
        self.assertEqual(strip_fadianji_catty_tics("喵，别闹喵"), "别闹")
        self.assertEqual(strip_fadianji_catty_tics("喵呜～先等等喵。"), "先等等")

    def test_no_tic_is_byte_identical(self) -> None:
        self.assertEqual(strip_fadianji_catty_tics("我去 好好好"), "我去 好好好")
        self.assertEqual(strip_fadianji_catty_tics(""), "")


if __name__ == "__main__":
    unittest.main()
