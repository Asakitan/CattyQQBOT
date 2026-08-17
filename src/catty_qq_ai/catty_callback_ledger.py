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
_SCHEMA_VERSION = 2
_STATUSES = {
    "open",
    "selected",
    "in_progress",
    "used",
    "awaiting_confirmation",
    "completed",
    "dismissed",
    "snoozed",
    "expired",
}
_PROMPT_STATUSES = {"open", "selected", "in_progress", "used", "awaiting_confirmation", "snoozed"}
_CLOSED_STATUSES = {"completed", "expired", "dismissed"}
_STATUS_ALIASES = {"acknowledged": "used"}
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
            entries_raw = (
                raw_scope.get("entries")
                if isinstance(raw_scope, dict) and "entries" in raw_scope
                else raw_scope
            )
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
        raw_status = str(raw.get("status", "open")).strip().lower()
        status = _STATUS_ALIASES.get(raw_status, raw_status)
        if not re.fullmatch(r"cb\d+", entry_id) or target_type not in _ALLOWED_TARGET_TYPES or status not in _STATUSES:
            return None
        try:
            created_at = float(raw.get("created_at", 0.0))
            last_seen_at = float(raw.get("last_seen_at", created_at))
            last_callback_at = float(raw.get("last_callback_at", 0.0))
            cooldown_until = float(raw.get("cooldown_until", 0.0))
            next_due_at = float(raw.get("next_due_at", 0.0))
        except (TypeError, ValueError):
            return None
        try:
            callback_count = max(int(raw.get("callback_count", 0)), 0)
        except (TypeError, ValueError):
            callback_count = 0
        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "id": entry_id,
            "scope": scope,
            "user_id": self._clip(raw.get("user_id", ""), 120),
            "target_type": target_type,
            "source_text": self._clip(raw.get("source_text", ""), 120),
            "summary": self._clip(raw.get("summary", ""), 80),
            "status": status,
            "created_at": created_at,
            "last_seen_at": last_seen_at,
            "callback_count": callback_count,
            "last_callback_at": last_callback_at,
            "cooldown_until": cooldown_until,
            "source_turn_id": self._clip(raw.get("source_turn_id", ""), 120),
            "selected_turn_id": self._clip(raw.get("selected_turn_id", ""), 120),
            "used_turn_id": self._clip(raw.get("used_turn_id", ""), 120),
            "priority": priority,
            "next_due_at": next_due_at,
            "completion_evidence": self._clip(raw.get("completion_evidence", ""), 120),
            "confidence": max(0.0, min(confidence, 1.0)),
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".callback_ledger.", suffix=".tmp", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": _SCHEMA_VERSION,
                    "next_ids": self._next_ids,
                    "scopes": {
                        scope: {"next_id": self._next_ids.get(scope, 1), "entries": entries}
                        for scope, entries in self._scopes.items()
                    },
                }, handle, ensure_ascii=False, indent=2)
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

    @staticmethod
    def _normalized_turn_id(turn_id: str | None) -> str:
        return str(turn_id or "").strip()

    def _validate_marker_action(
        self,
        entry: dict[str, Any],
        action: str,
        turn_id: str | None,
    ) -> str | None:
        normalized_turn_id = self._normalized_turn_id(turn_id)
        if not normalized_turn_id:
            return f"{action} marker requires a non-empty turn_id"
        status = entry["status"]
        if status in _CLOSED_STATUSES:
            return f"callback {entry['id']} is closed with status {status}"
        if entry.get("selected_turn_id", "") != normalized_turn_id:
            selected_turn_id = entry.get("selected_turn_id", "") or "<none>"
            return (
                f"{action} marker turn mismatch for {entry['id']}: "
                f"selected_turn_id={selected_turn_id}, turn_id={normalized_turn_id}"
            )
        if status not in {"selected", "used"}:
            return f"callback {entry['id']} cannot apply {action} from status {status}"
        if status == "used" and entry.get("used_turn_id", "") != normalized_turn_id:
            used_turn_id = entry.get("used_turn_id", "") or "<none>"
            return (
                f"{action} marker used-turn mismatch for {entry['id']}: "
                f"used_turn_id={used_turn_id}, turn_id={normalized_turn_id}"
            )
        return None

    def record_candidate(
        self,
        scope: str,
        *,
        target_type: str,
        source_text: str,
        summary: str,
        user_id: str = "",
        source_turn_id: str = "",
        priority: int = 0,
        next_due_at: float | None = None,
        confidence: float = 0.0,
        now: float | None = None,
    ) -> str | None:
        scope = str(scope).strip()
        target_type = str(target_type or "").strip().lower()
        source_text = self._clip(source_text, 120)
        summary = self._clip(summary, 80)
        if not scope or target_type not in _ALLOWED_TARGET_TYPES or not summary:
            return None
        n = self._now(now)
        try:
            normalized_priority = int(priority)
        except (TypeError, ValueError):
            normalized_priority = 0
        try:
            normalized_confidence = max(0.0, min(float(confidence), 1.0))
        except (TypeError, ValueError):
            normalized_confidence = 0.0
        with self._lock:
            self.purge(scope, now=n)
            entries = self._scopes.setdefault(scope, {})
            for entry in entries.values():
                if entry["summary"] == summary and entry["status"] in _PROMPT_STATUSES:
                    entry["last_seen_at"] = n
                    if source_turn_id:
                        entry["source_turn_id"] = self._clip(source_turn_id, 120)
                    entry["priority"] = max(entry.get("priority", 0), normalized_priority)
                    entry["confidence"] = max(entry.get("confidence", 0.0), normalized_confidence)
                    self._save()
                    return entry["id"]
            next_id = max(self._next_ids.get(scope, 1), 1)
            entry_id = f"cb{next_id}"
            self._next_ids[scope] = next_id + 1
            entries[entry_id] = {
                "id": entry_id,
                "scope": scope,
                "user_id": self._clip(user_id, 120),
                "target_type": target_type,
                "source_text": source_text,
                "summary": summary,
                "status": "open",
                "created_at": n,
                "last_seen_at": n,
                "callback_count": 0,
                "last_callback_at": 0.0,
                "cooldown_until": 0.0,
                "source_turn_id": self._clip(source_turn_id, 120),
                "selected_turn_id": "",
                "used_turn_id": "",
                "priority": normalized_priority,
                "next_due_at": float(next_due_at or 0.0),
                "completion_evidence": "",
                "confidence": normalized_confidence,
            }
            self._save()
            return entry_id

    def apply_marker(
        self,
        scope: str,
        payload: str,
        *,
        turn_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
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
                error = self._validate_marker_action(entry, "done", turn_id)
                if error:
                    return {"ok": False, "error": error}
                entry["status"] = "completed"
                entry["last_seen_at"] = n
                entry["completion_evidence"] = self._clip(parts[2] if len(parts) > 2 else "", 120)
                entry["next_due_at"] = 0.0
                self._save()
                return {"ok": True, "action": "done", "id": entry_id, "entry": dict(entry)}
            if action == "dismiss":
                if len(parts) != 2 or not parts[1].strip():
                    return {"ok": False, "error": "dismiss marker requires an id"}
                entry_id = parts[1].strip()
                entry = self._scopes.get(scope, {}).get(entry_id)
                if entry is None:
                    return {"ok": False, "error": f"unknown callback id: {entry_id}"}
                error = self._validate_marker_action(entry, "dismiss", turn_id)
                if error:
                    return {"ok": False, "error": error}
                entry["status"] = "dismissed"
                entry["last_seen_at"] = n
                self._save()
                return {"ok": True, "action": "dismiss", "id": entry_id, "entry": dict(entry)}
            if action == "used":
                if len(parts) < 2 or not parts[1].strip():
                    return {"ok": False, "error": "used marker requires an id"}
                entry_id = parts[1].strip()
                evidence = parts[2].strip() if len(parts) > 2 else ""
                entry = self._scopes.get(scope, {}).get(entry_id)
                if entry is None:
                    return {"ok": False, "error": f"unknown callback id: {entry_id}"}
                error = self._validate_marker_action(entry, "used", turn_id)
                if error:
                    return {"ok": False, "error": error}
                updated = self.mark_callback_used(scope, entry_id, turn_id=turn_id, evidence=evidence, now=n)
                if updated is None:
                    return {"ok": False, "error": f"used marker could not update callback: {entry_id}"}
                return {"ok": True, "action": "used", "id": entry_id, "entry": updated}
            if action == "snooze":
                if len(parts) < 2 or not parts[1].strip():
                    return {"ok": False, "error": "snooze marker requires an id"}
                entry_id = parts[1].strip()
                entry = self._scopes.get(scope, {}).get(entry_id)
                if entry is None:
                    return {"ok": False, "error": f"unknown callback id: {entry_id}"}
                error = self._validate_marker_action(entry, "snooze", turn_id)
                if error:
                    return {"ok": False, "error": error}
                try:
                    minutes = float(parts[2]) if len(parts) > 2 and parts[2].strip() else float(self._cooldown_minutes or 60)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "snooze minutes must be numeric"}
                entry["status"] = "snoozed"
                entry["last_seen_at"] = n
                entry["next_due_at"] = n + max(minutes, 0.0) * 60.0
                self._save()
                return {"ok": True, "action": "snooze", "id": entry_id, "entry": dict(entry)}
            if action == "open":
                if len(parts) != 3 or parts[1].strip().lower() not in _ALLOWED_TARGET_TYPES or not parts[2].strip():
                    return {"ok": False, "error": "invalid open marker target_type or summary"}
                normalized_turn_id = self._normalized_turn_id(turn_id)
                if not normalized_turn_id:
                    return {"ok": False, "error": "open marker requires a non-empty turn_id"}
                summary = parts[2].strip()
                entry_id = self.record_candidate(
                    scope,
                    target_type=parts[1].strip().lower(),
                    source_text=summary,
                    summary=summary,
                    source_turn_id=normalized_turn_id,
                    now=n,
                )
                if entry_id is None:
                    return {"ok": False, "error": "open marker could not create an entry"}
                return {"ok": True, "action": "open", "id": entry_id, "entry": dict(self._scopes[scope][entry_id])}
            return {"ok": False, "error": "unsupported or malformed callback marker"}

    def mark_callback_used(
        self,
        scope: str,
        entry_id: str,
        *,
        turn_id: str | None = None,
        evidence: str = "",
        now: float | None = None,
    ) -> dict[str, Any] | None:
        n = self._now(now)
        with self._lock:
            entry = self._scopes.get(str(scope).strip(), {}).get(str(entry_id).strip())
            if entry is None:
                return None
            error = self._validate_marker_action(entry, "used", turn_id)
            if error:
                return None
            normalized_turn_id = self._normalized_turn_id(turn_id)
            if entry["status"] == "used":
                return dict(entry)
            entry["callback_count"] += 1
            entry["last_callback_at"] = n
            entry["last_seen_at"] = n
            entry["status"] = "used"
            entry["used_turn_id"] = self._clip(normalized_turn_id, 120)
            if evidence:
                entry["completion_evidence"] = self._clip(evidence, 120)
            entry["cooldown_until"] = n + self._cooldown_minutes * 60.0
            self._save()
            return dict(entry)

    def active_entries(self, scope: str, *, now: float | None = None, max_items: int = 3) -> list[dict[str, Any]]:
        n = self._now(now)
        with self._lock:
            entries = [
                dict(entry)
                for entry in self._scopes.get(str(scope).strip(), {}).values()
                if entry["status"] in _PROMPT_STATUSES
                and entry["cooldown_until"] <= n
                and entry.get("next_due_at", 0.0) <= n
            ]
            entries.sort(key=lambda entry: (entry.get("next_due_at", 0.0), -entry.get("priority", 0), -entry["last_seen_at"], entry["id"]))
            return entries[: max(int(max_items), 0)]

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
                removable = sorted(
                    (entry for entry in entries.values() if entry["status"] not in _PROMPT_STATUSES),
                    key=lambda entry: entry["last_seen_at"],
                )
                for entry in removable[: max(len(entries) - limit, 0)]:
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

    def build_prompt_block(self, scope: str, *, turn_id: str | None = None, now: float | None = None) -> str:
        n = self._now(now)
        entries = self.active_entries(scope, now=n, max_items=2)
        if not entries:
            return ""
        normalized_turn_id = self._normalized_turn_id(turn_id)
        if normalized_turn_id:
            with self._lock:
                normalized_scope = str(scope).strip()
                changed = False
                for entry in entries:
                    stored = self._scopes.get(normalized_scope, {}).get(entry["id"])
                    if stored is None or stored["status"] in _CLOSED_STATUSES:
                        continue
                    if stored["status"] in _PROMPT_STATUSES and (
                        stored["status"] != "selected"
                        or stored.get("selected_turn_id", "") != normalized_turn_id
                        or bool(stored.get("used_turn_id", ""))
                    ):
                        stored["selected_turn_id"] = normalized_turn_id
                        stored["status"] = "selected"
                        stored["used_turn_id"] = ""
                        stored["next_due_at"] = 0.0
                        stored["cooldown_until"] = 0.0
                        changed = True
                if changed:
                    self._save()
                entries = [dict(self._scopes[normalized_scope][entry["id"]]) for entry in entries]
        lines = [
            "【待回调事项】以下是本会话尚未了结的事项, 当前话题自然相关时再回调, 不要生硬提起;",
            "实际完成回调后输出 used marker；只有真正完成才输出 done，提到事项不等于完成。",
            "markers: <<<CATTY_CB:used:ID[:evidence]>>> / <<<CATTY_CB:done:ID[:evidence]>>> / <<<CATTY_CB:snooze:ID[:minutes]>>> / <<<CATTY_CB:open:plan:简述>>> / <<<CATTY_CB:dismiss:ID>>>",
        ]
        lines.extend(
            f"- {entry['id']} [{entry['target_type']}] {entry['summary']} ({self._relative_time(entry['last_seen_at'], n)})"
            for entry in entries
        )
        return "\n".join(lines)

