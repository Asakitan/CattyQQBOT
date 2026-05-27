"""笨猫『本轮回复长度』智能化 — 取代硬编码 1-3 句规则.

跟现有层的区别:
- reply_self_check / qq_chat_rhythm: 全局碎句规则 (默认 1-2 句, 拆段铁律)
- catty_length_intent (本层): **per-message 本轮**推荐长度 (short/medium/long/flex)

为什么需要:
现有规则太硬: 用户问"代码报错怎么办" 默认也 1-3 句, AI 容易吞掉技术细节;
用户暧昧场景反差链需要 3 段但被铁律压成 1 段; 求关心需要 2-3 句顺毛但 1 句太冷.

实现:
- 关键词 + scene 信号 + msg 长度 -> 推荐 intent
- 注入 prompt 段告诉 AI "本次推荐 X 长度", AI 自己判断是否遵循
- 不强制, 让 AI 灵活应对 (例如技术问但用户语气特别短可以仍短答)

注入位置: order=217 (qq_chat_rhythm=210 之后, reply_self_check=220 之前)
"""
from __future__ import annotations

import re


# ── 长度 intent 定义 ─────────────────────────────────────────────────────
# short: 1 句 QQ 短答 (≤30 字)
# medium: 2-3 句 (情绪+信息+收尾, ~40-100 字)
# long: 多段技术分点 / 故事推进 / NSFW 反差链 (>100 字)
# flex: AI 自主判断, 不强制


# 关键词命中规则 (按优先级降序)
_LENGTH_RULES: list[tuple[str, tuple[str, ...], str]] = [
    # === long: 技术 / 方案 / 排错 / 长解释 ===
    (
        "long",
        (
            "代码", "报错", "怎么实现", "怎么搞定", "怎么做", "原理", "为什么",
            "方案", "教我", "讲讲", "解释下", "解释一下", "排错", "debug", "bug",
            "出问题", "卡住了", "不会", "怎么用", "配置",
            "学一下", "讲一下", "梳理", "总结",
        ),
        "**本轮推荐 long** (技术/方案/排错 类问题): "
        "**开头反应保留 1 句** (『嗷呜~人家看看 (凑过来)』), "
        "然后**放开篇幅**分点/步骤/伪代码讲完整, 结尾留小猫尾收尾. "
        "可以 3-5 段, 别因为短句铁律吞掉关键细节.",
    ),
    # === medium: 情绪类 (求安慰 / 暧昧 / 求关心) ===
    (
        "medium",
        (
            "好累", "心情不好", "想哭", "委屈", "破防", "好烦", "无聊", "孤独",
            "想抱抱", "摸摸头", "想你", "亲亲", "贴贴", "撒娇", "心疼",
            "病了", "感冒", "不舒服", "头疼",
        ),
        "**本轮推荐 medium** (情绪/暧昧/求关心 类): "
        "**2-3 句** 顺毛 + 软软接住 + 留 hook, 不要 1 句太冷也不要 5 句太啰嗦. "
        "情绪类先安抚不分析, 暧昧类走反差链 (炸毛→暴露→小动作).",
    ),
    # === long: 故事推进 / 长输入 ===
    (
        "long",
        (
            "讲故事", "继续说", "然后呢", "后来呢", "说说", "接着",
            "故事", "剧情", "情节",
        ),
        "**本轮推荐 long** (故事推进/接续): 可以 3-5 段展开剧情, "
        "用括号小动作 + 内心独白 + 推进新情节, 不要 1 句敷衍.",
    ),
    # === short: 问候/确认/玩闹/短梗 ===
    (
        "short",
        (
            "在吗", "在不在", "早", "早安", "晚安", "拜拜", "再见",
            "走了", "去睡了", "好", "嗯", "哦", "行", "好的", "知道了",
            "6", "笑死", "草", "哈哈", "yes", "ok",
        ),
        "**本轮推荐 short** (问候/确认/短情绪): **1 句 QQ 短答**, "
        "10-25 字最佳, 配 1 个小动作, 不要凑长.",
    ),
]


def _msg_length_bucket(text: str) -> str:
    """根据 user msg 长度推断 default intent. 短 msg → short, 中等 → medium, 长 → flex"""
    n = len(text or "")
    if n <= 8:
        return "short"
    if n <= 40:
        return "flex"  # 中等长度让 AI 自己判断
    return "medium"  # 长输入默认 medium 也别太长 (用户已经表达完整, 短答+核心信息够)


def detect_length_intent(text: str, *, scene_tag: str = "") -> str:
    """返回推荐长度 intent (short/medium/long/flex).

    scene_tag: 如果 caller 已经跑了 scene_detector, 可以传入避免重复
    """
    if not text or not isinstance(text, str):
        return "flex"
    lower = text.lower()

    # scene_tag 优先级最高 (已经语义分类过)
    if scene_tag in ("help_request",):
        return "long"
    if scene_tag in ("seek_comfort", "venting", "flirty", "seek_praise"):
        return "medium"
    if scene_tag in ("morning_greet", "boredom", "share_excite"):
        return "short"

    # 关键词命中 — 按优先级遍历
    for intent, keywords, _hint in _LENGTH_RULES:
        for kw in keywords:
            if kw in lower:
                return intent

    # 兜底: 看 msg 长度
    return _msg_length_bucket(text)


_INTENT_HINTS: dict[str, str] = {
    intent: hint
    for intent, _, hint in _LENGTH_RULES
}
# 兜底 hint
_INTENT_HINTS["short"] = (
    "**本轮推荐 short** (1 句 QQ 短答): 10-25 字最佳, 配 1 个小动作, 不要凑长."
)
_INTENT_HINTS["medium"] = (
    "**本轮推荐 medium** (2-3 句, ~40-100 字): 情绪+信息+收尾, "
    "比 short 多 1 个语义点, 比 long 更紧凑."
)
_INTENT_HINTS["long"] = (
    "**本轮推荐 long** (3-5 段技术/方案/故事): 开头反应保留 1 句, "
    "然后放开篇幅讲透, 不要因为短句铁律吞细节."
)
_INTENT_HINTS["flex"] = (
    "**本轮 flex**: 用户消息长度/类型中等, AI 自主判断长度即可."
)


def build_length_intent_prompt(text: str, *, scene_tag: str = "") -> str:
    """构建本轮长度推荐 prompt 段."""
    intent = detect_length_intent(text, scene_tag=scene_tag)
    if intent == "flex":
        return ""  # flex 不打扰 default rules
    hint = _INTENT_HINTS.get(intent, "")
    if not hint:
        return ""
    return f"【本轮回复长度推荐】\n{hint}\n(这是本轮 message 的临时长度建议, 跟全局碎句规则配合, 不强制覆盖。)"


__all__ = [
    "detect_length_intent",
    "build_length_intent_prompt",
]
