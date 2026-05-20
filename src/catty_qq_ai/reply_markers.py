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


def extract_emoji_query(reply: str) -> tuple[str, str]:
    clean = reply
    selected_query = ""

    while True:
        start = clean.find(EMOJI_QUERY_PREFIX)
        if start < 0:
            return clean.strip(), selected_query

        query_start = start + len(EMOJI_QUERY_PREFIX)
        end = clean.find(EMOJI_QUERY_SUFFIX, query_start)
        if end >= 0:
            query = clean[query_start:end].strip()
            clean = clean[:start] + clean[end + len(EMOJI_QUERY_SUFFIX) :]
        else:
            line_end = clean.find("\n", query_start)
            if line_end >= 0:
                query = clean[query_start:line_end].strip()
                clean = clean[:start] + clean[line_end + 1 :]
            else:
                query = clean[query_start:].strip()
                clean = clean[:start]

        if query and not selected_query:
            selected_query = query


def extract_meme_queries(reply: str) -> tuple[str, list[tuple[int, str]]]:
    """把 reply 里所有 ``<<<CATTY_MEME:关键词>>>`` 标记替换成 inline 占位符 ``\\x00MEME_n\\x00``,
    返回 ``(new_text, [(idx, query), ...])`` 让上层异步拉图后回填。

    用 NUL 占位符的好处:reply_chunks 切段时不会把标记切到中间;且 NUL 在 QQ 文本里
    天然不存在,不会和正常内容冲突。
    """
    queries: list[tuple[int, str]] = []
    out: list[str] = []
    i = 0
    n = len(reply)
    while True:
        start = reply.find(MEME_QUERY_PREFIX, i)
        if start < 0:
            out.append(reply[i:])
            break
        out.append(reply[i:start])
        query_start = start + len(MEME_QUERY_PREFIX)
        end = reply.find(MEME_QUERY_SUFFIX, query_start)
        if end < 0:
            # 没闭合,按行尾兜底
            line_end = reply.find("\n", query_start)
            if line_end < 0:
                query = reply[query_start:].strip()
                i = n
            else:
                query = reply[query_start:line_end].strip()
                i = line_end
        else:
            query = reply[query_start:end].strip()
            i = end + len(MEME_QUERY_SUFFIX)
        if query:
            idx = len(queries)
            queries.append((idx, query))
            out.append(f"\x00MEME_{idx}\x00")
    return "".join(out), queries


def replace_meme_placeholders(text: str, urls: list[str]) -> str:
    """把 ``\\x00MEME_n\\x00`` 占位符替换成 ``<<<CATTY_INLINE_IMAGE:url>>>`` 标记。

    拉图失败(``urls[n]`` 为空)的位置占位符会被去掉,让该梗图自然消失而不留 NUL 残渣。
    """
    if not text or not urls:
        return text.replace("\x00MEME_", "").replace("\x00", "") if "\x00MEME_" in text else text

    import re

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
    """把 ``text`` 里所有 ``<<<CATTY_INLINE_IMAGE:URL>>>`` 标记替换成占位符 ``\\x00IMG_n\\x00``,
    返回 ``(new_text, [url, ...])`` 给发送链路把占位符还原成 ``MessageSegment.image``。
    """
    urls: list[str] = []
    out: list[str] = []
    i = 0
    while True:
        start = text.find(INLINE_IMAGE_PREFIX, i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        url_start = start + len(INLINE_IMAGE_PREFIX)
        end = text.find(INLINE_IMAGE_SUFFIX, url_start)
        if end < 0:
            # 兜底:整段当 URL 取到末尾(异常输入,但不让它把后续文本吞了)
            url = text[url_start:].strip()
            i = len(text)
        else:
            url = text[url_start:end].strip()
            i = end + len(INLINE_IMAGE_SUFFIX)
        if url:
            idx = len(urls)
            urls.append(url)
            out.append(f"\x00IMG_{idx}\x00")
    return "".join(out), urls


def strip_inline_image_markers(text: str, *, placeholder: str = INLINE_IMAGE_PLACEHOLDER) -> str:
    """把 ``text`` 里的 ``<<<CATTY_INLINE_IMAGE:URL>>>`` 全部替换成可读占位符(用于 history/memory)。

    避免把 base64 data URI 灌进 prompt token 池。
    """
    if INLINE_IMAGE_PREFIX not in text:
        return text
    out: list[str] = []
    i = 0
    while True:
        start = text.find(INLINE_IMAGE_PREFIX, i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        url_start = start + len(INLINE_IMAGE_PREFIX)
        end = text.find(INLINE_IMAGE_SUFFIX, url_start)
        if end < 0:
            out.append(placeholder)
            i = len(text)
        else:
            out.append(placeholder)
            i = end + len(INLINE_IMAGE_SUFFIX)
    return "".join(out)


def strip_inline_image_placeholders(text: str, *, placeholder: str = INLINE_IMAGE_PLACEHOLDER) -> str:
    """把 ``\\x00IMG_n\\x00`` 占位符替换成可读字符串(用于 history/training sample)。"""
    if "\x00IMG_" not in text:
        return text
    import re

    return re.sub(r"\x00IMG_\d+\x00", placeholder, text)


def split_chunk_with_image_placeholders(chunk_text: str, image_urls: list[str]) -> list[tuple[str, str]]:
    """把含 ``\\x00IMG_n\\x00`` 占位符的 chunk 拆成 ``[(kind, content), ...]`` 序列。

    返回的 kind 只有 ``"text"`` 和 ``"image"`` 两种;``"image"`` 的 content 是可直接喂给
    ``MessageSegment.image(file=...)`` 的 URL/base64 URI。失败位置(``image_urls[n]`` 为空)
    占位符会被丢弃,不留 NUL 残渣。
    """
    import re

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
