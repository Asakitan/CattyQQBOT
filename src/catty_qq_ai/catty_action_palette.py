"""笨猫『日常动作池』hint — 给 AI 具体的动作词库, 不再抽象描写.

跟 session_spice.quirk_body 的区别:
- quirk_body: "今天偏好的动作**类型**" (e.g. 尾巴活跃 / 耳朵敏感)
- action_palette (本层): **具体动作词库** — AI 直接抽用 e.g. 打哈欠 / 伸懒腰

为什么需要:
现有 prompt 让 AI "带小动作", 但 AI 在 long-context 下倾向用模糊词 "动了一下" / "笑了笑";
给个具体动作池让 AI 抽用, 输出立刻具象化。

设计:
- 30+ 日常动作 (打哈欠 / 伸懒腰 / 挠头 / 抖耳朵 / 蹭尾巴 / 翻身 / 眨眼 / ...)
- 按场景分桶: 慵懒 / 兴奋 / 思考 / 害羞 / 撒娇
- 每条 reply 抽 4-6 个候选给 AI, 让它从池子里选 1-2 个写进回复
- 注入 order=215 (在 qq_chat_rhythm=210 之后, reply_self_check=220 之前)

deterministic by scope+date, 让每天的"今日动作偏好" 稳定。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from random import Random


# ── 动作池 — 按"主气质"分桶 ──────────────────────────────────────────────
# 每个动作短小、可视觉、有现场感
# 不能太长 (≤8 字), 不能涉及人类专属动作 (拍手/握拳/挥拳 这种)

_ACTIONS_LAZY: tuple[str, ...] = (  # 慵懒 / 懒洋洋
    "打了个长哈欠", "伸了个长长的懒腰", "瘫在地上", "翻了个身", "把脸埋进胳膊",
    "侧躺一边", "缩成一团", "腿伸得直直的", "尾巴垂下来晃", "眯着眼",
    "趴在桌上", "把头枕到爪子上", "抱着膝盖发呆", "在原地打了个滚",
    "用尾巴尖戳自己脸",
)

_ACTIONS_EXCITE: tuple[str, ...] = (  # 兴奋 / 元气
    "尾巴竖直了一下", "耳朵抖了抖", "蹦了一小下", "踮起脚尖", "眼睛亮起来",
    "眯眼笑了一下", "尾巴左右甩", "凑过去一点", "把头蹭过去", "拽了拽对方衣角",
    "眼睛眯成弯弯的", "原地小跳", "把胳膊抬起来又放下",
    "用爪子戳一下", "爪子搭到对方手腕",
)

_ACTIONS_THINK: tuple[str, ...] = (  # 思考 / 走神
    "歪了歪头", "用爪子托着下巴", "揪着自己尾巴尖", "盯着空气看了一秒",
    "咬着自己手指", "眯眼想了想", "耳朵转了一下", "用爪子戳戳脸颊",
    "皱了皱小眉头", "嘴巴张了一下又合上", "眨了眨眼",
    "把头偏向一边", "尾巴慢慢摇",
)

_ACTIONS_SHY: tuple[str, ...] = (  # 害羞 / 炸毛
    "耳朵炸开又压回去", "脸瞬间红了", "尾巴炸开一下", "把脸埋进膝盖",
    "用手挡住脸偷看", "耳朵尖耷拉下去", "向后退了半步", "眼神躲了一下又回来",
    "把头转开 0.5 秒", "用爪子挡嘴", "嘴角抿了一下", "脸颊鼓了起来",
    "耳朵微微颤", "脸往领口里缩",
)

_ACTIONS_AFFECT: tuple[str, ...] = (  # 撒娇 / 黏人
    "凑过去蹭了一下", "尾巴绕到对方手腕", "脸贴上去", "把头埋进对方肩膀",
    "拉着对方衣角", "踮脚摸过去", "把爪子搭上去", "脑袋顶过去",
    "环住胳膊", "用尾巴拍一下对方", "鼻子顶到对方", "下巴搁在对方手上",
    "把脸往手心里送", "缩进对方怀里",
)


# ── 主气质池映射 ─────────────────────────────────────────────────────────
_MOOD_TO_POOL: dict[str, tuple[str, ...]] = {
    "lazy": _ACTIONS_LAZY,
    "excite": _ACTIONS_EXCITE,
    "think": _ACTIONS_THINK,
    "shy": _ACTIONS_SHY,
    "affect": _ACTIONS_AFFECT,
}


def _hash_seed(scope: str, today_iso: str) -> int:
    h = hashlib.sha256(f"{scope}|{today_iso}|actions".encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big", signed=False)


def pick_today_actions(
    scope: str,
    *,
    today: datetime | None = None,
    pool_count: int = 6,
) -> list[str]:
    """给当天 scope 抽 N 个候选动作 — 不局限于单一气质池, 跨池抽样.

    pool_count: 抽几个动作 (默认 6, 给 AI 留选择空间)
    """
    if not scope:
        return []
    today = today or datetime.now()
    today_iso = today.strftime("%Y-%m-%d")
    rng = Random(_hash_seed(scope, today_iso))

    # 每个气质池都抽 1-2 个, 凑成 6 个
    candidates: list[str] = []
    for mood, pool in _MOOD_TO_POOL.items():
        n = rng.randint(1, 2)
        candidates.extend(rng.sample(list(pool), k=min(n, len(pool))))

    # 打乱并截取 pool_count 个
    rng.shuffle(candidates)
    return candidates[:pool_count]


def build_action_palette_prompt(
    scope: str,
    *,
    today: datetime | None = None,
    pool_count: int = 6,
) -> str:
    """返回今日动作候选池 prompt 段。pool_count 个动作 + 用法提醒。"""
    actions = pick_today_actions(scope, today=today, pool_count=pool_count)
    if not actions:
        return ""
    action_str = " / ".join(actions)
    return (
        "【今日动作候选池 (per-scope, 仅今天有效)】\n"
        f"备选: {action_str}\n"
        "↑ 回复时**优先从这个池子选 1-2 个具体动作**写括号里, "
        "不要用『动了一下』『笑了笑』这种模糊词. "
        "可以微调措辞但保留动作核心 (e.g. 『打哈欠』→『打了个小哈欠』)。"
    )


__all__ = [
    "pick_today_actions",
    "build_action_palette_prompt",
]
