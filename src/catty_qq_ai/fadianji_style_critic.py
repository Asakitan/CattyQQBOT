from __future__ import annotations

import logging
import re
from typing import Any

from .openai_client import _post_chat_completion


logger = logging.getLogger("catty_qq_ai.fadianji_style_critic")
_SYSTEM_PROMPT = (
    "你是 QQ 机器人「机机」的发言质检员。把输入的回复改写成机机的说话方式, 或判断无需改写。"
    "机机风格: 1) 单条短句, 中位 4 字, 绝大多数 ≤13 字; 2) 几乎不写句末标点; 3) 真人网友口吻, "
    "口语现场件: 谢谢/我去/我靠/好好好/好耶/唉/不知道/没事/哈哈, 跳脸: 滚啊/有病吧; "
    "4) 绝不用客服腔/分点列表/markdown/自我介绍/铺垫; 5) 被问技术问题也只短答, 不写教程; "
    "6) 保留原回复的事实与立场, 只改口吻, 不新增信息, 不输出任何解释或引号, 直接输出改写结果。"
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
_EMOJI_BASE = r"(?:[\U0001F1E6-\U0001F1FF]{2}|[\u2600-\u27BF\U0001F300-\U0001FAFF])"
_EMOJI_TOKEN_RE = re.compile(
    rf"{_EMOJI_BASE}(?:[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]*(?:\u200D{_EMOJI_BASE}[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]*)*)?"
)
_MARKDOWN_HEADING_RE = re.compile(r"^\s*#{2,3}(?:\s|$)", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*-\s+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_STATS = {"checked": 0, "suspect": 0, "rewritten": 0, "rewrite_failed": 0}


def _is_technical_question(user_text: str) -> bool:
    lowered = (user_text or "").lower()
    return any(term in lowered for term in _TECHNICAL_TERMS)


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


def check_ai_smell(reply: str, *, user_text: str = "") -> tuple[bool, list[str]]:
    _STATS["checked"] += 1
    text = str(reply or "")
    stripped = text.strip()
    if "<<<CATTY_NO_REPLY>>>" in text or len(stripped) <= 6 or _is_pure_emoji_or_image(stripped):
        return False, []
    reasons: list[str] = []
    for term in _CUSTOMER_SERVICE_TERMS:
        if term in text:
            reasons.append(f"客服腔词：{term}")
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
    if "```" in text and not _is_technical_question(user_text):
        reasons.append("格式味：代码块")
    if len(stripped) <= 80 and stripped.count("。") >= 3:
        reasons.append("标点味：短回复含 3 个及以上句号")
    if "！！" in stripped:
        reasons.append("标点味：感叹号连用")
    if not _is_technical_question(user_text) and len(stripped) > 60:
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


async def rewrite_if_needed(reply: str, *, user_text: str, config: Any, enabled_attr: str = "catty_style_critic_enabled") -> str:
    if not getattr(config, enabled_attr, True):
        return reply
    suspect, _reasons = check_ai_smell(reply, user_text=user_text)
    if not suspect:
        return reply
    base_url = str(getattr(config, "catty_audit_ai_base_url", "") or "").strip()
    api_key = str(getattr(config, "catty_audit_ai_api_key", "") or "").strip()
    model = str(getattr(config, "catty_audit_ai_model", "") or "").strip()
    if not base_url or not api_key or not model:
        _STATS["rewrite_failed"] += 1
        return reply
    try:
        result = await _post_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"用户: {user_text[-200:]}\n机机原回复: {reply[:500:]}"},
            ],
            timeout=20.0,
            proxy=str(getattr(config, "catty_http_proxy", "") or ""),
            temperature=0.3,
            max_tokens=300,
            extra_headers=dict(getattr(config, "catty_audit_ai_extra_headers", {}) or {}),
            extra_body=dict(getattr(config, "catty_audit_ai_extra_body", {}) or {}),
            enable_cache=False,
            cache_depth=2,
            request_route="style_critic_rewrite",
        )
        cleaned = _clean_rewrite_result(result)
        if not cleaned or len(cleaned) > 500 or any(term in cleaned for term in _CUSTOMER_SERVICE_TERMS):
            _STATS["rewrite_failed"] += 1
            return reply
        _STATS["rewritten"] += 1
        return cleaned
    except Exception:  # noqa: BLE001
        _STATS["rewrite_failed"] += 1
        logger.debug("fadianji style critic rewrite failed", exc_info=True)
        return reply


def get_stats() -> dict[str, int]:
    return dict(_STATS)


def reset_stats() -> None:
    _STATS.update({"checked": 0, "suspect": 0, "rewritten": 0, "rewrite_failed": 0})