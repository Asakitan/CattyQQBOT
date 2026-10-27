"""Per-scope 人格命令覆盖持久化 — /人格 命令的落盘层。

优先级: 本 store(命令切换) > config.catty_group_personas > catty_default_persona。
低频写(只有主人发 /人格 才写), 不做 debounce, set/clear 即时 atomic 落盘。
文件: memory_dir/persona_overrides.json — {scope: {"persona": name, "set_by": qq, "set_at": ts}}
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import logging
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.01


def _try_file_lock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    locked = False
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _try_file_lock(fd)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("persona override lock timed out") from exc
                time.sleep(min(_LOCK_RETRY_SECONDS, remaining))
        yield
    finally:
        if locked:
            try:
                _unlock_file(fd)
            except OSError:
                logging.getLogger(__name__).warning("persona override lock release failed; closing descriptor")
        os.close(fd)


class PersonaOverrideStore:
    """线程安全的 scope → persona 覆盖表。"""

    def __init__(self, memory_path: str | Path) -> None:
        base = Path(str(memory_path or "memory.json")).expanduser()
        self._path = base.parent / "persona_overrides.json"
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load_failed = False
        self._load()

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        data: dict[str, dict[str, Any]] = {}
        for scope, payload in raw.items():
            if isinstance(payload, dict) and str(payload.get("persona") or "").strip():
                try:
                    set_at = float(payload.get("set_at") or 0.0)
                    if not math.isfinite(set_at):
                        set_at = 0.0
                except (TypeError, ValueError, OverflowError):
                    set_at = 0.0
                data[str(scope)] = {
                    "persona": str(payload["persona"]).strip(),
                    "set_by": str(payload.get("set_by") or ""),
                    "set_at": set_at,
                }
        return data

    def _read_disk(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            if self._path.is_symlink():
                raise OSError("persona override file is a dangling symlink") from exc
            return {}, {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OSError("persona override file could not be read; restore it before writing") from exc
        if not isinstance(raw, dict):
            raise OSError("persona override file is not an object; restore it before writing")
        return raw, self._normalize(raw)

    def _load(self) -> None:
        try:
            _, current = self._read_disk()
        except OSError:
            if not self._load_failed:
                logging.getLogger(__name__).warning("persona override file could not be read; writes disabled")
            self._load_failed = True
            return
        self._data = current
        self._load_failed = False

    def _write_locked(self, candidate: dict[str, Any]) -> None:
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
            self._load()
            payload = self._data.get(scope)
        return payload["persona"] if payload else None

    def set(self, scope: str, persona_name: str, *, set_by: str = "") -> None:
        if not scope or not str(persona_name or "").strip():
            return
        with self._lock:
            with _exclusive_file_lock(self._path.with_name(self._path.name + ".lock")):
                raw, _ = self._read_disk()
                candidate = dict(raw)
                candidate[scope] = {
                    "persona": str(persona_name).strip(),
                    "set_by": str(set_by or ""),
                    "set_at": time.time(),
                }
                current = self._normalize(candidate)
                self._write_locked(candidate)
                self._data = current
                self._load_failed = False

    def clear(self, scope: str) -> bool:
        with self._lock:
            with _exclusive_file_lock(self._path.with_name(self._path.name + ".lock")):
                raw, current = self._read_disk()
                existed = scope in current
                if existed:
                    candidate = dict(raw)
                    del candidate[scope]
                    current = self._normalize(candidate)
                    self._write_locked(candidate)
                self._data = current
                self._load_failed = False
        return existed

    def all_overrides(self) -> dict[str, str]:
        with self._lock:
            self._load()
            return {scope: payload["persona"] for scope, payload in self._data.items()}


__all__ = ["PersonaOverrideStore"]
