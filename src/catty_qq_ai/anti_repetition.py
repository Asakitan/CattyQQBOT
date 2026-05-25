"""Anti-Repetition Tracker — 防笨猫连续 N 条回复都重复同样的猫系词/口头禅。

SillyTavern 有 "no_repeat_ngram"/sample 层做 token 级去重,但我们走的是 chat
completion API 不是本地推理,没法控制采样。改走 prompt 层:每条笨猫回复发出去后
扫描里面出现的 high-signal 猫系词(喵呜/ฅฅ/贴贴/杂鱼/...),记录到 per-scope
窗口里。下一轮 prompt 装配时如果某个词在最近 3 条里出现 ≥2 次,就插一句
『最近用过 X / Y 了,这条换个表达』提醒。

数据全在内存,scope 维度,3 小时 TTL,不持久化(纯短期防复读)。

接入位置:
- handle_chat 在 _build_messages 注册一个 catty_anti_repetition 段(order=820)
- 笨猫回复发出去后(_remember_bot_reply 等地方)调 record_bot_reply 记录
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

# 监控的猫系词/口头禅 — 这些重复多了就显得机械。
# 用 unicode 完整词匹配,因为部分是颜文字/符号。
_TRACKED_PHRASES: tuple[str, ...] = (
    "喵呜", "嗷呜", "ฅฅ", "贴贴", "蹭蹭", "爪爪",
    "杂鱼", "杂鱼主人", "笨蛋", "笨蛋主人",
    "尾巴摇摇", "尾巴一甩", "炸毛",
    "嗨喵", "诶?",
    "(*/ω＼*)", "(ฅ>ω<*ฅ)", "(=ↀωↀ=)", "(>ω<*)",
)

# 每个 scope 保留最近 N 条 bot 回复,只看里面出现过的词
_WINDOW_PER_SCOPE = 4
_TTL_SECONDS = 3 * 3600

# 同一词在窗口里出现 ≥ 这个次数就提醒
_REPEAT_THRESHOLD = 2

# scope -> deque[(timestamp, set_of_phrases_used_in_that_reply)]
_REPLY_HISTORY: dict[str, deque[tuple[float, frozenset[str]]]] = {}


def _phrases_in_text(text: str) -> frozenset[str]:
    """返回 text 里出现过的(且被跟踪的)词集合。"""
    if not text:
        return frozenset()
    found: set[str] = set()
    for phrase in _TRACKED_PHRASES:
        if phrase in text:
            found.add(phrase)
    return frozenset(found)


def _prune_scope(scope: str, now: float) -> None:
    dq = _REPLY_HISTORY.get(scope)
    if not dq:
        return
    while dq and (now - dq[0][0]) > _TTL_SECONDS:
        dq.popleft()
    if not dq:
        _REPLY_HISTORY.pop(scope, None)


def record_bot_reply(scope: str, reply_text: str, *, now: float | None = None) -> None:
    """笨猫每条回复发出去后调一次,记录用了哪些被跟踪的词。"""
    if not scope or not reply_text:
        return
    now = now or time.time()
    phrases = _phrases_in_text(reply_text)
    if not phrases:
        # 即使没命中也记录一个空 entry(占窗口位,避免空 reply 让旧 entry 永远卡住)
        phrases = frozenset()
    dq = _REPLY_HISTORY.setdefault(scope, deque(maxlen=_WINDOW_PER_SCOPE))
    dq.append((now, phrases))
    _prune_scope(scope, now)


def overused_phrases(scope: str, *, now: float | None = None) -> list[str]:
    """返回最近窗口里出现 ≥ _REPEAT_THRESHOLD 次的词,按出现次数降序。"""
    if not scope:
        return []
    now = now or time.time()
    _prune_scope(scope, now)
    dq = _REPLY_HISTORY.get(scope)
    if not dq or len(dq) < 2:
        return []
    counts: dict[str, int] = {}
    for _, phrases in dq:
        for p in phrases:
            counts[p] = counts.get(p, 0) + 1
    overused = [(p, c) for p, c in counts.items() if c >= _REPEAT_THRESHOLD]
    overused.sort(key=lambda kv: (-kv[1], kv[0]))
    return [p for p, _ in overused]


def build_anti_repetition_prompt(scope: str, *, now: float | None = None) -> str:
    """给主回复 LLM 看的提醒。空字符串表示没有过度重复,无需注入。"""
    overused = overused_phrases(scope, now=now)
    if not overused:
        return ""
    # 限 5 个避免列表过长
    display = "、".join(f"『{p}』" for p in overused[:5])
    return (
        f"【防复读提醒】最近本会话笨猫已经连续用过 {display}。"
        "这条回复**换一种猫系表达**(口头禅、动作、颜文字、自称都可以轮换)"
        ",避免群友觉得『又是同一句模板』。"
    )


def clear_scope(scope: str) -> None:
    _REPLY_HISTORY.pop(scope, None)


# 测试用
def _debug_dump(scope: str) -> list[tuple[float, list[str]]]:
    dq = _REPLY_HISTORY.get(scope)
    if not dq:
        return []
    return [(ts, sorted(ps)) for ts, ps in dq]


__all__ = [
    "record_bot_reply",
    "overused_phrases",
    "build_anti_repetition_prompt",
    "clear_scope",
]
