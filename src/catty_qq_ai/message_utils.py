import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .config import Config


EXPRESSION_SEGMENT_TYPES = {"face", "mface", "image"}


@dataclass(slots=True)
class ExtractedMessage:
    text: str
    history_content: str
    mentioned: bool
    used_prefix: bool
    image_urls: list[str]
    has_image: bool
    directed: bool
    opportunistic: bool = False


def _plain_text(event: MessageEvent) -> str:
    parts: list[str] = []
    for segment in event.message:
        if segment.type == "text":
            parts.append(str(segment.data.get("text", "")))
    return "".join(parts).strip()


def event_plain_text(event: MessageEvent) -> str:
    return _plain_text(event)


def extract_image_urls(event: MessageEvent) -> list[str]:
    urls: list[str] = []
    for segment in event.message:
        if segment.type != "image":
            continue
        url = str(segment.data.get("url") or "").strip()
        if url:
            urls.append(url)
    return urls


def expression_message_signature(event: MessageEvent, *, include_images: bool = True) -> tuple[str, ...] | None:
    signature: list[str] = []
    for segment in event.message:
        if segment.type == "text" and not str(segment.data.get("text", "")).strip():
            continue
        if segment.type == "image" and not include_images:
            return None
        if segment.type not in EXPRESSION_SEGMENT_TYPES:
            return None

        data: dict[str, str] = {}
        for key, value in segment.data.items():
            if value is None:
                continue
            text_value = str(value)
            if text_value:
                data[str(key)] = text_value
        if segment.type == "face":
            value = data.get("id")
        elif segment.type == "mface":
            parts = [
                data.get("package_id"),
                data.get("emoji_id"),
                data.get("key"),
                data.get("summary"),
                data.get("url"),
                data.get("file"),
            ]
            value = "|".join(part for part in parts if part)
        else:
            value = data.get("file") or data.get("summary") or data.get("url")

        if not value:
            return None
        signature.append(f"{segment.type}:{value}")

    return tuple(signature) if signature else None


def _mentioned_self(self_id: str, event: MessageEvent) -> bool:
    for segment in event.message:
        if segment.type != "at":
            continue
        target = str(segment.data.get("qq", ""))
        if target in {self_id, "all"}:
            return True
    return False


def _strip_prefix(text: str, prefixes: list[str]) -> tuple[str, bool]:
    stripped = text.strip()
    for prefix in sorted(prefixes, key=len, reverse=True):
        prefix = prefix.strip()
        if not prefix:
            continue
        if stripped == prefix:
            return "", True
        if stripped.startswith(prefix):
            rest = stripped[len(prefix) :]
            if not rest or re.match(r"^[\s:：,，;；-]+", rest):
                return rest.strip(" \t\r\n:：,，;；-"), True
    return stripped, False


def _has_directed_keyword(text: str, config: Config) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    for keyword in config.catty_directed_keywords:
        keyword = keyword.strip().lower()
        if keyword and keyword in normalized:
            return True
    return False


def _is_special_group(event: MessageEvent, config: Config) -> bool:
    return isinstance(event, GroupMessageEvent) and int(event.group_id) in config.catty_memory_special_group_ids


def _special_group_in_active_window(event: MessageEvent, config: Config) -> bool:
    if not _is_special_group(event, config) or not config.catty_special_group_active_window_enabled:
        return False
    active_minutes = max(min(config.catty_special_group_active_minutes_per_hour, 60), 0)
    if active_minutes <= 0:
        return False
    if active_minutes >= 60:
        return True
    now = datetime.now()
    seed = f"{event.group_id}:{now:%Y%m%d%H}:{active_minutes}"
    active_slots = set(random.Random(seed).sample(range(60), active_minutes))
    return now.minute in active_slots


def _allowed_by_config(event: MessageEvent, config: Config) -> bool:
    if config.catty_allowed_user_ids and int(event.user_id) not in config.catty_allowed_user_ids:
        return False
    if isinstance(event, GroupMessageEvent):
        if not config.catty_enable_group:
            return False
        if config.catty_allowed_group_ids and int(event.group_id) not in config.catty_allowed_group_ids:
            return False
    elif isinstance(event, PrivateMessageEvent):
        if not config.catty_enable_private:
            return False
    else:
        return False
    return True


def _sender_name(event: MessageEvent) -> str:
    sender: Any = getattr(event, "sender", None)
    for attr in ("card", "nickname"):
        value = getattr(sender, attr, "") if sender is not None else ""
        if value:
            return str(value)
    return str(event.user_id)


def extract_incoming_message(self_id: str, event: MessageEvent, config: Config) -> ExtractedMessage | None:
    if not _allowed_by_config(event, config):
        return None

    text = _plain_text(event)
    image_urls = extract_image_urls(event)
    has_image = bool(image_urls)
    mentioned = _mentioned_self(self_id, event)
    if not text and not has_image and not mentioned:
        return None

    text_without_prefix, used_prefix = _strip_prefix(text, config.catty_trigger_prefixes)
    directed = _has_directed_keyword(text_without_prefix or text, config)

    if isinstance(event, GroupMessageEvent) and config.catty_group_require_mention_or_prefix:
        special_active = _special_group_in_active_window(event, config)
        image_directed = config.catty_image_response_enabled and has_image and directed
        directly_requested = mentioned or used_prefix or directed or image_directed
        if not directly_requested and not special_active:
            return None
        opportunistic = special_active and not directly_requested
    else:
        opportunistic = False
    if isinstance(event, PrivateMessageEvent) and config.catty_private_require_prefix and not used_prefix and not has_image:
        return None

    final_text = text_without_prefix.strip()
    if not final_text and has_image:
        final_text = "请看这张图片并自然回应。"
    if not final_text and mentioned:
        final_text = "群友 @ 了你但没有附加文字，请自然开口回应。"
    if not final_text:
        return None

    history_content = final_text
    if has_image:
        history_content = f"{history_content}\n[图片数量: {len(image_urls)}]"
    if isinstance(event, GroupMessageEvent):
        history_content = f"{_sender_name(event)}({event.user_id}): {final_text}"
        if has_image:
            history_content = f"{history_content}\n[图片数量: {len(image_urls)}]"

    return ExtractedMessage(
        text=final_text,
        history_content=history_content,
        mentioned=mentioned,
        used_prefix=used_prefix,
        image_urls=image_urls,
        has_image=has_image,
        directed=directed,
        opportunistic=opportunistic,
    )


def build_history_key(event: MessageEvent, config: Config) -> str:
    if isinstance(event, GroupMessageEvent):
        if config.catty_group_history_scope == "user":
            return f"group:{event.group_id}:user:{event.user_id}"
        return f"group:{event.group_id}"
    return f"private:{event.user_id}"


def split_reply(text: str, max_chars: int) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    if max_chars <= 0 or len(clean) <= max_chars:
        return [clean]

    chunks: list[str] = []
    remaining = clean
    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n", 0, max_chars)
        if split_at < max_chars // 2:
            split_at = remaining.rfind("。", 0, max_chars)
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks
