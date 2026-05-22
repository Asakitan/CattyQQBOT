"""中文互联网实时热搜聚合 —— 让笨猫能接当下网络热梗。

设计目标:
- 多源聚合(微博 / 哔哩哔哩 / 知乎 / 抖音热点),任一源挂掉不影响整体。
- 单次拉取 in-process TTL 缓存(默认 180s),避免一群人同时戳服务器。
- 总超时 8s 硬约束,绝不拖垮主回复链路。
- 返回结构化结果(source/rank/title/hot 分数/url)供 LLM 直接组织成口语化的"猫猫听说"。

不依赖任何商业 API key:走第三方公益聚合接口 + 兜底官方 HTML/RSS。
所有源若全部失败,executor 返回 error,AI 用人格自然说"猫猫今天网线断了"即可。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Config


_logger = logging.getLogger("catty_qq_ai.hot_trends")


# ── 数据结构 ──────────────────────────────────────────────────────────

@dataclass(slots=True)
class HotItem:
    source: str          # 来源标识:weibo / bilibili / zhihu / douyin
    rank: int            # 1-based 排名
    title: str           # 热搜词条
    hot: str = ""        # 热度文本(微博"沸/爆/N 万讨论"、B 站点击数等)
    url: str = ""        # 跳转链接(可选)
    summary: str = ""    # 词条简介/描述(可选)


# ── 共享 TTL 缓存 ─────────────────────────────────────────────────────

_HOT_CACHE_TTL = 180.0      # 全局聚合结果 3 分钟缓存
_HOT_TOTAL_TIMEOUT = 8.0    # 单次聚合总超时
_HOT_PER_SOURCE_TIMEOUT = 4.5  # 单源超时
_cache: dict[str, tuple[float, list[HotItem]]] = {}


def _cache_get(key: str) -> list[HotItem] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if expires_at <= time.monotonic():
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: list[HotItem]) -> None:
    _cache[key] = (time.monotonic() + _HOT_CACHE_TTL, value)
    if len(_cache) > 32:
        stale = sorted(_cache.items(), key=lambda item: item[1][0])
        for k, _ in stale[: len(_cache) - 32]:
            _cache.pop(k, None)


# ── 各源抓取实现 ──────────────────────────────────────────────────────

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Accept": "application/json, text/plain, */*",
}


def _headers_with_referer(referer: str) -> dict[str, str]:
    """很多国内站(微博/B 站/抖音)的开放 API 无 Referer 直接 403,这里按源附上对应主域。"""
    return {**_BASE_HEADERS, "Referer": referer}


async def _fetch_weibo(client: httpx.AsyncClient, limit: int) -> list[HotItem]:
    """微博热搜榜:走 mobile API,返回 JSON。必须带 Referer 否则 403。"""
    url = "https://weibo.com/ajax/side/hotSearch"
    headers = _headers_with_referer("https://weibo.com/")
    try:
        resp = await client.get(url, headers=headers, timeout=_HOT_PER_SOURCE_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.info("weibo hot fetch failed: %s", exc)
        return []
    items: list[HotItem] = []
    realtime = (data.get("data") or {}).get("realtime") or []
    for idx, entry in enumerate(realtime[:limit], start=1):
        title = str(entry.get("word") or "").strip()
        if not title:
            continue
        hot_raw = entry.get("raw_hot") or entry.get("num") or ""
        try:
            hot_val = int(hot_raw)
            hot_str = f"{hot_val // 10000}万" if hot_val >= 10000 else str(hot_val)
        except (TypeError, ValueError):
            hot_str = str(hot_raw) if hot_raw else ""
        flag_label = ""
        flag = entry.get("label_name") or entry.get("icon_desc") or ""
        if flag:
            flag_label = f"[{flag}] "
        items.append(
            HotItem(
                source="weibo",
                rank=idx,
                title=title,
                hot=f"{flag_label}{hot_str}".strip() if (flag_label or hot_str) else "",
                url=f"https://s.weibo.com/weibo?q=%23{title}%23",
            )
        )
    return items


async def _fetch_bilibili(client: httpx.AsyncClient, limit: int) -> list[HotItem]:
    """B 站热搜:走 search 域名公开 API。"""
    url = "https://app.bilibili.com/x/v2/search/trending/ranking"
    try:
        resp = await client.get(
            url,
            params={"limit": max(limit, 10)},
            headers=_headers_with_referer("https://www.bilibili.com/"),
            timeout=_HOT_PER_SOURCE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.info("bilibili hot fetch failed: %s", exc)
        return []
    items: list[HotItem] = []
    raw_list = (data.get("data") or {}).get("list") or []
    for idx, entry in enumerate(raw_list[:limit], start=1):
        title = str(entry.get("show_name") or entry.get("keyword") or "").strip()
        if not title:
            continue
        items.append(
            HotItem(
                source="bilibili",
                rank=idx,
                title=title,
                hot="",  # B 站 ranking API 不返回数值热度,留空
                url=f"https://search.bilibili.com/all?keyword={title}",
            )
        )
    return items


async def _fetch_zhihu(client: httpx.AsyncClient, limit: int) -> list[HotItem]:
    """知乎热榜:走 api.zhihu.com 公开热榜接口。"""
    url = "https://api.zhihu.com/topstory/hot-list"
    try:
        resp = await client.get(
            url,
            params={"limit": max(limit, 10), "desktop": "true"},
            headers=_headers_with_referer("https://www.zhihu.com/"),
            timeout=_HOT_PER_SOURCE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.info("zhihu hot fetch failed: %s", exc)
        return []
    items: list[HotItem] = []
    for idx, entry in enumerate((data.get("data") or [])[:limit], start=1):
        target = entry.get("target") or {}
        title = str(target.get("title") or "").strip()
        if not title:
            continue
        detail = str(entry.get("detail_text") or "").strip()
        url_id = target.get("id")
        items.append(
            HotItem(
                source="zhihu",
                rank=idx,
                title=title,
                hot=detail,
                url=f"https://www.zhihu.com/question/{url_id}" if url_id else "",
                summary=str(target.get("excerpt") or "")[:140],
            )
        )
    return items


async def _fetch_douyin(client: httpx.AsyncClient, limit: int) -> list[HotItem]:
    """抖音热搜榜:走 aweme 公开 hot/search API。"""
    url = "https://www.iesdouyin.com/aweme/v1/hot/search/list/"
    try:
        resp = await client.get(
            url,
            headers=_headers_with_referer("https://www.douyin.com/"),
            timeout=_HOT_PER_SOURCE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.info("douyin hot fetch failed: %s", exc)
        return []
    items: list[HotItem] = []
    raw = ((data.get("data") or {}).get("word_list")) or []
    for idx, entry in enumerate(raw[:limit], start=1):
        title = str(entry.get("word") or "").strip()
        if not title:
            continue
        hot_val = entry.get("hot_value")
        try:
            hot_int = int(hot_val)
            hot_str = f"{hot_int // 10000}万" if hot_int >= 10000 else str(hot_int)
        except (TypeError, ValueError):
            hot_str = ""
        items.append(
            HotItem(
                source="douyin",
                rank=idx,
                title=title,
                hot=hot_str,
                url=f"https://www.douyin.com/search/{title}",
            )
        )
    return items


_SOURCE_FETCHERS = {
    "weibo": _fetch_weibo,
    "bilibili": _fetch_bilibili,
    "zhihu": _fetch_zhihu,
    "douyin": _fetch_douyin,
}


def normalize_sources(raw: Any) -> list[str]:
    """把 AI 传来的 sources 参数归一化成合法源列表;空/无效就用默认全部。"""
    default = ["weibo", "bilibili", "zhihu", "douyin"]
    if not raw:
        return default
    if isinstance(raw, str):
        # 允许 "weibo,bilibili" / "weibo bilibili" / "全部" / "all"
        if raw.strip().lower() in {"all", "全部", "*"}:
            return default
        candidates = [s.strip().lower() for s in re.split(r"[,，\s/|]+", raw) if s.strip()]
    elif isinstance(raw, (list, tuple)):
        candidates = [str(s).strip().lower() for s in raw if str(s).strip()]
    else:
        return default
    # 中文别名映射
    alias = {
        "微博": "weibo",
        "b站": "bilibili",
        "哔哩哔哩": "bilibili",
        "bili": "bilibili",
        "知乎": "zhihu",
        "抖音": "douyin",
    }
    seen: list[str] = []
    for c in candidates:
        norm = alias.get(c, c)
        if norm in _SOURCE_FETCHERS and norm not in seen:
            seen.append(norm)
    return seen or default


async def fetch_hot_trends(
    config: Config,
    *,
    sources: list[str] | None = None,
    limit_per_source: int = 6,
) -> dict[str, Any]:
    """聚合多源热搜。返回 {sources: {weibo:[...], ...}, total: N, errors: [...]}。

    任一源失败不阻塞其它源,所有源都失败返回 errors 列表。
    全局缓存 180s。

    返回结构:
    {
      "sources": {"weibo": [HotItem 字典化], ...},
      "total": int,
      "errors": ["weibo: timeout", ...],  # 仅失败的源,成功的不会出现
      "from_cache": bool,
    }
    """
    chosen = normalize_sources(sources)
    limit_per_source = max(min(int(limit_per_source or 6), 20), 1)
    cache_key = f"{','.join(chosen)}|n:{limit_per_source}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return _build_payload(cached, errors=[], from_cache=True)

    timeout = httpx.Timeout(_HOT_TOTAL_TIMEOUT, connect=3.0)
    proxy = config.catty_http_proxy or None
    items_all: list[HotItem] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=proxy) as client:
        tasks = {
            name: asyncio.create_task(_SOURCE_FETCHERS[name](client, limit_per_source))
            for name in chosen
        }
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks.values(), return_exceptions=True),
                timeout=_HOT_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            errors.append("总超时,部分源未返回")
        for name, task in tasks.items():
            if not task.done():
                task.cancel()
                errors.append(f"{name}: 超时被取消")
                continue
            try:
                items = task.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc.__class__.__name__}")
                continue
            if not items:
                errors.append(f"{name}: 空结果")
                continue
            items_all.extend(items)

    if items_all:
        _cache_put(cache_key, items_all)
    return _build_payload(items_all, errors=errors, from_cache=False)


def _build_payload(items: list[HotItem], *, errors: list[str], from_cache: bool) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        grouped.setdefault(it.source, []).append(
            {
                "rank": it.rank,
                "title": it.title,
                **({"hot": it.hot} if it.hot else {}),
                **({"url": it.url} if it.url else {}),
                **({"summary": it.summary} if it.summary else {}),
            }
        )
    payload: dict[str, Any] = {
        "sources": grouped,
        "total": len(items),
        "from_cache": from_cache,
    }
    if errors:
        payload["errors"] = errors
    return payload


def format_for_prompt(payload: dict[str, Any], *, max_per_source: int = 5) -> str:
    """把 fetch_hot_trends 结果整成方便 LLM 阅读的紧凑文本(供其它模块直接拼 prompt)。"""
    sources = payload.get("sources") or {}
    if not sources:
        return ""
    lines: list[str] = []
    source_titles = {
        "weibo": "微博热搜",
        "bilibili": "B 站",
        "zhihu": "知乎热榜",
        "douyin": "抖音热搜",
    }
    for src in ("weibo", "bilibili", "zhihu", "douyin"):
        rows = sources.get(src) or []
        if not rows:
            continue
        title = source_titles.get(src, src)
        for row in rows[:max_per_source]:
            rank = row.get("rank", "?")
            t = row.get("title", "")
            hot = row.get("hot")
            if hot:
                lines.append(f"[{title} #{rank}] {t} ({hot})")
            else:
                lines.append(f"[{title} #{rank}] {t}")
    return "\n".join(lines)
