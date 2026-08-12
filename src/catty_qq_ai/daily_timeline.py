from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_DEFAULT_PERSONA = "catty"
_KINDS = {"planned", "observed"}
_STATUSES = {"open", "done", "cancelled", "overdue"}


def _text(value: Any, limit: int | None = None) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:limit] if limit is not None else text


def _scope(value: Any) -> str:
    return _text(value)


def _persona(value: Any) -> str:
    return _text(value or _DEFAULT_PERSONA).lower() or _DEFAULT_PERSONA


def _now(value: Any, clock: Callable[[], Any]) -> datetime:
    value = clock() if value is None else value
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).astimezone()
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        parsed = datetime.now().astimezone()
    return parsed if parsed.tzinfo else parsed.astimezone()


def _day(value: Any, fallback: date) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return fallback.isoformat()


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class DailyTimelineStore:
    def __init__(self, path: str | Path, *, retention_days: int = 30, max_items: int = 64, max_chars: int = 12_000, clock: Callable[[], Any] | None = None) -> None:
        self._path = Path(path).expanduser()
        self._retention_days = max(int(retention_days), 1)
        self._max_items = max(int(max_items), 1)
        self._max_chars = max(int(max_chars), 1)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._load()

    def _items(self, scope: str, persona: str, create: bool = False) -> list[dict[str, Any]]:
        if create:
            return self._data.setdefault(scope, {}).setdefault(persona, [])
        return self._data.get(scope, {}).get(persona, [])

    def _normalise(self, raw: Any, now: datetime, default_day: Any = None) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        title = _text(raw.get("title"), 240)
        if not title:
            return None
        kind = _text(raw.get("kind") or "planned").lower()
        status = _text(raw.get("status") or "open").lower()
        kind = kind if kind in _KINDS else "planned"
        status = status if status in _STATUSES else "open"
        created = _text(raw.get("created_at"), 80) or _stamp(now)
        fallback = (default_day or now).date() if isinstance(default_day or now, datetime) else now.date()
        return {
            "id": _text(raw.get("id"), 80) or f"timeline_{uuid.uuid4().hex[:10]}",
            "day": _day(raw.get("day"), fallback),
            "kind": kind,
            "status": status,
            "title": title,
            "details": _text(raw.get("details"), 1200),
            "due_at": _text(raw.get("due_at"), 80),
            "source": _text(raw.get("source"), 160),
            "evidence": _text(raw.get("evidence"), 800),
            "subject": _text(raw.get("subject"), 160),
            "created_at": created,
            "updated_at": _text(raw.get("updated_at"), 80) or created,
            "completed_at": _text(raw.get("completed_at"), 80) or (created if status == "done" else ""),
        }

    def _load_items(self, scope: Any, persona: Any, values: Iterable[Any]) -> None:
        scope_name = _scope(scope)
        if not scope_name:
            return
        now = datetime.now().astimezone()
        parsed = [item for value in values if (item := self._normalise(value, now)) is not None]
        if parsed:
            self._data.setdefault(scope_name, {})[_persona(persona)] = parsed

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return
        if isinstance(raw, dict) and isinstance(raw.get("scopes"), dict):
            raw = raw["scopes"]
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            raw = raw["items"]
        if isinstance(raw, list):
            for value in raw:
                if isinstance(value, dict):
                    self._load_items(value.get("scope"), value.get("persona"), [value])
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if isinstance(value, list):
                if "::" in str(key):
                    scope, persona = str(key).split("::", 1)
                elif "|" in str(key):
                    scope, persona = str(key).rsplit("|", 1)
                else:
                    scope, persona = key, _DEFAULT_PERSONA
                self._load_items(scope, persona, value)
            elif isinstance(value, dict):
                for persona, items in value.items():
                    if isinstance(items, list):
                        self._load_items(key, persona, items)

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            temp.write_text(json.dumps({"version": 1, "scopes": self._data}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(self._path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def flush_sync(self) -> bool:
        with self._lock:
            try:
                self._write_locked()
            except OSError:
                return False
            return True

    def _prepare_locked(self, current: datetime) -> bool:
        changed = False
        cutoff = current.date() - timedelta(days=self._retention_days)
        for scope, personas in list(self._data.items()):
            for persona, items in list(personas.items()):
                kept = []
                for item in items:
                    item_day = _day(item.get("day"), current.date())
                    if item.get("status") == "open" and item_day < current.date().isoformat():
                        item["status"] = "overdue"
                        item["updated_at"] = _stamp(current)
                        changed = True
                    if item_day >= cutoff.isoformat():
                        kept.append(item)
                    else:
                        changed = True
                items[:] = kept
                if not items:
                    personas.pop(persona, None)
            if not personas:
                self._data.pop(scope, None)
        for personas in self._data.values():
            for items in personas.values():
                items.sort(key=lambda item: (item.get("day", ""), item.get("updated_at", ""), item.get("id", "")))
                while len(items) > self._max_items:
                    items.pop(0)
                    changed = True
                while len(items) > 1 and len(json.dumps(items, ensure_ascii=False, separators=(",", ":"))) > self._max_chars:
                    items.pop(0)
                    changed = True
                if items and len(json.dumps(items, ensure_ascii=False, separators=(",", ":"))) > self._max_chars:
                    items[-1]["details"] = _text(items[-1].get("details"), 120)
                    items[-1]["evidence"] = _text(items[-1].get("evidence"), 80)
                    items[-1]["title"] = _text(items[-1].get("title"), 80)
                    changed = True
        return changed

    def add(self, scope: str, title: str, *, persona: str = _DEFAULT_PERSONA, day: Any = None, kind: str = "planned", status: str = "open", details: str = "", due_at: Any = None, source: str = "", evidence: str = "", subject: str = "", item_id: str | None = None, now: Any = None) -> dict[str, Any] | None:
        scope_name, persona_name = _scope(scope), _persona(persona)
        if not scope_name:
            return None
        current = _now(now, self._clock)
        item = self._normalise({"id": item_id, "day": day or current.date().isoformat(), "kind": kind, "status": status, "title": title, "details": details, "due_at": due_at, "source": source, "evidence": evidence, "subject": subject, "created_at": _stamp(current), "updated_at": _stamp(current)}, current, current)
        if item is None:
            return None
        with self._lock:
            self._prepare_locked(current)
            self._items(scope_name, persona_name, True).append(item)
            self._prepare_locked(current)
            self._write_locked()
            return dict(item)

    def update(self, scope: str, item_id: str, *, persona: str = _DEFAULT_PERSONA, now: Any = None, **changes: Any) -> dict[str, Any] | None:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        with self._lock:
            prepared = self._prepare_locked(current)
            for item in self._items(scope_name, persona_name):
                if item.get("id") != item_id:
                    continue
                for field in ("day", "kind", "status", "title", "details", "due_at", "source", "evidence", "subject"):
                    if field not in changes:
                        continue
                    value = changes[field]
                    if field == "day":
                        item[field] = _day(value, current.date())
                    elif field == "kind":
                        item[field] = str(value).lower() if str(value).lower() in _KINDS else "planned"
                    elif field == "status":
                        item[field] = str(value).lower() if str(value).lower() in _STATUSES else "open"
                    else:
                        item[field] = _text(value, 1200 if field == "details" else 800 if field == "evidence" else 240)
                item["updated_at"] = _stamp(current)
                if item["status"] == "done" and not item.get("completed_at"):
                    item["completed_at"] = _stamp(current)
                self._prepare_locked(current)
                self._write_locked()
                return dict(item)
            if prepared:
                self._write_locked()
        return None

    def complete(self, scope: str, item_id: str, *, persona: str = _DEFAULT_PERSONA, now: Any = None) -> dict[str, Any] | None:
        return self.update(scope, item_id, persona=persona, status="done", now=now)

    def remove(self, scope: str, item_id: str, *, persona: str = _DEFAULT_PERSONA, now: Any = None) -> bool:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        with self._lock:
            prepared = self._prepare_locked(current)
            items = self._items(scope_name, persona_name)
            kept = [item for item in items if item.get("id") != item_id]
            if len(kept) == len(items):
                if prepared:
                    self._write_locked()
                return False
            if kept:
                self._data[scope_name][persona_name] = kept
            else:
                self._data.get(scope_name, {}).pop(persona_name, None)
                if not self._data.get(scope_name):
                    self._data.pop(scope_name, None)
            self._write_locked()
            return True

    def list(self, scope: str, persona: str = _DEFAULT_PERSONA, *, day: Any = None, status: str | None = None, statuses: Iterable[str] | None = None, now: Any = None) -> list[dict[str, Any]]:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        with self._lock:
            prepared = self._prepare_locked(current)
            wanted_day = _day(day, current.date()) if day is not None else None
            wanted_statuses = {str(value).lower() for value in statuses} if statuses is not None else None
            result = [dict(item) for item in self._items(scope_name, persona_name) if (wanted_day is None or item["day"] == wanted_day) and (status is None or item["status"] == str(status).lower()) and (wanted_statuses is None or item["status"] in wanted_statuses)]
            result.sort(key=lambda item: (item.get("day", ""), item.get("updated_at", ""), item.get("id", "")))
            if prepared:
                self._write_locked()
            return result

    def clear(self, scope: str, persona: str = _DEFAULT_PERSONA, *, now: Any = None) -> int:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        with self._lock:
            prepared = self._prepare_locked(current)
            count = len(self._items(scope_name, persona_name))
            self._data.get(scope_name, {}).pop(persona_name, None)
            if not self._data.get(scope_name):
                self._data.pop(scope_name, None)
            if count or prepared:
                self._write_locked()
            return count

    def record_tool_activity(self, scope: str, tool: str, *, persona: str = _DEFAULT_PERSONA, success: bool = True, title: str | None = None, details: str = "", evidence: str = "", subject: str = "", source: str | None = None, day: Any = None, due_at: Any = None, now: Any = None) -> dict[str, Any] | None:
        tool_name = _text(tool, 120)
        if not success or not tool_name:
            return None
        return self.add(scope, title or f"{tool_name} 成功", persona=persona, day=day, kind="observed", status="done", details=details, evidence=evidence, subject=subject, source=source or tool_name, due_at=due_at, now=now)

    def build_prompt(self, scope: str, persona: str = _DEFAULT_PERSONA, *, today: Any = None, now: Any = None) -> str:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        today_text = _day(today, current.date()) if today is not None else current.date().isoformat()
        yesterday = (date.fromisoformat(today_text) - timedelta(days=1)).isoformat()
        with self._lock:
            prepared = self._prepare_locked(current)
            items = [dict(item) for item in self._items(scope_name, persona_name)]
            if prepared:
                self._write_locked()
        today_items = [item for item in items if item["day"] == today_text and item["status"] in {"done", "open"}]
        overdue = [item for item in items if item["day"] == yesterday and item["status"] == "overdue"]
        lines = [f"【日程记录·{persona_name}/{scope_name}】", f"今天（{today_text}）："]
        lines.extend([f"- [{'已完成' if item['status'] == 'done' else '待处理'}] {item['title']}" + (f"：{item['details']}" if item.get("details") else "") for item in today_items] or ["- 没有记录"])
        lines.append(f"昨天逾期（{yesterday}）：")
        lines.extend([f"- {item['title']}" + (f"：{item['details']}" if item.get("details") else "") for item in overdue] or ["- 没有记录"])
        lines.append("以上只包含当前范围和当前人格的真实记录；没有记录就不要编造，也不要把推测写成事实。")
        return "\n".join(lines)

    add_item = add
    update_item = update
    complete_item = complete
    remove_item = remove
    list_items = list
    clear_items = clear


__all__ = ["DailyTimelineStore"]
