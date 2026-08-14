"""Catty marker 常量与提取/替换工具。

历史上的 substring find 实现对 LLM 偶发的字符偏差(``>>`` / ``>>>>`` / 漏闭合)很脆,
现在统一用 regex:
- 闭合 marker 允许 ``<{2,4}...>{2,4}`` 范围闭合(``<<`` 到 ``<<<<`` / ``>>`` 到 ``>>>>``)
- payload 禁止跨行也禁止含尖括号,避免吞掉相邻文本
- 不闭合时退到行尾/文件末尾(lookahead 不消耗 ``\n``,保留用户其它内容)
"""
import re


REPLY_SPLIT_MARKER = "<<<CATTY_REPLY_SPLIT>>>"
NO_REPLY_MARKER = "<<<CATTY_NO_REPLY>>>"
EMOJI_QUERY_PREFIX = "<<<CATTY_EMOJI_QUERY:"
EMOJI_QUERY_SUFFIX = ">>>"
# 梗图标记: AI 想让笨猫主动发一张梗图/网图时,在回复里写 <<<CATTY_MEME:关键词>>>,
# 后端去搜图(Bing 图片)拉一张,转成 INLINE_IMAGE 占位符插回原位置。
MEME_QUERY_PREFIX = "<<<CATTY_MEME:"
MEME_QUERY_SUFFIX = ">>>"
# Inline 图片占位符: 主 AI 多模态响应里的 image_url/base64,或 MEME 拉到图后,
# 统一用 <<<CATTY_INLINE_IMAGE:url>>> 表达,发送链路看到就插 MessageSegment.image。
INLINE_IMAGE_PREFIX = "<<<CATTY_INLINE_IMAGE:"
INLINE_IMAGE_SUFFIX = ">>>"
INLINE_IMAGE_PLACEHOLDER = "[图片]"  # history/memory 里替换 INLINE_IMAGE 用,省 token
# 机机情绪自我标记: 主 AI 在回复末尾自报 <<<CATTY_FD_MOOD:tag>>>, harness 提取后
# 喂给 fadianji_state 事件状态机; 标记被吃掉, 不出现在发送文本里。
FADIANJI_MOOD_PREFIX = "<<<CATTY_FD_MOOD:"
FADIANJI_MOOD_SUFFIX = ">>>"
TRAILING_CHAT_PUNCTUATION = " \t\r\n。！？!?；;，,、：:…."


# ── 宽容 regex 模板 ─────────────────────────────────────────────────
#
# 设计要点:
# - ``<{2,4}`` / ``>{2,4}`` 容忍 LLM 写成 `<<...>>` 或 `<<<<...>>>>` 这种字符偏差
# - payload 用 ``[^<>\n]*?`` 禁止跨行且禁止含尖括号(吞掉相邻 marker)
# - 闭合用 ``>{2,4}`` 否则 fallback 到 ``\n`` / ``$`` lookahead(不消耗,保留行内容)
# - 对 INLINE_IMAGE 而言 URL 可能含 ``>`` 字符的 edge case 极少(base64:// 不含;
#   http URL 含 ``>`` 也应该被 percent-encode 成 %3E),所以 payload 允许 ``>`` 之外的
#   闭合检查仍生效。

_EMOJI_QUERY_RE = re.compile(
    r"<{2,4}CATTY_EMOJI_QUERY:([^<>\n]*?)(?:>{2,4}|(?=\n)|\Z)",
    re.MULTILINE,
)
_MEME_QUERY_RE = re.compile(
    r"<{2,4}CATTY_MEME:([^<>\n]*?)(?:>{2,4}|(?=\n)|\Z)",
    re.MULTILINE,
)
# INLINE_IMAGE 比较特殊:base64:// URI 可能很长(几十 KB),也允许 ``>`` 之外的所有字符。
# 用专用 regex:闭合时严格 ``>{3}``(避免吞掉后续段落的开头);未闭合时退到行尾。
_INLINE_IMAGE_RE = re.compile(
    r"<{2,4}CATTY_INLINE_IMAGE:([^<>\n]*?)(?:>{2,4}|(?=\n)|\Z)",
    re.MULTILINE,
)
_FADIANJI_MOOD_RE = re.compile(
    r"<{2,4}CATTY_FD_MOOD:([^<>\n]*?)(?:>{2,4}|(?=\n)|\Z)",
    re.MULTILINE,
)


def extract_emoji_query(reply: str) -> tuple[str, str]:
    """提取并删除 ``<<<CATTY_EMOJI_QUERY:xxx>>>`` 标记。

    返回 ``(cleaned_text, first_payload)``;后续 stage 用 first_payload 去查表情库。
    多个 marker 都被删,但只取第一个非空 payload 作为选定查询。
    """
    if not reply:
        return "", ""
    selected_query = ""

    def _sub(match: "re.Match[str]") -> str:
        nonlocal selected_query
        query = (match.group(1) or "").strip()
        if query and not selected_query:
            selected_query = query
        return ""

    cleaned = _EMOJI_QUERY_RE.sub(_sub, reply)
    return cleaned.strip(), selected_query


def extract_fadianji_mood(reply: str) -> tuple[str, str]:
    """提取并删除 ``<<<CATTY_FD_MOOD:tag>>>`` 机机情绪自我标记。

    返回 ``(cleaned_text, first_tag)``;多个 marker 都删,只取第一个非空 tag。
    tag 合法性由 fadianji_state.apply_event 判定,这里只做提取。
    """
    if not reply:
        return "", ""
    selected_tag = ""

    def _sub(match: "re.Match[str]") -> str:
        nonlocal selected_tag
        tag = (match.group(1) or "").strip()
        if tag and not selected_tag:
            selected_tag = tag
        return ""

    cleaned = _FADIANJI_MOOD_RE.sub(_sub, reply)
    return cleaned.strip(), selected_tag


def extract_meme_queries(reply: str) -> tuple[str, list[tuple[int, str]]]:
    """把 reply 里所有 ``<<<CATTY_MEME:关键词>>>`` 替换成 inline 占位符 ``\\x00MEME_n\\x00``。

    返回 ``(text_with_placeholders, [(idx, query), ...])`` 让上层异步拉图后回填。
    用 NUL 占位符的好处:reply_chunks 切段时不会把标记切到中间,且 NUL 在 QQ 文本里
    天然不存在,不会和正常内容冲突。
    """
    if not reply:
        return "", []
    queries: list[tuple[int, str]] = []

    def _sub(match: "re.Match[str]") -> str:
        query = (match.group(1) or "").strip()
        if not query:
            return ""
        idx = len(queries)
        queries.append((idx, query))
        return f"\x00MEME_{idx}\x00"

    cleaned = _MEME_QUERY_RE.sub(_sub, reply)
    return cleaned, queries


def replace_meme_placeholders(text: str, urls: list[str]) -> str:
    """把 ``\\x00MEME_n\\x00`` 占位符替换成 ``<<<CATTY_INLINE_IMAGE:url>>>``。

    拉图失败(``urls[n]`` 为空)的位置占位符会被去掉,让该梗图自然消失而不留 NUL 残渣。
    """
    if not text:
        return ""
    if not urls:
        if "\x00MEME_" in text:
            return re.sub(r"\x00MEME_\d+\x00", "", text)
        return text

    def _sub(match: "re.Match[str]") -> str:
        try:
            idx = int(match.group(1))
        except (TypeError, ValueError):
            return ""
        if 0 <= idx < len(urls) and urls[idx]:
            return f"{INLINE_IMAGE_PREFIX}{urls[idx]}{INLINE_IMAGE_SUFFIX}"
        return ""

    return re.sub(r"\x00MEME_(\d+)\x00", _sub, text)


def extract_inline_images(text: str) -> tuple[str, list[str]]:
    """把 ``<<<CATTY_INLINE_IMAGE:URL>>>`` 标记替换成占位符 ``\\x00IMG_n\\x00``,
    返回 ``(text_with_placeholders, [url, ...])``。
    """
    if not text:
        return "", []
    urls: list[str] = []

    def _sub(match: "re.Match[str]") -> str:
        url = (match.group(1) or "").strip()
        if not url:
            return ""
        idx = len(urls)
        urls.append(url)
        return f"\x00IMG_{idx}\x00"

    cleaned = _INLINE_IMAGE_RE.sub(_sub, text)
    return cleaned, urls


def strip_inline_image_markers(text: str, *, placeholder: str = INLINE_IMAGE_PLACEHOLDER) -> str:
    """把 ``<<<CATTY_INLINE_IMAGE:URL>>>`` 全部替换成可读占位符(用于 history/memory)。

    避免把 base64 data URI 灌进 prompt token 池。
    """
    if not text or "CATTY_INLINE_IMAGE" not in text:
        return text
    return _INLINE_IMAGE_RE.sub(lambda _m: placeholder, text)


def strip_inline_image_placeholders(text: str, *, placeholder: str = INLINE_IMAGE_PLACEHOLDER) -> str:
    """把 ``\\x00IMG_n\\x00`` 占位符替换成可读字符串(用于 history/training sample)。"""
    if not text or "\x00IMG_" not in text:
        return text
    return re.sub(r"\x00IMG_\d+\x00", placeholder, text)


def split_chunk_with_image_placeholders(chunk_text: str, image_urls: list[str]) -> list[tuple[str, str]]:
    """把含 ``\\x00IMG_n\\x00`` 占位符的 chunk 拆成 ``[(kind, content), ...]`` 序列。

    返回的 kind 只有 ``"text"`` 和 ``"image"`` 两种;``"image"`` 的 content 是可直接喂给
    ``MessageSegment.image(file=...)`` 的 URL/base64 URI。失败位置(``image_urls[n]`` 为空)
    占位符会被丢弃,不留 NUL 残渣。
    """
    if not chunk_text:
        return []
    if not image_urls or "\x00IMG_" not in chunk_text:
        return [("text", chunk_text)]
    parts: list[tuple[str, str]] = []
    last = 0
    for match in re.finditer(r"\x00IMG_(\d+)\x00", chunk_text):
        s, e = match.span()
        if s > last:
            parts.append(("text", chunk_text[last:s]))
        try:
            idx = int(match.group(1))
        except (TypeError, ValueError):
            last = e
            continue
        if 0 <= idx < len(image_urls) and image_urls[idx]:
            parts.append(("image", image_urls[idx]))
        last = e
    if last < len(chunk_text):
        parts.append(("text", chunk_text[last:]))
    return parts


# ── （小声）式语气/动作注解括号剥除 (主人 2026-08-12, 机机人格用) ─────────────
#
# 机机 prompt (§5 输出格式 / chat_rhythm / voice_guide) 已明文禁止「（小声）（轻笑）」式
# 语气/动作注解, 模型仍偶发 → 出站前程序硬剥。只剥黑名单注解词 (可带 的/着/状/一下 等后缀,
# 也可带 喵/呜/呐 等语气尾词如（小声喵）, 可两词连用如 小声嘀咕); 语料实证玩梗括号
# （？）（不是）（悲）（去🦌了）（龟速做视频中）不在名单, 原样保留。
# （笑死）这类「名单词 + 非后缀字(死)」也不误伤 — 死/惨/爆/裂 等强调后缀不进尾词表。
_TONE_PAREN_BAN_WORDS: tuple[str, ...] = (
    "超小声", "小声", "轻声", "低声", "耳语", "嘀咕", "嘟囔", "咕哝", "喃喃",
    "笑出声", "轻笑", "偷笑", "憋笑", "苦笑", "笑",
    "叹气", "叹息", "轻叹",
    "捂脸", "掩面", "扶额", "歪头", "嘟嘴", "眨眼", "脸红", "害羞",
    "哼唧", "呜咽", "哽咽", "抽泣", "吸鼻子", "清嗓子", "轻咳", "哭", "哼",
    "尖叫", "怪叫", "转圈", "跺脚", "跳脚", "黑线", "无语", "冒汗", "冷汗",
)
_TONE_PAREN_BAN_ALT = "|".join(sorted(_TONE_PAREN_BAN_WORDS, key=len, reverse=True))
# 后缀: 结构助词/补语 + 语气尾词。主人 2026-08-14: 补 地 + 喵/呜/呐/嘤/呀/哦/呢/吧/嘛/啦/咯
# 修「（小声喵）」漏过滤; 不加 死/惨/爆/裂(强调后缀, 保（笑死）玩梗)。
_TONE_PAREN_SUFFIX = r"(?:的|着|了|状|声|中|一下|说|道|样|脸|地|喵|呜|呐|嘤|呀|哦|呢|吧|嘛|啦|咯)?"
_TONE_PAREN_RE = re.compile(
    r"[（(]\s*(?:" + _TONE_PAREN_BAN_ALT + r")" + _TONE_PAREN_SUFFIX
    + r"(?:" + _TONE_PAREN_BAN_ALT + r")?" + _TONE_PAREN_SUFFIX + r"\s*[）)]"
)


def strip_tone_parenthetical(text: str) -> str:
    """剥掉（小声）式语气/动作注解括号; 无命中时不改一字 (byte-identical)。

    命中后收拾残渣: 行内多余空格压成单个、空行丢弃、各行去首尾空白。
    persona 门控在调用方 (当前只机机人格启用; catty 的（动作）是人格本体, 不过滤)。
    """
    if not text or ("（" not in text and "(" not in text):
        return text
    cleaned = _TONE_PAREN_RE.sub("", text)
    if cleaned == text:
        return text
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    lines = [line for line in (ln.strip() for ln in cleaned.split("\n")) if line]
    return "\n".join(lines).strip()
