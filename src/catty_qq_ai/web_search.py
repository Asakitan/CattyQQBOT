from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree

import httpx

from .config import Config


_logger = logging.getLogger("catty_qq_ai.web_search")


@dataclass(slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""


def _clean_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    if url.startswith("//"):
        return "https:" + url
    return url


def _clean_google_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = parse_qs(parsed.query).get("q", [""])[0]
        if target:
            return unquote(target)
    if url.startswith("/url?"):
        target = parse_qs(urlparse(url).query).get("q", [""])[0]
        if target:
            return unquote(target)
    if url.startswith("//"):
        return "https:" + url
    return url


def _is_search_result_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    blocked_hosts = (
        "bing.com",
        "google.com",
        "gstatic.com",
        "googleusercontent.com",
        "microsoft.com",
        "msn.com",
    )
    return not any(parsed.netloc.endswith(host) for host in blocked_hosts)


class _GoogleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[WebSearchResult] = []
        self._pending_href = ""
        self._capture_title = False
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name: value or "" for name, value in attrs}
        if tag == "a":
            href = _clean_google_url(unescape(attrs_map.get("href", "")).strip())
            if _is_search_result_url(href):
                self._pending_href = href
        elif tag == "h3" and self._pending_href:
            self._capture_title = True
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._capture_title:
            title = unescape("".join(self._text_parts)).strip()
            if title and self._pending_href:
                self.results.append(WebSearchResult(title=title, url=self._pending_href, source="Google"))
            self._capture_title = False
            self._pending_href = ""
            self._text_parts = []


class _BingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[WebSearchResult] = []
        self._in_result = False
        self._in_title = False
        self._capture_title = False
        self._capture_snippet = False
        self._href = ""
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name: value or "" for name, value in attrs}
        classes = set(attrs_map.get("class", "").split())
        if tag == "li" and "b_algo" in classes:
            self._in_result = True
        elif self._in_result and tag == "h2":
            self._in_title = True
        elif self._in_title and tag == "a":
            href = unescape(attrs_map.get("href", "")).strip()
            if _is_search_result_url(href):
                self._capture_title = True
                self._href = href
                self._text_parts = []
        elif self._in_result and tag == "p":
            self._capture_snippet = True
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title or self._capture_snippet:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            title = unescape("".join(self._text_parts)).strip()
            if title and self._href:
                self.results.append(WebSearchResult(title=title, url=self._href, source="Bing"))
            self._capture_title = False
            self._href = ""
            self._text_parts = []
        elif tag == "h2" and self._in_title:
            self._in_title = False
        elif tag == "p" and self._capture_snippet:
            snippet = unescape("".join(self._text_parts)).strip()
            if snippet and self.results and not self.results[-1].snippet:
                self.results[-1].snippet = " ".join(snippet.split())
            self._capture_snippet = False
            self._text_parts = []
        elif tag == "li" and self._in_result:
            self._in_result = False


def _parse_rss_results(xml_text: str, source: str) -> list[WebSearchResult]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    results: list[WebSearchResult] = []
    for item in root.findall(".//item"):
        title = "".join(item.findtext("title") or "").strip()
        url = "".join(item.findtext("link") or "").strip()
        snippet = "".join(item.findtext("description") or "").strip()
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = " ".join(unescape(snippet).split())
        if title and url:
            results.append(WebSearchResult(title=unescape(title), url=url, snippet=snippet, source=source))
    return results


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
    max_results = max(int(config.catty_web_search_max_results), 1)
    engines = [engine.lower() for engine in config.catty_web_search_engines if engine.strip()]
    if not engines:
        engines = ["google", "bing"]

    seen_urls: set[str] = set()
    results_by_engine: list[list[WebSearchResult]] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=config.catty_http_proxy or None) as client:
        for engine in engines:
            parser: HTMLParser
            try:
                if engine == "google":
                    response = await client.get(
                        "https://news.google.com/rss/search",
                        params={"q": query, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
                        headers=headers,
                    )
                    parser = _GoogleParser()
                elif engine == "bing":
                    response = await client.get(
                        "https://www.bing.com/search",
                        params={"q": query, "format": "rss", "setlang": "zh-CN"},
                        headers=headers,
                    )
                    parser = _BingParser()
                elif engine == "duckduckgo":
                    response = await client.get("https://duckduckgo.com/html/", params={"q": query}, headers=headers)
                    parser = _DuckDuckGoParser()
                else:
                    continue
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            if engine in {"google", "bing"} and "xml" in response.headers.get("content-type", "").lower():
                parsed_results = _parse_rss_results(response.text, "Google" if engine == "google" else "Bing")
            else:
                parser.feed(response.text)
                parsed_results = getattr(parser, "results", [])
            engine_results: list[WebSearchResult] = []
            for result in parsed_results:
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                engine_results.append(result)
                if len(engine_results) >= max_results:
                    break
            if engine_results:
                results_by_engine.append(engine_results)
    results: list[WebSearchResult] = []
    for offset in range(max_results):
        for engine_results in results_by_engine:
            if offset < len(engine_results):
                results.append(engine_results[offset])
                if len(results) >= max_results:
                    return results
    return results


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


# Bing 图片搜索响应里每个图片是 <a class="iusc" m='{"murl":"https://...","turl":"..."}'>;
# 提取 m 属性内 JSON 的 murl 字段(原图)。HTML 实体可能被 escape 成 &quot;,unescape 后能 json.loads。
_BING_IUSC_RE = re.compile(r'<a[^>]+class="iusc"[^>]+m="([^"]+)"', re.DOTALL)


async def _search_image_urls_bing(
    config: Config,
    query: str,
    *,
    max_results: int,
    timeout: float,
) -> list[str]:
    """Bing 图片搜索;国内可直连,不依赖 duckduckgo。失败抛 httpx.HTTPError,让 caller fallback。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    params = {"q": query, "form": "HDRSC2", "first": "1"}
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, proxy=config.catty_http_proxy or None
    ) as client:
        response = await client.get("https://www.bing.com/images/search", params=params, headers=headers)
        response.raise_for_status()

    urls: list[str] = []
    for match in _BING_IUSC_RE.finditer(response.text):
        raw = unescape(match.group(1))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        murl = str(payload.get("murl") or "").strip()
        if murl.startswith("http") and murl not in urls:
            urls.append(murl)
        if len(urls) >= max_results:
            break
    return urls


async def _search_image_urls_duckduckgo(
    config: Config,
    query: str,
    *,
    max_results: int,
    timeout: float,
) -> list[str]:
    """DuckDuckGo 图片搜索 fallback;国内有时连不上。"""
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


async def search_image_urls(config: Config, query: str, *, max_results: int = 6) -> list[str]:
    """图片搜索:bing 优先(国内可直连),duckduckgo 降级兜底(走代理时可能 work)。

    任一源拿到 >= 1 张图就返回;两个都 fail 才返回空列表。
    单源 timeout 取 catty_web_search_request_timeout(默认 10s)的一半,留余量给 fallback。
    """
    if not query.strip() or not config.catty_web_search_enabled:
        return []
    total_timeout = float(config.catty_web_search_request_timeout or config.catty_request_timeout or 10.0)
    per_engine_timeout = max(min(total_timeout / 2, 8.0), 4.0)

    try:
        urls = await _search_image_urls_bing(config, query, max_results=max_results, timeout=per_engine_timeout)
        if urls:
            return urls
        _logger.info("Bing image search returned 0 urls for %r,fallback to duckduckgo", query)
    except (httpx.HTTPError, ValueError) as exc:
        _logger.info("Bing image search failed for %r: %s; fallback to duckduckgo", query, exc)

    try:
        return await _search_image_urls_duckduckgo(
            config, query, max_results=max_results, timeout=per_engine_timeout
        )
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("DuckDuckGo image search also failed for %r: %s", query, exc)
        return []


def format_search_context(query: str, results: list[WebSearchResult]) -> str:
    if not results:
        return f"本轮用户要求联网搜索「{query}」，但搜索没有返回可用结果。请如实说明没有搜到，避免编造。"
    lines = [
        f"本轮用户要求联网搜索「{query}」。请基于下面搜索结果回答，并在不确定时说明来源有限："
    ]
    for index, result in enumerate(results, 1):
        snippet = f"\n摘要：{result.snippet}" if result.snippet else ""
        source = f"来源：{result.source}\n" if result.source else ""
        lines.append(f"{index}. {result.title}\n{source}链接：{result.url}{snippet}")
    return "\n".join(lines)
