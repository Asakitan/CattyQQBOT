import asyncio
from collections import defaultdict
from dataclasses import dataclass
import time
from typing import DefaultDict

import httpx
from nonebot import get_driver, get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State

from .config import Config
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
from .memory import MemoryStore
from .openai_client import OpenAICompatibleError, chat_completion, describe_images, should_reply_to_group_message, should_request_reply_split


__plugin_meta__ = PluginMetadata(
    name="Catty QQ AI",
    description="OpenAI-compatible QQ chat plugin for NoneBot2 and OneBot v11.",
    usage="私聊直接发消息；群聊 @机器人 或发送 ai <内容>。",
    config=Config,
    supported_adapters={"~onebot.v11"},
)


config = get_plugin_config(Config)
memory_store = MemoryStore(config)

ChatMessage = dict[str, object]
_histories: DefaultDict[str, list[ChatMessage]] = defaultdict(list)
_locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass(slots=True)
class ExpressionRepeatState:
    signature: tuple[str, ...] | None = None
    count: int = 0
    last_seen: float = 0.0
    responded: bool = False


_expression_repeats: DefaultDict[str, ExpressionRepeatState] = defaultdict(ExpressionRepeatState)
REPLY_SPLIT_MARKER = "<<<CATTY_REPLY_SPLIT>>>"
NO_REPLY_MARKER = "<<<CATTY_NO_REPLY>>>"
TRAILING_CHAT_PUNCTUATION = " \t\r\n。！？!?；;，,、：:…."


def _has_api_key() -> bool:
    return bool(config.catty_openai_api_key.strip())


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


def _build_messages(
    event: MessageEvent,
    key: str,
    incoming: ExtractedMessage,
    *,
    image_description: str | None = None,
    semantic_reply_split: bool = False,
) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    if config.catty_system_prompt.strip():
        messages.append({"role": "system", "content": config.catty_system_prompt.strip()})
    if semantic_reply_split:
        messages.append({"role": "system", "content": _semantic_reply_split_prompt()})
    if incoming.opportunistic:
        messages.append({"role": "system", "content": _opportunistic_reply_prompt()})
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

    signature = expression_message_signature(event, include_images=config.catty_expression_repeat_include_images)
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
        "这是特别关注群活跃窗口内捕获的一条普通群聊消息，不是明确 @ 你、前缀命令或强指向请求。"
        "你需要先判断是否值得自然插话：只有能提供帮助、接住话题、纠正明显误解、或用户明显希望有人回应时才回复；"
        f"如果不该回复，只输出 {NO_REPLY_MARKER}，不要输出其他内容。"
    )


def _is_no_reply(reply: str) -> bool:
    return reply.strip().strip(TRAILING_CHAT_PUNCTUATION) == NO_REPLY_MARKER


async def _should_request_semantic_reply_split(incoming: ExtractedMessage) -> bool:
    if not config.catty_reply_human_split_enabled:
        return False
    if config.catty_reply_human_split_probability <= 0:
        return False
    min_chars = max(config.catty_reply_human_split_min_chars, 1)
    try:
        return await should_request_reply_split(config, incoming.history_content, min_chars=min_chars)
    except OpenAICompatibleError as exc:
        logger.warning(f"Reply split filter API error: {exc}")
    except httpx.HTTPError as exc:
        logger.warning(f"Reply split filter transport error: {exc}")
    return False


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
        try:
            should_reply = await should_reply_to_group_message(config, incoming.text, has_image=incoming.has_image)
        except OpenAICompatibleError as exc:
            logger.warning(f"Group message filter API error: {exc}")
            return False
        except httpx.HTTPError as exc:
            logger.warning(f"Group message filter transport error: {exc}")
            return False
        if not should_reply:
            return False
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
async def handle_expression_repeat(matcher: Matcher, state: T_State) -> None:
    await matcher.finish(state["catty_repeat_message"])


@observe_matcher.handle()
async def observe_memory(event: MessageEvent) -> None:
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


@get_driver().on_startup
async def start_memory_summary_loop() -> None:
    asyncio.create_task(_summary_loop())


@chat_matcher.handle()
async def handle_chat(matcher: Matcher, event: MessageEvent, state: T_State) -> None:
    incoming: ExtractedMessage = state["catty_incoming"]
    history_key = build_history_key(event, config)
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

    if not _has_api_key():
        await matcher.finish(Message("还没有配置 API Key，先在 config.json 里填好 ai.api_key 再来找我。"))

    async with _locks[history_key]:
        image_description: str | None = None
        image_description_cached = False
        if incoming.has_image and config.catty_image_vision_enabled:
            image_description = memory_store.get_image_summary(incoming.image_keys)
            image_description_cached = bool(image_description)
            if not image_description:
                try:
                    image_description = await describe_images(config, incoming.image_urls, incoming.history_content)
                    if image_description:
                        memory_store.remember_image_record(incoming.image_keys, image_description)
                except OpenAICompatibleError as exc:
                    logger.warning(f"Image recognition failed, falling back to image URLs: {exc}")
                except httpx.HTTPError as exc:
                    logger.warning(f"Image recognition transport error, falling back to image URLs: {exc}")
        semantic_reply_split = await _should_request_semantic_reply_split(incoming)
        messages = _build_messages(
            event,
            history_key,
            incoming,
            image_description=image_description,
            semantic_reply_split=semantic_reply_split,
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

        if incoming.opportunistic and _is_no_reply(reply):
            await matcher.finish()

        chunks = _reply_chunks(reply)
        if image_description and not image_description_cached:
            memory_store.remember_image_summary(event, image_description)
        _append_history(history_key, incoming.history_content, "\n".join(chunks) if chunks else reply)

    delay_seconds = max(config.catty_reply_human_split_delay_seconds, 0.0)
    for chunk in chunks[:-1]:
        await matcher.send(Message(chunk))
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
    await matcher.finish(Message(chunks[-1] if chunks else "AI 没有返回内容。"))
