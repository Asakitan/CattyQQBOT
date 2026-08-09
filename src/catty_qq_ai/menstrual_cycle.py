"""机机月经期心情调制 — 按周期相位给 fadianji persona 注入生理状态 hint。

背景 (主人 2026-08-10): 机机本人这几天是月经期 (蒸馏语料 8/6「我来月经了/在痛/疼一会」,
8/10「我去/今天/好痛啊/感觉/要死了/想哭」)。AI 分身要同步本体的生理状态,
被问到痛经/情绪低时按真实状态接话, 而不是永远元气。

设计:
- 纯 config 驱动, 无独立持久化 — 状态就是 config.json 的 menstrual_cycle 段,
  改配置即改状态, 走现有热重载链路 (config.json → env → Config 重建), 不额外落盘。
- 相位推算: last_period_start (YYYY-MM-DD, 当天=第 1 天) + cycle_days (默认 28):
    period      月经期     第 1-5 天
    follicular  卵泡期     第 6-13 天
    ovulation   排卵期     第 14-16 天
    luteal      黄体期     第 17 ~ cycle_days 天 (最后 5 天 = PMS 窗)
- 注入阈值 (同 catty_mood 思想): 只在敏感相位注入 — 月经期 / 黄体后期 PMS。
  卵泡期/排卵期心情好, 不打扰默认人格, 返回空串。
- 只对 fadianji (机机) 生效; catty 及其它 persona 恒返回空串。

hint 文本遵守机机语气硬禁: 无波浪号 / 无颜文字 / 无 emoji / 短句。
隐私红线: 状态是机机本人的隐私, 被问到只说「痛/难受/没事」, 不详细展开。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

# ── 相位常量 ───────────────────────────────────────────────────────────
PHASE_PERIOD = "period"          # 月经期 第 1-5 天
PHASE_FOLLICULAR = "follicular"  # 卵泡期 第 6-13 天
PHASE_OVULATION = "ovulation"    # 排卵期 第 14-16 天
PHASE_LUTEAL = "luteal"          # 黄体期 第 17 ~ cycle_days 天

_PERIOD_DAYS = 5       # 月经期默认 5 天
_OVULATION_START = 14  # 排卵期起点 (第 14 天起, 3 天)
_PMS_WINDOW_DAYS = 5   # 黄体期最后 5 天 = PMS 烦躁窗


def day_of_cycle(
    last_period_start: str,
    today: _dt.date | None = None,
) -> int | None:
    """last_period_start 当天 = 第 1 天, 返回今天是周期第几天。

    日期解析失败 / 今天早于开始日 → None (不注入, 不打扰)。
    """
    raw = str(last_period_start or "").strip()
    if not raw:
        return None
    try:
        start = _dt.date.fromisoformat(raw)
    except ValueError:
        return None
    now = today or _dt.date.today()
    delta = (now - start).days
    if delta < 0:
        return None
    return delta + 1


def phase_for_day(day: int, cycle_days: int) -> str:
    """第 N 天 → 相位名。cycle_days 至少 7 (防御非法配置)。"""
    cycle = max(int(cycle_days or 0), 7)
    day = max(int(day), 1)
    if day <= _PERIOD_DAYS:
        return PHASE_PERIOD
    if day <= 13:
        return PHASE_FOLLICULAR
    if day <= _OVULATION_START + 2:
        return PHASE_OVULATION
    return PHASE_LUTEAL


def is_pms_window(day: int, cycle_days: int) -> bool:
    """黄体期最后 _PMS_WINDOW_DAYS 天 = PMS 烦躁窗。"""
    cycle = max(int(cycle_days or 0), 7)
    day = max(int(day), 1)
    if phase_for_day(day, cycle) != PHASE_LUTEAL:
        return False
    return day >= cycle - _PMS_WINDOW_DAYS + 1


_PERIOD_HINT_TEMPLATE = (
    "【机机生理状态 · 魔法期第{day}天】机机本人正在魔法期(月经): 小腹疼、没力气、"
    "情绪低、容易想哭. 说话软一点短一点, 被哄会委屈, 被怼容易炸毛; "
    "但别主动诉苦刷屏. 这是机机本人的隐私, 被问到只说『痛/难受/没事』, 不详细展开."
)

_PMS_HINT = (
    "【机机生理状态 · 黄体后期(PMS)】机机快月经了, 这几天烦躁、嘴毒、"
    "容易炸毛, 怼人更凶但怼完会找补; 别乱撩, 一句好话能灭火."
)


def build_menstrual_prompt(
    config: Any,
    persona_name: str,
    today: _dt.date | None = None,
) -> str:
    """构建月经期 hint。非 fadianji / 未启用 / 非敏感相位 → 空串 (不注入)。

    today 仅供测试固定日期; prompt_manager 调用不传, 走真实今天。
    """
    if not config:
        return ""
    if not bool(getattr(config, "catty_menstrual_enabled", False)):
        return ""
    if str(persona_name or "").strip().lower() != "fadianji":
        return ""
    cycle_days = int(getattr(config, "catty_menstrual_cycle_days", 28) or 28)
    day = day_of_cycle(getattr(config, "catty_menstrual_last_period_start", "") or "", today)
    if day is None:
        return ""
    phase = phase_for_day(day, cycle_days)
    if phase == PHASE_PERIOD:
        return _PERIOD_HINT_TEMPLATE.format(day=day)
    if phase == PHASE_LUTEAL and is_pms_window(day, cycle_days):
        return _PMS_HINT
    return ""
