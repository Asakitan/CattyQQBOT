import asyncio
from html import escape
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


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
    def test_bing_images_preserve_identity_with_reordered_attributes(self) -> None:
        payload = {
            "murl": "https://images.example.com/portrait.png?a=1&b=2",
            "purl": "https://example.com/character",
            "t": 'Character "portrait" & reference',
        }
        parser = _web_search._BingImageParser()
        parser.feed(f"<a m='{escape(json.dumps(payload), quote=True)}' class='extra iusc selected'></a>")
        self.assertEqual(len(parser.results), 1)
        result = parser.results[0]
        self.assertEqual(result.image_url, payload["murl"])
        self.assertEqual(result.title, payload["t"])
        self.assertEqual(result.page_url, payload["purl"])
        self.assertEqual(result.source, "Bing")

    def test_bing_images_skip_invalid_and_duplicate_candidates(self) -> None:
        parser = _web_search._BingImageParser()
        for payload in ([], {"murl": "http-not-a-url"}, {"murl": "file:///photo.png"},
                        {"murl": "https://example.com/first.png"},
                        {"murl": "https://example.com/first.png"},
                        {"murl": "https://example.com/second.png"}):
            parser.feed(f'<a class="iusc" m="{escape(json.dumps(payload), quote=True)}"></a>')
        parser.feed('<a class="iusc" m="invalid json"></a><img src="https://example.com/ad.png">')
        self.assertEqual([item.image_url for item in parser.results], [
            "https://example.com/first.png", "https://example.com/second.png",
        ])

    def test_image_search_fallback_preserves_metadata_and_url_compatibility(self) -> None:
        config = types.SimpleNamespace(catty_web_search_enabled=True, catty_web_search_request_timeout=10)
        expected = [_web_search.ImageSearchResult(
            image_url="https://example.com/portrait.png", title="Portrait",
            page_url="https://example.com/source", source="DuckDuckGo",
        )]
        with (
            patch.object(_web_search, "_search_images_bing", AsyncMock(return_value=[])),
            patch.object(_web_search, "_search_images_duckduckgo", AsyncMock(return_value=expected)),
        ):
            self.assertEqual(asyncio.run(_web_search.search_image_results(config, "portrait")), expected)
            self.assertEqual(asyncio.run(_web_search.search_image_urls(config, "portrait")), [expected[0].image_url])
            self.assertEqual(asyncio.run(_web_search.search_image_results(config, "portrait", max_results=0)), [])

    def test_duckduckgo_images_preserve_title_and_source_page(self) -> None:
        config = types.SimpleNamespace(catty_http_proxy="")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.side_effect = [
            _web_search.httpx.Response(200, text="vqd='test-vqd'", request=_web_search.httpx.Request("GET", "https://example.com")),
            _web_search.httpx.Response(200, json={"results": [
                {"image": "https://example.com/portrait.png", "title": "Portrait", "url": "https://example.com/source"},
                {"image": "https://example.com/portrait.png", "title": "Duplicate"},
                {"image": "file:///bad.png"},
            ]}, request=_web_search.httpx.Request("GET", "https://example.com")),
        ]
        with patch.object(_web_search.httpx, "AsyncClient", return_value=client):
            results = asyncio.run(_web_search._search_images_duckduckgo(config, "portrait", max_results=3, timeout=4))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Portrait")
        self.assertEqual(results[0].page_url, "https://example.com/source")
        self.assertEqual(results[0].source, "DuckDuckGo")

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
