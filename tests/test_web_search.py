import importlib.util
from pathlib import Path
import sys
import types
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = types.ModuleType("catty_qq_ai")
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault("catty_qq_ai", _PACKAGE)
_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "web_search.py"
_SPEC = importlib.util.spec_from_file_location("catty_qq_ai.web_search", _MODULE_PATH)
_web_search = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = _web_search
_SPEC.loader.exec_module(_web_search)


class WebSearchParserTests(unittest.TestCase):
    def test_google_parser_extracts_result_links(self) -> None:
        parser = _web_search._GoogleParser()

        parser.feed(
            '<a href="/url?q=https%3A%2F%2Fexample.com%2Fnews&sa=U">'
            "<h3>Example News</h3></a>"
        )

        self.assertEqual(parser.results[0].title, "Example News")
        self.assertEqual(parser.results[0].url, "https://example.com/news")
        self.assertEqual(parser.results[0].source, "Google")

    def test_bing_parser_extracts_result_and_snippet(self) -> None:
        parser = _web_search._BingParser()

        parser.feed(
            '<li class="b_algo"><h2><a href="https://example.org/a">Example A</a></h2>'
            "<div><p>Snippet text here.</p></div></li>"
        )

        self.assertEqual(parser.results[0].title, "Example A")
        self.assertEqual(parser.results[0].url, "https://example.org/a")
        self.assertEqual(parser.results[0].snippet, "Snippet text here.")
        self.assertEqual(parser.results[0].source, "Bing")

    def test_format_search_context_includes_sources(self) -> None:
        context = _web_search.format_search_context(
            "query",
            [_web_search.WebSearchResult(title="Title", url="https://example.com", source="Google")],
        )

        self.assertIn("来源：Google", context)
        self.assertIn("https://example.com", context)

    def test_rss_parser_extracts_items(self) -> None:
        results = _web_search._parse_rss_results(
            "<?xml version='1.0'?><rss><channel><item>"
            "<title>Example</title><link>https://example.net</link>"
            "<description>&lt;b&gt;Snippet&lt;/b&gt; text</description>"
            "</item></channel></rss>",
            "Bing",
        )

        self.assertEqual(results[0].title, "Example")
        self.assertEqual(results[0].url, "https://example.net")
        self.assertEqual(results[0].snippet, "Snippet text")
        self.assertEqual(results[0].source, "Bing")


if __name__ == "__main__":
    unittest.main()
