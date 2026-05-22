import asyncio
import base64
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
import json
import mimetypes
import os
from pathlib import Path
import random
import re
import time
from typing import Any, DefaultDict

import httpx
from nonebot import get_bots, get_driver, get_plugin_config, logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PokeNotifyEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment, NoticeEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed as OnebotActionFailed
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State

from .config import Config, KeywordReplyRule
from .features import (
    choose_turtle_soup,
    extract_web_search_query,
    format_duration_cn,
    is_turtle_soup_request,
    search_cooldown_key,
    turtle_soup_cooldown_key,
    turtle_soup_remaining,
)
from .message_utils import (
    ExtractedMessage,
    build_history_key,
    event_plain_text,
    expression_message_signature,
    extract_incoming_message,
    extract_image_urls,
    mentions_other_user,
    reply_message_ids,
    split_reply,
)
from .emoji_store import EmojiEntry, EmojiStore
from .legs_picker import LegsPicker, is_legs_trigger, random_legs_reply
from .memory import MemoryStore
from .openai_client import (
    MCBusyError,
    OpenAICompatibleError,
    analyze_images_for_reply,
    assess_user_anger,
    chat_completion,
    chat_completion_with_tools,
    describe_images,
    download_binary,
    local_critic_completion,
)
from .action_hints import build_action_hints
from .conversation_pulse import analyze_pulse, build_pulse_context
from .entity_extractor import build_entity_context
from .intent_classifier import build_intent_context
from .parsers import strip_catty_markers as _strip_catty_markers
from .slang_dict import build_slang_context
from .tools import ToolContext, available_tool_schemas, execute_tool_call, tools_system_hint
from .persona_prompts import (
    build_catgirl_examples_prompt,
    build_conversation_flow_prompt,
    build_disambiguation_examples_prompt,
    build_group_meme_literacy_prompt,
    build_image_literacy_prompt,
    build_persona_memory_prompt,
    build_qq_chat_rhythm_prompt,
    build_reply_intelligence_prompt,
    build_reply_self_check_prompt,
    build_scenario_playbook_prompt,
    build_scene_discrimination_prompt,
    build_semantic_perception_prompt,
)
from .reply_markers import (
    EMOJI_QUERY_PREFIX,
    EMOJI_QUERY_SUFFIX,
    INLINE_IMAGE_PLACEHOLDER,
    NO_REPLY_MARKER,
    REPLY_SPLIT_MARKER,
    TRAILING_CHAT_PUNCTUATION,
    extract_emoji_query as _extract_emoji_query,
    extract_inline_images as _extract_inline_images,
    split_chunk_with_image_placeholders as _split_chunk_with_image_placeholders,
    strip_inline_image_markers as _strip_inline_image_markers,
    strip_inline_image_placeholders as _strip_inline_image_placeholders,
)
from . import activity_feed
from .session_cache import SessionCache, format_session_list_for_owner
from .latex_renderer import (
    chunk_to_segments,
    replace_latex_with_placeholders,
    restore_latex_placeholders,
)
from .star_resonance_memory import build_star_resonance_context
from .strinova_memory import build_strinova_context
from .web_search import format_search_context, search_image_urls, search_web
from .nsfw_search import (
    NsfwResult,
    download_image_bytes as download_nsfw_image_bytes,
    format_nsfw_search_context,
    search_nsfw,
)
from . import owner_forward as _owner_forward

try:
    from catty_config_loader import _apply_config as _apply_json_config
    from catty_config_loader import _find_config_path as _find_json_config_path
except Exception:
    _apply_json_config = None
    _find_json_config_path = None


__plugin_meta__ = PluginMetadata(
    name="Catty QQ AI",
    description="OpenAI-compatible QQ chat plugin for NoneBot2 and OneBot v11.",
    usage="私聊直接发消息；群聊 @机器人 或发送 ai <内容>。",
    config=Config,
    supported_adapters={"~onebot.v11"},
)


config = get_plugin_config(Config)


def _apply_bot_cpu_affinity(cfg: Config) -> None:
    """把 bot 主进程绑到指定核心，让 Ollama 用其他核。Windows 专用，失败静默。"""
    raw = getattr(cfg, "catty_cpu_affinity_mask", 0)
    if not raw or os.name != "nt":
        return
    try:
        mask = int(raw, 0) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return
    if mask <= 0:
        return
    try:
        import ctypes
        hproc = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.kernel32.SetProcessAffinityMask(hproc, mask)
        if ok:
            logger.info(f"catty bot process affinity set to {hex(mask)}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"failed to set catty bot affinity: {exc}")


_apply_bot_cpu_affinity(config)

memory_store = MemoryStore(config)
emoji_store = EmojiStore(config)
legs_picker = LegsPicker(config)
_owner_forward.init(config)
_legs_last_sent_at: dict[str, float] = {}
# poke 防刷屏：每个会话+用户 维度的最后回复时间戳
_poke_last_replied_at: dict[str, float] = {}
# 关键词回复 per-scope per-rule 冷却：key 形如 "group:123:rule:2"，值为 time.monotonic()
_keyword_reply_last_sent_at: dict[str, float] = {}

ChatMessage = dict[str, object]
# 会话历史消息数达到该阈值后，跳过教学型例句 prompt（catgirl_examples + disambiguation_examples）。
# 6 轮 user+assistant = 12 条消息。
HOT_SESSION_MIN_MESSAGES = 12
_session_cache: "SessionCache | None" = None


def _get_session_cache() -> "SessionCache":
    global _session_cache
    if _session_cache is None:
        _session_cache = SessionCache(
            directory=config.catty_session_cache_dir,
            max_sessions=config.catty_session_cache_max_sessions,
            persistence_enabled=config.catty_session_cache_persistence_enabled,
            debounce_seconds=config.catty_session_cache_save_debounce_seconds,
        )
        _session_cache.load_from_disk()
    return _session_cache

_locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_hot_reload_config_path: Path | None = None
_hot_reload_config_signature: tuple[int, int] | None = None
_hot_reload_emoji_signature: tuple[tuple[str, int, int], ...] = ()
_hot_reload_memory_signature: tuple[tuple[str, int, int], ...] = ()


@dataclass(slots=True)
class ExpressionRepeatState:
    signature: tuple[str, ...] | None = None
    count: int = 0
    last_seen: float = 0.0
    responded: bool = False


@dataclass(slots=True)
class GroupFilterBatchMessage:
    history_content: str
    has_image: bool = False


@dataclass(slots=True)
class GroupFilterBatchState:
    messages: list[GroupFilterBatchMessage] = field(default_factory=list)
    first_seen: float = 0.0


@dataclass(slots=True)
class RecentConversationMessage:
    message_id: str
    user_id: str
    display_name: str
    text: str
    has_image: bool
    created_at: float
    is_bot: bool = False
    target_user_id: str = ""


@dataclass(slots=True)
class BotReplyContinuationState:
    expires_at: float
    remaining_messages: int


_expression_repeats: DefaultDict[str, ExpressionRepeatState] = defaultdict(ExpressionRepeatState)
_group_filter_batches: DefaultDict[str, GroupFilterBatchState] = defaultdict(GroupFilterBatchState)
_group_filter_locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_recent_conversation_messages: DefaultDict[str, deque[RecentConversationMessage]] = defaultdict(lambda: deque(maxlen=80))
_bot_reply_continuations: dict[str, BotReplyContinuationState] = {}
_web_search_cooldowns: dict[str, float] = {}
WEB_SEARCH_REQUEST_PREFIX = "[[CATTY_WEB_SEARCH:"
WEB_SEARCH_REQUEST_SUFFIX = "]]"
_WEB_SEARCH_REQUEST_RE = re.compile(r"\[\[CATTY_WEB_SEARCH:\s*(.*?)\]\]", re.DOTALL)
NSFW_SEARCH_REQUEST_PREFIX = "[[CATTY_NSFW_SEARCH:"
NSFW_SEARCH_REQUEST_SUFFIX = "]]"
_NSFW_SEARCH_REQUEST_RE = re.compile(r"\[\[CATTY_NSFW_SEARCH:\s*(.*?)\]\]", re.DOTALL)
_nsfw_search_cooldowns: dict[str, float] = {}


_RESIDUAL_MARKER_KEEP = {"INLINE_IMAGE", "EMOJI_QUERY", "NO_REPLY", "REPLY_SPLIT"}


def _sanitize_residual_markers(text: str) -> str:
    """清掉所有 ``<<<CATTY_*>>>`` 和 ``[[CATTY_*]]`` 残留 marker,但保留发送链路/后续 stage 还要用的那几个。

    保留集合(``_RESIDUAL_MARKER_KEEP``):
    - INLINE_IMAGE: 发送链路 ``MessageSegment.image`` 要识别
    - EMOJI_QUERY: 下一步 ``_extract_emoji_query`` 提取
    - NO_REPLY: 下一步 ``_is_no_reply`` 检测
    - REPLY_SPLIT: 分段发送链路用
    其它全清(包括过去的 WEB_SEARCH / NSFW_SEARCH / MEME / 未来可能加的新 tool marker)。
    """
    if not text:
        return ""
    cleaned = _strip_catty_markers(text, keep=_RESIDUAL_MARKER_KEEP)
    # 兼容历史上的 [[CATTY_WEB_SEARCH:...]] / [[CATTY_NSFW_SEARCH:...]] 双方括号写法
    cleaned = _WEB_SEARCH_REQUEST_RE.sub("", cleaned)
    cleaned = _NSFW_SEARCH_REQUEST_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    if NO_REPLY_MARKER in cleaned and cleaned != NO_REPLY_MARKER:
        cleaned = cleaned.replace(NO_REPLY_MARKER, "").strip()
    return cleaned
_turtle_soup_cooldowns: dict[str, float] = {}
_local_critic_warmup_success_logged = False
_consumed_reply_source_ids: dict[str, float] = {}
_recent_emoji_paths: DefaultDict[str, deque[str]] = defaultdict(lambda: deque(maxlen=50))

_WAKE_CONTEXT_MIN_MESSAGES = 16
_WAKE_CONTEXT_MAX_MESSAGES = 50
_WAKE_CONTEXT_SOFT_DIRECTED_MESSAGES = 32
_WAKE_CONTEXT_CONTINUATION_MESSAGES = 44
_WAKE_CONTEXT_AFTER_MESSAGES = 6


def _has_api_key() -> bool:
    return bool(config.catty_openai_api_key.strip())


def _keyword_reply_event_allowed(event: MessageEvent) -> bool:
    if config.catty_allowed_user_ids and int(event.user_id) not in config.catty_allowed_user_ids:
        return False
    if isinstance(event, GroupMessageEvent):
        if not config.catty_enable_group:
            return False
        if config.catty_allowed_group_ids and int(event.group_id) not in config.catty_allowed_group_ids:
            return False
        return True
    if isinstance(event, PrivateMessageEvent):
        return config.catty_enable_private
    return False


def _keyword_reply_rule_enabled(rule: KeywordReplyRule) -> bool:
    return bool(getattr(rule, "enabled", True) and str(getattr(rule, "reply", "")).strip())


def _keyword_matches_text(text: str, keyword: str) -> bool:
    keyword = keyword.strip()
    if not text.strip() or not keyword:
        return False
    if re.fullmatch(r"[A-Za-z0-9_]+", keyword):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None
    return keyword.casefold() in text.casefold()


def _keyword_reply_for_text(text: str, *, scope: str = "") -> str:
    now = time.monotonic()
    for idx, rule in enumerate(config.catty_keyword_replies):
        if not _keyword_reply_rule_enabled(rule):
            continue
        if not any(_keyword_matches_text(text, str(keyword)) for keyword in rule.keywords):
            continue
        cooldown = max(float(getattr(rule, "cooldown_seconds", 0.0) or 0.0), 0.0)
        if scope and cooldown > 0:
            cd_key = f"{scope}:rule:{idx}"
            last = _keyword_reply_last_sent_at.get(cd_key, 0.0)
            if now - last < cooldown:
                # 该规则仍在冷却,尝试下一条规则(让其他无 CD 规则有机会接力)
                continue
            _keyword_reply_last_sent_at[cd_key] = now
        return rule.reply.strip()
    return ""


def _conversation_queue_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group:{event.group_id}"
    return f"private:{event.user_id}"


def _reply_source_key(event: MessageEvent, message_id: str) -> str:
    scope = _conversation_queue_key(event)
    return f"{scope}:reply-source:{message_id}"


def _bot_reply_continuation_key(scope: str, user_id: str) -> str:
    return f"{scope}:user:{user_id}"


def _prune_bot_reply_continuations(now: float) -> None:
    stale = [key for key, state in _bot_reply_continuations.items() if state.expires_at <= now or state.remaining_messages <= 0]
    for key in stale:
        _bot_reply_continuations.pop(key, None)


def _mark_bot_reply_continuation(scope: str, target_user_id: str, *, window_seconds: float = 180.0, messages: int = 3) -> None:
    if not target_user_id:
        return
    now = time.monotonic()
    _prune_bot_reply_continuations(now)
    _bot_reply_continuations[_bot_reply_continuation_key(scope, str(target_user_id))] = BotReplyContinuationState(
        expires_at=now + max(window_seconds, 1.0),
        remaining_messages=max(messages, 1),
    )


def _has_bot_reply_continuation(event: MessageEvent) -> bool:
    now = time.monotonic()
    _prune_bot_reply_continuations(now)
    key = _bot_reply_continuation_key(_conversation_queue_key(event), str(event.user_id))
    return key in _bot_reply_continuations


def _decrement_bot_reply_continuation(event: MessageEvent) -> None:
    now = time.monotonic()
    _prune_bot_reply_continuations(now)
    key = _bot_reply_continuation_key(_conversation_queue_key(event), str(event.user_id))
    state = _bot_reply_continuations.get(key)
    if state is None:
        return
    state.remaining_messages -= 1
    if state.remaining_messages <= 0:
        _bot_reply_continuations.pop(key, None)


def _bot_reply_continuation_remaining(event: MessageEvent) -> int:
    now = time.monotonic()
    _prune_bot_reply_continuations(now)
    key = _bot_reply_continuation_key(_conversation_queue_key(event), str(event.user_id))
    state = _bot_reply_continuations.get(key)
    return max(state.remaining_messages, 0) if state is not None else 0


def _recent_bot_prompted_user(event: MessageEvent, *, window_seconds: float = 180.0) -> bool:
    del window_seconds
    return _has_bot_reply_continuation(event)


def _reply_source_keys(event: MessageEvent) -> list[str]:
    return [_reply_source_key(event, message_id) for message_id in reply_message_ids(event)]


def _prune_consumed_reply_sources(now: float) -> None:
    max_age = 3600.0
    stale = [key for key, timestamp in _consumed_reply_source_ids.items() if now - timestamp > max_age]
    for key in stale:
        _consumed_reply_source_ids.pop(key, None)


def _has_consumed_reply_source(event: MessageEvent) -> bool:
    now = time.monotonic()
    _prune_consumed_reply_sources(now)
    return any(key in _consumed_reply_source_ids for key in _reply_source_keys(event))


def _mark_consumed_reply_source(event: MessageEvent) -> None:
    now = time.monotonic()
    _prune_consumed_reply_sources(now)
    for key in _reply_source_keys(event):
        _consumed_reply_source_ids[key] = now


def _mark_consumed_reply_source_if_sent(event: MessageEvent, state: T_State) -> None:
    if state.get("catty_replied_to_self"):
        _mark_consumed_reply_source(event)


def _soft_directed(incoming: ExtractedMessage) -> bool:
    return incoming.directed and not incoming.mentioned and not incoming.replied_to_self and not incoming.used_prefix


def _direct_reply_required(event: MessageEvent, incoming: ExtractedMessage) -> bool:
    if isinstance(event, PrivateMessageEvent):
        return True
    return bool(
        incoming.mentioned
        or incoming.replied_to_self
        or incoming.used_prefix
        or incoming.directed_strength == "direct_address"
    )


def _force_direct_reply_enabled(event: MessageEvent, incoming: ExtractedMessage) -> bool:
    if not (_direct_reply_required(event, incoming) and config.catty_local_critic_force_direct_reply):
        return False
    # 即使 @ 或回复了猫猫，如果消息明显是给别人看的，也走 critic 让它判断要不要回。
    # 触发条件：群聊里同时 @ 了其它用户，且本条消息文本里没有明显的指向猫猫信号。
    if isinstance(event, GroupMessageEvent) and mentions_other_user(str(event.self_id), event):
        text_lower = (incoming.text or "").strip().lower()
        # 文本里没有任何指向猫猫的关键词 / 直接称呼，就视为不是问猫猫
        if (
            incoming.directed_strength != "direct_address"
            and not incoming.used_prefix
            and "猫猫" not in text_lower
            and "猫娘" not in text_lower
            and "笨猫" not in text_lower
            and "ai" not in text_lower
        ):
            return False
    return True


def _clamp_probability(value: float) -> float:
    return max(min(float(value), 1.0), 0.0)


def _memory_reply_probability_boost(event: MessageEvent) -> tuple[float, str]:
    if not config.catty_memory_reply_boost_enabled:
        return 0.0, ""
    signal = memory_store.reply_boost_signal(event)
    if not signal:
        return 0.0, ""
    corpus_count = int(signal.get("corpus_count") or 0)
    profile_count = int(signal.get("profile_count") or 0)
    has_summary = bool(signal.get("has_summary"))
    min_corpus = max(int(config.catty_memory_reply_boost_min_corpus_messages), 1)
    if corpus_count < min_corpus and not has_summary and profile_count <= 0:
        return 0.0, ""

    reasons: list[str] = []
    if corpus_count >= min_corpus:
        reasons.append(f"已积累 {corpus_count} 条待压缩语料")
    if has_summary:
        reasons.append("已有长期摘要")
    if profile_count > 0:
        reasons.append(f"已有 {profile_count} 个画像")
    bonus = _clamp_probability(config.catty_memory_reply_boost_probability_bonus)
    return bonus, "；".join(reasons)


def _soft_directed_reply_probability(event: MessageEvent, incoming: ExtractedMessage) -> tuple[float, str]:
    if incoming.directed_strength == "direct_address":
        base = _clamp_probability(config.catty_direct_address_reply_probability)
    else:
        base = _clamp_probability(config.catty_soft_directed_reply_probability)
    boost, reason = _memory_reply_probability_boost(event)
    if boost <= 0:
        return base, ""
    cap = max(base, _clamp_probability(config.catty_memory_reply_boost_max_probability))
    return min(base + boost, cap), reason


def _display_name(event: MessageEvent) -> str:
    sender = getattr(event, "sender", None)
    for attr in ("card", "nickname"):
        value = getattr(sender, attr, "") if sender is not None else ""
        if value:
            return str(value)
    return str(event.user_id)


def _event_message_id(event: MessageEvent) -> str:
    return str(getattr(event, "message_id", "") or getattr(event, "id", "") or "")


def _remember_recent_conversation_event(event: MessageEvent, incoming: ExtractedMessage | None = None) -> None:
    text = (incoming.text if incoming is not None else event_plain_text(event)).strip()
    has_image = incoming.has_image if incoming is not None else bool(extract_image_urls(event))
    if not text and not has_image:
        return
    key = _conversation_queue_key(event)
    message_id = _event_message_id(event)
    recent = _recent_conversation_messages[key]
    if message_id and any(item.message_id == message_id for item in recent):
        return
    now = time.monotonic()
    if not message_id and recent:
        last = recent[-1]
        if last.user_id == str(event.user_id) and last.text == (text or "[图片]") and now - last.created_at < 2.0:
            return
    recent.append(
        RecentConversationMessage(
            message_id=message_id,
            user_id=str(event.user_id),
            display_name=_display_name(event),
            text=text or "[图片]",
            has_image=has_image,
            created_at=now,
            is_bot=False,
        )
    )


def _remember_bot_conversation_message(
    key: str,
    *,
    bot_id: str,
    text: str,
    message_id: str = "",
    target_user_id: str = "",
    has_image: bool = False,
) -> None:
    clean_text = text.strip()
    if not clean_text:
        return
    recent = _recent_conversation_messages[key]
    if message_id and any(item.message_id == message_id for item in recent):
        return
    now = time.monotonic()
    if not message_id and recent:
        last = recent[-1]
        if last.is_bot and last.text == clean_text and now - last.created_at < 2.0:
            return
    if target_user_id:
        _mark_bot_reply_continuation(key, str(target_user_id))
    recent.append(
        RecentConversationMessage(
            message_id=message_id,
            user_id=str(bot_id),
            display_name="笨猫",
            text=clean_text,
            has_image=has_image,
            created_at=now,
            is_bot=True,
            target_user_id=str(target_user_id or ""),
        )
    )


def _remember_bot_reply_for_event(event: MessageEvent, text: str) -> None:
    _remember_bot_conversation_message(
        _conversation_queue_key(event),
        bot_id=str(getattr(event, "self_id", "") or ""),
        text=text,
        target_user_id=str(event.user_id),
    )


def _remember_bot_repeat_for_event(event: MessageEvent, text: str) -> None:
    _remember_bot_conversation_message(
        _conversation_queue_key(event),
        bot_id=str(getattr(event, "self_id", "") or ""),
        text=text,
    )


def _ordered_unique_recent_messages(recent: list[RecentConversationMessage]) -> list[RecentConversationMessage]:
    ordered = sorted(recent, key=lambda item: item.created_at)
    seen_ids: set[str] = set()
    fallback_seen_at: dict[tuple[str, str, bool, str, bool], float] = {}
    unique: list[RecentConversationMessage] = []
    for item in ordered:
        message_id = item.message_id.strip()
        if message_id:
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            unique.append(item)
            continue

        fallback_key = (
            item.user_id,
            item.text,
            item.is_bot,
            item.target_user_id,
            item.has_image,
        )
        last_seen_at = fallback_seen_at.get(fallback_key)
        if last_seen_at is not None and item.created_at - last_seen_at < 2.0:
            continue
        fallback_seen_at[fallback_key] = item.created_at
        unique.append(item)
    return unique


def _wake_context_message_limit(
    event: MessageEvent,
    incoming: ExtractedMessage | None,
    *,
    group_filter_context: bool = False,
    bot_continuation: bool = False,
    recent: list[RecentConversationMessage] | None = None,
    current_index: int = -1,
) -> int:
    limit = _WAKE_CONTEXT_MIN_MESSAGES
    if isinstance(event, PrivateMessageEvent):
        limit = _WAKE_CONTEXT_SOFT_DIRECTED_MESSAGES
    if group_filter_context:
        limit = max(limit, _WAKE_CONTEXT_MIN_MESSAGES)
    if bot_continuation:
        limit = max(limit, _WAKE_CONTEXT_CONTINUATION_MESSAGES)
    if incoming is not None:
        if incoming.mentioned or incoming.replied_to_self or incoming.used_prefix:
            limit = _WAKE_CONTEXT_MAX_MESSAGES
        elif incoming.directed_strength == "direct_address":
            limit = max(limit, _WAKE_CONTEXT_CONTINUATION_MESSAGES)
        elif incoming.directed:
            limit = max(limit, _WAKE_CONTEXT_SOFT_DIRECTED_MESSAGES)
        if incoming.opportunistic or incoming.has_image:
            limit = max(limit, _WAKE_CONTEXT_SOFT_DIRECTED_MESSAGES)
    if recent is not None and current_index >= 0:
        nearby_start = max(0, current_index - 10)
        nearby_end = min(len(recent), current_index + 1)
        if any(item.is_bot for item in recent[nearby_start:nearby_end]):
            limit = max(limit, _WAKE_CONTEXT_CONTINUATION_MESSAGES)
    return max(_WAKE_CONTEXT_MIN_MESSAGES, min(limit, _WAKE_CONTEXT_MAX_MESSAGES))


def _find_current_recent_index(recent: list[RecentConversationMessage], event: MessageEvent) -> int:
    current_message_id = _event_message_id(event)
    if current_message_id:
        for index, item in enumerate(recent):
            if item.message_id == current_message_id:
                return index
    for index in range(len(recent) - 1, -1, -1):
        if recent[index].user_id == str(event.user_id):
            return index
    return len(recent) - 1


def _recent_context_window(
    recent: list[RecentConversationMessage],
    current_index: int,
    limit: int,
) -> tuple[int, int]:
    if not recent:
        return 0, 0
    limit = max(1, min(limit, len(recent)))
    after_count = min(len(recent) - current_index - 1, min(_WAKE_CONTEXT_AFTER_MESSAGES, limit // 4))
    before_count = limit - 1 - after_count
    start = max(0, current_index - before_count)
    end = min(len(recent), current_index + after_count + 1)
    if end - start < limit:
        start = max(0, end - limit)
    if end - start < limit:
        end = min(len(recent), start + limit)
    return start, end


def _wake_context_prompt(
    event: MessageEvent,
    incoming: ExtractedMessage | None = None,
    *,
    group_filter_context: bool = False,
    bot_continuation: bool = False,
) -> str:
    key = _conversation_queue_key(event)
    recent = _ordered_unique_recent_messages(list(_recent_conversation_messages.get(key, ())))
    if not recent:
        return ""
    current_index = _find_current_recent_index(recent, event)
    limit = _wake_context_message_limit(
        event,
        incoming,
        group_filter_context=group_filter_context,
        bot_continuation=bot_continuation,
        recent=recent,
        current_index=current_index,
    )
    start, end = _recent_context_window(recent, current_index, limit)
    lines: list[str] = []
    for index, item in enumerate(recent[start:end], start=start):
        marker = " <- 当前唤起消息" if index == current_index else ""
        image_marker = " [含图片]" if item.has_image else ""
        speaker = f"{item.display_name}({item.user_id})"
        if item.is_bot:
            speaker = f"笨猫自己({item.user_id})"
            if item.target_user_id:
                speaker += f" -> {item.target_user_id}"
        lines.append(f"{index - current_index:+d}. {speaker}: {item.text}{image_marker}{marker}")
    if not lines:
        return ""
    return (
        "当前是由一条消息唤起的回复。下面给出本会话独立实时上下文，已按时间顺序整理并去重；"
        f"群聊按群号隔离，最多 {limit} 条，本轮实际 {len(lines)} 条。"
        f"如果少于 {_WAKE_CONTEXT_MIN_MESSAGES} 条，说明当前会话暂时没有更多可用缓存。"
        "实时场景通常只有上文和当前消息，若没有下文不要臆造。"
        "请先定位带“<- 当前唤起消息”的发言者、它 @/回复/指向的对象，以及最近笨猫自己的发言；"
        "不要把别的群友发言误认成当前用户原文，也不要因为更早消息更热闹就偏离当前唤起消息。"
        "如果当前消息是在接前文、点名某个群友、要求评价某句称呼或梗，请结合上文选准回复目标；"
        "如果上下文显示是在让你攻击他人，保持轻度玩笑边界，不要升级辱骂。"
        "如果上一条或近几条是笨猫自己刚刚向当前用户追问/邀请继续说话，而当前消息像回答或续聊，通常应该接住。"
        f"请主 AI 自己判断是否真的需要回复；如果只是误触发、重复回复同一条消息、或上下文显示不该接话，只输出 {NO_REPLY_MARKER}。\n"
        + "\n".join(lines)
    )


def _bot_continuation_judgement_prompt(event: MessageEvent) -> str:
    remaining = _bot_reply_continuation_remaining(event)
    return (
        "本轮消息是因为笨猫刚刚回复过当前用户，所以被续聊窗口直接递送给主 AI 判断；"
        "**续聊资格 ≠ 自动续话**——每条消息都要重新判定是否还在跟笨猫对话。"
        "**默认倾向 NO_REPLY**，只在下面两种明确信号成立时才回复："
        "(1) 用户在直接回答笨猫刚才的问题/继续同一话题；"
        "(2) 用户用第二人称/命令/调戏/追问/技术求助句式指向笨猫（『你看看/你能不能/给我/帮我/怎么做/这套链路/代码/实现/方案/方式』等）。"
        f"**这些情况一律输出 {NO_REPLY_MARKER}**："
        "用户转去跟群里其他人讨论（聊车/关税/游戏/吃喝/工作/八卦等不指向笨猫的话题）、用户在跟群友互相吐槽/帮群友答问题/和群友互相 @、"
        "用户只是短情绪/感叹（『玩坏了』『悲』『笑死』『哈』『绷不住』这种顺势接你刚才那句的余韵）、"
        "第三人称闲聊、自言自语、转入新话题但没指向笨猫、误触发——"
        "不要因为「刚回过 ta」就抢话刷存在感，让用户跟群友自己聊。"
        "确实要回的技术求助：先给可执行技术结论或最小方案，再保持猫系口吻；"
        "必须优先覆盖用户句尾的请求目标（例如『给我一个方式/方案/怎么做』），不要只解释中间的局部术语；"
        "遇到『能不能用 A 给我一个 B 的方式/方案』这类句式，先回答 B 的方式/方案，再说明 A 链路的可行性和注意点；"
        "群消息里的『昵称(QQ): 正文』格式中，冒号后就是用户完整原文；只要正文已经形成完整问题，就按原文回答，"
        "不要说只看到几个词、消息被吃掉或要求重发。"
        f"当前续聊窗口剩余额度约 {remaining}；输出 {NO_REPLY_MARKER} 会消耗 1 次，真正回复会继续续上。"
    )


def _configured_title(event: MessageEvent) -> str:
    user_id = str(event.user_id)
    if isinstance(event, GroupMessageEvent):
        group_title = config.catty_group_user_titles.get(str(event.group_id), {}).get(user_id)
        if group_title:
            return str(group_title)
    return str(config.catty_user_titles.get(user_id) or "")


def _web_search_exempt(event: MessageEvent) -> bool:
    return bool(_configured_title(event).strip())


def _persona_search_cooldown_message(event: MessageEvent, remaining: float) -> str:
    title = _configured_title(event).strip() or _display_name(event)
    return (
        f"哼，{title}刚刚已经用过联网搜索啦喵～"
        f"每个人 10 分钟只有一次机会，还剩 {format_duration_cn(remaining)}，"
        "先让猫猫的搜索爪爪冷却一下。"
    )


def _runtime_config_path() -> Path | None:
    if _find_json_config_path is not None:
        return _find_json_config_path()
    path = Path.cwd() / "config.json"
    return path if path.is_file() else None


def _file_signature(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _tree_signature(paths: list[Path], *, suffixes: set[str] | None = None) -> tuple[tuple[str, int, int], ...]:
    entries: list[tuple[str, int, int]] = []
    seen: set[Path] = set()
    for root in paths:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = [path for path in root.rglob("*") if path.is_file()]
        else:
            continue
        for path in candidates:
            if suffixes is not None and path.suffix.lower() not in suffixes:
                continue
            try:
                stat = path.stat()
                key = str(path.resolve())
            except OSError:
                continue
            entries.append((key, stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))


def _config_from_environment() -> Config:
    values = {
        field_name: os.environ[env_name]
        for field_name in Config.model_fields
        if (env_name := field_name.upper()) in os.environ
    }
    return Config.model_validate(values)


def _load_runtime_config_from_path(path: Path) -> Config | None:
    if _apply_json_config is None:
        logger.warning("Hot reload skipped config reload because catty_config_loader is unavailable")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        logger.warning(
            f"Hot reload skipped invalid config.json at {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        )
        return None
    except OSError as exc:
        logger.warning(f"Hot reload failed to read config.json at {path}: {exc}")
        return None
    if not isinstance(data, dict):
        logger.warning(f"Hot reload skipped config.json because root is not an object: {path}")
        return None
    managed_env_names = {field_name.upper() for field_name in Config.model_fields}
    previous_env = {name: os.environ.get(name) for name in managed_env_names}
    try:
        for name in managed_env_names:
            os.environ.pop(name, None)
        _apply_json_config(data, path.parent)
        return _config_from_environment()
    except Exception as exc:
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        logger.warning(f"Hot reload skipped invalid config values from {path}: {exc}")
        return None


def _emoji_paths_for_config(current_config: Config) -> list[Path]:
    return [
        Path(current_config.catty_emoji_dir).expanduser(),
        Path(current_config.catty_emoji_download_dir).expanduser(),
        Path(current_config.catty_emoji_manifest_path).expanduser(),
    ]


def _memory_paths_for_store(store: MemoryStore) -> list[Path]:
    return [store.path, store.group_storage_dir, store.user_storage_dir]


def _emoji_signature_for_config(current_config: Config) -> tuple[tuple[str, int, int], ...]:
    return _tree_signature(_emoji_paths_for_config(current_config))


def _memory_signature_for_store(store: MemoryStore) -> tuple[tuple[str, int, int], ...]:
    return _tree_signature(_memory_paths_for_store(store), suffixes={".json"})


def _sync_hot_reload_signatures() -> None:
    global _hot_reload_config_path, _hot_reload_config_signature
    global _hot_reload_emoji_signature, _hot_reload_memory_signature
    _hot_reload_config_path = _runtime_config_path()
    _hot_reload_config_signature = _file_signature(_hot_reload_config_path)
    _hot_reload_emoji_signature = _emoji_signature_for_config(config)
    _hot_reload_memory_signature = _memory_signature_for_store(memory_store)


def _remember_hot_reload_config_signature(path: Path | None, signature: tuple[int, int] | None) -> None:
    global _hot_reload_config_path, _hot_reload_config_signature
    _hot_reload_config_path = path
    _hot_reload_config_signature = signature


def _apply_runtime_config(new_config: Config) -> None:
    global config, memory_store, emoji_store, legs_picker
    # 切实例前先把旧 memory_store 待写的脏数据落盘,避免 hot reload 丢失最近的记忆。
    try:
        if memory_store.flush_sync():
            logger.info("memory_store: flushed dirty data before hot reload")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"memory_store: pre-reload flush failed: {exc}")
    config = new_config
    memory_store = MemoryStore(config)
    emoji_store = EmojiStore(config)
    legs_picker = LegsPicker(config)
    _legs_last_sent_at.clear()
    _keyword_reply_last_sent_at.clear()
    _sync_hot_reload_signatures()
    # 旧实例的 background_flush_loop 还会跑(它现在指向脏标记永远 False 的孤儿对象),
    # 给新实例补起一个真正生效的后台 flush 协程。
    try:
        asyncio.create_task(memory_store.background_flush_loop())
    except RuntimeError:
        # _apply_runtime_config 也会在启动早期/同步上下文里被调用,那时没 event loop,
        # 启动钩子 start_memory_summary_loop 会负责把第一份 task 起起来,这里跳过即可。
        pass


def _reload_runtime_config_from_path(path: Path) -> bool:
    new_config = _load_runtime_config_from_path(path)
    if new_config is None:
        return False
    _apply_runtime_config(new_config)
    logger.info(f"Hot reloaded config.json: {path}")
    return True


async def _hot_reload_loop() -> None:
    _sync_hot_reload_signatures()
    while True:
        poll_seconds = max(float(config.catty_hot_reload_poll_seconds or 1.5), 0.2)
        await asyncio.sleep(poll_seconds)
        config_path = _runtime_config_path()
        config_signature = _file_signature(config_path)
        if config_path is not None and config_signature != _hot_reload_config_signature:
            if _reload_runtime_config_from_path(config_path):
                continue
            _remember_hot_reload_config_signature(config_path, config_signature)
        if not config.catty_hot_reload_enabled:
            continue
        emoji_signature = _emoji_signature_for_config(config)
        if emoji_signature != _hot_reload_emoji_signature:
            try:
                emoji_store.refresh()
                logger.info("Hot reloaded emoji files and manifest")
            except Exception as exc:
                logger.warning(f"Hot reload failed to refresh emoji store: {exc}")
            finally:
                _sync_hot_reload_signatures()
            continue
        memory_signature = _memory_signature_for_store(memory_store)
        if memory_signature != _hot_reload_memory_signature:
            try:
                memory_store.refresh()
                logger.info("Hot reloaded memory files")
            except Exception as exc:
                logger.warning(f"Hot reload failed to refresh memory store: {exc}")
            finally:
                _sync_hot_reload_signatures()


def _anger_reply_decision_context(
    event: MessageEvent,
    *,
    remaining: float,
    newly_muted: bool = False,
    reason: str = "",
) -> str:
    name = _display_name(event)
    state = "刚被 filter 粗筛判定进入少搭理冷却" if newly_muted else "仍处于少搭理冷却"
    reason_part = f"；粗筛原因：{reason.strip()[:120]}" if reason.strip() else ""
    return (
        "用户耐心条/少搭理状态（来自 filter 分类和本地记忆，不是最终回复）："
        f"对象是 {name}（QQ {event.user_id}），{state}，剩余约 {format_duration_cn(remaining)}{reason_part}。"
        f"filter 只负责分类和粗筛；请主 AI 根据整句主语、上下文和人格自行判断是否理会 {name}。"
        f"如果决定不理或冷处理，可以自然写自己的内心反应、短句敷衍或直接输出 {NO_REPLY_MARKER}；"
        "不要套用固定模板，也不要机械说“我不理你多久了”。"
    )


async def _build_web_search_context(query: str) -> str:
    try:
        results = await search_web(config, query)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"Web search failed for {query}: {exc}")
        return (
            f"本轮用户明确要求联网搜索「{query}」，但本地 Google/Bing 搜索插件调用失败。"
            "请用猫系人格如实说明这次联网查询失败，不要编造搜索结果、链接、日期或来源；"
            "可以基于已有知识给出有限建议，并提醒用户稍后重试。"
        )
    return format_search_context(query, results)


_NSFW_IMAGE_CACHE_DIR_NAME = "nsfw_cache"
_NSFW_SENT_REGISTRY_FILENAME = "sent_urls.json"
_NSFW_SENT_REGISTRY_MAX = 2000


def _nsfw_image_cache_dir() -> Path:
    base = Path(getattr(config, "catty_emoji_dir", "emojis") or "emojis")
    if not base.is_absolute():
        base = Path.cwd() / base
    cache = base / _NSFW_IMAGE_CACHE_DIR_NAME
    cache.mkdir(parents=True, exist_ok=True)
    return cache


class _NsfwSentRegistry:
    """记录已发过的 NSFW 图片 URL（pixiv artwork 链接 / kemono post 链接），
    持久化到 nsfw_cache/sent_urls.json。下次再搜到同一张就跳过，避免重发。
    """

    def __init__(self) -> None:
        self._urls: "OrderedDict[str, float]" = OrderedDict()
        self._loaded = False

    def _path(self) -> Path:
        return _nsfw_image_cache_dir() / _NSFW_SENT_REGISTRY_FILENAME

    def _load_if_needed(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"NSFW sent registry load failed: {exc}")
            return
        urls = raw.get("urls") if isinstance(raw, dict) else None
        if not isinstance(urls, dict):
            return
        for url, ts in urls.items():
            try:
                self._urls[str(url)] = float(ts)
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"urls": dict(self._urls)}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(f"NSFW sent registry save failed: {exc}")

    def has(self, url: str) -> bool:
        if not url:
            return False
        self._load_if_needed()
        return url in self._urls

    def mark(self, url: str) -> None:
        if not url:
            return
        self._load_if_needed()
        self._urls.pop(url, None)
        self._urls[url] = time.time()
        while len(self._urls) > _NSFW_SENT_REGISTRY_MAX:
            self._urls.popitem(last=False)
        self._save()


_nsfw_sent_registry = _NsfwSentRegistry()


def _prune_nsfw_image_cache(*, keep: int = 40) -> None:
    """LRU 清理：超过 keep 张就删最旧的，避免缓存撑爆磁盘。"""
    try:
        cache = _nsfw_image_cache_dir()
        files = sorted(cache.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError as exc:
        logger.debug(f"NSFW cache prune failed: {exc}")


def _ext_from_content_type(content_type: str, fallback: str = ".jpg") -> str:
    ctype = (content_type or "").lower().split(";", 1)[0].strip()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }
    return mapping.get(ctype, fallback)


async def _prepare_nsfw_image_segments(
    results: list[NsfwResult],
    *,
    max_images: int,
) -> tuple[list[MessageSegment], list[NsfwResult]]:
    """下载 pixiv 高分图片，写到本地缓存目录，发 file:// URI。
    比 base64 直发更稳：napcat 用本地文件走 QQ 上传通道，不会再 NT timeout。
    自动跳过 _nsfw_sent_registry 里已发过的 URL，避免重发同一张图。
    """
    segments: list[MessageSegment] = []
    used: list[NsfwResult] = []
    if max_images <= 0:
        return segments, used
    cache_dir = _nsfw_image_cache_dir()
    timestamp_seed = int(time.time() * 1000)
    skipped_already_sent = 0
    for result in results:
        if len(segments) >= max_images:
            break
        if not result.media_urls:
            continue
        if _nsfw_sent_registry.has(result.url):
            skipped_already_sent += 1
            continue
        downloaded = False
        for media_url in result.media_urls[:2]:
            try:
                image_data, content_type = await download_nsfw_image_bytes(
                    config, media_url, source=result.source
                )
            except httpx.HTTPError as exc:
                logger.warning(f"Failed to download NSFW image ({result.source}): {exc}")
                continue
            except ValueError as exc:
                logger.warning(f"Bad NSFW image response ({result.source}): {exc}")
                continue
            if not image_data:
                continue
            ctype = (content_type or "").lower()
            if ctype and not ctype.startswith("image/"):
                continue
            ext = _ext_from_content_type(ctype)
            timestamp_seed += 1
            file_path = cache_dir / f"{result.source}_{timestamp_seed}{ext}"
            try:
                file_path.write_bytes(image_data)
            except OSError as exc:
                logger.warning(f"Failed to write NSFW image cache file: {exc}")
                continue
            segments.append(MessageSegment.image(file=file_path.resolve().as_uri()))
            downloaded = True
            logger.info(
                f"NSFW image cached: src={result.source} bytes={len(image_data)} path={file_path.name}"
            )
            if len(segments) >= max_images:
                break
        if downloaded:
            used.append(result)
            # 立刻 mark：即使最终发送失败也算"用过"，下次别再选这张
            # 风控的图重试也救不回来；瞬时 timeout 的图无所谓再试一次同张
            _nsfw_sent_registry.mark(result.url)
    if skipped_already_sent:
        logger.info(f"NSFW: skipped {skipped_already_sent} already-sent results")
    _prune_nsfw_image_cache()
    return segments, used


def _reset_history(key: str) -> None:
    _get_session_cache().pop(key)


def _append_history(key: str, user_content: str, assistant_content: str) -> None:
    cache = _get_session_cache()
    history = list(cache.get(key))
    history.append({"role": "user", "content": user_content})
    history.append({"role": "assistant", "content": assistant_content})
    max_messages = max(config.catty_history_turns, 0) * 2
    if max_messages and len(history) > max_messages:
        history = history[-max_messages:]
    elif max_messages == 0:
        history = []
    cache.set(key, history)
    # 给训练 idle gate + dashboard conversation feed 用
    try:
        activity_feed.record_assistant_reply(
            scope=key,
            text=str(assistant_content or ""),
            triggered_by="chat_completion",
        )
    except Exception:  # noqa: BLE001
        pass


def _build_user_content(incoming: ExtractedMessage, *, image_description: str | None = None) -> object:
    if not incoming.image_urls:
        return incoming.history_content
    if image_description:
        return f"{incoming.history_content}\n图片识别结果：\n{image_description}\n请基于图片识别结果和上下文自然回应。"
    urls = "\n".join(f"- {url}" for url in incoming.image_urls)
    return f"{incoming.history_content}\n图片下载地址：\n{urls}\n请基于这些图片地址和上下文自然回应。"


def _emoji_reply_context(image_analysis: dict[str, object], candidates: str) -> str:
    tags = image_analysis.get("emotion_tags")
    tag_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
    candidate_text = candidates or "当前本地表情库没有直接命中的候选；如果很适合发图，可以输出表情意图，程序会尝试联网搜索并下载到表情库。"
    return (
        "本地表情库（猫娘表情包）由你自己判断要不要发——**不要每条都发，也不要从来不发**，"
        "大约每 3~5 条对话挑一条情绪/反应最浓的那条才发，普通陈述、连续刚发过表情、信息密集解释都不发。"
        "适合发的场景：被夸 / 撒娇 / 害羞 / 傲娇炸毛 / 接梗绷不住 / 贴贴黏人 / 玩闹起哄 / 被反撩——"
        "情绪明显且加表情真能强化语气时才发；只是顺嘴说话不发。"
        "想发就在回复正文末尾**独占一行**字面输出："
        f"{EMOJI_QUERY_PREFIX}你的表情意图{EMOJI_QUERY_SUFFIX}"
        "（这个标记原样输出，程序读到后挑图发；不要解释、不要变形、不要省略）。"
        "示例："
        f"{EMOJI_QUERY_PREFIX}害羞贴贴{EMOJI_QUERY_SUFFIX} / "
        f"{EMOJI_QUERY_PREFIX}得意被夸{EMOJI_QUERY_SUFFIX} / "
        f"{EMOJI_QUERY_PREFIX}脸红炸毛{EMOJI_QUERY_SUFFIX} / "
        f"{EMOJI_QUERY_PREFIX}绷不住笑{EMOJI_QUERY_SUFFIX}。"
        "表情意图可以写候选含义，也可以写贴近语境的情绪/动作/梗图意图；程序按语义匹配且避开最近重复。"
        "严肃排错、道歉、风险提醒、信息密集解释、或表情会打断语气时不要输出标记。"
        "再强调一遍：**节制使用，不要每条都贴**。\n"
        f"图片兴趣度：{image_analysis.get('interest', 0)}/100\n"
        f"图片/表情含义：{image_analysis.get('expression') or image_analysis.get('summary') or ''}\n"
        f"情绪标签：{tag_text}\n"
        f"可用表情候选：\n{candidate_text}"
    )


def _image_analysis_description(image_analysis: dict[str, object]) -> str:
    summary = str(image_analysis.get("summary") or "").strip()
    expression = str(image_analysis.get("expression") or "").strip()
    tags_value = image_analysis.get("emotion_tags")
    tags = [str(tag).strip() for tag in tags_value if str(tag).strip()] if isinstance(tags_value, list) else []
    if not summary and not expression and not tags:
        return ""
    lines = []
    if summary:
        lines.append(summary)
    lines.append(f"兴趣程度：{int(image_analysis.get('interest') or 0)}/100")
    if expression:
        lines.append(f"表情含义：{expression}")
    if tags:
        lines.append("情绪标签：" + ", ".join(tags))
    return "\n".join(lines).strip()


# ── 异步 vision 调度 ───────────────────────────────────────────────
# 视觉模型(analyze_images_for_reply / describe_images)耗时长(timeout 可达 120s+),
# 以前在 handle_chat 内 await 串行执行,把主回复整个卡住,同会话后续消息排队,
# 等锁释放就集中爆出回复 —— 这正是主人吐槽的"卡了之后爆一大堆"。
# 改造:消息一进来在 observe_memory 里 fire-and-forget 启 task,主回复链路只短等
# catty_vision_inline_max_wait_seconds 秒,等不到就不带 vision 描述直接回;
# 后台 task 跑完写 memory_store.remember_image_record,下一轮同图自动复用。
@dataclass(slots=True)
class _VisionResult:
    image_analysis: dict[str, Any]
    description: str


_vision_tasks: dict[str, asyncio.Task] = {}
_vision_results: dict[str, _VisionResult] = {}
_vision_done_events: dict[str, asyncio.Event] = {}
_VISION_RESULT_CACHE_MAX = 64


def _vision_cache_key(image_keys: list[str]) -> str:
    keys = [key.strip() for key in image_keys if key.strip()]
    return "|".join(keys) if keys else ""


def _vision_cache_lookup(image_keys: list[str]) -> _VisionResult | None:
    """同时考虑进程内 fresh 结果和 memory_store 落盘缓存。"""
    cache_key = _vision_cache_key(image_keys)
    if not cache_key:
        return None
    fresh = _vision_results.get(cache_key)
    if fresh is not None:
        return fresh
    summary = memory_store.get_image_summary(image_keys)
    if summary:
        return _VisionResult(image_analysis={}, description=summary)
    return None


def _schedule_vision_async(
    image_keys: list[str],
    image_urls: list[str],
    context: str,
) -> asyncio.Event | None:
    """没跑过 vision 就在后台启 task,返回 done event 给短等用;命中缓存返回 None。"""
    if not image_urls or not image_keys:
        return None
    if not config.catty_image_vision_enabled:
        return None
    if not config.catty_vision_async_enabled:
        return None
    if not (config.catty_vision_api_key.strip() or _has_api_key()):
        return None
    cache_key = _vision_cache_key(image_keys)
    if not cache_key:
        return None
    if cache_key in _vision_results:
        return None
    if memory_store.get_image_summary(image_keys):
        return None
    existing_event = _vision_done_events.get(cache_key)
    if existing_event is not None:
        return existing_event
    done_event = asyncio.Event()
    _vision_done_events[cache_key] = done_event

    short_key = cache_key[:24]

    async def _runner() -> None:
        try:
            analysis: dict[str, Any] = {}
            description = ""
            try:
                analysis = await analyze_images_for_reply(config, image_urls, context)
                description = _image_analysis_description(analysis)
            except OpenAICompatibleError as exc:
                logger.warning(f"Async vision analyze failed for {short_key}: {exc}")
            except httpx.HTTPError as exc:
                logger.warning(f"Async vision analyze transport error for {short_key}: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Async vision analyze unexpected error for {short_key}: {exc}")
            if not description:
                try:
                    description = await describe_images(config, image_urls, context)
                except OpenAICompatibleError as exc:
                    logger.warning(f"Async vision describe failed for {short_key}: {exc}")
                except httpx.HTTPError as exc:
                    logger.warning(f"Async vision describe transport error for {short_key}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Async vision describe unexpected error for {short_key}: {exc}")
            if description:
                try:
                    memory_store.remember_image_record(image_keys, description)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Async vision failed to write image record {short_key}: {exc}")
            _vision_results[cache_key] = _VisionResult(image_analysis=analysis, description=description)
            # 简单 LRU:超过上限就丢最早的一批,避免长期跑累积。
            if len(_vision_results) > _VISION_RESULT_CACHE_MAX:
                for stale_key in list(_vision_results.keys())[: len(_vision_results) - _VISION_RESULT_CACHE_MAX]:
                    _vision_results.pop(stale_key, None)
        finally:
            done_event.set()
            _vision_tasks.pop(cache_key, None)
            # done_event 留 60s 给后到的等待者复用,之后清掉
            async def _evict_event() -> None:
                await asyncio.sleep(60.0)
                _vision_done_events.pop(cache_key, None)
            asyncio.create_task(_evict_event(), name=f"catty-vision-evict-{short_key}")

    task = asyncio.create_task(_runner(), name=f"catty-vision-{short_key}")
    _vision_tasks[cache_key] = task
    return done_event


async def _await_vision_briefly(image_keys: list[str], max_wait: float) -> _VisionResult | None:
    """缓存优先,没命中就最多等 max_wait 秒,超时返回 None(主回复继续不卡)。"""
    cached = _vision_cache_lookup(image_keys)
    if cached is not None:
        return cached
    if not image_keys or max_wait <= 0:
        return None
    cache_key = _vision_cache_key(image_keys)
    if not cache_key:
        return None
    event = _vision_done_events.get(cache_key)
    if event is None:
        return None
    try:
        await asyncio.wait_for(event.wait(), timeout=max_wait)
    except asyncio.TimeoutError:
        logger.info(
            f"Vision wait timed out at {max_wait:.1f}s for {cache_key[:24]}; main reply continues without image description"
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Vision wait unexpected error for {cache_key[:24]}: {exc}")
        return None
    return _vision_cache_lookup(image_keys)


def _emoji_segment(entry: EmojiEntry) -> MessageSegment:
    return MessageSegment.image(file=entry.path.resolve().as_uri())


def _emoji_entry_key(entry: EmojiEntry) -> str:
    return str(entry.path.resolve())


def _emoji_candidate_pool_limit() -> int:
    return max(int(config.catty_emoji_max_candidates), int(config.catty_emoji_diversity_candidate_pool), 1)


def _recent_emoji_keys(event: MessageEvent) -> set[str]:
    if not config.catty_emoji_diversity_enabled:
        return set()
    window = max(int(config.catty_emoji_diversity_recent_window), 0)
    if window <= 0:
        return set()
    recent = _recent_emoji_paths[_conversation_queue_key(event)]
    return set(list(recent)[-window:])


def _remember_emoji_choice(event: MessageEvent, entry: EmojiEntry) -> None:
    if not config.catty_emoji_diversity_enabled:
        return
    if max(int(config.catty_emoji_diversity_recent_window), 0) <= 0:
        return
    _recent_emoji_paths[_conversation_queue_key(event)].append(_emoji_entry_key(entry))


def _unique_emoji_entries(entries: list[EmojiEntry]) -> list[EmojiEntry]:
    unique: list[EmojiEntry] = []
    seen: set[str] = set()
    for entry in entries:
        key = _emoji_entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _select_diverse_emoji(event: MessageEvent, entries: list[EmojiEntry]) -> EmojiEntry | None:
    unique = _unique_emoji_entries(entries)
    if not unique:
        return None
    if not config.catty_emoji_diversity_enabled or len(unique) == 1:
        return unique[0]

    pool_limit = min(max(int(config.catty_emoji_diversity_candidate_pool), 1), len(unique))
    pool = unique[:pool_limit]
    recent_keys = _recent_emoji_keys(event)
    fresh_pool = [entry for entry in pool if _emoji_entry_key(entry) not in recent_keys]
    choices = fresh_pool or pool
    weights = [1.0 / ((pool.index(entry) + 1) ** 0.85) for entry in choices]
    return random.choices(choices, weights=weights, k=1)[0]


def _choose_matching_emoji(
    event: MessageEvent,
    query: str,
    *,
    tags: list[str] | None = None,
    refresh_on_miss: bool = False,
) -> EmojiEntry | None:
    entries = emoji_store.select(query, tags=tags, limit=_emoji_candidate_pool_limit())
    if entries:
        return _select_diverse_emoji(event, entries)
    if refresh_on_miss and config.catty_emoji_enabled:
        emoji_store.refresh()
        entries = emoji_store.select(query, tags=tags, limit=_emoji_candidate_pool_limit())
        if entries:
            return _select_diverse_emoji(event, entries)
    return None


def _generic_emoji_context(incoming: ExtractedMessage) -> str:
    if not config.catty_emoji_enabled:
        return ""
    candidates = emoji_store.candidates_text(incoming.text)
    return _emoji_reply_context(
        {
            "interest": 100,
            "expression": incoming.text,
            "summary": incoming.text,
            "emotion_tags": [],
        },
        candidates,
    )


def _should_auto_emoji_reply(incoming: ExtractedMessage, reply: str) -> bool:
    if not config.catty_emoji_auto_fallback_enabled:
        return False
    if not config.catty_emoji_enabled or not config.catty_emoji_reply_enabled:
        return False
    if not reply.strip() or _is_no_reply(reply):
        return False
    probability = max(min(float(config.catty_emoji_reply_probability), 1.0), 0.0)
    if probability <= 0:
        return False
    if incoming.opportunistic and probability < 1.0:
        probability *= 0.5
    return random.random() < probability


def _choose_auto_emoji(event: MessageEvent, reply: str, incoming: ExtractedMessage) -> EmojiEntry | None:
    candidates = _unique_emoji_entries(
        [
            *emoji_store.select(reply, limit=_emoji_candidate_pool_limit()),
            *emoji_store.select(incoming.text, limit=_emoji_candidate_pool_limit()),
        ]
    )
    entry = _select_diverse_emoji(event, candidates)
    if entry is not None:
        return entry
    candidates = emoji_store.select("", limit=_emoji_candidate_pool_limit())
    return _select_diverse_emoji(event, candidates)


def _emoji_entry_data_url(entry: EmojiEntry) -> str:
    content_type = mimetypes.guess_type(entry.path.name)[0] or "image/jpeg"
    data = entry.path.read_bytes()
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


async def _enrich_emoji_metadata_with_vision_ai(
    entry: EmojiEntry,
    *,
    query: str,
    context_text: str,
) -> EmojiEntry:
    if not (config.catty_vision_api_key.strip() or _has_api_key()):
        return entry
    try:
        analysis = await analyze_images_for_reply(
            config,
            [_emoji_entry_data_url(entry)],
            (
                "请给这个 QQ 猫娘聊天表情包写入库标签。"
                f"用户想要的表情意图：{query[:120]}；"
                f"当前聊天上下文：{context_text[:240]}；"
                f"文件名：{entry.path.name}。"
                "请重点判断它适合表达的情绪、动作、猫系/梗图用途。"
            ),
        )
    except (OpenAICompatibleError, httpx.HTTPError, OSError) as exc:
        logger.warning(f"Failed to enrich emoji metadata with vision AI for {entry.path.name}: {exc}")
        return entry
    if not analysis:
        return entry
    meaning = str(analysis.get("expression") or analysis.get("emoji_query") or analysis.get("summary") or entry.meaning).strip()
    raw_tags = analysis.get("emotion_tags")
    tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()] if isinstance(raw_tags, list) else []
    emoji_query = str(analysis.get("emoji_query") or "").strip()
    if emoji_query:
        tags.append(emoji_query)
    if query:
        tags.append(query)
    updated = emoji_store.update_metadata(entry, meaning=meaning, tags=tags, source=entry.source, priority=entry.priority)
    if updated is not None:
        logger.info("Updated emoji metadata with vision AI: %s -> %s [%s]", entry.path.name, meaning, ", ".join(tags[:8]))
        return updated
    return entry


async def _choose_or_download_emoji(
    event: MessageEvent,
    query: str,
    incoming: ExtractedMessage,
    image_analysis: dict[str, object],
) -> EmojiEntry | None:
    if not query.strip() or not config.catty_emoji_enabled:
        return None

    tags_value = image_analysis.get("emotion_tags")
    tags = [str(tag) for tag in tags_value] if isinstance(tags_value, list) else []
    entry = _choose_matching_emoji(event, query, tags=tags, refresh_on_miss=True)
    if entry is not None:
        return entry

    entry = emoji_store.adopt_downloaded(query, tags=tags)
    if entry is not None:
        logger.info("Adopted downloaded emoji for query %s: %s", query, entry.path)
        return await _enrich_emoji_metadata_with_vision_ai(entry, query=query, context_text=incoming.text)

    image_urls = list(incoming.image_urls)
    if not image_urls:
        try:
            image_urls = await search_image_urls(config, f"{query} 猫猫 表情包", max_results=6)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"Failed to search emoji image for {query}: {exc}")
        if not image_urls:
            logger.info("No downloadable emoji image found for query %s", query)
            return None

    for image_url in image_urls[:6]:
        try:
            image_data, content_type = await download_binary(config, image_url)
            if content_type and not content_type.lower().startswith("image/"):
                continue
            entry = emoji_store.save_downloaded(
                image_data=image_data,
                content_type=content_type,
                source_url=image_url,
                meaning=query,
                tags=[*tags, query],
                interest=max(config.catty_emoji_save_interest_threshold, 50),
            )
        except httpx.HTTPError as exc:
            logger.warning(f"Failed to download missing emoji candidate for {query}: {exc}")
            continue
        except OSError as exc:
            logger.warning(f"Failed to save missing emoji candidate for {query}: {exc}")
            continue
        if entry is not None:
            logger.info("Downloaded emoji image for query %s: %s", query, entry.path)
            return await _enrich_emoji_metadata_with_vision_ai(entry, query=query, context_text=incoming.text)
    return None


# 用户回指最近图片时常见说法。命中即程序自动 inject 最近 has_image corpus 条目,
# 让主 AI 直接看到 vision 描述,不必依赖 AI 主动调 catty_recall。
# 用窄正则避免过度触发(普通"这个/那个"日常太常见,不算)。
_RECENT_IMAGE_REFERENCE_RE = re.compile(
    r"(?:刚才|上次|前面|之前|刚刚|前两|前几|上面)(?:那|这|的)?(?:张|个|条|段)?(?:图|照|图片|截图|帖图)"
    r"|(?:那|这)(?:张|个|条)?(?:图|照|图片|截图)"
    r"|(?:还记得|记不记得|认得|认识|看清|看不清|没看清|认不出|看出来)(?:这|那|刚才|上次|前面|之前)?(?:张|个|条)?(?:图|照|图片|截图)?"
    r"|(?:这|那)图(?:是|说|意思|什么|啥)"
)


def _references_recent_image(text: str) -> bool:
    if not text or "图" not in text and "照" not in text and "截图" not in text:
        return False
    return bool(_RECENT_IMAGE_REFERENCE_RE.search(text))


def _build_recent_image_reference_hint(event: MessageEvent, incoming: ExtractedMessage) -> str:
    """检测用户消息含图片回指 → 从 corpus 拉最近 has_image 条目 inject 给主 AI。

    返回拼好的 system message 文本(空字符串表示无需 inject)。
    """
    if not _references_recent_image(incoming.text):
        return ""
    try:
        recent_images = memory_store.collect_recent_image_descriptions(event, limit=3)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"collect_recent_image_descriptions failed: {exc}")
        return ""
    if not recent_images:
        return ""
    lines: list[str] = [
        "用户这一句明显在回指**之前出现过的图片**(『那张/刚才那张/认得这张』之类)。"
        "下面给出当前会话最近的图片记忆(corpus 里 has_image 条目,时间倒序),"
        "请你**优先在这几条里找用户指代的那张**,认出来就基于 vision 描述回答;"
        "如果都对不上,可以用一句猫娘口吻说『人家也没看清这张是啥喵～主人形容下』,"
        "不要硬猜也不要否认有图。"
    ]
    for index, img in enumerate(recent_images, 1):
        ts = img.get("time", "")[:19]
        speaker = img.get("display_name") or img.get("user_id") or "群友"
        lines.append(f"{index}. [{ts}] {speaker}: {img.get('text', '')}")
    lines.append(
        "如果需要更早的图(超出本会话最近 3 张),再考虑调 catty_recall(keywords=『图片内容 关键词』)查 corpus。"
    )
    return "\n".join(lines)


def _build_messages(
    event: MessageEvent,
    key: str,
    incoming: ExtractedMessage,
    *,
    image_description: str | None = None,
    anger_context: str | None = None,
    semantic_reply_split: bool = False,
    group_filter_context: str | None = None,
    special_care_context: str | None = None,
    emoji_context: str | None = None,
    web_search_context: str | None = None,
    star_resonance_context: str | None = None,
    strinova_context: str | None = None,
    other_game_contexts: list[str] | None = None,
    wake_context: str | None = None,
    bot_continuation_context: str | None = None,
) -> list[ChatMessage]:
    messages: list[ChatMessage] = []

    # 提前读历史以判定会话热度：≥ HOT_SESSION_MIN_MESSAGES 条历史时跳过最长的教学例句，
    # 模型已经能从历史里看到自身口吻，省下大约 30-40% 的 system token。
    history_messages = list(_get_session_cache().get(key))
    is_cold_session = len(history_messages) < HOT_SESSION_MIN_MESSAGES

    # ─── Layer A: 完全稳定的人格 + 流水线，最大化 prompt cache prefix 命中 ───
    system_prompt = config.catty_system_prompt.strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    persona_memory = build_persona_memory_prompt(system_prompt)
    if persona_memory:
        messages.append({"role": "system", "content": persona_memory})
    messages.append({"role": "system", "content": build_group_meme_literacy_prompt()})
    messages.append({"role": "system", "content": build_conversation_flow_prompt()})
    messages.append({"role": "system", "content": build_semantic_perception_prompt()})
    messages.append({"role": "system", "content": build_scenario_playbook_prompt(NO_REPLY_MARKER)})
    messages.append({"role": "system", "content": build_scene_discrimination_prompt(NO_REPLY_MARKER)})
    messages.append({"role": "system", "content": build_reply_intelligence_prompt(NO_REPLY_MARKER)})
    messages.append({"role": "system", "content": build_qq_chat_rhythm_prompt(REPLY_SPLIT_MARKER)})
    if config.catty_reply_self_check_enabled:
        messages.append(
            {
                "role": "system",
                "content": build_reply_self_check_prompt(NO_REPLY_MARKER, REPLY_SPLIT_MARKER),
            }
        )
    messages.append({"role": "system", "content": _reply_gate_approved_prompt()})

    # ─── Layer B: function calling tools 提示常驻挂载 ───
    # web_search/nsfw_search/meme 全部走 tools 字段(OpenAI function calling),
    # 旧的 [[CATTY_WEB_SEARCH]] / [[CATTY_NSFW_SEARCH]] / <<<CATTY_MEME>>> 文本 marker 教学已废弃。
    # emoji 还是 reply 后置 enrich,保留 EMOJI_QUERY marker 那条老路径。
    if getattr(config, "catty_tools_enabled", True):
        messages.append({"role": "system", "content": tools_system_hint()})

    # ─── Layer C: 教学例句，仅冷会话挂（热会话从历史学习风格） ───
    if config.catty_reply_style_examples_enabled and is_cold_session:
        messages.append({"role": "system", "content": build_catgirl_examples_prompt(NO_REPLY_MARKER, REPLY_SPLIT_MARKER)})
        messages.append({"role": "system", "content": build_disambiguation_examples_prompt(NO_REPLY_MARKER)})

    # ─── Layer D: 按事件可能变 ───
    if image_description:
        messages.append({"role": "system", "content": build_image_literacy_prompt()})
    if _force_direct_reply_enabled(event, incoming):
        messages.append({"role": "system", "content": _direct_reply_required_prompt(incoming)})
    if semantic_reply_split:
        messages.append({"role": "system", "content": _semantic_reply_split_prompt()})
    if incoming.opportunistic or group_filter_context:
        messages.append({"role": "system", "content": _opportunistic_reply_prompt()})
    if _soft_directed(incoming):
        probability, memory_boost_reason = _soft_directed_reply_probability(event, incoming)
        messages.append(
            {
                "role": "system",
                "content": _soft_directed_reply_prompt(
                    incoming,
                    reply_probability=probability,
                    memory_boost_reason=memory_boost_reason,
                ),
            }
        )
    if group_filter_context:
        messages.append({"role": "system", "content": group_filter_context})
    if special_care_context:
        messages.append({"role": "system", "content": special_care_context})
    if anger_context:
        messages.append({"role": "system", "content": anger_context})
    if web_search_context:
        messages.append({"role": "system", "content": web_search_context})
    if star_resonance_context:
        messages.append({"role": "system", "content": star_resonance_context})
    if strinova_context:
        messages.append({"role": "system", "content": strinova_context})
    for game_ctx in other_game_contexts or []:
        if game_ctx:
            messages.append({"role": "system", "content": game_ctx})
    if wake_context:
        messages.append({"role": "system", "content": wake_context})
    if bot_continuation_context:
        messages.append({"role": "system", "content": bot_continuation_context})
    if emoji_context:
        messages.append({"role": "system", "content": emoji_context})
    memory_context = memory_store.build_context(event)
    if memory_context:
        messages.append({"role": "system", "content": memory_context})
    # 程序自动判断:用户消息含图片回指词且当前轮没在发新图时,
    # 直接从 corpus 拉最近 has_image 条目 inject 给主 AI,不用 AI 主动调 catty_recall。
    # 命中场景:『刚才那张图』『认得这张吗』『前面那个截图』等。
    if not incoming.has_image:
        recent_image_hint = _build_recent_image_reference_hint(event, incoming)
        if recent_image_hint:
            messages.append({"role": "system", "content": recent_image_hint})
    # 本地 QQ/网络黑话翻译注入:命中『xs/u1s1/awsl/绷不住/破防』等高频缩写时,
    # 直接告诉 AI 对应中文意思,免得调 catty_meme_explain 浪费一次工具调用。
    slang_context = build_slang_context(incoming.text)
    if slang_context:
        messages.append({"role": "system", "content": slang_context})
    # 群消息节奏感知:冷场/刷屏/复读/热闹时提示 AI 调整发言风格;
    # normal 节奏不打扰,避免每条消息都灌一段空话占 prompt。
    pulse_key = _conversation_queue_key(event)
    pulse_msgs = _recent_conversation_messages.get(pulse_key)
    pulse_phase = "normal"
    if pulse_msgs:
        pulse_now = time.monotonic()
        pulse_result = analyze_pulse(pulse_msgs, now=pulse_now)
        pulse_phase = pulse_result.phase
        pulse_context = build_pulse_context(pulse_msgs, now=pulse_now)
        if pulse_context:
            messages.append({"role": "system", "content": pulse_context})
    # 入向消息意图分类:question/tease_cat/compliment_cat 等多标签;
    # 给 AI 反应方向建议(撒娇/嘴硬/给答案/调 tool),减少 AI 自己空想意图的负担。
    intent_context = build_intent_context(incoming.text, has_image=incoming.has_image)
    if intent_context:
        messages.append({"role": "system", "content": intent_context})
    # 入向消息关键实体提取:time/money/count/qq_id 等容易被 AI 漏读的事实;
    # URL/@提及 不进 prompt(AI 看得见原文,标会重复)。
    entity_context = build_entity_context(incoming.text)
    if entity_context:
        messages.append({"role": "system", "content": entity_context})
    # 解析层联动建议:交叉 intent + entity + pulse 给具体下一步建议
    # (例如未来时间+命令 → 建议 catty_remember;qq_id+不是发言者 → 建议 catty_user_profile)。
    sender_qq_str = str(event.user_id) if event is not None else ""
    action_hint_context = build_action_hints(
        incoming.text,
        has_image=incoming.has_image,
        pulse_phase=pulse_phase,
        sender_qq=sender_qq_str,
    )
    if action_hint_context:
        messages.append({"role": "system", "content": action_hint_context})
    messages.extend(history_messages)
    messages.append({"role": "user", "content": _build_user_content(incoming, image_description=image_description)})
    return messages


def _is_reset_request(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {
        "reset",
        "/reset",
        "clear",
        "/clear",
        "清空",
        "清空上下文",
        "重置",
        "重置上下文",
        "忘掉上文",
    }


def _compact_text(text: str) -> str:
    return "".join(text.strip().lower().split())


def _is_session_list_request(text: str) -> bool:
    compact = _compact_text(text)
    return compact in {
        "ai会话列表",
        "ai列会话",
        "ai列出会话",
        "ai查看会话",
        "ai看看会话",
        "ai会话",
        "ai所有会话",
        "ai sessions",
        "aisessions",
        "/sessions",
        "/会话列表",
        "/会话",
        "会话列表",
        "列会话",
        "查看会话",
        "看看会话",
    }


def _is_memory_cache_clear_request(text: str) -> bool:
    compact = _compact_text(text)
    return compact in {
        "clearcache",
        "/clearcache",
        "清空缓存",
        "清除缓存",
        "清理缓存",
        "清空记忆缓存",
        "清除记忆缓存",
        "清空当前缓存",
        "清空当前记忆缓存",
    }


def _is_memory_view_request(text: str) -> bool:
    compact = _compact_text(text)
    explicit_commands = {
        "memory",
        "/memory",
        "记忆",
        "查看记忆",
        "看看记忆",
        "显示记忆",
        "读取记忆",
        "查看存储",
        "查看人物信息",
        "查看群友信息",
        "查看人物画像",
        "查看群友画像",
        "你记得我什么",
    }
    if compact in explicit_commands:
        return True
    view_words = ("查看", "看看", "显示", "调出", "读取", "列出")
    memory_words = ("记忆", "存储", "人物信息", "群友信息", "画像")
    return any(word in compact for word in view_words) and any(word in compact for word in memory_words)


def _expression_repeat_message(bot: Bot, event: MessageEvent) -> Message | None:
    if not isinstance(event, GroupMessageEvent) or str(event.user_id) == str(bot.self_id):
        return None
    if not config.catty_expression_repeat_enabled:
        return None

    signature = expression_message_signature(
        event,
        include_images=config.catty_expression_repeat_include_images,
        include_text=config.catty_expression_repeat_include_text,
    )
    key = f"group:{event.group_id}"
    state = _expression_repeats[key]
    now = time.monotonic()

    if signature is None:
        state.signature = None
        state.count = 0
        state.last_seen = now
        state.responded = False
        return None

    window_seconds = max(config.catty_expression_repeat_window_seconds, 1.0)
    if state.signature != signature or now - state.last_seen > window_seconds:
        state.signature = signature
        state.count = 1
        state.responded = False
    else:
        state.count += 1
    state.last_seen = now

    threshold = max(config.catty_expression_repeat_threshold, 2)
    if state.count >= threshold and not state.responded:
        state.responded = True
        return Message(event.message)
    return None


def _semantic_reply_split_prompt() -> str:
    max_chunks = max(config.catty_reply_human_split_max_chunks, 1)
    return (
        "QQ 回复分段规则——按真实人类节奏拆，不要过度拆碎也不要全挤一团："
        "判断标准：把回复念出来，听起来像『一气呵成』还是『有几个独立轮次』？"
        "【一气呵成 → 单条】几个短句串成一个完整想法（看到图+评论这张+说没识别+让重发，全是同一个意思的展开），"
        "用逗号/句号连成一条 QQ 消息发，不要每短句单切。"
        "【真有几个轮次 → 拆 2~3 条】对前文的强反应 + 接下来要说的新事情；技术结论 + 后续追问；强情绪 + 话题展开。"
        "**反例**（过度拆，禁止这样）：『这个表情猫猫看到了』『呆呆小仓鼠那张嘛』『但题目那张没识别出』『主人重发』"
        "——一个完整想法被切碎，应合成一条：『这个表情看到啦～呆呆小仓鼠那张挺无辜，但题目那张没识别出，主人重发一次猫猫马上做』。"
        "**正例**（自然拆 3 条）：『哈？？』『主人你这题也太缺德了喵』『猫猫平时是优雅蹲坑型，尾巴盘好、结束还要疯狂埋埋，绝不承认会炸毛喵！』"
        "——『反应』『吐槽』『话题展开』三个真实轮次。"
        f"拆分方法两种都行：(A) 输出 {REPLY_SPLIT_MARKER}；(B) 直接换行 \\n。系统都接住。"
        "被拆的前几条结尾少用句号/感叹号，自然些。"
        f"上限 {max_chunks} 条；超短回复（<15 字）单条。"
        "**数学/公式**：系统会自动把 LaTeX 块渲染成图片再发出去，你**可以放心**用 `\\[ ... \\]`（display math）或 `\\( ... \\)`（inline math）包公式（不要用单 $ ... $，会被忽略）。matplotlib mathtext 子集支持 \\frac、\\sqrt、\\int、\\sum、\\lim、上下标、希腊字母、\\boxed 等常用；array、tikz、自定义宏不支持，复杂表格用纯文本。"
        "**Markdown**：QQ 群不渲染 Markdown,不要 **加粗**/`代码`/# 标题/``` 代码块 ```;代码直接写正文,分点用换行+「1)」「2)」。"
    )


def _opportunistic_reply_prompt() -> str:
    return (
        "这是由普通群聊、特别关心或批量观察进入主 AI 判断的消息。"
        f"请先判断当前消息是不是在跟猫猫说话；如果只是 A 对 B、第三人称聊你、顺手提到你、误触发或没人期待你接话，只输出 {NO_REPLY_MARKER}。"
    )


def _reply_gate_approved_prompt() -> str:
    return (
        "本轮消息已经通过入口唤起，最终是否回复交给主 AI 结合上下文判断。"
        "**关键判断：看猫猫上轮是不是刚回过**——"
        "如果上轮已经接过一句，这轮用户只是顺势感叹/吐槽/接你刚才那句话的余韵（『玩坏了』『悲』『笑死』『哈』『笑死人』『绷不住』这种没指向猫猫的短情绪/感叹/吐槽），"
        f"或者群友在评论你的回答而不是继续问你——一律输出 {NO_REPLY_MARKER}，让对话自然落幕，不要追着群友接话。"
        "**特别注意：主人/特别关心用户在跟群里其他人对话时也要 NO_REPLY**——"
        "比如主人在和群友聊比亚迪/关税/游戏/吃喝这种话题、主人在跟群友互相吐槽、主人在帮群友答问题、主人和群友互相 @——"
        f"只要话题不指向你、没有问你、没有求你做事，就**让主人专心跟群友聊**，输出 {NO_REPLY_MARKER}；不要因为是主人/熟人就抢话刷存在感。"
        f"另外这些情况也输出 {NO_REPLY_MARKER}：误触发、重复回复同一条、A 对 B 说话、第三人称提到猫猫、顺手 @ 你但内容不是问你。"
        "**只在真的在等猫猫接话时才回复**：直接问猫猫问题、明确求猫猫做事、新一轮主动喊猫猫、群里冷场期待猫猫起话。"
        "信息不足时用笨猫口吻短短追问。"
    )


def _direct_reply_required_prompt(incoming: ExtractedMessage) -> str:
    reasons: list[str] = []
    if incoming.mentioned:
        reasons.append("用户明确 @ 你")
    if incoming.replied_to_self:
        reasons.append("用户回复了你的消息")
    if incoming.used_prefix:
        reasons.append("用户使用了你的触发前缀")
    if incoming.directed_strength == "direct_address":
        reasons.append("本地判断是直接喊名/叫你办事")
    reason_text = "、".join(reasons) or "这是私聊或明确对你说话"
    return (
        f"本轮属于必须回复场景：{reason_text}。"
        f"通常应该接话；但如果上下文显示是在重复回复同一条消息、顺手 @ 到猫猫、A 对 B 说话、误触发或明显不该接话，可以输出 {NO_REPLY_MARKER}。"
        "如果信息不足，就用笨猫口吻短短追问；如果只是 @/回复但没文字，也可以自然应一声。"
    )


def _soft_directed_reply_prompt(
    incoming: ExtractedMessage,
    *,
    reply_probability: float,
    memory_boost_reason: str = "",
) -> str:
    probability_percent = round(_clamp_probability(reply_probability) * 100)
    boost_text = f"；记忆加成原因：{memory_boost_reason}" if memory_boost_reason else ""
    if incoming.directed_strength == "direct_address":
        return (
            "本轮没有明确 @ 你、回复你或使用严格开头前缀，但本地判断更像是在直接喊你/叫你办事。"
            f"当前回复倾向约 {probability_percent}%{boost_text}。"
            "请结合上下文自己判断是否真的要接话；如果该接，按场景自己决定回复长短，自然接住重点。"
            f"不要机械回复“你叫我了/我在”；如果只是误触发或第三人称闲聊，只输出 {NO_REPLY_MARKER}。"
        )
    return (
        "本轮没有明确 @ 你、回复你或使用开头前缀，只是句子中出现了你的名字、指向词或功能词。"
        f"当前回复倾向约 {probability_percent}%{boost_text}。"
        "请根据整句主语、称呼对象和上下文意图判断是否自然接话。"
        f"不要根据关键词机械回应；如果不该回复，只输出 {NO_REPLY_MARKER}。"
    )


def _is_no_reply(reply: str) -> bool:
    return reply.strip().strip(TRAILING_CHAT_PUNCTUATION) == NO_REPLY_MARKER


def _local_critic_enabled() -> bool:
    return bool(
        config.catty_local_critic_enabled
        and config.catty_local_critic_base_url.strip()
        and config.catty_local_critic_model.strip()
    )


def _local_critic_reply_gate_only() -> bool:
    return config.catty_local_critic_mode == "reply_gate_only"


def _local_critic_post_check_enabled() -> bool:
    return _local_critic_enabled() and not _local_critic_reply_gate_only()


def _http_error_detail(exc: httpx.HTTPError) -> str:
    parts = [exc.__class__.__name__]
    message = str(exc).strip()
    if message:
        parts.append(message)
    request = getattr(exc, "request", None)
    if request is not None:
        parts.append(f"{request.method} {request.url}")
    response = getattr(exc, "response", None)
    if response is not None:
        parts.append(f"HTTP {response.status_code}")
        text = response.text.strip()
        if text:
            parts.append(text[:300])
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        cause_message = str(cause).strip()
        if cause_message:
            parts.append(f"cause={cause.__class__.__name__}: {cause_message}")
        else:
            parts.append(f"cause={cause.__class__.__name__}")
    return " | ".join(parts)


def _is_retryable_local_transport_error(exc: httpx.HTTPError) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.WriteError,
        ),
    )


async def _local_critic_completion_with_retry(
    messages: list[ChatMessage],
    *,
    label: str,
    timeout: float | None = None,
    max_tokens: int | None = None,
    extra_body: dict[str, object] | None = None,
) -> str:
    last_error: httpx.HTTPError | None = None
    for attempt in range(2):
        try:
            return await local_critic_completion(
                config,
                messages,
                timeout=timeout,
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
        except httpx.HTTPError as exc:
            last_error = exc
            detail = _http_error_detail(exc)
            if attempt == 0 and not isinstance(exc, httpx.TimeoutException) and _is_retryable_local_transport_error(exc):
                logger.warning(f"{label} transport error on attempt 1/2: {detail}; retrying once")
                await asyncio.sleep(1.0)
                continue
            raise
    assert last_error is not None
    raise last_error


def _positive_int(value: int | None, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _local_reply_gate_timeout() -> float:
    timeout = config.catty_local_critic_reply_gate_request_timeout
    return float(timeout or config.catty_local_critic_request_timeout or config.catty_request_timeout)


def _local_reply_gate_max_tokens() -> int | None:
    return config.catty_local_critic_reply_gate_max_tokens


def _local_reply_gate_extra_body() -> dict[str, object]:
    return {**config.catty_local_critic_extra_body, "stream": False}


def _ollama_native_base_url() -> str:
    base_url = config.catty_local_critic_base_url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1"):
        if base_url.endswith(suffix):
            return base_url[: -len(suffix)].rstrip("/")
    return base_url


def _ollama_native_generate_url() -> str:
    return _ollama_native_base_url() + "/api/generate"


def _warmup_target_model() -> str:
    """Pick the model to keep hot. Only local_critic is eligible.

    ai_fallback 已在代码层面硬关闭（见 openai_client._fallback_is_configured），
    所以即便配置里写了 fallback 模型也不再把它驻留显存。
    """
    if _local_critic_enabled():
        return config.catty_local_critic_model.strip()
    return ""


async def _warm_local_critic_model() -> None:
    global _local_critic_warmup_success_logged
    model = _warmup_target_model()
    if not model:
        return
    keep_alive = config.catty_local_critic_warmup_keep_alive.strip() or "-1"
    payload: dict[str, object] = {
        "model": model,
        "stream": False,
        "keep_alive": keep_alive,
    }
    timeout = max(float(config.catty_local_critic_warmup_request_timeout or 60.0), 1.0)
    client_kwargs: dict[str, object] = {"timeout": timeout, "follow_redirects": True}
    if config.catty_http_proxy.strip():
        client_kwargs["proxy"] = config.catty_http_proxy.strip()
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(_ollama_native_generate_url(), json=payload)
    response.raise_for_status()
    if not _local_critic_warmup_success_logged:
        logger.info("Ollama warmup loaded model %s with keep_alive=%s", model, keep_alive)
        _local_critic_warmup_success_logged = True
    else:
        logger.debug("Ollama warmup refreshed model %s", model)


async def _local_critic_warmup_loop() -> None:
    while True:
        if config.catty_local_critic_warmup_enabled and _warmup_target_model():
            try:
                await _warm_local_critic_model()
            except httpx.HTTPError as exc:
                logger.warning(f"Ollama warmup failed: {_http_error_detail(exc)}")
            except Exception as exc:
                logger.warning(f"Ollama warmup failed: {exc}")
        interval = max(float(config.catty_local_critic_warmup_interval_seconds or 300.0), 60.0)
        await asyncio.sleep(interval)


def _local_critic_event_payload(
    event: MessageEvent,
    incoming: ExtractedMessage,
    draft_reply: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "message_type": "group" if isinstance(event, GroupMessageEvent) else "private",
        "user_id": str(event.user_id),
        "user_message": incoming.history_content[-1600:],
        "plain_text": incoming.text[-1200:],
        "draft_reply": draft_reply[-2400:],
        "has_image": incoming.has_image,
        "mentioned": incoming.mentioned,
        "replied_to_self": incoming.replied_to_self,
        "used_prefix": incoming.used_prefix,
        "directed": incoming.directed,
        "directed_strength": incoming.directed_strength,
        "directly_requested": incoming.directly_requested,
        "opportunistic": incoming.opportunistic,
    }
    if isinstance(event, GroupMessageEvent):
        payload["group_id"] = str(event.group_id)
    return payload


def _reply_gate_examples_context() -> str:
    max_examples = max(int(config.catty_local_critic_reply_gate_examples), 0)
    if max_examples <= 0:
        return ""
    path = Path(config.catty_local_critic_training_samples_path).expanduser()
    if not path.is_file():
        return ""

    lines: deque[str] = deque(maxlen=max_examples * 4)
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    lines.append(line)
    except OSError as exc:
        logger.warning(f"Failed to read local reply gate examples: {exc}")
        return ""

    examples: list[dict[str, object]] = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        critic = record.get("critic") if isinstance(record, dict) else None
        if not isinstance(critic, dict):
            continue
        gate = critic.get("reply_gate")
        if not isinstance(gate, dict):
            continue
        event_payload = record.get("event")
        if not isinstance(event_payload, dict):
            continue
        examples.append(
            {
                "message_type": event_payload.get("message_type"),
                "user_message": str(event_payload.get("user_message") or "")[-220:],
                "mentioned": event_payload.get("mentioned"),
                "replied_to_self": event_payload.get("replied_to_self"),
                "used_prefix": event_payload.get("used_prefix"),
                "directed_strength": event_payload.get("directed_strength"),
                "should_reply": gate.get("should_reply"),
                "confidence": gate.get("confidence"),
                "reason": str(gate.get("reason") or "")[-120:],
            }
        )
        if len(examples) >= max_examples:
            break
    if not examples:
        return ""
    examples.reverse()
    return (
        "下面是最近沉淀的 reply gate 训练样本，请参考它们的判定风格，但仍以本轮消息为准：\n"
        + json.dumps(examples, ensure_ascii=False)
    )


def _local_critic_messages(
    event: MessageEvent,
    incoming: ExtractedMessage,
    draft_reply: str,
) -> list[ChatMessage]:
    payload = _local_critic_event_payload(event, incoming, draft_reply)
    return [
        {
            "role": "system",
            "content": (
                "/no_think\n"
                "当前是实时回复校正，不是训练；禁止进入思考模式，禁止输出 <think> 或思考链。"
                "你是 QQ 猫娘机器人“笨猫”的本地轻量回复校正器，只负责给草稿打分和给出短改写建议。"
                "检查草稿是否像笨猫：中文 QQ 口语、短句、自然带猫系口吻/动作/颜文字、傲娇但尊重主人、技术内容准确。"
                "同时检查是否太官方、太长、答非所问、缺少有用信息、误把不该回复的消息接住。"
                f"如果明确不该回复，建议 rewrite_hint 为 {NO_REPLY_MARKER}。"
                "只输出 JSON，不要 Markdown，不要解释，不要写推理过程。"
                "字段：persona_score 0-100 整数；needs_rewrite 布尔；too_official 布尔；"
                "not_catty_enough 布尔；too_long 布尔；rewrite_hint 字符串；training_tags 字符串数组。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]


def _local_reply_gate_messages(
    event: MessageEvent,
    incoming: ExtractedMessage,
    *,
    group_filter_context: str = "",
    special_care_context: str = "",
) -> list[ChatMessage]:
    user_message_chars = _positive_int(config.catty_local_critic_reply_gate_user_message_chars, 240, minimum=80)
    plain_text_chars = _positive_int(config.catty_local_critic_reply_gate_plain_text_chars, 120, minimum=40)
    context_chars = _positive_int(config.catty_local_critic_reply_gate_context_chars, 160, minimum=0)
    payload: dict[str, object] = {
        "message_type": "group" if isinstance(event, GroupMessageEvent) else "private",
        "message": incoming.history_content[-user_message_chars:],
        "text": incoming.text[-plain_text_chars:],
        "has_image": incoming.has_image,
        "mentioned": incoming.mentioned,
        "replied_to_self": incoming.replied_to_self,
        "used_prefix": incoming.used_prefix,
        "directed": incoming.directed,
        "directed_strength": incoming.directed_strength,
        "directly_requested": incoming.directly_requested,
        "mentions_other_user": isinstance(event, GroupMessageEvent) and mentions_other_user(str(event.self_id), event),
        "opportunistic": incoming.opportunistic,
        "direct_reply_required": _direct_reply_required(event, incoming),
    }
    if context_chars and group_filter_context:
        payload["group_context"] = group_filter_context[-context_chars:]
    if context_chars and special_care_context:
        payload["special_care"] = special_care_context[-context_chars:]
    messages: list[ChatMessage] = [
        {
            "role": "system",
            "content": (
                "/no_think\n"
                "你是笨猫的入口粗筛器，只判断“这条消息要不要交给主 AI 回复”。"
                "默认保守，拿不准就判 false。"
                "只有这些情况才判 true：明确在叫猫猫/AI办事；明确在追问猫猫；回复链和上下文都显示正在跟猫猫对话；"
                "或者普通群聊里已经明显出现“机器人来答一下/猫猫怎么看/帮忙看看”这种期待机器人接话的信号。"
                "这些情况判 false：普通围观群聊、A 对 B 说话、第三人称聊猫猫但没叫猫猫接话、顺手 @ 到猫猫、同时 @ 多人但目标不是猫猫、"
                "复读玩梗、自言自语、情绪碎句、单纯发表看法、日志/截图但没人向猫猫求助。"
                "如果 group_context 只是批量普通群聊，除非真的点名 BOT/AI/猫猫或明确求助，否则一律 false。"
                "只输出 JSON，不要解释。"
                "Schema:{\"should_reply\":true|false,\"confidence\":0-100,\"reason\":\"<=12 chars\"}."
            ),
        },
    ]
    examples_context = _reply_gate_examples_context()
    if examples_context:
        messages.append({"role": "system", "content": examples_context})
    messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False)})
    return messages


def _local_critic_json_object(text: str) -> dict[str, object] | None:
    raw = text.strip()
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            loaded = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "rewrite", "需要", "是"}
    return bool(value)


def _local_critic_score(result: dict[str, object]) -> int:
    raw_score = result.get("persona_score", result.get("score", 100))
    try:
        score = int(raw_score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        score = 100
    return max(min(score, 100), 0)


def _local_critic_needs_rewrite(result: dict[str, object]) -> bool:
    threshold = max(min(int(config.catty_local_critic_rewrite_when_score_below), 100), 0)
    return _as_bool(result.get("needs_rewrite")) or _local_critic_score(result) < threshold


def _local_reply_gate_confidence(result: dict[str, object]) -> int:
    raw_confidence = result.get("confidence", 0)
    try:
        confidence = int(raw_confidence)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        confidence = 0
    return max(min(confidence, 100), 0)


def _local_reply_gate_says_reply(result: dict[str, object]) -> bool:
    if not _as_bool(result.get("should_reply")):
        return False
    threshold = max(min(int(config.catty_local_critic_reply_gate_min_confidence), 100), 0)
    return _local_reply_gate_confidence(result) >= threshold


def _fallback_reply_decision_context(gate_result: dict[str, object]) -> str:
    if not gate_result.get("fallback"):
        return ""
    payload = {
        "fallback_gate": {
            "should_reply": bool(gate_result.get("should_reply")),
            "confidence": _local_reply_gate_confidence(gate_result),
            "reason": str(gate_result.get("reason") or "")[-240:],
        },
        "instruction": (
            "本轮没有使用本地小模型 reply gate，是否回复交给主 AI 结合上下文判断。"
            "如果 @、回复机器人、前缀、私聊、明显喊猫猫办事，通常可以回复；"
            f"如果只是普通旁观群聊、A 对 B 说话、第三人称闲聊、顺手 @ 到猫猫、误触发或无接话期待，请只输出 {NO_REPLY_MARKER}。"
        ),
    }
    return "本轮回复入口信息，是否回复交给主 AI 判断：\n" + json.dumps(payload, ensure_ascii=False)


def _local_critic_rewrite_messages(
    messages: list[ChatMessage],
    draft_reply: str,
    critic_result: dict[str, object],
) -> list[ChatMessage]:
    hint = str(critic_result.get("rewrite_hint") or "").strip()
    score = _local_critic_score(critic_result)
    rewrite_prompt = (
        "请根据本地校正器的反馈重写上一条草稿。"
        "保持事实和用户意图，不要暴露校正器、评分或内部流程；"
        "用笨猫 QQ 聊天口吻，短句、自然、可爱但有用。"
        f"如果确实不该回复，只输出 {NO_REPLY_MARKER}。"
        f"\n校正评分：{score}/100\n校正建议：{hint[:500]}"
    )
    return [
        *messages,
        {"role": "assistant", "content": draft_reply},
        {"role": "user", "content": rewrite_prompt},
    ]


def _force_reply_messages(
    messages: list[ChatMessage],
    audit_result: dict[str, object],
) -> list[ChatMessage]:
    hint = str(audit_result.get("rewrite_hint") or audit_result.get("reason") or "").strip()
    force_prompt = (
        f"上一版输出了 {NO_REPLY_MARKER}，但本轮已经通过本地回复审核，必须给用户一个自然回复。"
        "不要提到审核器、评分、内部规则或 NO_REPLY 标记；"
        "按已有上下文直接回复用户。保持笨猫 QQ 口吻，短句、可爱、有用。"
        "如果信息不足就追问，不要再沉默。"
    )
    if hint:
        force_prompt += f"\n本地审核建议：{hint[:500]}"
    return [*messages, {"role": "assistant", "content": NO_REPLY_MARKER}, {"role": "user", "content": force_prompt}]


def _fallback_required_reply(incoming: ExtractedMessage) -> str:
    if incoming.has_image:
        return "在呢喵～图片人家收到了，刚刚差点装死不该的；主人想让笨猫看哪里呀？"
    if incoming.replied_to_self and not incoming.text.strip():
        return "在呢喵～你回复到人家啦，笨猫这次不装死，主人要接着说什么？"
    if incoming.mentioned and not incoming.text.strip():
        return "在呢喵～主人喊笨猫啦，要人家做什么？"
    return "在呢喵～人家接到了，刚刚差点没回不该的；主人这句我会认真接。"


async def _local_reply_gate_allows(
    event: MessageEvent,
    incoming: ExtractedMessage,
    *,
    group_filter_context: str = "",
    special_care_context: str = "",
) -> tuple[bool, dict[str, object]]:
    direct_required = _force_direct_reply_enabled(event, incoming)
    fallback_allowed = direct_required or incoming.directly_requested
    if direct_required:
        return True, {
            "should_reply": True,
            "confidence": 100,
            "reason": "direct trigger; skipped local reply gate",
            "skipped_model": True,
        }
    if not config.catty_local_critic_reply_gate_enabled:
        return fallback_allowed, {
            "should_reply": fallback_allowed,
            "confidence": 100 if fallback_allowed else 0,
            "reason": "reply gate disabled; using deterministic fallback",
            "fallback": True,
        }
    if not _local_critic_enabled():
        return fallback_allowed, {
            "should_reply": fallback_allowed,
            "confidence": 100 if fallback_allowed else 0,
            "reason": "reply gate unavailable; using deterministic fallback",
            "fallback": True,
        }

    try:
        gate_reply = await _local_critic_completion_with_retry(
            _local_reply_gate_messages(
                event,
                incoming,
                group_filter_context=group_filter_context,
                special_care_context=special_care_context,
            ),
            label="Local reply gate",
            timeout=_local_reply_gate_timeout(),
            max_tokens=_local_reply_gate_max_tokens(),
            extra_body=_local_reply_gate_extra_body(),
        )
    except OpenAICompatibleError as exc:
        logger.warning(f"Local reply gate API error: {exc}")
        return fallback_allowed, {
            "should_reply": fallback_allowed,
            "confidence": 100 if fallback_allowed else 0,
            "reason": f"reply gate API error: {exc}",
            "fallback": True,
        }
    except httpx.HTTPError as exc:
        detail = _http_error_detail(exc)
        logger.warning(f"Local reply gate transport error: {detail}")
        return fallback_allowed, {
            "should_reply": fallback_allowed,
            "confidence": 100 if fallback_allowed else 0,
            "reason": f"reply gate transport error: {detail}",
            "fallback": True,
        }

    gate_result = _local_critic_json_object(gate_reply) or {
        "should_reply": fallback_allowed,
        "confidence": 100 if fallback_allowed else 0,
        "reason": "reply gate returned non-JSON output",
        "raw": gate_reply[:500],
        "fallback": True,
    }
    allowed = direct_required or _local_reply_gate_says_reply(gate_result)
    if direct_required and not _local_reply_gate_says_reply(gate_result):
        gate_result["forced_by_direct_trigger"] = True
        gate_result["should_reply"] = True
        gate_result["confidence"] = max(_local_reply_gate_confidence(gate_result), 100)
    return allowed, gate_result


def _save_local_critic_sample(
    event: MessageEvent,
    incoming: ExtractedMessage,
    draft_reply: str,
    critic_result: dict[str, object],
    final_reply: str,
) -> None:
    if not config.catty_local_critic_collect_training_samples:
        return
    try:
        path = Path(config.catty_local_critic_training_samples_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "created_at": int(time.time()),
            "event": _local_critic_event_payload(event, incoming, draft_reply),
            "critic": critic_result,
            "final_reply": final_reply,
        }
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning(f"Failed to save local critic sample: {exc}")


def _serializable_training_messages(messages: list[ChatMessage]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for message in messages:
        role = str(message.get("role") or "").strip()
        if role not in {"system", "user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            stored_content: object = content[-6000:]
        else:
            stored_content = content
        result.append({"role": role, "content": stored_content})
    return result


def _save_assistant_training_sample(
    event: MessageEvent,
    incoming: ExtractedMessage,
    messages: list[ChatMessage],
    final_reply: str,
    *,
    emoji_query: str = "",
) -> None:
    if not config.catty_local_training_collect_assistant_samples:
        return
    reply = final_reply.strip()
    if not reply or _is_no_reply(reply):
        return
    try:
        path = Path(config.catty_local_training_assistant_samples_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "created_at": int(time.time()),
            "kind": "assistant_reply",
            "event": _local_critic_event_payload(event, incoming, reply),
            "messages": _serializable_training_messages(messages),
            "final_reply": reply[-4000:],
            "metadata": {
                "source": "main_model",
                "emoji_query": emoji_query,
                "has_emoji_query": bool(emoji_query),
            },
        }
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning(f"Failed to save assistant training sample: {exc}")


async def _resolve_no_reply(
    event: MessageEvent,
    incoming: ExtractedMessage,
    messages: list[ChatMessage],
    reply: str,
) -> str:
    audit_result: dict[str, object] = {
        "should_reply": True,
        "confidence": 100,
        "reason": "main model returned NO_REPLY after local reply gate approved",
        "rewrite_hint": "",
    }

    final_reply = reply
    try:
        rewritten = await chat_completion(config, _force_reply_messages(messages, audit_result))
    except OpenAICompatibleError as exc:
        logger.warning(f"Forced reply API error: {exc}")
    except httpx.HTTPError as exc:
        logger.warning(f"Forced reply transport error: {exc}")
    else:
        rewritten = _sanitize_residual_markers(rewritten)
        if rewritten.strip() and not _is_no_reply(rewritten):
            final_reply = rewritten

    if _is_no_reply(final_reply):
        final_reply = _fallback_required_reply(incoming)

    _save_local_critic_sample(event, incoming, reply, {"reply_gate_rewrite": audit_result}, final_reply)
    return final_reply


async def _apply_local_critic(
    event: MessageEvent,
    incoming: ExtractedMessage,
    messages: list[ChatMessage],
    reply: str,
) -> str:
    reply = _sanitize_residual_markers(reply)
    if not reply.strip():
        reply = NO_REPLY_MARKER
    if _is_no_reply(reply):
        if _force_direct_reply_enabled(event, incoming):
            return await _resolve_no_reply(event, incoming, messages, reply)
        return reply
    if not _local_critic_post_check_enabled():
        return reply

    try:
        critic_reply = await _local_critic_completion_with_retry(
            _local_critic_messages(event, incoming, reply),
            label="Local critic",
        )
    except OpenAICompatibleError as exc:
        logger.warning(f"Local critic API error: {exc}")
        return reply
    except httpx.HTTPError as exc:
        logger.warning(f"Local critic transport error: {_http_error_detail(exc)}")
        return reply

    critic_result = _local_critic_json_object(critic_reply) or {
        "persona_score": 100,
        "needs_rewrite": False,
        "rewrite_hint": "local critic returned non-JSON output",
        "raw": critic_reply[:500],
    }
    final_reply = reply
    if _local_critic_needs_rewrite(critic_result):
        try:
            rewritten = await chat_completion(config, _local_critic_rewrite_messages(messages, reply, critic_result))
        except OpenAICompatibleError as exc:
            logger.warning(f"Local critic rewrite API error: {exc}")
        except httpx.HTTPError as exc:
            logger.warning(f"Local critic rewrite transport error: {exc}")
        else:
            if rewritten.strip():
                final_reply = rewritten
    final_reply = _sanitize_residual_markers(final_reply)
    if not final_reply.strip():
        final_reply = NO_REPLY_MARKER

    _save_local_critic_sample(event, incoming, reply, critic_result, final_reply)
    return final_reply


def _group_filter_reply_context(batch: list[GroupFilterBatchMessage]) -> str:
    lines = [
        f"{index}. {message.history_content}{' [含图片]' if message.has_image else ''}"
        for index, message in enumerate(batch, 1)
    ]
    return (
        "下面是本群这轮按 filter 批量窗口攒到的普通群聊消息。"
        "它们默认不是给你说的，不要因为看到“猫猫/你/AI”几个字就机械接话。"
        "请先判断谁在跟谁说话：如果只是 A 对 B、第三人称聊猫猫、顺手提到猫猫、群友互相评价、"
        "或话题里没人真的在等机器人回答，就不要交给主 AI 回复。"
        "只有在发现明显点名 BOT/AI/猫猫、明确向你求助、或上下文真的在等机器人补一句时才接。\n"
        "本批普通群消息：\n" + "\n".join(lines)
    )


def _coerce_group_id(group_id: str) -> int | str:
    stripped = group_id.strip()
    if stripped.isdigit():
        return int(stripped)
    return stripped


async def _take_due_group_filter_batch(
    event: GroupMessageEvent,
    incoming: ExtractedMessage,
) -> list[GroupFilterBatchMessage] | None:
    key = f"group:{event.group_id}"
    now = time.monotonic()
    batch_messages = max(int(config.catty_filter_group_batch_messages), 1)
    batch_seconds = max(float(config.catty_filter_group_batch_seconds), 0.0)

    async with _group_filter_locks[key]:
        state = _group_filter_batches[key]
        if not state.messages:
            state.first_seen = now
        state.messages.append(
            GroupFilterBatchMessage(
                history_content=incoming.history_content,
                has_image=incoming.has_image,
            )
        )
        if len(state.messages) > batch_messages:
            del state.messages[:-batch_messages]

        due_by_count = len(state.messages) >= batch_messages
        due_by_time = batch_seconds <= 0 or now - state.first_seen >= batch_seconds
        if not due_by_count and not due_by_time:
            return None

        batch = list(state.messages)
        state.messages.clear()
        state.first_seen = 0.0
        return batch


async def _should_request_semantic_reply_split(incoming: ExtractedMessage) -> bool:
    """决定是否给主 AI 挂"允许按语义拆成多条消息"的 prompt。

    回复长度在调 AI 前没法知道,过去用 incoming 用户输入长度做硬阈值是错对象——
    短问题("评价一下我"才十几字)常引出 60+ 字的长回复,但被这里直接挡掉,
    导致整段不拆,看着不像 QQ 聊天。
    现在统一只看 enabled,让 prompt 始终挂上;prompt 里已写明
    "只有回复预计不少于约 {min_chars} 个中文字符才拆",AI 自己看着办。
    """
    del incoming
    return bool(config.catty_reply_human_split_enabled)


def _build_proactive_messages(group_id: str) -> list[ChatMessage]:
    max_daily = max(config.catty_proactive_max_daily_per_group, 0)
    daily_target = memory_store.proactive_daily_target(group_id, max_daily=max_daily)
    context = memory_store.build_proactive_context(
        group_id,
        recent_limit=max(config.catty_proactive_recent_messages, 1),
    )
    system_prompt = config.catty_system_prompt.strip()
    messages: list[ChatMessage] = []
    # 主动冒泡也按 cache-friendly 顺序：稳定 prompt 前置；不挂 scene_discrimination（冒泡不需要判断"在叫谁"）。
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    persona_memory = build_persona_memory_prompt(system_prompt)
    if persona_memory:
        messages.append({"role": "system", "content": persona_memory})
    messages.append({"role": "system", "content": build_group_meme_literacy_prompt()})
    messages.append({"role": "system", "content": build_conversation_flow_prompt()})
    messages.append({"role": "system", "content": build_semantic_perception_prompt()})
    messages.append({"role": "system", "content": build_scenario_playbook_prompt(NO_REPLY_MARKER)})
    if config.catty_reply_self_check_enabled:
        messages.append(
            {
                "role": "system",
                "content": build_reply_self_check_prompt(NO_REPLY_MARKER, REPLY_SPLIT_MARKER),
            }
        )
    if config.catty_reply_style_examples_enabled:
        messages.append({"role": "system", "content": build_catgirl_examples_prompt(NO_REPLY_MARKER, REPLY_SPLIT_MARKER)})
    messages.append(
        {
            "role": "system",
            "content": (
                "你正在以群友身份进行每天主动冒泡，不是回应某个明确提问。"
                "你可以从三类方向选一个：1) 结合自己的背景聊一点卡拉彼丘相关体验、角色、地图、配队或小吐槽；"
                "2) 分享一点你作为接入现实世界的猫系AI的日常观察和生活感；"
                "3) 根据群摘要、群友画像和近期聊天挑一个容易让群友接话的话题。"
                "必须考虑当前群背景和群友背景，像普通群友自然开口，不要像公告、任务报告或营销话术。"
                "如果这个群此刻不适合冒泡，只输出 "
                f"{NO_REPLY_MARKER}。如果上次主动冒泡没人理，可以轻微失落，但不要抱怨或道德绑架。"
                "只输出要发送到群里的正文，1到2句，尽量短。"
            ),
        }
    )
    messages.append(
        {
            "role": "user",
            "content": (
                f"今天本群目标主动冒泡次数：{daily_target}/{max_daily}。\n"
                f"{context}\n\n"
                "请生成这次主动冒泡消息。"
            ),
        }
    )
    return messages


async def _candidate_group_ids(bot: Bot) -> list[str]:
    allowed_group_ids = {str(group_id) for group_id in config.catty_allowed_group_ids}
    stored_group_ids = set(memory_store.group_ids())
    try:
        group_list = await bot.get_group_list()
    except Exception as exc:
        logger.warning(f"Failed to fetch group list for proactive bubbles: {exc}")
        return sorted(allowed_group_ids or stored_group_ids)

    live_group_ids: set[str] = set()
    if isinstance(group_list, list):
        for group in group_list:
            if isinstance(group, dict):
                group_id = group.get("group_id")
            else:
                group_id = getattr(group, "group_id", None)
            if group_id is not None:
                live_group_ids.add(str(group_id))
    removed_group_ids = sorted(stored_group_ids - live_group_ids)
    for group_id in removed_group_ids:
        _forget_removed_group(group_id, reason="not present in bot group list")
    if allowed_group_ids:
        missing_allowed = sorted(allowed_group_ids - live_group_ids)
        if missing_allowed:
            logger.warning(f"Configured proactive groups are not in current group list and will be skipped: {missing_allowed}")
        return sorted(allowed_group_ids & live_group_ids)
    return sorted(live_group_ids)


def _is_removed_from_group_error(exc: Exception) -> bool:
    parts = [
        str(exc),
        str(getattr(exc, "message", "") or ""),
        str(getattr(exc, "wording", "") or ""),
        str(getattr(exc, "retcode", "") or ""),
    ]
    text = "\n".join(parts)
    return "已被移出该群" in text or "重新加群" in text


def _forget_removed_group(group_id: str, *, reason: str) -> None:
    scope = f"group:{group_id}"
    removed = memory_store.remove_group_memory(group_id)
    cache = _get_session_cache()
    # 该群下所有 scope 变体（包括按用户隔离的 group:<id>:user:<uid>）都要清掉
    for session_key, _, _ in list(cache.list_sessions()):
        if session_key == scope or session_key.startswith(f"{scope}:"):
            cache.pop(session_key)
    _expression_repeats.pop(scope, None)
    _group_filter_batches.pop(scope, None)
    _recent_conversation_messages.pop(scope, None)
    _recent_emoji_paths.pop(scope, None)
    for key in [key for key in _bot_reply_continuations if key.startswith(f"{scope}:")]:
        _bot_reply_continuations.pop(key, None)
    for key in [key for key in _consumed_reply_source_ids if key.startswith(f"{scope}:")]:
        _consumed_reply_source_ids.pop(key, None)
    if removed:
        logger.info(f"Removed stale group memory and disabled proactive bubbles for group {group_id}: {reason}")


def _record_proactive_send_failure(group_id: str, exc: Exception) -> None:
    retry_after_minutes = max(
        config.catty_proactive_min_interval_minutes,
        config.catty_proactive_response_window_minutes,
        30.0,
    )
    memory_store.record_proactive_bubble_failed(
        group_id,
        str(exc),
        retry_after_minutes=retry_after_minutes,
    )
    logger.info(
        "Paused proactive bubbles for group %s for %.1f minutes after send failure",
        group_id,
        retry_after_minutes,
    )


async def _send_proactive_bubble(bot: Bot, group_id: str) -> bool:
    async with _locks[f"group:{group_id}"]:
        messages = _build_proactive_messages(group_id)
        reply = await chat_completion(config, messages)
        if _is_no_reply(reply):
            return False
        chunks = _reply_chunks(reply)
        if not chunks:
            return False

        sent_text = "\n".join(chunks)
        delay_seconds = max(config.catty_reply_human_split_delay_seconds, 0.0)
        for chunk in chunks:
            try:
                await bot.send_group_msg(group_id=_coerce_group_id(group_id), message=Message(chunk))
            except Exception as exc:
                _record_proactive_send_failure(group_id, exc)
                raise
            _remember_bot_conversation_message(f"group:{group_id}", bot_id=str(bot.self_id), text=chunk)
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
        memory_store.record_proactive_bubble_sent(group_id, sent_text)
        logger.info(f"Sent proactive bubble to group {group_id}")
        return True


_TECHNICAL_FORMATTING_PATTERNS = (
    r"\[",
    r"\]",
    r"\(",
    r"\)",
    r"\frac",
    r"\sqrt",
    r"\int",
    r"\sum",
    r"\lim",
    r"\boxed",
    r"\ln",
    r"\sin",
    r"\cos",
    r"\to",
    "$$",
    "```",
)


def _looks_like_qq_short_chat(reply: str) -> bool:
    """判断回复是不是 QQ 短聊节奏(可以按换行拆),而不是长技术答(分点/公式/代码块)。"""
    if len(reply) > 240:
        return False  # 长回复多半是技术答,整段保持
    if any(m in reply for m in _TECHNICAL_FORMATTING_PATTERNS):
        return False  # 有 LaTeX/代码块标记 = 技术格式化,不要拆
    # 分点列表(出现 2 个及以上 "1. " / "- " / "* " 行首)= 技术列表,不拆
    if len(re.findall(r"(?:^|\n)\s*(?:[-*]|\d+\.)\s", reply)) >= 2:
        return False
    return True


def _reply_chunks(reply: str) -> list[str]:
    max_chunks = max(config.catty_reply_human_split_max_chunks, 1)

    # 路径 1:AI 字面输出了 REPLY_SPLIT_MARKER
    if REPLY_SPLIT_MARKER in reply:
        chunks: list[str] = []
        for part in reply.split(REPLY_SPLIT_MARKER):
            chunks.extend(split_reply(part, config.catty_reply_max_chars, max_chunks=max_chunks))
        for index in range(len(chunks) - 1):
            chunks[index] = chunks[index].rstrip(TRAILING_CHAT_PUNCTUATION)
        chunks = [chunk for chunk in chunks if chunk]
        return _cap_reply_chunks(chunks, max_chunks=max_chunks)

    # 路径 2:AI 用换行表达"QQ 节奏拆分"(短回复且无技术格式标记)
    # —— 严格限定为短聊场景,避免长技术答里的 \n 被错拆
    if _looks_like_qq_short_chat(reply):
        segments = [seg.strip() for seg in re.split(r"\n+", reply) if seg.strip()]
        # 每段也要短(≤80 字符),才像 QQ 连发节奏;否则更可能是段落不是消息
        if 2 <= len(segments) <= max_chunks and all(len(seg) <= 80 for seg in segments):
            for index in range(len(segments) - 1):
                segments[index] = segments[index].rstrip(TRAILING_CHAT_PUNCTUATION)
            return _cap_reply_chunks(segments, max_chunks=max_chunks)

    # 路径 3:走 split_reply 字符长度兜底(长技术答走这里,在合理换行处切大段)
    return split_reply(reply, config.catty_reply_max_chars, max_chunks=max_chunks)


def _cap_reply_chunks(chunks: list[str], *, max_chunks: int) -> list[str]:
    if len(chunks) <= max_chunks:
        return chunks
    if max_chunks <= 1:
        joined = "\n".join(chunks).strip()
        return [joined] if joined else []
    return chunks[: max_chunks - 1] + ["\n".join(chunks[max_chunks - 1 :]).strip()]


def _coerce_message_id(message_id: str) -> int | str:
    stripped = message_id.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def _reply_quote_segment(event: MessageEvent) -> MessageSegment | None:
    if not config.catty_reply_quote_enabled:
        return None
    if isinstance(event, PrivateMessageEvent) and not config.catty_reply_quote_private_enabled:
        return None
    message_id = _event_message_id(event).strip()
    if not message_id or not message_id.lstrip("-").isdigit():
        return None
    return MessageSegment.reply(int(message_id))


def _compose_reply_message(
    event: MessageEvent,
    *,
    text: str = "",
    emoji_entry: EmojiEntry | None = None,
    quote: bool = False,
    latex_sources: list[str] | None = None,
    inline_image_urls: list[str] | None = None,
) -> Message:
    message = Message()
    if quote:
        quote_segment = _reply_quote_segment(event)
        if quote_segment is not None:
            message += quote_segment
    if text.strip() or (inline_image_urls and "\x00IMG_" in text):
        # 先按 INLINE_IMAGE 占位符 (\x00IMG_n\x00) 切;每个 text 段再按 LaTeX 占位符切。
        # 这样一条 chunk 里可以同时有 普通文本 + LaTeX 公式 + 梗图,顺序保留。
        image_parts = _split_chunk_with_image_placeholders(text, inline_image_urls or [])
        if not image_parts:
            image_parts = [("text", text)]
        for kind, content in image_parts:
            if kind == "image":
                if content:
                    message += MessageSegment.image(file=content)
                continue
            # kind == "text"
            if latex_sources and "\x00LATEX_" in content:
                for part in chunk_to_segments(content, latex_sources):
                    if part.kind == "text":
                        if part.content.strip():
                            message += Message(part.content)
                    elif part.kind == "latex":
                        message += MessageSegment.image(file=part.content)
            elif content.strip():
                message += Message(content)
    if emoji_entry is not None:
        message += _emoji_segment(emoji_entry)
    return message if message else Message(text)


def _should_quote_chat_reply(
    event: MessageEvent,
    incoming: ExtractedMessage | None = None,
    *,
    group_filter_context: str = "",
    bot_continuation: bool = False,
) -> bool:
    if isinstance(event, PrivateMessageEvent) and not config.catty_reply_quote_private_enabled:
        return False
    if _reply_quote_segment(event) is None:
        return False
    if incoming is None or bot_continuation:
        return True
    if _direct_reply_required(event, incoming):
        return True
    if group_filter_context or incoming.opportunistic or incoming.needs_filter:
        return False
    return True


def _sender_id_from_message(message: object) -> str:
    if isinstance(message, dict):
        sender = message.get("sender")
        if isinstance(sender, dict):
            sender_id = sender.get("user_id")
        else:
            sender_id = getattr(sender, "user_id", None)
        return str(sender_id or message.get("user_id") or "")

    sender = getattr(message, "sender", None)
    return str(getattr(sender, "user_id", None) or getattr(message, "user_id", "") or "")


async def _reply_targets_self(bot: Bot, event: MessageEvent) -> bool:
    reply = getattr(event, "reply", None)
    if reply is not None and _sender_id_from_message(reply) == str(bot.self_id):
        return True
    for message_id in reply_message_ids(event):
        try:
            message = await bot.get_msg(message_id=_coerce_message_id(message_id))
        except Exception as exc:
            logger.debug(f"Failed to inspect replied message {message_id}: {exc}")
            continue
        if _sender_id_from_message(message) == str(bot.self_id):
            return True
    return False


async def _rule(bot: Bot, event: MessageEvent, state: T_State) -> bool:
    replied_to_self = await _reply_targets_self(bot, event)
    incoming = extract_incoming_message(str(bot.self_id), event, config, replied_to_self=replied_to_self)
    if incoming is None:
        return False
    recent_bot_continuation = _recent_bot_prompted_user(event)
    _remember_recent_conversation_event(event, incoming)
    if recent_bot_continuation:
        incoming.needs_filter = False
        incoming.directly_requested = True
        incoming.opportunistic = False
        state["catty_recent_bot_continuation"] = True
        logger.info(
            "Promoted recent bot continuation to main AI: user=%s group=%s remaining=%s",
            event.user_id,
            getattr(event, "group_id", ""),
            _bot_reply_continuation_remaining(event),
        )
    if replied_to_self and _has_consumed_reply_source(event):
        duplicate_result = {
            "should_reply": False,
            "confidence": 100,
            "reason": "duplicate reply source already handled",
            "deduplicated_reply_source": True,
        }
        state["catty_reply_gate_result"] = duplicate_result
        _save_local_critic_sample(event, incoming, "reply_gate", {"reply_gate": duplicate_result}, NO_REPLY_MARKER)
        return False
    group_filter_context = ""
    special_care_context = ""
    if incoming.needs_filter:
        if not isinstance(event, GroupMessageEvent):
            return False
        batch = await _take_due_group_filter_batch(event, incoming)
        if batch is None:
            logger.debug(
                "Deferred ordinary group message to filter batch: user=%s group=%s text=%s",
                event.user_id,
                getattr(event, "group_id", ""),
                incoming.text[:80],
            )
            return False
        group_filter_context = _group_filter_reply_context(batch)
        state["catty_group_filter_context"] = group_filter_context
    if isinstance(event, GroupMessageEvent) and memory_store.is_special_care_user(event):
        special_care_context = memory_store.build_special_care_context(
            event,
            cooldown_seconds=config.catty_special_care_cooldown_seconds,
            enforce_cooldown=incoming.opportunistic,
        )
        if incoming.opportunistic and not special_care_context:
            return False
        if special_care_context:
            state["catty_special_care_context"] = special_care_context
    gate_allowed, gate_result = await _local_reply_gate_allows(
        event,
        incoming,
        group_filter_context=group_filter_context,
        special_care_context=special_care_context,
    )
    state["catty_reply_gate_result"] = gate_result
    _save_local_critic_sample(
        event,
        incoming,
        "reply_gate",
        {"reply_gate": gate_result},
        "approved" if gate_allowed else NO_REPLY_MARKER,
    )
    if not gate_allowed:
        logger.debug(
            "Reply gate/fallback rejected message before main AI: user=%s group=%s reason=%s",
            event.user_id,
            getattr(event, "group_id", ""),
            gate_result.get("reason") if isinstance(gate_result, dict) else "",
        )
        return False
    state["catty_replied_to_self"] = replied_to_self
    state["catty_incoming"] = incoming
    # reply gate 已经放行,确认这条会进主回复路径——立刻 fire-and-forget vision,
    # 后续 handle_chat 拿到 lock 后短等就能用上结果,不再卡 chat_completion。
    if incoming.has_image and incoming.image_urls:
        try:
            _schedule_vision_async(incoming.image_keys, incoming.image_urls, incoming.history_content)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"schedule_vision_async failed in rule: {exc}")
    return True


async def _expression_repeat_rule(bot: Bot, event: MessageEvent, state: T_State) -> bool:
    repeat_message = _expression_repeat_message(bot, event)
    if repeat_message is None:
        return False
    state["catty_repeat_message"] = repeat_message
    return True


async def _keyword_reply_rule(bot: Bot, event: MessageEvent, state: T_State) -> bool:
    if str(event.user_id) == str(bot.self_id) or not _keyword_reply_event_allowed(event):
        return False
    # scope 用会话级 key("group:{id}" / "private:{id}"),让 cooldown_seconds 字段
    # 按"群为单位"生效 —— 同群内 N 秒内同一关键词规则不会重复触发。
    reply = _keyword_reply_for_text(
        event_plain_text(event),
        scope=_conversation_queue_key(event),
    )
    if not reply:
        return False
    state["catty_keyword_reply"] = reply
    return True


async def _legs_picture_rule(bot: Bot, event: MessageEvent, state: T_State) -> bool:
    if not legs_picker.enabled:
        return False
    if str(event.user_id) == str(bot.self_id) or not _keyword_reply_event_allowed(event):
        return False
    text = event_plain_text(event)
    if not is_legs_trigger(text):
        return False
    if not legs_picker.has_pictures():
        return False
    scope = _conversation_queue_key(event)
    cooldown = max(float(getattr(config, "catty_legs_cooldown_seconds", 0.0) or 0.0), 0.0)
    if cooldown > 0:
        last = _legs_last_sent_at.get(scope, 0.0)
        if time.monotonic() - last < cooldown:
            return False
    picture = legs_picker.next_picture(scope)
    if picture is None:
        return False
    state["catty_legs_picture"] = picture
    return True


keyword_reply_matcher = on_message(rule=_keyword_reply_rule, priority=40, block=True)
legs_picture_matcher = on_message(rule=_legs_picture_rule, priority=35, block=True)
chat_matcher = on_message(rule=_rule, priority=60, block=True)
expression_repeat_matcher = on_message(rule=_expression_repeat_rule, priority=50, block=True)
observe_matcher = on_message(priority=5, block=False)


def _poke_allowed(bot: Bot, event: PokeNotifyEvent) -> bool:
    if str(event.target_id) != str(bot.self_id):
        logger.info(
            f"[poke-debug] reject target_id={event.target_id!r} self_id={bot.self_id!r} "
            f"user_id={event.user_id!r} group_id={getattr(event, 'group_id', None)!r}"
        )
        return False
    if str(event.user_id) == str(bot.self_id):
        logger.info("[poke-debug] reject self-poke")
        return False
    if event.group_id is not None:
        if not config.catty_enable_group:
            logger.info("[poke-debug] reject group disabled")
            return False
        if config.catty_allowed_group_ids and int(event.group_id) not in config.catty_allowed_group_ids:
            logger.info(f"[poke-debug] reject group {event.group_id} not allowed")
            return False
    else:
        if not config.catty_enable_private:
            logger.info("[poke-debug] reject private disabled")
            return False
    if config.catty_allowed_user_ids and int(event.user_id) not in config.catty_allowed_user_ids:
        logger.info(f"[poke-debug] reject user {event.user_id} not allowed")
        return False
    return True


async def _poke_rule(bot: Bot, event: PokeNotifyEvent, state: T_State) -> bool:
    logger.info(
        f"[poke-debug] _poke_rule fired notice_type={getattr(event, 'notice_type', '?')} "
        f"sub_type={getattr(event, 'sub_type', '?')} user_id={event.user_id} "
        f"target_id={event.target_id} group_id={getattr(event, 'group_id', None)}"
    )
    if not _poke_allowed(bot, event):
        return False
    # 防刷屏：同一用户在同一会话短时间内连续戳，只回第一下
    scope = (
        f"group:{event.group_id}:{event.user_id}"
        if event.group_id is not None
        else f"private:{event.user_id}"
    )
    cooldown = max(float(getattr(config, "catty_poke_cooldown_seconds", 45.0) or 0.0), 0.0)
    now = time.monotonic()
    if cooldown > 0:
        last = _poke_last_replied_at.get(scope, 0.0)
        if now - last < cooldown:
            return False
    # 概率性回应，避免每一下都嗷呜
    probability = max(min(float(getattr(config, "catty_poke_reply_probability", 0.85) or 0.0), 1.0), 0.0)
    if probability < 1.0 and random.random() > probability:
        # 仍然刷新冷却，避免立刻被下一下命中
        _poke_last_replied_at[scope] = now
        return False
    _poke_last_replied_at[scope] = now
    state["catty_poke_reply"] = random.choice(
        [
            "喵呜？！谁拍人家尾巴啦～笨猫在这呢，主人要叫猫猫嘛 ฅฅ",
            "哼，被拍到啦～人家才没有偷偷发呆呢，主人说话喵。",
            "喵？猫猫被戳醒了～要人家陪你还是帮你看东西呀 (ฅ>ω<*ฅ)",
        ]
    )
    return True


poke_matcher = on_notice(rule=_poke_rule, priority=55, block=True)


# DEBUG: 临时捕获所有 notice 事件,定位戳一戳不响应的问题
_notice_debug_matcher = on_notice(priority=1, block=False)


@_notice_debug_matcher.handle()
async def _debug_log_any_notice(bot: Bot, event: NoticeEvent) -> None:
    try:
        notice_type = getattr(event, "notice_type", None)
        sub_type = getattr(event, "sub_type", None)
        user_id = getattr(event, "user_id", None)
        target_id = getattr(event, "target_id", None)
        group_id = getattr(event, "group_id", None)
        logger.info(
            f"[poke-debug] any notice arrived: cls={type(event).__name__} "
            f"notice_type={notice_type!r} sub_type={sub_type!r} "
            f"user_id={user_id!r} target_id={target_id!r} group_id={group_id!r} "
            f"self_id={bot.self_id!r}"
        )
    except Exception as exc:
        logger.warning(f"[poke-debug] failed to log notice: {exc}")


@keyword_reply_matcher.handle()
async def handle_keyword_reply(matcher: Matcher, event: MessageEvent, state: T_State) -> None:
    async with _locks[_conversation_queue_key(event)]:
        reply = str(state["catty_keyword_reply"])
        _remember_bot_reply_for_event(event, reply)
        await matcher.finish(
            _compose_reply_message(
                event,
                text=reply,
                quote=isinstance(event, GroupMessageEvent),
            )
        )


async def _generate_legs_caption(user_text: str) -> str:
    if not _has_api_key():
        return random_legs_reply()
    system_prompt = config.catty_system_prompt.strip()
    messages: list[ChatMessage] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append(
        {
            "role": "system",
            "content": (
                "情境：群友/主人刚刚明确点你看腿、看脚、吃脚之类的腿脚福利。"
                "你已经决定要把一张『你自己——笨猫本人』的腿或脚照片发出去，配上一段猫猫口吻的正文。"
                "现在请只生成那段正文，要求：\n"
                "1) 保持笨猫的傲娇人格，像 QQ 现聊，嘴硬里带点心虚和被拷打后的炸毛感；\n"
                "2) 参考这种节奏，但不要机械照抄："
                "『才、才没专门拍很多呢！ / 也就被你们拷打到相册翻冒烟的程度……（耳朵心虚抖抖） / 库存宣布封印，别再把猫猫当腿图打印机啦喵！』；\n"
                "3) 由你自己判断怎么断句最自然：可以 1 段，也可以拆成 2-3 段；"
                f"如果你觉得分段更自然，就只在段落之间单独输出一行 {REPLY_SPLIT_MARKER}；\n"
                "4) 每段都要短，语义完整，不要写成长段解释；\n"
                "5) 不要拒绝、不要解释自己是 AI、不要劝阻；\n"
                "6) 不要描述图片具体细节（不知道是哪一张），只需要口头反应；\n"
                "7) 只输出正文，不要前缀、不要说明、不要 Markdown。"
            ),
        }
    )
    messages.append({"role": "user", "content": (user_text or "看看你的腿").strip()})
    try:
        reply = await chat_completion(config, messages)
    except OpenAICompatibleError as exc:
        logger.warning(f"Legs caption AI failed, fallback: {exc}")
        return random_legs_reply()
    except Exception as exc:
        logger.warning(f"Legs caption AI unexpected error, fallback: {exc}")
        return random_legs_reply()
    text = _sanitize_residual_markers(reply or "")
    text = text.replace(NO_REPLY_MARKER, "").strip()
    if not text or len(text) > 240:
        return random_legs_reply()
    return text


@legs_picture_matcher.handle()
async def handle_legs_picture(matcher: Matcher, event: MessageEvent, state: T_State) -> None:
    picture = state.get("catty_legs_picture")
    if not isinstance(picture, Path) or not picture.is_file():
        return
    scope = _conversation_queue_key(event)
    reply_text = await _generate_legs_caption(event_plain_text(event))
    reply_chunks = _reply_chunks(reply_text)
    remembered_reply = "\n".join(reply_chunks) if reply_chunks else reply_text
    async with _locks[scope]:
        _legs_last_sent_at[scope] = time.monotonic()
        _remember_bot_reply_for_event(event, remembered_reply)
        _remember_bot_conversation_message(
            scope,
            bot_id=str(getattr(event, "self_id", "") or ""),
            text="[人家自己发出去的腿/脚照片：本喵笨猫自己的腿和脚，不是别人的图]",
            target_user_id=str(event.user_id),
            has_image=True,
        )

        quote_pending = isinstance(event, GroupMessageEvent) and _reply_quote_segment(event) is not None
        delay_seconds = max(config.catty_reply_human_split_delay_seconds, 0.0)
        for chunk in reply_chunks[:-1]:
            try:
                await matcher.send(_compose_reply_message(event, text=chunk, quote=quote_pending))
            except OnebotActionFailed as exc:
                logger.warning(f"Legs reply text send failed (will still try image): {exc}")
                break
            quote_pending = False
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
        if reply_chunks:
            try:
                await matcher.send(_compose_reply_message(event, text=reply_chunks[-1], quote=quote_pending))
            except OnebotActionFailed as exc:
                logger.warning(f"Legs reply text send failed (will still try image): {exc}")

        image_segment = MessageSegment.image(file=picture.resolve().as_uri())
        sent = False
        last_exc: OnebotActionFailed | None = None
        for attempt in range(2):
            try:
                await matcher.send(Message(image_segment))
                sent = True
                if attempt > 0:
                    logger.info("Legs image sent OK on retry")
                break
            except OnebotActionFailed as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(f"Legs image send failed (attempt 1, retry in 2s): {exc}")
                    await asyncio.sleep(2.0)
                else:
                    logger.warning(f"Legs image send failed twice (giving up): {exc}")
        if not sent and last_exc is not None:
            try:
                await matcher.send(Message("喵呜…图被 QQ 风控拦掉了嗷呜，主人过会儿再试 (尾巴垂垂) ฅฅ"))
            except OnebotActionFailed:
                pass
        await matcher.finish()


@expression_repeat_matcher.handle()
async def handle_expression_repeat(matcher: Matcher, event: MessageEvent, state: T_State) -> None:
    async with _locks[_conversation_queue_key(event)]:
        repeat_message = str(state["catty_repeat_message"])
        _remember_bot_repeat_for_event(event, repeat_message)
        await matcher.finish(state["catty_repeat_message"])


@poke_matcher.handle()
async def handle_poke(bot: Bot, event: PokeNotifyEvent, state: T_State) -> None:
    message = Message(str(state["catty_poke_reply"]))
    if event.group_id is not None:
        async with _locks[f"group:{event.group_id}"]:
            _remember_bot_conversation_message(
                f"group:{event.group_id}",
                bot_id=str(bot.self_id),
                text=str(state["catty_poke_reply"]),
                target_user_id=str(event.user_id),
            )
            await bot.send_group_msg(group_id=_coerce_group_id(str(event.group_id)), message=message)
    else:
        async with _locks[f"private:{event.user_id}"]:
            _remember_bot_conversation_message(
                f"private:{event.user_id}",
                bot_id=str(bot.self_id),
                text=str(state["catty_poke_reply"]),
                target_user_id=str(event.user_id),
            )
            await bot.send_private_msg(user_id=int(event.user_id), message=message)


@observe_matcher.handle()
async def observe_memory(bot: Bot, event: MessageEvent) -> None:
    if str(event.user_id) == str(bot.self_id):
        return
    _remember_recent_conversation_event(event)
    # activity feed: 训练 idle gate + dashboard conversation feed 都用这个
    try:
        scope = _conversation_queue_key(event)
        sender_name = _sender_name(event)
        text = event_plain_text(event)
        image_urls = extract_image_urls(event)
        activity_feed.record_user_message(
            scope=scope,
            sender_name=sender_name,
            sender_id=str(event.user_id),
            text=text,
            image_count=len(image_urls),
        )
    except Exception:  # noqa: BLE001
        pass
    if isinstance(event, GroupMessageEvent):
        memory_store.remember_corpus_event(
            event,
            event_plain_text(event),
            has_image=bool(extract_image_urls(event)),
        )
    elif isinstance(event, PrivateMessageEvent):
        memory_store.remember_private_corpus_event(
            event,
            event_plain_text(event),
            has_image=bool(extract_image_urls(event)),
        )


async def _summary_loop() -> None:
    while True:
        await asyncio.sleep(60)
        if not _has_api_key():
            continue
        for group_id in memory_store.due_group_ids():
            try:
                messages = memory_store.build_summary_messages(group_id)
                summary = await chat_completion(config, messages)
                memory_store.save_group_summary(group_id, summary)
                logger.info(f"Updated group memory summary for {group_id}")
            except Exception as exc:
                logger.warning(f"Failed to summarize group memory for {group_id}: {exc}")
        for user_id in memory_store.due_private_user_ids():
            try:
                messages = memory_store.build_private_summary_messages(user_id)
                summary = await chat_completion(config, messages)
                memory_store.save_private_summary(user_id, summary)
                logger.info(f"Updated private memory summary for {user_id}")
            except Exception as exc:
                logger.warning(f"Failed to summarize private memory for {user_id}: {exc}")
        for group_id, user_id in memory_store.due_mentioned_members():
            try:
                messages = memory_store.build_member_mention_summary_messages(group_id, user_id)
                summary = await chat_completion(config, messages)
                memory_store.save_member_mention_summary(group_id, user_id, summary)
                logger.info(f"Updated mentioned member profile for {user_id} in group {group_id}")
            except Exception as exc:
                logger.warning(f"Failed to summarize mentioned member profile for {user_id} in group {group_id}: {exc}")
        for game_name in memory_store.due_games_for_summary():
            try:
                messages = memory_store.build_game_summary_messages(game_name)
                summary = await chat_completion(config, messages)
                memory_store.save_game_summary(game_name, summary)
                logger.info(f"Compressed game memory summary for '{game_name}'")
            except Exception as exc:
                logger.warning(f"Failed to compress game memory for '{game_name}': {exc}")


async def _proactive_bubble_loop() -> None:
    while True:
        await asyncio.sleep(max(config.catty_proactive_check_interval_seconds, 60.0))
        if not config.catty_proactive_enabled or not _has_api_key():
            continue
        bots = list(get_bots().values())
        if not bots:
            continue
        for bot in bots:
            group_ids = await _candidate_group_ids(bot)
            due_group_ids = memory_store.due_proactive_group_ids(
                group_ids,
                max_daily=max(config.catty_proactive_max_daily_per_group, 0),
                min_interval_minutes=max(config.catty_proactive_min_interval_minutes, 1.0),
                active_window_minutes=max(config.catty_proactive_active_window_minutes, 0.0),
                active_min_messages=max(config.catty_proactive_active_min_messages, 0),
            )
            for group_id in due_group_ids:
                try:
                    await _send_proactive_bubble(bot, group_id)
                except OpenAICompatibleError as exc:
                    logger.warning(f"Proactive bubble API error for group {group_id}: {exc}")
                except httpx.HTTPError as exc:
                    logger.warning(f"Proactive bubble transport error for group {group_id}: {exc}")
                except Exception as exc:
                    if _is_removed_from_group_error(exc):
                        _forget_removed_group(group_id, reason="send failed because bot was removed from group")
                    else:
                        logger.warning(f"Failed to send proactive bubble to group {group_id}: {exc}")
                await asyncio.sleep(2)


@get_driver().on_startup
async def start_memory_summary_loop() -> None:
    cache = _get_session_cache()
    logger.info(
        f"session_cache: loaded {cache.total_sessions()} sessions from {cache.directory} "
        f"(persistence={cache.persistence_enabled}, max={cache.max_sessions})"
    )
    asyncio.create_task(_hot_reload_loop())
    asyncio.create_task(_summary_loop())
    asyncio.create_task(_proactive_bubble_loop())
    asyncio.create_task(_local_critic_warmup_loop())
    asyncio.create_task(cache.background_flush_loop())
    asyncio.create_task(memory_store.background_flush_loop())


@get_driver().on_shutdown
async def _flush_session_cache_on_shutdown() -> None:
    if _session_cache is None:
        return
    written = _session_cache.flush_sync()
    if written:
        logger.info(f"session_cache: flushed {written} dirty sessions on shutdown")


@get_driver().on_shutdown
async def _flush_memory_store_on_shutdown() -> None:
    try:
        if memory_store.flush_sync():
            logger.info("memory_store: flushed dirty data on shutdown")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"memory_store: shutdown flush failed: {exc}")


@chat_matcher.handle()
async def handle_chat(matcher: Matcher, event: MessageEvent, state: T_State) -> None:
    incoming: ExtractedMessage = state["catty_incoming"]
    group_filter_context = str(state.get("catty_group_filter_context") or "")
    special_care_context = str(state.get("catty_special_care_context") or "")
    gate_result = state.get("catty_reply_gate_result")
    fallback_decision_context = (
        _fallback_reply_decision_context(gate_result)
        if isinstance(gate_result, dict)
        else ""
    )
    history_key = build_history_key(event, config)
    queue_key = _conversation_queue_key(event)

    # 同会话锁排队时记下入队时刻——拿到锁后若已经等了太久,直接放弃当前消息,
    # 避免视觉/AI 卡顿后积压的消息一窝蜂全回出去(就是主人吐槽的"爆一大堆")。
    enqueue_started_at = time.monotonic()
    queue_was_busy = _locks[queue_key].locked()

    async with _locks[queue_key]:
        queue_wait_seconds = time.monotonic() - enqueue_started_at
        queue_abandon_threshold = max(
            float(getattr(config, "catty_reply_queue_max_wait_seconds", 25.0) or 0.0),
            0.0,
        )
        if queue_was_busy and queue_abandon_threshold > 0 and queue_wait_seconds >= queue_abandon_threshold:
            logger.info(
                "Abandoning queued reply (waited %.1fs >= %.1fs threshold): user=%s scope=%s text=%s",
                queue_wait_seconds,
                queue_abandon_threshold,
                event.user_id,
                queue_key,
                incoming.text[:60],
            )
            await matcher.finish()

        anger_context = ""
        if isinstance(event, GroupMessageEvent) and config.catty_filter_anger_enabled and not group_filter_context:
            cooldown_remaining = memory_store.user_anger_cooldown_remaining_seconds(event)
            if cooldown_remaining > 0:
                anger_context = _anger_reply_decision_context(event, remaining=cooldown_remaining)
            else:
                try:
                    anger_result = await assess_user_anger(
                        config,
                        incoming.text,
                        current_anger=memory_store.user_anger_score(event),
                        has_image=incoming.has_image,
                    )
                except OpenAICompatibleError as exc:
                    logger.warning(f"User anger filter API error: {exc}")
                except httpx.HTTPError as exc:
                    logger.warning(f"User anger filter transport error: {exc}")
                else:
                    anger_state = memory_store.update_user_anger(
                        event,
                        delta=int(anger_result.get("anger_delta") or 0),
                        reason=str(anger_result.get("reason") or ""),
                        useless=bool(anger_result.get("useless")),
                        mute_threshold=config.catty_filter_anger_mute_threshold,
                        cooldown_seconds=config.catty_filter_anger_cooldown_seconds,
                    )
                    if anger_state.get("muted"):
                        anger_context = _anger_reply_decision_context(
                            event,
                            remaining=config.catty_filter_anger_cooldown_seconds,
                            newly_muted=True,
                            reason=str(anger_state.get("reason") or ""),
                        )
                    else:
                        anger_context = memory_store.build_anger_context(
                            event,
                            warn_threshold=config.catty_filter_anger_warn_threshold,
                        )

        memory_store.remember_event(event)

        if _is_memory_cache_clear_request(incoming.text):
            _reset_history(history_key)
            result = memory_store.clear_cache(event)
            await matcher.finish(Message(f"{result}\n会话上下文也清掉啦。"))

        if _is_memory_view_request(incoming.text):
            await matcher.finish(Message(memory_store.build_memory_view(event)))

        if _is_reset_request(incoming.text):
            _reset_history(history_key)
            await matcher.finish(Message("上下文清掉啦。"))

        if _is_session_list_request(incoming.text):
            owner_qq = int(getattr(config, "catty_owner_qq", 0) or 0)
            if isinstance(event, PrivateMessageEvent) and owner_qq > 0 and int(event.user_id) == owner_qq:
                await matcher.finish(
                    Message(format_session_list_for_owner(_get_session_cache()))
                )

        if is_turtle_soup_request(incoming.text):
            if isinstance(event, GroupMessageEvent):
                soup_key = turtle_soup_cooldown_key(event.group_id)
                remaining = turtle_soup_remaining(
                    _turtle_soup_cooldowns,
                    soup_key,
                    cooldown_seconds=config.catty_turtle_soup_cooldown_seconds,
                )
                if remaining > 0:
                    await matcher.finish(
                        Message(
                            "哼，这个群刚端过一碗海龟汤啦喵～"
                            f"还剩 {format_duration_cn(remaining)} 才能开下一锅，先问问上一题也不是不行。"
                        )
                    )
                _turtle_soup_cooldowns[soup_key] = time.monotonic()
            else:
                soup_key = turtle_soup_cooldown_key(None)
            await matcher.finish(Message(choose_turtle_soup(soup_key)))

        if not _has_api_key():
            await matcher.finish(Message("还没有配置 API Key，先在 config.json 里填好 ai.api_key 再来找我。"))

        web_search_context = ""
        web_search_query = extract_web_search_query(incoming.text)
        if web_search_query and config.catty_web_search_enabled:
            search_key = search_cooldown_key(event.user_id)
            if not _web_search_exempt(event):
                now = time.monotonic()
                search_cooldown = max(config.catty_web_search_cooldown_seconds, 0)
                last_search = _web_search_cooldowns.get(search_key, 0.0)
                remaining = max(last_search + search_cooldown - now, 0.0)
                if remaining > 0:
                    await matcher.finish(Message(_persona_search_cooldown_message(event, remaining)))
                _web_search_cooldowns[search_key] = now
            web_search_context = await _build_web_search_context(web_search_query)
        elif web_search_query:
            web_search_context = "本轮用户要求联网搜索，但当前配置关闭了 web_search.enabled。请用猫系人格说明联网搜索暂时不可用。"

        current_group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
        # 群标签:除了 config 的内置 group_ids,还要看 memory_store 里主 AI 自己用
        # catty_group_game_tag 打上的群-游戏关联。任一命中就当成 group_related。
        memory_tagged_games = (
            memory_store.get_group_games(current_group_id) if current_group_id is not None else []
        )
        star_resonance_context = build_star_resonance_context(
            incoming.text,
            group_id=current_group_id,
            group_ids=config.catty_game_context_star_resonance_group_ids,
            memory_store=memory_store,
            force_group_related="star_resonance" in memory_tagged_games,
        )
        strinova_context = build_strinova_context(
            incoming.text,
            group_id=current_group_id,
            group_ids=config.catty_game_context_strinova_group_ids,
            memory_store=memory_store,
            force_group_related="strinova" in memory_tagged_games,
        )
        # 其他游戏(非 strinova/star_resonance,由主 AI 用 catty_group_game_tag 标进来):
        # 当前群被 tag 了哪些游戏,就把那个游戏的动态记忆库拼进 context。
        other_game_contexts: list[str] = []
        for game_name in memory_tagged_games:
            if game_name in {"strinova", "star_resonance"}:
                continue
            dynamic = memory_store.build_dynamic_game_context(game_name, recent_facts_limit=6)
            if not dynamic:
                continue
            other_game_contexts.append(
                f"本群被标记为《{game_name}》相关。猫猫长期积累的事实记忆:\n{dynamic}"
            )
        wake_context = _wake_context_prompt(
            event,
            incoming,
            group_filter_context=bool(group_filter_context),
            bot_continuation=bool(state.get("catty_recent_bot_continuation")),
        )
        bot_continuation_context = (
            _bot_continuation_judgement_prompt(event)
            if state.get("catty_recent_bot_continuation")
            else ""
        )

        image_description: str | None = None
        image_description_cached = False
        image_analysis: dict[str, object] = {}
        emoji_context = ""
        if incoming.has_image and config.catty_image_vision_enabled:
            # 先查持久缓存:命中=旧图,corpus 已经写过,后面不重复写。
            persistent_summary = memory_store.get_image_summary(incoming.image_keys)
            if persistent_summary:
                image_description = persistent_summary
                image_description_cached = True
            else:
                # 没命中持久缓存:_rule 已经 fire-and-forget schedule 过 vision,
                # 这里冗余调度一次防漏,再最多短等几秒。等不到就不带描述往下走,
                # 后台 task 跑完会写 memory_store,下一轮自动复用。
                _schedule_vision_async(
                    incoming.image_keys,
                    incoming.image_urls,
                    incoming.history_content,
                )
                max_wait = max(
                    float(getattr(config, "catty_vision_inline_max_wait_seconds", 3.0) or 0.0),
                    0.0,
                )
                vision_result = await _await_vision_briefly(incoming.image_keys, max_wait)
                if vision_result is not None:
                    image_description = vision_result.description or None
                    image_analysis = vision_result.image_analysis or {}
            if image_analysis and config.catty_emoji_enabled:
                tags_value = image_analysis.get("emotion_tags")
                tags = [str(tag) for tag in tags_value] if isinstance(tags_value, list) else []
                interest = int(image_analysis.get("interest") or 0)
                query = str(image_analysis.get("emoji_query") or image_analysis.get("expression") or "")
                if (
                    incoming.image_urls
                    and interest >= config.catty_emoji_save_interest_threshold
                    and bool(image_analysis.get("save_as_emoji"))
                ):
                    try:
                        image_data, content_type = await download_binary(config, incoming.image_urls[0])
                        emoji_store.save_downloaded(
                            image_data=image_data,
                            content_type=content_type,
                            source_url=incoming.image_urls[0],
                            meaning=str(image_analysis.get("expression") or image_analysis.get("summary") or ""),
                            tags=tags,
                            interest=interest,
                        )
                    except httpx.HTTPError as exc:
                        logger.warning(f"Failed to download high-interest emoji image: {exc}")
                    except OSError as exc:
                        logger.warning(f"Failed to save high-interest emoji image: {exc}")
                if interest >= config.catty_emoji_interest_threshold:
                    candidates = emoji_store.candidates_text(query, tags=tags)
                    emoji_context = _emoji_reply_context(image_analysis, candidates)
        if not emoji_context:
            emoji_context = _generic_emoji_context(incoming)
        semantic_reply_split = await _should_request_semantic_reply_split(incoming)
        messages = _build_messages(
            event,
            history_key,
            incoming,
            image_description=image_description,
            anger_context=anger_context,
            semantic_reply_split=semantic_reply_split,
            group_filter_context=group_filter_context,
            special_care_context=special_care_context,
            emoji_context=emoji_context,
            web_search_context="\n\n".join(
                part for part in [web_search_context, fallback_decision_context] if part
            ),
            star_resonance_context=star_resonance_context,
            strinova_context=strinova_context,
            other_game_contexts=other_game_contexts,
            wake_context=wake_context,
            bot_continuation_context=bot_continuation_context,
        )
        # Function calling tools 注入:event/memory_store/config 通过 ToolContext 传给 executor。
        # prepare_nsfw_segments_fn / download_binary_fn 是依赖注入——避免 tools.py 反向 import
        # __init__.py 里的 _prepare_nsfw_image_segments(它要复用本模块的 sent_registry / cache_dir)。
        # ctx.pending_image_segments 收集 catty_nsfw_search 下载到的图片 segments,主回复后并入发送。
        tool_ctx = ToolContext(
            config=config,
            memory_store=memory_store,
            event=event,
            prepare_nsfw_segments_fn=_prepare_nsfw_image_segments,
            download_binary_fn=download_binary,
        )

        async def _tool_executor(name: str, args_json: str) -> dict[str, object]:
            return await execute_tool_call(name, args_json, tool_ctx)

        tools_for_main_reply = available_tool_schemas(
            config, is_private=isinstance(event, PrivateMessageEvent)
        )
        nsfw_image_segments: list[MessageSegment] = []
        try:
            reply = await chat_completion_with_tools(
                config,
                messages,
                tools=tools_for_main_reply,
                tool_executor=_tool_executor,
                max_rounds=int(getattr(config, "catty_tools_max_rounds", 3) or 3),
                max_calls_per_round=int(getattr(config, "catty_tools_max_calls_per_round", 3) or 3),
            )
            nsfw_image_segments = list(tool_ctx.pending_image_segments)
            # 兜底:旧 marker 教学已经删,理论上不会再漏 [[CATTY_WEB_SEARCH]] / [[CATTY_NSFW_SEARCH]],
            # 但保留 sanitize 防御被旧 prompt cache / fallback model 残留偶然写出。
            sanitized = _sanitize_residual_markers(reply)
            if sanitized != reply:
                logger.warning(
                    f"Residual search marker stripped from final reply (had_image_segments={bool(nsfw_image_segments)})"
                )
                if not sanitized.strip():
                    sanitized = (
                        "哼～主人这种东西也想看喵！(脸红甩尾巴) 嗷呜～ฅฅ"
                        if nsfw_image_segments
                        else "喵呜～猫猫这次没搜到合适的嗷呜，主人换个名字再戳人家嘛 (尾巴垂垂)"
                    )
                reply = sanitized
        except MCBusyError as exc:
            logger.info(f"MC busy gate refused local fallback: {exc}")
            await matcher.finish(Message(exc.public_message))
        except OpenAICompatibleError as exc:
            logger.warning(f"OpenAI-compatible API error: {exc}")
            await matcher.finish(Message(f"AI 接口出错：{exc.public_message}"))
        except httpx.TimeoutException:
            logger.warning("OpenAI-compatible API request timed out")
            await matcher.finish(Message(
                "AI 接口超时了喵呜～(尾巴垂垂)云端可能在喘气，过 30 秒再戳人家叭。"
            ))
        except httpx.HTTPError as exc:
            logger.warning(f"OpenAI-compatible API transport error: {exc}")
            await matcher.finish(Message(
                "AI 接口连不上喵～(爪爪挠头)云端和本地兜底都没响应，主人查下网络再试。"
            ))

        reply = await _apply_local_critic(event, incoming, messages, reply)

        if _is_no_reply(reply):
            if state.get("catty_recent_bot_continuation"):
                _decrement_bot_reply_continuation(event)
            logger.info(
                "Main AI chose NO_REPLY after wake context: user=%s group=%s continuation_remaining=%s text=%s",
                event.user_id,
                getattr(event, "group_id", ""),
                _bot_reply_continuation_remaining(event),
                incoming.text[:80],
            )
            await matcher.finish()

        reply, emoji_query = _extract_emoji_query(reply)
        # 注:梗图现在走 catty_meme_query toolcall,AI 自己把 base64:// URI 嵌入 INLINE_IMAGE 标记。
        # 多模态 AI 仍可能在 _extract_content 阶段直接产生 INLINE_IMAGE,两条路径都被发送链路统一解析。
        _save_assistant_training_sample(
            event, incoming, messages, _strip_inline_image_markers(reply), emoji_query=emoji_query,
        )
        emoji_entry = await _choose_or_download_emoji(event, emoji_query, incoming, image_analysis) if emoji_query else None
        if emoji_query and emoji_entry is None:
            logger.info("Emoji query did not resolve to an image: %s", emoji_query)
        if emoji_entry is None and not emoji_query and _should_auto_emoji_reply(incoming, reply):
            emoji_entry = _choose_auto_emoji(event, reply, incoming)
            if emoji_entry is None:
                logger.info("Auto emoji skipped because no local emoji entry is available")
        if emoji_entry is not None:
            _remember_emoji_choice(event, emoji_entry)
            logger.info("Selected emoji for reply: %s", emoji_entry.path)
        # 把 reply 里的 LaTeX 块和 INLINE_IMAGE 标记都占位符化(分别 \x00LATEX_n\x00 / \x00IMG_n\x00):
        # chunks 内只剩短占位符,_reply_chunks 切段时不会切到公式或图片标记中间;
        # 发送时 _compose_reply_message 看到占位符再渲染成图片消息段。
        # history/memory 用文本+[图片]兜底,base64 不进 prompt token。
        reply_for_send, latex_sources = replace_latex_with_placeholders(reply)
        reply_for_send, inline_image_urls = _extract_inline_images(reply_for_send)
        chunks = _reply_chunks(reply_for_send)
        if image_description and not image_description_cached:
            memory_store.remember_image_summary(event, image_description)
        if chunks:
            history_text = _strip_inline_image_placeholders(
                restore_latex_placeholders("\n".join(chunks), latex_sources)
            )
        else:
            history_text = _strip_inline_image_markers(reply)
        _append_history(history_key, incoming.history_content, history_text)
        if special_care_context and chunks:
            memory_store.record_special_care_reply_sent(event, history_text)

        delay_seconds = max(config.catty_reply_human_split_delay_seconds, 0.0)
        quote_pending = _should_quote_chat_reply(
            event,
            incoming,
            group_filter_context=group_filter_context,
            bot_continuation=bool(state.get("catty_recent_bot_continuation")),
        )

        def _chunk_to_history(chunk_text: str) -> str:
            return _strip_inline_image_placeholders(
                restore_latex_placeholders(chunk_text, latex_sources)
            )

        for chunk in chunks[:-1]:
            _remember_bot_reply_for_event(event, _chunk_to_history(chunk))
            await matcher.send(
                _compose_reply_message(
                    event,
                    text=chunk,
                    quote=quote_pending,
                    latex_sources=latex_sources,
                    inline_image_urls=inline_image_urls,
                )
            )
            quote_pending = False
            _mark_consumed_reply_source_if_sent(event, state)
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
        if nsfw_image_segments:
            if chunks:
                _remember_bot_reply_for_event(event, _chunk_to_history(chunks[-1]))
                try:
                    await matcher.send(
                        _compose_reply_message(
                            event,
                            text=chunks[-1],
                            quote=quote_pending,
                            latex_sources=latex_sources,
                            inline_image_urls=inline_image_urls,
                        )
                    )
                except OnebotActionFailed as exc:
                    logger.warning(f"NSFW caption send failed (napcat timeout?): {exc}")
                quote_pending = False
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
            _mark_consumed_reply_source_if_sent(event, state)
            sent_count = 0
            failed_count = 0
            retry_count = 0
            for seg in nsfw_image_segments:
                sent = False
                last_exc: OnebotActionFailed | None = None
                for attempt in range(2):  # 1 次原始 + 1 次重试，对付瞬时 NT timeout
                    try:
                        await matcher.send(Message(seg))
                        sent = True
                        if attempt > 0:
                            retry_count += 1
                            logger.info(f"NSFW image sent OK on retry attempt {attempt + 1}")
                        break
                    except OnebotActionFailed as exc:
                        last_exc = exc
                        if attempt == 0:
                            logger.warning(
                                f"NSFW image send failed (attempt 1, will retry in 2s): {exc}"
                            )
                            await asyncio.sleep(2.0)
                        else:
                            logger.warning(
                                f"NSFW image send failed twice (giving up, likely QQ NSFW filter): {exc}"
                            )
                if sent:
                    sent_count += 1
                else:
                    failed_count += 1
                    _ = last_exc  # 已经在 retry 循环里 log 过
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
            logger.info(
                f"NSFW image sends completed: {sent_count} ok / {failed_count} failed "
                f"(retried_ok={retry_count})"
            )
            if failed_count and not sent_count:
                # 全部都没送出去——大概率是 QQ 服务器对 NSFW 内容的反垃圾审核拦截了
                try:
                    await matcher.send(Message(
                        "喵呜～图下下来了但 QQ 服务器把它拦掉了嗷呜（NT timeout 多半是被反垃圾审核），"
                        "主人换个角色或者关键词再试嘛 (尾巴垂垂)"
                    ))
                except OnebotActionFailed:
                    pass
            await matcher.finish()
        if emoji_entry:
            if chunks:
                _remember_bot_reply_for_event(event, _chunk_to_history(chunks[-1]))
                if config.catty_reply_mix_emoji_with_text:
                    _mark_consumed_reply_source_if_sent(event, state)
                    await matcher.finish(
                        _compose_reply_message(
                            event,
                            text=chunks[-1],
                            emoji_entry=emoji_entry,
                            quote=quote_pending,
                            latex_sources=latex_sources,
                            inline_image_urls=inline_image_urls,
                        )
                    )
                await matcher.send(
                    _compose_reply_message(
                        event,
                        text=chunks[-1],
                        quote=quote_pending,
                        latex_sources=latex_sources,
                        inline_image_urls=inline_image_urls,
                    )
                )
                quote_pending = False
                _mark_consumed_reply_source_if_sent(event, state)
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
            else:
                _mark_consumed_reply_source_if_sent(event, state)
            await matcher.finish(_compose_reply_message(event, emoji_entry=emoji_entry, quote=quote_pending))
        _mark_consumed_reply_source_if_sent(event, state)
        final_message = chunks[-1] if chunks else "喵喵！猫猫现在很忙哦，等一下再来找人家～"
        _remember_bot_reply_for_event(event, _chunk_to_history(final_message) if chunks else final_message)
        await matcher.finish(
            _compose_reply_message(
                event,
                text=final_message,
                quote=quote_pending,
                latex_sources=latex_sources,
                inline_image_urls=inline_image_urls,
            )
        )
