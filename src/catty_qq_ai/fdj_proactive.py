"""Offline, at-most-once opportunity scheduling for the fadianji persona.

All public methods are synchronous; async callers must use ``asyncio.to_thread``.
Only scheduling metadata is stored. This module never generates or sends messages.
One process owns a root; multiple scheduler instances in that process are safe.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import stat
import tempfile
import threading
from typing import Any
import uuid


_TZ = timezone(timedelta(hours=8))
_LOCK = threading.RLock()
_PROCESS_TOKEN = uuid.uuid4().hex
_KNOWN_FILES: set[str] = set()
_BLOCKED_FILES: dict[str, str] = {}
_MAX_BYTES = 128 * 1024
_MAX_ATTEMPTS = 64
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_RESERVED = {"con", "prn", "aux", "nul", "clock$"} | {
    f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)
}
_OUTCOMES = {"sent", "failed", "unknown"}
_STATES = {"open", "expired", "skipped", "pending"} | _OUTCOMES


def _finite(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _timestamp(now: float) -> float:
    if not _finite(now):
        raise ValueError("now must be a finite nonnegative Unix timestamp")
    value = float(now)
    try:
        datetime.fromtimestamp(value, _TZ)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("now is outside the supported calendar") from exc
    return value


def _group_id(group_id: str) -> str:
    if (not isinstance(group_id, str) or not _SAFE_ID.fullmatch(group_id)
            or group_id.lower() in _RESERVED):
        raise ValueError("group_id must be a safe ASCII identifier, not a path")
    # Windows filenames are case insensitive; use the same identity everywhere.
    return group_id.lower()


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, _TZ).date().isoformat()


def _midnight(day: str) -> float:
    parsed = datetime.strptime(day, "%Y-%m-%d")
    if parsed.strftime("%Y-%m-%d") != day:
        raise ValueError("invalid day")
    return parsed.replace(tzinfo=_TZ).timestamp()


@dataclass(frozen=True)
class BubblePolicy:
    daily_min: int = 5
    daily_max: int = 10
    min_gap_seconds: float = 3600
    active_window_seconds: float = 900
    active_min_messages: int = 5
    warm_window_seconds: float = 21600
    night_chance: float = 0.2
    failure_backoff_seconds: float = 3600
    slot_grace_seconds: float = 1800

    def __post_init__(self) -> None:
        if (type(self.daily_min) is not int or type(self.daily_max) is not int
                or not 1 <= self.daily_min <= self.daily_max <= 64):
            raise ValueError("daily quota must satisfy 1 <= min <= max <= 64")
        if type(self.active_min_messages) is not int or self.active_min_messages < 1:
            raise ValueError("active_min_messages must be a positive integer")
        for name in ("min_gap_seconds", "active_window_seconds", "warm_window_seconds",
                     "failure_backoff_seconds", "slot_grace_seconds"):
            value = getattr(self, name)
            if not _finite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.warm_window_seconds < self.active_window_seconds:
            raise ValueError("warm_window_seconds must cover active_window_seconds")
        if not _finite(self.night_chance) or self.night_chance > 1:
            raise ValueError("night_chance must be between zero and one")


@dataclass(frozen=True)
class BubbleCandidate:
    group_id: str
    day: str
    slot_id: str
    mode: str
    night: bool


def _plan(group_id: str, day: str, policy: BubblePolicy) -> dict:
    seed = hashlib.sha256(f"fdj-bubble-v1\0{group_id}\0{day}".encode("ascii")).digest()
    rng = random.Random(int.from_bytes(seed, "big"))
    quota = rng.randint(policy.daily_min, policy.daily_max)
    # A one-slot custom policy still has a daytime slot.
    night = quota > 1 and rng.random() < policy.night_chance
    daytime = quota - int(night)
    topics = set(rng.sample(range(daytime), min(quota // 4, daytime)))
    start = _midnight(day)
    width = 14 * 3600 / daytime
    slots = []
    for index in range(quota):
        is_night = index == daytime
        if is_night:
            at = start + 22 * 3600 + rng.randint(900, 5400)
            end = start + 24 * 3600
        else:
            # Narrow central jitter keeps the default daytime starts >1h apart.
            at = start + 8 * 3600 + int((index + rng.uniform(0.4, 0.6)) * width)
            end = start + 22 * 3600
        slots.append({
            "slot_id": f"{day}:{index:02d}", "at": at,
            "expires_at": min(at + policy.slot_grace_seconds, end),
            "mode": "topic" if index in topics else "join", "night": is_night,
            "state": "open", "attempt_id": None,
        })
    return {"day": day, "quota": quota, "policy": asdict(policy), "slots": slots}


def _fresh(group_id: str, day: str, policy: BubblePolicy) -> dict:
    return {
        "version": 1, "group_id": group_id, "plan": _plan(group_id, day, policy),
        "last_attempt_at": None, "last_send_at": None, "backoff_until": None,
        "attempts": [],
    }


def _validate(state: Any, group_id: str) -> None:
    """Reject partial/corrupt state instead of inventing a fresh allowance."""
    if not isinstance(state, dict) or set(state) != {
        "version", "group_id", "plan", "last_attempt_at", "last_send_at",
        "backoff_until", "attempts",
    }:
        raise ValueError("invalid state shape")
    if type(state["version"]) is not int or state["version"] != 1 or state["group_id"] != group_id:
        raise ValueError("invalid state identity")
    for name in ("last_attempt_at", "last_send_at", "backoff_until"):
        if state[name] is not None and not _finite(state[name]):
            raise ValueError("invalid state clock")
    plan = state["plan"]
    if not isinstance(plan, dict) or set(plan) != {"day", "quota", "policy", "slots"}:
        raise ValueError("invalid plan")
    policy = BubblePolicy(**plan["policy"])
    expected = _plan(group_id, plan["day"], policy)
    if type(plan["quota"]) is not int or plan["quota"] != expected["quota"]:
        raise ValueError("invalid quota")
    if not isinstance(plan["slots"], list) or len(plan["slots"]) != plan["quota"]:
        raise ValueError("invalid slots")
    attempts = state["attempts"]
    if not isinstance(attempts, list) or len(attempts) > _MAX_ATTEMPTS:
        raise ValueError("invalid attempts")
    by_id = {}
    pending = 0
    for attempt in attempts:
        if not isinstance(attempt, dict) or set(attempt) != {
            "attempt_id", "day", "slot_id", "owner", "started_at", "finished_at", "outcome",
        }:
            raise ValueError("invalid attempt")
        aid = attempt["attempt_id"]
        if not isinstance(aid, str) or re.fullmatch(r"[0-9a-f]{32}", aid) is None or aid in by_id:
            raise ValueError("invalid attempt id")
        if (not isinstance(attempt["owner"], str)
                or re.fullmatch(r"[0-9a-f]{32}", attempt["owner"]) is None):
            raise ValueError("invalid owner")
        _midnight(attempt["day"])
        if (attempt["day"] > plan["day"] or not isinstance(attempt["slot_id"], str)
                or re.fullmatch(re.escape(attempt["day"]) + r":[0-9]{2}", attempt["slot_id"]) is None
                or not _finite(attempt["started_at"]) or _day(attempt["started_at"]) != attempt["day"]):
            raise ValueError("invalid attempt clock or slot")
        outcome = attempt["outcome"]
        if outcome not in _OUTCOMES | {"pending"}:
            raise ValueError("invalid outcome")
        if outcome == "pending":
            pending += 1
            if attempt["finished_at"] is not None:
                raise ValueError("pending attempt has finish clock")
        elif (not _finite(attempt["finished_at"])
              or attempt["finished_at"] < attempt["started_at"]):
            raise ValueError("invalid finish clock")
        if state["last_attempt_at"] is None or state["last_attempt_at"] < attempt["started_at"]:
            raise ValueError("lost attempt clock")
        if outcome == "sent" and (state["last_send_at"] is None
                                  or state["last_send_at"] < attempt["finished_at"]):
            raise ValueError("lost send clock")
        by_id[aid] = attempt
    if pending > 1:
        raise ValueError("multiple pending attempts")
    current_ids = set()
    for slot, original in zip(plan["slots"], expected["slots"]):
        if not isinstance(slot, dict) or set(slot) != set(original):
            raise ValueError("invalid slot shape")
        for name in ("slot_id", "at", "expires_at", "mode", "night"):
            if type(slot[name]) is not type(original[name]) or slot[name] != original[name]:
                raise ValueError("changed plan")
        if slot["state"] not in _STATES:
            raise ValueError("invalid slot state")
        if slot["state"] in {"open", "skipped", "expired"}:
            if slot["attempt_id"] is not None:
                raise ValueError("unexpected attempt")
        else:
            attempt = by_id.get(slot["attempt_id"])
            if (attempt is None or attempt["day"] != plan["day"]
                    or attempt["slot_id"] != slot["slot_id"] or attempt["outcome"] != slot["state"]
                    or not slot["at"] <= attempt["started_at"] < slot["expires_at"]):
                raise ValueError("lost or inconsistent attempt")
            current_ids.add(attempt["attempt_id"])
    if current_ids != {a["attempt_id"] for a in attempts if a["day"] == plan["day"]}:
        raise ValueError("orphaned attempt")


class BubbleScheduler:
    """Synchronous single-process scheduler; ``claim`` grants send permission once."""

    def __init__(self, root: str | Path, policy: BubblePolicy):
        if not isinstance(policy, BubblePolicy):
            raise TypeError("policy must be BubblePolicy")
        # No filesystem access in the constructor, safe to create on an event loop.
        self.root = Path(os.path.abspath(os.fspath(root)))
        self.policy = policy
        self._offers: dict[BubbleCandidate, tuple[float, float]] = {}

    def _path(self, group_id: str) -> Path:
        return self.root.resolve() / f"{group_id}.json"

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path))

    def _write(self, path: Path, state: dict) -> bool:
        temporary = None
        try:
            encoded = json.dumps(state, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _MAX_BYTES:
                raise ValueError("state exceeds size limit")
            path.parent.mkdir(parents=True, exist_ok=True)
            # Each write has a unique sibling tempfile, never a shared .tmp name.
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                             prefix=f".{path.stem}-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _KNOWN_FILES.add(self._key(path))
            return True
        except (OSError, ValueError, TypeError):
            _BLOCKED_FILES[self._key(path)] = "storage_error"
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load(self, group_id: str, now: float) -> tuple[Path | None, dict | None, str | None]:
        try:
            path = self._path(group_id)
        except (OSError, RuntimeError, ValueError):
            return None, None, "storage_error"
        key = self._key(path)
        if key in _BLOCKED_FILES:
            return path, None, _BLOCKED_FILES[key]
        changed = False
        try:
            try:
                info = path.lstat()
            except FileNotFoundError:
                if key in _KNOWN_FILES:
                    _BLOCKED_FILES[key] = "missing_state"
                    return path, None, "missing_state"
                state = _fresh(group_id, _day(now), self.policy)
                changed = True
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                    raise ValueError("unsafe state file")
                with path.open("rb") as stream:
                    raw = stream.read(_MAX_BYTES + 1)
                if len(raw) > _MAX_BYTES:
                    raise ValueError("oversized state file")
                state = json.loads(raw)
                _validate(state, group_id)
                _KNOWN_FILES.add(key)
        except OSError:
            _BLOCKED_FILES[key] = "storage_error"
            return path, None, "storage_error"
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            _BLOCKED_FILES[key] = "invalid_state"
            return path, None, "invalid_state"
        day = _day(now)
        if day < state["plan"]["day"]:
            return path, None, "clock_rollback"
        for attempt in state["attempts"]:
            if attempt["outcome"] == "pending" and attempt["owner"] != _PROCESS_TOKEN:
                if now < attempt["started_at"]:
                    return path, None, "clock_rollback"
                attempt["outcome"] = "unknown"
                attempt["finished_at"] = now
                old_policy = BubblePolicy(**state["plan"]["policy"])
                state["backoff_until"] = max(state["backoff_until"] or 0,
                                             now + old_policy.failure_backoff_seconds)
                for slot in state["plan"]["slots"]:
                    if slot["attempt_id"] == attempt["attempt_id"]:
                        slot["state"] = "unknown"
                changed = True
        if day > state["plan"]["day"]:
            # Cooldowns and outstanding attempts survive midnight unchanged.
            state["plan"] = _plan(group_id, day, self.policy)
            changed = True
        for slot in state["plan"]["slots"]:
            if slot["state"] == "open" and now >= slot["expires_at"]:
                slot["state"] = "expired"
                changed = True
        if changed and not self._write(path, state):
            return path, None, "storage_error"
        return path, state, None

    @staticmethod
    def _ready(state: dict, now: float) -> bool:
        policy = BubblePolicy(**state["plan"]["policy"])
        if any(a["outcome"] == "pending" for a in state["attempts"]):
            return False
        if now < (state["backoff_until"] or 0):
            return False
        for name in ("last_attempt_at", "last_send_at"):
            if state[name] is not None and now - state[name] < policy.min_gap_seconds:
                return False
        return 8 <= datetime.fromtimestamp(now, _TZ).hour < 24

    def due(self, group_id: str, now: float, *, human_times: Sequence[float]) -> BubbleCandidate | None:
        group_id, now = _group_id(group_id), _timestamp(now)
        with _LOCK:
            # Keep issued evidence for skip even after its SEND permission expires;
            # another group's tick must not invalidate an in-flight generation.
            day = _day(now)
            self._offers = {c: proof for c, proof in self._offers.items() if c.day == day}
            _, state, _ = self._load(group_id, now)
            if state is None or not self._ready(state, now):
                return None
            policy = BubblePolicy(**state["plan"]["policy"])
            warm = sorted((float(t) for t in human_times if _finite(t)
                           and now - policy.warm_window_seconds <= t <= now), reverse=True)
            active = [t for t in warm if t >= now - policy.active_window_seconds]
            for slot in state["plan"]["slots"]:
                if slot["state"] != "open" or not slot["at"] <= now < slot["expires_at"]:
                    continue
                if len(active) >= policy.active_min_messages:
                    mode = "join"
                    eligible_until = active[policy.active_min_messages - 1] + policy.active_window_seconds
                elif slot["mode"] == "topic" and not slot["night"] and warm:
                    mode = "topic"
                    eligible_until = warm[0] + policy.warm_window_seconds
                else:
                    continue
                candidate = BubbleCandidate(group_id, state["plan"]["day"], slot["slot_id"], mode, slot["night"])
                self._offers[candidate] = (now, min(eligible_until, slot["expires_at"]))
                return candidate
            return None

    def _slot(self, candidate: BubbleCandidate, state: dict) -> dict | None:
        if candidate.day != state["plan"]["day"] or type(candidate.night) is not bool:
            return None
        for slot in state["plan"]["slots"]:
            if (slot["slot_id"] == candidate.slot_id and slot["night"] == candidate.night
                    and candidate.mode in ({"join", "topic"} if slot["mode"] == "topic" else {"join"})):
                return slot
        return None

    def _offered(self, candidate: BubbleCandidate, slot: dict, now: float) -> bool:
        proof = self._offers.get(candidate)
        return (proof is not None and proof[0] <= now <= proof[1]
                and slot["state"] == "open" and slot["at"] <= now < slot["expires_at"])

    def claim(self, candidate: BubbleCandidate, now: float) -> str | None:
        if not isinstance(candidate, BubbleCandidate):
            return None
        group_id, now = _group_id(candidate.group_id), _timestamp(now)
        with _LOCK:
            path, state, _ = self._load(group_id, now)
            if state is None or path is None or not self._ready(state, now):
                return None
            slot = self._slot(candidate, state)
            if slot is None or not self._offered(candidate, slot, now):
                return None
            attempt_id = uuid.uuid4().hex
            state["attempts"].append({
                "attempt_id": attempt_id, "day": candidate.day, "slot_id": candidate.slot_id,
                "owner": _PROCESS_TOKEN, "started_at": now, "finished_at": None, "outcome": "pending",
            })
            state["attempts"] = state["attempts"][-_MAX_ATTEMPTS:]
            state["last_attempt_at"] = now
            slot["state"], slot["attempt_id"] = "pending", attempt_id
            if not self._write(path, state):
                return None
            self._offers.pop(candidate, None)
            return attempt_id

    def finish(self, group_id: str, attempt_id: str, outcome: str, now: float) -> bool:
        group_id, now = _group_id(group_id), _timestamp(now)
        if outcome not in _OUTCOMES or not isinstance(attempt_id, str):
            return False
        with _LOCK:
            path, state, _ = self._load(group_id, now)
            if state is None or path is None:
                return False
            attempt = next((a for a in state["attempts"] if a["attempt_id"] == attempt_id), None)
            if attempt is None or now < attempt["started_at"]:
                return False
            if attempt["outcome"] != "pending":
                return attempt["outcome"] == outcome
            attempt["outcome"], attempt["finished_at"] = outcome, now
            for slot in state["plan"]["slots"]:
                if slot["attempt_id"] == attempt_id:
                    slot["state"] = outcome
            if outcome == "sent":
                state["last_send_at"] = max(state["last_send_at"] or 0, now)
            else:
                policy = BubblePolicy(**state["plan"]["policy"])
                state["backoff_until"] = max(state["backoff_until"] or 0,
                                             now + policy.failure_backoff_seconds)
            return self._write(path, state)

    def skip(self, candidate: BubbleCandidate, now: float, reason: str = "unsuitable") -> bool:
        # Deliberately do not persist caller-controlled reason text or user data.
        del reason
        if not isinstance(candidate, BubbleCandidate):
            return False
        group_id, now = _group_id(candidate.group_id), _timestamp(now)
        with _LOCK:
            path, state, _ = self._load(group_id, now)
            if state is None or path is None:
                return False
            slot = self._slot(candidate, state)
            if slot is None:
                return False
            if slot["state"] == "skipped":
                return True
            proof = self._offers.get(candidate)
            # Generation can outlive the human-activity window. Consuming a known
            # opportunity is still safe; only permission to SEND needs fresh proof.
            if slot["state"] != "open" or proof is None or now < proof[0]:
                return False
            slot["state"] = "skipped"
            if not self._write(path, state):
                return False
            self._offers.pop(candidate, None)
            return True

    def status(self, group_id: str, now: float) -> dict:
        group_id, now = _group_id(group_id), _timestamp(now)
        with _LOCK:
            _, state, error = self._load(group_id, now)
            if state is None:
                return {"ok": False, "error": error, "day": _day(now), "quota": 0,
                        "sent": 0, "unknown": 0, "attempted": 0, "slots": []}
            slots = state["plan"]["slots"]
            return {
                "ok": True, "error": None, "day": state["plan"]["day"], "quota": state["plan"]["quota"],
                "sent": sum(s["state"] == "sent" for s in slots),
                "unknown": sum(s["state"] == "unknown" for s in slots),
                "attempted": sum(s["attempt_id"] is not None for s in slots),
                "failed": sum(s["state"] == "failed" for s in slots),
                "skipped": sum(s["state"] == "skipped" for s in slots),
                "expired": sum(s["state"] == "expired" for s in slots),
                "pending": next((a["attempt_id"] for a in state["attempts"] if a["outcome"] == "pending"), None),
                "last_attempt_at": state["last_attempt_at"], "last_send_at": state["last_send_at"],
                "backoff_until": state["backoff_until"],
                "slots": [dict(slot) for slot in slots],
            }