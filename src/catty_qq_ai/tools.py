"""主 AI 主动调用的 tool 集合 —— catty_recall / catty_user_profile / catty_mc_status。

设计目标:让笨猫像 IDE 那样按场景拉记忆/画像/MC 状态,而不是被动等程序灌 context。

- Schema 走标准 OpenAI function calling 协议(tools[i].function.parameters JSON Schema)。
- Executor 是异步 callable,接收已解析的 JSON args,返回 JSON 友好的 dict。
- 每个 tool 内置 in-process LRU + TTL 缓存,重复调度直接命中本地。
- 调用上下文(event/memory_store/config)通过 ToolContext 注入,tools 模块自己不持有全局状态。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .config import Config
from .mc_status import _default_probe
from .memory import MemoryStore


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


ALL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "catty_recall": _RECALL_SCHEMA,
    "catty_user_profile": _USER_PROFILE_SCHEMA,
    "catty_mc_status": _MC_STATUS_SCHEMA,
}


# ── Tool 上下文 / 注入 ─────────────────────────────────────────────────

@dataclass(slots=True)
class ToolContext:
    config: Config
    memory_store: MemoryStore
    event: MessageEvent | None

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


# Executor 注册表:name → async callable
ToolExecutor = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

_EXECUTORS: dict[str, ToolExecutor] = {
    "catty_recall": _exec_recall,
    "catty_user_profile": _exec_user_profile,
    "catty_mc_status": _exec_mc_status,
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
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as exc:
        return {"error": f"arguments 不是合法 JSON: {exc.msg}"}
    if not isinstance(args, dict):
        return {"error": "arguments 必须是 JSON 对象。"}
    try:
        return await executor(args, ctx)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Tool %s execution failed: %s", name, exc, exc_info=True)
        return {"error": f"{name} 执行失败: {exc.__class__.__name__}: {exc}"}


def tools_system_hint() -> str:
    """常驻 system 提示:告诉主 AI 工具的存在和调用边界。"""
    return (
        "你接入了三个本地工具:catty_recall(查历史记忆)、catty_user_profile(查用户画像)、"
        "catty_mc_status(查 MC 服务器状态)。**只在真的需要时调用**,不要每条消息都查。"
        "判断标准:"
        "(a) 用户用了'上次/记得/之前/还记得'等时间指代且常驻 context 里没给答案 → 调 catty_recall;"
        "(b) 群里出现一个你不确定怎么称呼的非当前发言者 QQ 号 → 调 catty_user_profile;"
        "(c) 用户直接问 MC 在线人数/服务器掉没掉 → 调 catty_mc_status。"
        "其它情况(普通闲聊、撒娇、玩梗、技术问答、已经能直接答上来的)**不要调工具**——"
        "工具结果是 system 消息回填,任何调用都会让回复变慢。"
        "如果调了 tool,在拿到结果后基于结果写最终回复;**禁止把 tool 结果原文复读**,"
        "也禁止在最终回复里出现 tool_call/function_call 标记或 JSON。"
        "拿到 error 字段时用猫娘口吻自然说一句'人家想不起来了/查不到/服掉啦'即可,不要把 error 文本贴出来。"
    )
