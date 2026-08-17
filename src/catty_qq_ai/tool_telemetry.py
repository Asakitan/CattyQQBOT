"""轻量级工具调用遥测，记录进程内最近若干轮的聚合事件。"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
from threading import RLock
import json
import time
from typing import Any
import uuid


MAX_TURNS = 500
JSONL_MAX_BYTES = 2 * 1024 * 1024
_REPLY_USAGE = {"reported", "used", "not_used", "contradicted"}
_FINAL_REPLY_USAGE = {"used", "not_used", "contradicted"}
_REPLY_USAGE_ALIASES = {
    "used_directly": "used",
    "used_indirectly": "used",
    "unused": "not_used",
    "unknown": "reported",
    "true": "used",
    "yes": "used",
    "1": "used",
    "false": "not_used",
    "no": "not_used",
    "0": "not_used",
}

_TURNS: deque[dict[str, Any]] = deque(maxlen=MAX_TURNS)
_LOCK = RLock()
_PERSISTED_REVISIONS: dict[str, dict[str, int]] = {}


def _scope_matches(event_scope: str, query_scope: str) -> bool:
    if event_scope == query_scope:
        return True
    return event_scope.startswith(query_scope + ":") or query_scope.startswith(event_scope + ":")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _bump_revision(event: dict[str, Any]) -> None:
    event["revision"] = int(event.get("revision", 1) or 1) + 1


def _find_turn(scope: str, turn_id: str | None) -> dict[str, Any] | None:
    normalized_scope = str(scope or "").strip()
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_scope or not normalized_turn_id:
        return None
    for event in reversed(_TURNS):
        if (
            str(event.get("turn_id", "")) == normalized_turn_id
            and str(event.get("scope", "")) == normalized_scope
        ):
            return event
    return None


def _normalize_reply_usage(value: bool | str | None, usage: str | None = None) -> str | None:
    candidate: Any = usage if usage is not None else value
    if isinstance(candidate, bool):
        return "used" if candidate else "not_used"
    normalized = str(candidate or "").strip().lower()
    if normalized in _REPLY_USAGE:
        return normalized
    return _REPLY_USAGE_ALIASES.get(normalized)


def start_turn(scope: str, persona: str, tools_offered: int) -> str:
    turn_id = _new_id("turn")
    event = {
        "event_id": turn_id,
        "turn_id": turn_id,
        "revision": 1,
        "ts": time.time(),
        "scope": str(scope),
        "persona": str(persona),
        "tools_offered": int(tools_offered),
        "tool_calls": [],
        "reply_used_result": None,
        "reply_usage": None,
    }
    with _LOCK:
        _TURNS.append(event)
        return turn_id


def record_call(
    scope: str,
    name: str,
    *,
    success: bool,
    result_nonempty: bool,
    result_chars: int,
    turn_id: str,
    tool_call_id: str | None = None,
) -> None:
    with _LOCK:
        event = _find_turn(scope, turn_id)
        if event is None:
            return
        event["tool_calls"].append(
            {
                "tool_call_id": str(tool_call_id or _new_id("tool")),
                "name": str(name),
                "success": bool(success),
                "result_nonempty": bool(result_nonempty),
                "result_chars": int(result_chars),
            }
        )
        if event.get("reply_usage") is None:
            event["reply_used_result"] = "reported"
            event["reply_usage"] = "reported"
        _bump_revision(event)


def mark_reply_used(
    scope: str,
    used: bool | str | None = None,
    *,
    turn_id: str,
    usage: str | None = None,
) -> None:
    with _LOCK:
        event = _find_turn(scope, turn_id)
        if event is None:
            return
        normalized = _normalize_reply_usage(used, usage)
        if normalized is None:
            return
        if event.get("reply_used_result") != normalized:
            event["reply_used_result"] = normalized
            event["reply_usage"] = normalized
            _bump_revision(event)


def summarize(*, last_n: int = 100, scope: str | None = None) -> dict[str, Any]:
    with _LOCK:
        events = [
            event
            for event in _TURNS
            if scope is None or _scope_matches(str(event.get("scope", "")), scope)
        ]
        events = events[-last_n:] if last_n > 0 else []
        calls = [call for event in events for call in event["tool_calls"]]
        turns = len(events)
        turns_with_call = sum(bool(event["tool_calls"]) for event in events)
        classified_events = [event for event in events if event.get("reply_used_result") in _FINAL_REPLY_USAGE]

    call_count = len(calls)
    success_count = sum(bool(call["success"]) for call in calls)
    nonempty_count = sum(bool(call["result_nonempty"]) for call in calls)
    usage_counts = {
        usage: sum(event.get("reply_used_result") == usage for event in events)
        for usage in _REPLY_USAGE
    }
    used_count = usage_counts["used"]
    return {
        "turns": turns,
        "turns_with_call": turns_with_call,
        "call_rate": turns_with_call / turns if turns else 0.0,
        "tool_calls_total": call_count,
        "success_rate": success_count / call_count if call_count else 0.0,
        "nonempty_rate": nonempty_count / call_count if call_count else 0.0,
        "used_rate": used_count / len(classified_events) if classified_events else 0.0,
        "contradicted_count": usage_counts["contradicted"],
        "reply_usage": usage_counts,
    }


def dump_jsonl(memory_dir: str | Path) -> None:
    path = Path(memory_dir) / "tool_telemetry.jsonl"
    path_key = str(path.resolve())
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        persisted = _PERSISTED_REVISIONS.setdefault(path_key, {})
        if not persisted and path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                lines = []
            for line in lines:
                try:
                    saved = json.loads(line)
                except (TypeError, ValueError):
                    continue
                saved_turn_id = str(saved.get("turn_id", "") or "")
                if not saved_turn_id:
                    continue
                try:
                    revision = int(saved.get("revision", 1) or 1)
                except (TypeError, ValueError):
                    revision = 1
                persisted[saved_turn_id] = max(persisted.get(saved_turn_id, 0), revision)

        events = deepcopy(list(_TURNS))
        pending = [
            event
            for event in events
            if int(event.get("revision", 1) or 1)
            > persisted.get(str(event.get("turn_id", "")), 0)
        ]
        if pending:
            with path.open("a", encoding="utf-8") as handle:
                for event in pending:
                    handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
                    persisted[str(event["turn_id"])] = int(event["revision"])
        if not path.exists() or path.stat().st_size <= JSONL_MAX_BYTES:
            return
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[len(lines) // 2 :]))


def recent_events(n: int = 20, scope: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        events = [
            event
            for event in _TURNS
            if scope is None or _scope_matches(str(event.get("scope", "")), str(scope))
        ]
        return deepcopy(events[-n:] if n > 0 else [])











