import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
import random
import time
from typing import DefaultDict

import httpx
from nonebot import get_bots, get_driver, get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
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
)
from .persona_prompts import build_catgirl_examples_prompt, build_reply_self_check_prompt
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


def _has_api_key() -> bool:
    return bool(config.catty_openai_api_key.strip())


def _conversation_queue_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group:{event.group_id}"
    return f"private:{event.user_id}"


def _soft_directed(incoming: ExtractedMessage) -> bool:
    return incoming.directed and not incoming.mentioned and not incoming.replied_to_self and not incoming.used_prefix


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
    if config.catty_system_prompt.strip():
        messages.append({"role": "system", "content": config.catty_system_prompt.strip()})
    if config.catty_reply_self_check_enabled:
        messages.append(
            {
                "role": "system",
                "content": build_reply_self_check_prompt(NO_REPLY_MARKER, REPLY_SPLIT_MARKER),
            }
        )
    if config.catty_reply_style_examples_enabled:
        messages.append({"role": "system", "content": build_catgirl_examples_prompt(NO_REPLY_MARKER)})
    if semantic_reply_split:
        messages.append({"role": "system", "content": _semantic_reply_split_prompt()})
    if incoming.opportunistic or group_filter_context:
        messages.append({"role": "system", "content": _opportunistic_reply_prompt()})
    if _soft_directed(incoming):
        messages.append({"role": "system", "content": _soft_directed_reply_prompt()})
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
        "这是长期群聊观察窗口内捕获的普通群聊消息，不是明确 @ 你、前缀命令或强指向请求。"
        "你需要先判断是否值得自然插话：只有能提供帮助、接住话题、纠正明显误解、或用户明显希望有人回应时才回复；"
        f"如果不该回复，只输出 {NO_REPLY_MARKER}，不要输出其他内容。"
    )


def _soft_directed_reply_prompt() -> str:
    return (
        "本轮没有明确 @ 你、回复你或使用开头前缀，只是句子中出现了你的名字、指向词或功能词。"
        "不要根据关键词机械回应；请根据整句主语、称呼对象、上下文意图判断用户是不是在呼唤你或要求你办事。"
        "如果用户是在对你发问、让你帮忙、要求搜索/海龟汤/星痕共鸣相关回答，就自然回应；"
        "如果只是第一人称/第三人称提到名字、讨论名字本身、或没有期待你接话，只输出 "
        f"{NO_REPLY_MARKER}。不要机械回复“你叫我了/我在”。"
    )


def _is_no_reply(reply: str) -> bool:
    return reply.strip().strip(TRAILING_CHAT_PUNCTUATION) == NO_REPLY_MARKER


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
    return chunks


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
    if incoming.needs_filter:
        if not isinstance(event, GroupMessageEvent):
            return False
        batch = await _take_due_group_filter_batch(event, incoming)
        if batch is None:
            return False
        state["catty_group_filter_context"] = _group_filter_reply_context(batch)
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


@expression_repeat_matcher.handle()
async def handle_expression_repeat(matcher: Matcher, event: MessageEvent, state: T_State) -> None:
    async with _locks[_conversation_queue_key(event)]:
        await matcher.finish(state["catty_repeat_message"])


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


@chat_matcher.handle()
async def handle_chat(matcher: Matcher, event: MessageEvent, state: T_State) -> None:
    incoming: ExtractedMessage = state["catty_incoming"]
    group_filter_context = str(state.get("catty_group_filter_context") or "")
    special_care_context = str(state.get("catty_special_care_context") or "")
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
            web_search_context=web_search_context,
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

        if (
            incoming.opportunistic
            or group_filter_context
            or special_care_context
            or _soft_directed(incoming)
            or anger_context
        ) and _is_no_reply(reply):
            await matcher.finish()

        reply, emoji_query = _extract_emoji_query(reply)
        emoji_entry = await _choose_or_download_emoji(emoji_query, incoming, image_analysis) if emoji_query else None
        if emoji_entry is None and not emoji_query and _should_auto_emoji_reply(incoming, reply):
            emoji_entry = _choose_auto_emoji(reply, incoming)
        chunks = _reply_chunks(reply)
        if image_description and not image_description_cached:
            memory_store.remember_image_summary(event, image_description)
        _append_history(history_key, incoming.history_content, "\n".join(chunks) if chunks else reply)
        if special_care_context and chunks:
            memory_store.record_special_care_reply_sent(event, "\n".join(chunks))

        delay_seconds = max(config.catty_reply_human_split_delay_seconds, 0.0)
        for chunk in chunks[:-1]:
            await matcher.send(Message(chunk))
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
        if emoji_entry:
            if chunks:
                await matcher.send(Message(chunks[-1]))
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
            await matcher.finish(Message(_emoji_segment(emoji_entry)))
        await matcher.finish(Message(chunks[-1] if chunks else "AI 没有返回内容。"))
