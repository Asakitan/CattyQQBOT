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
