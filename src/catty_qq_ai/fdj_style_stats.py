"""机机统计风格评分 (2026-08-25 升级计划 Wave ⑤)。

用离线挖掘的 style_stats.json 给回复打「机机像」分:
长度分布 / 句末标点 / markdown 分点 / 客服腔 / 喵味泄漏 / emoji 密度 / 口头禅命中。
分数达标 → 跳过 LLM 质检 (省 audit 调用、降延迟); 不达标仍走原 rewrite_if_needed。
技术/工具回复豁免长度惩罚 (信息完整优先于口吻)。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_TRAIL_PUNCT = "。！？!~～…"
_CATTY_TIC = "喵"
_CS_RE = re.compile(r"您好|亲爱的|祝您|如有需要|很高兴为您|请问还有什么|希望对您有帮助|欢迎随时")
_BULLET_RE = re.compile(r"(?m)^\s*(?:-\s+|\d+[.)]\s+|#{2,3}\s)")
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_EMOJI_RE = re.compile(r"[\u2600-\u27BF\U0001F300-\U0001FAFF]")
_FADIAN_RE = re.compile(r"(?:啊{3,}|呜{3,}|好{3,}|哈{3,}|哦{3,}|嘿{3,}|嘻{3,}|妈妈{2,}|谢谢{2,}|笑死|我超|救命|好好好|呜呜)")


def _config_value(name: str, default: Any) -> Any:
    try:
        from . import config as _module_config
        return getattr(_module_config.config, name, default)
    except Exception:  # noqa: BLE001
        return default


def _stats(scope: str) -> dict[str, Any]:
    try:
        from . import fdj_corpus_store
        value = fdj_corpus_store.style_stats(scope)
        return value if isinstance(value, Mapping) else {}
    except Exception:  # noqa: BLE001
        return {}


def _top_replies(scope: str) -> dict[str, int]:
    try:
        from . import fdj_corpus_store
        return fdj_corpus_store.top_reply_counts(scope)
    except Exception:  # noqa: BLE001
        return {}


def style_score(reply: str, *, scope: str = "group", technical: bool = False) -> tuple[float, list[str]]:
    """返回 (0..1 分数, 扣分/加分原因列表)。语料统计缺失时给中性 0.75 (不拦截也不放行)。"""
    text = str(reply or "")
    stripped = text.strip()
    if not stripped:
        return 1.0, ["empty"]
    stats = _stats(scope)
    if not stats:
        return 0.75, ["no-corpus-stats"]
    score = 1.0
    reasons: list[str] = []
    length = len("".join(stripped.split()))
    reply_len = stats.get("reply_len") if isinstance(stats.get("reply_len"), Mapping) else {}
    try:
        p95 = int(reply_len.get("p95") or 26)
    except (TypeError, ValueError):
        p95 = 26
    if not technical:
        if length > max(p95 * 4, 80):
            score -= 0.35
            reasons.append(f"len{length}>p95x4")
        elif length > max(p95 * 2, 40):
            score -= 0.2
            reasons.append(f"len{length}>p95x2")
        elif length > p95:
            score -= 0.08
            reasons.append(f"len{length}>p95")
    no_punct_ratio = float(stats.get("no_punct_anywhere_ratio") or 0.0)
    if no_punct_ratio >= 0.8:
        if stripped[-1] in _TRAIL_PUNCT:
            score -= 0.06
            reasons.append("trail-punct")
        if len(re.findall(r"[。！？!]", stripped)) >= 2:
            score -= 0.06
            reasons.append("multi-sentence-punct")
    prose = _CODE_FENCE_RE.sub("", text)
    prose = _INLINE_CODE_RE.sub("", prose)
    if not technical and _BULLET_RE.search(prose):
        score -= 0.35
        reasons.append("markdown-list")
    if _CS_RE.search(text):
        score -= 0.3
        reasons.append("customer-service-tone")
    if _CATTY_TIC in text:
        score -= 0.35
        reasons.append("catty-meow-leak")
    if len(_EMOJI_RE.findall(text)) > 2:
        score -= 0.08
        reasons.append("emoji-heavy")
    if stripped in _top_replies(scope):
        score += 0.08
        reasons.append("canonical-catchphrase")
    elif _FADIAN_RE.search(stripped) and float(stats.get("fadian_ratio") or 0.0) > 0:
        score += 0.04
        reasons.append("fadian-shape")
    return max(0.0, min(1.0, score)), reasons


def stat_critic_passes(reply: str, *, scope: str = "group", technical: bool = False, user_text: str = "") -> bool:
    """统计评分前置判定: True = 够机机, 免 LLM 质检。

    技术语境 (工具结果/技术问题) 不做前置放行 — 信息密度高的回复仍让 LLM 质检把关,
    只享受技术豁免的长度容忍。
    """
    if not bool(_config_value("catty_fdj_stat_critic_enabled", True)):
        return False
    if technical:
        return False
    if str(user_text or "").strip() and _looks_technical_query(user_text):
        return False
    if not _stats(scope):
        return False  # 语料统计缺失 → 保守放行给 LLM 质检
    try:
        threshold = float(_config_value("catty_fdj_stat_critic_pass_score", 0.72) or 0.72)
    except (TypeError, ValueError):
        threshold = 0.72
    score, reasons = style_score(reply, scope=scope, technical=False)
    passed = score >= threshold
    if not passed:
        logger.debug(f"fdj stat critic hold: score={score:.2f}<{threshold:.2f} reasons={reasons}")
    return passed


def _looks_technical_query(user_text: str) -> bool:
    try:
        from . import fadianji_style_critic
        return bool(fadianji_style_critic._TECHNICAL_CONTEXT_RE.search(str(user_text or "")))
    except Exception:  # noqa: BLE001
        terms = ("报错", "代码", "配置", "部署", "接口", "脚本", "traceback", "bug")
        lowered = str(user_text or "").lower()
        return any(term in lowered for term in terms)


__all__ = ["stat_critic_passes", "style_score"]
