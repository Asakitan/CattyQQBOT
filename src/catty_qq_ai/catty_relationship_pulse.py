"""笨猫『关系升降温』 — stateless 版本, 用 user_vibe_store 算最近 vibe 趋势.

跟 affection / affection_scorer 区别: 那俩看 long-term Lv + per-msg score,
本层看 user_vibe_store 内最近 N 条 vibe 的暖冷分布趋势, 判断"这一阵子" 是升温还是降温.

设计:
- pure function, 不读 store 不写 (caller 传 user_vibe_store profile)
- vibe history 暖冷分类:
  - 暖 vibes (升温信号): soft_care, celebratory, curious, playful, intimate_warming(虚构)
  - 冷 vibes (降温信号): complaint, short_dry(虚构 — 短回标记), serious(中性偏冷)
  - 中性: tease, gossip, braggart, nostalgia, techie
- 算 score: 暖 +1, 冷 -1, 中性 0; sum >= 3 = rising, <= -2 = cooling
- 注入 prompt hint: rising → 撒娇 ↑↑; cooling → 收着 + callback

注入位置: order=464 (user_vibe=460 / user_details=462 之后)
"""
from __future__ import annotations

from typing import Any


_WARM_VIBES: frozenset[str] = frozenset({
    "soft_care",        # 求安慰
    "celebratory",      # 庆祝兴奋
    "curious",          # 好奇求科普
    "playful",          # 玩闹
    "lewd_curious",     # 暧昧好奇 (跟笨猫升温信号)
})


_COLD_VIBES: frozenset[str] = frozenset({
    "complaint",        # 吐槽抱怨
    "serious",          # 认真讨论 (中性偏冷, 距离感)
})


def detect_pulse_from_vibe_history(vibe_history: list[str]) -> dict[str, Any]:
    """从 vibe_history (倒序最新在前) 算暖冷分数 → trend tag.

    返回 {trend: rising/stable/cooling, warm_count, cold_count, score}.
    """
    if not vibe_history:
        return {"trend": "stable", "warm_count": 0, "cold_count": 0, "score": 0}
    warm = sum(1 for v in vibe_history if v in _WARM_VIBES)
    cold = sum(1 for v in vibe_history if v in _COLD_VIBES)
    score = warm - cold
    if score >= 3:
        trend = "rising"
    elif score <= -2:
        trend = "cooling"
    else:
        trend = "stable"
    return {
        "trend": trend,
        "warm_count": warm,
        "cold_count": cold,
        "score": score,
    }


_PULSE_HINTS: dict[str, str] = {
    "rising": (
        "**关系正在升温** (近期暖 vibe ≥ 冷 vibe + 3) — "
        "笨猫**主动撒娇浓度 +1**: 主动贴贴/试探/暴露真心更多, 称呼亲密度 ↑↑. "
        "owner 场景下: 黏到极致; 非 owner 场景下: 比平时多一档主动."
    ),
    "cooling": (
        "**关系正在降温** (近期冷 vibe 多于暖 ≥ 2) — "
        "笨猫**收着撒娇**, 多顺毛 + 主动 callback 上次开心的话题, "
        "不要硬撩硬贴. 给对方喘息空间; 但也别冷漠 — 用『...笨猫陪着主人/XX 喵』式安静在场感."
    ),
    "stable": "",
}


def build_relationship_pulse_prompt(
    user_vibe_store: Any,
    user_id: str,
) -> str:
    """构建 pulse prompt 段. 拿 user_vibe_store.profile_for(uid) 内 vibe_history 算.

    stable 返回 "" 不打扰; 数据不足 (vibe_history 太短) 也返回 "".
    """
    if not user_vibe_store or not user_id:
        return ""
    try:
        profile = user_vibe_store.profile_for(user_id)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(profile, dict):
        return ""

    # profile 里没有 vibe_history 字段, 但 store 内 deque 是 vibe_history
    # 看 user_vibe.UserVibeStore — profile_for 返回 vibe_tag 和 topic_tags, 不直接给 history
    # 我直接读 store._data[user_id]['vibe_history'] 取 list
    try:
        with user_vibe_store._lock:  # noqa: SLF001
            rec = user_vibe_store._data.get(user_id)  # noqa: SLF001
            if not rec:
                return ""
            vibe_history = list(rec.get("vibe_history") or [])
    except Exception:  # noqa: BLE001
        return ""

    if len(vibe_history) < 4:
        return ""  # 数据太少, 别瞎判

    pulse = detect_pulse_from_vibe_history(vibe_history)
    trend = pulse.get("trend", "stable")
    if trend == "stable":
        return ""
    hint = _PULSE_HINTS.get(trend, "")
    if not hint:
        return ""
    return (
        f"【笨猫·关系动态 ({trend}, 暖={pulse['warm_count']} 冷={pulse['cold_count']})】\n"
        f"{hint}\n"
        "(从 user_vibe_store 最近 vibe 趋势推断, 跟 long-term affection Lv 互补。)"
    )


__all__ = [
    "detect_pulse_from_vibe_history",
    "build_relationship_pulse_prompt",
]
