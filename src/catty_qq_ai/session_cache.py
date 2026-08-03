"""会话上下文缓存：每个群/私聊一个独立窗口。

- 内存里用 OrderedDict 维持热会话 LRU，max_sessions 只限制 RAM 驻留数。
- 持久化到 sessions/ 目录；冷会话保留 JSON 和索引，按需懒加载。
- 写盘走 dirty 标记 + 后台节流 flush，避免每条消息都同步写。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from nonebot import logger


ChatMessage = dict[str, Any]
SessionMetadata = dict[str, int | float]


def _legacy_sanitize_key_for_filename(key: str) -> str:
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


def _sanitize_key_for_filename(key: str) -> str:
    # 2026-08-03 Review: 旧纯替换方案不是单射 ("group:a:b" 和 "group__a__b" 同名),
    # 冷启动按 last_access 二选一会藏掉合法会话。保留可读前缀 + key 摘要保证单射;
    # 旧命名文件仍按 JSON 内 key 字段加载, 写盘成功后按同 key 清理 (见 _write_one)。
    safe = _legacy_sanitize_key_for_filename(key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    return f"{safe}--{digest}"


def _estimate_history_tokens(messages: Iterable[ChatMessage]) -> int:
    """给旧 session 的缺失元数据提供保守、无依赖的 token 估计。"""
    total = 0
    for message in messages:
        try:
            serialized = json.dumps(
                message,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
        except Exception:  # noqa: BLE001
            serialized = str(message)
        total += max((len(serialized) + 3) // 4, 1)
    return total


def _coerce_nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def _coerce_timestamp(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


def _default_metadata(
    messages: Iterable[ChatMessage],
    *,
    context_updated_at: float,
) -> SessionMetadata:
    return {
        "history_tokens_estimate": _estimate_history_tokens(messages),
        "trim_epoch": 0,
        "trim_count": 0,
        "context_updated_at": context_updated_at,
    }


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
        # 所有已知会话（热 + 冷）的元数据和真实落盘路径。messages 只留在 _sessions。
        self._metadata: dict[str, SessionMetadata] = {}
        self._paths: dict[str, Path] = {}
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
            self._touch(key)
            return self._sessions[key]
        messages = self._load_cold_session(key)
        return messages if messages is not None else []

    def set(self, key: str, messages: list[ChatMessage]) -> None:
        """整段写入并标记 dirty。会触发 LRU 淘汰。"""
        now = time.time()
        self._sessions[key] = list(messages)
        self._sessions.move_to_end(key)
        self._last_access[key] = now
        # 一轮对话写回 = 这一轮真正发生的时刻(read 不更新它, 量 idle/跨天专用)。
        self._last_turn_at[key] = now
        metadata = self._metadata.get(key)
        if metadata is None:
            self._metadata[key] = _default_metadata(
                messages,
                context_updated_at=now,
            )
        else:
            metadata["history_tokens_estimate"] = _estimate_history_tokens(messages)
            metadata["trim_epoch"] = _coerce_nonnegative_int(
                metadata.get("trim_epoch"),
                0,
            )
            metadata["trim_count"] = _coerce_nonnegative_int(
                metadata.get("trim_count"),
                0,
            )
            metadata["context_updated_at"] = now
        self._paths.setdefault(key, self._key_to_path(key))
        self._dirty.add(key)
        self._evict_lru()

    def get_metadata(self, key: str) -> SessionMetadata | None:
        """返回 session 上下文元数据副本；冷会话不会因读取元数据而进 RAM。"""
        metadata = self._metadata.get(key)
        return dict(metadata) if metadata is not None else None

    def set_metadata(
        self,
        key: str,
        *,
        history_tokens_estimate: int,
        trim_epoch: int = 0,
        trim_count: int = 0,
        context_updated_at: float | None = None,
    ) -> SessionMetadata | None:
        """完整替换一个已存在 session 的上下文元数据并标记持久化。"""
        if not self._ensure_resident(key):
            return None
        updated_at = time.time() if context_updated_at is None else _coerce_timestamp(
            context_updated_at,
            time.time(),
        )
        self._metadata[key] = {
            "history_tokens_estimate": _coerce_nonnegative_int(
                history_tokens_estimate,
                0,
            ),
            "trim_epoch": _coerce_nonnegative_int(trim_epoch, 0),
            "trim_count": _coerce_nonnegative_int(trim_count, 0),
            "context_updated_at": updated_at,
        }
        self._touch(key, mark_dirty=True)
        return self.get_metadata(key)

    def update_metadata(
        self,
        key: str,
        *,
        history_tokens_estimate: int | None = None,
        trim_epoch: int | None = None,
        trim_count: int | None = None,
        context_updated_at: float | None = None,
    ) -> SessionMetadata | None:
        """局部更新一个已存在 session 的上下文元数据并标记持久化。"""
        if (
            history_tokens_estimate is None
            and trim_epoch is None
            and trim_count is None
            and context_updated_at is None
        ):
            return self.get_metadata(key)
        if not self._ensure_resident(key):
            return None
        metadata = self._metadata[key]
        if history_tokens_estimate is not None:
            metadata["history_tokens_estimate"] = _coerce_nonnegative_int(
                history_tokens_estimate,
                0,
            )
        if trim_epoch is not None:
            metadata["trim_epoch"] = _coerce_nonnegative_int(trim_epoch, 0)
        if trim_count is not None:
            metadata["trim_count"] = _coerce_nonnegative_int(trim_count, 0)
        metadata["context_updated_at"] = (
            time.time()
            if context_updated_at is None
            else _coerce_timestamp(context_updated_at, time.time())
        )
        self._touch(key, mark_dirty=True)
        return self.get_metadata(key)

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
        path = self._paths.pop(key, None)
        self._metadata.pop(key, None)
        self._last_access.pop(key, None)
        self._last_turn_at.pop(key, None)
        self._dirty.discard(key)
        if self._persistence_enabled:
            self._delete_file(key, path)
        return msgs

    def list_sessions(self) -> list[tuple[str, int, float]]:
        """返回热会话 [(key, 消息数, 最后访问时间)]，按最近访问倒序。"""
        items = [
            (key, len(messages), self._last_access.get(key, 0.0))
            for key, messages in self._sessions.items()
        ]
        items.sort(key=lambda x: x[2], reverse=True)
        return items

    def total_sessions(self) -> int:
        return len(self._sessions)

    def has(self, key: str) -> bool:
        return key in self._metadata

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
        entries: dict[
            str,
            tuple[float, float, str, list[ChatMessage], SessionMetadata, Path],
        ] = {}
        for path in self._dir.glob("*.json"):
            entry = self._read_session_file(path)
            if entry is None:
                continue
            last_access, _last_turn, key, _messages, _metadata = entry
            previous = entries.get(key)
            if previous is None or (last_access, path.name) >= (previous[0], previous[5].name):
                entries[key] = (*entry, path)

        cold_entries: list[
            tuple[float, float, str, list[ChatMessage], SessionMetadata, Path]
        ] = []
        for entry in entries.values():
            last_access, last_turn, key, messages, metadata, path = entry
            if key in self._sessions:
                continue
            self._last_access[key] = last_access
            self._last_turn_at[key] = last_turn
            self._metadata[key] = metadata
            self._paths[key] = path
            cold_entries.append(entry)

        cold_entries.sort(key=lambda entry: (entry[0], entry[2]))
        available_slots = max(self._max_sessions - len(self._sessions), 0)
        if available_slots:
            for last_access, last_turn, key, messages, metadata, path in cold_entries[-available_slots:]:
                self._sessions[key] = messages
                self._last_access[key] = last_access
                self._last_turn_at[key] = last_turn
                self._metadata[key] = metadata
                self._paths[key] = path
        return len(self._sessions)

    def flush_sync(self) -> int:
        if not self._persistence_enabled:
            self._dirty.clear()
            return 0
        if not self._dirty:
            return 0
        self._dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for key in tuple(self._dirty):
            if key not in self._sessions:
                self._dirty.discard(key)
                continue
            if self._write_one(key):
                written += 1
                self._dirty.discard(key)
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

    def _read_session_file(
        self,
        path: Path,
    ) -> tuple[float, float, str, list[ChatMessage], SessionMetadata] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"session_cache: failed to read {path.name}: {exc}")
            return None
        if not isinstance(data, dict):
            return None
        key = data.get("key")
        messages = data.get("messages")
        if not isinstance(key, str) or not isinstance(messages, list):
            return None

        cleaned: list[ChatMessage] = []
        for item in messages:
            if isinstance(item, dict) and "role" in item and "content" in item:
                cleaned.append({"role": str(item["role"]), "content": item["content"]})
        try:
            file_mtime = path.stat().st_mtime
        except OSError:
            file_mtime = time.time()
        last_access = _coerce_timestamp(data.get("last_access"), file_mtime)
        # 旧文件没 last_turn → 退回 last_access(对老数据已是最好的"上轮时刻"估计)。
        last_turn = _coerce_timestamp(data.get("last_turn"), last_access)
        metadata = _default_metadata(cleaned, context_updated_at=last_turn)
        metadata["history_tokens_estimate"] = _coerce_nonnegative_int(
            data.get("history_tokens_estimate"),
            int(metadata["history_tokens_estimate"]),
        )
        metadata["trim_epoch"] = _coerce_nonnegative_int(data.get("trim_epoch"), 0)
        metadata["trim_count"] = _coerce_nonnegative_int(data.get("trim_count"), 0)
        metadata["context_updated_at"] = _coerce_timestamp(
            data.get("context_updated_at"),
            last_turn,
        )
        return last_access, last_turn, key, cleaned, metadata

    def _load_cold_session(self, key: str) -> list[ChatMessage] | None:
        path = self._paths.get(key)
        if path is None:
            return None
        entry = self._read_session_file(path)
        if entry is None or entry[2] != key:
            self._forget_index(key)
            return None
        last_access, last_turn, _loaded_key, messages, metadata = entry
        self._sessions[key] = messages
        self._last_access[key] = last_access
        self._last_turn_at[key] = last_turn
        self._metadata[key] = metadata
        self._paths[key] = path
        self._touch(key)
        self._evict_lru()
        return messages

    def _ensure_resident(self, key: str) -> bool:
        if key in self._sessions:
            return True
        return self._load_cold_session(key) is not None

    def _touch(self, key: str, *, mark_dirty: bool = False) -> None:
        # 2026-08-03 Review: 读路径 (get / 冷会话懒加载) 只刷新内存 LRU + last_access,
        # 不再标 dirty — 否则每次拼 prompt 都触发一次无变化落盘。只有 set /
        # set_metadata / update_metadata 这种真实内容变更才传 mark_dirty=True。
        self._sessions.move_to_end(key)
        self._last_access[key] = time.time()
        if mark_dirty:
            self._dirty.add(key)

    def _forget_index(self, key: str) -> None:
        self._sessions.pop(key, None)
        self._metadata.pop(key, None)
        self._paths.pop(key, None)
        self._last_access.pop(key, None)
        self._last_turn_at.pop(key, None)
        self._dirty.discard(key)

    def _evict_lru(self) -> None:
        while len(self._sessions) > self._max_sessions:
            oldest_key = next(iter(self._sessions))
            if self._persistence_enabled and oldest_key in self._dirty:
                if not self._write_one(oldest_key):
                    logger.warning(
                        f"session_cache: retaining dirty LRU session after failed write: {oldest_key}"
                    )
                    break
                self._dirty.discard(oldest_key)
            if self._persistence_enabled:
                self._sessions.pop(oldest_key)
            else:
                self._forget_index(oldest_key)
            logger.info(f"session_cache: unloaded LRU session {oldest_key}")

    def _key_to_path(self, key: str) -> Path:
        return self._dir / f"{_sanitize_key_for_filename(key)}.json"

    def _legacy_key_to_path(self, key: str) -> Path:
        return self._dir / f"{_legacy_sanitize_key_for_filename(key)}.json"

    def _write_one(self, key: str) -> bool:
        messages = self._sessions.get(key)
        if messages is None:
            return False
        canonical = self._key_to_path(key)
        path = self._paths.get(key) or canonical
        if path == self._legacy_key_to_path(key) and path != canonical:
            # 旧命名文件迁移: 下一次写盘直接落到 digest 名, 成功后按同 key 清旧文件。
            path = canonical
        tmp = path.with_suffix(path.suffix + ".tmp")
        metadata = self._metadata.get(key)
        if metadata is None:
            metadata = _default_metadata(
                messages,
                context_updated_at=self._last_turn_at.get(key, time.time()),
            )
            self._metadata[key] = metadata
        payload = {
            "key": key,
            "messages": messages,
            "last_access": self._last_access.get(key, time.time()),
            "last_turn": self._last_turn_at.get(key, self._last_access.get(key, time.time())),
            "history_tokens_estimate": metadata["history_tokens_estimate"],
            "trim_epoch": metadata["trim_epoch"],
            "trim_count": metadata["trim_count"],
            "context_updated_at": metadata["context_updated_at"],
            "saved_at": time.time(),
        }
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            self._paths[key] = path
            # 旧命名 (无 digest) 文件: 仅当 JSON 内 key 确实属于本会话才清理,
            # 避免误删旧方案下撞名的其它会话数据。
            legacy = self._legacy_key_to_path(key)
            if legacy != path and legacy.exists():
                legacy_entry = self._read_session_file(legacy)
                if legacy_entry is not None and legacy_entry[2] == key:
                    try:
                        legacy.unlink()
                    except OSError as exc:
                        logger.warning(f"session_cache: failed to remove legacy file {legacy.name}: {exc}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"session_cache: failed to write {key}: {exc}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False

    def _delete_file(self, key: str, stored_path: Path | None = None) -> None:
        legacy_path = self._legacy_key_to_path(key)
        paths = [stored_path, self._key_to_path(key), legacy_path]
        seen: set[Path] = set()
        for path in paths:
            if path is None or path in seen:
                continue
            seen.add(path)
            if path == legacy_path:
                # 旧命名不是单射: 撞名文件可能属于别的 key (如 group:a:b vs
                # group__a__b), 只有 JSON 内 key 完全匹配才允许删除。
                entry = self._read_session_file(path) if path.exists() else None
                if entry is None or entry[2] != key:
                    continue
            for candidate in (path, path.with_suffix(path.suffix + ".tmp")):
                try:
                    if candidate.exists():
                        candidate.unlink()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"session_cache: failed to delete {key} ({candidate.name}): {exc}"
                    )


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
        f"喵～当前热会话窗口 {total} 个（内存上限 {cache.max_sessions}，持久化{'开' if cache.persistence_enabled else '关'}）：",
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