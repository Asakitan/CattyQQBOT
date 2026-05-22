"""入向消息实体本地提取 —— 让笨猫不漏读消息里的关键事实。

设计目标:
- 纯正则,O(n),零网络。每条消息平均 < 0.2 ms。
- **保守**:宁可漏(让 AI 自己读原文)别误(贴错实体反误导)。
- 输出给 AI 当 system prompt 提示,可以决定要不要 catty_remember/调 tool。

实体类型(按提取难度排序):
| 类型      | 例子                                | 用途                          |
|-----------|------------------------------------|------------------------------|
| url       | https://...                        | 决定要不要 web_search 跟进    |
| time      | 明天下午 3 点 / 3 月 5 日 / 周日   | 约定登记(可调 catty_remember) |
| money     | 50 元 / ¥100 / 200 块               | 价格类问题/承诺              |
| count     | 3 个 / 100 块钱 / 第 5 次          | 数量级敏感                   |
| at_mention| @小明                              | 知道在叫谁                   |
| qq_id     | "qq 12345678"  /  "号 12345"        | 给 catty_user_profile 用     |

不做的事:
- normalize time(『明天』→ 2026-05-24): 单独留给将来一轮或 catty_now 提供 reference。
- NER/语义级实体: 名字/地点等 high-recall 场景容易误,留给 LLM 自己读。
- emoji/表情符号: 不算"实体",已有 emoji_store 单独处理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .time_normalizer import normalize_time_entity


@dataclass(slots=True)
class Entity:
    kind: str       # url / time / money / count / at_mention / qq_id
    raw: str        # 原文里的子串
    note: str = ""  # 给 AI 的语义提示(可选,例如 time 类标"未来"/"过去")
    iso: str = ""   # time 类专用:normalize 出的 ISO 字符串(日期或带时分),空表示 normalize 失败


# ── URL ──────────────────────────────────────────────────────────────

_URL_RE = re.compile(
    r"https?://[^\s　<>\"'\[\]\(\)，。、；：]+",
    re.IGNORECASE,
)


# ── @提及(已经有过滤层,但残留可能有 @xxx 文本) ───────────────────

_AT_MENTION_RE = re.compile(r"@[一-鿿\w\-]{1,32}")


# ── QQ号(必须明确 prefix "qq" / "号",避免误判长数字) ────────────

_QQ_ID_RE = re.compile(r"(?:qq[\s::]*|号[\s::]*|QQ[\s::]*)(\d{5,12})")


# ── 时间表达式 ───────────────────────────────────────────────────────

# 相对日期:今天/明天/后天/大后天/昨天/前天/今晚/明早 等
_REL_DATE = (
    "今天", "明天", "明日", "后天", "大后天", "昨天", "前天",
    "今晚", "明晚", "今早", "明早", "今下午", "明下午", "今上午", "明上午",
)
# 星期表达式
_WEEKDAY = (
    "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天",
    "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日", "星期天",
    "下周一", "下周二", "下周三", "下周四", "下周五", "下周六", "下周日",
    "本周末", "下周末", "周末",
)
# 绝对日期: 3月5日 / 3/5 / 5月1号(允许"5 月 1 日"这种带空格写法)
_ABS_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_ABS_DATE_SLASH_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})\b")
# 时分:3 点 / 15:30 / 3:30 PM
_HOUR_CN_RE = re.compile(r"(?<![\d:])(上午|下午|早上|晚上|凌晨|中午)?\s*(\d{1,2})\s*点\s*(半|\d{1,2})?(?:分)?")
_HOUR_HMS_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b")
# X 小时/天/周/月/年 后 / 前
_REL_DURATION_RE = re.compile(r"\d{1,3}\s*(小时|天|日|周|个月|月|年|分钟|秒)\s*(后|前)")


def _find_phrases(text: str, phrases: Iterable[str]) -> list[tuple[int, int, str]]:
    """返回 (start, end, raw) 列表,按 start 排序。"""
    hits: list[tuple[int, int, str]] = []
    for p in phrases:
        start = 0
        while True:
            idx = text.find(p, start)
            if idx < 0:
                break
            hits.append((idx, idx + len(p), p))
            start = idx + len(p)
    hits.sort()
    return hits


# ── 金额 / 数量 ─────────────────────────────────────────────────────

# 钱有两种命中:
#   (1) [¥￥] + 数字(单位可选): "¥100" / "￥50 块"
#   (2) 数字 + 单位词: "50 元" / "100 块钱"
_MONEY_SYMBOL_RE = re.compile(r"[¥￥]\s*\d+(?:\.\d+)?(?:\s*(?:元|块钱|块|RMB|rmb|刀|美元|人民币))?")
_MONEY_WORD_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:元|块钱|块|RMB|rmb|刀|美元|人民币)")
# 数量(数字 + 通用量词)— \b 对中文不生效,直接靠 unit 唯一性 + 不允许尾随中文/字母粘连
_COUNT_RE = re.compile(
    r"(\d+)\s*(个|只|条|根|份|次|位|名|遍|场|张|本|页|公里|米|千米|斤|克|公斤|kg|g)"
)


# ── 主提取函数 ────────────────────────────────────────────────────────

def extract_entities(text: str, *, reference: datetime | None = None) -> list[Entity]:
    """从消息文本提取所有实体,按出现位置排序,无命中返回 []。

    去重:同 kind+raw 的二次出现只保留首次。
    reference: 用于 time 实体的 ISO normalize,默认 ``datetime.now()``。
    """
    if not text or not isinstance(text, str):
        return []

    found: list[tuple[int, Entity]] = []

    # URL
    for m in _URL_RE.finditer(text):
        found.append((m.start(), Entity(kind="url", raw=m.group(0))))

    # @提及
    for m in _AT_MENTION_RE.finditer(text):
        # 跳过 URL 内部的 @(如 user@host)
        raw = m.group(0)
        if "://" in text[max(0, m.start() - 8) : m.start()]:
            continue
        found.append((m.start(), Entity(kind="at_mention", raw=raw)))

    # QQ号(prefix 模式)
    for m in _QQ_ID_RE.finditer(text):
        found.append((m.start(), Entity(kind="qq_id", raw=m.group(1))))

    # 时间:相对日期 + 星期 + 绝对日期 + 时分 + 相对时长
    for start, end, raw in _find_phrases(text, _REL_DATE):
        found.append((start, Entity(kind="time", raw=raw, note="相对日期")))
    for start, end, raw in _find_phrases(text, _WEEKDAY):
        found.append((start, Entity(kind="time", raw=raw, note="星期")))
    for m in _ABS_DATE_RE.finditer(text):
        found.append((m.start(), Entity(kind="time", raw=m.group(0), note="绝对日期")))
    for m in _HOUR_CN_RE.finditer(text):
        raw = m.group(0).strip()
        # 容忍前后空白,但确保至少有"点"且数字合法
        if raw and "点" in raw:
            try:
                hour = int(m.group(2))
                if 0 <= hour <= 24:
                    found.append((m.start(), Entity(kind="time", raw=raw, note="时分")))
            except (TypeError, ValueError):
                pass
    for m in _HOUR_HMS_RE.finditer(text):
        found.append((m.start(), Entity(kind="time", raw=m.group(0), note="HH:MM")))
    for m in _REL_DURATION_RE.finditer(text):
        found.append((m.start(), Entity(kind="time", raw=m.group(0), note="相对时长")))

    # 金额 — 符号优先,然后是 "N 元/块" 词式
    money_spans: list[tuple[int, int]] = []
    for m in _MONEY_SYMBOL_RE.finditer(text):
        money_spans.append((m.start(), m.end()))
        found.append((m.start(), Entity(kind="money", raw=m.group(0).strip())))
    for m in _MONEY_WORD_RE.finditer(text):
        # 已被 symbol 捕获范围吃掉的不重复加
        if any(s <= m.start() < e for s, e in money_spans):
            continue
        money_spans.append((m.start(), m.end()))
        found.append((m.start(), Entity(kind="money", raw=m.group(0).strip())))

    # 数量
    for m in _COUNT_RE.finditer(text):
        # 不能落在 money 范围内(避免"50 元"被同时算 count)
        if any(s <= m.start() < e for s, e in money_spans):
            continue
        found.append((m.start(), Entity(kind="count", raw=m.group(0).strip())))

    # 按 start 排,然后去重(同 kind+raw 保留首次)
    found.sort(key=lambda x: x[0])
    seen: set[tuple[str, str]] = set()
    out: list[Entity] = []
    for _, ent in found:
        key = (ent.kind, ent.raw)
        if key in seen:
            continue
        seen.add(key)
        # time 实体补 ISO normalize(失败保持空 iso,不阻塞输出)
        if ent.kind == "time":
            iso = normalize_time_entity(ent.raw, ent.note, reference=reference)
            if iso:
                ent.iso = iso
        out.append(ent)
    return out


def build_entity_context(
    text: str,
    *,
    max_entities: int = 8,
    reference: datetime | None = None,
) -> str:
    """主回复链路用:返回一段 system prompt 文本。

    无命中或全是 url/at_mention 时返回空(URL 和 @ AI 自己看得见,没必要再标)。
    只对 time/money/count/qq_id 这类容易漏读的实体提示。
    time 实体如果 normalize 成功,会附带 iso=YYYY-MM-DD 让 AI 直接复用。
    """
    ents = extract_entities(text, reference=reference)
    if not ents:
        return ""
    # 过滤:URL 和 @ 不进 prompt(AI 看得见原文)
    interesting = [e for e in ents if e.kind in {"time", "money", "count", "qq_id"}]
    if not interesting:
        return ""
    interesting = interesting[:max_entities]
    parts: list[str] = []
    for e in interesting:
        if e.kind == "time" and e.iso:
            # ISO 比 raw 更精确,优先标 ISO 后括号给 raw 让 AI 知道原话
            parts.append(f"time={e.iso}(原话『{e.raw}』)")
        elif e.note:
            parts.append(f"{e.kind}({e.note})={e.raw}")
        else:
            parts.append(f"{e.kind}={e.raw}")
    return (
        "群友消息里的关键实体(供你别漏读,可决定是否 catty_remember 或调 tool): "
        + "; ".join(parts) + "。"
    )
