"""反向图片搜索:把一张图扔进 SauceNAO / trace.moe / ascii2d / iqdb,问出
「这是谁画的/什么番剧/什么角色/出自哪里」。

设计要点
========
- 每个引擎一个 ``async def _search_<engine>(client, image_url, *, max_results)``
  函数,失败 silent return [] (httpx.HTTPError / 解析失败都吞),不抛出。
- 顶层 ``reverse_image_search`` 并发跑选中的引擎,收集 ``ImageSearchResult``
  统一格式返回。caller 自己排序/截取/格式化。
- 引擎选择遵循 ``kind`` 提示:
    - ``anime`` → trace.moe(番剧场景识别)+ saucenao(动漫 indexer)
    - ``artwork`` → saucenao + ascii2d + iqdb(画师/角色)
    - ``auto``    → saucenao 主力,失败/无 API key 时退到 ascii2d
- SauceNAO 公开 JSON API 需要 ``api_key``;没配时直接跳过(caller 看到结果
  为空再 fallback)。日免费额度 100/day,所以默认 ``numres=5``。
- trace.moe 无需 key,但 IP 上限 60 req / minute,流量大可挂代理。
- ascii2d/iqdb 是 HTML 抓取,解析比较 fragile,只取最核心字段。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from .config import Config

logger = logging.getLogger("nonebot").getChild("catty_qq_ai.image_reverse_search")


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)


@dataclass(slots=True)
class ImageSearchResult:
    """一个搜图引擎返回的一条结果。

    similarity: 0-100 浮点(高=越像);引擎返回字符串/小数都归一化到 100 制。
    kind: anime / artwork / character / general — 给 AI 做语言提示。
    extra: 引擎专属字段(番名/集数/时间戳/作者ID 等),AI 自己挑话用。
    """

    source: str
    title: str
    url: str
    similarity: float = 0.0
    author: str = ""
    thumbnail: str = ""
    kind: str = "general"
    extra: dict[str, Any] = field(default_factory=dict)


def _common_headers(referer: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.6",
    }
    if referer:
        headers["Referer"] = referer
    return headers


# ---------- SauceNAO ----------

# 常见 index_id → (label, kind, 主链接字段)
# https://saucenao.com/tools/examples/api/index_details.txt
_SAUCENAO_INDEX_HINTS: dict[int, tuple[str, str]] = {
    5: ("Pixiv", "artwork"),
    6: ("Pixiv Historical", "artwork"),
    8: ("Nico Nico Seiga", "artwork"),
    9: ("Danbooru", "artwork"),
    10: ("drawr", "artwork"),
    11: ("Nijie", "artwork"),
    12: ("Yandere", "artwork"),
    16: ("FAKKU", "artwork"),
    18: ("nHentai", "artwork"),
    19: ("2D-Market", "artwork"),
    20: ("MediBang", "artwork"),
    21: ("Anime", "anime"),
    22: ("H-Anime", "anime"),
    23: ("Movies", "anime"),
    24: ("Shows", "anime"),
    25: ("Gelbooru", "artwork"),
    26: ("Konachan", "artwork"),
    27: ("Sankaku Channel", "artwork"),
    28: ("Anime-Pictures.net", "artwork"),
    29: ("e621.net", "artwork"),
    30: ("Idol Complex", "artwork"),
    31: ("bcy.net Illust", "artwork"),
    32: ("bcy.net Cosplay", "artwork"),
    34: ("deviantArt", "artwork"),
    35: ("Pawoo.net", "artwork"),
    36: ("Madokami", "artwork"),
    37: ("MangaDex", "artwork"),
    38: ("E-Hentai", "artwork"),
    39: ("ArtStation", "artwork"),
    40: ("FurAffinity", "artwork"),
    41: ("Twitter", "artwork"),
    42: ("Furry Network", "artwork"),
    43: ("Kemono", "artwork"),
    44: ("Skeb", "artwork"),
}


async def _search_saucenao(
    client: httpx.AsyncClient,
    image_url: str,
    *,
    api_key: str,
    max_results: int,
) -> list[ImageSearchResult]:
    if not api_key:
        logger.info("saucenao skipped: no api_key configured")
        return []
    params = {
        "output_type": "2",  # JSON
        "api_key": api_key,
        "db": "999",
        "numres": str(max(min(max_results, 16), 1)),
        "url": image_url,
    }
    try:
        response = await client.get(
            "https://saucenao.com/search.php",
            params=params,
            headers=_common_headers(),
        )
    except httpx.HTTPError as exc:
        logger.info("saucenao request failed: %s: %s", exc.__class__.__name__, exc)
        return []
    if response.status_code >= 400:
        logger.info(
            "saucenao status=%d body=%s", response.status_code, response.text[:200]
        )
        return []
    try:
        data = response.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    header = data.get("header") if isinstance(data.get("header"), dict) else {}
    status = header.get("status")
    if isinstance(status, int) and status != 0:
        # status<0 = 用户层错误(rate limit / bad key); status>0 = 服务端错误。
        logger.info(
            "saucenao logical error status=%s message=%s",
            status,
            header.get("message") or header.get("user_id"),
        )
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return []
    out: list[ImageSearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        item_header = item.get("header") if isinstance(item.get("header"), dict) else {}
        item_data = item.get("data") if isinstance(item.get("data"), dict) else {}
        try:
            similarity = float(str(item_header.get("similarity") or "0").strip())
        except (TypeError, ValueError):
            similarity = 0.0
        index_id = int(item_header.get("index_id") or 0)
        label, kind = _SAUCENAO_INDEX_HINTS.get(index_id, ("SauceNAO", "general"))

        ext_urls = item_data.get("ext_urls")
        url = ""
        if isinstance(ext_urls, list) and ext_urls:
            url = str(ext_urls[0]).strip()
        # 标题优先级:title → 番名 source → 角色/material → index_name
        title = ""
        for key in ("title", "jp_name", "source", "eng_name", "material"):
            value = item_data.get(key)
            if isinstance(value, str) and value.strip():
                title = value.strip()
                break
        if not title:
            title = str(item_header.get("index_name") or label).strip()

        author = ""
        for key in ("member_name", "creator", "author_name", "user_name", "twitter_user_handle", "pawoo_user_username"):
            value = item_data.get(key)
            if isinstance(value, str) and value.strip():
                author = value.strip()
                break
            if isinstance(value, list) and value:
                first = str(value[0]).strip()
                if first:
                    author = first
                    break

        thumbnail = str(item_header.get("thumbnail") or "").strip()

        extra: dict[str, Any] = {"index_label": label}
        for key in (
            "pixiv_id", "danbooru_id", "gelbooru_id", "yandere_id", "konachan_id",
            "tweet_id", "member_id", "characters", "material", "part", "year",
            "est_time", "source", "eng_name", "jp_name",
        ):
            value = item_data.get(key)
            if value not in (None, "", []):
                extra[key] = value
        if isinstance(ext_urls, list) and len(ext_urls) > 1:
            extra["alt_urls"] = [str(u).strip() for u in ext_urls[1:] if str(u).strip()]

        out.append(
            ImageSearchResult(
                source="saucenao",
                title=title,
                url=url,
                similarity=similarity,
                author=author,
                thumbnail=thumbnail,
                kind=kind,
                extra=extra,
            )
        )
        if len(out) >= max_results:
            break
    return out


# ---------- trace.moe(番剧场景) ----------

async def _search_tracemoe(
    client: httpx.AsyncClient,
    image_url: str,
    *,
    max_results: int,
) -> list[ImageSearchResult]:
    params = {"url": image_url, "anilistInfo": "1", "cutBorders": "1"}
    try:
        response = await client.get(
            "https://api.trace.moe/search", params=params, headers=_common_headers()
        )
    except httpx.HTTPError as exc:
        logger.info("trace.moe request failed: %s: %s", exc.__class__.__name__, exc)
        return []
    if response.status_code >= 400:
        logger.info(
            "trace.moe status=%d body=%s", response.status_code, response.text[:200]
        )
        return []
    try:
        data = response.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    raw_results = data.get("result")
    if not isinstance(raw_results, list):
        return []
    out: list[ImageSearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        anilist = item.get("anilist") if isinstance(item.get("anilist"), dict) else {}
        titles = anilist.get("title") if isinstance(anilist.get("title"), dict) else {}
        title = ""
        for key in ("chinese", "native", "romaji", "english"):
            value = titles.get(key) if isinstance(titles, dict) else None
            if isinstance(value, str) and value.strip():
                title = value.strip()
                break
        if not title:
            title = str(item.get("filename") or "Unknown anime").strip()
        try:
            similarity_raw = float(item.get("similarity") or 0.0)
        except (TypeError, ValueError):
            similarity_raw = 0.0
        similarity = round(similarity_raw * 100.0, 2)
        anilist_id = anilist.get("id") if isinstance(anilist, dict) else None
        mal_id = anilist.get("idMal") if isinstance(anilist, dict) else None
        url = ""
        if isinstance(anilist_id, int) and anilist_id > 0:
            url = f"https://anilist.co/anime/{anilist_id}"
        elif isinstance(mal_id, int) and mal_id > 0:
            url = f"https://myanimelist.net/anime/{mal_id}"

        extra: dict[str, Any] = {}
        if item.get("episode") is not None:
            extra["episode"] = item.get("episode")
        if item.get("from") is not None:
            extra["from_seconds"] = round(float(item.get("from") or 0.0), 2)
        if item.get("to") is not None:
            extra["to_seconds"] = round(float(item.get("to") or 0.0), 2)
        if item.get("filename"):
            extra["filename"] = item.get("filename")
        if anilist.get("isAdult"):
            extra["is_adult"] = True
        if isinstance(titles, dict):
            for key in ("native", "romaji", "english"):
                value = titles.get(key)
                if isinstance(value, str) and value.strip() and value.strip() != title:
                    extra.setdefault("alt_titles", []).append(value.strip())
        if anilist.get("synonyms"):
            extra["synonyms"] = [
                str(s).strip()
                for s in anilist.get("synonyms") or []
                if str(s).strip()
            ][:3]
        preview = item.get("image") or item.get("video")
        thumbnail = str(preview or "").strip()

        out.append(
            ImageSearchResult(
                source="trace.moe",
                title=title,
                url=url,
                similarity=similarity,
                author="",
                thumbnail=thumbnail,
                kind="anime",
                extra=extra,
            )
        )
        if len(out) >= max_results:
            break
    return out


# ---------- ascii2d ----------

class _Ascii2dParser(HTMLParser):
    """ascii2d 结果页解析:每个 ``div.row.item-box`` 是一条结果。

    每个 item-box 内含:
        ``.detail-box .info-box``  - 来源/作者/链接
        ``.detail-box h6 a``       - 标题(作品名)+ 标题链接(pixiv/twitter 等)
        ``img``                    - 缩略图

    我们只关心带外链的条目(纯本机缩图无意义)。
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[ImageSearchResult] = []
        self._depth_stack: list[str] = []
        self._cur_links: list[tuple[str, str]] = []  # (href, text)
        self._cur_thumb: str = ""
        self._capture_text = False
        self._text_buf: list[str] = []
        self._pending_href = ""

    def _enter_item(self) -> None:
        self._cur_links = []
        self._cur_thumb = ""

    def _flush_item(self) -> None:
        # 优先取第一条 absolute 外链(pixiv / twitter / ...)
        outbound: tuple[str, str] | None = None
        for href, text in self._cur_links:
            if not href:
                continue
            if href.startswith("/search/") or href.startswith("/about"):
                continue
            if href.startswith("http"):
                outbound = (href, text)
                break
        if outbound is None:
            self._enter_item()
            return
        href, label = outbound
        # 拼一个易读 title:作者 + 作品名(若 label 有)
        title = label.strip() or "ascii2d match"
        author = ""
        # ascii2d 通常把作者放在另一条链接里(后续的 /users/xxx 链接 text 是作者名)
        for h, t in self._cur_links[1:]:
            if "pixiv.net/users/" in h or "twitter.com/" in h:
                author = t.strip()
                break
        self.results.append(
            ImageSearchResult(
                source="ascii2d",
                title=title,
                url=href,
                similarity=0.0,  # ascii2d 不返回相似度
                author=author,
                thumbnail=self._cur_thumb,
                kind="artwork",
            )
        )
        self._enter_item()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name: value or "" for name, value in attrs}
        classes = set(attrs_map.get("class", "").split())
        if tag == "div" and "item-box" in classes:
            self._depth_stack.append("item")
            self._enter_item()
            return
        if not self._depth_stack:
            return
        if tag == "img":
            src = attrs_map.get("src", "").strip()
            if src and not self._cur_thumb:
                # ascii2d 缩图相对路径,补 origin
                if src.startswith("/"):
                    src = urljoin("https://ascii2d.net", src)
                self._cur_thumb = src
        elif tag == "a":
            href = attrs_map.get("href", "").strip()
            self._pending_href = href
            self._capture_text = True
            self._text_buf = []

    def handle_data(self, data: str) -> None:
        if self._capture_text:
            self._text_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_text:
            text = unescape("".join(self._text_buf)).strip()
            if self._pending_href:
                self._cur_links.append((self._pending_href, text))
            self._capture_text = False
            self._pending_href = ""
            self._text_buf = []
        elif tag == "div" and self._depth_stack and self._depth_stack[-1] == "item":
            # 不靠 class 精确闭合(浏览器宽容),用启发式:遇到下一个 item-box 时 flush。
            # 实际依赖 _enter_item 在每个 item-box start 时重置;这里偷懒只 pop。
            self._depth_stack.pop()
            self._flush_item()


async def _search_ascii2d(
    client: httpx.AsyncClient,
    image_url: str,
    *,
    max_results: int,
) -> list[ImageSearchResult]:
    try:
        # ascii2d /search/uri 接受 form 字段 uri,返回 302 → /search/color/{hash}
        response = await client.post(
            "https://ascii2d.net/search/uri",
            data={"uri": image_url},
            headers=_common_headers("https://ascii2d.net/"),
        )
    except httpx.HTTPError as exc:
        logger.info("ascii2d request failed: %s: %s", exc.__class__.__name__, exc)
        return []
    if response.status_code >= 400:
        logger.info(
            "ascii2d status=%d body=%s", response.status_code, response.text[:200]
        )
        return []
    parser = _Ascii2dParser()
    try:
        parser.feed(response.text)
    except Exception as exc:  # noqa: BLE001
        logger.info("ascii2d parse failed: %s", exc)
        return []
    return parser.results[:max_results]


# ---------- iqdb ----------

_IQDB_TR_SIMILARITY_RE = re.compile(r"(\d+)%\s+similarity", re.IGNORECASE)


class _IqdbParser(HTMLParser):
    """iqdb HTML:核心结构是 ``<div class="pages">`` 内多个 ``<div>`` 块,
    每块一个 ``<table>`` 含 thumbnail + 元数据 + similarity 行。

    我们只抓 ``<table>`` 内第一条 ``<a href="..."`` (booru/pixiv 链接) 和
    底部 ``XX% similarity`` 文本。
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[ImageSearchResult] = []
        self._in_pages = False
        self._in_table = False
        self._cur_href = ""
        self._cur_thumb = ""
        self._cur_text_chunks: list[str] = []
        self._cur_table_text: list[str] = []
        self._capture_text = False
        self._text_buf: list[str] = []

    def _reset_cur(self) -> None:
        self._cur_href = ""
        self._cur_thumb = ""
        self._cur_text_chunks = []
        self._cur_table_text = []

    def _flush_table(self) -> None:
        if not self._cur_href:
            self._reset_cur()
            return
        href = self._cur_href
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://iqdb.org", href)
        # similarity 在 table 内的纯文本里
        full_text = " ".join(self._cur_table_text)
        match = _IQDB_TR_SIMILARITY_RE.search(full_text)
        similarity = float(match.group(1)) if match else 0.0
        # 来源(Danbooru/Konachan...)在 td 文本里,粗略提取
        source_label = ""
        for token in full_text.split():
            for kw in ("Danbooru", "Konachan", "Yandere", "Gelbooru", "Sankaku", "Anime-Pictures", "Zerochan", "Mangadex"):
                if kw.lower() in token.lower():
                    source_label = kw
                    break
            if source_label:
                break
        self.results.append(
            ImageSearchResult(
                source="iqdb",
                title=source_label or "iqdb match",
                url=href,
                similarity=similarity,
                author="",
                thumbnail=self._cur_thumb,
                kind="artwork",
                extra={"raw_meta": full_text[:200]},
            )
        )
        self._reset_cur()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {name: value or "" for name, value in attrs}
        if tag == "div" and "pages" in attrs_map.get("class", ""):
            self._in_pages = True
            return
        if not self._in_pages:
            return
        if tag == "table":
            self._in_table = True
            self._reset_cur()
        if not self._in_table:
            return
        if tag == "a":
            href = attrs_map.get("href", "").strip()
            if href and not self._cur_href and not href.startswith("#"):
                self._cur_href = href
            self._capture_text = True
            self._text_buf = []
        elif tag == "img":
            src = attrs_map.get("src", "").strip()
            if src and not self._cur_thumb:
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = urljoin("https://iqdb.org", src)
                self._cur_thumb = src

    def handle_data(self, data: str) -> None:
        if not self._in_table:
            return
        text = data.strip()
        if text:
            self._cur_table_text.append(text)
        if self._capture_text:
            self._text_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_text:
            self._capture_text = False
            self._text_buf = []
        elif tag == "table" and self._in_table:
            self._flush_table()
            self._in_table = False
        elif tag == "div" and self._in_pages:
            # 不可靠的闭合检测,但 iqdb 的 .pages div 是文档级别,流末关掉一次即可。
            pass


async def _search_iqdb(
    client: httpx.AsyncClient,
    image_url: str,
    *,
    max_results: int,
) -> list[ImageSearchResult]:
    try:
        response = await client.get(
            "https://iqdb.org/",
            params={"url": image_url},
            headers=_common_headers("https://iqdb.org/"),
        )
    except httpx.HTTPError as exc:
        logger.info("iqdb request failed: %s: %s", exc.__class__.__name__, exc)
        return []
    if response.status_code >= 400:
        logger.info("iqdb status=%d body=%s", response.status_code, response.text[:200])
        return []
    parser = _IqdbParser()
    try:
        parser.feed(response.text)
    except Exception as exc:  # noqa: BLE001
        logger.info("iqdb parse failed: %s", exc)
        return []
    # iqdb 第一条往往是「Your image」上传回显,过滤掉 similarity=0 的同时保 top 1。
    real = [r for r in parser.results if r.similarity >= 1.0 or "iqdb" not in r.url.lower()]
    return real[:max_results]


# ---------- 顶层调度 ----------

_ENGINE_REGISTRY = {
    "saucenao": _search_saucenao,
    "tracemoe": _search_tracemoe,
    "trace.moe": _search_tracemoe,
    "ascii2d": _search_ascii2d,
    "iqdb": _search_iqdb,
}

_KIND_DEFAULT_ENGINES = {
    "anime": ["tracemoe", "saucenao"],
    "artwork": ["saucenao", "ascii2d", "iqdb"],
    "auto": ["saucenao", "ascii2d", "tracemoe"],
    "general": ["saucenao", "ascii2d", "tracemoe"],
}


def _normalize_engine_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = re.split(r"[\s,;，；]+", raw.strip())
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower().replace("trace.moe", "tracemoe")
        if not key or key in seen or key not in _ENGINE_REGISTRY:
            continue
        seen.add(key)
        out.append(key)
    return out


async def reverse_image_search(
    config: Config,
    image_url: str,
    *,
    kind: str = "auto",
    engines: list[str] | None = None,
    max_per_engine: int | None = None,
) -> tuple[list[ImageSearchResult], dict[str, str]]:
    """对单张图片跑反向搜索。

    返回 ``(results, errors)``:
        results - 按 similarity 倒序合并后的列表(保留 source);
                  同 url 去重(优先保留 similarity 高的那条)。
        errors  - {engine_name: 简短错误字符串};供 caller 告诉 AI 哪些引擎跪了。

    任何单引擎挂掉只会出现在 errors 里,不会影响其它引擎。
    """
    image_url = (image_url or "").strip()
    if not image_url:
        return [], {"_": "image_url 为空"}
    if not getattr(config, "catty_image_search_enabled", True):
        return [], {"_": "image_search 已被配置禁用"}

    kind_normalized = (kind or "auto").strip().lower()
    if kind_normalized not in _KIND_DEFAULT_ENGINES:
        kind_normalized = "auto"

    if engines:
        selected = _normalize_engine_list(engines)
    else:
        selected = _normalize_engine_list(_KIND_DEFAULT_ENGINES[kind_normalized])
    if not selected:
        selected = ["saucenao", "ascii2d"]

    timeout = float(
        getattr(config, "catty_image_search_request_timeout", None)
        or config.catty_request_timeout
        or 15.0
    )
    proxy = config.catty_http_proxy or None
    per_engine_max = max(
        int(max_per_engine or getattr(config, "catty_image_search_max_results", 5) or 5),
        1,
    )
    saucenao_key = str(
        getattr(config, "catty_image_search_saucenao_api_key", "") or ""
    ).strip()

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, proxy=proxy
    ) as client:
        tasks: dict[str, asyncio.Task[list[ImageSearchResult]]] = {}
        for engine in selected:
            func = _ENGINE_REGISTRY[engine]
            if engine == "saucenao":
                coro = func(client, image_url, api_key=saucenao_key, max_results=per_engine_max)
            else:
                coro = func(client, image_url, max_results=per_engine_max)
            tasks[engine] = asyncio.create_task(coro)
        # 等所有引擎,容忍单引擎异常
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)

    errors: dict[str, str] = {}
    # 收集时先不做去重——同一 URL 可能多个引擎打出不同 similarity,
    # 我们要保留最高的那条(便于 AI 拿到最自信的判断)。
    pooled: dict[str, ImageSearchResult] = {}
    for engine, outcome in zip(tasks.keys(), gathered):
        if isinstance(outcome, BaseException):
            errors[engine] = f"{outcome.__class__.__name__}: {outcome}"
            continue
        if not outcome:
            continue
        for r in outcome:
            key = (r.url or f"{r.source}:{r.title}").strip().lower()
            existing = pooled.get(key)
            if existing is None or r.similarity > existing.similarity:
                pooled[key] = r

    results = list(pooled.values())
    results.sort(key=lambda r: (r.similarity, r.source == "saucenao"), reverse=True)

    if not results and not errors:
        errors["_"] = f"所有引擎都没拿到匹配(engines={selected})"
    return results, errors


def format_image_search_summary(
    image_ref: str,
    results: list[ImageSearchResult],
    errors: dict[str, str],
    *,
    max_show: int = 6,
) -> str:
    """把搜索结果格式化成 AI 友好的 context 文本。

    AI 拿到后**禁止**照搬 JSON,要用猫娘人格挑 1-3 条最有信息量的复述。
    """
    lines: list[str] = []
    head = f"反向搜图结果(图片来源: {image_ref}):"
    if not results:
        head = f"反向搜图没拿到任何结果(图片: {image_ref})。"
    lines.append(head)
    for idx, r in enumerate(results[:max_show], 1):
        bits: list[str] = []
        if r.similarity > 0:
            bits.append(f"相似度 {r.similarity:.1f}%")
        if r.author:
            bits.append(f"作者 {r.author}")
        if r.kind == "anime":
            ep = r.extra.get("episode")
            from_sec = r.extra.get("from_seconds")
            if ep is not None:
                bits.append(f"第{ep}集")
            if from_sec is not None:
                try:
                    mins = int(float(from_sec) // 60)
                    secs = int(float(from_sec) % 60)
                    bits.append(f"~{mins:02d}:{secs:02d}")
                except (TypeError, ValueError):
                    pass
        meta = " / ".join(bits)
        line = f"{idx}. [{r.source}] {r.title}"
        if meta:
            line += f"  ({meta})"
        if r.url:
            line += f"\n   链接: {r.url}"
        lines.append(line)
    if errors:
        err_bits = [f"{k}={v}" for k, v in errors.items() if k != "_"]
        if err_bits:
            lines.append("(部分引擎失败: " + "; ".join(err_bits) + ")")
        elif "_" in errors:
            lines.append(f"(原因: {errors['_']})")
    lines.append(
        "AI 复述要求: 用笨猫人格挑 1-3 条最关键的(优先 similarity>80% 或主人明确想要的字段),"
        "不要照搬 JSON,不要复读相似度小数;禁止编造没出现在结果里的作者/番名/链接。"
    )
    return "\n".join(lines)
