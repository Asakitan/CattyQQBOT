"""Per-scope 人格命令覆盖持久化 — /人格 命令的落盘层。

优先级: 本 store(命令切换) > config.catty_group_personas > catty_default_persona。
低频写(只有主人发 /人格 才写), 不做 debounce, set/clear 即时 atomic 落盘。
文件: memory_dir/persona_overrides.json — {scope: {"persona": name, "set_by": qq, "set_at": ts}}
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class PersonaOverrideStore:
    """线程安全的 scope → persona 覆盖表。"""

    def __init__(self, memory_path: str | Path) -> None:
        base = Path(str(memory_path or "memory.json")).expanduser()
        self._path = base.parent / "persona_overrides.json"
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for scope, payload in raw.items():
            if isinstance(payload, dict) and str(payload.get("persona") or "").strip():
                self._data[str(scope)] = {
                    "persona": str(payload["persona"]).strip(),
                    "set_by": str(payload.get("set_by") or ""),
                    "set_at": float(payload.get("set_at") or 0.0),
                }

    def _save_locked(self) -> None:
        self._write_locked(self._data)

    def _write_locked(self, candidate: dict[str, dict[str, Any]]) -> None:
        """Write first; callers publish the candidate only after replace succeeds."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent,
        )
        tmp = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(candidate, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self._path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def get(self, scope: str) -> str | None:
        if not scope:
            return None
        with self._lock:
            payload = self._data.get(scope)
        return payload["persona"] if payload else None

    def set(self, scope: str, persona_name: str, *, set_by: str = "") -> None:
        if not scope or not str(persona_name or "").strip():
            return
        with self._lock:
            candidate = dict(self._data)
            candidate[scope] = {
                "persona": str(persona_name).strip(),
                "set_by": str(set_by or ""),
                "set_at": time.time(),
            }
            self._write_locked(candidate)
            self._data = candidate

    def clear(self, scope: str) -> bool:
        with self._lock:
            existed = scope in self._data
            if existed:
                candidate = dict(self._data)
                del candidate[scope]
                self._write_locked(candidate)
                self._data = candidate
        return existed

    def all_overrides(self) -> dict[str, str]:
        with self._lock:
            return {scope: payload["persona"] for scope, payload in self._data.items()}


__all__ = ["PersonaOverrideStore"]
