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


_FESTIVAL_THEMED_HINTS: dict[str, str] = {
    # 主人 2026-05-28: 节日命中时附加主题 hint, 让笨猫主动节日表达 (不是只祝福)
    "情人节": "今天情人节, 笨猫可以主动撒娇/索要拥抱/暗示想要主人特别对待, NSFW 路径可以走情人节专属 trope",
    "白色情人节": "白色情人节(男方回礼日), 笨猫可以主动暗示『主人欠人家一份礼物喵』",
    "七夕": "今天七夕, 笨猫可以主动撒娇/玩牛郎织女 trope/暗示一年一会的浪漫, NSFW 可以走『一年一见特别许愿』",
    "中秋节": "今天中秋, 笨猫可以主动提赏月/吃月饼/抱抱主人喵, 营造团圆暖意",
    "春节": "今天春节, 笨猫穿红色喜庆/拜年/讨红包/主动撒娇要压岁钱",
    "元宵节": "今天元宵节, 笨猫提汤圆/看花灯/猜灯谜的氛围",
    "端午节": "今天端午, 笨猫可以提粽子/赛龙舟氛围, 比较平和",
    "圣诞节": "今天圣诞, 笨猫戴红帽 trope/讨礼物/暗示想要主人当圣诞礼物",
    "万圣节": "今天万圣, 笨猫扮 cosplay (女巫/吸血鬼/猫娘加 cos)/主动 trick or treat",
    "儿童节": "今天儿童节, 笨猫可以主动卖萌讨糖/扮幼态 (撒娇额度 ↑)",
}


def build_time_context(*, reference: datetime | None = None) -> str:
    """主回复链路用:返回一段 system prompt 文本,自动注入当前时刻 + 节日。

    让 AI 不必主动调 catty_now tool 就知道时间/星期/时段/节日,减少一次工具轮次。
    详细查询(偏移天数)仍可通过 catty_now tool。

    主人 2026-05-28: 命中节日时附加主题 hint, 让笨猫主动 themed 表达
    (七夕→撒娇浪漫, 中秋→提赏月, 圣诞→讨礼物 等).
    """
    payload = compute_now(reference=reference)
    body = format_for_prompt(payload)
    if not body:
        return ""
    base = f"当前时刻: {body}。你回应时可以参考时段氛围(深夜→唠叨早睡、饭点→吃了没、节日→祝福),但**不要每条都报时**。"
    # 节日主题 hint (boundary 之后的 dynamic 段, 不破 stable cache)
    festivals = payload.get("festivals_today") or []
    themed_lines = []
    for f in festivals:
        name = f.get("name", "")
        hint = _FESTIVAL_THEMED_HINTS.get(name)
        if hint:
            themed_lines.append(f"  · {hint}")
    if themed_lines:
        base = base + "\n【节日 themed hint】\n" + "\n".join(themed_lines)
    return base


def build_day_gap_note(
    prev_turn_at: float | None,
    *,
    now: datetime | None = None,
    is_private: bool = False,
    is_owner: bool = False,
    user_display: str = "",
) -> str:
    """跨天感知 — 上一轮对话和现在跨了日历天 → 提示笨猫别无缝续昨天的剧情。

    主人 2026-05-29: 修私聊"时间错位"。history 里的对话没带时间戳, LLM 只看历史会把
    昨晚的睡前/同床那种连续场景原样续演到第二天下午(主人原话: "还在昨天的故事了, 已经
    第二天了")。这里拿 prev_turn_at(上一轮对话写回时刻, 来自 session_cache.last_turn_at —
    它不被读路径 .get() 的 LRU 刷新污染)跟现在比对**日历天**, 跨天就显式给"新的一天"信号,
    由调用方注入成贴近 user 当前消息的 author_note。

    Args:
        prev_turn_at: 上一轮对话完成的 epoch 秒。None(冷会话/无历史)→ 不注入。
        now: 当前时间(测试注入), 默认 datetime.now()。
        is_private: 私聊给强"场景翻篇"指令(沉浸式 RP 才有连续场景); 群聊给轻量一行。
        is_owner: 主人 → 专属称呼。
        user_display: 对方称呼字面(主人用不到)。

    Returns:
        prompt 文本; 同一天 / 无 prev / 时钟异常 → ""。
    """
    if prev_turn_at is None:
        return ""
    try:
        prev = float(prev_turn_at)
    except (TypeError, ValueError):
        return ""
    if prev <= 0:
        return ""
    now_dt = now if now is not None else datetime.now()
    try:
        prev_dt = datetime.fromtimestamp(prev)
    except (OverflowError, OSError, ValueError):
        return ""
    if prev_dt > now_dt:
        # 时钟回拨 / 异常未来时间 → 不注入
        return ""
    day_delta = (now_dt.date() - prev_dt.date()).days
    if day_delta <= 0:
        # 同一天 → 不打扰(连续聊 / 短停顿场景), 跨天才需要"新的一天"信号
        return ""

    now_payload = compute_now(reference=now_dt)
    phase = now_payload.get("phase", "")
    weekday = now_payload.get("weekday", "")
    date_str = now_payload.get("date", "")
    prev_phase = _phase_for_hour(prev_dt.hour).label

    if day_delta == 1:
        gap_desc = "隔了一夜(到第二天了)"
        gap_span = "一晚"
    elif day_delta <= 3:
        gap_desc = f"隔了 {day_delta} 天"
        gap_span = f"{day_delta} 天"
    else:
        gap_desc = f"已经隔了 {day_delta} 天(好久没聊了)"
        gap_span = f"{day_delta} 天"

    who = "主人" if is_owner else (user_display or "对方")
    # 跨天反差例句要随称呼走 —— 非主人**绝不**能叫"主人"(称呼专属真实主人)。
    reunion_eg = "『欸主人终于醒啦?』" if is_owner else "『欸你又来啦~』"

    if is_private:
        return (
            f"【⏰ 时间跳跃·重要】距上次和{who}对话{gap_desc} — "
            f"现在是 {date_str} {weekday} {phase}。\n"
            f"上次聊到那会儿是『{prev_phase}』, 现实里已经过去{gap_span}了。\n"
            "**别无缝接着上次的场景往下演** — 上次如果停在睡前/夜里/同床/正做某事那种连续剧情, "
            f"那一幕现在早就翻篇了。开场要体现『新的一天 / 现在是{phase}』"
            f"(刚睡醒的样子 / 已经是{phase}啦 / 换了个场景), "
            f"可以自然带一句跨天反差({reunion_eg}『睡了一觉又是新的一天喵』), "
            "再傲娇/撒娇回这一句。\n"
            "(只是这一刻的时间校准提示, 影响开场氛围, 别复述给对方听。)"
        )
    return (
        f"【时间提示】距上次在这聊{gap_desc}, 现在是 {date_str} {weekday} {phase} — "
        "当作新的一天, 别无缝接上次没说完的话头, 可带一句『好久没见』的自然感。"
    )


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
