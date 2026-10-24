"""知识库缺口自动补网 (主人 2026-10-25).

被问到的东西如果本地知识库 (群语料 / 群笔记 / scope lorebook / 游戏事实 / 人物细节)
里没有, 不等模型自己想起来调 ``catty_web_search``: 这里在拼 prompt 之前自己探一遍,
缺口就搜索 + 抓正文, 拼成【联网补全】块交给主 AI 作答。

设计取舍:
- 判定全本地, 网络动作受 cooldown / 小时预算 / query 缓存三重限流;
- 判定宁可稍宽 (诉求就是"不在知识库就自己联网"), 限流靠冷启动预算兜;
- 任何一步失败都静默返回空 context, 绝不打断主回复。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import asyncio
import html as _html
import logging
import re
import time
from typing import Any, Callable, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from .web_search import WebSearchResult, search_web


_logger = logging.getLogger("catty_qq_ai.auto_web_fetch")


# ── 判定词表 ────────────────────────────────────────────────────────────
# 「知识类问句」: 出现任何一个就说明这句可能要吃外部事实
_KNOWLEDGE_HINTS = (
    "是什么", "是啥", "什么意思", "啥意思", "什么梗", "怎么用", "怎么弄", "怎么办",
    "为什么", "为啥", "多少钱", "价格", "什么时候", "多久", "在哪", "哪里", "谁",
    "介绍", "科普", "区别", "对比", "攻略", "教程", "原理", "配置", "要求",
    "怎么买", "怎么下", "下载地址", "官网", "版本", "型号", "参数", "评价", "值得",
    "开服", "上线", "发售", "发布", "更新", "赛季", "赛程", "比分", "汇率",
    "股价", "票价", "新闻", "热搜",
)
# 「时效信息」: 知识库里的旧记录不算数, 与知识类词同时出现才触发
_FRESH_HINTS = (
    "最新", "现在", "目前", "最近", "今天", "今年", "这周", "本周", "刚刚",
    "新闻", "热搜", "更新", "发布", "上线", "开服", "赛季", "赛程", "比分",
    "汇率", "股价", "票价", "发售", "什么时候出", "多少钱", "价格",
)
# 「用户明确要求查」: 不看知识库直接搜
_SEARCH_HINTS = (
    "查一下", "查查", "帮我查", "搜一下", "搜搜", "搜下", "百度", "谷歌",
    "google", "联网", "查资料", "查查资料",
)
# 明显不是查资料的请求 (生图/创作/社交/日常/命令), 直接放过
_SKIP_HINTS = (
    "画一张", "画个", "画一", "来张图", "生图", "出图", "写一段", "写个",
    "写一篇", "接龙", "角色扮演", "签到", "积分", "排行榜", "点歌", "复读",
    "吃什么", "喝什么", "穿什么", "干嘛", "干什么", "做什么", "去哪", "无聊",
    "好困", "饿了", "睡觉", "打游戏吗",
)
_SOCIAL_ONLY_RE = re.compile(
    r"^(?:哈{2,}|6{2,}|草+|呃+|额+|嗯+|哦+|噢+|好+|行+|早|早安|晚安|在吗|在不在|"
    r"你好|您好|谢谢|谢了|么么|贴贴|抱抱|草泥马|牛逼|牛逼啊|\?+|？+)$"
)
_QUOTED_RE = re.compile(r"[《「『【\[“\"']([^》」』】\]”\"']{2,24})[》」』】\]”\"']")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9][A-Za-z0-9._+-]{1,23}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# 抽实体词时先剥掉这些虚词/问句词, 剩下的连续中文段才当候选词
_CJK_STOP_TERMS = (
    "请问", "帮我", "帮忙", "一下", "什么", "怎么", "怎样", "为什么", "为啥", "哪个",
    "哪里", "哪些", "多少", "多久", "什么时候", "是不是", "有没有", "能不能", "可不可以",
    "这个", "那个", "这些", "那些", "现在", "目前", "最近", "知道", "告诉", "介绍",
    "解释", "大概", "相关", "东西", "内容", "然后", "可以", "我们", "你们", "他们",
    "自己", "就是", "还是", "已经", "感觉", "觉得", "时候", "机机", "笨猫", "猫猫",
)
_STOP_TERMS = frozenset(
    {
        "?", "？", "ok", "hhh", "www", "com", "the", "and", "for", "you", "are",
        "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "啥", "谁", "哪", "几",
    }
)

# 抓正文时可接受的 content-type; 其它 (pdf/zip/图片) 直接跳过
_SKIP_URL_SUFFIXES = (
    ".pdf", ".zip", ".rar", ".7z", ".apk", ".exe", ".dmg", ".iso",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mp3", ".svg",
)
# Google News RSS 的条目是 JS 跳转壳, 抓不到正文; 这类直接跳到下一条
_UNFETCHABLE_HOSTS = ("news.google.com",)


@dataclass(slots=True)
class AutoFetchOutcome:
    """一次自动补网的结果。``skipped`` 非空表示没有产生可注入内容。"""

    triggered: bool = False
    ok: bool = False
    query: str = ""
    reason: str = ""
    skipped: str = ""
    terms: tuple[str, ...] = ()
    pages: list[dict[str, str]] = field(default_factory=list)
    snippets: list[dict[str, str]] = field(default_factory=list)
    context: str = ""
    from_cache: bool = False


# ── 进程内状态 (冷却 / 预算 / 缓存) ─────────────────────────────────────
_cooldowns: dict[str, float] = {}
_cache: dict[str, tuple[float, AutoFetchOutcome]] = {}
_hour_bucket: int = -1
_hour_count: int = 0
_CACHE_MAX_ENTRIES = 256


def _cache_put(key: str, outcome: AutoFetchOutcome, now: float) -> None:
    _cache[key] = (now, outcome)
    if len(_cache) <= _CACHE_MAX_ENTRIES:
        return
    for stale_key, _value in sorted(_cache.items(), key=lambda item: item[1][0])[
        : len(_cache) - _CACHE_MAX_ENTRIES
    ]:
        _cache.pop(stale_key, None)


def reset_state() -> None:
    """测试用: 清空冷却/预算/缓存。"""
    global _hour_bucket, _hour_count
    _cooldowns.clear()
    _cache.clear()
    _hour_bucket = -1
    _hour_count = 0


# ── 判定 ────────────────────────────────────────────────────────────────
def extract_terms(text: str, *, limit: int = 4) -> list[str]:
    """抽候选实体词: 引号/书名号内 > 带数字或较长的英文 > 剥掉虚词后的中文段。"""
    raw = str(text or "")
    terms: list[str] = []

    def _push(value: str) -> None:
        term = value.strip(" \t\r\n,，。.!！?？:：;；、()（）[]【】<>《》\"'“”‘’~～")
        if len(term) < 2 or term.casefold() in _STOP_TERMS:
            return
        if not any(ch.isalnum() for ch in term):
            return
        if term not in terms:
            terms.append(term)

    for match in _QUOTED_RE.finditer(raw):
        _push(match.group(1))
    for match in _LATIN_RE.finditer(raw):
        token = match.group(0)
        if any(ch.isdigit() for ch in token) or len(token) >= 4:
            _push(token)
    for run in _CJK_RE.findall(raw):
        cleaned = run
        for stop in _CJK_STOP_TERMS:
            cleaned = cleaned.replace(stop, " ")
        for chunk in re.split(r"\s+", cleaned):
            chunk = chunk.strip("的了着过地得在是把被和与及就都也还")
            if 2 <= len(chunk) <= 12:
                _push(chunk)
    return terms[: max(int(limit), 1)]


def _has_entity(text: str) -> bool:
    if _QUOTED_RE.search(text):
        return True
    for match in _LATIN_RE.finditer(text):
        token = match.group(0)
        if any(ch.isdigit() for ch in token) or len(token) >= 4:
            return True
    return False


def should_auto_fetch(text: str) -> tuple[bool, str]:
    """判断这句是否值得联网补。返回 (是否需要外部事实, 原因/跳过原因)。"""
    clean = str(text or "").strip()
    if not clean:
        return False, "empty"
    if _SOCIAL_ONLY_RE.match(clean):
        return False, "social"
    lowered = clean.casefold()
    if any(hint in lowered for hint in _SKIP_HINTS):
        return False, "skip_intent"
    if len(clean) < 4:
        return False, "too_short"
    entity = _has_entity(clean)
    if any(hint in lowered for hint in _SEARCH_HINTS):
        return True, "explicit_search"
    fresh = any(hint in lowered for hint in _FRESH_HINTS)
    knowledge = any(hint in lowered for hint in _KNOWLEDGE_HINTS)
    if fresh and (entity or knowledge):
        return True, "fresh_info"
    if knowledge and (entity or len(clean) >= 6):
        return True, "kb_question"
    if entity and re.search(r"怎么样|好用|值得|哪个好|能不能|是不是", clean):
        return True, "entity_question"
    return False, "no_signal"


def build_query(text: str, terms: Sequence[str]) -> str:
    """把用户原话收成一条搜索词 (去 @/去链接/去前缀/限长)。"""
    cleaned = _URL_RE.sub(" ", str(text or ""))
    cleaned = re.sub(r"@\S+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\r\n,，。.!！?？~～:：")
    for prefix in ("机机", "笨猫", "猫猫"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip(" ,，。.!！?？:：")
    if len(cleaned) <= 48:
        return cleaned[:80]
    joined = " ".join(term for term in list(terms)[:2] if term).strip()
    return (joined or cleaned)[:80]


def _normalize_key(query: str) -> str:
    return re.sub(r"\s+", " ", query.casefold()).strip()


def _matches_terms(text: str, terms: Sequence[str]) -> bool:
    """结果/正文里至少要出现一个候选词, 否则当无关结果丢掉 (搜索后端偶尔瞎给)。"""
    checked = [term for term in terms if len(term) >= 2]
    if not checked:
        return True
    haystack = str(text or "").casefold()
    return any(term.casefold() in haystack for term in checked)


# ── 本地知识库探针 ──────────────────────────────────────────────────────
def knowledge_base_hit(
    term: str,
    *,
    memory_store: Any,
    scope_lorebook_store: Any = None,
    user_details_store: Any = None,
    scope_key: str = "",
    persona_name: str | None = None,
    user_id: str = "",
    group_id: str = "",
) -> bool:
    """本地知识库里是否有 term 的记录 (群语料/笔记/lorebook/游戏事实/人物细节)。

    纯内存查询, 不发网络请求; 任何一路失败都当没命中。
    """
    needle = str(term or "").strip().casefold()
    if len(needle) < 2:
        return False
    if memory_store is not None:
        try:
            recalled = memory_store.recall(
                group_id=group_id,
                user_id=user_id if group_id else "",
                keywords=needle,
                limit=3,
            )
        except Exception:  # noqa: BLE001
            recalled = None
        if isinstance(recalled, dict):
            if int(recalled.get("match_count") or 0) > 0:
                return True
            if needle in str(recalled.get("long_term_summary") or "").casefold():
                return True
        try:
            notes = memory_store.recall_notes(user_id=user_id, group_id=group_id, limit=20)
        except Exception:  # noqa: BLE001
            notes = None
        if isinstance(notes, dict):
            for bucket in notes.values():
                if not isinstance(bucket, list):
                    continue
                for note in bucket:
                    if isinstance(note, dict) and needle in str(note.get("text") or "").casefold():
                        return True
        try:
            game_name = memory_store.game_for_group(group_id) if group_id else ""
            if game_name:
                facts = memory_store.recall_game(game_name, keywords=needle, limit=1)
                if isinstance(facts, dict) and int(facts.get("match_count") or 0) > 0:
                    return True
        except Exception:  # noqa: BLE001
            pass
    if scope_lorebook_store is not None and scope_key:
        try:
            entries = scope_lorebook_store.list_entries(scope_key, persona=persona_name)
        except Exception:  # noqa: BLE001
            entries = []
        for entry in entries or []:
            haystack = (
                f"{getattr(entry, 'content', '')}\n"
                f"{' '.join(getattr(entry, 'keys', ()) or ())}"
            )
            if needle in haystack.casefold():
                return True
    if user_details_store is not None and user_id:
        try:
            details = user_details_store.get_details(user_id)
        except Exception:  # noqa: BLE001
            details = {}
        if isinstance(details, dict):
            for snippets in details.values():
                for snippet in snippets or []:
                    if needle in str(snippet).casefold():
                        return True
    return False


# ── 正文提取 ────────────────────────────────────────────────────────────
_BLOCK_SPLIT_RE = re.compile(
    r"</(?:p|div|li|ul|ol|td|tr|table|section|article|main|h[1-6]|blockquote|dd|dt)>",
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_MAIN_REGION_RE = re.compile(
    r"<(article|main)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL
)


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _blocks_text(fragment: str) -> str:
    """按块切 HTML, 丢掉链接密度过高的导航块, 只留正文段落。"""
    parts: list[str] = []
    for raw in _BLOCK_SPLIT_RE.split(fragment):
        text = _strip_tags(raw)
        if len(text) < 40:
            continue
        anchor_chars = sum(len(_strip_tags(match.group(1))) for match in _ANCHOR_RE.finditer(raw))
        if anchor_chars / max(len(text), 1) > 0.6:
            continue
        parts.append(text)
    return " ".join(parts)


def _readable_body(html_doc: str) -> str:
    """从整页 HTML 里抽正文: 优先 article/main, 否则按块过滤导航/页眉页脚。"""
    body = re.sub(
        r"<(script|style|noscript|nav|footer|header|aside|form|iframe)\b[^>]*>.*?</\1>",
        " ",
        html_doc,
        flags=re.IGNORECASE | re.DOTALL,
    )
    main = _MAIN_REGION_RE.search(body)
    if main:
        candidate = _blocks_text(main.group(2))
        if len(candidate) >= 200:
            return candidate
    return _blocks_text(body)


# ── 抓正文 ──────────────────────────────────────────────────────────────
async def _fetch_page_text(
    url: str,
    *,
    max_chars: int,
    timeout: float,
    proxy: str | None = None,
) -> dict[str, str] | None:
    """抓一页正文。SSRF 校验/HTML 提取复用 tools.py 的实现, 保持同一套规则。"""
    lowered = url.lower()
    if any(lowered.endswith(suffix) for suffix in _SKIP_URL_SUFFIXES):
        return None
    host = (urlsplit(url).hostname or "").lower()
    if any(host.endswith(skip_host) for skip_host in _UNFETCHABLE_HOSTS):
        return None
    try:
        from .tools import _TOOL_BROWSER_USER_AGENT, _html_text, _validate_public_url
    except Exception:  # noqa: BLE001 - tools 依赖 nonebot, 缺失时静默降级
        return None
    current_url, reason = _validate_public_url(url)
    if reason or not current_url:
        return None
    headers = {
        "User-Agent": _TOOL_BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
    }
    response: httpx.Response | None = None
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, proxy=proxy or None
        ) as client:
            for _ in range(5):
                response = await client.get(current_url, headers=headers)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    return None
                next_url, reason = _validate_public_url(urljoin(current_url, location))
                if reason or not next_url:
                    return None
                current_url = next_url
    except (httpx.HTTPError, ValueError):
        return None
    if response is None or response.status_code >= 400:
        return None
    content_type = (response.headers.get("content-type") or "").lower()
    body_text = response.text
    if "html" in content_type or "<html" in body_text[:800].lower():
        title, _ignored = _html_text(body_text)
        body = _readable_body(body_text)
    elif "text/" in content_type or not content_type:
        title, body = "", body_text
    else:
        return None
    body = (body or "").strip()
    if len(body) < 40:
        return None
    return {
        "url": current_url,
        "title": (title or "").strip(),
        "text": body[: max(int(max_chars), 200)],
    }


# ── 主入口 ──────────────────────────────────────────────────────────────
class _SearchConfigView:
    """只覆写搜索引擎列表的 config 视图, 其它字段全部透传真 config。"""

    def __init__(self, config: Any, engines: Sequence[str]) -> None:
        self._config = config
        self.catty_web_search_engines = [str(engine) for engine in engines]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._config, name)


async def maybe_auto_fetch(
    config: Any,
    *,
    scope_key: str,
    user_id: str,
    text: str,
    kb_probe: Callable[[str], bool],
    is_exempt: bool = False,
    now: float | None = None,
) -> AutoFetchOutcome:
    """知识库缺口 → 搜索 + 抓正文。永不抛异常 (调用方仍建议 wait_for 兜底)。"""
    global _hour_bucket, _hour_count

    outcome = AutoFetchOutcome()
    if not bool(getattr(config, "catty_auto_fetch_enabled", True)):
        outcome.skipped = "disabled"
        return outcome
    if not bool(getattr(config, "catty_web_search_enabled", True)):
        outcome.skipped = "web_search_disabled"
        return outcome
    clean = str(text or "").strip()
    max_question_chars = int(getattr(config, "catty_auto_fetch_max_question_chars", 300) or 300)
    if not clean:
        outcome.skipped = "empty"
        return outcome
    if len(clean) > max_question_chars:
        outcome.skipped = "too_long"
        return outcome
    terms = extract_terms(clean)
    outcome.terms = tuple(terms)
    needed, reason = should_auto_fetch(clean)
    if not needed:
        outcome.skipped = reason
        return outcome
    # 用户明确要查/时效信息 → 不看知识库直接查; 其余先探本地知识库
    if reason in {"kb_question", "entity_question"} and terms:
        try:
            hit_terms = [term for term in terms if kb_probe(term)]
        except Exception as exc:  # noqa: BLE001
            _logger.debug("auto_web_fetch kb_probe failed: %s", exc)
            hit_terms = []
        if hit_terms:
            outcome.skipped = f"kb_hit:{hit_terms[0][:20]}"
            return outcome
    query = build_query(clean, terms)
    if not query:
        outcome.skipped = "empty_query"
        return outcome
    outcome.triggered = True
    outcome.reason = reason
    outcome.query = query

    current = time.monotonic() if now is None else float(now)
    # 整轮预算: 搜索 + 抓页共享, 外层 handle_chat 还有 wait_for 兜底
    budget_seconds = max(
        float(getattr(config, "catty_auto_fetch_timeout_seconds", 12.0) or 12.0), 1.0
    )
    deadline = current + budget_seconds
    # 缓存按 query 全局共享: 公开网页事实跟群/私聊无关, 同题不重复抓
    cache_key = _normalize_key(query)
    cached = _cache.get(cache_key)
    ttl = max(int(getattr(config, "catty_auto_fetch_cache_ttl_seconds", 21_600) or 0), 0)
    negative_ttl = max(int(getattr(config, "catty_auto_fetch_negative_ttl_seconds", 900) or 0), 0)
    if cached is not None:
        cached_at, cached_outcome = cached
        effective_ttl = ttl if cached_outcome.ok else min(negative_ttl, ttl or negative_ttl)
        if current - cached_at <= effective_ttl:
            return replace(cached_outcome, from_cache=True)
        _cache.pop(cache_key, None)

    if not is_exempt:
        cooldown = max(float(getattr(config, "catty_auto_fetch_cooldown_seconds", 120) or 0), 0.0)
        last = _cooldowns.get(scope_key, 0.0)
        remaining = last + cooldown - current
        if cooldown > 0 and remaining > 0:
            outcome.skipped = f"cooldown:{int(remaining)}"
            return outcome

    budget = max(int(getattr(config, "catty_auto_fetch_hourly_budget", 20) or 0), 0)
    bucket = int(current // 3600)
    if bucket != _hour_bucket:
        _hour_bucket = bucket
        _hour_count = 0
    if budget > 0 and _hour_count >= budget:
        outcome.skipped = "hourly_budget"
        return outcome
    _hour_count += 1
    _cooldowns[scope_key] = current

    try:
        engines = [
            str(engine).strip()
            for engine in (getattr(config, "catty_auto_fetch_engines", None) or [])
            if str(engine).strip()
        ]
        search_config = _SearchConfigView(config, engines) if engines else config
        # 搜索最多占掉总预算的前半段, 剩下的时间留给抓正文
        search_budget = max(min(deadline - time.monotonic() - 1.0, 8.0), 1.0)
        results: list[WebSearchResult] = await asyncio.wait_for(
            search_web(search_config, query), timeout=search_budget
        )
    except asyncio.TimeoutError:
        _logger.info("auto_web_fetch search timed out for %r", query)
        outcome.skipped = "search_timeout"
        return outcome
    except Exception as exc:  # noqa: BLE001
        _logger.info("auto_web_fetch search failed for %r: %s", query, exc)
        outcome.skipped = "search_error"
        return outcome

    if results and outcome.terms:
        # 搜索后端偶尔整页瞎给 (查询串被忽略); 标题/摘要一个候选词都不沾的全丢
        results = [
            result
            for result in results
            if _matches_terms(f"{result.title} {result.snippet}", outcome.terms)
        ]
    if not results:
        # 查过但没结果也要进 prompt: 明确告诉模型"别编", 比让它凭记忆硬答安全
        outcome.context = format_auto_fetch_context(outcome)
        _cache_put(cache_key, outcome, current)
        return outcome

    outcome.ok = True
    outcome.snippets = [
        {"title": result.title, "url": result.url, "snippet": result.snippet}
        for result in results[:4]
    ]

    max_pages = max(int(getattr(config, "catty_auto_fetch_max_pages", 2) or 0), 0)
    page_chars = max(int(getattr(config, "catty_auto_fetch_page_chars", 1200) or 1200), 200)
    total_chars = max(int(getattr(config, "catty_auto_fetch_total_chars", 2600) or 2600), page_chars)
    try:
        request_timeout = float(getattr(config, "catty_web_search_request_timeout", 10.0) or 10.0)
    except (TypeError, ValueError):
        request_timeout = 10.0
    page_timeout = min(max(request_timeout, 3.0), 6.0)
    proxy = str(getattr(config, "catty_http_proxy", "") or "")

    used = 0
    seen_urls: set[str] = set()
    for result in results:
        if len(outcome.pages) >= max_pages or used >= total_chars:
            break
        if result.url in seen_urls:
            continue
        seen_urls.add(result.url)
        page_budget = min(page_timeout, deadline - time.monotonic())
        if page_budget < 0.5:
            break
        try:
            page = await _fetch_page_text(
                result.url,
                max_chars=min(page_chars, total_chars - used),
                timeout=page_budget,
                proxy=proxy,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.debug("auto_web_fetch page fetch failed for %r: %s", result.url, exc)
            continue
        if not page:
            continue
        if not _matches_terms(f"{page['title']} {page['text'][:600]}", outcome.terms):
            continue
        outcome.pages.append(page)
        used += len(page["text"])

    outcome.context = format_auto_fetch_context(outcome)
    _cache_put(cache_key, outcome, current)
    return outcome


# ── prompt 块 ───────────────────────────────────────────────────────────
def format_auto_fetch_context(outcome: AutoFetchOutcome) -> str:
    """把结果拼成可注入 prompt 的【联网补全】块。空/跳过 → 空串。"""
    if not outcome.triggered or outcome.skipped or not outcome.query:
        return ""
    lines = [
        "【联网补全】",
        f"问题线索: {outcome.query}",
        "本地知识库没有这条的现成记录, 本轮已经替你联网核对过"
        " (回复里不要提『知识库/检索/联网/查了一下』这类后台动作)。",
    ]
    if outcome.pages:
        lines.append("下面是从公开网页抓到的正文摘录, 可以直接当成你知道的事实, 但只限写到的部分:")
        for index, page in enumerate(outcome.pages, 1):
            title = page.get("title") or page.get("url", "")
            lines.append(f"{index}. {title} <{page.get('url', '')}>")
            lines.append(f"   {page.get('text', '')}")
    elif outcome.snippets:
        lines.append("联网只拿到搜索结果摘要 (没抓到正文), 摘要可以当参考, 但细节别当确定事实:")
        for index, item in enumerate(outcome.snippets, 1):
            snippet = f" — {item['snippet']}" if item.get("snippet") else ""
            lines.append(f"{index}. {item.get('title', '')} <{item.get('url', '')}>{snippet}")
    else:
        lines.append(
            "联网也没有搜到可用结果。按你自己确定的知识短答; 不确定就说不确定,"
            " 绝对不要编链接、日期、价格或版本号。"
        )
    lines.append(
        "答法: 先给结论 (1-3 句, 办事模式才分步), 时效信息以摘录里的日期为准;"
        " 摘录和你记忆冲突时以摘录为准, 摘录没覆盖的部分不补编。"
    )
    return "\n".join(lines)
