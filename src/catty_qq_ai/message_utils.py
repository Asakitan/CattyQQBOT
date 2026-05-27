import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .config import Config
from .features import FEATURE_DIRECT_KEYWORDS


_logger = logging.getLogger("catty_qq_ai")

# 第一次命中机器人标记字段时 dump sender 一次方便主人调整精确化规则
_marked_bot_dump_done = False

# QQ 协议/NapCat sender 字段里常见的机器人标记。多字段试探,覆盖不同实现。
_BOT_FLAG_BOOL_ATTRS = ("is_bot", "is_robot", "robot", "official")
_BOT_FLAG_STRING_ATTRS = ("role", "category", "sub_type", "type", "user_type", "account_type")
_BOT_FLAG_STRING_VALUES = {"bot", "robot", "official_bot", "qqbot", "officialrobot", "official_robot"}

# 启发式机器人自介模板:大概率是另一个机器人在自介,普通用户不会这么说
# 真人 @ 笨猫的消息(mentioned=True)会跳过这个匹配——
# 真人会把"Q 群管家""签到助手"等名字当**话题对象**说("画一张白丝Q群管家""@猫猫 帮我配置群管家"),
# 老的全文 substring 匹配把这种合法消息也拦了。所以这里只用强自介信号:开头是"我是 / 这里是 / 在下"
# 后面再跟管家/小助手等名词。
_BOT_INTRO_PATTERNS = (
    re.compile(r"暂时还?不能(?:和|跟|与)你?(?:对话|交流|聊天|回复)"),
    re.compile(r"我是.{0,12}(?:机器人|小助手|助手|管家|bot|BOT|AI|ai)(?:[，,。！!？?～~\s]|$)"),
    # 严格自介:行首/开头 1-6 字 → 名字 → 句尾标点 or 短句结尾。避免把"画一张白丝Q群管家"这种话题词
    # 错杀。例:✅ "Q群管家上线啦"  ✅ "群管家来啦"  ❌ "画一张Q群管家"  ❌ "@猫猫 群管家咋用"
    re.compile(
        r"(?:^|[\s。！!？?])(?:Q群管家|qq群管家|群管家|签到小助手|签到助手)"
        r"(?:[在为是已上来]|[，,。！!？?～~\s]|上线|来啦|启动|为您|为你|开始|已就绪)",
        re.IGNORECASE,
    ),
    # 助手/管家类机器人 + 强推送动词(提醒您/温馨提示/通知您/推送)无须行首,
    # 这种组合在正常用户对话里几乎不会出现,bot 推送(『Q群管家签到小助手提醒您...』
    # 『签到助手温馨提示...』)却高度共现。
    re.compile(
        r"(?:Q群管家|qq群管家|群管家|签到小助手|签到助手|小助手|管家|机器人|bot)"
        r"(?:[\s,，]*)?(?:提醒您|提醒你|温馨提示|温馨提醒|通知您|通知你|推送|播报)",
        re.IGNORECASE,
    ),
)


def _looks_like_marked_bot(event: MessageEvent) -> bool:
    """QQ 协议层标记的机器人:检查 sender 上多种可能的字段。

    优先 OneBot/NapCat 暴露的字段(is_bot/role/category/user_type 等)。
    第一次命中时 log 一行 dump sender,方便主人确认是哪个字段触发的。
    """
    sender = getattr(event, "sender", None)
    if sender is None:
        return False

    candidates: list[Any] = [sender]
    raw_sender = None
    raw_event = getattr(event, "raw_event", None) or getattr(event, "__raw_event__", None)
    if isinstance(raw_event, dict):
        rs = raw_event.get("sender")
        if isinstance(rs, dict):
            raw_sender = rs
            candidates.append(rs)

    for source in candidates:
        if source is None:
            continue
        getter = source.get if isinstance(source, dict) else lambda name, default=None, _s=source: getattr(_s, name, default)
        for attr in _BOT_FLAG_BOOL_ATTRS:
            try:
                if bool(getter(attr, False)):
                    _maybe_dump_marked_bot(event, sender, raw_sender, hit=f"{attr}=truthy")
                    return True
            except Exception:  # noqa: BLE001
                continue
        for attr in _BOT_FLAG_STRING_ATTRS:
            try:
                value = str(getter(attr, "") or "").strip().lower()
            except Exception:  # noqa: BLE001
                continue
            if value and value in _BOT_FLAG_STRING_VALUES:
                _maybe_dump_marked_bot(event, sender, raw_sender, hit=f"{attr}={value}")
                return True
    return False


def _maybe_dump_marked_bot(event: MessageEvent, sender: Any, raw_sender: Any, *, hit: str) -> None:
    global _marked_bot_dump_done
    if _marked_bot_dump_done:
        return
    _marked_bot_dump_done = True
    try:
        sender_dump = sender.dict() if hasattr(sender, "dict") else dict(getattr(sender, "__dict__", {}))
    except Exception:  # noqa: BLE001
        sender_dump = str(sender)
    _logger.info(
        "marked-bot filter fired: hit=%s user_id=%s sender_dump=%s raw_sender=%s",
        hit,
        getattr(event, "user_id", "?"),
        sender_dump,
        raw_sender,
    )


def _looks_like_bot_self_intro(text: str) -> bool:
    if not text:
        return False
    body = text.strip()
    if not body:
        return False
    for pattern in _BOT_INTRO_PATTERNS:
        if pattern.search(body):
            return True
    return False


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


def _render_at_segment(seg: Any) -> str:
    """把 at segment 渲染成可见 @文本,保留 AI 能看懂的 nickname/qq 信息。

    NapCat 的 at segment 通常带 data={'qq': '12345', 'name': '昵称'(可选)},
    历史 _plain_text 只 join type=text 直接丢了 at,导致用户『画一个符合 @某某 的画像』
    猫猫看到的是『画一个符合  的画像』(name 段空白),完全不知道画谁。
    """
    data = getattr(seg, "data", None) or {}
    name = str(data.get("name") or "").strip()
    qq = str(data.get("qq") or "").strip()
    if name and qq:
        return f"@{name}(QQ:{qq})"
    if name:
        return f"@{name}"
    if qq:
        return f"@QQ_{qq}"
    return ""


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
    # 按 segment 顺序拼:text 原文 + at -> "@<nickname>(QQ:xxx)"。
    # 这样『画一个符合 @某某 的画像』猫猫看到的是『画一个符合 @某某(QQ:12345) 的画像』,
    # AI 能拿到画谁的明确信息。其他控制码(reply/face/image 等)由后续处理或 strip。
    parts: list[str] = []
    self_id_to_skip: str | None = None  # 不去掉 @ 笨猫自己的标记,让 AI 知道被 @
    for seg in getattr(event, "message", []) or []:
        seg_type = getattr(seg, "type", "")
        if seg_type == "text":
            parts.append(str((getattr(seg, "data", None) or {}).get("text", "")))
        elif seg_type == "at":
            rendered = _render_at_segment(seg)
            if rendered:
                parts.append(rendered)
    text = "".join(parts).strip()
    if not text:
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


def mentions_other_user(self_id: str, event: MessageEvent) -> bool:
    """是否在同一条消息里 @ 了猫猫之外的其他用户（用于判断这条消息其实是给别人看的）。"""
    self_str = str(self_id)
    for segment in event.message:
        if segment.type != "at":
            continue
        target = str(segment.data.get("qq", "")).strip()
        if not target or target == "all":
            continue
        if target != self_str:
            return True
    for text in _control_text_sources(event):
        for target in _control_code_values(text, "at", "qq"):
            target = str(target or "").strip()
            if target and target != "all" and target != self_str:
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


# 这些关键词太泛，单独子串命中很容易误判（"我跟他说你坏话"、"搜集邮票"、"看看那个"…）。
# 命中后必须再额外满足"看起来是在喊人/请猫猫做事"的语境才算 directed。
# 分两组：
#   动词类（看看/搜/查 等动作请求）—— 出现在句首/紧跟请求引导词等位置算指向
#   名词类（图片/图里/你 等称呼或对象词）—— 还需要配合请求引导词或问句尾，单独句首不算
_WEAK_GENERIC_VERB_MARKERS = {
    "搜", "查", "看看", "看一下", "看一眼",
}
_WEAK_GENERIC_NOUN_MARKERS = {
    "你", "妳", "您",
    "图片", "图里", "这张图", "这个图", "评价一下", "怎么回事",
}
_WEAK_GENERIC_MARKERS = _WEAK_GENERIC_VERB_MARKERS | _WEAK_GENERIC_NOUN_MARKERS

# 在这些位置出现弱关键词，才视为真的在指向猫猫：
# - 句首/独立短句开头（仅动词类）
# - 明显的请求/称呼前导词后面（请/帮/麻烦/喊/找/问/求/给…）
# - 紧跟问号/语气助词形成"对你说话"的句尾（你呢/你吧/你呀/你嘛）
_ADDRESSING_LEAD_CHARS = "请帮麻烦喊找问求给替让叫艾"
_ADDRESSING_TRAIL_CHARS = "呢吧呀嘛啊么吗"
_ADDRESSING_BOUNDARY_CHARS = " \t\r\n,，。.;；!！?？:：、~～\"'“”‘’()（）[]【】「」"


def _weak_marker_in_addressing_context(normalized: str, marker: str, *, allow_sentence_start: bool) -> bool:
    # 在原文里逐个出现位置判断，命中一次就行
    start = 0
    while True:
        idx = normalized.find(marker, start)
        if idx < 0:
            return False
        end = idx + len(marker)
        prev_char = normalized[idx - 1] if idx > 0 else ""
        next_char = normalized[end] if end < len(normalized) else ""
        is_at_sentence_start = (not prev_char) or (prev_char in _ADDRESSING_BOUNDARY_CHARS)

        # 0) 单字称呼词出现在消息最最开头，几乎必然是在喊人（"你好啊"/"您慢走"）
        if idx == 0 and len(marker) <= 1 and marker in {"你", "妳", "您"}:
            return True
        # 1) 句首/紧跟标点（仅动词类弱标记可凭此触发，名词类不行——"图片素材网站"不该算）
        if allow_sentence_start and is_at_sentence_start:
            return True
        # 2) 前面是明显的请求/称呼引导字
        if prev_char and prev_char in _ADDRESSING_LEAD_CHARS:
            return True
        # 3) 后面紧跟问号/语气助词，像在喊人
        if next_char and (next_char in _ADDRESSING_TRAIL_CHARS or next_char in "?？!！"):
            return True
        # 4) 整段就是这一个词
        if normalized.strip() == marker:
            return True

        start = end
        if start >= len(normalized):
            return False


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]")


def _ascii_marker_matches(normalized: str, marker: str) -> bool:
    """对 ASCII 关键词使用单词边界匹配，避免 ai 命中 'wait'/'aigc'。"""
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(marker)}(?![A-Za-z0-9_])", re.IGNORECASE)
    return pattern.search(normalized) is not None


def _has_directed_keyword(text: str, config: Config) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    for keyword in _configured_direct_markers(config):
        # ASCII 关键词需要单词边界，避免在 "wait"/"aigc"/"saint" 里误命中
        if keyword.isascii() and _ASCII_WORD_RE.search(keyword):
            if not _ascii_marker_matches(normalized, keyword):
                continue
        elif keyword not in normalized:
            continue
        # 单字 / 弱泛化关键词需要更强的指向上下文，避免在"他对你说"、"搜集"、"看看那个"中误判
        if len(keyword) <= 1 or keyword in _WEAK_GENERIC_MARKERS:
            allow_start = keyword in _WEAK_GENERIC_VERB_MARKERS or (
                len(keyword) <= 1 and keyword not in _WEAK_GENERIC_NOUN_MARKERS
            )
            if _weak_marker_in_addressing_context(normalized, keyword, allow_sentence_start=allow_start):
                return True
            continue
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
    user_id = int(event.user_id)
    # 硬黑名单(主人手动把已知机器人 QQ 写进来)
    if getattr(config, "catty_ignored_user_ids", set()) and user_id in config.catty_ignored_user_ids:
        return False
    # QQ 协议层标记的机器人(NapCat/OneBot sender 字段)
    if bool(getattr(config, "catty_ignore_marked_bots", True)) and _looks_like_marked_bot(event):
        return False
    if config.catty_allowed_user_ids and user_id not in config.catty_allowed_user_ids:
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
    # 启发式 bot-self-intro 拦截:即使 QQ 协议没标记,文本明显是机器人自介模板就静默忽略
    if bool(getattr(config, "catty_ignore_bot_self_intro_enabled", True)) and _looks_like_bot_self_intro(text):
        _logger.info(
            "bot-loop guard: dropped self-intro message from user=%s text=%r",
            getattr(event, "user_id", "?"),
            (text or "")[:120],
        )
        return None

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
            # 群聊纯图(或文字极短)且没有任何指向猫猫的信号 → 直接丢掉,不进 filter。
            # 过去 filter AI 偶尔会把"群里有人发的没头没尾的图"判成要互动,导致莫名其妙回复图片。
            text_for_signal = (text_without_prefix or text).strip()
            if has_image and len(text_for_signal) <= 3:
                return None
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
        # 主人 2026-05-28: history 用数字 QQ 号替代 nickname 让 history bytes 跨请求稳定
        # (nickname 来自 memory/sender.card 实时读, 可能变 → 破 cache). QQ 号是稳定的
        # 不可逆 ID. 模型回复时通过 system 末尾的 "QQ→昵称" 映射表 (1-2 token/user) 知道
        # 具体名字. 这样 cache prefix 在群里多用户场景下也能稳定 hit.
        history_content = f"[QQ:{event.user_id}] {final_text}"
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


def split_reply(text: str, max_chars: int, *, max_chunks: int = 2) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    if max_chars <= 0 or len(clean) <= max_chars:
        return [clean]
    if max_chunks <= 1:
        return [clean]

    chunks: list[str] = []
    remaining = clean
    while len(remaining) > max_chars and len(chunks) < max_chunks - 1:
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
