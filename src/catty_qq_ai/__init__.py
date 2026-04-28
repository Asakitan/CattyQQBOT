import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
import time
from typing import DefaultDict

import httpx
from nonebot import get_bots, get_driver, get_plugin_config, logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PokeNotifyEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State

from .config import Config
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
    reply_message_ids,
    split_reply,
)
from .emoji_store import EmojiEntry, EmojiStore
from .memory import MemoryStore
from .openai_client import (
    OpenAICompatibleError,
    analyze_images_for_reply,
    assess_user_anger,
    chat_completion,
    describe_images,
    download_binary,
    local_critic_completion,
)
from .persona_prompts import (
    build_catgirl_examples_prompt,
    build_persona_memory_prompt,
    build_reply_intelligence_prompt,
    build_reply_self_check_prompt,
)
from .reply_markers import (
    EMOJI_QUERY_PREFIX,
    EMOJI_QUERY_SUFFIX,
    NO_REPLY_MARKER,
    REPLY_SPLIT_MARKER,
    TRAILING_CHAT_PUNCTUATION,
    extract_emoji_query as _extract_emoji_query,
)
from .star_resonance_memory import build_star_resonance_context
from .web_search import format_search_context, search_web


__plugin_meta__ = PluginMetadata(
    name="Catty QQ AI",
    description="OpenAI-compatible QQ chat plugin for NoneBot2 and OneBot v11.",
    usage="私聊直接发消息；群聊 @机器人 或发送 ai <内容>。",
    config=Config,
    supported_adapters={"~onebot.v11"},
)


config = get_plugin_config(Config)
memory_store = MemoryStore(config)
emoji_store = EmojiStore(config)

ChatMessage = dict[str, object]
_histories: DefaultDict[str, list[ChatMessage]] = defaultdict(list)
_locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


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


_expression_repeats: DefaultDict[str, ExpressionRepeatState] = defaultdict(ExpressionRepeatState)
_group_filter_batches: DefaultDict[str, GroupFilterBatchState] = defaultdict(GroupFilterBatchState)
_group_filter_locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_web_search_cooldowns: dict[str, float] = {}
_turtle_soup_cooldowns: dict[str, float] = {}
_local_critic_warmup_success_logged = False
_consumed_reply_source_ids: dict[str, float] = {}
_recent_bot_reply_threads: dict[str, float] = {}


def _has_api_key() -> bool:
    return bool(config.catty_openai_api_key.strip())


def _conversation_queue_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group:{event.group_id}"
    return f"private:{event.user_id}"


def _reply_source_key(event: MessageEvent, message_id: str) -> str:
    scope = _conversation_queue_key(event)
    return f"{scope}:reply-source:{message_id}"


def _followup_thread_key(event: MessageEvent) -> str | None:
    if isinstance(event, GroupMessageEvent):
        return f"group:{event.group_id}:user:{event.user_id}"
    if isinstance(event, PrivateMessageEvent):
        return f"private:{event.user_id}"
    return None


def _prune_recent_bot_reply_threads(now: float) -> None:
    max_age = max(float(config.catty_followup_reply_window_seconds or 0.0), 0.0)
    if max_age <= 0:
        _recent_bot_reply_threads.clear()
        return
    stale = [key for key, timestamp in _recent_bot_reply_threads.items() if now - timestamp > max_age]
    for key in stale:
        _recent_bot_reply_threads.pop(key, None)


def _recent_bot_reply_context(event: MessageEvent) -> dict[str, object]:
    window_seconds = max(float(config.catty_followup_reply_window_seconds or 0.0), 0.0)
    if window_seconds <= 0:
        return {"recent_bot_reply": False}
    key = _followup_thread_key(event)
    if key is None:
        return {"recent_bot_reply": False}
    now = time.monotonic()
    _prune_recent_bot_reply_threads(now)
    last_reply_at = _recent_bot_reply_threads.get(key)
    if last_reply_at is None:
        return {"recent_bot_reply": False}
    elapsed = now - last_reply_at
    return {
        "recent_bot_reply": elapsed <= window_seconds,
        "seconds_since_bot_reply": round(elapsed, 1),
        "followup_window_seconds": window_seconds,
    }


def _has_recent_bot_reply(event: MessageEvent) -> bool:
    return bool(_recent_bot_reply_context(event).get("recent_bot_reply"))


def _mark_recent_bot_reply_thread(event: MessageEvent) -> None:
    key = _followup_thread_key(event)
    if key is None:
        return
    now = time.monotonic()
    _prune_recent_bot_reply_threads(now)
    _recent_bot_reply_threads[key] = now


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
    except httpx.HTTPError as exc:
        logger.warning(f"Web search failed for {query}: {exc}")
        return f"本轮用户要求联网搜索「{query}」，但搜索请求失败：{exc}。请如实说明搜索失败，不要编造。"
    return format_search_context(query, results)


def _reset_history(key: str) -> None:
    _histories.pop(key, None)


def _append_history(key: str, user_content: str, assistant_content: str) -> None:
    history = _histories[key]
    history.append({"role": "user", "content": user_content})
    history.append({"role": "assistant", "content": assistant_content})
    max_messages = max(config.catty_history_turns, 0) * 2
    if max_messages and len(history) > max_messages:
        del history[:-max_messages]
    elif max_messages == 0:
        history.clear()


def _build_user_content(incoming: ExtractedMessage, *, image_description: str | None = None) -> object:
    if not incoming.image_urls:
        return incoming.history_content
    if image_description:
        return f"{incoming.history_content}\n图片识别结果：\n{image_description}\n请基于图片识别结果和上下文自然回应。"
    urls = "\n".join(f"- {url}" for url in incoming.image_urls)
    return f"{incoming.history_content}\n图片下载地址：\n{urls}\n请基于这些图片地址和上下文自然回应。"


def _emoji_reply_context(image_analysis: dict[str, object], candidates: str) -> str:
    if not candidates:
        return ""
    tags = image_analysis.get("emotion_tags")
    tag_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
    return (
        "本轮可以额外发送一个本地表情包，普通轻松聊天默认建议发送。"
        "如果需要表情包，在回复正文末尾单独追加一行 "
        f"{EMOJI_QUERY_PREFIX}你的表情意图{EMOJI_QUERY_SUFFIX}；"
        "不要解释这个标记；只有严肃排错、道歉、风险提醒或确实不适合时才不输出标记。"
        "优先选择能自然贴合情绪的默认表情；没有合适默认表情时才用下载表情。\n"
        f"图片兴趣度：{image_analysis.get('interest', 0)}/100\n"
        f"图片/表情含义：{image_analysis.get('expression') or image_analysis.get('summary') or ''}\n"
        f"情绪标签：{tag_text}\n"
        f"可用表情候选：\n{candidates}"
    )


def _emoji_segment(entry: EmojiEntry) -> MessageSegment:
    return MessageSegment.image(file=entry.path.resolve().as_uri())


def _generic_emoji_context(incoming: ExtractedMessage) -> str:
    if not config.catty_emoji_enabled:
        return ""
    candidates = emoji_store.candidates_text(incoming.text)
    if not candidates:
        return ""
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


def _choose_auto_emoji(reply: str, incoming: ExtractedMessage) -> EmojiEntry | None:
    return emoji_store.choose(reply or incoming.text)


async def _choose_or_download_emoji(
    query: str,
    incoming: ExtractedMessage,
    image_analysis: dict[str, object],
) -> EmojiEntry | None:
    if not query.strip() or not config.catty_emoji_enabled:
        return None

    entry = emoji_store.choose(query, refresh_on_miss=True)
    if entry is not None:
        return entry

    tags_value = image_analysis.get("emotion_tags")
    tags = [str(tag) for tag in tags_value] if isinstance(tags_value, list) else []
    entry = emoji_store.adopt_downloaded(query, tags=tags)
    if entry is not None:
        logger.info("Adopted downloaded emoji for query %s: %s", query, entry.path)
        return entry

    if not incoming.image_urls:
        return None

    try:
        image_data, content_type = await download_binary(config, incoming.image_urls[0])
        entry = emoji_store.save_downloaded(
            image_data=image_data,
            content_type=content_type,
            source_url=incoming.image_urls[0],
            meaning=query,
            tags=[*tags, query],
            interest=max(config.catty_emoji_save_interest_threshold, 50),
        )
    except httpx.HTTPError as exc:
        logger.warning(f"Failed to download missing emoji candidate for {query}: {exc}")
        return None
    except OSError as exc:
        logger.warning(f"Failed to save missing emoji candidate for {query}: {exc}")
        return None
    return entry


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
) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    system_prompt = config.catty_system_prompt.strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    persona_memory = build_persona_memory_prompt(system_prompt)
    if persona_memory:
        messages.append({"role": "system", "content": persona_memory})
    messages.append({"role": "system", "content": build_reply_intelligence_prompt(NO_REPLY_MARKER)})
    if config.catty_reply_self_check_enabled:
        messages.append(
            {
                "role": "system",
                "content": build_reply_self_check_prompt(NO_REPLY_MARKER, REPLY_SPLIT_MARKER),
            }
        )
    if config.catty_reply_style_examples_enabled:
        messages.append({"role": "system", "content": build_catgirl_examples_prompt(NO_REPLY_MARKER)})
    if _direct_reply_required(event, incoming) and config.catty_local_critic_force_direct_reply:
        messages.append({"role": "system", "content": _direct_reply_required_prompt(incoming)})
    if semantic_reply_split:
        messages.append({"role": "system", "content": _semantic_reply_split_prompt()})
    if incoming.opportunistic or group_filter_context:
        messages.append({"role": "system", "content": _opportunistic_reply_prompt()})
    messages.append({"role": "system", "content": _reply_gate_approved_prompt()})
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
    if emoji_context:
        messages.append({"role": "system", "content": emoji_context})
    memory_context = memory_store.build_context(event)
    if memory_context:
        messages.append({"role": "system", "content": memory_context})
    messages.extend(_histories.get(key, []))
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
    min_chars = max(config.catty_reply_human_split_min_chars, 1)
    return (
        "本轮允许你在语义自然时把回复拆成两条 QQ 消息。"
        f"只有当回复预计不少于约 {min_chars} 个中文字符、且拆分后两条都完整自然时才拆；"
        "如果拆成两条，第一条结尾不能带标点，整体尽量像群友聊天一样少用收尾标点；"
        f"如果决定拆分，只在两条消息之间单独输出一行 {REPLY_SPLIT_MARKER}。"
        "不要解释这个标记，不要为了拆分牺牲原本回答质量；不适合拆分就正常单条回复。"
    )


def _opportunistic_reply_prompt() -> str:
    return (
        "这是由本地 reply gate 放行的普通群聊/特别关心/批量观察消息。"
        "本地模型已经判断本轮值得回复，你只负责写自然正文；不要再做是否回复判断。"
    )


def _reply_gate_approved_prompt() -> str:
    return (
        "本轮消息已经通过本地 reply gate；主 AI 只负责生成要发送给用户的正文。"
        f"禁止输出 {NO_REPLY_MARKER}，禁止用空回复代替正文。"
        "如果信息不足，就用笨猫口吻短短追问。"
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
        f"除非消息完全无法解析且没有可追问点，否则不要输出 {NO_REPLY_MARKER}。"
        "如果信息不足，就用笨猫口吻短短追问；如果只是 @/回复但没文字，也要自然应一声。"
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
            "本地 reply gate 已经放行本轮消息，请用 1-3 句自然接住。"
            "不要机械回复“你叫我了/我在”，也不要输出不回复标记。"
        )
    return (
        "本轮没有明确 @ 你、回复你或使用开头前缀，只是句子中出现了你的名字、指向词或功能词。"
        f"当前回复倾向约 {probability_percent}%{boost_text}。"
        "本地 reply gate 已经判断应该回复；请根据整句主语、称呼对象、上下文意图自然接话。"
        "不要根据关键词机械回应，也不要输出不回复标记。"
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


async def _warm_local_critic_model() -> None:
    global _local_critic_warmup_success_logged
    keep_alive = config.catty_local_critic_warmup_keep_alive.strip()
    payload: dict[str, object] = {
        "model": config.catty_local_critic_model,
        "stream": False,
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive
    timeout = max(float(config.catty_local_critic_warmup_request_timeout or 60.0), 1.0)
    client_kwargs: dict[str, object] = {"timeout": timeout, "follow_redirects": True}
    if config.catty_http_proxy.strip():
        client_kwargs["proxy"] = config.catty_http_proxy.strip()
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(_ollama_native_generate_url(), json=payload)
    response.raise_for_status()
    if not _local_critic_warmup_success_logged:
        logger.info(
            "Local critic Ollama warmup loaded model %s with keep_alive=%s",
            config.catty_local_critic_model,
            keep_alive or "default",
        )
        _local_critic_warmup_success_logged = True
    else:
        logger.debug("Local critic Ollama warmup refreshed model %s", config.catty_local_critic_model)


async def _local_critic_warmup_loop() -> None:
    if not config.catty_local_critic_warmup_enabled or not _local_critic_enabled():
        return
    interval = max(float(config.catty_local_critic_warmup_interval_seconds or 300.0), 60.0)
    while True:
        try:
            await _warm_local_critic_model()
        except httpx.HTTPError as exc:
            logger.warning(f"Local critic Ollama warmup failed: {_http_error_detail(exc)}")
        except Exception as exc:
            logger.warning(f"Local critic Ollama warmup failed: {exc}")
        await asyncio.sleep(interval)


def _local_critic_event_payload(
    event: MessageEvent,
    incoming: ExtractedMessage,
    draft_reply: str,
) -> dict[str, object]:
    followup_context = _recent_bot_reply_context(event)
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
        **followup_context,
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
    followup_context = _recent_bot_reply_context(event)
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
        "opportunistic": incoming.opportunistic,
        "direct_reply_required": _direct_reply_required(event, incoming),
        **followup_context,
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
                "Fast QQ reply gate. JSON only. "
                "true=direct bot ask/image help/special-care/batched context expects bot. "
                "recent_bot_reply=true means the bot just replied to this same user in this chat; "
                "then short follow-ups, teasing, questions, or second-person commands are usually active conversation, "
                "but still output false for clear third-person chatter/spam/no expectation. "
                "false=idle chatter/third-person/no expectation/spam. "
                "Schema:{\"should_reply\":true|false,\"confidence\":0-100,\"reason\":\"<=8 chars\"}."
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
            "本地 reply gate 本轮不可用或返回异常，才由主 AI 接管是否回复。"
            "请沿用旧硬判断：如果 @、回复机器人、前缀、私聊、明显喊猫猫办事，就回复；"
            f"如果只是普通旁观群聊、第三人称闲聊、无接话期待，请只输出 {NO_REPLY_MARKER}。"
        ),
    }
    return "本地 reply gate fallback，本轮是否回复临时交给主 AI 判断：\n" + json.dumps(payload, ensure_ascii=False)


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
    direct_required = _direct_reply_required(event, incoming) and config.catty_local_critic_force_direct_reply
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
    if not reply.strip():
        reply = NO_REPLY_MARKER
    if _is_no_reply(reply):
        return await _resolve_no_reply(event, incoming, messages, reply)
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

    _save_local_critic_sample(event, incoming, reply, critic_result, final_reply)
    return final_reply


def _group_filter_reply_context(batch: list[GroupFilterBatchMessage]) -> str:
    lines = [
        f"{index}. {message.history_content}{' [含图片]' if message.has_image else ''}"
        for index, message in enumerate(batch, 1)
    ]
    return (
        "下面是本群这轮按 filter 批量窗口攒到的普通群聊消息。"
        "它们不是明确 @ 你、回复你或前缀命令；请先压缩理解最近没 filter 的疑似话题，"
        "只在发现明显指向 BOT/AI/猫猫、需要你补充、或群友期待机器人回应的话题时回复。\n"
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
    if not config.catty_reply_human_split_enabled:
        return False
    probability = max(min(float(config.catty_reply_human_split_probability), 1.0), 0.0)
    if probability <= 0:
        return False
    min_chars = max(config.catty_reply_human_split_min_chars, 1)
    if len(incoming.history_content.strip()) < min_chars:
        return False
    return random.random() < probability


def _build_proactive_messages(group_id: str) -> list[ChatMessage]:
    max_daily = max(config.catty_proactive_max_daily_per_group, 0)
    daily_target = memory_store.proactive_daily_target(group_id, max_daily=max_daily)
    context = memory_store.build_proactive_context(
        group_id,
        recent_limit=max(config.catty_proactive_recent_messages, 1),
    )
    system_prompt = config.catty_system_prompt.strip()
    messages: list[ChatMessage] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if config.catty_reply_self_check_enabled:
        messages.append(
            {
                "role": "system",
                "content": build_reply_self_check_prompt(NO_REPLY_MARKER, REPLY_SPLIT_MARKER),
            }
        )
    if config.catty_reply_style_examples_enabled:
        messages.append({"role": "system", "content": build_catgirl_examples_prompt(NO_REPLY_MARKER)})
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
    if config.catty_allowed_group_ids:
        return sorted(str(group_id) for group_id in config.catty_allowed_group_ids)

    group_ids = set(memory_store.group_ids())
    try:
        group_list = await bot.get_group_list()
    except Exception as exc:
        logger.warning(f"Failed to fetch group list for proactive bubbles: {exc}")
    else:
        if isinstance(group_list, list):
            for group in group_list:
                if isinstance(group, dict):
                    group_id = group.get("group_id")
                else:
                    group_id = getattr(group, "group_id", None)
                if group_id is not None:
                    group_ids.add(str(group_id))
    return sorted(group_ids)


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
            await bot.send_group_msg(group_id=_coerce_group_id(group_id), message=Message(chunk))
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
        memory_store.record_proactive_bubble_sent(group_id, sent_text)
        logger.info(f"Sent proactive bubble to group {group_id}")
        return True


def _reply_chunks(reply: str) -> list[str]:
    if REPLY_SPLIT_MARKER not in reply:
        return split_reply(reply, config.catty_reply_max_chars)

    chunks: list[str] = []
    for part in reply.split(REPLY_SPLIT_MARKER):
        chunks.extend(split_reply(part, config.catty_reply_max_chars))
    for index in range(len(chunks) - 1):
        chunks[index] = chunks[index].rstrip(TRAILING_CHAT_PUNCTUATION)
    chunks = [chunk for chunk in chunks if chunk]
    return _cap_reply_chunks(chunks, max_chunks=2)


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
    if incoming.needs_filter and _has_recent_bot_reply(event):
        incoming.needs_filter = False
    group_filter_context = ""
    special_care_context = ""
    if incoming.needs_filter:
        if not isinstance(event, GroupMessageEvent):
            return False
        batch = await _take_due_group_filter_batch(event, incoming)
        if batch is None:
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
        return False
    state["catty_replied_to_self"] = replied_to_self
    state["catty_incoming"] = incoming
    return True


async def _expression_repeat_rule(bot: Bot, event: MessageEvent, state: T_State) -> bool:
    repeat_message = _expression_repeat_message(bot, event)
    if repeat_message is None:
        return False
    state["catty_repeat_message"] = repeat_message
    return True


chat_matcher = on_message(rule=_rule, priority=60, block=True)
expression_repeat_matcher = on_message(rule=_expression_repeat_rule, priority=50, block=True)
observe_matcher = on_message(priority=5, block=False)


def _poke_allowed(bot: Bot, event: PokeNotifyEvent) -> bool:
    if str(event.target_id) != str(bot.self_id) or str(event.user_id) == str(bot.self_id):
        return False
    if event.group_id is not None:
        if not config.catty_enable_group:
            return False
        if config.catty_allowed_group_ids and int(event.group_id) not in config.catty_allowed_group_ids:
            return False
    else:
        if not config.catty_enable_private:
            return False
    if config.catty_allowed_user_ids and int(event.user_id) not in config.catty_allowed_user_ids:
        return False
    return True


async def _poke_rule(bot: Bot, event: PokeNotifyEvent, state: T_State) -> bool:
    if not _poke_allowed(bot, event):
        return False
    state["catty_poke_reply"] = random.choice(
        [
            "喵呜？！谁拍人家尾巴啦～笨猫在这呢，主人要叫猫猫嘛 ฅฅ",
            "哼，被拍到啦～人家才没有偷偷发呆呢，主人说话喵。",
            "喵？猫猫被戳醒了～要人家陪你还是帮你看东西呀 (ฅ>ω<*ฅ)",
        ]
    )
    return True


poke_matcher = on_notice(rule=_poke_rule, priority=55, block=True)


@expression_repeat_matcher.handle()
async def handle_expression_repeat(matcher: Matcher, event: MessageEvent, state: T_State) -> None:
    async with _locks[_conversation_queue_key(event)]:
        _mark_recent_bot_reply_thread(event)
        await matcher.finish(state["catty_repeat_message"])


@poke_matcher.handle()
async def handle_poke(bot: Bot, event: PokeNotifyEvent, state: T_State) -> None:
    message = Message(str(state["catty_poke_reply"]))
    if event.group_id is not None:
        async with _locks[f"group:{event.group_id}"]:
            _mark_recent_bot_reply_thread(event)
            await bot.send_group_msg(group_id=_coerce_group_id(str(event.group_id)), message=message)
    else:
        async with _locks[f"private:{event.user_id}"]:
            _mark_recent_bot_reply_thread(event)
            await bot.send_private_msg(user_id=int(event.user_id), message=message)


@observe_matcher.handle()
async def observe_memory(bot: Bot, event: MessageEvent) -> None:
    if str(event.user_id) == str(bot.self_id):
        return
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
    if not _has_api_key():
        return
    while True:
        await asyncio.sleep(60)
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


async def _proactive_bubble_loop() -> None:
    if not config.catty_proactive_enabled or not _has_api_key():
        return
    while True:
        await asyncio.sleep(max(config.catty_proactive_check_interval_seconds, 60.0))
        bots = list(get_bots().values())
        if not bots:
            continue
        for bot in bots:
            group_ids = await _candidate_group_ids(bot)
            due_group_ids = memory_store.due_proactive_group_ids(
                group_ids,
                max_daily=max(config.catty_proactive_max_daily_per_group, 0),
                min_interval_minutes=max(config.catty_proactive_min_interval_minutes, 1.0),
            )
            for group_id in due_group_ids:
                try:
                    await _send_proactive_bubble(bot, group_id)
                except OpenAICompatibleError as exc:
                    logger.warning(f"Proactive bubble API error for group {group_id}: {exc}")
                except httpx.HTTPError as exc:
                    logger.warning(f"Proactive bubble transport error for group {group_id}: {exc}")
                except Exception as exc:
                    logger.warning(f"Failed to send proactive bubble to group {group_id}: {exc}")
                await asyncio.sleep(2)


@get_driver().on_startup
async def start_memory_summary_loop() -> None:
    asyncio.create_task(_summary_loop())
    asyncio.create_task(_proactive_bubble_loop())
    asyncio.create_task(_local_critic_warmup_loop())


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

    async with _locks[queue_key]:
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

        star_resonance_context = build_star_resonance_context(incoming.text)

        image_description: str | None = None
        image_description_cached = False
        image_analysis: dict[str, object] = {}
        emoji_context = ""
        if incoming.has_image and config.catty_image_vision_enabled:
            image_description = memory_store.get_image_summary(incoming.image_keys)
            image_description_cached = bool(image_description)
            if not image_description:
                try:
                    image_analysis = await analyze_images_for_reply(config, incoming.image_urls, incoming.history_content)
                    tags = image_analysis.get("emotion_tags")
                    tag_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
                    image_description = (
                        f"{image_analysis.get('summary') or ''}\n"
                        f"兴趣程度：{image_analysis.get('interest', 0)}/100\n"
                        f"表情含义：{image_analysis.get('expression') or ''}\n"
                        f"情绪标签：{tag_text}"
                    ).strip()
                    if image_description:
                        memory_store.remember_image_record(incoming.image_keys, image_description)
                except OpenAICompatibleError as exc:
                    logger.warning(f"Image recognition failed, falling back to image URLs: {exc}")
                except httpx.HTTPError as exc:
                    logger.warning(f"Image recognition transport error, falling back to image URLs: {exc}")
            if not image_description and not image_description_cached:
                try:
                    image_description = await describe_images(config, incoming.image_urls, incoming.history_content)
                    if image_description:
                        memory_store.remember_image_record(incoming.image_keys, image_description)
                except OpenAICompatibleError as exc:
                    logger.warning(f"Image recognition failed, falling back to image URLs: {exc}")
                except httpx.HTTPError as exc:
                    logger.warning(f"Image recognition transport error, falling back to image URLs: {exc}")
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
        )
        try:
            reply = await chat_completion(config, messages)
        except OpenAICompatibleError as exc:
            logger.warning(f"OpenAI-compatible API error: {exc}")
            await matcher.finish(Message(f"AI 接口出错：{exc.public_message}"))
        except httpx.TimeoutException:
            logger.warning("OpenAI-compatible API request timed out")
            await matcher.finish(Message("AI 接口超时了，稍后再试一下。"))
        except httpx.HTTPError as exc:
            logger.warning(f"OpenAI-compatible API transport error: {exc}")
            await matcher.finish(Message("AI 接口连接失败了，检查一下 BASE_URL、网络或代理配置。"))

        reply = await _apply_local_critic(event, incoming, messages, reply)

        if _is_no_reply(reply):
            reply = _fallback_required_reply(incoming)

        reply, emoji_query = _extract_emoji_query(reply)
        _save_assistant_training_sample(event, incoming, messages, reply, emoji_query=emoji_query)
        emoji_entry = await _choose_or_download_emoji(emoji_query, incoming, image_analysis) if emoji_query else None
        if emoji_entry is None and not emoji_query and _should_auto_emoji_reply(incoming, reply):
            emoji_entry = _choose_auto_emoji(reply, incoming)
        chunks = _reply_chunks(reply)
        if image_description and not image_description_cached:
            memory_store.remember_image_summary(event, image_description)
        _append_history(history_key, incoming.history_content, "\n".join(chunks) if chunks else reply)
        _mark_recent_bot_reply_thread(event)
        if special_care_context and chunks:
            memory_store.record_special_care_reply_sent(event, "\n".join(chunks))

        delay_seconds = max(config.catty_reply_human_split_delay_seconds, 0.0)
        for chunk in chunks[:-1]:
            await matcher.send(Message(chunk))
            _mark_consumed_reply_source_if_sent(event, state)
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
        if emoji_entry:
            if chunks:
                await matcher.send(Message(chunks[-1]))
                _mark_consumed_reply_source_if_sent(event, state)
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
            else:
                _mark_consumed_reply_source_if_sent(event, state)
            await matcher.finish(Message(_emoji_segment(emoji_entry)))
        _mark_consumed_reply_source_if_sent(event, state)
        await matcher.finish(Message(chunks[-1] if chunks else "喵喵！猫猫现在很忙哦，等一下再来找人家～"))
