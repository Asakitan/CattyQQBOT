"""Small durable-file and process-lock helpers; no plugin imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


def reject_links(path: Path) -> None:
    """Inspect lexical ancestors before resolve can erase junction evidence."""
    absolute = path.absolute()
    for item in reversed((absolute, *absolute.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked/reparse paths are not accepted by self repair")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("hardlinked files are not accepted by self repair")


def atomic_bytes(path: Path, data: bytes) -> None:
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_links(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".repair-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        reject_links(path)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, data: Any) -> None:
    atomic_bytes(path, (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def read_json(path: Path) -> dict[str, Any]:
    reject_links(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("self repair state must be a JSON object")
    return value


class FileLock:
    """Nonblocking OS lock, released by process exit; the lock inode is retained."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: Any = None

    def acquire(self) -> bool:
        if self.stream is not None:
            return True
        reject_links(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def close(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise RuntimeError("another self repair process owns this workspace")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()