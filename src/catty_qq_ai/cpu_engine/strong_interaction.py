"""强互动判定 (S3.2): 必须走 DeepSeek 的场景检测.

四个触发条件 (任一命中即"强互动", 强制透传到 L5 主链路并扣积分):
1. NSFW phase >= P3 (米雪儿性情已触发)
2. intent ∈ {tease_cat, compliment_cat, 表白} (高情感价值)
3. emotion_intensity > 0.7 (用户哭/吐槽/生气, CPU 模板会失人格)
4. cpu_confidence < 0.7 (CPU 层不自信, 不能用模糊回复糊弄)

主人 2026-05-28 plan: 主人特权 #ai 强制 DeepSeek 在 router 入口前已处理,
本模块只判"不是主人显式强制时, 是否还该走 DeepSeek".

emotion_intensity 用纯规则: 情绪词命中数 + 标点密度.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_EMOTION_LEXICON: dict[str, float] = {
    "哈哈": 0.4, "笑死": 0.6, "嘻嘻": 0.3, "笑死我": 0.7,
    "生气": 0.6, "气死": 0.8, "气炸": 0.8, "烦死": 0.7, "烦": 0.4,
    "讨厌": 0.5, "妈的": 0.6, "卧槽": 0.5, "操": 0.4, "草": 0.3,
    "fuck": 0.6, "shit": 0.5, "啊啊": 0.6,
    "哭": 0.7, "呜": 0.5, "呜呜": 0.7, "难过": 0.6, "委屈": 0.7,
    "心碎": 0.7, "好惨": 0.6, "崩溃": 0.7, "扛不住": 0.6,
    "喜欢你": 0.8, "爱你": 0.8, "想你": 0.7, "亲亲": 0.6, "抱抱": 0.5,
    "贴贴": 0.5, "rua": 0.5, "嘿嘿": 0.4,
    "好棒": 0.5, "牛逼": 0.5, "厉害": 0.4, "绝了": 0.5, "yyds": 0.5,
}


@dataclass(slots=True)
class StrongInteractionResult:
    is_strong: bool
    reason: str
    emotion_intensity: float
    triggered_by: list[str] = field(default_factory=list)


def detect_emotion_intensity(text: str) -> float:
    """0.0-1.0 情绪强度. 词典命中权重 + 标点密度."""
    if not text:
        return 0.0
    lower = text.lower()
    word_score = 0.0
    for word, weight in _EMOTION_LEXICON.items():
        if word in lower:
            word_score += weight
    bang_count = text.count("!") + text.count("！") + text.count("?") + text.count("？")
    ellipsis_count = text.count("...") + text.count("…")
    punct_score = bang_count * 0.12 + ellipsis_count * 0.08
    length_norm = max(len(text) / 30.0, 1.0)
    raw = (word_score + punct_score) / length_norm
    return min(raw, 1.0)


def is_strong_interaction(
    *,
    text: str,
    intent: str,
    cpu_confidence: float,
    nsfw_phase: int,
    config: Any,
) -> StrongInteractionResult:
    """聚合判定. 任一触发即返回 is_strong=True 并列出 triggered_by."""
    triggered: list[str] = []

    nsfw_threshold = int(getattr(config, "catty_strong_nsfw_phase_threshold", 3))
    if nsfw_phase >= nsfw_threshold:
        triggered.append(f"nsfw_phase={nsfw_phase}>=P{nsfw_threshold}")

    strong_intents = set(getattr(config, "catty_strong_intents", []) or [])
    if intent and intent in strong_intents:
        triggered.append(f"intent={intent}")

    emotion = detect_emotion_intensity(text)
    emotion_threshold = float(getattr(config, "catty_strong_emotion_intensity_threshold", 0.7))
    if emotion >= emotion_threshold:
        triggered.append(f"emotion={emotion:.2f}")

    conf_threshold = float(getattr(config, "catty_strong_cpu_confidence_threshold", 0.7))
    if cpu_confidence < conf_threshold:
        triggered.append(f"low_conf={cpu_confidence:.2f}")

    is_strong = bool(triggered)
    return StrongInteractionResult(
        is_strong=is_strong,
        reason="|".join(triggered) if triggered else "",
        emotion_intensity=emotion,
        triggered_by=triggered,
    )
