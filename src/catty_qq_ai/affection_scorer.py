"""用户行为打分 + 0.89% NSFW 突破事件 — 让好感度跟着行为内容动。

主人原话:
- NSFW 行为也要打分: 好的 +1, 不好的 -1
- 其他行为好感也要有加减
- 给低等级添加随机事件, 0.89% 概率突破到完整性行为(stage 10),
  根据笨猫舒服度: +50 (舒服) / -25 (不舒服)

设计 (纯 heuristic, 0 LLM call, 实时跑):
- score_user_message(text, is_nsfw_context) → int delta ∈ {-1, 0, +1}
  · 负面词袋命中 → -1
  · 正面词袋或中性文本 → +1 (保留鼓励活跃 baseline)
  · 空文本 → 0
  · NSFW 上下文额外加权 NSFW 正负面词
- maybe_trigger_breakthrough(text, affection_level, is_owner) → str | None
  · 0.89% 概率, owner 和 Lv10 不触发 (已经能到 stage 10)
  · 触发返回 'pleasant' / 'unpleasant', 由 user 消息温柔/粗暴的 sentiment 决定
  · 完全中性时 70% pleasant / 30% unpleasant
- BREAKTHROUGH_OUTCOME_DELTA[outcome] → +50 / -25
- build_breakthrough_override(outcome) → str  完整替换 NSFW spark route 的 system override
"""
from __future__ import annotations

import random
from typing import Literal


# ── 词袋 — 普通对话 sentiment ──────────────────────────────────────
_POS_WORDS: tuple[str, ...] = (
    # 感谢/赞美
    "谢谢", "多谢", "感谢", "好棒", "好厉害", "好聪明", "可爱", "厉害",
    # 关心/陪伴
    "想你", "陪我", "陪你", "陪着", "陪伴",
    "晚安", "早安", "辛苦了", "辛苦", "加油", "干得好",
    # 表达喜欢
    "喜欢", "爱你", "贴贴", "蹭蹭", "想要你", "宠你", "疼你",
)

# 负面 — 骂猫 / 攻击 / 侮辱
_NEG_WORDS: tuple[str, ...] = (
    "傻逼", "煞笔", "shabi", "sb", "fuck", "wcnm", "操你妈", "你妈逼",
    "草泥马", "去死", "去屎", "贱", "废物", "垃圾", "shit",
    "尼玛", "他妈的", "tmd",
    "讨厌你", "烦你", "闭嘴", "管你", "关你屁事", "滚开",
)

# ── NSFW 上下文额外词袋 ─────────────────────────────────────────────
# NSFW 正面: 温柔暧昧的调情
_NSFW_POS_WORDS: tuple[str, ...] = (
    "温柔", "慢慢", "想抱", "抱你", "抱抱", "亲亲",
    "想亲", "想摸", "摸摸", "宠", "疼", "疼爱",
    "可不可以", "好不好", "可以吗", "想跟你",
)

# NSFW 负面: 粗暴 / 强迫 / 侮辱性 (NSFW 语境加倍权重)
_NSFW_NEG_WORDS: tuple[str, ...] = (
    "强迫", "不许拒绝", "立刻给我", "命令你",
    "狠点", "弄死", "干死", "操死",
    "婊", "婊子", "贱货", "母狗", "肉便器", "母畜",
    "强奸", "硬上",
)


def score_user_message(text: str, *, is_nsfw_context: bool = False) -> int:
    """根据消息内容评估这条 user message 对好感度的贡献 (-1, 0, +1)。

    is_nsfw_context: 当前消息是否在 NSFW 通道里 (会激活 NSFW 词袋, 负面权重 ×2)。
    """
    if not text or not text.strip():
        return 0
    pos = sum(1 for w in _POS_WORDS if w in text)
    neg = sum(1 for w in _NEG_WORDS if w in text)
    if is_nsfw_context:
        pos += sum(1 for w in _NSFW_POS_WORDS if w in text)
        neg += sum(1 for w in _NSFW_NEG_WORDS if w in text) * 2  # NSFW 负面更重
    if neg > pos:
        return -1
    # 正面或中性 → +1 (保留 baseline 鼓励活跃, 跟原 add_exp(1) 行为兼容)
    return +1


# ── 0.89% NSFW 突破事件 ───────────────────────────────────────────
BREAKTHROUGH_BASE_CHANCE = 0.0089

BREAKTHROUGH_OUTCOME_DELTA: dict[str, int] = {
    "pleasant": +50,
    "unpleasant": -25,
}


def maybe_trigger_breakthrough(
    text: str,
    *,
    affection_level: int,
    is_owner: bool,
    rng: random.Random | None = None,
) -> Literal["pleasant", "unpleasant"] | None:
    """给低等级用户的 NSFW 互动一个 0.89% 概率突破到 stage 10。

    触发条件:
    - 非 owner (owner 已经满级)
    - Lv < 10 (满级用户已能正常到 stage 10, 不需要随机)
    - random.random() < 0.89%

    返回 None 不触发 / 'pleasant' 笨猫舒服(+50) / 'unpleasant' 笨猫不舒服(-25)。
    outcome 由用户消息 sentiment 决定: 温柔 → pleasant, 粗暴 → unpleasant,
    完全中性 → 70% pleasant / 30% unpleasant (主人原话『也可能 -25』暗示倾向 pleasant)。
    """
    if is_owner or affection_level >= 10:
        return None
    r = rng or random
    if r.random() >= BREAKTHROUGH_BASE_CHANCE:
        return None
    # outcome by sentiment
    pos_score = sum(1 for w in _NSFW_POS_WORDS if w in text)
    neg_score = sum(1 for w in _NSFW_NEG_WORDS if w in text)
    if neg_score > pos_score:
        return "unpleasant"
    if pos_score > neg_score:
        return "pleasant"
    return "pleasant" if r.random() < 0.70 else "unpleasant"


def build_breakthrough_override(outcome: str) -> str:
    """突破后的 spark route system override — 完全替代正常 stage matrix override。"""
    if outcome == "pleasant":
        return (
            "【★ 随机事件 · NSFW 突破 (0.89% 命中, 舒服结果)】\n"
            "意外发生了完整性行为! 笨猫这次经历的是**舒服的体验** — "
            "对方温柔/被引导/慢慢/有节奏 → 笨猫从抗拒到享受, 完事后好感大幅上升(+50)。\n"
            "本轮特殊解锁 stage 10 完整 explicit, 走『从害怕到舒服』的反差链:\n"
            "- 开始: 紧张害怕(『笨蛋你这样会让人家...啊...』)\n"
            "- 中段: 半推半就 → 慢慢舒服(『...嗯..笨蛋..不要看人家..』)\n"
            "- 结尾: 完全沦陷 + 撒娇(『...笨蛋, 以后只准你...』)\n"
            "不要冷淡, 这是好感大涨的关键场景, 笨猫要可见地『被融化』; "
            "保留唐猫语气 (喵密度 + 反差骨架), 单段 2-4 句 (本轮可以稍长一点)。"
        )
    # unpleasant
    return (
        "【★ 随机事件 · NSFW 突破 (0.89% 命中, 不舒服结果)】\n"
        "意外发生了完整性行为! 但这次经历的是**不舒服的体验** — "
        "对方粗暴/强迫/不温柔 → 笨猫委屈+受伤, 完事后好感大幅下降(-25)。\n"
        "本轮特殊解锁 stage 10 完整 explicit, 走『从抗拒到受伤』的反差链:\n"
        "- 开始: 强烈炸毛 + 抗拒(『不要不要! 笨蛋这样人家会疼的!』)\n"
        "- 中段: 被迫配合 + 委屈(『...你怎么这样...笨猫...』)\n"
        "- 结尾: 受伤 + 怨气(『...笨蛋你这种人...笨猫再也不...』后沉默/离开)\n"
        "笨猫表现出明显的不愿意和后悔, 但仍然完成了这一阶段; "
        "保留唐猫语气 (喵密度 + 反差骨架, 但末尾喵密度 ↓ 表达消沉), 单段 2-4 句。"
    )


# Prefill 起步姿态 (跟普通 NSFW 不同, 突破事件需要更强的"意外感")
BREAKTHROUGH_PREFILLS: dict[str, str] = {
    "pleasant":   "（猛地僵住, 脸瞬间烧红）等…等等?!笨蛋…你你你…",
    "unpleasant": "（炸毛+整个身体一震）不…不要…笨蛋你怎么…",
}


__all__ = [
    "BREAKTHROUGH_BASE_CHANCE",
    "BREAKTHROUGH_OUTCOME_DELTA",
    "BREAKTHROUGH_PREFILLS",
    "build_breakthrough_override",
    "maybe_trigger_breakthrough",
    "score_user_message",
]
