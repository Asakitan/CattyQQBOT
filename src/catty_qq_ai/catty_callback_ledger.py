"""Structured callback ledger with atomic JSON persistence and AI markers."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

CALLBACK_MARKER_PREFIX = "<<<CATTY_CB:"
CALLBACK_MARKER_SUFFIX = ">>>"
_ALLOWED_TARGET_TYPES = {"plan", "promise", "question", "meme", "event"}
_ACTIVE_STATUSES = {"open", "acknowledged", "in_progress"}
_CLOSED_STATUSES = {"completed", "expired", "dismissed"}
_CALLBACK_MARKER_RE = re.compile(r"<{2,4}CATTY_CB:([^<>\n]*?)(?:>{2,4}|(?=\n)|\Z)", re.MULTILINE)


def extract_callback_markers(reply: str) -> tuple[str, list[str]]:
    if not reply:
        return "", []
    payloads: list[str] = []
    def _sub(match: re.Match[str]) -> str:
        payloads.append((match.group(1) or "").strip())
        return ""
    return _CALLBACK_MARKER_RE.sub(_sub, reply).strip(), payloads


class CallbackLedger:
    def __init__(self, memory_path: str | Path, cooldown_minutes: int = 120):
        mem_path = Path(memory_path).expanduser()
        if not mem_path.is_absolute():
            mem_path = mem_path.resolve()
        self._path = mem_path.parent / "callback_ledger.json"
        self._lock = threading.RLock()
        self._cooldown_minutes = max(int(cooldown_minutes), 0)
        self._scopes: dict[str, dict[str, dict[str, Any]]] = {}
        self._next_ids: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(raw, dict):
            return
        scopes = raw.get("scopes", raw)
        if not isinstance(scopes, dict):
            return
        saved_next = raw.get("next_ids", {})
        if isinstance(saved_next, dict):
            for scope, value in saved_next.items():
                try:
                    self._next_ids[str(scope)] = max(int(value), 1)
                except (TypeError, ValueError):
                    pass
        for scope_key, raw_scope in scopes.items():
            scope = str(scope_key)
            if scope == "next_ids":
                continue
            entries_raw = raw_scope.get("entries") if isinstance(raw_scope, dict) and "entries" in raw_scope else raw_scope
            if not isinstance(entries_raw, dict):
                continue
            entries: dict[str, dict[str, Any]] = {}
            for entry_id, raw_entry in entries_raw.items():
                if not isinstance(raw_entry, dict):
                    continue
                entry = self._normalize_entry(scope, str(entry_id), raw_entry)
                if entry is not None:
                    entries[entry["id"]] = entry
            if entries:
                self._scopes[scope] = entries
                highest = max((self._id_number(entry_id) for entry_id in entries), default=0)
                self._next_ids[scope] = max(self._next_ids.get(scope, 1), highest + 1)

    @staticmethod
    def _id_number(entry_id: str) -> int:
        match = re.fullmatch(r"cb(\d+)", entry_id)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _clip(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    def _normalize_entry(self, scope: str, entry_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
        target_type = str(raw.get("target_type", "")).strip().lower()
        status = str(raw.get("status", "open")).strip().lower()
        if not re.fullmatch(r"cb\d+", entry_id) or target_type not in _ALLOWED_TARGET_TYPES or status not in (_ACTIVE_STATUSES | _CLOSED_STATUSES):
            return None
        try:
            created_at = float(raw.get("created_at", 0.0))
            last_seen_at = float(raw.get("last_seen_at", created_at))
            last_callback_at = float(raw.get("last_callback_at", 0.0))
            cooldown_until = float(raw.get("cooldown_until", 0.0))
        except (TypeError, ValueError):
            return None
        try:
            callback_count = max(int(raw.get("callback_count", 0)), 0)
        except (TypeError, ValueError):
            callback_count = 0
        return {
            "id": entry_id, "scope": scope, "user_id": self._clip(raw.get("user_id", ""), 120),
            "target_type": target_type, "source_text": self._clip(raw.get("source_text", ""), 120),
            "summary": self._clip(raw.get("summary", ""), 80), "status": status,
            "created_at": created_at, "last_seen_at": last_seen_at, "callback_count": callback_count,
            "last_callback_at": last_callback_at, "cooldown_until": cooldown_until,
            "completion_evidence": self._clip(raw.get("completion_evidence", ""), 120),
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".callback_ledger.", suffix=".tmp", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "next_ids": self._next_ids, "scopes": {scope: {"next_id": self._next_ids.get(scope, 1), "entries": entries} for scope, entries in self._scopes.items()}}, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            temp_path = None
        except (OSError, TypeError, ValueError):
            pass
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    def record_candidate(self, scope: str, *, target_type: str, source_text: str, summary: str, user_id: str = "", now: float | None = None) -> str | None:
        scope = str(scope).strip()
        target_type = str(target_type or "").strip().lower()
        source_text = self._clip(source_text, 120)
        summary = self._clip(summary, 80)
        if not scope or target_type not in _ALLOWED_TARGET_TYPES or not summary:
            return None
        n = self._now(now)
        with self._lock:
            self.purge(scope, now=n)
            entries = self._scopes.setdefault(scope, {})
            for entry in entries.values():
                if entry["summary"] == summary and entry["status"] in _ACTIVE_STATUSES:
                    entry["last_seen_at"] = n
                    self._save()
                    return entry["id"]
            next_id = max(self._next_ids.get(scope, 1), 1)
            entry_id = f"cb{next_id}"
            self._next_ids[scope] = next_id + 1
            entries[entry_id] = {"id": entry_id, "scope": scope, "user_id": self._clip(user_id, 120), "target_type": target_type, "source_text": source_text, "summary": summary, "status": "open", "created_at": n, "last_seen_at": n, "callback_count": 0, "last_callback_at": 0.0, "cooldown_until": 0.0, "completion_evidence": ""}
            self._save()
            return entry_id

    def apply_marker(self, scope: str, payload: str, *, now: float | None = None) -> dict[str, Any]:
        scope = str(scope).strip()
        parts = str(payload or "").strip().split(":", 2)
        action = parts[0].strip().lower() if parts else ""
        n = self._now(now)
        with self._lock:
            if action == "done":
                if len(parts) < 2 or not parts[1].strip():
                    return {"ok": False, "error": "done marker requires an id"}
                entry_id = parts[1].strip()
                entry = self._scopes.get(scope, {}).get(entry_id)
                if entry is None:
                    return {"ok": False, "error": f"unknown callback id: {entry_id}"}
                entry["status"] = "completed"
                entry["last_seen_at"] = n
                entry["completion_evidence"] = self._clip(parts[2] if len(parts) > 2 else "", 120)
                self._save()
                return {"ok": True, "action": "done", "id": entry_id, "entry": dict(entry)}
            if action == "dismiss":
                if len(parts) != 2 or not parts[1].strip():
                    return {"ok": False, "error": "dismiss marker requires an id"}
                entry_id = parts[1].strip()
                entry = self._scopes.get(scope, {}).get(entry_id)
                if entry is None:
                    return {"ok": False, "error": f"unknown callback id: {entry_id}"}
                entry["status"] = "dismissed"
                entry["last_seen_at"] = n
                self._save()
                return {"ok": True, "action": "dismiss", "id": entry_id, "entry": dict(entry)}
            if action == "open":
                if len(parts) != 3 or parts[1].strip().lower() not in _ALLOWED_TARGET_TYPES or not parts[2].strip():
                    return {"ok": False, "error": "invalid open marker target_type or summary"}
                summary = parts[2].strip()
                entry_id = self.record_candidate(scope, target_type=parts[1].strip().lower(), source_text=summary, summary=summary, now=n)
                if entry_id is None:
                    return {"ok": False, "error": "open marker could not create an entry"}
                return {"ok": True, "action": "open", "id": entry_id, "entry": dict(self._scopes[scope][entry_id])}
            return {"ok": False, "error": "unsupported or malformed callback marker"}

    def mark_callback_used(self, scope: str, entry_id: str, *, now: float | None = None) -> None:
        n = self._now(now)
        with self._lock:
            entry = self._scopes.get(str(scope).strip(), {}).get(str(entry_id).strip())
            if entry is None:
                return
            entry["callback_count"] += 1
            entry["last_callback_at"] = n
            entry["last_seen_at"] = n
            if entry["status"] == "open":
                entry["status"] = "acknowledged"
            entry["cooldown_until"] = n + self._cooldown_minutes * 60.0
            self._save()

    def active_entries(self, scope: str, *, now: float | None = None, max_items: int = 3) -> list[dict[str, Any]]:
        n = self._now(now)
        with self._lock:
            entries = [dict(entry) for entry in self._scopes.get(str(scope).strip(), {}).values() if entry["status"] in _ACTIVE_STATUSES and entry["cooldown_until"] <= n]
            entries.sort(key=lambda entry: entry["last_seen_at"], reverse=True)
            return entries[:max(int(max_items), 0)]

    def purge(self, scope: str, *, max_age_days: int = 14, max_entries: int = 50, now: float | None = None) -> None:
        n = self._now(now)
        scope = str(scope).strip()
        with self._lock:
            entries = self._scopes.get(scope)
            if not entries:
                return
            age_seconds = max(float(max_age_days), 0.0) * 86400.0
            for entry_id, entry in list(entries.items()):
                if entry["status"] in _CLOSED_STATUSES and n - entry["last_seen_at"] > age_seconds:
                    del entries[entry_id]
            limit = max(int(max_entries), 0)
            if len(entries) > limit:
                removable = sorted((entry for entry in entries.values() if entry["status"] not in _ACTIVE_STATUSES), key=lambda entry: entry["last_seen_at"])
                for entry in removable[:max(len(entries) - limit, 0)]:
                    entries.pop(entry["id"], None)
            if not entries:
                self._scopes.pop(scope, None)
            self._save()

    @staticmethod
    def _relative_time(timestamp: float, now: float) -> str:
        seconds = max(now - timestamp, 0.0)
        if seconds < 60:
            return "刚刚"
        minutes = int(seconds // 60)
        if minutes < 60:
            return f"{minutes}分钟前"
        hours = int(seconds // 3600)
        if hours < 24:
            return f"{hours}小时前"
        return f"{int(seconds // 86400)}天前"

    def build_prompt_block(self, scope: str, *, now: float | None = None) -> str:
        n = self._now(now)
        entries = self.active_entries(scope, now=n)
        if not entries:
            return ""
        lines = ["【待回调事项】以下是本会话尚未了结的事项, 若当前话题自然相关可以短回调一句, 不要生硬提起;", "回调过或了结后用 marker 汇报: <<<CATTY_CB:done:ID>>> / <<<CATTY_CB:open:plan:简述>>> / <<<CATTY_CB:dismiss:ID>>>"]
        lines.extend(f"- {entry['id']} [{entry['target_type']}] {entry['summary']} ({self._relative_time(entry['last_seen_at'], n)})" for entry in entries)
        return "\n".join(lines)
