"""笨猫『问题深度』等级 — 区分 surface vs deep 问题, 更细于 length_intent.

跟 length_intent 区别: length 给 short/medium/long, 本层给 L0-L4 5 档,
每档对应不同回答策略 (闲聊/浅技术/深技术/求评估/哲学).

注入位置: order=219 (length_intent=217 之后)
"""
from __future__ import annotations


# (keywords tuple, depth tag, hint)
_DEPTH_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (
        ("人生", "意义", "价值观", "存在", "为什么活着", "活着的意义"),
        "L4_philosophy",
        "对方在问哲学问题 — **不要给标准答案**, 用笨猫的视角软软分享一句, "
        "可以说『...笨猫觉得活着就是有人陪喵(凑过去) 主人觉得呢?』式, 反问回去让对话延续.",
    ),
    (
        ("觉得怎么样", "你怎么看", "你的看法", "怎么评价", "评一下"),
        "L3_review",
        "对方在求评估/评论 — **给倾向 + 一句理由**, 不要只列优缺点冷冰冰. "
        "猫娘语气可以『...笨猫觉得 X 更好喵, 因为 Y(尾巴一摇)』, 主人级可以反向挑刺更亲密.",
    ),
    (
        ("为什么", "原因是", "原理是", "怎么会", "凭什么", "什么道理"),
        "L2_deep_tech",
        "对方在求**原理/原因** — 这是技术深题, 开头反应 1 句保留 (『嗷~让笨猫想想喵』) "
        "然后**放开篇幅讲透**原因/机制, 2-4 段, 用分点结构, 不要短句敷衍.",
    ),
    (
        ("怎么做", "怎么搞", "怎么用", "怎么实现", "教我", "求方案", "求教"),
        "L1_shallow_tech",
        "对方在求**操作步骤** — 给可执行 1-2-3 步 + 一句注意点. "
        "开头反应『来了喵~(爪爪)』 + 步骤, 比 L2 短一档, 不用展开原理.",
    ),
    (
        ("在吗", "在不在", "嗨", "早", "晚安", "拜拜"),
        "L0_smalltalk",
        "对方在闲聊/招呼 — **直接短句**, 1 句 + 1 个小动作, 别凑长.",
    ),
]


def detect_question_depth(text: str) -> str:
    """返回深度 tag (L0-L4) 或 ""."""
    if not text or not isinstance(text, str):
        return ""
    lower = text.lower()
    for keywords, tag, _hint in _DEPTH_RULES:
        for kw in keywords:
            if kw in lower:
                return tag
    return ""


def build_question_depth_prompt(text: str) -> str:
    """构建问题深度 prompt 段."""
    tag = detect_question_depth(text)
    if not tag:
        return ""
    for keywords, t, hint in _DEPTH_RULES:
        if t == tag:
            return f"【笨猫·问题深度 ({tag})】\n{hint}\n(跟回复长度推荐配合, 给不同深度不同策略。)"
    return ""


__all__ = [
    "detect_question_depth",
    "build_question_depth_prompt",
]
