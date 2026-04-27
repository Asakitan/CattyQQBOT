import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .config import Config
from .features import FEATURE_DIRECT_KEYWORDS


EXPRESSION_SEGMENT_TYPES = {"face", "mface", "image"}
GENERIC_DIRECTED_MARKERS = {
    "你",
    "妳",
    "您",
    "看看",
    "帮我看看",
    "这张图",
    "这个图",
    "图片",
    "图里",
    "评价一下",
    "怎么回事",
}
DIRECT_ADDRESS_LEADS = ("问", "找", "喊", "求", "请", "麻烦", "艾特", "@")
DIRECT_ADDRESS_TRAILS = (
    "你",
    "妳",
    "您",
    "帮",
    "看",
    "瞅",
    "查",
    "搜",
    "来",
    "给",
    "说",
    "讲",
    "想",
    "评",
    "能",
    "可以",
    "要",
    "在",
    "出来",
    "救",
    "教",
    "今天",
    "这个",
    "这张",
    "怎么",
    "为啥",
    "为什么",
)


@dataclass(slots=True)
class ExtractedMessage:
    text: str
    history_content: str
    mentioned: bool
    replied_to_self: bool
    used_prefix: bool
    image_urls: list[str]
    image_keys: list[str]
    has_image: bool
    directed: bool
    directed_strength: str
    directly_requested: bool
    needs_filter: bool
    opportunistic: bool = False


_CONTROL_CODE_PATTERN = re.compile(r"\[(?:CQ:)?(?:at|reply)[:,][^\]]*\]", re.IGNORECASE)
_CONTROL_TAG_PATTERN = re.compile(r"\[(?:CQ:)?(?P<type>at|reply)[:,](?P<data>[^\]]*)\]", re.IGNORECASE)


def _message_text_segments(event: MessageEvent) -> list[str]:
    return [str(segment.data.get("text", "")) for segment in event.message if segment.type == "text"]


def _raw_message_text(event: MessageEvent) -> str:
    return str(getattr(event, "raw_message", "") or "")


def _control_text_sources(event: MessageEvent) -> list[str]:
    sources = _message_text_segments(event)
    raw_message = _raw_message_text(event)
    if raw_message:
        sources.append(raw_message)
    return sources


def _control_code_values(text: str, code_type: str, key: str) -> list[str]:
    values: list[str] = []
    for match in _CONTROL_TAG_PATTERN.finditer(text):
        if match.group("type").lower() != code_type:
            continue
        for part in match.group("data").split(","):
            name, sep, value = part.partition("=")
            if sep and name.strip().lower() == key:
                value = value.strip()
                if value:
                    values.append(value)
    return values


def _strip_control_codes(text: str) -> str:
    return _CONTROL_CODE_PATTERN.sub("", text).strip()


def _plain_text(event: MessageEvent) -> str:
    text = "".join(_message_text_segments(event))
    if not text.strip():
        text = _raw_message_text(event)
    return _strip_control_codes(text)


def event_plain_text(event: MessageEvent) -> str:
    return _plain_text(event)


def extract_image_urls(event: MessageEvent) -> list[str]:
    return [url for url, _key in extract_images(event)]


def _image_cache_key(segment_type: str, data: dict[str, Any], url: str) -> str:
    if segment_type == "mface":
        parts = [
            data.get("package_id"),
            data.get("emoji_id"),
            data.get("key"),
            data.get("summary"),
            data.get("file"),
        ]
    else:
        parts = [
            data.get("file_id"),
            data.get("file"),
            data.get("md5"),
            data.get("sha1"),
            data.get("id"),
            data.get("summary"),
        ]
    values = [str(part).strip() for part in parts if str(part or "").strip()]
    if values:
        return f"{segment_type}:" + "|".join(values)
    return f"{segment_type}:url:{url}"


def extract_images(event: MessageEvent) -> list[tuple[str, str]]:
    images: list[tuple[str, str]] = []
    for segment in event.message:
        if segment.type not in {"image", "mface"}:
            continue
        url = str(segment.data.get("url") or "").strip()
        if not url:
            continue
        images.append((url, _image_cache_key(segment.type, segment.data, url)))
    return images


def _normalize_repeat_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def expression_message_signature(
    event: MessageEvent,
    *,
    include_images: bool = True,
    include_text: bool = True,
) -> tuple[str, ...] | None:
    signature: list[str] = []
    for segment in event.message:
        if segment.type == "text":
            text = _normalize_repeat_text(str(segment.data.get("text", "")))
            if not text:
                continue
            if not include_text:
                return None
            signature.append(f"text:{text}")
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
                data.get("file"),
            ]
            value = "|".join(part for part in parts if part) or data.get("url")
        else:
            parts = [
                data.get("file_id"),
                data.get("file"),
                data.get("md5"),
                data.get("sha1"),
                data.get("id"),
                data.get("summary"),
            ]
            value = "|".join(part for part in parts if part) or data.get("url")

        if not value:
            return None
        signature.append(f"{segment.type}:{value}")

    return tuple(signature) if signature else None


def _mentioned_self(self_id: str, event: MessageEvent) -> bool:
    target_ids = {str(self_id), "all"}
    for segment in event.message:
        if segment.type != "at":
            continue
        target = str(segment.data.get("qq", "")).strip()
        if target in target_ids:
            return True
    for text in _control_text_sources(event):
        for target in _control_code_values(text, "at", "qq"):
            if target in target_ids:
                return True
    return False


def reply_message_ids(event: MessageEvent) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        message_id = str(value or "").strip()
        if message_id and message_id not in seen:
            seen.add(message_id)
            ids.append(message_id)

    for segment in event.message:
        if segment.type == "reply":
            add(segment.data.get("id") or segment.data.get("message_id"))
    for text in _control_text_sources(event):
        for message_id in _control_code_values(text, "reply", "id"):
            add(message_id)
    return ids


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


def _strip_textual_mention(text: str, aliases: list[str]) -> tuple[str, bool]:
    stripped = text.strip()
    if not stripped.startswith("@"):
        return stripped, False

    rest = stripped[1:].lstrip()
    for alias in sorted(aliases, key=len, reverse=True):
        alias = alias.strip().lstrip("@")
        if not alias:
            continue
        if rest == alias:
            return "", True
        if rest.startswith(alias):
            after_alias = rest[len(alias) :]
            if not after_alias or re.match(r"^[\s:：,，;；-]+", after_alias):
                return after_alias.strip(" \t\r\n:：,，;；-"), True
    return stripped, False


def _configured_direct_markers(config: Config) -> list[str]:
    markers: list[str] = []
    for raw_marker in [*config.catty_trigger_prefixes, *config.catty_directed_keywords, *FEATURE_DIRECT_KEYWORDS]:
        marker = raw_marker.strip().lower().lstrip("@")
        if marker and marker not in markers:
            markers.append(marker)
    return sorted(markers, key=len, reverse=True)


def _configured_direct_address_markers(config: Config) -> list[str]:
    trigger_markers = {
        raw_marker.strip().lower().lstrip("@")
        for raw_marker in config.catty_trigger_prefixes
        if raw_marker.strip().lstrip("@")
    }
    markers: list[str] = []
    for raw_marker in [*config.catty_trigger_prefixes, *config.catty_directed_keywords]:
        marker = raw_marker.strip().lower().lstrip("@")
        if not marker:
            continue
        if marker not in trigger_markers and marker in GENERIC_DIRECTED_MARKERS:
            continue
        if marker not in trigger_markers and len(marker) < 2:
            continue
        if marker not in markers:
            markers.append(marker)
    return sorted(markers, key=len, reverse=True)


def _looks_like_direct_address(text: str, config: Config) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    compact = re.sub(r"[\s:：,，!！?？~～。、“”\"'‘’、]+", "", normalized)
    for marker in _configured_direct_address_markers(config):
        if normalized.startswith(marker):
            after_marker = normalized[len(marker) :]
            if not after_marker:
                return True
            if re.match(r"^[\s:：,，!！?？~～。、“”\"'‘’、]+", after_marker):
                return True
            compact_after = re.sub(r"\s+", "", after_marker)
            if compact_after.startswith(DIRECT_ADDRESS_TRAILS):
                return True
        if compact == marker:
            return True
        if any(f"{lead}{marker}" in compact for lead in DIRECT_ADDRESS_LEADS):
            return True
    return False


def _directed_keyword_strength(text: str, config: Config) -> str:
    if not _has_directed_keyword(text, config):
        return "none"
    if _looks_like_direct_address(text, config):
        return "direct_address"
    return "keyword"


def _has_directed_keyword(text: str, config: Config) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    for keyword in _configured_direct_markers(config):
        if keyword in normalized:
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


def _is_special_care_user(event: MessageEvent, config: Config) -> bool:
    user_id = int(event.user_id)
    if user_id in getattr(config, "catty_special_care_user_ids", set()):
        return True
    if isinstance(event, GroupMessageEvent):
        group_map = getattr(config, "catty_group_special_care_user_ids", {})
        return user_id in group_map.get(str(event.group_id), set())
    return False


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


def extract_incoming_message(self_id: str, event: MessageEvent, config: Config, *, replied_to_self: bool = False) -> ExtractedMessage | None:
    if not _allowed_by_config(event, config):
        return None

    text = _plain_text(event)
    images = extract_images(event)
    image_urls = [url for url, _key in images]
    image_keys = [key for _url, key in images]
    has_image = bool(image_urls)
    text_without_mention, textual_mention = _strip_textual_mention(text, config.catty_trigger_prefixes)
    mentioned = _mentioned_self(self_id, event) or textual_mention
    if not text and not has_image and not mentioned and not replied_to_self:
        return None

    text_without_prefix, used_prefix = _strip_prefix(text_without_mention, config.catty_trigger_prefixes)
    directed_text = text_without_prefix or text
    directed_strength = _directed_keyword_strength(directed_text, config)
    directed = directed_strength != "none"

    directly_requested = True
    needs_filter = False
    if isinstance(event, GroupMessageEvent):
        special_active = _is_special_care_user(event, config)
        image_directed = config.catty_image_response_enabled and has_image and directed
        directly_requested = mentioned or replied_to_self or used_prefix or directed or image_directed
        if not directly_requested and not special_active:
            if not config.catty_filter_enabled:
                if config.catty_group_require_mention_or_prefix:
                    return None
            else:
                needs_filter = True
        opportunistic = special_active and not directly_requested
    else:
        opportunistic = False
    if isinstance(event, PrivateMessageEvent) and config.catty_private_require_prefix and not used_prefix and not has_image:
        return None

    final_text = text_without_prefix.strip()
    if not final_text and has_image:
        final_text = "请看这张图片并自然回应。"
    if not final_text and replied_to_self:
        final_text = "群友回复了你的消息但没有附加文字，请自然开口回应。"
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
        replied_to_self=replied_to_self,
        used_prefix=used_prefix,
        image_urls=image_urls,
        image_keys=image_keys,
        has_image=has_image,
        directed=directed,
        directed_strength=directed_strength,
        directly_requested=directly_requested,
        needs_filter=needs_filter,
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
