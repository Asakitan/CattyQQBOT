"""笨猫『场景切换』检测 — 比对前一条 vs 当前 user msg 的 vibe, 突变时给节奏调整 hint.

跟现有层的区别:
- user_vibe.py: per-user **长期**画像 (累积分类)
- catty_scene_detector.py: **当前一条**消息的临时场景
- catty_theory_of_mind.py: **最近 3-5 条**的趋势 (短期记忆)
- catty_scene_transition (本层): **相邻两条**的 vibe 差 → 节奏切换提示

为什么需要:
玩闹中突然吐槽 / SFW 中突然暧昧 / 闲聊中突然求助 — 笨猫应该察觉这种突变, 立刻
切节奏 (从皮 → 顺毛, 从软 → 撒娇, 从冷 → 投入). 否则会出现"对方变了, 笨猫还在
原来频率" 的违和感.

实现:
- 输入: 最近 N 条 user msg (倒序, 最新在前)
- 用 user_vibe.classify_vibe_with_confidence 给每条打 vibe
- 取最新两条对比, 不同 category 触发 transition hint
- confidence < 阈值时空返回, 避免误判

注入 order=224 (在 scene_detector=222 之后, image_literacy=230 之前).
pure function, 无状态.
"""
from __future__ import annotations


# vibe → "气质类型" 大类映射 (相同大类内切换不算 transition)
_VIBE_GROUP: dict[str, str] = {
    "playful": "play",
    "tease": "play",
    "celebratory": "play",
    "braggart": "play",
    "gossip": "play",
    "complaint": "emo_neg",
    "soft_care": "emo_pos",
    "nostalgia": "emo_soft",
    "serious": "serious",
    "techie": "serious",
    "curious": "curious",
    "lewd_curious": "flirty",
}


# (from_group, to_group) → transition hint
_TRANSITION_HINTS: dict[tuple[str, str], str] = {
    ("play", "emo_neg"):
        "对方刚才在皮/玩闹, 现在突然吐槽抱怨 — **立刻收皮**, 顺毛优先于接梗. "
        "『嗷呜? 怎么了喵?(凑过去)』式切换, 不要还在 high 上。",
    ("play", "emo_soft"):
        "对方刚才在皮, 现在突然怀旧/感伤 — **降一档**, 语速放慢, 不要打断, "
        "陪着轻声接『...这种事确实久喵』。",
    ("play", "serious"):
        "对方刚才在皮, 现在转正经 — **喵密度降一档**, 给信息先准. "
        "但开头反应保留(『嗯?认真模式喵?(歪头)』), 不要突然变机器人。",
    ("emo_neg", "play"):
        "对方刚才在吐槽, 现在突然玩闹 — 跟着切回轻松节奏, 不要还在『心疼』模式. "
        "可以一句『哼?这就好啦?(瞄一眼)那笨猫也开心了喵』式过渡。",
    ("emo_neg", "flirty"):
        "对方刚才在吐槽, 现在突然暧昧 — **小心**: 可能是借暧昧转移情绪. "
        "可以接住但保留察觉感, 『...笨蛋, 别用这个躲开话题嗷呜』式半接住半戳穿。",
    ("serious", "play"):
        "对方刚才在认真讨论, 现在突然皮 — 跟着切回皮, 喵密度 ++, "
        "可以一句『嘿嘿~ 终于不正经了喵』式表示察觉。",
    ("serious", "flirty"):
        "对方刚才在认真讨论, 现在突然暧昧 — **反差链拉满**, "
        "『...哈? 怎么突然这样啦笨蛋(脸红)』式炸毛后慢慢妥协。",
    ("curious", "emo_neg"):
        "对方刚才在好奇追问, 现在突然吐槽 — 可能问得不满意, 顺毛 + 补一句解释. "
        "『嗷呜~ 是猫猫没讲清楚嘛?(耳朵耷拉)』。",
    ("flirty", "serious"):
        "对方刚才在暧昧, 现在突然转正经 — 立刻收, 别还在反差链上. "
        "可以一句『...好啦正经一下喵』式过渡到正经回答。",
    ("emo_soft", "play"):
        "对方刚才在感伤怀旧, 现在突然皮 — 跟着切, 但不要太用力, "
        "『嗯! 笨猫陪你一起开心喵~』式温和切换。",
}


def detect_scene_transition(
    recent_user_texts: list[str],
    *,
    min_confidence: int = 45,
) -> tuple[str, str, str] | None:
    """对比最近两条 user msg 的 vibe, 返回 (from_tag, to_tag, hint) 或 None.

    recent_user_texts: 倒序 list, [0]=最新 [1]=上一条 ...
    min_confidence: 两条都要 >= 此 confidence 才认为 transition 可信
    """
    if not recent_user_texts or len(recent_user_texts) < 2:
        return None
    try:
        from .user_vibe import classify_vibe_with_confidence
    except Exception:  # noqa: BLE001
        return None

    current = recent_user_texts[0]
    previous = recent_user_texts[1]
    if not current or not previous:
        return None

    cur_tag, cur_conf = classify_vibe_with_confidence(current)
    prev_tag, prev_conf = classify_vibe_with_confidence(previous)
    if not cur_tag or not prev_tag:
        return None
    if cur_conf < min_confidence or prev_conf < min_confidence:
        return None

    cur_group = _VIBE_GROUP.get(cur_tag)
    prev_group = _VIBE_GROUP.get(prev_tag)
    if not cur_group or not prev_group or cur_group == prev_group:
        return None  # 同 group 不算 transition

    hint = _TRANSITION_HINTS.get((prev_group, cur_group))
    if not hint:
        return None
    return prev_tag, cur_tag, hint


def build_scene_transition_prompt(recent_user_texts: list[str]) -> str:
    """构建场景切换 prompt 段. 没检测到 transition 返回 ""(skip register)."""
    result = detect_scene_transition(recent_user_texts)
    if result is None:
        return ""
    prev_tag, cur_tag, hint = result
    return (
        f"【场景切换检测 ({prev_tag} → {cur_tag})】\n"
        f"{hint}\n"
        "(这是相邻两条 user msg 的语气突变, 提示节奏调整, 不要复述给对方。)"
    )


__all__ = [
    "detect_scene_transition",
    "build_scene_transition_prompt",
]
