"""Time-bucketed conversation context sidecar for cache-friendly prompts.

This module keeps Catty's existing SessionCache intact, but records each completed
turn into coarse wall-clock buckets. Finalized buckets are serialized
byte-stably and can be hoisted/cached; the current bucket is represented by a
small parameter pointer only.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from nonebot import logger


def _sanitize_key_for_filename(key: str) -> str:
    safe = (
        key.replace(":", "__")
        .replace("/", "_")
        .replace("\\", "_")
        .replace("*", "_")
        .replace("?", "_")
        .replace("\"", "_")
        .replace("<", "_")
        .replace(">", "_")
        .replace("|", "_")
    )
    return safe.strip(". ") or "_"


def _clip_one_line(text: object, limit: int = 64) -> str:
    s = str(text or "")
    s = " ".join(s.replace("\r", "\n").split())
    if len(s) <= limit:
        return s
    return s[: max(limit - 1, 1)].rstrip() + "…"


def _bucket_id(ts: float, minutes: int) -> str:
    minutes = max(int(minutes), 1)
    lt = time.localtime(ts)
    slot = (lt.tm_min // minutes) * minutes
    return time.strftime("%Y%m%d-%H", lt) + f"-{slot:02d}"


def _bucket_bounds(ts: float, minutes: int) -> tuple[float, float]:
    minutes = max(int(minutes), 1)
    lt = time.localtime(ts)
    slot = (lt.tm_min // minutes) * minutes
    start_tuple = (
        lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, slot, 0,
        lt.tm_wday, lt.tm_yday, lt.tm_isdst,
    )
    start = time.mktime(start_tuple)
    return start, start + minutes * 60


class TimeBucketContextStore:
    """Persist coarse conversation buckets per scope without mutating history."""

    def __init__(
        self,
        directory: str | Path,
        *,
        group_minutes: int = 15,
        private_minutes: int = 30,
        max_finalized_buckets: int = 8,
        max_turns_per_bucket: int = 24,
        enabled: bool = True,
    ) -> None:
        self._dir = Path(directory)
        self._group_minutes = max(int(group_minutes), 1)
        self._private_minutes = max(int(private_minutes), 1)
        self._max_finalized = max(int(max_finalized_buckets), 1)
        self._max_turns = max(int(max_turns_per_bucket), 1)
        self._enabled = bool(enabled)
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded: set[str] = set()
        self._dirty: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def directory(self) -> Path:
        return self._dir

    def minutes_for_scope(self, scope: str, *, is_group: bool | None = None) -> int:
        if is_group is None:
            is_group = scope.startswith("group:")
        return self._group_minutes if is_group else self._private_minutes

    def current_bucket_id(self, scope: str, *, is_group: bool | None = None, now: float | None = None) -> str:
        return _bucket_id(now or time.time(), self.minutes_for_scope(scope, is_group=is_group))

    def record_turn(
        self,
        scope: str,
        user_content: object,
        assistant_content: object,
        *,
        is_group: bool | None = None,
        now: float | None = None,
    ) -> None:
        if not self._enabled or not scope:
            return
        now = now or time.time()
        minutes = self.minutes_for_scope(scope, is_group=is_group)
        bucket = _bucket_id(now, minutes)
        start, end = _bucket_bounds(now, minutes)
        state = self._load_scope(scope)
        current = state.get("current") if isinstance(state.get("current"), dict) else None
        if not current or current.get("bucket") != bucket:
            if current and current.get("turns"):
                self._finalize_current(state, current)
            current = {
                "bucket": bucket,
                "start": int(start),
                "end": int(end),
                "minutes": minutes,
                "turns": [],
            }
            state["current"] = current
        turns = current.setdefault("turns", [])
        if not isinstance(turns, list):
            turns = []
            current["turns"] = turns
        turns.append({
            "t": int(now),
            "u": _clip_one_line(user_content, 96),
            "a": _clip_one_line(assistant_content, 96),
        })
        if len(turns) > self._max_turns:
            del turns[: len(turns) - self._max_turns]
        self._dirty.add(scope)
        self.flush_scope(scope)

    def roll_current_if_needed(
        self,
        scope: str,
        *,
        is_group: bool | None = None,
        now: float | None = None,
    ) -> bool:
        """Finalize the previous current bucket if wall-clock moved into a new bucket.

        This is called on the read/build path before generating prompts, so the first
        message of a new time segment can already see the finalized previous bucket.
        It does not create an empty new bucket; `record_turn()` creates it after the
        assistant reply is known.
        """
        if not self._enabled or not scope:
            return False
        now = now or time.time()
        minutes = self.minutes_for_scope(scope, is_group=is_group)
        bucket = _bucket_id(now, minutes)
        state = self._load_scope(scope)
        current = state.get("current") if isinstance(state.get("current"), dict) else None
        if not current or current.get("bucket") == bucket:
            return False
        if current.get("turns"):
            self._finalize_current(state, current)
        state["current"] = None
        self._dirty.add(scope)
        self.flush_scope(scope)
        return True

    def build_stable_summary_prompt(self, scope: str, *, limit: int = 3) -> str:
        if not self._enabled or not scope:
            return ""
        state = self._load_scope(scope)
        finalized = state.get("finalized") if isinstance(state.get("finalized"), list) else []
        if not finalized:
            return ""
        rows = finalized[-max(int(limit), 1):]
        lines = [
            "【时间桶上下文摘要】这些是已经封存的会话时间段摘要; 同一 bucket 内容稳定, 可当作长期上下文, 不要复述标签。"
        ]
        for item in rows:
            if not isinstance(item, dict):
                continue
            bucket = str(item.get("bucket") or "")
            turns = int(item.get("turn_count") or 0)
            user_line = _clip_one_line(item.get("user_digest"), 88)
            catty_line = _clip_one_line(item.get("catty_digest"), 88)
            if bucket:
                lines.append(f"- bucket={bucket}; turns={turns}; user≈{user_line}; catty≈{catty_line}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def build_current_params_prompt(self, scope: str, *, is_group: bool | None = None, now: float | None = None) -> str:
        if not self._enabled or not scope:
            return ""
        now = now or time.time()
        minutes = self.minutes_for_scope(scope, is_group=is_group)
        bucket = _bucket_id(now, minutes)
        state = self._load_scope(scope)
        current = state.get("current") if isinstance(state.get("current"), dict) else None
        current_turns = 0
        if current and current.get("bucket") == bucket and isinstance(current.get("turns"), list):
            current_turns = len(current.get("turns") or [])
        finalized = state.get("finalized") if isinstance(state.get("finalized"), list) else []
        scope_type = "group" if (is_group if is_group is not None else scope.startswith("group:")) else "private"
        return f"【TIME_BUCKET】s={scope_type};b={bucket};m={minutes};cur={current_turns};fin={len(finalized)}"

    def flush_sync(self) -> int:
        written = 0
        for scope in list(self._dirty):
            if self.flush_scope(scope):
                written += 1
        return written

    def flush_scope(self, scope: str) -> bool:
        if not self._enabled or scope not in self._dirty:
            return False
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._path(scope)
            tmp = path.with_suffix(path.suffix + ".tmp")
            payload = self._data.get(scope) or {"key": scope, "current": None, "finalized": []}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
            self._dirty.discard(scope)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"time_bucket_context: failed to write {scope}: {exc}")
            return False

    def _path(self, scope: str) -> Path:
        return self._dir / f"{_sanitize_key_for_filename(scope)}.json"

    def _load_scope(self, scope: str) -> dict[str, Any]:
        if scope in self._loaded:
            return self._data.setdefault(scope, {"key": scope, "current": None, "finalized": []})
        self._loaded.add(scope)
        state: dict[str, Any] = {"key": scope, "current": None, "finalized": []}
        path = self._path(scope)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    state.update(raw)
                    state["key"] = scope
                    if not isinstance(state.get("finalized"), list):
                        state["finalized"] = []
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"time_bucket_context: failed to read {path.name}: {exc}")
        self._data[scope] = state
        return state

    def _finalize_current(self, state: dict[str, Any], current: dict[str, Any]) -> None:
        turns = current.get("turns") if isinstance(current.get("turns"), list) else []
        if not turns:
            return
        user_parts = []
        catty_parts = []
        for turn in turns[-6:]:
            if isinstance(turn, dict):
                u = _clip_one_line(turn.get("u"), 48)
                a = _clip_one_line(turn.get("a"), 48)
                if u:
                    user_parts.append(u)
                if a:
                    catty_parts.append(a)
        finalized = state.setdefault("finalized", [])
        if not isinstance(finalized, list):
            finalized = []
            state["finalized"] = finalized
        finalized.append({
            "bucket": str(current.get("bucket") or ""),
            "start": int(current.get("start") or 0),
            "end": int(current.get("end") or 0),
            "minutes": int(current.get("minutes") or 0),
            "turn_count": len(turns),
            "user_digest": " / ".join(user_parts)[:240],
            "catty_digest": " / ".join(catty_parts)[:240],
        })
        if len(finalized) > self._max_finalized:
            del finalized[: len(finalized) - self._max_finalized]


__all__ = ["TimeBucketContextStore"]
