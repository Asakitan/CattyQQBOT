from __future__ import annotations

import contextvars
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_TEXT_FIELDS = (
    "trace_id", "parent_id", "task_id", "user_id", "scope", "persona", "tool", "error_type",
)
_COUNT_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "event_count")
_IDENTIFIER = re.compile(r"[^\w.:/@#-]", re.UNICODE)


def _identifier(value: Any) -> str:
    return _IDENTIFIER.sub("", str(value or ""))[:160]


def _count(value: Any) -> int:
    try:
        return max(0, min(int(value), 2**53 - 1))
    except (ValueError, TypeError, OverflowError):
        return 0


class TraceStore:
    def __init__(self, path: str | Path, *, max_events: int = 10000, retention_days: float = 7):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.max_events = max(1, int(max_events))
        self.retention_seconds = max(0.0, float(retention_days)) * 86400
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(target), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS trace_events ("
            "id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, kind TEXT NOT NULL, "
            + ", ".join(f"{field} TEXT NOT NULL DEFAULT ''" for field in _TEXT_FIELDS)
            + ", duration_ms REAL NOT NULL DEFAULT 0, "
            + ", ".join(f"{field} INTEGER NOT NULL DEFAULT 0" for field in _COUNT_FIELDS)
            + ")"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS trace_by_id ON trace_events(trace_id, id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS trace_by_task ON trace_events(task_id, id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS trace_by_time ON trace_events(timestamp)")
        self._db.commit()

    def record(self, kind: str, **metadata: Any) -> int:
        now = time.time()
        try:
            duration = max(0.0, min(float(metadata.get("duration_ms", 0)), 86400000.0))
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
        columns = ("timestamp", "kind", *_TEXT_FIELDS, "duration_ms", *_COUNT_FIELDS)
        values = (
            now, _identifier(kind), *(_identifier(metadata.get(field)) for field in _TEXT_FIELDS),
            duration, *(_count(metadata.get(field)) for field in _COUNT_FIELDS),
        )
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                cursor = self._db.execute(
                    f"INSERT INTO trace_events ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    values,
                )
                self._db.execute("DELETE FROM trace_events WHERE timestamp < ?", (now - self.retention_seconds,))
                self._db.execute(
                    "DELETE FROM trace_events WHERE id <= COALESCE("
                    "(SELECT id FROM trace_events ORDER BY id DESC LIMIT 1 OFFSET ?), 0)",
                    (self.max_events,),
                )
                return int(cursor.lastrowid or 0)

    def query(
        self, *, trace_id: str = "", task_id: str = "", scope: str = "", user_id: str = "",
        limit: int = 100, before: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["timestamp >= ?"]
        parameters: list[Any] = [time.time() - self.retention_seconds]
        for name, value in (("trace_id", trace_id), ("task_id", task_id), ("scope", scope), ("user_id", user_id)):
            if value:
                clauses.append(f"{name} = ?")
                parameters.append(_identifier(value))
        if before is not None:
            clauses.append("id < ?")
            parameters.append(_count(before))
        parameters.append(max(1, min(int(limit), 1000)))
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT * FROM trace_events WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT ?",
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]

    def export(self, **filters: Any) -> str:
        return json.dumps(self.query(**filters), ensure_ascii=False, separators=(",", ":"))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._db.close()


@dataclass
class _TraceLifetime:
    active: bool = True


@dataclass(frozen=True)
class TraceContext:
    store: TraceStore
    trace_id: str
    user_id: str
    scope: str
    persona: str
    parent_id: str = ""
    task_id: str = ""
    lifetime: _TraceLifetime = field(default_factory=_TraceLifetime)


_CURRENT_TRACE: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar("catty_task_trace", default=None)


def current_trace() -> TraceContext | None:
    context = _CURRENT_TRACE.get()
    return context if context is not None and context.lifetime.active else None


def bind_trace(store: TraceStore, trace_id: str, **identity: str) -> contextvars.Token:
    return _CURRENT_TRACE.set(TraceContext(
        store=store, trace_id=trace_id,
        **{name: str(identity.get(name) or "") for name in ("user_id", "scope", "persona", "parent_id", "task_id")},
    ))


def reset_trace(token: contextvars.Token) -> None:
    context = _CURRENT_TRACE.get()
    if context is not None:
        context.lifetime.active = False
    _CURRENT_TRACE.reset(token)


def record_event(kind: str, **metadata: Any) -> None:
    context = current_trace()
    if context is None:
        return
    try:
        context.store.record(kind, **{
            **metadata, "trace_id": context.trace_id, "parent_id": context.parent_id,
            "task_id": context.task_id or str(metadata.get("task_id", "")),
            "user_id": context.user_id, "scope": context.scope, "persona": context.persona,
        })
    except Exception:
        pass


def record_model_usage(usage: Mapping[str, Any]) -> None:
    if current_trace() is None:
        return
    record_event(
        "model_usage", input_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)),
        output_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)),
        cached_tokens=usage.get("cached_tokens", usage.get("cache_read_input_tokens", 0)), event_count=1,
    )