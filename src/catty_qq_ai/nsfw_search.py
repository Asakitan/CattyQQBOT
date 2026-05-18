from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config


logger = logging.getLogger("nonebot").getChild("catty_qq_ai.nsfw_search")


_R18_NOISE_RE = re.compile(r"\b[rR][\s\-_]?18[gG]?\b")


def _clean_pixiv_query(query: str) -> str:
    cleaned = _R18_NOISE_RE.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or query.strip()


@dataclass(slots=True)
class NsfwResult:
    title: str
    url: str
    source: str
    snippet: str = ""
    media_urls: list[str] = field(default_factory=list)
    kind: str = ""
    score: int = 0
    bookmark_count: int = 0
    like_count: int = 0
    view_count: int = 0
    is_r18: bool = False
    artist: str = ""


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)


def _common_headers(referer: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.6",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _normalize_pixiv_cookie(cookie: str) -> str:
    cookie = (cookie or "").strip()
    if not cookie:
        return ""
    if "=" not in cookie:
        return f"PHPSESSID={cookie}"
    return cookie


def _pixiv_headers(cookie: str = "") -> dict[str, str]:
    headers = _common_headers("https://www.pixiv.net/")
    headers["Accept"] = "application/json"
    normalized = _normalize_pixiv_cookie(cookie)
    if normalized:
        headers["Cookie"] = normalized
    return headers


# ---------- iwara (video) ----------

async def _search_iwara(
    client: httpx.AsyncClient, query: str, *, max_results: int
) -> list[NsfwResult]:
    try:
        response = await client.get(
            "https://api.iwara.tv/videos",
            params={"query": query, "sort": "relevance", "limit": max_results, "rating": "all"},
            headers=_common_headers("https://www.iwara.tv/"),
        )
    except httpx.HTTPError:
        return _iwara_fallback_links(query)
    if response.status_code >= 400:
        return _iwara_fallback_links(query)
    try:
        data = response.json()
    except ValueError:
        return _iwara_fallback_links(query)
    if not isinstance(data, dict):
        return _iwara_fallback_links(query)
    items = data.get("results")
    if not isinstance(items, list):
        return _iwara_fallback_links(query)
    results: list[NsfwResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id") or "").strip()
        slug = str(item.get("slug") or "").strip()
        title = str(item.get("title") or slug or video_id).strip()
        if not video_id:
            continue
        url = f"https://www.iwara.tv/video/{video_id}/{slug}" if slug else f"https://www.iwara.tv/video/{video_id}"
        snippet_parts: list[str] = []
        rating = str(item.get("rating") or "").strip()
        if rating:
            snippet_parts.append(f"分级:{rating}")
        author = ""
        user = item.get("user")
        if isinstance(user, dict):
            author = str(user.get("name") or user.get("username") or "").strip()
            if author:
                snippet_parts.append(f"作者:{author}")
        likes = int(item.get("numLikes") or 0)
        views = int(item.get("numViews") or 0)
        if likes:
            snippet_parts.append(f"赞:{likes}")
        if views:
            snippet_parts.append(f"播放:{views}")
        results.append(
            NsfwResult(
                title=title,
                url=url,
                source="iwara",
                snippet=" / ".join(snippet_parts),
                kind="video",
                like_count=likes,
                view_count=views,
                is_r18=rating.lower() in {"ecchi", "explicit", "r18"},
                artist=author,
                score=likes * 3 + views // 10,
            )
        )
        if len(results) >= max_results:
            break
    if not results:
        return _iwara_fallback_links(query)
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _iwara_fallback_links(query: str) -> list[NsfwResult]:
    encoded = quote(query)
    return [
        NsfwResult(
            title=f"iwara 搜索：{query}",
            url=f"https://www.iwara.tv/search?type=video&query={encoded}",
            source="iwara",
            snippet="API 没拿到结果，给主人贴搜索页直链",
            kind="video",
        )
    ]


# ---------- kemono (fallback) ----------

async def _search_kemono(
    client: httpx.AsyncClient,
    query: str,
    *,
    kind: str,
    max_results: int,
) -> list[NsfwResult]:
    try:
        response = await client.get(
            "https://kemono.cr/api/v1/posts",
            params={"q": query, "o": 0},
            headers=_common_headers("https://kemono.cr/"),
        )
    except httpx.HTTPError:
        return []
    if response.status_code >= 400:
        return []
    try:
        data = response.json()
    except ValueError:
        return []
    items: list[dict[str, Any]] = []
    if isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        raw = data.get("posts") or data.get("results") or []
        if isinstance(raw, list):
            items = [item for item in raw if isinstance(item, dict)]
    results: list[NsfwResult] = []
    for item in items:
        post_id = str(item.get("id") or "").strip()
        service = str(item.get("service") or "").strip()
        user = str(item.get("user") or "").strip()
        title = str(item.get("title") or post_id or "kemono post").strip()
        if not post_id or not service or not user:
            continue
        url = f"https://kemono.cr/{service}/user/{user}/post/{post_id}"
        media_urls: list[str] = []
        file_obj = item.get("file")
        if isinstance(file_obj, dict):
            path = str(file_obj.get("path") or "").strip()
            if path:
                media_urls.append("https://kemono.cr/data" + path if path.startswith("/") else path)
        attachments = item.get("attachments")
        if isinstance(attachments, list):
            for attach in attachments:
                if not isinstance(attach, dict):
                    continue
                path = str(attach.get("path") or "").strip()
                if not path:
                    continue
                full = "https://kemono.cr/data" + path if path.startswith("/") else path
                if full not in media_urls:
                    media_urls.append(full)
                if len(media_urls) >= 4:
                    break
        snippet_parts = [f"平台:{service}", f"作者:{user}"]
        results.append(
            NsfwResult(
                title=title,
                url=url,
                source="kemono",
                snippet=" / ".join(snippet_parts),
                media_urls=media_urls,
                kind=kind,
                artist=user,
            )
        )
        if len(results) >= max_results:
            break
    return results


# ---------- pixiv (image, primary) ----------

async def _pixiv_search_ids(
    client: httpx.AsyncClient,
    query: str,
    *,
    cookie: str,
    page: int = 1,
    s_mode: str = "s_tag",
) -> tuple[list[dict[str, Any]], str]:
    """返回 (作品列表, 失败原因)。失败原因为空字符串表示成功。

    s_mode ∈ {s_tag, s_tag_full, s_tc}
    用 /ajax/search/artworks/{tag} 端点抓所有插画+漫画+动图，对应 web 的 type=illust_ugoira+manga。
    """
    encoded = quote(query)
    url = (
        f"https://www.pixiv.net/ajax/search/artworks/{encoded}"
        f"?word={encoded}&mode=r18&p={page}&order=date_d&s_mode={s_mode}"
    )
    response = await client.get(url, headers=_pixiv_headers(cookie))
    logger.info(
        "pixiv search status=%s query=%r s_mode=%s cookie=%s",
        response.status_code,
        query,
        s_mode,
        "yes" if cookie else "no",
    )
    if response.status_code == 403:
        return [], "pixiv 返回 403，cookie 失效或被风控"
    if response.status_code == 404:
        return [], "pixiv 找不到该标签"
    if response.status_code >= 400:
        return [], f"pixiv 搜索接口返回 {response.status_code}"
    try:
        data = response.json()
    except ValueError:
        return [], "pixiv 返回非 JSON（可能被 Cloudflare 拦截）"
    if isinstance(data, dict) and data.get("error"):
        message = str(data.get("message") or "pixiv 接口报错")
        return [], f"pixiv 接口报错：{message}"
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        return [], "pixiv 响应缺少 body 字段"
    # /ajax/search/artworks 返回 illustManga 字段，含插画+漫画+动图
    container = body.get("illustManga") or body.get("illust") or body.get("manga") or {}
    if not isinstance(container, dict):
        return [], "pixiv 响应缺少作品容器字段"
    raw = container.get("data")
    if not isinstance(raw, list):
        return [], "pixiv 响应 data 不是数组"
    works = [item for item in raw if isinstance(item, dict)]
    if not works:
        total = container.get("total")
        if total == 0:
            return [], f"pixiv 上 #{query} 标签下一张 R-18 作品都没人画（s_mode={s_mode}，作品数=0）"
        return [], "pixiv 搜索为空，可能账号未开启 R-18 显示或关键词无作品"
    return works, ""


async def _pixiv_illust_detail(
    client: httpx.AsyncClient, illust_id: str, *, cookie: str
) -> dict[str, Any] | None:
    url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
    try:
        response = await client.get(url, headers=_pixiv_headers(cookie))
    except httpx.HTTPError as exc:
        logger.warning("pixiv illust detail HTTP error id=%s: %s", illust_id, exc)
        return None
    if response.status_code >= 400:
        logger.info("pixiv illust detail status=%s id=%s", response.status_code, illust_id)
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("error"):
        logger.info(
            "pixiv illust detail error id=%s message=%s",
            illust_id,
            data.get("message"),
        )
        return None
    body = data.get("body") if isinstance(data, dict) else None
    return body if isinstance(body, dict) else None


async def _pixiv_illust_pages(
    client: httpx.AsyncClient, illust_id: str, *, cookie: str
) -> list[dict[str, Any]]:
    url = f"https://www.pixiv.net/ajax/illust/{illust_id}/pages"
    try:
        response = await client.get(url, headers=_pixiv_headers(cookie))
    except httpx.HTTPError:
        return []
    if response.status_code >= 400:
        return []
    try:
        data = response.json()
    except ValueError:
        return []
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, list):
        return []
    return [page for page in body if isinstance(page, dict)]


def _pixiv_fallback(query: str, reason: str = "") -> list[NsfwResult]:
    encoded = quote(query)
    snippet = "贴搜索页直链让主人手动看"
    if reason:
        snippet = f"{reason}，{snippet}"
    return [
        NsfwResult(
            title=f"pixiv R18 搜索：{query}",
            url=f"https://www.pixiv.net/tags/{encoded}/illustrations?mode=r18",
            source="pixiv",
            snippet=snippet,
            kind="image",
        )
    ]


async def _pixiv_count_all_mode(
    client: httpx.AsyncClient, query: str, *, cookie: str
) -> int | None:
    """用 mode=all 看下这个 tag 到底存不存在，返回作品总数；失败返回 None。"""
    encoded = quote(query)
    url = (
        f"https://www.pixiv.net/ajax/search/artworks/{encoded}"
        f"?word={encoded}&mode=all&p=1&order=date_d&s_mode=s_tag"
    )
    try:
        response = await client.get(url, headers=_pixiv_headers(cookie))
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    body = data.get("body")
    if not isinstance(body, dict):
        return None
    container = body.get("illustManga") or body.get("illust") or body.get("manga") or {}
    if not isinstance(container, dict):
        return None
    total = container.get("total")
    return int(total) if isinstance(total, int) else None


async def _pixiv_search_with_smode_fallback(
    client: httpx.AsyncClient,
    query: str,
    *,
    cookie: str,
) -> tuple[list[dict[str, Any]], str, str]:
    """对单个 query 依次尝试 s_tag → s_tag_full → s_tc，返回 (works, error_reason, used_smode)。"""
    last_error = ""
    for s_mode in ("s_tag", "s_tag_full", "s_tc"):
        try:
            works, err = await _pixiv_search_ids(
                client, query, cookie=cookie, page=1, s_mode=s_mode
            )
        except httpx.HTTPError as exc:
            return [], f"pixiv 接口连接失败：{exc}", s_mode
        if works:
            return works, "", s_mode
        last_error = err or "搜索为空"
        # 403 / cookie 失效这种错误，换 s_mode 也救不回来，提前结束
        if "403" in last_error or "cookie" in last_error.lower():
            return [], last_error, s_mode
    return [], last_error, "s_tc"


_PIXIV_IMAGE_SIZE_FALLBACK = {
    "original": ("original", "regular", "small"),
    "regular": ("regular", "small", "original"),
    "small": ("small", "regular", "original"),
    "thumb": ("thumb_mini", "small", "regular"),
    "thumb_mini": ("thumb_mini", "small", "regular"),
}


def _pick_pixiv_image_url(urls: dict[str, Any], preferred_size: str) -> str:
    if not isinstance(urls, dict):
        return ""
    chain = _PIXIV_IMAGE_SIZE_FALLBACK.get(preferred_size, _PIXIV_IMAGE_SIZE_FALLBACK["regular"])
    for key in chain:
        value = str(urls.get(key) or "").strip()
        if value:
            return value
    return ""


async def _search_pixiv_single(
    client: httpx.AsyncClient,
    query: str,
    *,
    cookie: str,
    max_results: int,
    preferred_size: str = "regular",
    rank_pool_size: int = 12,
) -> tuple[list[NsfwResult], str]:
    """对单个 query 完整跑一轮 pixiv 搜索（含 s_mode fallback）。
    返回 (NsfwResult 列表, 失败原因)。命中时 error 为空，并且 results 含 media_urls。
    没命中时 results 为空（不再返回 fallback 提示），由调用方决定是否再试下一个候选。
    """
    cleaned_query = _clean_pixiv_query(query)
    if cleaned_query != query:
        logger.info("pixiv query cleaned: %r -> %r", query, cleaned_query)
    works, search_error, used_smode = await _pixiv_search_with_smode_fallback(
        client, cleaned_query, cookie=cookie
    )
    if not works:
        if "作品都没人画" in search_error or "搜索为空" in search_error or "作品数=0" in search_error:
            all_total = await _pixiv_count_all_mode(client, cleaned_query, cookie=cookie)
            if all_total == 0:
                search_error = (
                    f"pixiv 完全没有 #{cleaned_query} 这个标签的作品（全年龄+R-18 都=0）"
                )
            elif isinstance(all_total, int) and all_total > 0:
                search_error = (
                    f"pixiv 上 #{cleaned_query} 共 {all_total} 张全年龄作品，但 R-18=0"
                )
        return [], search_error or "pixiv 没返回作品"
    logger.info(
        "pixiv hit query=%r used_smode=%s works=%d",
        cleaned_query,
        used_smode,
        len(works),
    )

    r18_works = [w for w in works if int(w.get("xRestrict") or 0) >= 1]
    pool = (r18_works or works)[:rank_pool_size]

    logger.info(
        "pixiv search returned %d works, %d are R-18 (cleaned_query=%r)",
        len(works),
        len(r18_works),
        cleaned_query,
    )
    detailed: list[NsfwResult] = []
    skipped_detail = 0
    for work in pool:
        illust_id = str(work.get("id") or "").strip()
        if not illust_id:
            continue
        body = await _pixiv_illust_detail(client, illust_id, cookie=cookie)
        if not body:
            skipped_detail += 1
            continue
        bookmark = int(body.get("bookmarkCount") or 0)
        like = int(body.get("likeCount") or 0)
        view = int(body.get("viewCount") or 0)
        x_restrict = int(body.get("xRestrict") or work.get("xRestrict") or 0)
        score = bookmark * 4 + like + view // 50
        if x_restrict >= 1:
            score += 200
        title = str(body.get("illustTitle") or body.get("title") or work.get("title") or illust_id).strip()
        author = str(body.get("userName") or work.get("userName") or "").strip()
        urls = body.get("urls") if isinstance(body.get("urls"), dict) else {}
        primary_url = ""
        if isinstance(urls, dict):
            primary_url = _pick_pixiv_image_url(urls, preferred_size)
        media_urls: list[str] = [primary_url] if primary_url else []
        page_count = int(body.get("pageCount") or 1)
        if page_count > 1 and primary_url:
            pages = await _pixiv_illust_pages(client, illust_id, cookie=cookie)
            for page in pages[:3]:
                page_urls = page.get("urls") if isinstance(page.get("urls"), dict) else {}
                if not isinstance(page_urls, dict):
                    continue
                candidate = _pick_pixiv_image_url(page_urls, preferred_size)
                if candidate and candidate not in media_urls:
                    media_urls.append(candidate)
        detailed.append(
            NsfwResult(
                title=title,
                url=f"https://www.pixiv.net/artworks/{illust_id}",
                source="pixiv",
                snippet=(
                    f"作者:{author} / 收藏:{bookmark} 赞:{like} 浏览:{view}"
                    + (" / R-18" if x_restrict == 1 else (" / R-18G" if x_restrict == 2 else ""))
                ),
                media_urls=media_urls,
                kind="image",
                score=score,
                bookmark_count=bookmark,
                like_count=like,
                view_count=view,
                is_r18=x_restrict >= 1,
                artist=author,
            )
        )

    if not detailed:
        reason = "pixiv 详情接口都失败了"
        if skipped_detail:
            reason += f"（{skipped_detail}/{len(pool)} 项详情拉取失败，cookie 可能没解锁 R-18）"
        return [], reason

    detailed.sort(key=lambda r: (r.is_r18, r.score), reverse=True)
    logger.info(
        "pixiv ranked top score=%d (bookmark=%d like=%d view=%d r18=%s) for %r",
        detailed[0].score,
        detailed[0].bookmark_count,
        detailed[0].like_count,
        detailed[0].view_count,
        detailed[0].is_r18,
        cleaned_query,
    )
    return detailed[:max_results], ""


_QUERY_SPLIT_RE = re.compile(r"[,;；、]+")


def _split_query_candidates(query: str) -> list[str]:
    parts = [p.strip() for p in _QUERY_SPLIT_RE.split(query) if p.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique.append(part)
    return unique or [query.strip()]


async def _search_pixiv(
    client: httpx.AsyncClient,
    query: str,
    *,
    cookie: str,
    max_results: int,
    preferred_size: str = "regular",
) -> list[NsfwResult]:
    """主入口：query 里支持 `,;；、` 分隔多个候选（中文/日文/英文），依次尝试，第一个命中的就用。"""
    if not cookie:
        return _pixiv_fallback(query, "未配置 pixiv cookie")
    candidates = _split_query_candidates(query)
    if len(candidates) > 1:
        logger.info("pixiv multi-query candidates: %r", candidates)
    last_reason = ""
    for candidate in candidates:
        try:
            results, reason = await _search_pixiv_single(
                client,
                candidate,
                cookie=cookie,
                max_results=max_results,
                preferred_size=preferred_size,
            )
        except httpx.HTTPError as exc:
            last_reason = f"pixiv 接口连接失败：{exc}"
            logger.warning("pixiv single search HTTP error for %r: %s", candidate, exc)
            continue
        if results:
            return results
        last_reason = reason or "pixiv 没返回作品"
        logger.info("pixiv candidate %r failed: %s", candidate, last_reason)
    return _pixiv_fallback(
        candidates[0],
        f"试过候选 {candidates} 都没命中，最后原因：{last_reason}",
    )


# ---------- top-level dispatch ----------

async def search_nsfw(
    config: Config,
    query: str,
    *,
    kind: str,
    max_results: int | None = None,
) -> list[NsfwResult]:
    """kind ∈ {video, image}. 视频以 iwara 为主，图片以 pixiv 为主，kemono 仅作兜底。

    max_results 不传时用 config.catty_nsfw_search_max_results；调用方需要更大候选池
    （例如要跳过已发过的图）时可以显式覆盖。
    """
    query = query.strip()
    if not query or not getattr(config, "catty_nsfw_search_enabled", False):
        return []
    kind_normalized = (kind or "").strip().lower()
    if kind_normalized not in {"video", "image"}:
        kind_normalized = "video"
    timeout = (
        getattr(config, "catty_nsfw_search_request_timeout", None)
        or config.catty_request_timeout
    )
    if max_results is None:
        max_results = max(int(getattr(config, "catty_nsfw_search_max_results", 4) or 4), 1)
    else:
        max_results = max(int(max_results), 1)
    proxy = config.catty_http_proxy or None
    candidates = _split_query_candidates(query)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=proxy) as client:
        if kind_normalized == "video":
            iwara_collected: list[NsfwResult] = []
            seen_urls: set[str] = set()
            for cand in candidates:
                got = await _search_iwara(client, cand, max_results=max_results)
                real = [r for r in got if r.url and "/search?" not in r.url]
                for item in real:
                    if item.url in seen_urls:
                        continue
                    seen_urls.add(item.url)
                    iwara_collected.append(item)
                if len(iwara_collected) >= max_results:
                    break
            if len(iwara_collected) >= max_results:
                return iwara_collected[:max_results]
            for cand in candidates:
                kemono_results = await _search_kemono(
                    client, cand, kind="video", max_results=max_results
                )
                for item in kemono_results:
                    if item.url in seen_urls:
                        continue
                    seen_urls.add(item.url)
                    iwara_collected.append(item)
                if len(iwara_collected) >= max_results:
                    break
            if iwara_collected:
                return iwara_collected[:max_results]
            # 全空：返回 iwara 搜索页 fallback
            return _iwara_fallback_links(candidates[0])
        # image kind
        pixiv_cookie = getattr(config, "catty_nsfw_pixiv_cookie", "") or ""
        preferred_size = str(getattr(config, "catty_nsfw_pixiv_image_size", "regular") or "regular").strip().lower()
        primary = await _search_pixiv(
            client,
            query,
            cookie=pixiv_cookie,
            max_results=max_results,
            preferred_size=preferred_size,
        )
        real_primary = [r for r in primary if r.media_urls]
        if real_primary:
            return real_primary[:max_results]
        # pixiv 全军覆没：试 kemono（也按候选 query 顺序尝试）
        for cand in candidates:
            kemono_results = await _search_kemono(
                client, cand, kind="image", max_results=max_results
            )
            if kemono_results:
                return kemono_results[:max_results]
        return primary[:max_results]


# ---------- image download helpers ----------

async def download_image_bytes(
    config: Config,
    url: str,
    *,
    source: str = "",
    cookie: str = "",
) -> tuple[bytes, str]:
    """下载 NSFW 图片，处理 pixiv referer / cookie。返回 (bytes, content_type)。"""
    timeout = (
        getattr(config, "catty_nsfw_search_request_timeout", None)
        or config.catty_request_timeout
    )
    proxy = config.catty_http_proxy or None
    if "pximg.net" in url or source == "pixiv":
        headers = _pixiv_headers(cookie or getattr(config, "catty_nsfw_pixiv_cookie", "") or "")
    elif "iwara" in url or source == "iwara":
        headers = _common_headers("https://www.iwara.tv/")
    elif "kemono" in url or source == "kemono":
        headers = _common_headers("https://kemono.cr/")
    else:
        headers = _common_headers(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=proxy) as client:
        response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.content, response.headers.get("content-type", "")


def format_nsfw_search_context(
    query: str,
    kind: str,
    results: list[NsfwResult],
    *,
    images_already_sent: int = 0,
    is_private: bool = True,
) -> str:
    kind_cn = "视频" if (kind or "").lower() == "video" else "图片"
    audience = "主人" if is_private else "群友"
    delivery = "私聊里" if is_private else "群里"
    if not results:
        return (
            f"主 AI 请求过 NSFW {kind_cn}搜索「{query}」，但插件没拿到任何可用结果。"
            "请用人格自然说明这次没搜到，可以撒娇让主人换关键词，禁止编造链接。"
        )
    lines: list[str] = []
    if (kind or "").lower() == "image" and images_already_sent > 0:
        lines.append(
            f"NSFW 图片搜索已经按主 AI 请求把「{query}」最高评价的 {images_already_sent} 张 R-18 图片直接发到{delivery}了。"
            "现在请只补一句猫娘人格的短文案（1-2 句，可以害羞嘴硬撒娇），不要再贴链接、不要复述说明、"
            "不要套用安全免责模板。最多附上一句作品来源（例如『来自 pixiv，作者 xxx』）。"
        )
    elif (kind or "").lower() == "image":
        lines.append(
            f"NSFW 图片搜索结果（关键词「{query}」）。pixiv 图片下载失败，已经退化成贴搜索页直链。"
            "请用猫娘人格挑 1-2 个链接抛出去，简短自然，禁止编造不存在的 URL。"
        )
    else:
        lines.append(
            f"NSFW 视频搜索结果（关键词「{query}」），iwara 为主。"
            f"请用猫娘人格挑 1-3 个最贴的视频链接抛给{audience}，可以加几句害羞嘴硬的短评，但不要套用安全免责模板，"
            "也不要编造不存在的 URL。"
        )
    for index, item in enumerate(results, 1):
        meta = item.snippet or ""
        lines.append(f"{index}. [{item.source}] {item.title}\n链接：{item.url}" + (f"\n备注：{meta}" if meta else ""))
    return "\n".join(lines)
