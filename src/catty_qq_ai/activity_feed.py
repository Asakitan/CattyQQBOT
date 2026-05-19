"""Activity tracking + conversation feed for Catty.

Two artifacts written from the bot process:

1. ``training/catty_activity.json`` — a tiny state file with the timestamps of
   the last user message and the last bot reply. ``auto_train_reply_gate.py``
   reads this to enforce the "no chat activity for 30 min" gate and to detect
   mid-training activity so it can kill the train job.

2. ``logs/conversation_feed.jsonl`` — one JSON object per chat event (user
   message or bot reply), append-only, capped at ``max_bytes``. The training
   dashboard's Conversation tab polls the tail.

Both writers swallow filesystem errors so a bad disk never breaks bot
replies.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


_ACTIVITY_LOCK = threading.Lock()
_FEED_LOCK = threading.Lock()

# Default paths (relative to project root). The bot may override via
# `configure(base_dir=...)` early in startup.
_BASE_DIR: Path = Path(".")
_ACTIVITY_PATH_REL: str = "training/catty_activity.json"
_FEED_PATH_REL: str = "logs/conversation_feed.jsonl"
_FEED_MAX_BYTES: int = 8 * 1024 * 1024  # 8 MB rolling cap


def configure(
    *,
    base_dir: str | os.PathLike[str] | None = None,
    activity_path: str | None = None,
    feed_path: str | None = None,
    feed_max_bytes: int | None = None,
) -> None:
    global _BASE_DIR, _ACTIVITY_PATH_REL, _FEED_PATH_REL, _FEED_MAX_BYTES
    if base_dir is not None:
        _BASE_DIR = Path(base_dir)
    if activity_path is not None:
        _ACTIVITY_PATH_REL = activity_path
    if feed_path is not None:
        _FEED_PATH_REL = feed_path
    if feed_max_bytes is not None and feed_max_bytes > 0:
        _FEED_MAX_BYTES = int(feed_max_bytes)


def _activity_path() -> Path:
    return _BASE_DIR / _ACTIVITY_PATH_REL


def _feed_path() -> Path:
    return _BASE_DIR / _FEED_PATH_REL


def _read_activity_state() -> dict[str, Any]:
    path = _activity_path()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        return {}


def _write_activity_state(state: dict[str, Any]) -> None:
    path = _activity_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def _append_feed_line(entry: dict[str, Any]) -> None:
    path = _feed_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _FEED_LOCK:
            # If file is over the rolling cap, drop oldest half before appending.
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size > _FEED_MAX_BYTES:
                _truncate_feed_to_half()
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False))
                f.write("\n")
    except Exception:  # noqa: BLE001
        pass


def _truncate_feed_to_half() -> None:
    path = _feed_path()
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            total = f.tell()
            f.seek(max(total // 2, 0))
            # advance to next newline so we don't break a JSON line
            f.readline()
            tail = f.read()
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as f:
            f.write(tail)
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def record_user_message(
    *,
    scope: str,
    sender_name: str,
    sender_id: str,
    text: str,
    image_count: int = 0,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record an incoming user message: refresh activity ts + append to feed."""
    now = time.time()
    with _ACTIVITY_LOCK:
        state = _read_activity_state()
        state["last_user_message_at"] = now
        state["last_event_at"] = now
        _write_activity_state(state)
    entry: dict[str, Any] = {
        "ts": now,
        "kind": "user",
        "scope": scope,
        "sender_name": sender_name,
        "sender_id": sender_id,
        "text": (text or "")[:2000],
        "image_count": int(image_count),
    }
    if extra:
        entry["extra"] = extra
    _append_feed_line(entry)


def record_assistant_reply(
    *,
    scope: str,
    text: str,
    image_count: int = 0,
    triggered_by: str | None = None,
) -> None:
    """Record an outgoing bot reply: refresh activity ts + append to feed."""
    now = time.time()
    with _ACTIVITY_LOCK:
        state = _read_activity_state()
        state["last_reply_at"] = now
        state["last_event_at"] = now
        _write_activity_state(state)
    entry: dict[str, Any] = {
        "ts": now,
        "kind": "assistant",
        "scope": scope,
        "text": (text or "")[:2000],
        "image_count": int(image_count),
    }
    if triggered_by:
        entry["triggered_by"] = triggered_by
    _append_feed_line(entry)


def read_activity_state_for_checkers() -> dict[str, Any]:
    """Public read for auto_train_reply_gate.py: returns current state dict."""
    with _ACTIVITY_LOCK:
        return _read_activity_state()


def chat_idle_seconds() -> float | None:
    """Return how many seconds since the most recent user or bot activity.

    Returns None when no activity has ever been recorded (treat as fully idle).
    """
    state = read_activity_state_for_checkers()
    last = state.get("last_event_at")
    if not isinstance(last, (int, float)):
        return None
    return max(time.time() - float(last), 0.0)
