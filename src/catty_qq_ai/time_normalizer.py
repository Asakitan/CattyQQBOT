"""时间表达式 → ISO 绝对时间规范化。

给 entity_extractor 抓到的 time 实体加 ``iso`` 字段,让 AI 看到的
不是『明天』『周日』而是『2026-05-24』/『2026-05-25T18:00』,直接
可以塞进 catty_remember 笔记或贴回复给主人,免得错位。

设计原则:
- **失败安静返回 None**,不抛异常。 entity 保留 raw 即可。
- **本地无网络**,reference 默认 ``datetime.now()``(测试可注入)。
- **only 日期粒度** 当只有相对日期/星期时(『明天』/『周日』→ 2026-05-24);
  **粒度细到分钟** 当带『X 点 Y 分』/HH:MM 时。
- 不处理:跨年模糊 X 月 X 日 + 现在已过该日 → 默认推到下一年(只 +1 年内,不再远)。
- 不处理:时区 → 用本机 local。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


# ── 表 ────────────────────────────────────────────────────────────────

_REL_DATE_OFFSETS = {
    "今天": 0, "今晚": 0, "今早": 0, "今上午": 0, "今下午": 0,
    "明天": 1, "明日": 1, "明晚": 1, "明早": 1, "明上午": 1, "明下午": 1,
    "后天": 2, "大后天": 3,
    "昨天": -1, "前天": -2,
}

# 当天哪个时段(仅当 raw 含『晚/早/午/凌晨』之类时叠上)
_PHASE_HOUR = {
    "凌晨": (3, 0),
    "早上": (8, 0), "早": (8, 0),
    "上午": (10, 0),
    "中午": (12, 0),
    "下午": (15, 0),
    "傍晚": (18, 0),
    "晚上": (20, 0), "晚": (20, 0),
}

_WEEKDAY_MAP = {
    "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6,
    "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3, "星期五": 4,
    "星期六": 5, "星期日": 6, "星期天": 6,
}

# 正则:绝对日期 / HH:MM / 中文时分 / 相对时长
_ABS_DATE_CN_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_HMS_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b")
_CN_HOUR_RE = re.compile(
    r"(上午|下午|早上|早|晚上|晚|凌晨|中午|傍晚)?\s*"
    r"(\d{1,2})\s*点\s*"
    r"(半|\d{1,2})?(?:分)?"
)
_REL_DURATION_RE = re.compile(r"(\d{1,3})\s*(小时|天|日|周|个月|月|年|分钟|秒)\s*(后|前)")


# ── helpers ──────────────────────────────────────────────────────────

def _iso_date(d: date) -> str:
    return d.isoformat()


def _iso_datetime(dt: datetime) -> str:
    return dt.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def _next_weekday_from(base: date, target_weekday: int, *, prefer_future: bool = True) -> date:
    """从 base 起找最近一个 weekday 等于 target 的日期。

    prefer_future=True: 今天就是该 weekday 时也跳到下周(『周日见』不会指今天)。
    """
    delta = (target_weekday - base.weekday()) % 7
    if delta == 0 and prefer_future:
        delta = 7
    return base + timedelta(days=delta)


# ── 主 normalize ──────────────────────────────────────────────────────

def normalize_time_entity(raw: str, note: str = "", *, reference: datetime | None = None) -> str | None:
    """把一个 time entity 的 raw 文本规范成 ISO 字符串。失败返回 None。

    note 是 entity_extractor 给的标签:相对日期/星期/绝对日期/时分/HH:MM/相对时长。
    没传 note 也会尝试自动判断。
    """
    if not raw:
        return None
    ref = reference if reference is not None else datetime.now()
    today = ref.date()

    # 1) 相对日期(优先识别,因为可能叠"明早"/"今晚"这样混合短语)
    for word, offset in _REL_DATE_OFFSETS.items():
        if raw == word or raw.startswith(word):
            target_date = today + timedelta(days=offset)
            # 附加时段(『今早』『明晚』隐含 hour)
            for phase, (hh, mm) in _PHASE_HOUR.items():
                if phase in word:
                    return _iso_datetime(datetime.combine(target_date, time(hh, mm)))
            # 单纯日期粒度
            return _iso_date(target_date)

    # 2) 星期 / 下周 X
    for kw, wd in _WEEKDAY_MAP.items():
        if raw == kw or raw == "下" + kw:
            base = today + timedelta(days=7) if raw.startswith("下") else today
            target = _next_weekday_from(base, wd, prefer_future=True)
            return _iso_date(target)
    if raw in ("周末", "本周末"):
        target = _next_weekday_from(today, 5, prefer_future=False)  # 最近的周六
        return _iso_date(target)
    if raw == "下周末":
        target = _next_weekday_from(today + timedelta(days=7), 5, prefer_future=False)
        return _iso_date(target)

    # 3) 绝对中文日期: X 月 Y 日
    m = _ABS_DATE_CN_RE.fullmatch(raw.replace(" ", ""))
    if not m:
        m = _ABS_DATE_CN_RE.search(raw)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            year = today.year
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
            # 已过 → 推到下一年(只 +1)
            if candidate < today:
                try:
                    candidate = date(year + 1, month, day)
                except ValueError:
                    return None
            return _iso_date(candidate)

    # 4) HH:MM 时分(假设今天;已过则次日)
    m = _HMS_RE.fullmatch(raw.strip())
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        dt = datetime.combine(today, time(hour, minute))
        if dt < ref:
            dt = dt + timedelta(days=1)
        return _iso_datetime(dt)

    # 5) 中文时分: 下午 3 点 / 晚上 8 点半 / 3 点 20
    m = _CN_HOUR_RE.fullmatch(raw.replace(" ", ""))
    if not m:
        m = _CN_HOUR_RE.search(raw)
    if m:
        phase = m.group(1) or ""
        try:
            hour = int(m.group(2))
        except (TypeError, ValueError):
            return None
        minute_raw = m.group(3) or ""
        if minute_raw == "半":
            minute = 30
        elif minute_raw:
            try:
                minute = int(minute_raw)
            except ValueError:
                minute = 0
        else:
            minute = 0
        # 24h 化:『下午/晚上/傍晚』+ hour<12 → +12
        if phase in ("下午", "晚上", "晚", "傍晚") and hour < 12:
            hour += 12
        if phase == "凌晨" and hour >= 12:
            hour -= 12
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        dt = datetime.combine(today, time(hour, minute))
        if dt < ref:
            dt = dt + timedelta(days=1)
        return _iso_datetime(dt)

    # 6) 相对时长: N 小时后 / N 天后 / N 分钟后
    m = _REL_DURATION_RE.fullmatch(raw.replace(" ", ""))
    if not m:
        m = _REL_DURATION_RE.search(raw)
    if m:
        try:
            qty = int(m.group(1))
        except ValueError:
            return None
        unit = m.group(2)
        direction = m.group(3)
        sign = 1 if direction == "后" else -1
        delta: timedelta
        if unit == "秒":
            delta = timedelta(seconds=qty)
        elif unit == "分钟":
            delta = timedelta(minutes=qty)
        elif unit == "小时":
            delta = timedelta(hours=qty)
        elif unit in ("天", "日"):
            delta = timedelta(days=qty)
        elif unit == "周":
            delta = timedelta(days=qty * 7)
        elif unit in ("月", "个月"):
            delta = timedelta(days=qty * 30)
        elif unit == "年":
            delta = timedelta(days=qty * 365)
        else:
            return None
        dt = ref + sign * delta
        # 天/周/月/年 粒度只到日;小时/分钟 含时分
        if unit in ("天", "日", "周", "月", "个月", "年"):
            return _iso_date(dt.date())
        return _iso_datetime(dt)

    return None
