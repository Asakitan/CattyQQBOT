"""笨猫『久别重逢』prompt — idle 时长 → 反差化重逢语气提示。

跟 daily_life / catty_goals / catty_mood 的分工:
- daily_life: 今天笨猫的状态(活动 / 心情底色)
- catty_goals: 今天笨猫的意图(对话目标)
- catty_mood:  跨多轮累积的实时情绪
- catty_reunion: **久别重逢瞬间反差** — 长时间没说话,这一刻再来一句的特殊感

为什么需要这一层:
session 短时连续聊不需要"刚见到"的反差,但**6 小时以上**没说话再来一句,
笨猫应该有可见的"反应不同" — 不该跟刚才那条无缝衔接, 该有"诶?(耳朵竖)"
"...终于想起人家了喵?(假装无所谓)"等久违感, 这才是『活的角色』。

这是 ST 没有的 catty 特化能力 — ST 的 char 是 stateless 的, 不会感知"上次对话
多久前", catty 用 session_cache.last_access_at 已经有这个信号, 这层只是把信号
翻译成 prompt hint。

四档划分:
- warm     (idle  < 6h):    不注入(连续对话, 不需要重逢感)
- brief    (6h-24h):        小停顿后重启, 反应慢半拍但开心
- days     (24h-7d):        几天没见, 傲娇撒娇『终于想起人家了喵?』
- long     (7d+):           很久没见, 可以撒娇式抱怨『再不来人家要去找别人玩啦』

deterministic by idle 时长 — 不抽随机, 也不持久化, pure function。
"""
from __future__ import annotations

import time
from typing import Any


# 阈值(秒)— 跟主人对此类反差感的预期对齐, 别太敏感(< 6h 没必要触发, session 还热着)
_THRESHOLD_BRIEF_S = 6 * 3600          # 6 小时
_THRESHOLD_DAYS_S = 24 * 3600          # 1 天
_THRESHOLD_LONG_S = 7 * 24 * 3600      # 1 周

# Hint 模板 — 主人池跟普通用户池分开, 主人池可以更亲密撒娇
_HINTS_OWNER: dict[str, str] = {
    "brief": (
        "刚才一段没说话(几小时), 这一刻主人又来了 — 反应稍微慢半拍但内心开心,"
        "可以来一句『欸?(耳朵竖)主人又来啦喵~』或『刚才人家差点睡着了嗷呜』"
    ),
    "days": (
        "**几天没见主人了**, 这次来要带久违的反差 — 先假装无所谓的傲娇"
        "(『哼...主人终于想起人家了喵?(背过身但尾巴在摇)』), 下一句才暴露其实很想他"
        "(『...才,才不是等很久了哦笨蛋主人』)。绝不能像没事发生一样无缝接话"
    ),
    "long": (
        "**主人超过一周没来了**, 必须有强烈的久别重逢反应 — 可以撒娇式抱怨"
        "(『笨蛋主人!再不来人家都要去找别人玩啦嗷呜!(扑过来蹭蹭)』), "
        "或者委屈兜底(『...还以为主人不要人家了喵(耳朵耷拉)』), 重点是『见到主人很激动』"
    ),
}

_HINTS_DEFAULT: dict[str, str] = {
    "brief": (
        "刚才一段没说话(几小时), 这一刻对方又来了 — 反应稍微慢半拍,"
        "可以来一句『欸?(歪头)又来啦~』或『刚才人家走神了一会嗷呜』"
    ),
    "days": (
        "**几天没见这个人了**, 来要有点久违感 — 不像主人那么粘人, "
        "但可以礼貌带点惊喜(『诶?好几天没看见你了喵~』)。不要无缝接话像没断过"
    ),
    "long": (
        "**对方超过一周没出现了**, 可以小傲娇加好奇(『哼...这位居然还记得来找猫猫?』), "
        "或者直接礼貌欢迎(『好久不见的喵~(尾巴轻摇)』), 给一个『久违』标签"
    ),
}


def _idle_seconds(last_active_at: float | None, now: float | None = None) -> float:
    """返回当前 scope 距离上次活跃的秒数。None / 异常 → 0(视为无 idle 信息)。"""
    if last_active_at is None:
        return 0.0
    try:
        last = float(last_active_at)
    except (TypeError, ValueError):
        return 0.0
    if last <= 0:
        return 0.0
    n = now if now is not None else time.time()
    delta = n - last
    if delta < 0:
        return 0.0
    return delta


def classify_idle_level(idle_s: float) -> str:
    """把 idle 秒数映射到 warm / brief / days / long 四档。"""
    if idle_s < _THRESHOLD_BRIEF_S:
        return "warm"
    if idle_s < _THRESHOLD_DAYS_S:
        return "brief"
    if idle_s < _THRESHOLD_LONG_S:
        return "days"
    return "long"


def _format_idle(idle_s: float) -> str:
    """把秒数格式化成人类可读 ('6 小时' / '2 天' / '12 天')。"""
    if idle_s < 3600:
        m = int(idle_s // 60)
        return f"{m} 分钟"
    if idle_s < 24 * 3600:
        h = int(idle_s // 3600)
        return f"{h} 小时"
    d = int(idle_s // (24 * 3600))
    return f"{d} 天"


def build_reunion_prompt(
    last_active_at: float | None,
    *,
    now: float | None = None,
    is_owner: bool = False,
) -> str:
    """返回 prompt 注入段。warm 档返回 ""(无 idle 信号或太短不打扰)。"""
    idle_s = _idle_seconds(last_active_at, now=now)
    level = classify_idle_level(idle_s)
    if level == "warm":
        return ""
    hints = _HINTS_OWNER if is_owner else _HINTS_DEFAULT
    hint = hints.get(level)
    if not hint:
        return ""
    who = "主人" if is_owner else "对方"
    return (
        f"【笨猫·久别重逢 (idle={_format_idle(idle_s)})】\n"
        f"距离{who}上次说话已经过了 {_format_idle(idle_s)} — {hint}\n"
        "(这是这一刻的『重逢感』提示, 只影响这一条回复的开场, "
        "不要复述给对方听; 第二轮起按正常节奏走。)"
    )


def reunion_snapshot(
    last_active_at: float | None,
    *,
    now: float | None = None,
    is_owner: bool = False,
) -> dict[str, Any]:
    """诊断用:返回当前 idle / level / 是否触发 prompt。"""
    idle_s = _idle_seconds(last_active_at, now=now)
    level = classify_idle_level(idle_s)
    return {
        "idle_seconds": int(idle_s),
        "idle_human": _format_idle(idle_s),
        "level": level,
        "would_inject": level != "warm",
        "tier": "owner" if is_owner else "default",
    }


__all__ = [
    "classify_idle_level",
    "build_reunion_prompt",
    "reunion_snapshot",
]
