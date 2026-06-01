"""网络梗 / 二次元词条百科查询 —— 让笨猫认识群里冒出来的新词。

设计目标:
- 单源 萌娘百科 (zh.moegirl.org.cn) MediaWiki API。覆盖网络流行语 + ACG/二次元 +
  角色/作品/术语。打 yyds / 孤独摇滚 / 雷电将军 都能命中。
- 工业/金融/百科级冷门术语 萌娘百科 没有,这种 AI 应改调 catty_web_search。
  本工具返回 {error: not_found, suggest: 'try catty_web_search'} 让 AI 自己路由。
- in-process LRU + TTL 缓存 600s(百科条目一天不会变多次)。
- 单次调用总超时 6s。
- 无 cookie/无登录,公益 API,带正确 UA 即可。

返回结构:
{
  "term": "原始查询词",
  "resolved_title": "命中后的页面标题(可能因 redirect 不同于 term)",
  "extract": "条目首段纯文本摘要",
  "url": "https://zh.moegirl.org.cn/...",
  "source": "moegirl",
  "candidates": ["相关候选词1", "相关候选词2"],  # opensearch 命中的近似词,供 AI 补述
  "from_cache": bool,
}
失败:
{ "error": "...", "term": "...", "suggest": "..."(可选) }
"""
from __future__ import annotations

from collections import OrderedDict
import logging
import time
from threading import RLock
from typing import Any

import httpx

from .config import Config


_logger = logging.getLogger("catty_qq_ai.meme_dict")


_API = "https://zh.moegirl.org.cn/api.php"
_PAGE_BASE = "https://zh.moegirl.org.cn/"
_PER_REQUEST_TIMEOUT = 6.0
_TOTAL_TIMEOUT = 12.0  # opensearch + extract 两次请求总和
_EXTRACT_MAX_CHARS = 360  # 给 AI 的摘要长度上限;太长会挤掉群聊回复 token
_CACHE_TTL = 600.0
_CACHE_MAX = 128

_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_cache_lock = RLock()


def _cache_get(key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return value


def _cache_put(key: str, value: dict[str, Any]) -> None:
    expires_at = time.monotonic() + _CACHE_TTL
    with _cache_lock:
        _cache[key] = (expires_at, value)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Accept": "application/json, text/plain, */*",
}


def _normalize_term(term: str) -> str:
    return (term or "").strip()


def _build_url(title: str) -> str:
    # MediaWiki 把空格替成下划线,引号/中文都不用 quote(萌娘允许直传)
    safe = title.replace(" ", "_")
    return f"{_PAGE_BASE}{safe}"


async def _opensearch(client: httpx.AsyncClient, term: str) -> list[str]:
    """拉前几个候选词,确认是否有命中。返回候选标题列表(可能为空)。"""
    try:
        resp = await client.get(
            _API,
            params={"action": "opensearch", "search": term, "limit": 5, "format": "json"},
            timeout=_PER_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.info("moegirl opensearch failed for %r: %s", term, exc)
        return []
    # opensearch 协议:[term, [titles], [descriptions], [urls]]
    if not isinstance(data, list) or len(data) < 2:
        return []
    titles = data[1]
    if not isinstance(titles, list):
        return []
    return [str(t).strip() for t in titles if t]


async def _extract(client: httpx.AsyncClient, title: str) -> tuple[str, str]:
    """拉指定页面的首段纯文本摘要。返回 (resolved_title, extract)。

    MediaWiki query API + prop=extracts + exintro=1 + explaintext=1 + redirects=1
    会自动处理 yyds → 永远的神 这种重定向。
    """
    try:
        resp = await client.get(
            _API,
            params={
                "action": "query",
                "format": "json",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "titles": title,
                "redirects": 1,
            },
            timeout=_PER_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.info("moegirl extract failed for %r: %s", title, exc)
        return title, ""
    pages = ((data.get("query") or {}).get("pages") or {})
    if not isinstance(pages, dict):
        return title, ""
    for _pid, page in pages.items():
        if not isinstance(page, dict):
            continue
        resolved = str(page.get("title") or title).strip()
        extract = str(page.get("extract") or "").strip()
        if extract:
            return resolved, extract
    return title, ""


async def lookup_term(config: Config, term: str) -> dict[str, Any]:
    """查询一个词条。命中返回完整结构,未命中返回 error。

    流程:
      1. opensearch 拿候选词列表(空 → not_found,引导 web_search)
      2. 用第一个候选词(MediaWiki opensearch 是相关度降序)调 extract
      3. extract 自动处理 redirect(yyds → 永远的神),返回 resolved title
      4. 缓存按 normalized term 当 key
    """
    normalized = _normalize_term(term)
    if not normalized:
        return {"error": "term 不能为空"}
    if len(normalized) > 80:
        return {"error": "term 太长,缩成 1-3 个关键词"}

    cache_key = normalized.lower()
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "from_cache": True}

    timeout = httpx.Timeout(_TOTAL_TIMEOUT, connect=3.0)
    proxy = config.catty_http_proxy or None
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, proxy=proxy, headers=_HEADERS
    ) as client:
        candidates = await _opensearch(client, normalized)
        if not candidates:
            return {
                "error": "not_found",
                "term": normalized,
                "suggest": (
                    "萌娘百科没收录,这词可能不是 ACG/网络梗范畴。"
                    "如果是新闻/工业/金融/通用名词,改调 catty_web_search 搜一下。"
                ),
            }

        # 优先用精确匹配(忽略大小写)的候选,没有就用第一个(最相关)
        normalized_lower = normalized.lower()
        chosen = next(
            (c for c in candidates if c.lower() == normalized_lower),
            candidates[0],
        )
        resolved_title, extract = await _extract(client, chosen)
        if not extract:
            return {
                "error": "page_empty",
                "term": normalized,
                "candidates": candidates[:5],
                "suggest": "萌娘百科有这个条目但没有 intro 摘要,你按候选词自由发挥即可",
            }

        if len(extract) > _EXTRACT_MAX_CHARS:
            extract = extract[:_EXTRACT_MAX_CHARS].rstrip() + "…"

        payload = {
            "term": normalized,
            "resolved_title": resolved_title,
            "extract": extract,
            "url": _build_url(resolved_title),
            "source": "moegirl",
            "candidates": [c for c in candidates if c != resolved_title][:4],
            "from_cache": False,
        }
        _cache_put(cache_key, {**payload, "from_cache": False})
        return payload
