"""LLM 输出宽容解析器:统一处理 JSON 噪声和 marker 闭合偏差。

设计动机:GPT/Claude/Qwen 在格式化输出时偶尔会带 markdown fence、智能引号、
尾随逗号、单引号、前后废话等噪声。任意一个解析失败就让一整轮决策走 fallback,
所以集中实现一个 lenient_json_loads 让所有 JSON 调用点共享同一容错策略。
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any


# ── JSON 容错解析 ──────────────────────────────────────────────────────

# ```json 或 ``` 开头 / 结尾的 markdown code fence
_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?", re.MULTILINE)
_MARKDOWN_FENCE_CLOSE_RE = re.compile(r"\n?\s*```\s*$", re.MULTILINE)
# 尾随逗号:JSON 严格禁止 } 或 ] 前的逗号,但 LLM 经常写出来
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
# 智能引号 / 全角引号(LLM 偶尔受中文输入法影响输出非 ASCII 引号)
_SMART_QUOTES_MAP = str.maketrans(
    {
        "“": '"',  # 左弯双引号
        "”": '"',  # 右弯双引号
        "‘": "'",  # 左弯单引号
        "’": "'",  # 右弯单引号
        "＂": '"',  # 全角双引号
        "＇": "'",  # 全角单引号
        "「": '"',  # 直角左
        "」": '"',  # 直角右
    }
)


def _strip_markdown_fence(text: str) -> str:
    """去掉首尾 ```json / ``` 围栏(仅当成对出现时才剥)。"""
    if "```" not in text:
        return text
    cleaned = _MARKDOWN_FENCE_OPEN_RE.sub("", text, count=1)
    cleaned = _MARKDOWN_FENCE_CLOSE_RE.sub("", cleaned, count=1)
    return cleaned.strip()


def _normalize_quotes(text: str) -> str:
    return text.translate(_SMART_QUOTES_MAP)


def _fix_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _find_balanced(text: str, start: int, open_char: str, close_char: str) -> str:
    """从 ``start`` 处开始找 balanced ``open_char``/``close_char``,跳过字符串内字符。

    返回 ``text[start:end+1]`` 或失败时空串。能正确处理嵌套对象、字符串里出现的括号、
    转义字符。仅支持双引号字符串(JSON 标准),不识别单引号字符串。
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def _convert_single_quoted(text: str) -> str:
    """简陋单引号 → 双引号转换。仅当整段没有双引号 + 看起来像 JSON 时启用。

    Python repr / 部分模型用单引号写 dict 字面量,这种情况退化处理。
    若 text 里同时混用单双引号(更可能是字符串值里有单引号),不动它。
    """
    if '"' in text or "'" not in text:
        return text
    return text.replace("'", '"')


def lenient_json_loads(text: str) -> Any:
    """宽容 JSON 解析,失败返回 ``None``。

    依次尝试(每步都独立保留前面失败的 cleanup):
    1. 直接 json.loads
    2. 剥 markdown fence
    3. 归一化引号 + 修尾逗号
    4. 单引号转双引号
    5. 从 ``{`` 或 ``[`` 起找第一个 balanced 子串再试
    """
    if not text or not isinstance(text, (str, bytes, bytearray)):
        return None
    if isinstance(text, (bytes, bytearray)):
        try:
            text = text.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, AttributeError):
            return None
    raw = text.strip()
    if not raw:
        return None

    # 步骤 1:直球
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass

    # 步骤 2:剥 markdown fence
    no_fence = _strip_markdown_fence(raw)
    if no_fence and no_fence != raw:
        try:
            return json.loads(no_fence)
        except (ValueError, TypeError):
            pass
    base = no_fence or raw

    # 步骤 3:归一化引号 + 修尾逗号
    normalized = _fix_trailing_commas(_normalize_quotes(base))
    if normalized != base:
        try:
            return json.loads(normalized)
        except (ValueError, TypeError):
            pass

    # 步骤 4:单引号转双引号(只在没有双引号的场景安全)
    single_fixed = _convert_single_quoted(normalized)
    if single_fixed != normalized:
        try:
            return json.loads(single_fixed)
        except (ValueError, TypeError):
            pass

    # 步骤 5:从首个 { 或 [ 起找 balanced 子串再试
    for open_char, close_char in (("{", "}"), ("[", "]")):
        start = normalized.find(open_char)
        if start < 0:
            continue
        substring = _find_balanced(normalized, start, open_char, close_char)
        if not substring:
            continue
        for candidate in (substring, _fix_trailing_commas(substring)):
            try:
                return json.loads(candidate)
            except (ValueError, TypeError):
                continue

    return None


def lenient_json_object(text: str) -> dict[str, Any] | None:
    """宽容 JSON 对象解析:在 ``lenient_json_loads`` 基础上要求结果是 ``dict``。"""
    parsed = lenient_json_loads(text)
    return parsed if isinstance(parsed, dict) else None


# ── Content-block 字面量兜底解包 ───────────────────────────────────────
# 模型偶发把自己的回复包成 OpenAI/Anthropic content-block 的「字面量」再当纯文本吐出来:
#     [{'type': 'text', 'text': '...'}]
# 单引号 + 转义 \n 是 Python ``str(list)`` 的指纹(模型真换行会是实际换行、JSON 会用双引号),
# 根因是历史里混入了这种格式后模型 few-shot 复读。这里在响应解析处把它解回纯文本,
# 断掉「脏 reply 落盘 → 下轮模型再复读」的自我强化回声。纯输出侧, 不碰 prompt 组装/cache。

# 廉价闸门:出现 'type':'text' / "type":"text" 才值得进昂贵的字面量解析。
_CONTENT_BLOCK_HINT_RE = re.compile(r"""['"]type['"]\s*:\s*['"]text['"]""")
_MALFORMED_TEXT_BLOCK_WRAPPER_RE = re.compile(
    r"""\A\s*
        (?:\[\s*)?\{\s*
        (?P<q1>['"])type(?P=q1)\s*:\s*(?P<q2>['"])text(?P=q2)\s*:\s*
        (?P<q3>['"])text(?P=q3)\s*:\s*
        (?P<q4>['"])(?P<body>(?:\\.|(?!\s*(?P=q4)\s*\}\s*(?:\]\s*)?\Z).)*)(?P=q4)
        \s*\}\s*(?:\]\s*)?\Z
    """,
    re.DOTALL | re.VERBOSE,
)


def _candidate_content_block_literals(text: str) -> list[str]:
    """返回可安全尝试解析的 content-block 外壳候选。"""
    candidates = [text]
    # 线上见过 ``{'type': 'text', 'text': '...'}]``: 少了开头 [, 但右侧残留 ].
    if text.startswith("{") and text.endswith("}]"):
        candidates.append(text[:-1])
    return candidates


def _unwrap_malformed_text_block_wrapper(text: str) -> str | None:
    """剥掉模型偶发的坏 content-block 外壳: ``{'type': 'text': 'text': '...'}``。

    这不是合法 JSON/Python 字面量,所以 ``json.loads`` / ``ast.literal_eval`` 都会失败。
    仅接受整段文本就是单个 text block(可包一层 ``[]``)的形态,避免误伤普通正文。
    """
    match = _MALFORMED_TEXT_BLOCK_WRAPPER_RE.match(text)
    if not match:
        return None
    body = match.group("body")
    try:
        return ast.literal_eval(match.group("q4") + body + match.group("q4"))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return body


def _text_from_content_blocks(obj: Any) -> str | None:
    """从 content-block 结构(list[dict] 或单 dict)抽出拼接 text;不匹配返回 ``None``。

    只认纯 text 块:出现非 ``text`` 类型块或 ``text`` 不是字符串就放弃(返回 None),
    避免误伤本就合法的含 ``image_url`` / 工具结构的数据。
    """
    blocks = obj if isinstance(obj, list) else [obj]
    if not blocks or not all(isinstance(b, dict) for b in blocks):
        return None
    parts: list[str] = []
    saw_text = False
    for b in blocks:
        btype = b.get("type")
        txt = b.get("text")
        if btype not in (None, "text"):
            return None
        if isinstance(txt, str):
            saw_text = True
            parts.append(txt)
        elif txt is not None:
            return None
    return "".join(parts) if saw_text else None


def unwrap_content_block_repr(text: str) -> str:
    """若整段文本是 content-block 列表/字典的字面量,解回纯文本;否则原样返回。

    只在「整段」恰好是 ``[{'type':'text','text':...}]`` / ``{'type':'text','text':...}``
    时解包(防止误伤正文里偶然出现的方括号)。先试 ``json.loads`` (双引号变体),
    再试 ``ast.literal_eval`` (单引号 Python repr 变体)——两者都安全,literal_eval 只解析
    字面量不执行代码。
    """
    if not text or not isinstance(text, str):
        return text
    stripped = text.strip()
    if not (stripped.startswith("[{") or stripped.startswith("{")):
        return text
    if not stripped.endswith(("]", "}")):
        return text
    if not _CONTENT_BLOCK_HINT_RE.search(stripped):
        return text
    for candidate in _candidate_content_block_literals(stripped):
        try:
            parsed: Any = json.loads(candidate)
        except (ValueError, TypeError):
            try:
                parsed = ast.literal_eval(candidate)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                extracted_malformed = _unwrap_malformed_text_block_wrapper(candidate)
                if extracted_malformed is not None:
                    return extracted_malformed.strip() or text
                continue
        extracted = _text_from_content_blocks(parsed)
        if extracted is not None:
            return extracted.strip() or text
    return text


# ── Marker 闭合宽容化 ─────────────────────────────────────────────────

# LLM 偶尔会把 `<<<` 写成 `<<` 或 `<<<<`、`>>>` 写成 `>>` 或 `>>>>`。
# 用 `<{2,4}` / `>{2,4}` 范围容忍偏差。内容禁止包含尖括号或换行,
# 避免吞掉相邻文本或把多个 marker 错连成一个。
_MARKER_BODY_RE = re.compile(
    r"<{2,4}CATTY_([A-Z_]+)(?::([^<>\n]*?))?>{2,4}",
    re.IGNORECASE,
)


def iter_catty_markers(text: str):
    """yield (match, name_upper, payload) for each <<<CATTY_NAME[:payload]>>> in text.

    Payload 为空字符串表示该 marker 没有 ``:xxx`` 部分(例如 NO_REPLY)。
    """
    if not text:
        return
    for match in _MARKER_BODY_RE.finditer(text):
        name = (match.group(1) or "").upper()
        payload = (match.group(2) or "").strip()
        yield match, name, payload


def strip_catty_markers(
    text: str,
    *,
    keep: set[str] | None = None,
) -> str:
    """删掉所有 ``<<<CATTY_NAME[:payload]>>>`` 标记;保留 ``keep`` 集合里(大写)的 marker 名。

    用于 _sanitize_residual_markers:发送给用户前清掉所有"AI 不应输出"的残留 marker,
    白名单(默认在 caller 设)留给后续 stage 处理(INLINE_IMAGE/EMOJI_QUERY/NO_REPLY)。
    """
    if not text:
        return ""
    keep_upper = {name.upper() for name in keep} if keep else set()

    def _sub(match: "re.Match[str]") -> str:
        name = (match.group(1) or "").upper()
        return match.group(0) if name in keep_upper else ""

    return _MARKER_BODY_RE.sub(_sub, text)
