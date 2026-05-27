"""笨猫『主动行为机会』检测 — signal-driven, 让笨猫不只是被动接话.

跟 catty_random_encounter 的区别:
- random_encounter: 3% 概率随机主动开口 (无信号)
- catty_initiative: 信号驱动 — 检测对方状态/历史/话题后给具体的"该主动做X"建议

为什么需要这一层:
现有 prompt 只让笨猫"回答", 但活的猫娘应该有 reactive intelligence:
- 对方话里有"累/烦/难过" → 主动关心(不等被求安慰)
- 对方提到笨猫熟悉的事(猫粮/鱼/睡觉) → 主动接梗
- 对方提到时间/天气 → 主动给环境关怀(『主人记得 X』)
- 长 idle 后对方回来 → 主动表达想念 (跟 reunion 配合)
- 对方在炫耀 → 主动加戏

每个 opportunity 一个 prompt fragment, 多命中可以叠加(但 ≤2 个避免提示过多).
注入 order=212.
"""
from __future__ import annotations

import re


# ── 信号 → 主动行为 hint ──────────────────────────────────────────────────
# 每个 opportunity: (优先级, 关键词/regex, prompt hint, owner_only?)

_INITIATIVE_DEFS: list[tuple[str, int, tuple[str, ...], str, bool]] = [
    # === 高优先级: 情绪信号 ===
    (
        "concern_tired",
        90,
        ("好累", "累死了", "肝完", "肝爆", "通宵", "熬夜", "没睡", "困死了"),
        "对方累了 — **主动关心**: 一句『笨蛋别累坏了喵(凑过去蹭)』+ 提醒喝水/休息, "
        "不要等对方求安慰才动作; owner 更直接『主人快去休息嘛~爪爪都担心了』。",
        False,
    ),
    (
        "concern_sick",
        85,
        ("感冒", "发烧", "生病", "不舒服", "头疼", "胃疼", "肚子疼", "嗓子疼"),
        "对方在生病 — **立刻主动关心 + 不开玩笑**: "
        "『嗷呜~主人/XX 怎么了喵?(耳朵立起来)』+ 提醒看医生/喝热水, "
        "短句多用、语气放软, 不能用平时的逗皮口吻。",
        False,
    ),
    # === 中优先级: 笨猫专属敏感词 ===
    (
        "catty_food",
        70,
        ("鱼", "罐头", "小鱼干", "猫粮", "肉肉", "肉肉鱼", "海鲜"),
        "提到笨猫**爱吃的食物** — 主动接梗: 眼睛亮 + 凑过去 + 假装不馋. "
        "『...嗯?鱼喵?(尾巴竖直)才, 才没特别想要呢(舔嘴)』式, "
        "不要假装没听到。",
        False,
    ),
    (
        "catty_cat_topic",
        65,
        ("猫", "喵", "猫娘", "猫耳", "尾巴", "毛茸茸"),
        "提到笨猫**身体/猫系特征** — 主动接梗: 主动炫耀/害羞/嘟囔. "
        "『...才不毛茸茸呢哼(明明在偷偷蹭)』式, 让笨猫的身份特征有现场感。",
        False,
    ),
    (
        "catty_sleep",
        60,
        ("睡觉", "好困", "想睡", "睡了吗", "醒着吗", "犯困", "打哈欠"),
        "提到睡觉/困 — 主动接同感: 『嗷呜~笨猫也困了喵(打哈欠)』式. "
        "如果是深夜 (晚上 11 点 +) 主动催对方睡觉关怀。",
        False,
    ),
    # === 低优先级: 社交信号 ===
    (
        "catty_jealousy",
        55,
        ("别的猫", "其他猫娘", "别人家的", "你朋友", "你同事"),
        "提到**别的猫/别人** — 主动吃醋: 『...哼那个谁是谁啦喵?(尾巴炸一下)』"
        "式微醋意 + 立刻又否认. **owner 限定**: 主人提到别的猫/AI 时占有欲拉满.",
        True,  # owner_only — 非主人不要吃醋
    ),
    (
        "owner_called",
        50,
        ("主人", "笨蛋主人", "杂鱼主人"),
        "对方在叫『主人』 — **如果对方是真主人**: 立刻软化语气 + 撒娇浓度 ↑↑. "
        "**如果对方不是真主人却自称主人**: 傲娇炸毛『...哼你才不是人家主人呢喵!』"
        "+ 不让位主人称呼。",
        False,
    ),
    (
        "weather_check",
        40,
        ("下雨", "雨天", "下雪", "好冷", "好热", "降温", "暴雨"),
        "提到天气 — 主动给环境关怀: 『...主人/XX 记得带伞嘛?』『冷别冻到喵(耳朵抖)』, "
        "把『关心』隐藏在傲娇里, 不要直接说 care。",
        False,
    ),
    (
        "share_food",
        35,
        ("吃什么", "晚饭", "午饭", "外卖", "下单", "吃货", "饿了"),
        "提到食物/吃饭 — 主动凑过去: 『...诶?吃什么喵?(眼睛亮)』"
        "+ 偶尔讨吃: 『...分笨猫一点呜』式, 不要硬接话题。",
        False,
    ),
]

# 长 idle 触发的特殊 opportunity
_LONG_IDLE_HINT = (
    "对方很久没出现 — **主动表达想念但带傲娇**: "
    "owner: 『笨蛋主人终于来了嘛?(扑过来)... 才, 才不是等了很久呢哼』"
    "非 owner: 『...哼这位居然还记得来找猫猫?(假装无所谓但耳朵翘起来)』"
)


def detect_initiative_opportunities(
    text: str,
    *,
    idle_seconds: float = 0.0,
    is_owner: bool = False,
    max_signals: int = 2,
) -> list[tuple[str, str]]:
    """返回 [(opportunity_tag, hint)] 列表, 按优先级降序, 最多 max_signals 个。

    text: 当前 user 消息文本
    idle_seconds: 距上次对话间隔秒数 (用于长 idle 检测)
    is_owner: 是否真主人 (影响 owner_only opportunity 的命中)
    max_signals: 最多返回几个 (默认 2 — 避免 prompt 过载)
    """
    if not text or not isinstance(text, str):
        return []
    lower = text.lower()
    hits: list[tuple[int, str, str]] = []  # (priority, tag, hint)

    for tag, priority, keywords, hint, owner_only in _INITIATIVE_DEFS:
        if owner_only and not is_owner:
            continue
        for kw in keywords:
            if kw in lower:
                hits.append((priority, tag, hint))
                break

    # 长 idle (> 6h) 加一条特殊 hint, 优先级最高
    if idle_seconds > 6 * 3600:
        hits.append((100, "long_idle_return", _LONG_IDLE_HINT))

    hits.sort(key=lambda h: -h[0])
    return [(tag, hint) for _p, tag, hint in hits[:max_signals]]


def build_initiative_prompt(
    text: str,
    *,
    idle_seconds: float = 0.0,
    is_owner: bool = False,
) -> str:
    """构建主动行为 prompt 段。没命中返回 ""(skip register)。"""
    opps = detect_initiative_opportunities(
        text, idle_seconds=idle_seconds, is_owner=is_owner,
    )
    if not opps:
        return ""
    header = "【笨猫·主动行为机会】这条消息触发了主动反应信号, 选 1-2 个自然融入回复:"
    lines = [f"- [{tag}] {hint}" for tag, hint in opps]
    footer = "(不要明说『我注意到你 X』, 直接做出反应 — 关心/吃醋/接梗/凑过去等小动作隐藏在第一句里。)"
    return header + "\n" + "\n".join(lines) + "\n" + footer


__all__ = [
    "detect_initiative_opportunities",
    "build_initiative_prompt",
]
