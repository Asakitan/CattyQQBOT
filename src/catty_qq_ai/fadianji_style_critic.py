from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from .openai_client import _post_chat_completion


logger = logging.getLogger("catty_qq_ai.fadianji_style_critic")
_SYSTEM_PROMPT = (
    "你是 QQ 机器人「机机」的发言质检员。把输入的回复改写成机机的说话方式, 或判断无需改写。"
    "机机风格: 1) 单条短句, 中位 4 字, 绝大多数 ≤13 字; 2) 几乎不写句末标点; 3) 真人网友口吻, "
    "口语现场件: 谢谢/我去/我靠/好好好/好耶/唉/不知道/没事/哈哈, 跳脸: 滚啊/有病吧; "
    "4) 非技术闲聊避免客服腔/分点列表/markdown/自我介绍/铺垫; "
    "5) 技术问题要短而完整, 允许必要步骤/代码/命令/公式, 不删掉解决问题所需的信息; "
    "6) 『喵』是 Catty 专属口癖, 机机任何场景都不输出; "
    "7) 保留原回复的事实与立场, 只改 prose 口吻, 不新增信息, 不输出任何解释或引号, 直接输出改写结果。"
    "若原回复已经足够像机机, 原样输出即可。"
)
_CUSTOMER_SERVICE_TERMS = (
    "您好", "亲爱的", "感谢您的", "很高兴为您", "请问还有什么", "如有需要", "祝您",
    "希望对您有帮助", "欢迎随时",
)
_STRUCTURE_TERMS = ("总的来说", "综上所述", "需要注意的是")
_TECHNICAL_TERMS = (
    "怎么", "如何", "报错", "代码", "教程", "步骤", "配置", "技术", "编程", "接口",
    "api", "函数", "脚本", "命令", "日志", "bug", "错误", "traceback", "exception",
    "json", "python", "sql", "http", "部署", "服务器", "网络", "数据库",
)
_TECHNICAL_STRONG_TERMS = tuple(
    term for term in _TECHNICAL_TERMS
    if term not in {"怎么", "如何", "技术", "接口", "网络"}
)
_TECHNICAL_CONTEXT_RE = re.compile(
    r"(?:怎么|如何|帮我|能不能|为何).{0,12}(?:技术|接口|网络|端口|服务|请求|响应|环境|依赖|安装|文件|路径)"
    r"|(?:技术|接口|网络|端口|服务|请求|响应|环境|依赖|安装|文件|路径).{0,12}(?:怎么|如何|报错|失败|异常|配置|调试)",
    re.IGNORECASE,
)
_AI_REVEAL_TERMS = (
    "我是ai", "我是 ai", "我是人工智能", "我是机器人", "我是聊天机器人",
    "作为ai", "作为 ai", "作为人工智能", "作为语言模型", "chatgpt",
    "language model", "as an ai",
)
_EMOJI_BASE = r"(?:[\U0001F1E6-\U0001F1FF]{2}|[\u2600-\u27BF\U0001F300-\U0001FAFF])"
_EMOJI_TOKEN_RE = re.compile(
    rf"{_EMOJI_BASE}(?:[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]*(?:\u200D{_EMOJI_BASE}[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]*)*)?"
)
_MARKDOWN_HEADING_RE = re.compile(r"^\s*#{2,3}(?:\s|$)", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*-\s+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_CATTY_MARKER_RE = re.compile(
    r"<{2,4}CATTY_[A-Z_]+(?::[^<>\n]*?)?>{2,4}|\[\[CATTY_[^\]\n]*\]\]",
    re.IGNORECASE,
)
_URL_RE = re.compile(
    r"(?i)\b(?:https?://|ftp://|www\.)[^\s<>\"'，。！？,;；：:）)\]]+"
)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
_LATEX_RE = re.compile(
    r"\\\[[\s\S]*?\\\]"
    r"|\\\([\s\S]*?\\\)"
    r"|\$\$[\s\S]*?\$\$"
    r"|(?<!\$)\$(?=[^$\n]*\\[A-Za-z]+)[^$\n]+\$(?!\$)"
)
_WINDOWS_PATH_RE = re.compile(r"(?<![\w:])[A-Za-z]:[\\/][^\s<>\"']+")
_UNC_PATH_RE = re.compile(r"\\\\[^\s<>\"']+\\[^\s<>\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[A-Za-z0-9._~-]+/){1,}[A-Za-z0-9._~-]+")
_FILENAME_RE = re.compile(
    r"\b[\w.-]+\.(?:py|pyw|js|ts|tsx|jsx|json|ya?ml|md|txt|log|exe|dll|bat|cmd|ps1|sh|bash|cpp|cc|cxx|h|hpp|sql|toml|ini|cfg|png|jpe?g|webp|zip)\b",
    re.IGNORECASE,
)
_FUNCTION_CALL_RE = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\([^()\n]{0,200}\)")
_FUNCTION_NAME_RE = re.compile(r"\b[A-Za-z_]\w*(?=\s*\()")
_VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+)+(?:[-+._][0-9A-Za-z]+)*\b")
_NUMBER_RE = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?(?![A-Za-z_])")
_DIGIT_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*\b")
_OPAQUE_TOKEN_RE = re.compile(r"__CATTY_OPAQUE_\d{4}__")
_SUCCESS_FACT_RE = re.compile(r"(?i)(?:\bok\b|success|成功|已完成|已生成|已找到|已入队|返回\s*2\d\d)")
_FAILURE_FACT_RE = re.compile(r"(?i)(?:\berror\b|failed?|失败|报错|异常|没找到|未找到|未生成|未完成|超时)")
# 成人创作模式豁免 (主人 2026-08-15): 机机被点名写小黄文时输出是 300-800 字散文,
# 「长度味/结构味」预筛会把它送去 audit 重写成短句, 直接毁掉创作。user_text 命中
# 创作请求词 **或** 回复本体含成人内容词 ≥2 → 整条免检原样放行。
_ADULT_REQUEST_RE = re.compile(
    r"小黄文|黄文|写文|写点文|来一篇|写一篇|写一段|写长一点|写点那个|整一篇|扩写|续写|接龙"
    r"|开车|开下?车|来点车|车车|上车|上高速|涩图|涩涩|色色|来点涩|h文|肉文|R18|r18"
    r"|写点涩|写点色|来点色|来点黄|整点涩|搞点黄的",
    re.IGNORECASE,
)
_ADULT_CONTENT_TERMS = (
    "高潮", "插入", "抽插", "肉棒", "阴茎", "小穴", "阴道", "阴蒂", "乳头", "乳房",
    "乳尖", "呻吟", "娇喘", "前戏", "口交", "舔舐", "蜜液", "爱液", "穴口", "子宫口",
    "大腿内侧", "挺腰", "撞进", "顶到", "深处", "痉挛", "潮吹", "失禁", "敏感点",
)
_IMAGE_TAG_RE = re.compile(r"(?i)(?:\[图片(?:[:：][^\]\n]+)?\]|<image:[^>\n]+>)")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^\)\n]+\)")
_MAX_AUDIT_REPLY_CHARS = 6000
_STATS = {"checked": 0, "suspect": 0, "rewritten": 0, "rewrite_failed": 0}


def _is_technical_question(user_text: str) -> bool:
    lowered = (user_text or "").lower()
    if any(term in lowered for term in _TECHNICAL_STRONG_TERMS):
        return True
    contextual = lowered.replace("接口人", "").replace("网络节目", "").replace("技术分享", "")
    return bool(_TECHNICAL_CONTEXT_RE.search(contextual))


def _fact_polarity(text: str) -> frozenset[str]:
    raw_text = str(text or "")
    stripped = raw_text.strip()
    if stripped:
        try:
            structured = json.loads(stripped)
        except (TypeError, ValueError):
            pass
        else:
            return _structured_fact_polarity(structured)
    values: set[str] = set()
    if _SUCCESS_FACT_RE.search(raw_text):
        values.add("success")
    if _FAILURE_FACT_RE.search(raw_text):
        values.add("failure")
    return frozenset(values)


def _structured_fact_polarity(value: Any) -> frozenset[str]:
    values: set[str] = set()
    if isinstance(value, dict):
        for key, field in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key == "ok":
                if field is True or (isinstance(field, str) and field.strip().lower() in {"true", "ok"}):
                    values.add("success")
                elif field is False or (isinstance(field, str) and field.strip().lower() in {"false", "failed", "failure"}):
                    values.add("failure")
            elif normalized_key == "status" and isinstance(field, str):
                normalized_status = field.strip().lower()
                if normalized_status in {"ok", "success", "succeeded", "complete", "completed", "done"}:
                    values.add("success")
                elif normalized_status in {"error", "failed", "failure", "incomplete", "timeout"}:
                    values.add("failure")
            elif normalized_key == "error":
                if field not in (None, False, "", 0):
                    values.add("failure")
            if isinstance(field, (dict, list)):
                values.update(_structured_fact_polarity(field))
    elif isinstance(value, list):
        for item in value:
            values.update(_structured_fact_polarity(item))
    return frozenset(values)


def _emoji_tokens(text: str) -> list[str]:
    return _EMOJI_TOKEN_RE.findall(text or "")


def _is_pure_emoji_or_image(text: str) -> bool:
    stripped = (text or "").strip()
    if stripped == "[图片]":
        return True
    tokens = _emoji_tokens(stripped)
    if not tokens:
        return False
    remainder = _EMOJI_TOKEN_RE.sub("", stripped)
    remainder = re.sub(r"[\s\uFE0E\uFE0F\u200D\u20E3]", "", remainder)
    return not remainder


def _has_short_obvious_smell(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in (*_CUSTOMER_SERVICE_TERMS, *_AI_REVEAL_TERMS))


def _is_adult_creative_turn(reply: str, user_text: str) -> bool:
    """成人创作模式判定: 创作请求词命中 user_text, 或回复本体成人内容词 ≥2。"""
    if _ADULT_REQUEST_RE.search(str(user_text or "")):
        return True
    reply_text = str(reply or "")
    return sum(term in reply_text for term in _ADULT_CONTENT_TERMS) >= 2


def check_ai_smell(
    reply: str,
    *,
    user_text: str = "",
    min_reply_chars: int = 6,
) -> tuple[bool, list[str]]:
    _STATS["checked"] += 1
    text = str(reply or "")
    stripped = text.strip()
    if "<<<CATTY_NO_REPLY>>>" in text or _is_pure_emoji_or_image(stripped):
        return False, []
    # 成人创作模式: 写小黄文是长散文, 豁免全部预筛 (长度/结构/格式味全免)。
    if _is_adult_creative_turn(text, user_text):
        return False, []
    try:
        short_limit = max(int(min_reply_chars), 0)
    except (TypeError, ValueError):
        short_limit = 6
    if len(stripped) <= short_limit and not _has_short_obvious_smell(stripped):
        return False, []
    reasons: list[str] = []
    for term in _CUSTOMER_SERVICE_TERMS:
        if term in text:
            reasons.append(f"客服腔词：{term}")
    if any(term in text.lower() for term in _AI_REVEAL_TERMS):
        reasons.append("自报身份味：AI/语言模型")
    is_technical = _is_technical_question(user_text)
    if not is_technical:
        if "首先" in text and ("其次" in text or "最后" in text):
            reasons.append("结构词：出现首先与其次/最后")
        for term in _STRUCTURE_TERMS:
            if term in text:
                reasons.append(f"结构词：{term}")
        if _MARKDOWN_HEADING_RE.search(text):
            reasons.append("格式味：markdown 标题")
        if len(_BULLET_RE.findall(text)) >= 3:
            reasons.append("格式味：3 行及以上列表")
        if len(_NUMBERED_RE.findall(text)) >= 3:
            reasons.append("格式味：3 项及以上编号列表")
        if "```" in text:
            reasons.append("格式味：代码块")
    if len(stripped) <= 80 and stripped.count("。") >= 3:
        reasons.append("标点味：短回复含 3 个及以上句号")
    if "！！" in stripped:
        reasons.append("标点味：感叹号连用")
    if not is_technical and len(stripped) > 60:
        reasons.append("长度味：非技术问题回复超过 60 字")
    emoji_kinds = {
        re.sub(r"[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]", "", token)
        for token in _emoji_tokens(text)
    }
    if len(emoji_kinds) >= 3:
        reasons.append("emoji 装饰味：混用 3 种及以上 emoji")
    for phrase in ("让我来", "我来帮你", "作为一个", "根据我的"):
        if phrase in text:
            reasons.append(f"解释腔：{phrase}")
    suspect = bool(reasons)
    if suspect:
        _STATS["suspect"] += 1
    return suspect, reasons


def _strip_markers_for_audit(text: str) -> str:
    if not text:
        return ""
    return _CATTY_MARKER_RE.sub("", str(text))


def _opaque_spans(text: str) -> list[tuple[int, int]]:
    patterns = (
        _CATTY_MARKER_RE, _CODE_FENCE_RE, _IMAGE_TAG_RE, _MARKDOWN_IMAGE_RE,
        _URL_RE, _LATEX_RE, _INLINE_CODE_RE,
        _WINDOWS_PATH_RE, _UNC_PATH_RE, _POSIX_PATH_RE, _FILENAME_RE,
        _FUNCTION_CALL_RE, _FUNCTION_NAME_RE, _VERSION_RE, _DIGIT_IDENTIFIER_RE, _NUMBER_RE,
    )
    matches: list[tuple[int, int]] = []
    for pattern in patterns:
        matches.extend(match.span() for match in pattern.finditer(text))
    selected: list[tuple[int, int]] = []
    for start, end in sorted(matches, key=lambda span: (span[0], -(span[1] - span[0]))):
        if start < (selected[-1][1] if selected else 0):
            continue
        selected.append((start, end))
    return selected


def _protect_opaque_spans(text: str) -> tuple[str, list[str]]:
    if not text:
        return "", []
    spans = _opaque_spans(text)
    if not spans:
        return text, []
    parts: list[str] = []
    values: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(spans):
        parts.append(text[cursor:start])
        values.append(text[start:end])
        parts.append(f"__CATTY_OPAQUE_{index:04d}__")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), values


def _restore_opaque_spans(cleaned: str, values: list[str]) -> str | None:
    if not cleaned:
        return None
    expected = [f"__CATTY_OPAQUE_{index:04d}__" for index in range(len(values))]
    found = _OPAQUE_TOKEN_RE.findall(cleaned)
    if found != expected:
        return None
    if any(cleaned.count(token) != 1 for token in expected):
        return None
    if _CATTY_MARKER_RE.search(cleaned):
        return None
    restored = cleaned
    for token, value in zip(expected, values):
        restored = restored.replace(token, value)
    return restored


def _clean_rewrite_result(result: Any) -> str:
    cleaned = str(result or "").strip()
    quote_pairs = (("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"), ("`", "`"))
    changed = True
    while changed:
        changed = False
        for prefix in ("改写：", "改写:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                changed = True
                break
        if changed:
            continue
        for left, right in quote_pairs:
            if cleaned.startswith(left) and cleaned.endswith(right):
                cleaned = cleaned[1:-1].strip()
                changed = True
                break
    return cleaned


async def rewrite_if_needed(
    reply: str,
    *,
    user_text: str,
    config: Any,
    enabled_attr: str = "catty_style_critic_enabled",
    tool_result_texts: Iterable[str] | None = None,
    context_hint: str = "",
) -> str:
    if not getattr(config, enabled_attr, True):
        return reply
    try:
        min_reply_chars = max(int(getattr(config, "catty_style_critic_min_reply_chars", 6) or 0), 0)
    except (TypeError, ValueError):
        min_reply_chars = 6
    suspect, _reasons = check_ai_smell(
        reply,
        user_text=user_text,
        min_reply_chars=min_reply_chars,
    )
    if not suspect or len(str(reply or "")) > _MAX_AUDIT_REPLY_CHARS:
        return reply
    base_url = str(getattr(config, "catty_audit_ai_base_url", "") or "").strip()
    api_key = str(getattr(config, "catty_audit_ai_api_key", "") or "").strip()
    model = str(getattr(config, "catty_audit_ai_model", "") or "").strip()
    if not base_url or not api_key or not model:
        _STATS["rewrite_failed"] += 1
        return reply
    protected_reply, protected_values = _protect_opaque_spans(str(reply or ""))
    audit_user_content = (
        f"用户: {_strip_markers_for_audit(user_text)}\n"
        "机机原回复（只改 prose，双下划线占位符必须逐个原样保留、顺序不变）:\n"
        f"{protected_reply}"
    )
    clean_hint = _strip_markers_for_audit(context_hint)
    if clean_hint:
        audit_user_content += f"\n\n上下文提示:\n{clean_hint}"
    audit_results = [
        _strip_markers_for_audit(str(item))[:2000]
        for item in (tool_result_texts or [])
        if str(item).strip()
    ]
    if audit_results:
        audit_user_content += (
            "\n\n本轮真实工具结果（事实优先，禁止把成功写成失败或把失败写成成功）:\n"
            + "\n---\n".join(audit_results)
        )
    if audit_results:
        source_polarity = frozenset().union(*(_fact_polarity(item) for item in audit_results))
    else:
        source_polarity = _fact_polarity(str(reply or ""))
    timeout = getattr(config, "catty_audit_ai_request_timeout", None)
    if timeout is None:
        timeout = 20.0
    temperature = getattr(config, "catty_audit_ai_temperature", None)
    if temperature is None:
        temperature = 0.3
    max_tokens = getattr(config, "catty_audit_ai_max_tokens", None)
    if max_tokens is None:
        max_tokens = 300
    try:
        result = await _post_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": audit_user_content},
            ],
            timeout=float(timeout),
            proxy=str(getattr(config, "catty_http_proxy", "") or ""),
            temperature=temperature,
            max_tokens=max_tokens,
            extra_headers=dict(getattr(config, "catty_audit_ai_extra_headers", {}) or {}),
            extra_body=dict(getattr(config, "catty_audit_ai_extra_body", {}) or {}),
            enable_cache=False,
            cache_depth=2,
            request_route="style_critic_rewrite",
        )
        cleaned = _clean_rewrite_result(result)
        restored = _restore_opaque_spans(cleaned, protected_values)
        if (
            not restored
            or len(restored) > _MAX_AUDIT_REPLY_CHARS
            or any(term in restored for term in _CUSTOMER_SERVICE_TERMS)
            or (source_polarity == {"success"} and "failure" in _fact_polarity(restored))
            or (source_polarity == {"failure"} and "success" in _fact_polarity(restored))
        ):
            _STATS["rewrite_failed"] += 1
            return reply
        _STATS["rewritten"] += 1
        return restored
    except Exception:  # noqa: BLE001
        _STATS["rewrite_failed"] += 1
        logger.debug("fadianji style critic rewrite failed", exc_info=True)
        return reply


def get_stats() -> dict[str, int]:
    return dict(_STATS)


def reset_stats() -> None:
    _STATS.update({"checked": 0, "suspect": 0, "rewritten": 0, "rewrite_failed": 0})
