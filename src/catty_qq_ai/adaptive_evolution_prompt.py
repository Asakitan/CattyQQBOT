from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_DEFAULT_PERSONA = "catty"
_MARKER_RE = re.compile(r"(?ix)<\|(?:system|developer|assistant|internal|cache|prompt_cache)[^|]*\|>|\[/?(?:internal|cache|prompt[_ -]?cache|system[_ -]?prompt)[^\]]*\]|\b(?:internal|cache|prompt[_ -]?cache|dynamic[_ -]?context|system[_ -]?prompt|assistant[_ -]?cache)(?:[_ -](?:key|hit|miss|prefix|state|marker|entry))?\b")
_BLOCK_RE = re.compile(r"(?is)\[(?:internal|cache|prompt[_ -]?cache|system[_ -]?prompt)[^\]]*\].*?\[/(?:internal|cache|prompt[_ -]?cache|system[_ -]?prompt)\]")


def _text(value: Any, limit: int | None = None) -> str:
    value = " ".join(str(value or "").strip().split())
    return value[:limit] if limit is not None else value


def _scope(value: Any) -> str:
    return _text(value)


def _persona(value: Any) -> str:
    return _text(value or _DEFAULT_PERSONA).lower() or _DEFAULT_PERSONA


def _sanitize(value: Any, limit: int | None = None) -> str:
    value = _BLOCK_RE.sub(" ", str(value or ""))
    return _text(_MARKER_RE.sub(" ", value), limit)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now(value: Any, clock: Callable[[], Any]) -> datetime:
    value = clock() if value is None else value
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        parsed = datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dedupe(value: str) -> str:
    return " ".join(value.casefold().split())


class AdaptiveEvolutionPromptStore:
    def __init__(self, path: str | Path, *, default_ttl_hours: float = 24, max_ttl_days: float = 7, max_entries: int = 8, max_chars: int = 2048, clock: Callable[[], Any] | None = None) -> None:
        self._path = Path(path).expanduser()
        self._default_ttl_hours = max(float(default_ttl_hours), 0.0)
        self._max_ttl = timedelta(days=max(float(max_ttl_days), 0.0))
        self._max_entries = max(int(max_entries), 1)
        self._max_chars = max(int(max_chars), 1)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._load()

    def _entries(self, scope: str, persona: str, create: bool = False) -> list[dict[str, Any]]:
        if create:
            return self._data.setdefault(scope, {}).setdefault(persona, [])
        return self._data.get(scope, {}).get(persona, [])

    def _normalise(self, raw: Any, now: datetime) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        content = _sanitize(raw.get("content"), 1200)
        if not content:
            return None
        created = _text(raw.get("created"), 80) or _stamp(now)
        return {
            "id": _text(raw.get("id"), 80) or f"adaptive_{uuid.uuid4().hex[:10]}",
            "content": content,
            "reason": _sanitize(raw.get("reason"), 400),
            "created": created,
            "updated": _text(raw.get("updated"), 80) or created,
            "expires": _text(raw.get("expires"), 80) or _stamp(now + timedelta(hours=self._default_ttl_hours)),
            "enabled": bool(raw.get("enabled", True)),
            "use_count": max(int(raw.get("use_count") or 0), 0),
        }

    def _load_entries(self, scope: Any, persona: Any, values: Iterable[Any]) -> None:
        scope_name = _scope(scope)
        if not scope_name:
            return
        now = datetime.now(timezone.utc)
        parsed = [entry for value in values if (entry := self._normalise(value, now)) is not None]
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
        if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
            raw = raw["entries"]
        if isinstance(raw, list):
            for value in raw:
                if isinstance(value, dict):
                    self._load_entries(value.get("scope"), value.get("persona"), [value])
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
                self._load_entries(scope, persona, value)
            elif isinstance(value, dict):
                for persona, entries in value.items():
                    if isinstance(entries, list):
                        self._load_entries(key, persona, entries)

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

    def _purge_locked(self, now: datetime) -> bool:
        changed = False
        for scope, personas in list(self._data.items()):
            for persona, entries in list(personas.items()):
                kept = []
                for entry in entries:
                    expires = _time(entry.get("expires"))
                    if expires is None or expires > now:
                        kept.append(entry)
                    else:
                        changed = True
                entries[:] = kept
                if not entries:
                    personas.pop(persona, None)
            if not personas:
                self._data.pop(scope, None)
        return changed

    def _size(self, entries: list[dict[str, Any]]) -> int:
        return len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))

    def _trim_locked(self) -> bool:
        changed = False
        for scope, personas in list(self._data.items()):
            for persona, entries in list(personas.items()):
                entries.sort(key=lambda entry: (entry.get("updated", ""), entry.get("created", "")))
                while len(entries) > self._max_entries:
                    entries.pop(0)
                    changed = True
                while len(entries) > 1 and self._size(entries) > self._max_chars:
                    entries.pop(0)
                    changed = True
                if entries and self._size(entries) > self._max_chars:
                    entry = entries[-1]
                    entry["reason"] = _sanitize(entry.get("reason"), 80)
                    entry["content"] = _sanitize(entry.get("content"), max(1, self._max_chars // 2))
                    while self._size(entries) > self._max_chars and len(entry["content"]) > 1:
                        entry["content"] = entry["content"][:-1]
                    changed = True
                if not entries:
                    personas.pop(persona, None)
            if not personas:
                self._data.pop(scope, None)
        return changed

    def add(self, scope: str, content: str, *, persona: str = _DEFAULT_PERSONA, reason: str = "", expires: Any = None, ttl_hours: float | None = None, enabled: bool = True, entry_id: str | None = None, now: Any = None) -> dict[str, Any] | None:
        scope_name, persona_name = _scope(scope), _persona(persona)
        clean = _sanitize(content, 1200)
        if not scope_name or not clean:
            return None
        current = _now(now, self._clock)
        with self._lock:
            changed = self._purge_locked(current)
            for entry in self._entries(scope_name, persona_name):
                if _dedupe(entry.get("content", "")) == _dedupe(clean):
                    if changed:
                        self._write_locked()
                    return dict(entry)
            if expires is None:
                ttl = self._default_ttl_hours if ttl_hours is None else max(float(ttl_hours), 0.0)
                expiry = current + timedelta(hours=ttl)
            else:
                expiry = _time(expires) or (current + timedelta(hours=self._default_ttl_hours))
            expiry = min(expiry, current + self._max_ttl)
            entry = {"id": _text(entry_id, 80) or f"adaptive_{uuid.uuid4().hex[:10]}", "content": clean, "reason": _sanitize(reason, 400), "created": _stamp(current), "updated": _stamp(current), "expires": _stamp(expiry), "enabled": bool(enabled), "use_count": 0}
            self._entries(scope_name, persona_name, True).append(entry)
            self._trim_locked()
            self._write_locked()
            return dict(entry)

    def update(self, scope: str, entry_id: str, *, persona: str = _DEFAULT_PERSONA, now: Any = None, **changes: Any) -> dict[str, Any] | None:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        with self._lock:
            prepared = self._purge_locked(current)
            for entry in self._entries(scope_name, persona_name):
                if entry.get("id") != entry_id:
                    continue
                if "content" in changes:
                    entry["content"] = _sanitize(changes["content"], 1200)
                    if not entry["content"]:
                        return None
                if "reason" in changes:
                    entry["reason"] = _sanitize(changes["reason"], 400)
                if "enabled" in changes:
                    entry["enabled"] = bool(changes["enabled"])
                if "expires" in changes:
                    entry["expires"] = _stamp(min(_time(changes["expires"]) or current, current + self._max_ttl))
                if "ttl_hours" in changes:
                    entry["expires"] = _stamp(min(current + timedelta(hours=max(float(changes["ttl_hours"]), 0.0)), current + self._max_ttl))
                if "use_count" in changes:
                    entry["use_count"] = max(int(changes["use_count"]), 0)
                entry["updated"] = _stamp(current)
                self._trim_locked()
                self._write_locked()
                return dict(entry)
            if prepared:
                self._write_locked()
        return None

    def list(self, scope: str, persona: str = _DEFAULT_PERSONA, *, include_disabled: bool = True, now: Any = None) -> list[dict[str, Any]]:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        with self._lock:
            changed = self._purge_locked(current)
            result = [dict(entry) for entry in self._entries(scope_name, persona_name) if include_disabled or entry.get("enabled", True)]
            if changed:
                self._write_locked()
            result.sort(key=lambda entry: (entry.get("created", ""), entry.get("id", "")))
            return result

    def remove(self, scope: str, entry_id: str, *, persona: str = _DEFAULT_PERSONA, now: Any = None) -> bool:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        with self._lock:
            self._purge_locked(current)
            entries = self._entries(scope_name, persona_name)
            kept = [entry for entry in entries if entry.get("id") != entry_id]
            if len(kept) == len(entries):
                return False
            if kept:
                self._data[scope_name][persona_name] = kept
            else:
                self._data.get(scope_name, {}).pop(persona_name, None)
                if not self._data.get(scope_name):
                    self._data.pop(scope_name, None)
            self._write_locked()
            return True

    def clear(self, scope: str, persona: str = _DEFAULT_PERSONA, *, now: Any = None) -> int:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        with self._lock:
            self._purge_locked(current)
            count = len(self._entries(scope_name, persona_name))
            self._data.get(scope_name, {}).pop(persona_name, None)
            if not self._data.get(scope_name):
                self._data.pop(scope_name, None)
            if count:
                self._write_locked()
            return count

    def build_prompt(self, scope: str, persona: str = _DEFAULT_PERSONA, current_user_payload: Any = "", *, now: Any = None) -> str:
        scope_name, persona_name = _scope(scope), _persona(persona)
        current = _now(now, self._clock)
        if isinstance(current_user_payload, (dict, list, tuple)):
            current_user_payload = json.dumps(current_user_payload, ensure_ascii=False, separators=(",", ":"))
        payload = _sanitize(current_user_payload, 1600)
        with self._lock:
            changed = self._purge_locked(current)
            entries = [dict(entry) for entry in self._entries(scope_name, persona_name) if entry.get("enabled", True)]
            for entry in self._entries(scope_name, persona_name):
                if entry.get("enabled", True):
                    entry["use_count"] = max(int(entry.get("use_count") or 0), 0) + 1
                    entry["updated"] = _stamp(current)
                    changed = True
            if changed:
                self._write_locked()
        if not payload and not entries:
            return ""
        lines = ["【自适应进化prompt】"]
        if payload:
            lines.append(f"当前用户动态：{payload}")
        if entries:
            lines.append("当前可用的动态提示：")
            for entry in entries:
                line = f"- {entry['content']}"
                if entry.get("reason"):
                    line += f"（原因：{entry['reason']}）"
                lines.append(line)
        lines.append("【/自适应进化prompt】")
        return "\n".join(lines)

    def list_prompts(self, scope: str, *, persona: str = _DEFAULT_PERSONA, include_disabled: bool = True, now: Any = None) -> list[dict[str, Any]]:
        return self.list(scope, persona=persona, include_disabled=include_disabled, now=now)

    def upsert_prompt(self, scope: str, name: str, content: str, *, persona: str = _DEFAULT_PERSONA, ttl_hours: float | None = None, enabled: bool = True, now: Any = None) -> dict[str, Any] | None:
        stable_name = _sanitize(name, 120)
        if not stable_name:
            return None
        for entry in self.list(scope, persona=persona, include_disabled=True, now=now):
            if entry.get("id") == stable_name or entry.get("reason") == stable_name:
                changes: dict[str, Any] = {
                    "content": content,
                    "reason": stable_name,
                    "enabled": enabled,
                }
                if ttl_hours is not None:
                    changes["ttl_hours"] = ttl_hours
                return self.update(scope, str(entry["id"]), persona=persona, now=now, **changes)
        return self.add(
            scope,
            content,
            persona=persona,
            reason=stable_name,
            ttl_hours=ttl_hours,
            enabled=enabled,
            now=now,
        )

    def remove_prompt(self, scope: str, entry_id: str, *, persona: str = _DEFAULT_PERSONA, now: Any = None) -> bool:
        return self.remove(scope, entry_id, persona=persona, now=now)

    def clear_prompts(self, scope: str, *, persona: str = _DEFAULT_PERSONA, now: Any = None) -> int:
        return self.clear(scope, persona=persona, now=now)

    add_entry = add
    update_entry = update
    list_entries = list
    remove_entry = remove
    clear_entries = clear


__all__ = ["AdaptiveEvolutionPromptStore"]
