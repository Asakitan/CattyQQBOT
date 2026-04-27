import importlib.util
from pathlib import Path
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "reply_markers.py"
_SPEC = importlib.util.spec_from_file_location("reply_markers", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_reply_markers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_reply_markers)
extract_emoji_query = _reply_markers.extract_emoji_query


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
        reply, query = extract_emoji_query(
            "第一句\n<<<CATTY_EMOJI_QUERY:哭哭\n第二句"
        )

        self.assertEqual(reply, "第一句\n第二句")
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


if __name__ == "__main__":
    unittest.main()
