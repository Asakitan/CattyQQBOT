"""轻量级工具调用遥测，记录进程内最近若干轮的聚合事件。"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
from threading import RLock
import json
import time
from typing import Any


MAX_TURNS = 500
JSONL_MAX_BYTES = 2 * 1024 * 1024

_TURNS: deque[dict[str, Any]] = deque(maxlen=MAX_TURNS)
_LOCK = RLock()


def _scope_matches(event_scope: str, query_scope: str) -> bool:
    """scope 归一化匹配 (Review 2026-08-15 major 修正)。

    start_turn 用 history_key (group_history_scope=user 时是 ``group:<id>:user:<id>``),
    而 tools.py 执行挂钩只拿得到群级 ``group:<id>``。两边方向不定, 双向前缀匹配:
    完全相等, 或一方是另一方的前缀 (以 ``:`` 分隔)。
    """
    if event_scope == query_scope:
        return True
    return event_scope.startswith(query_scope + ":") or query_scope.startswith(event_scope + ":")


def start_turn(scope: str, persona: str, tools_offered: int) -> int:
    event = {
        "ts": time.time(),
        "scope": str(scope),
        "persona": str(persona),
        "tools_offered": int(tools_offered),
        "tool_calls": [],
        "reply_used_result": None,
    }
    with _LOCK:
        _TURNS.append(event)
        return len(_TURNS) - 1


def record_call(
    scope: str,
    name: str,
    *,
    success: bool,
    result_nonempty: bool,
    result_chars: int,
) -> None:
    with _LOCK:
        for event in reversed(_TURNS):
            if not _scope_matches(event["scope"], scope):
                continue
            event["tool_calls"].append(
                {
                    "name": str(name),
                    "success": bool(success),
                    "result_nonempty": bool(result_nonempty),
                    "result_chars": int(result_chars),
                }
            )
            return


def mark_reply_used(scope: str, used: bool) -> None:
    with _LOCK:
        for event in reversed(_TURNS):
            if _scope_matches(event["scope"], scope):
                event["reply_used_result"] = bool(used)
                return


def summarize(*, last_n: int = 100, scope: str | None = None) -> dict[str, Any]:
    with _LOCK:
        events = [
            event for event in _TURNS
            if scope is None or _scope_matches(event["scope"], scope)
        ]
        events = events[-last_n:] if last_n > 0 else []
        calls = [call for event in events for call in event["tool_calls"]]
        turns = len(events)
        turns_with_call = sum(bool(event["tool_calls"]) for event in events)
        used_events = [
            event for event in events
            if event["reply_used_result"] is not None
        ]

    call_count = len(calls)
    success_count = sum(bool(call["success"]) for call in calls)
    nonempty_count = sum(bool(call["result_nonempty"]) for call in calls)
    used_count = sum(bool(event["reply_used_result"]) for event in used_events)
    return {
        "turns": turns,
        "turns_with_call": turns_with_call,
        "call_rate": turns_with_call / turns if turns else 0.0,
        "tool_calls_total": call_count,
        "success_rate": success_count / call_count if call_count else 0.0,
        "nonempty_rate": nonempty_count / call_count if call_count else 0.0,
        "used_rate": used_count / len(used_events) if used_events else 0.0,
    }


def dump_jsonl(memory_dir: str | Path) -> None:
    path = Path(memory_dir) / "tool_telemetry.jsonl"
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        events = deepcopy(list(_TURNS))
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        if path.stat().st_size <= JSONL_MAX_BYTES:
            return
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[len(lines) // 2:]))


def recent_events(n: int = 20, scope: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        events = [
            event for event in _TURNS
            if scope is None or event["scope"] == scope
        ]
        return deepcopy(events[-n:] if n > 0 else [])
