"""Data-only shared skills. No plugin imports, network calls or LLM execution.

``save_verified`` is an internal trusted-executor API, not a tool accepting chat
text. The caller verifies provenance and removes conversation/private data.
``root`` is the skill directory itself (normally ``data/skills``).
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from typing import Any, Iterator
from urllib.parse import urlsplit


_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA = re.compile(r"[0-9a-fA-F]{64}\Z")
_DEVICES = frozenset({"con", "prn", "aux", "nul", "clock"} | {
    f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(10)
})
_REQUIRED = frozenset({"id", "title", "description", "keywords", "body", "sources"})
_OPTIONAL = frozenset({"visibility", "visible", "created_at", "updated_at", "schema_version"})
_RECORD_KEYS = _REQUIRED | {"visibility", "created_at", "updated_at", "schema_version"}
_METADATA_KEYS = ("id", "title", "description", "keywords", "updated_at", "sources")
_SOURCE_KEYS = frozenset({"url", "title", "fetched_at", "content_sha256"})
_ROOT_LOCKS: dict[str, Any] = {}
_ROOT_LOCKS_GUARD = threading.Lock()
# Bound directory enumeration AND total bytes read, including malformed records.
_MAX_SCAN_ENTRIES = 4096
_MAX_SCAN_BYTES = 16 * 1024 * 1024
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class _StoreError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _error(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": exc.code if isinstance(exc, _StoreError) else "io_error"}


def _safe_id(value: Any) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 64
            or _ID.fullmatch(value) is None or value in _DEVICES):
        raise _StoreError("invalid_id")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise _StoreError("invalid_sha256")
    return value.lower()


def _text(value: Any, maximum: int, code: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or "\0" in value):
        raise _StoreError(code)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _StoreError(code) from None
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64 or "T" not in value:
        raise _StoreError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise _StoreError("invalid_timestamp") from None


def _definition(value: Any, body_limit: int) -> dict[str, Any]:
    if (not isinstance(value, dict) or not _REQUIRED <= value.keys()
            or value.keys() - (_REQUIRED | _OPTIONAL)):
        raise _StoreError("invalid_definition")
    result = {
        "id": _safe_id(value["id"]),
        "title": _text(value["title"], 100, "invalid_title"),
        "description": _text(value["description"], 300, "invalid_description"),
        "body": _text(value["body"], body_limit, "invalid_body"),
    }
    keywords = value["keywords"]
    if not isinstance(keywords, list) or len(keywords) > 8:
        raise _StoreError("invalid_keywords")
    result["keywords"] = [_text(item, 80, "invalid_keywords") for item in keywords]
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 3:
        raise _StoreError("invalid_sources")
    result["sources"] = []
    for source in sources:
        if not isinstance(source, dict) or source.keys() != _SOURCE_KEYS:
            raise _StoreError("invalid_sources")
        url = _text(source["url"], 2048, "invalid_sources")
        try:
            parts = urlsplit(url)
            if (parts.scheme not in {"http", "https"} or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or any(char.isspace() or ord(char) < 32 for char in url)):
                raise ValueError
            # Force malformed ports to fail during validation, not during later use.
            parts.port
        except ValueError:
            raise _StoreError("invalid_sources") from None
        try:
            source_sha = _sha(source["content_sha256"])
            fetched_at = _timestamp(source["fetched_at"])
        except _StoreError:
            raise _StoreError("invalid_sources") from None
        result["sources"].append({
            "url": url, "title": _text(source["title"], 300, "invalid_sources"),
            "fetched_at": fetched_at, "content_sha256": source_sha,
        })
    # Supplied scope/timestamps never control sharing or server timestamps.
    result["visibility"] = "global"
    result["schema_version"] = 1
    return result


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StoreError("invalid_record")
        result[key] = value
    return result


def _bad_constant(_: str) -> None:
    raise _StoreError("invalid_record")


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _regular(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _StoreError("unsafe_path")
    return info


def _check_root(root: Path, *, create: bool) -> bool:
    # Do not resolve first: that would erase evidence of symlinks/junctions.
    for directory in (*reversed(root.parents), root):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                directory.mkdir()
            except FileExistsError:
                pass
            info = directory.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise _StoreError("unsafe_path")
    return True


def _open_regular(path: Path, *, create: bool = False) -> int:
    """Check both path and opened object; never truncate an existing file."""
    before = _regular(path)
    flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        after = _regular(path)
        if (after is None or _is_link(opened) or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1 or not _same_file(opened, after)
                or (before is not None and not _same_file(before, opened))):
            raise _StoreError("unsafe_path")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _file_lock(root: Path) -> Iterator[None]:
    """Persistent lock inode: never unlink it, even when unlocking."""
    fd = _open_regular(root / ".skills.lock", create=True)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            # Windows byte-range locks may extend past EOF; no lock-file write
            # (and therefore no pre-lock initialization race) is needed.
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            except OSError:
                raise _StoreError("lock_unavailable") from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                raise _StoreError("lock_unavailable") from None
        acquired = True
        if not _check_root(root, create=False):
            raise _StoreError("unsafe_path")
        current = _regular(root / ".skills.lock")
        if current is None or not _same_file(os.fstat(fd), current):
            raise _StoreError("unsafe_path")
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            # Closing the descriptor releases the lock. Do not turn an already
            # committed replace into an apparent failed transaction.
            pass
        finally:
            os.close(fd)


def _tokens(text: str) -> set[str]:
    text = text.casefold()
    words = set(re.findall(r"[a-z0-9]+", text))
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", text):
        words.update(run[index:index + 2] for index in range(len(run) - 1))
        if len(run) == 1:
            words.add(run)
    return words


class SkillStore:
    """Lazy, metadata-only cache with disk-authoritative optimistic updates."""

    def __init__(self, root: str | Path, *, max_body_chars: int = 6000,
                 max_bytes: int = 65536) -> None:
        if (type(max_body_chars) is not int or max_body_chars <= 0
                or type(max_bytes) is not int or max_bytes <= 0):
            raise ValueError("invalid_limits")
        if not isinstance(root, (str, Path)) or not str(root) or "\0" in str(root):
            raise ValueError("invalid_root")
        self.root = Path(os.path.abspath(os.path.expanduser(str(root))))
        self.max_body_chars = max_body_chars
        self.max_bytes = max_bytes
        with _ROOT_LOCKS_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(os.path.normcase(str(self.root)), threading.RLock())
        self._metadata: dict[str, dict[str, Any]] = {}
        # No mkdir/stat/scan/load here, even if an existing library is huge.

    @contextmanager
    def _locked(self, *, create: bool = False) -> Iterator[bool]:
        with self._lock:
            if not _check_root(self.root, create=create):
                yield False
                return
            with _file_lock(self.root):
                yield True

    def _path(self, skill_id: str) -> Path:
        return self.root / f"{_safe_id(skill_id)}.json"

    def _read_bytes(self, path: Path) -> bytes:
        if path.parent != self.root or not _check_root(self.root, create=False):
            raise _StoreError("unsafe_path")
        fd = _open_regular(path)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > self.max_bytes:
                raise _StoreError("record_too_large")
            raw = handle.read(self.max_bytes + 1)
            after = os.fstat(handle.fileno())
            current = _regular(path)
            if (current is None or not _same_file(after, current)
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise _StoreError("sha_conflict")
        if not _check_root(self.root, create=False):
            raise _StoreError("unsafe_path")
        if len(raw) > self.max_bytes:
            raise _StoreError("record_too_large")
        return raw

    def _decode(self, raw: bytes, skill_id: str) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad_constant)
            if (not isinstance(value, dict) or value.keys() != _RECORD_KEYS
                    or value.get("id") != skill_id or value.get("visibility") != "global"
                    or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
                raise _StoreError("invalid_record")
            result = _definition(value, self.max_body_chars)
            result["created_at"] = _timestamp(value["created_at"])
            result["updated_at"] = _timestamp(value["updated_at"])
            if result["updated_at"] < result["created_at"]:
                raise _StoreError("invalid_record")
            return result
        except (ValueError, UnicodeError, RecursionError, TypeError):
            raise _StoreError("invalid_record") from None

    @staticmethod
    def _meta(skill: dict[str, Any], sha: str) -> dict[str, Any]:
        return {**{key: deepcopy(skill[key]) for key in _METADATA_KEYS},
                "sha256": sha, "body_chars": len(skill["body"])}

    def search(self, query: str, *, limit: int = 3) -> dict[str, Any]:
        try:
            _text(query, 1000, "invalid_query")
            if type(limit) is not int or limit < 1:
                raise _StoreError("invalid_limit")
            limit = min(limit, 5)
            query_tokens = _tokens(query)
            with self._locked() as exists:
                if not exists:
                    self._metadata = {}
                    return {"ok": True, "candidates": []}
                metadata: dict[str, dict[str, Any]] = {}
                consumed = 0
                with os.scandir(self.root) as entries:
                    for index, entry in enumerate(entries):
                        if index >= _MAX_SCAN_ENTRIES:
                            raise _StoreError("scan_limit")
                        if not entry.name.endswith(".json"):
                            continue
                        skill_id = entry.name[:-5]
                        try:
                            path = self._path(skill_id)
                            info = _regular(path)
                            if info is None or info.st_size > self.max_bytes:
                                continue
                            # Include a one-byte overflow probe in the bound.
                            consumed += info.st_size + 1
                            if consumed > _MAX_SCAN_BYTES:
                                raise _StoreError("scan_limit")
                            raw = self._read_bytes(path)
                            skill = self._decode(raw, skill_id)
                            metadata[skill_id] = self._meta(skill, hashlib.sha256(raw).hexdigest())
                        except _StoreError as exc:
                            if exc.code == "scan_limit":
                                raise
                            # Quarantine logically: omit this item, never rename
                            # or delete someone else's malformed file.
                            continue
                        except (OSError, ValueError, UnicodeError, RecursionError):
                            continue
                self._metadata = metadata
                ranked = []
                for item in metadata.values():
                    fields = (item["id"], item["title"], item["description"], *item["keywords"])
                    score = len(query_tokens & _tokens(" ".join(fields)))
                    if score:
                        ranked.append((score, item))
                ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
                return {"ok": True, "candidates": [deepcopy(item) for _, item in ranked[:limit]]}
        except (_StoreError, OSError, ValueError, UnicodeError, RecursionError) as exc:
            return _error(exc)

    def load(self, skill_id: str, *, expected_sha256: str) -> dict[str, Any]:
        try:
            path = self._path(skill_id)
            expected = _sha(expected_sha256)
            with self._locked() as exists:
                if not exists:
                    raise _StoreError("not_found")
                try:
                    raw = self._read_bytes(path)
                except FileNotFoundError:
                    raise _StoreError("not_found") from None
                sha = hashlib.sha256(raw).hexdigest()
                if sha != expected:
                    raise _StoreError("sha_conflict")
                skill = self._decode(raw, skill_id)
                return {"ok": True, "skill": skill, "sha256": sha}
        except (_StoreError, OSError, ValueError, UnicodeError, RecursionError) as exc:
            return _error(exc)

    def save_verified(self, definition: dict, *, overwrite: bool = False,
                      expected_sha256: str | None = None) -> dict[str, Any]:
        """Save an executor-verified definition, never untrusted tool/chat input."""
        try:
            skill = _definition(definition, self.max_body_chars)
            if type(overwrite) is not bool:
                raise _StoreError("invalid_overwrite")
            if overwrite and expected_sha256 is None:
                raise _StoreError("expected_sha256_required")
            if not overwrite and expected_sha256 is not None:
                raise _StoreError("unexpected_sha256")
            expected = _sha(expected_sha256) if overwrite else None
            with self._locked(create=True):
                path = self._path(skill["id"])

                def check_revision() -> bytes | None:
                    if not _check_root(self.root, create=False):
                        raise _StoreError("unsafe_path")
                    exists = _regular(path) is not None
                    if not overwrite:
                        if exists:
                            raise _StoreError("already_exists")
                        return None
                    if not exists:
                        raise _StoreError("not_found")
                    raw = self._read_bytes(path)
                    if hashlib.sha256(raw).hexdigest() != expected:
                        raise _StoreError("sha_conflict")
                    return raw

                previous_raw = check_revision()
                previous = self._decode(previous_raw, skill["id"]) if previous_raw is not None else None
                now = datetime.now(timezone.utc)
                if previous is not None:
                    last = datetime.fromisoformat(previous["updated_at"].replace("Z", "+00:00"))
                    now = max(now, last + timedelta(microseconds=1))
                timestamp = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
                skill["created_at"] = previous["created_at"] if previous else timestamp
                skill["updated_at"] = timestamp
                encoded = (json.dumps(skill, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
                if len(encoded) > self.max_bytes:
                    raise _StoreError("record_too_large")
                sha = hashlib.sha256(encoded).hexdigest()
                fd, temporary = tempfile.mkstemp(prefix=f".{skill['id']}.", suffix=".tmp", dir=self.root)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                    check_revision()
                    os.replace(temporary, path)
                finally:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
                # Do not cache any body (including the just-saved one). Search
                # re-reads disk so other instances/processes are immediately visible.
                self._metadata.pop(skill["id"], None)
                return {"ok": True, "status": "updated" if overwrite else "created",
                        "id": skill["id"], "sha256": sha}
        except (_StoreError, OSError, ValueError, UnicodeError, RecursionError, OverflowError) as exc:
            return _error(exc)


__all__ = ["SkillStore"]