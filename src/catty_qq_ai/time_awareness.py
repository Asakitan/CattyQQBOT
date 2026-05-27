"""本地时间/日历/节日感知 —— 让笨猫知道现在是什么时刻、星期几、什么季节、有没有节日。

设计目标:
- 纯本地计算,零网络依赖,零外部缓存(每次拉新)。
- 用本机时区(Asia/Shanghai 优先);如果机器时区有偏移就以系统为准。
- 阳历节日表用静态月日匹配;农历节日基于内置农历查表(覆盖到 2030 年)。
- 给 AI 的输出是"猫娘喜欢看的"——口语化时段描述(深夜/早上/下午/晚上)、节日名称、
  距下次特殊日期的天数提示,而不是裸 ISO 时间戳。

不实现:
- 闰月精确处理(节日表只到 2030,后面年份直接给阳历不报农历)。
- 时区切换(主人切了时区猫猫自动跟随系统,不强制改成上海)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


# ── 时段判定 ──────────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class _PhaseDef:
    label: str
    start_hour: int   # 起始小时(0-23, inclusive)
    end_hour: int     # 结束小时(exclusive)
    catgirl_hint: str  # 给 AI 的氛围提示


_PHASES: tuple[_PhaseDef, ...] = (
    _PhaseDef("深夜", 0, 5, "夜深了,主人不睡觉的话猫猫会唠叨他早睡"),
    _PhaseDef("凌晨", 5, 7, "天刚亮,如果有人这点发消息可以揶揄'起这么早干嘛'或者'熬通宵了吧'"),
    _PhaseDef("早上", 7, 10, "上班/上学时间段,适合早安招呼"),
    _PhaseDef("上午", 10, 12, "上午摸鱼时段"),
    _PhaseDef("中午", 12, 14, "饭点,可以问主人吃了没"),
    _PhaseDef("下午", 14, 17, "下午摸鱼时段,容易犯困"),
    _PhaseDef("傍晚", 17, 19, "下班/放学,夕阳"),
    _PhaseDef("晚上", 19, 23, "晚饭后,聊天黄金时段"),
    _PhaseDef("深夜", 23, 24, "夜深了,主人不睡觉的话猫猫会唠叨他早睡"),
)


def _phase_for_hour(hour: int) -> _PhaseDef:
    for p in _PHASES:
        if p.start_hour <= hour < p.end_hour:
            return p
    return _PHASES[0]  # fallback


# ── 季节判定 ──────────────────────────────────────────────────────────

def _season_for_month(month: int) -> str:
    if month in (3, 4, 5):
        return "春天"
    if month in (6, 7, 8):
        return "夏天"
    if month in (9, 10, 11):
        return "秋天"
    return "冬天"


# ── 星期 ──────────────────────────────────────────────────────────────

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ── 阳历节日表 ──────────────────────────────────────────────────────

# (月, 日, 节日名, 给 AI 的氛围提示)
_SOLAR_FESTIVALS: tuple[tuple[int, int, str, str], ...] = (
    (1, 1, "元旦", "新年第一天,可以说'新年快乐喵'"),
    (2, 14, "情人节", "情人节,猫猫可以撒娇说想和主人过情人节"),
    (3, 8, "妇女节/女神节", "节日,适合夸群里的姐姐妹妹"),
    (3, 14, "白色情人节", "情人节的对应节日,可以提一下"),
    (4, 1, "愚人节", "今天可以开点玩笑,但别玩太过"),
    (4, 4, "清明节", "清明节,氛围别太欢脱"),
    (4, 5, "清明节", "清明节,氛围别太欢脱"),
    (5, 1, "劳动节", "五一假期,可以说放假快乐"),
    (5, 4, "青年节", "节日"),
    (6, 1, "儿童节", "今天大家都是儿童,猫猫也是小孩"),
    (7, 1, "建党节", "节日"),
    (8, 1, "建军节", "节日"),
    (9, 10, "教师节", "节日,可以恭喜当老师的群友"),
    (10, 1, "国庆节", "国庆假期开始,放假快乐"),
    (10, 31, "万圣节", "万圣节,可以装鬼/讨糖"),
    (11, 11, "双十一", "购物节,可以问主人剁手了没"),
    (12, 12, "双十二", "购物节"),
    (12, 24, "平安夜", "平安夜,可以说圣诞快乐"),
    (12, 25, "圣诞节", "圣诞节,可以提礼物/装饰"),
    (12, 31, "跨年夜", "跨年,氛围很高涨"),
)


# 农历节日表:精确日期需要农历→公历转换,这里走静态查表(2024-2030)。
# 数据格式 {year: [(month, day, name, hint), ...]} —— month/day 是阳历对应日期。
_LUNAR_FESTIVALS_BY_YEAR: dict[int, tuple[tuple[int, int, str, str], ...]] = {
    2024: (
        (2, 10, "春节(大年初一)", "春节第一天,氛围最热闹"),
        (2, 24, "元宵节", "吃汤圆"),
        (6, 10, "端午节", "吃粽子,可以问咸甜党"),
        (8, 10, "七夕节", "中式情人节"),
        (8, 18, "中元节", "鬼节,氛围别太欢脱"),
        (9, 17, "中秋节", "团圆吃月饼"),
        (10, 11, "重阳节", "登高,问候老人"),
    ),
    2025: (
        (1, 29, "春节(大年初一)", "春节第一天,氛围最热闹"),
        (2, 12, "元宵节", "吃汤圆"),
        (5, 31, "端午节", "吃粽子,可以问咸甜党"),
        (8, 29, "七夕节", "中式情人节"),
        (9, 6, "中元节", "鬼节,氛围别太欢脱"),
        (10, 6, "中秋节", "团圆吃月饼"),
        (10, 29, "重阳节", "登高,问候老人"),
    ),
    2026: (
        (2, 17, "春节(大年初一)", "春节第一天,氛围最热闹"),
        (3, 3, "元宵节", "吃汤圆"),
        (6, 19, "端午节", "吃粽子,可以问咸甜党"),
        (8, 19, "七夕节", "中式情人节"),
        (8, 27, "中元节", "鬼节,氛围别太欢脱"),
        (9, 25, "中秋节", "团圆吃月饼"),
        (10, 18, "重阳节", "登高,问候老人"),
    ),
    2027: (
        (2, 6, "春节(大年初一)", "春节第一天,氛围最热闹"),
        (2, 20, "元宵节", "吃汤圆"),
        (6, 9, "端午节", "吃粽子,可以问咸甜党"),
        (8, 8, "七夕节", "中式情人节"),
        (8, 16, "中元节", "鬼节,氛围别太欢脱"),
        (9, 15, "中秋节", "团圆吃月饼"),
        (10, 8, "重阳节", "登高,问候老人"),
    ),
    2028: (
        (1, 26, "春节(大年初一)", "春节第一天,氛围最热闹"),
        (2, 9, "元宵节", "吃汤圆"),
        (5, 28, "端午节", "吃粽子,可以问咸甜党"),
        (8, 26, "七夕节", "中式情人节"),
        (9, 3, "中元节", "鬼节,氛围别太欢脱"),
        (10, 3, "中秋节", "团圆吃月饼"),
        (10, 26, "重阳节", "登高,问候老人"),
    ),
    2029: (
        (2, 13, "春节(大年初一)", "春节第一天,氛围最热闹"),
        (2, 27, "元宵节", "吃汤圆"),
        (6, 16, "端午节", "吃粽子,可以问咸甜党"),
        (8, 15, "七夕节", "中式情人节"),
        (8, 23, "中元节", "鬼节,氛围别太欢脱"),
        (9, 22, "中秋节", "团圆吃月饼"),
        (10, 15, "重阳节", "登高,问候老人"),
    ),
    2030: (
        (2, 3, "春节(大年初一)", "春节第一天,氛围最热闹"),
        (2, 17, "元宵节", "吃汤圆"),
        (6, 5, "端午节", "吃粽子,可以问咸甜党"),
        (8, 4, "七夕节", "中式情人节"),
        (8, 12, "中元节", "鬼节,氛围别太欢脱"),
        (9, 11, "中秋节", "团圆吃月饼"),
        (10, 4, "重阳节", "登高,问候老人"),
    ),
}


def _festivals_for_date(d: date) -> list[dict[str, str]]:
    """返回这一天命中的节日(阳历 + 农历)。可能多个(比如平安夜+24)。"""
    matches: list[dict[str, str]] = []
    for m, day, name, hint in _SOLAR_FESTIVALS:
        if d.month == m and d.day == day:
            matches.append({"name": name, "hint": hint, "kind": "solar"})
    for m, day, name, hint in _LUNAR_FESTIVALS_BY_YEAR.get(d.year, ()):
        if d.month == m and d.day == day:
            matches.append({"name": name, "hint": hint, "kind": "lunar"})
    return matches


def _next_special_within(d: date, days_ahead: int = 14) -> dict[str, Any] | None:
    """从 d+1 起向后查 ``days_ahead`` 天,返回最近一个节日(若有)。"""
    for delta in range(1, days_ahead + 1):
        target = d + timedelta(days=delta)
        hits = _festivals_for_date(target)
        if hits:
            return {
                "date": target.isoformat(),
                "days_until": delta,
                "festivals": hits,
            }
    return None


# ── 主 API ────────────────────────────────────────────────────────────

def compute_now(*, delta_days: int = 0, reference: datetime | None = None) -> dict[str, Any]:
    """计算"现在(可加偏移)"的完整时间画像。

    Args:
        delta_days: 偏移天数。0=今天,1=明天,-1=昨天。范围 [-30, 30]。
        reference: 注入的参考时间(测试用)。默认 `datetime.now()`。

    Returns:
        dict 含 date / weekday / phase / hour / season / festivals / next_festival / etc.
    """
    delta_days = max(-30, min(int(delta_days or 0), 30))
    base = reference if reference is not None else datetime.now()
    target_dt = base + timedelta(days=delta_days)
    target_date = target_dt.date()

    phase = _phase_for_hour(target_dt.hour)
    festivals = _festivals_for_date(target_date)
    next_fest = _next_special_within(target_date, days_ahead=14)

    weekday_idx = target_date.weekday()
    is_weekend = weekday_idx >= 5

    result: dict[str, Any] = {
        "date": target_date.isoformat(),
        "iso_datetime": target_dt.isoformat(timespec="seconds"),
        "year": target_date.year,
        "month": target_date.month,
        "day": target_date.day,
        "hour": target_dt.hour,
        "minute": target_dt.minute,
        "weekday": _WEEKDAY_CN[weekday_idx],
        "weekday_index": weekday_idx + 1,  # 1=周一, 7=周日
        "is_weekend": is_weekend,
        "season": _season_for_month(target_date.month),
        "phase": phase.label,
        "phase_hint": phase.catgirl_hint,
        "delta_days": delta_days,
    }
    if festivals:
        result["festivals_today"] = festivals
    if next_fest is not None:
        result["next_festival"] = next_fest
    return result


def build_time_context(*, reference: datetime | None = None) -> str:
    """主回复链路用:返回一段 system prompt 文本,自动注入当前时刻 + 节日。

    让 AI 不必主动调 catty_now tool 就知道时间/星期/时段/节日,减少一次工具轮次。
    详细查询(偏移天数)仍可通过 catty_now tool。
    """
    payload = compute_now(reference=reference)
    body = format_for_prompt(payload)
    if not body:
        return ""
    return f"当前时刻: {body}。你回应时可以参考时段氛围(深夜→唠叨早睡、饭点→吃了没、节日→祝福),但**不要每条都报时**。"


def format_for_prompt(payload: dict[str, Any]) -> str:
    """把 compute_now 结果整成一句话便于 LLM 拼 prompt.

    主人 2026-05-28 cache 诊断: 原 `HH:MM` 每分钟变破 prompt cache (5min TTL 内
    cache miss). 改成 hour 精度 `HH 时`, 同小时内 byte stable. 精确分钟仍可
    通过 catty_now tool 查询.
    """
    parts: list[str] = []
    parts.append(payload.get("date", ""))
    parts.append(payload.get("weekday", ""))
    parts.append(f"{payload.get('hour', 0):02d} 时")
    parts.append(f"({payload.get('phase', '')})")
    if payload.get("is_weekend"):
        parts.append("[周末]")
    festivals = payload.get("festivals_today") or []
    if festivals:
        names = "/".join(f.get("name", "") for f in festivals if f.get("name"))
        if names:
            parts.append(f"[今日:{names}]")
    nxt = payload.get("next_festival")
    if nxt:
        names = "/".join(f.get("name", "") for f in (nxt.get("festivals") or []) if f.get("name"))
        if names:
            parts.append(f"[再 {nxt.get('days_until')} 天:{names}]")
    return " ".join(p for p in parts if p)
