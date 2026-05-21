"""LLM 输出宽容解析器:统一处理 JSON 噪声和 marker 闭合偏差。

设计动机:GPT/Claude/Qwen 在格式化输出时偶尔会带 markdown fence、智能引号、
尾随逗号、单引号、前后废话等噪声。任意一个解析失败就让一整轮决策走 fallback,
所以集中实现一个 lenient_json_loads 让所有 JSON 调用点共享同一容错策略。
"""
from __future__ import annotations

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
