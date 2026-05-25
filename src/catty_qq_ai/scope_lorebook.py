"""Per-scope 笨猫学到的『这个群专属小事』lorebook — AI 自动总结生成的 character_book。

跟 character_card.CATTY_CARD.character_book (硬编码角色私货)的分工:
- character_book:  写死在代码里的角色基础设定(尾巴/猫粮/弦化/欧泊...)
- scope_lorebook:  **AI 每天从对话总结**出的『这个群特有的东西』 — 群内梗 / 重要人物
                   / 群规 / 重大事件, 让笨猫『记得这个群』

为什么 AI 自动总结而不是主人手动:
- 主人懒得手动维护
- 群内动态变化(新梗每天有),AI 能从最近 history 抓
- 主人保留 /lore_show /lore_remove 的最终裁决权

数据结构:
- 每条 entry: identifier / keys / content / created_at / last_hit_at / hit_count
- per-scope LRU (最多 20 条), 满了删 last_hit_at 最早的
- 持久化到 memory_dir/scope_lorebooks.json, 30s flush + on_shutdown flush

触发学习:
- 主人发 /lore_summarize 时手动跑一次 spark 总结
- (后续轮做后台定时, 这一轮先验证 spark 总结质量)

prompt 注入:
- 集成到 prompt_manager 的 _build_character_book 递归扫描 — scope entries
  跟 character_card 的 hardcoded entries 一起进入 BFS pool, 命中关键词时注入。
- (本轮先建 store + 命令, 集成留下一轮)
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


_MAX_ENTRIES_PER_SCOPE = 30        # 数量 cap (prompt 注入数量不要太多, 防稀释效果)
_MAX_BYTES_PER_SCOPE = 200_000     # 大小 cap = 200KB/scope (主人约束: 超过就 LRU 压缩)
_MAX_TOTAL_SCOPES = 100            # 整体 scope 数 cap (磁盘保护, 太多 scope 时 LRU)


@dataclass
class ScopeLoreEntry:
    """单条 scope 学到的小事(类比 ST character_book entry)。"""
    identifier: str
    keys: tuple[str, ...]
    content: str
    created_at: float = 0.0
    last_hit_at: float = 0.0
    hit_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "keys": list(self.keys),
            "content": self.content,
            "created_at": self.created_at,
            "last_hit_at": self.last_hit_at,
            "hit_count": self.hit_count,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ScopeLoreEntry | None":
        ident = str(payload.get("identifier") or "")
        content = str(payload.get("content") or "").strip()
        if not ident or not content:
            return None
        raw_keys = payload.get("keys") or []
        if not isinstance(raw_keys, list):
            return None
        keys = tuple(str(k).strip() for k in raw_keys if str(k).strip())
        if not keys:
            return None
        return cls(
            identifier=ident,
            keys=keys,
            content=content,
            created_at=float(payload.get("created_at") or 0.0),
            last_hit_at=float(payload.get("last_hit_at") or 0.0),
            hit_count=int(payload.get("hit_count") or 0),
        )


class ScopeLorebookStore:
    """per-scope 持久化 lorebook entries 容器。

    线程安全;30s 后台 flush;atomic write 不丢数据。
    """
    def __init__(self, memory_path: str | Path) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._path = p.parent / "scope_lorebooks.json"
        self._lock = threading.RLock()
        self._data: dict[str, list[ScopeLoreEntry]] = {}
        self._meta: dict[str, dict[str, Any]] = {}  # per-scope metadata: last_summary_date 等
        self._last_access: dict[str, float] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        scopes = raw.get("scopes") or {}
        if not isinstance(scopes, dict):
            return
        now = time.time()
        for sc, items in scopes.items():
            if not isinstance(items, list):
                continue
            parsed: list[ScopeLoreEntry] = []
            for it in items:
                if isinstance(it, dict):
                    entry = ScopeLoreEntry.from_payload(it)
                    if entry is not None:
                        parsed.append(entry)
            if parsed:
                self._data[str(sc)] = parsed
                self._last_access[str(sc)] = now
        meta = raw.get("meta") or {}
        if isinstance(meta, dict):
            for sc, m in meta.items():
                if isinstance(m, dict):
                    self._meta[str(sc)] = dict(m)

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "scopes": {
                sc: [e.to_payload() for e in entries]
                for sc, entries in self._data.items()
            },
            "meta": {sc: dict(m) for sc, m in self._meta.items() if m},
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    def flush_sync(self) -> bool:
        with self._lock:
            if not self._dirty:
                return False
            try:
                self._atomic_write()
            except OSError:
                return False
            self._dirty = False
            return True

    async def background_flush_loop(self) -> None:
        import asyncio
        while True:
            try:
                await asyncio.sleep(30.0)
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                pass

    def _evict_scope_lru(self) -> None:
        if len(self._data) <= _MAX_TOTAL_SCOPES:
            return
        ordered = sorted(self._last_access.items(), key=lambda kv: kv[1])
        for sc, _ in ordered[: len(self._data) - _MAX_TOTAL_SCOPES]:
            self._data.pop(sc, None)
            self._last_access.pop(sc, None)

    @staticmethod
    def _entry_byte_size(entry: ScopeLoreEntry) -> int:
        """单条 entry 估算字节大小 (UTF-8 编码后, content + keys + 元数据)。"""
        return (
            len(entry.content.encode("utf-8"))
            + sum(len(k.encode("utf-8")) for k in entry.keys)
            + 64  # identifier + 时间戳 + JSON 包装的常数开销
        )

    def _evict_entries_until_fit(self, scope: str) -> None:
        """按 last_hit_at 升序删最早的, 直到同时满足 entry count <= MAX 且 size <= MAX。

        主人约束: 200KB/scope 超过就 LRU 压缩 — 保新弃旧, 命中过的优先保留。
        """
        entries = self._data.get(scope) or []
        # 按 last_hit_at 升序排(没命中过的当 created_at), 早的先 evict
        entries.sort(key=lambda e: e.last_hit_at if e.last_hit_at > 0 else e.created_at)
        while entries and (
            len(entries) > _MAX_ENTRIES_PER_SCOPE
            or sum(self._entry_byte_size(e) for e in entries) > _MAX_BYTES_PER_SCOPE
        ):
            entries.pop(0)  # 删 last_hit_at 最早的那条
        self._data[scope] = entries

    def add_entry(self, scope: str, keys: Iterable[str], content: str) -> ScopeLoreEntry | None:
        """添加一条新 entry。keys 去重 / 去空 / content trim。返回新 entry 或 None(参数无效)。"""
        if not scope or not content or not content.strip():
            return None
        key_tuple = tuple(dict.fromkeys(k.strip() for k in keys if k and k.strip()))
        if not key_tuple:
            return None
        now = time.time()
        entry = ScopeLoreEntry(
            identifier=f"scope_lore_{uuid.uuid4().hex[:8]}",
            keys=key_tuple,
            content=content.strip(),
            created_at=now,
            last_hit_at=0.0,
            hit_count=0,
        )
        with self._lock:
            self._data.setdefault(scope, []).append(entry)
            self._last_access[scope] = now
            self._evict_entries_until_fit(scope)
            self._evict_scope_lru()
            self._dirty = True
        return entry

    def list_entries(self, scope: str) -> list[ScopeLoreEntry]:
        """返回 scope 当前所有 entries 拷贝(按 created_at 升序)。"""
        with self._lock:
            entries = list(self._data.get(scope) or [])
        entries.sort(key=lambda e: e.created_at)
        return entries

    def remove_entry(self, scope: str, identifier: str) -> bool:
        """按 identifier 删除一条 entry。返回是否真删了。"""
        with self._lock:
            entries = self._data.get(scope) or []
            before = len(entries)
            entries = [e for e in entries if e.identifier != identifier]
            if len(entries) == before:
                return False
            if entries:
                self._data[scope] = entries
            else:
                self._data.pop(scope, None)
                self._last_access.pop(scope, None)
            self._dirty = True
            return True

    def mark_hit(self, scope: str, identifier: str) -> None:
        """命中时刷 last_hit_at + hit_count(给 character_book 递归扫描调用)。"""
        now = time.time()
        with self._lock:
            for entry in self._data.get(scope) or []:
                if entry.identifier == identifier:
                    entry.last_hit_at = now
                    entry.hit_count += 1
                    self._last_access[scope] = now
                    self._dirty = True
                    return

    def total_scopes(self) -> int:
        with self._lock:
            return len(self._data)

    def total_entries(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._data.values())

    def scope_byte_size(self, scope: str) -> int:
        """诊断: 返回 scope 当前 entries 估算字节大小(给 dashboard / debug)。"""
        with self._lock:
            entries = self._data.get(scope) or []
            return sum(self._entry_byte_size(e) for e in entries)

    # ── 后台 auto-summary 节流支持 ─────────────────────────────────
    def was_summarized_on(self, scope: str, date_str: str) -> bool:
        """检查 scope 是否在指定日期(YYYY-MM-DD) 已总结过。"""
        with self._lock:
            m = self._meta.get(scope) or {}
            return str(m.get("last_summary_date") or "") == date_str

    def mark_summarized_on(self, scope: str, date_str: str) -> None:
        """记录 scope 在指定日期总结过(节流标记)。"""
        with self._lock:
            m = self._meta.setdefault(scope, {})
            m["last_summary_date"] = date_str
            m["last_summary_at"] = time.time()
            self._dirty = True

    def last_summary_date(self, scope: str) -> str:
        """返回 scope 上次总结日期(YYYY-MM-DD), 没有返回 ''。"""
        with self._lock:
            m = self._meta.get(scope) or {}
            return str(m.get("last_summary_date") or "")


__all__ = [
    "ScopeLoreEntry",
    "ScopeLorebookStore",
]
