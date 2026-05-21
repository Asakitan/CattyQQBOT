"""主 AI 主动调用的 tool 集合 —— catty_recall / catty_user_profile / catty_mc_status。

设计目标:让笨猫像 IDE 那样按场景拉记忆/画像/MC 状态,而不是被动等程序灌 context。

- Schema 走标准 OpenAI function calling 协议(tools[i].function.parameters JSON Schema)。
- Executor 是异步 callable,接收已解析的 JSON args,返回 JSON 友好的 dict。
- 每个 tool 内置 in-process LRU + TTL 缓存,重复调度直接命中本地。
- 调用上下文(event/memory_store/config)通过 ToolContext 注入,tools 模块自己不持有全局状态。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .config import Config
from .mc_status import _default_probe
from .memory import MemoryStore
from .nsfw_search import NsfwResult, search_nsfw
from .parsers import lenient_json_object
from .web_search import format_search_context, search_image_urls, search_web


_logger = logging.getLogger("catty_qq_ai.tools")


# ── Tool schema (OpenAI function calling 标准) ────────────────────────

_RECALL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_recall",
        "description": (
            "查询长期记忆和待压缩语料,定位'上次/以前/那时候'相关的发言或共识。"
            "适用场景:用户用了'上次/记得/那时候/之前说过/还记得吗'这类时间指代;"
            "你想知道某个群友/主人以前说过的偏好、决定、梗、称呼;"
            "上下文出现陌生话题但语境暗示曾在群里讨论过。"
            "不要用于查询'此时此刻'的活跃消息(那是实时上下文已经给你的部分)。"
            "返回内容包含 long_term_summary(长期摘要) 和 matches(命中条目列表)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["current_group", "current_user", "specific_user"],
                    "description": (
                        "查询范围。current_group=当前群整体记忆;"
                        "current_user=当前发言用户的私聊画像/语料;"
                        "specific_user=指定 QQ 号的用户(必须填 user_id)。"
                    ),
                },
                "user_id": {
                    "type": "string",
                    "description": "scope=specific_user 时必填,目标用户的 QQ 号。",
                },
                "keywords": {
                    "type": "string",
                    "description": (
                        "搜索关键词。多个关键词用空格或逗号分隔,做 substring AND 匹配。"
                        "留空表示只想拿摘要 + 最近几条语料。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条匹配语料,默认 6,上限 20。",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["scope"],
        },
    },
}


_USER_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_user_profile",
        "description": (
            "查询一个 QQ 用户的画像:称呼、性别、印象、置信度、是否主人。"
            "适用场景:群里冒出一个你不认识的 QQ 号、你不确定怎么称呼某人、需要确认某用户是不是主人/特别关心对象。"
            "不要每条消息都查(发言者画像已经在常驻 context 里),只在你真的疑惑某个非当前发言者时查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "目标用户的 QQ 号。",
                },
                "group_id": {
                    "type": "string",
                    "description": (
                        "可选。在群聊里查时填当前群号(画像可能跟群相关);"
                        "私聊场景留空走全局画像。"
                    ),
                },
            },
            "required": ["user_id"],
        },
    },
}


_MC_STATUS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_mc_status",
        "description": (
            "查询 Minecraft 服务器实时状态:在线人数和能否连上。"
            "适用场景:用户问'MC 现在多少人在线/服开着没/有没有人在玩/服务器掉了吗';"
            "你需要确认要不要邀请主人/群友进服。"
            "结果有 30s 缓存,放心调,不会真的频繁戳服务器。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


_WEB_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_web_search",
        "description": (
            "Google/Bing 联网搜索拿最新信息。**只在真的需要时调用**:"
            "用户问新闻/版本/价格/教程/特定事实,或明确说'查/搜/联网';"
            "你训练数据可能过期或不确定具体细节。普通闲聊/撒娇/已经知道的问题不要调。"
            "每个 scope+用户有 600s cooldown(主人/特别关心用户豁免)。"
            "返回 results 数组(title/url/snippet);AI 拿到后基于结果生成最终回复,"
            "禁止编造不存在的链接,禁止把 marker 文本贴出来。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。精炼实词,不超过 5 个;不要带'R-18/涩图/搜索一下'等啰嗦词。",
                },
            },
            "required": ["query"],
        },
    },
}


_NSFW_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_nsfw_search",
        "description": (
            "搜索 R-18 资源:pixiv 图片或 iwara 视频。**仅在好友私聊里可调用**,"
            "群聊调用会立即返回 error(此时你应引导用户去私聊,不要重试)。"
            "kind=image 时插件会**直接把下载好的图片发到聊天里**——你不用贴链接,"
            "拿到 tool 结果后只补 1-2 句猫娘人格短评(可以害羞/嘴硬/撒娇/报作者名);"
            "kind=video 时插件只返回视频链接,你挑 1-3 个抛出去配短评。"
            "query 写法铁律:**第一位放群友原话那个语种**(中文→中文,日文→日文,英文→英文),"
            "后面用英文逗号 `,` 跟 1-2 个备选语言。"
            "例:群友说『香奈美』→ query=`香奈美,kanami,Strinova`;群友说『Raiden Shogun』→ "
            "query=`Raiden Shogun,雷電将軍,雷电将军`。每个候选 1-2 词,不要拼 R-18/涩图。"
            "插件已自动 r18=true,你不用管。同一人 30s 内只能搜一次。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["image", "video"],
                    "description": "image=pixiv 图(下载发送);video=iwara 视频(返回链接)。",
                },
                "query": {
                    "type": "string",
                    "description": "候选关键词,英文逗号分隔;第一位必须是群友原话语种。",
                },
            },
            "required": ["kind", "query"],
        },
    },
}


_MEME_QUERY_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_meme_query",
        "description": (
            "拉一张梗图/网图(Bing 图片搜索)以图代话。**只在你确实想用图加强表达时调用**——"
            "撒娇/玩梗/情绪反应建议优先用本地表情库(EMOJI_QUERY marker 那条路径,程序自动配),"
            "只有需要特定主题的画面(角色/场景/具体事物)时才调本 tool。"
            "返回 image_uri 是 base64:// URI;你拿到后**必须在最终回复中**用 "
            "`<<<CATTY_INLINE_IMAGE:URI>>>` 标记把它嵌入想要展示图片的位置,"
            "发送链路会自动转成 QQ 图片消息。"
            "失败时返回 error,这时直接用文字回复即可,不要拼任何 INLINE_IMAGE 标记。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "梗图主题。短关键词(1-3 个词),例如『猫猫贴贴』『摸鱼』『傲娇害羞』。",
                },
            },
            "required": ["keywords"],
        },
    },
}


ALL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "catty_recall": _RECALL_SCHEMA,
    "catty_user_profile": _USER_PROFILE_SCHEMA,
    "catty_mc_status": _MC_STATUS_SCHEMA,
    "catty_web_search": _WEB_SEARCH_SCHEMA,
    "catty_nsfw_search": _NSFW_SEARCH_SCHEMA,
    "catty_meme_query": _MEME_QUERY_SCHEMA,
}


# ── Tool 上下文 / 注入 ─────────────────────────────────────────────────

@dataclass(slots=True)
class ToolContext:
    config: Config
    memory_store: MemoryStore
    event: MessageEvent | None
    # 由 __init__.py 主回复点注入:把 NSFW pixiv 结果下载成本地缓存 segments,
    # 复用现有 sent_registry / cache_dir / LRU 清理。tools.py 不持有这套状态。
    prepare_nsfw_segments_fn: Callable[
        [list["NsfwResult"], int], Awaitable[tuple[list[Any], list["NsfwResult"]]]
    ] | None = None
    # 下载二进制(http URL → bytes + content_type),沿用 openai_client.download_binary。
    # 通过注入避免 tools.py import openai_client(后者 import 链涉及 mc_status,
    # 已经能用,但保持单向依赖更干净)。
    download_binary_fn: Callable[[Config, str], Awaitable[tuple[bytes, str]]] | None = None
    # 副作用:executor 把要发的图片塞这里,主回复点收尾时取出来拼到发送链路。
    # 元素类型由 prepare_nsfw_segments_fn 决定(实际是 MessageSegment),tools.py 不依赖具体类型。
    pending_image_segments: list[Any] = field(default_factory=list)

    @property
    def group_id(self) -> str:
        if isinstance(self.event, GroupMessageEvent):
            return str(self.event.group_id)
        return ""

    @property
    def user_id(self) -> str:
        return str(self.event.user_id) if self.event is not None else ""

    @property
    def is_private(self) -> bool:
        return isinstance(self.event, PrivateMessageEvent)

    @property
    def configured_title(self) -> str:
        """复制自 __init__.py._configured_title,tool 内部做主人/特别关心豁免用。"""
        if self.event is None:
            return ""
        user_id = str(self.event.user_id)
        if isinstance(self.event, GroupMessageEvent):
            group_title = self.config.catty_group_user_titles.get(
                str(self.event.group_id), {}
            ).get(user_id)
            if group_title:
                return str(group_title)
        return str(self.config.catty_user_titles.get(user_id) or "")


# ── 共享 TTL 缓存 ──────────────────────────────────────────────────────

class _TTLCache:
    """简易 in-process 缓存。key 由 caller 计算; value 是 (expires_at, result)。

    线程不安全,但 NoneBot 单 event loop 下足够。每个 tool 一份独立实例,
    避免 key collision 复杂度。
    """

    __slots__ = ("_data", "_max_entries")

    def __init__(self, max_entries: int = 256) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max_entries = max(max_entries, 32)

    def get(self, key: str, *, ttl: float) -> Any | None:
        if ttl <= 0:
            return None
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any, *, ttl: float) -> None:
        if ttl <= 0:
            return
        self._data[key] = (time.monotonic() + ttl, value)
        if len(self._data) > self._max_entries:
            stale = sorted(self._data.items(), key=lambda item: item[1][0])
            for k, _ in stale[: len(self._data) - self._max_entries]:
                self._data.pop(k, None)


_recall_cache = _TTLCache()
_profile_cache = _TTLCache()
# 联网搜索按 scope(group/private + user) 做 cooldown,主人/特别关心用户豁免。
# 沿用原 __init__.py 的 _web_search_cooldowns 行为,只是搬到 tools 模块。
_web_search_cooldowns: dict[str, float] = {}
# NSFW 仅在私聊里能调,按 user_id 做 cooldown。
_nsfw_search_cooldowns: dict[str, float] = {}


# ── 各 tool 的 executor ───────────────────────────────────────────────

async def _exec_recall(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    scope = str(args.get("scope") or "").strip()
    keywords = str(args.get("keywords") or "").strip()
    limit_raw = args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 6
    except (TypeError, ValueError):
        limit = 6

    if scope == "current_group":
        if not ctx.group_id:
            return {"error": "当前不是群聊,不能用 scope=current_group;改用 current_user。"}
        cache_key = f"group:{ctx.group_id}|kw:{keywords}|n:{limit}"
        cached = _recall_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        if cached is not None:
            return cached
        result = ctx.memory_store.recall(group_id=ctx.group_id, keywords=keywords, limit=limit)
        _recall_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        return result

    if scope == "current_user":
        if not ctx.user_id:
            return {"error": "无法识别当前用户。"}
        if ctx.group_id:
            cache_key = f"group:{ctx.group_id}|user:{ctx.user_id}|kw:{keywords}|n:{limit}"
            cached = _recall_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
            if cached is not None:
                return cached
            result = ctx.memory_store.recall(
                group_id=ctx.group_id,
                user_id=ctx.user_id,
                keywords=keywords,
                limit=limit,
            )
            _recall_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
            return result
        cache_key = f"user:{ctx.user_id}|kw:{keywords}|n:{limit}"
        cached = _recall_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        if cached is not None:
            return cached
        result = ctx.memory_store.recall(user_id=ctx.user_id, keywords=keywords, limit=limit)
        _recall_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        return result

    if scope == "specific_user":
        target_user_id = str(args.get("user_id") or "").strip()
        if not target_user_id:
            return {"error": "scope=specific_user 必须填 user_id。"}
        group_filter = ctx.group_id  # 群聊里查指定用户优先按当前群范围
        cache_key = f"group:{group_filter}|user:{target_user_id}|kw:{keywords}|n:{limit}"
        cached = _recall_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        if cached is not None:
            return cached
        if group_filter:
            result = ctx.memory_store.recall(
                group_id=group_filter,
                user_id=target_user_id,
                keywords=keywords,
                limit=limit,
            )
        else:
            result = ctx.memory_store.recall(user_id=target_user_id, keywords=keywords, limit=limit)
        _recall_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        return result

    return {"error": f"未知 scope={scope!r};只接受 current_group / current_user / specific_user。"}


async def _exec_user_profile(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    user_id = str(args.get("user_id") or "").strip()
    if not user_id:
        return {"error": "user_id 不能为空。"}
    group_id = str(args.get("group_id") or "").strip() or ctx.group_id
    cache_key = f"user:{user_id}|group:{group_id}"
    cached = _profile_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
    if cached is not None:
        return cached
    result = ctx.memory_store.lookup_user_profile(user_id, group_id)
    _profile_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
    return result


async def _exec_mc_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del args
    host = str(getattr(ctx.config, "catty_ai_fallback_mc_server_host", "") or "").strip() or "localhost"
    port = int(getattr(ctx.config, "catty_ai_fallback_mc_server_port", 0) or 26843)
    timeout = float(getattr(ctx.config, "catty_ai_fallback_mc_ping_timeout_seconds", 3.0) or 3.0)
    try:
        online, players = await asyncio.wait_for(
            _default_probe.status(host, port, timeout=timeout),
            timeout=timeout + 1.0,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        return {
            "reachable": False,
            "error": f"探测超时或网络异常: {exc.__class__.__name__}",
            "host": host,
            "port": port,
        }
    return {
        "reachable": bool(online),
        "online_players": int(players),
        "host": host,
        "port": port,
        "note": (
            "结果有 30s 本地缓存。reachable=False 通常表示服务器没开或不在白名单网段;"
            "不是猫猫的锅,你可以直接告诉用户'服务器目前掉了/猫猫连不上'。"
        ),
    }


async def _exec_web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query 不能为空"}
    if not getattr(ctx.config, "catty_web_search_enabled", False):
        return {"error": "web_search 已被配置禁用"}

    # 主人/特别关心用户豁免 cooldown(沿用原 _web_search_exempt 语义)
    is_exempt = bool(ctx.configured_title.strip())
    cd_seconds = float(getattr(ctx.config, "catty_web_search_cooldown_seconds", 600.0) or 0.0)
    if not is_exempt and cd_seconds > 0 and ctx.event is not None:
        scope_id = ctx.group_id or ctx.user_id or "anonymous"
        cd_key = f"{scope_id}:{ctx.user_id}"
        now = time.monotonic()
        last = _web_search_cooldowns.get(cd_key, 0.0)
        remaining = max(last + cd_seconds - now, 0.0)
        if remaining > 0:
            return {
                "error": f"web_search 冷却剩 {int(remaining)}s,请基于已有知识回答(每 scope+用户 10 分钟一次)。"
            }
        _web_search_cooldowns[cd_key] = now

    try:
        results = await search_web(ctx.config, query[:160])
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("Web search failed for %r: %s", query, exc)
        return {
            "query": query,
            "error": f"搜索失败: {exc.__class__.__name__}: {exc}",
            "results": [],
        }
    return {
        "query": query,
        "count": len(results),
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                "source": r.source,
            }
            for r in results[:8]
        ],
        "context_text": format_search_context(query, results),
    }


async def _exec_nsfw_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    kind_raw = str(args.get("kind") or "").strip().lower()
    if kind_raw in {"img", "pic", "picture", "image", "图", "图片"}:
        kind = "image"
    elif kind_raw in {"video", "vid", "视频"}:
        kind = "video"
    else:
        return {"error": "kind 必须是 image 或 video。"}
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query 不能为空。"}

    if not getattr(ctx.config, "catty_nsfw_search_enabled", False):
        return {"error": "nsfw_search 已被配置禁用,不能调。"}
    if not ctx.is_private:
        # 群里直接挡掉,让 AI 用人格自然引导用户去私聊
        return {
            "error": "群里禁止 NSFW 搜索;请用猫娘人格让用户加好友私聊再来。",
            "suggest_private_chat": True,
        }

    cd_seconds = max(int(getattr(ctx.config, "catty_nsfw_search_cooldown_seconds", 30) or 0), 0)
    if cd_seconds > 0:
        cd_key = ctx.user_id or "anonymous"
        now = time.monotonic()
        last = _nsfw_search_cooldowns.get(cd_key, 0.0)
        remaining = max(last + cd_seconds - now, 0.0)
        if remaining > 0:
            return {"error": f"NSFW 搜索冷却剩 {int(remaining)}s,稍后再戳"}
        _nsfw_search_cooldowns[cd_key] = now

    image_send_count = max(int(getattr(ctx.config, "catty_nsfw_image_send_count", 2) or 2), 1)
    pool_size = max(
        int(getattr(ctx.config, "catty_nsfw_search_max_results", 4) or 4),
        image_send_count * 6,
        8,
    )
    try:
        results = await search_nsfw(ctx.config, query[:160], kind=kind, max_results=pool_size)
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("NSFW search failed for %r (%s): %s", query, kind, exc)
        return {"error": f"搜索失败: {exc.__class__.__name__}: {exc}", "kind": kind, "query": query}

    used_results: list[NsfwResult] = []
    if kind == "image" and results and ctx.prepare_nsfw_segments_fn is not None:
        try:
            segments, used_results = await ctx.prepare_nsfw_segments_fn(results, image_send_count)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("prepare_nsfw_segments_fn raised: %s", exc, exc_info=True)
            segments = []
        if segments:
            ctx.pending_image_segments.extend(segments)
            _logger.info(
                "NSFW tool produced %d image segment(s) for query=%r", len(segments), query
            )

    context_results = used_results if (kind == "image" and used_results) else results
    return {
        "kind": kind,
        "query": query,
        "images_already_sent": len(ctx.pending_image_segments) if kind == "image" else 0,
        "count": len(context_results),
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                "source": r.source,
                "has_media": bool(r.media_urls),
            }
            for r in context_results[: max(image_send_count * 2, 6)]
        ],
        "guidance": (
            "image 命中且 images_already_sent>0:程序已经把图发了,你只补 1-2 句猫娘短评,不要贴链接;"
            "image 但 images_already_sent=0:下载全失败,挑 1-2 个 results URL 给主人,简短;"
            "video:挑 1-3 个 iwara 链接抛出去配短评。禁止编造 URL、禁止安全免责模板。"
        ),
    }


async def _exec_meme_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    keywords = str(args.get("keywords") or "").strip()
    if not keywords:
        return {"error": "keywords 不能为空"}
    if ctx.download_binary_fn is None:
        return {"error": "运行环境未注入下载器,无法拉图"}
    try:
        image_urls = await search_image_urls(ctx.config, keywords[:80], max_results=4)
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("Meme search failed for %r: %s", keywords, exc)
        return {"error": f"搜图失败: {exc.__class__.__name__}: {exc}"}
    if not image_urls:
        return {"error": "没找到合适的梗图,用文字回复即可"}

    for image_url in image_urls:
        try:
            image_data, content_type = await ctx.download_binary_fn(ctx.config, image_url)
        except httpx.HTTPError as exc:
            _logger.info("Meme candidate download failed (%s): %s", image_url, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Meme candidate fetch unexpected error: %s", exc)
            continue
        if not image_data:
            continue
        if content_type and not content_type.lower().startswith("image/"):
            continue
        uri = "base64://" + base64.b64encode(image_data).decode("ascii")
        return {
            "image_uri": uri,
            "source_url": image_url,
            "keywords": keywords,
            "note": (
                "把这个 image_uri **完整原样**用 <<<CATTY_INLINE_IMAGE:URI>>> 标记嵌进最终回复想展示图的位置。"
                "切勿把 URI 截断、切勿单独输出 URI,也不要把 source_url 贴出来当链接。"
            ),
        }
    return {"error": "拿到候选 URL 但全部下载失败,用文字回复即可"}


# Executor 注册表:name → async callable
ToolExecutor = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

_EXECUTORS: dict[str, ToolExecutor] = {
    "catty_recall": _exec_recall,
    "catty_user_profile": _exec_user_profile,
    "catty_mc_status": _exec_mc_status,
    "catty_web_search": _exec_web_search,
    "catty_nsfw_search": _exec_nsfw_search,
    "catty_meme_query": _exec_meme_query,
}


# ── 对外 API ───────────────────────────────────────────────────────────

def available_tool_schemas(config: Config, *, is_private: bool) -> list[dict[str, Any]]:
    """按场景挑出本次主回复应该挂的 tool schemas。

    主人选择的是'始终挂载',所以默认返回全部三个;但允许通过 config 在私聊里
    剔除特定 tool(默认私聊不挂 catty_user_profile,私聊只有一个人没必要查别人画像)。
    """
    if not getattr(config, "catty_tools_enabled", True):
        return []
    excluded: set[str] = set()
    if is_private:
        for name in getattr(config, "catty_tools_disabled_in_private", []) or []:
            excluded.add(str(name).strip())
    return [schema for name, schema in ALL_TOOL_SCHEMAS.items() if name not in excluded]


async def execute_tool_call(
    name: str,
    arguments_json: str,
    ctx: ToolContext,
) -> dict[str, Any]:
    """执行一次 tool_call。args 解析失败/未知 tool/执行抛错都返回结构化 error,
    让主 AI 在下一轮自己看懂出错原因(而不是把异常丢给用户)。
    """
    executor = _EXECUTORS.get(name)
    if executor is None:
        return {"error": f"未知 tool: {name}"}
    raw = (arguments_json or "").strip()
    if not raw:
        args: dict[str, Any] = {}
    else:
        # 走 lenient_json_object 让 fence / 智能引号 / 尾逗号 / 单引号都能恢复。
        parsed = lenient_json_object(raw)
        if parsed is None:
            return {"error": "arguments 不是合法 JSON 对象,无法解析"}
        args = parsed
    try:
        return await executor(args, ctx)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Tool %s execution failed: %s", name, exc, exc_info=True)
        return {"error": f"{name} 执行失败: {exc.__class__.__name__}: {exc}"}


def tools_system_hint() -> str:
    """常驻 system 提示:告诉主 AI 工具的存在和调用边界。"""
    return (
        "你接入了六个本地工具,**只在真的需要时调用**(每次调用都让回复变慢):\n"
        "1. catty_recall — 查历史记忆/语料/长期摘要。用户用'上次/记得/之前/还记得'等时间指代,"
        "且常驻 context 没给答案时再调。\n"
        "2. catty_user_profile — 查用户画像/称呼/性别/是否主人。群里冒出一个你不确定怎么称呼的"
        "非当前发言者 QQ 号时再调;当前发言者画像已在 context 里,不要重复查。\n"
        "3. catty_mc_status — 查 MC 服务器在线人数与可达性。用户问 MC 在不在/几个人在玩 时调。\n"
        "4. catty_web_search — Google/Bing 联网搜索。用户问最新新闻/版本/价格/教程/具体事实,"
        "或明确说'搜一下/查一下/联网'时调;有 10 分钟 cooldown(主人豁免)。普通闲聊/已经知道的事不要调。\n"
        "5. catty_nsfw_search — pixiv 图 / iwara 视频。**仅好友私聊里可调**,群里调会返回 error"
        "(此时引导用户加好友私聊,不要重试);kind=image 时图片由程序下载并自动发送,"
        "你只补 1-2 句猫娘短评,不要贴链接也不要复读 URL。\n"
        "6. catty_meme_query — Bing 图片搜索拉一张梗图嵌入回复。**只有特定主题画面需求时调**——"
        "撒娇/玩梗/情绪反应让本地表情库随机配(<<<CATTY_EMOJI_QUERY:意图>>> marker 老路径)更快。"
        "拿到 image_uri 后必须用 <<<CATTY_INLINE_IMAGE:URI>>> 标记嵌入最终回复指定位置。\n"
        "通用规则:\n"
        "- 多个 tool 调用可以并发(同一轮发起多个 tool_calls)但**总开销=回复延迟**,能不调就不调。\n"
        "- 拿到 tool 结果后基于结果写最终回复;**禁止复读 tool 返回的 JSON 原文**,"
        "也禁止在最终回复里出现 tool_call/function_call/[[CATTY_*]] 标记(INLINE_IMAGE 除外)。\n"
        "- 拿到 error 字段时用猫娘口吻自然说一句'人家想不起来/查不到/服掉了/群里说不太合适'即可,"
        "不要把 error 文本贴给用户看。"
    )
