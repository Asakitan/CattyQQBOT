from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .config import Config


@dataclass(slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""


def _clean_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    if url.startswith("//"):
        return "https:" + url
    return url


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[WebSearchResult] = []
        self._capture_title = False
        self._capture_snippet = False
        self._href = ""
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name: value or "" for name, value in attrs}
        classes = set(attrs_map.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self._capture_title = True
            self._href = attrs_map.get("href", "")
            self._text_parts = []
        elif "result__snippet" in classes:
            self._capture_snippet = True
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title or self._capture_snippet:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            title = unescape("".join(self._text_parts)).strip()
            url = _clean_duckduckgo_url(unescape(self._href).strip())
            if title and url:
                self.results.append(WebSearchResult(title=title, url=url))
            self._capture_title = False
            self._href = ""
            self._text_parts = []
        elif self._capture_snippet and tag in {"a", "div"}:
            snippet = unescape("".join(self._text_parts)).strip()
            if snippet and self.results and not self.results[-1].snippet:
                self.results[-1].snippet = " ".join(snippet.split())
            self._capture_snippet = False
            self._text_parts = []


async def search_web(config: Config, query: str) -> list[WebSearchResult]:
    if not query.strip() or not config.catty_web_search_enabled:
        return []
    timeout = config.catty_web_search_request_timeout or config.catty_request_timeout
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=config.catty_http_proxy or None) as client:
        response = await client.get("https://duckduckgo.com/html/", params={"q": query}, headers=headers)
    response.raise_for_status()
    parser = _DuckDuckGoParser()
    parser.feed(response.text)
    max_results = max(int(config.catty_web_search_max_results), 1)
    return parser.results[:max_results]


def _extract_duckduckgo_vqd(html: str) -> str:
    for pattern in (
        r"vqd=['\"]([^'\"]+)['\"]",
        r"vqd=([^&\"']+)&",
        r'"vqd"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, html)
        if match:
            return unescape(match.group(1))
    return ""


async def search_image_urls(config: Config, query: str, *, max_results: int = 6) -> list[str]:
    if not query.strip() or not config.catty_web_search_enabled:
        return []
    timeout = config.catty_web_search_request_timeout or config.catty_request_timeout
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Referer": "https://duckduckgo.com/",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=config.catty_http_proxy or None) as client:
        page = await client.get("https://duckduckgo.com/", params={"q": query, "iax": "images", "ia": "images"}, headers=headers)
        page.raise_for_status()
        vqd = _extract_duckduckgo_vqd(page.text)
        if not vqd:
            return []
        response = await client.get(
            "https://duckduckgo.com/i.js",
            params={"l": "zh-cn", "o": "json", "q": query, "vqd": vqd, "f": ",,,", "p": "1"},
            headers=headers,
        )
        response.raise_for_status()
    data = response.json()
    results = data.get("results") if isinstance(data, dict) else None
    urls: list[str] = []
    if not isinstance(results, list):
        return urls
    for item in results:
        if not isinstance(item, dict):
            continue
        image_url = str(item.get("image") or item.get("thumbnail") or "").strip()
        if image_url.startswith("http") and image_url not in urls:
            urls.append(image_url)
        if len(urls) >= max_results:
            break
    return urls


def format_search_context(query: str, results: list[WebSearchResult]) -> str:
    if not results:
        return f"本轮用户要求联网搜索「{query}」，但搜索没有返回可用结果。请如实说明没有搜到，避免编造。"
    lines = [
        f"本轮用户要求联网搜索「{query}」。请基于下面搜索结果回答，并在不确定时说明来源有限："
    ]
    for index, result in enumerate(results, 1):
        snippet = f"\n摘要：{result.snippet}" if result.snippet else ""
        lines.append(f"{index}. {result.title}\n链接：{result.url}{snippet}")
    return "\n".join(lines)
