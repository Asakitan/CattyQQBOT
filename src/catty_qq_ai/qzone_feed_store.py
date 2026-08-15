"""QQ空间动态见闻存储 - 机机 persona 语料源 (2026-08-15)."""
from __future__ import annotations
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any
from nonebot import logger

class QzoneFeedStore:
    _AUTO_LIKE_STATES = frozenset({"", "pending", "failed", "liked", "skipped"})
    _AI_LIKE_DECISIONS = frozenset({"", "LIKE", "SKIP"})

    def __init__(self, path: str | Path, max_items: int = 200, ttl_days: int = 7) -> None:
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            self.path = Path.cwd() / self.path
        self.max_items = max(int(max_items), 1)
        self.ttl_days = max(float(ttl_days), 0.0)
        self._lock = threading.RLock()
        self._feeds: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._mtime_ns: int | None = None
        self._flush_timer: threading.Timer | None = None
        self._load()

    @staticmethod
    def _mtime(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _likers(value: Any) -> list[str]:
        if value is None:
            values = []
        elif isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            values = [value]
        result: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    @classmethod
    def _auto_like_state(cls, value: Any) -> str:
        state = str(value or "").strip().lower()
        return state if state in cls._AUTO_LIKE_STATES else ""

    @classmethod
    def _ai_like_decision(cls, value: Any) -> str:
        decision = str(value or "").strip().upper()
        return decision if decision in cls._AI_LIKE_DECISIONS else ""

    @classmethod
    def _normalize(cls, raw: Any, now: float | None = None) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        feed_id = str(raw.get("feed_id") or "").strip()
        if not feed_id:
            return None
        current = time.time() if now is None else now
        feed_time = cls._float(raw.get("feed_time"), 0.0)
        if not math.isfinite(feed_time) or feed_time <= 0:
            return None
        recorded_at_raw = raw.get("recorded_at")
        if recorded_at_raw is None or recorded_at_raw == "":
            recorded_at = current
        else:
            recorded_at = cls._float(recorded_at_raw, 0.0)
            if not math.isfinite(recorded_at) or recorded_at <= 0:
                return None
        return {
            "feed_id": feed_id,
            "author_uin": str(raw.get("author_uin") or ""),
            "author_name": str(raw.get("author_name") or ""),
            "text": str(raw.get("text") or ""),
            "like_count": max(cls._int(raw.get("like_count")), 0),
            "liked_by": cls._likers(raw.get("liked_by")),
            "feed_time": feed_time,
            "recorded_at": recorded_at,
            "source": str(raw.get("source") or "qzone"),
            "auto_like_state": cls._auto_like_state(raw.get("auto_like_state")),
            "ai_like_decision": cls._ai_like_decision(raw.get("ai_like_decision")),
        }

    def _load(self) -> None:
        with self._lock:
            self._feeds = {}
            try:
                if not self.path.exists():
                    self._mtime_ns = None
                    return
                loaded = json.loads(self.path.read_text(encoding="utf-8-sig"))
                raw_feeds = loaded.get("feeds", []) if isinstance(loaded, dict) else []
                if isinstance(raw_feeds, list):
                    for raw in raw_feeds:
                        item = self._normalize(raw)
                        if item is not None:
                            self._feeds[item["feed_id"]] = item
                self._trim_locked()
                self._mtime_ns = self._mtime(self.path)
            except Exception as exc:
                self._feeds = {}
                self._mtime_ns = self._mtime(self.path)
                logger.warning(f"qzone_feed_store: load failed: {exc}")

    def _trim_locked(self) -> None:
        if len(self._feeds) <= self.max_items:
            return
        values = sorted(self._feeds.values(), key=lambda item: item["feed_time"], reverse=True)
        self._feeds = {item["feed_id"]: item for item in values[: self.max_items]}

    def _prune_locked(self) -> bool:
        cutoff = time.time() - self.ttl_days * 86400.0
        expired = [key for key, item in self._feeds.items() if item["feed_time"] < cutoff]
        for key in expired:
            self._feeds.pop(key, None)
        return bool(expired)

    def _mark_dirty_locked(self) -> None:
        self._dirty = True
        if self._flush_timer is not None and self._flush_timer.is_alive():
            return
        self._flush_timer = threading.Timer(3.0, self.flush_sync)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _payload_locked(self) -> dict[str, list[dict[str, Any]]]:
        values = sorted(self._feeds.values(), key=lambda item: item["feed_time"], reverse=True)
        return {"feeds": [dict(item, liked_by=list(item["liked_by"])) for item in values]}

    def record_feed(self, *, feed_id: str, author_uin: str, author_name: str, text: str, like_count: int = 0, liked_by: list[str] | None = None, feed_time: float = 0.0, source: str = "qzone", auto_like_state: str | None = None, ai_like_decision: str | None = None) -> dict[str, Any]:
        try:
            now = time.time()
            raw = {"feed_id": feed_id, "author_uin": author_uin, "author_name": author_name, "text": text, "like_count": like_count, "liked_by": liked_by, "feed_time": feed_time, "recorded_at": now, "source": source}
            if auto_like_state is not None:
                raw["auto_like_state"] = auto_like_state
            if ai_like_decision is not None:
                normalized_decision = str(ai_like_decision or "").strip().upper()
                if normalized_decision not in self._AI_LIKE_DECISIONS:
                    return {}
                raw["ai_like_decision"] = normalized_decision
            item = self._normalize(raw, now)
            if item is None:
                return {}
            with self._lock:
                old = self._feeds.get(item["feed_id"])
                if old is not None:
                    item["like_count"] = max(old["like_count"], item["like_count"])
                    item["liked_by"] = old["liked_by"] + [x for x in item["liked_by"] if x not in old["liked_by"]]
                    item["recorded_at"] = old["recorded_at"]
                    if auto_like_state is None:
                        item["auto_like_state"] = old["auto_like_state"]
                    if ai_like_decision is None:
                        item["ai_like_decision"] = old["ai_like_decision"]
                self._feeds[item["feed_id"]] = item
                self._prune_locked()
                self._trim_locked()
                self._mark_dirty_locked()
                return dict(item, liked_by=list(item["liked_by"]))
        except Exception as exc:
            logger.warning(f"qzone_feed_store: record_feed failed: {exc}")
            return {}

    def bump_like(self, feed_id: str, *, like_count: int | None = None, liker_uin: str = "", liker_name: str = "") -> bool:
        try:
            with self._lock:
                item = self._feeds.get(str(feed_id or "").strip())
                if item is None:
                    return False
                old_like_count = item["like_count"]
                old_liked_by = list(item["liked_by"])
                changed = False
                if like_count is not None:
                    count = max(self._int(like_count), 0)
                    if count > item["like_count"]:
                        item["like_count"] = count
                        changed = True
                # 分别 strip 再 fallback: liker_uin=" " 时不挡住有效 liker_name (Review 2026-08-15)
                liker = str(liker_uin or "").strip() or str(liker_name or "").strip()
                if liker and liker not in item["liked_by"]:
                    item["liked_by"].append(liker)
                    changed = True
                if changed:
                    self._mark_dirty_locked()
                if not changed:
                    # 无实际变化返回 False: 防止重复点赞事件触发下游语料随机改写 (Review 2026-08-15)
                    return False
                if self.flush_sync():
                    return True
                item["like_count"] = old_like_count
                item["liked_by"] = old_liked_by
                self._mark_dirty_locked()
                return False
        except Exception as exc:
            logger.warning(f"qzone_feed_store: bump_like failed: {exc}")
            return False

    def _persist_auto_like_fields(
        self,
        feed_id: str,
        *,
        state: str | None = None,
        decision: str | None = None,
    ) -> bool:
        normalized_id = str(feed_id or "").strip()
        normalized_state = None if state is None else str(state or "").strip().lower()
        normalized_decision = None if decision is None else str(decision or "").strip().upper()
        if not normalized_id:
            return False
        if normalized_state is not None and normalized_state not in self._AUTO_LIKE_STATES:
            return False
        if normalized_decision is not None and normalized_decision not in self._AI_LIKE_DECISIONS:
            return False
        try:
            with self._lock:
                item = self._feeds.get(normalized_id)
                if item is None:
                    return False
                old_state = item["auto_like_state"]
                old_decision = item["ai_like_decision"]
                changed = False
                if normalized_state is not None and item["auto_like_state"] != normalized_state:
                    item["auto_like_state"] = normalized_state
                    changed = True
                if normalized_decision is not None and item["ai_like_decision"] != normalized_decision:
                    item["ai_like_decision"] = normalized_decision
                    changed = True
                if changed:
                    self._mark_dirty_locked()
                if self.flush_sync():
                    return True
                item["auto_like_state"] = old_state
                item["ai_like_decision"] = old_decision
                self._mark_dirty_locked()
                return False
        except Exception as exc:
            logger.warning(f"qzone_feed_store: persist auto-like fields failed: {exc}")
            return False

    def set_auto_like_state(self, feed_id: str, state: str) -> bool:
        return self._persist_auto_like_fields(feed_id, state=state)

    def set_ai_like_decision(self, feed_id: str, decision: str) -> bool:
        return self._persist_auto_like_fields(feed_id, decision=decision)

    def set_auto_like_outcome(self, feed_id: str, *, state: str, decision: str) -> bool:
        return self._persist_auto_like_fields(feed_id, state=state, decision=decision)

    def feeds_needing_auto_like(self, *, limit: int = 20, max_chars_each: int = 120) -> list[dict[str, Any]]:
        try:
            limit = max(int(limit), 0)
            max_chars_each = max(int(max_chars_each), 0)
            with self._lock:
                if self._prune_locked():
                    self._mark_dirty_locked()
                values = sorted(
                    (
                        item for item in self._feeds.values()
                        if item["auto_like_state"] in {"pending", "failed"}
                    ),
                    key=lambda item: item["feed_time"],
                    reverse=True,
                )[:limit]
                result = []
                for item in values:
                    copy = dict(item, liked_by=list(item["liked_by"]))
                    copy["text"] = copy["text"][:max_chars_each]
                    result.append(copy)
                return result
        except Exception as exc:
            logger.warning(f"qzone_feed_store: feeds_needing_auto_like failed: {exc}")
            return []

    def get_feed(self, feed_id: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                if self._prune_locked():
                    self._mark_dirty_locked()
                item = self._feeds.get(str(feed_id or "").strip())
                if item is None:
                    return None
                return dict(item, liked_by=list(item["liked_by"]))
        except Exception as exc:
            logger.warning(f"qzone_feed_store: get_feed failed: {exc}")
            return None

    def recent_feeds(self, *, limit: int = 5, max_chars_each: int = 120) -> list[dict[str, Any]]:
        try:
            limit = max(int(limit), 0)
            max_chars_each = max(int(max_chars_each), 0)
            with self._lock:
                if self._prune_locked():
                    self._mark_dirty_locked()
                values = sorted(self._feeds.values(), key=lambda item: item["feed_time"], reverse=True)[:limit]
                result = []
                for item in values:
                    copy = dict(item, liked_by=list(item["liked_by"]))
                    copy["text"] = copy["text"][:max_chars_each]
                    result.append(copy)
                return result
        except Exception as exc:
            logger.warning(f"qzone_feed_store: recent_feeds failed: {exc}")
            return []

    def refresh(self) -> None:
        try:
            current = self._mtime(self.path)
            with self._lock:
                if current == self._mtime_ns:
                    return
                if self._dirty and not self.flush_sync():
                    return
                self._load()
        except Exception as exc:
            logger.warning(f"qzone_feed_store: refresh failed: {exc}")

    def flush_sync(self) -> bool:
        try:
            with self._lock:
                if not self._dirty:
                    return True
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                try:
                    tmp.write_text(json.dumps(self._payload_locked(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    os.replace(tmp, self.path)
                finally:
                    if tmp.exists():
                        tmp.unlink()
                self._dirty = False
                self._mtime_ns = self._mtime(self.path)
                if self._mtime_ns is None:
                    self._dirty = True
                    raise OSError("persisted feed store is not stat-able")
                return True
        except Exception as exc:
            logger.warning(f"qzone_feed_store: flush_sync failed: {exc}")
            return False