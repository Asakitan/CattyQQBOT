"""会话上下文缓存：每个群/私聊一个独立窗口。

- 内存里用 OrderedDict 维持 LRU 顺序，超过 max_sessions 时从最久未访问端淘汰。
- 持久化到 sessions/ 目录，每个 session 一份 JSON，重启后自动恢复。
- 写盘走 dirty 标记 + 后台节流 flush，避免每条消息都同步写。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from nonebot import logger


ChatMessage = dict[str, Any]


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


class SessionCache:
    def __init__(
        self,
        directory: str | Path,
        *,
        max_sessions: int = 200,
        persistence_enabled: bool = True,
        debounce_seconds: float = 2.0,
    ) -> None:
        self._dir = Path(directory)
        self._max_sessions = max(int(max_sessions), 1)
        self._persistence_enabled = bool(persistence_enabled)
        self._debounce_seconds = max(float(debounce_seconds), 0.1)

        self._sessions: "OrderedDict[str, list[ChatMessage]]" = OrderedDict()
        self._last_access: dict[str, float] = {}
        # 主人 2026-05-29: last_turn_at 只在 set()(一轮对话完成写回)时更新, 读路径 get()
        # **不**碰它。_last_access 被 get() 的 LRU 刷新污染(每次读都变 now), 不能用来量
        # idle/跨天; last_turn_at 才是"上一轮对话真正发生的时刻"。
        self._last_turn_at: dict[str, float] = {}
        self._dirty: set[str] = set()
        self._loaded = False

    # ---- public sync API ----

    def get(self, key: str) -> list[ChatMessage]:
        """返回该会话的消息列表（不存在则空列表）。命中时会刷新 LRU 顺序。"""
        if key in self._sessions:
            self._sessions.move_to_end(key)
            self._last_access[key] = time.time()
            return self._sessions[key]
        return []

    def set(self, key: str, messages: list[ChatMessage]) -> None:
        """整段写入并标记 dirty。会触发 LRU 淘汰。"""
        now = time.time()
        self._sessions[key] = list(messages)
        self._sessions.move_to_end(key)
        self._last_access[key] = now
        # 一轮对话写回 = 这一轮真正发生的时刻(read 不更新它, 量 idle/跨天专用)。
        self._last_turn_at[key] = now
        self._dirty.add(key)
        self._evict_lru()

    def last_access_at(self, key: str) -> float | None:
        """返回该 scope 上次**访问**(含 get 读路径)epoch seconds。

        注意: get() 的 LRU 刷新会把它顶到 now, 跨请求量 idle 不可靠 —— 量 idle/跨天
        请用 last_turn_at()。这个保留给 LRU/dashboard 报告。
        """
        return self._last_access.get(key)

    def last_turn_at(self, key: str) -> float | None:
        """返回该 scope 上一轮对话**完成写回**的 epoch seconds。空 = 没对话过。

        只由 set()(_append_history 写回一轮)更新, 读路径 get() 不碰 —— 所以跨请求读到
        的是"上一轮真正发生的时刻", 用来量 idle / 判定跨天 (reunion / 时间跳跃感知)。
        """
        return self._last_turn_at.get(key)

    def pop(self, key: str) -> list[ChatMessage] | None:
        """删除会话（内存 + 盘上）。"""
        msgs = self._sessions.pop(key, None)
        self._last_access.pop(key, None)
        self._last_turn_at.pop(key, None)
        self._dirty.discard(key)
        if self._persistence_enabled:
            self._delete_file(key)
        return msgs

    def list_sessions(self) -> list[tuple[str, int, float]]:
        """返回 [(key, 消息数, 最后访问时间)]，按最近访问倒序。"""
        items = [
            (key, len(messages), self._last_access.get(key, 0.0))
            for key, messages in self._sessions.items()
        ]
        items.sort(key=lambda x: x[2], reverse=True)
        return items

    def total_sessions(self) -> int:
        return len(self._sessions)

    def has(self, key: str) -> bool:
        return key in self._sessions

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    @property
    def persistence_enabled(self) -> bool:
        return self._persistence_enabled

    @property
    def directory(self) -> Path:
        return self._dir

    # ---- persistence ----

    def load_from_disk(self) -> int:
        if self._loaded:
            return len(self._sessions)
        self._loaded = True
        if not self._persistence_enabled:
            return 0
        if not self._dir.exists():
            return 0
        entries: list[tuple[float, str, list[ChatMessage]]] = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"session_cache: failed to read {path.name}: {exc}")
                continue
            key = data.get("key") if isinstance(data, dict) else None
            messages = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(key, str) or not isinstance(messages, list):
                continue
            last_access = float(data.get("last_access") or path.stat().st_mtime)
            # 旧文件没 last_turn → 退回 last_access(对老数据已是最好的"上轮时刻"估计)。
            last_turn = float(data.get("last_turn") or last_access)
            cleaned: list[ChatMessage] = []
            for item in messages:
                if isinstance(item, dict) and "role" in item and "content" in item:
                    cleaned.append({"role": str(item["role"]), "content": item["content"]})
            entries.append((last_access, last_turn, key, cleaned))
        entries.sort(key=lambda x: x[0])
        for last_access, last_turn, key, messages in entries:
            self._sessions[key] = messages
            self._last_access[key] = last_access
            self._last_turn_at[key] = last_turn
        self._evict_lru()
        return len(self._sessions)

    def flush_sync(self) -> int:
        if not self._persistence_enabled:
            self._dirty.clear()
            return 0
        if not self._dirty:
            return 0
        self._dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for key in list(self._dirty):
            if self._write_one(key):
                written += 1
        self._dirty.clear()
        return written

    async def background_flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._debounce_seconds)
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"session_cache: background flush failed: {exc}")

    # ---- internal ----

    def _evict_lru(self) -> None:
        while len(self._sessions) > self._max_sessions:
            oldest_key, _ = self._sessions.popitem(last=False)
            self._last_access.pop(oldest_key, None)
            self._last_turn_at.pop(oldest_key, None)
            self._dirty.discard(oldest_key)
            if self._persistence_enabled:
                self._delete_file(oldest_key)
            logger.info(f"session_cache: evicted LRU session {oldest_key}")

    def _key_to_path(self, key: str) -> Path:
        return self._dir / f"{_sanitize_key_for_filename(key)}.json"

    def _write_one(self, key: str) -> bool:
        messages = self._sessions.get(key)
        if messages is None:
            return False
        path = self._key_to_path(key)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "key": key,
            "messages": messages,
            "last_access": self._last_access.get(key, time.time()),
            "last_turn": self._last_turn_at.get(key, self._last_access.get(key, time.time())),
            "saved_at": time.time(),
        }
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"session_cache: failed to write {key}: {exc}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False

    def _delete_file(self, key: str) -> None:
        path = self._key_to_path(key)
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"session_cache: failed to delete {key} ({path.name}): {exc}")


def format_session_list_for_owner(
    cache: SessionCache,
    *,
    limit: int = 30,
) -> str:
    items = cache.list_sessions()
    if not items:
        return (
            "喵～现在一个会话都还没攒到呢ฅฅ\n"
            f"目录：{cache.directory}\n"
            f"上限：{cache.max_sessions}，持久化：{'开' if cache.persistence_enabled else '关'}"
        )
    total = len(items)
    shown = items[:max(int(limit), 1)]
    lines = [
        f"喵～当前会话窗口 {total} 个（上限 {cache.max_sessions}，持久化{'开' if cache.persistence_enabled else '关'}）：",
    ]
    now = time.time()
    for idx, (key, count, last_access) in enumerate(shown, start=1):
        age = max(now - last_access, 0.0)
        if age < 60:
            age_str = f"{int(age)}s 前"
        elif age < 3600:
            age_str = f"{int(age / 60)}m 前"
        elif age < 86400:
            age_str = f"{int(age / 3600)}h 前"
        else:
            age_str = f"{int(age / 86400)}d 前"
        lines.append(f"{idx}. {key}  msgs={count}  最近 {age_str}")
    if total > len(shown):
        lines.append(f"…（还有 {total - len(shown)} 个未显示）")
    lines.append(f"目录：{cache.directory}")
    return "\n".join(lines)
