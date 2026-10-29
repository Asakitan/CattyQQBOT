"""Frozen source workspaces and journalled compare-before-write publication.

Model tools never execute code. Copies and validation guards are not an OS
sandbox. The caller must stop the bot before release/recovery operations.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any

from .config import DEFAULT_ALLOWED_PATHS, RepairConfig, state_dir
from .storage import FileLock, read_json, reject_links

_SCHEMA = 1
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
_MAX_SNAPSHOT_FILES = 8192
_SUPPORT_DIRS = ("tests", "scripts", "src/catty_self_repair")
_SUPPORT_FILES = ("config.example.json", "pyproject.toml", "setup.py", "CattyQQAI.spec")
_SKIP_DIRS = frozenset({"__pycache__", "data", "assets", "models", "logs", "memory", "memory_users", "memory_groups", "sessions", "backup", "node_modules", "venv", "build", "dist"})
_SECRET_NAMES = frozenset({"secrets.py", "secret.py", "credentials.py", "credential.py", "local_config.py", "config_local.py", "settings_local.py", "tokens.py", "passwords.py"})
_PERSONA_NAMES = frozenset({"catty_core_persona.py", "catty_persona_reminder.py", "character_card.py", "fadianji_style_critic.py", "nsfw_phase.py", "adaptive_evolution_prompt.py", "prompts.py", "system_prompt.py"})
_DEVICE = re.compile(r"^(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])$", re.I)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


class WorkspaceError(ValueError):
    """An unsafe path, stale input, incomplete validation, or corrupt receipt."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _digest(value: Any) -> str:
    return _sha(_json_bytes(value))


def _relative(value: str | Path) -> str:
    text = value.as_posix() if isinstance(value, Path) else value
    if not isinstance(text, str) or not text or len(text) > 400:
        raise WorkspaceError("a nonempty, bounded relative path is required")
    if any(not part or part in {".", ".."} or part.endswith((" ", ".")) or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
           or _DEVICE.fullmatch(part.split(".", 1)[0]) for part in text.split("/")):
        raise WorkspaceError("path traversal, ADS, device names and noncanonical paths are forbidden")
    return text


def _absolute(path: Path) -> Path:
    path = Path(path).absolute()
    reject_links(path)
    if ".." in path.parts:
        raise WorkspaceError("noncanonical absolute path")
    return path


def _child(root: Path, relative: str | Path) -> Path:
    path = root / _relative(relative)
    reject_links(path)
    return path


def _mkdir(path: Path) -> None:
    reject_links(path)
    path.mkdir(parents=True, exist_ok=True)
    reject_links(path)
    if not path.is_dir():
        raise WorkspaceError("expected a real directory")


def _regular(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise WorkspaceError("only single-link regular files are accepted")


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read(path: Path) -> bytes:
    reject_links(path)
    before = path.lstat()
    _regular(before)
    if before.st_size > _MAX_FILE_BYTES:
        raise WorkspaceError("file exceeds the snapshot size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        _regular(opened)
        if _identity(opened) != _identity(before):
            raise WorkspaceError("file changed while being opened")
        data = stream.read(_MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    reject_links(path)
    final = path.lstat()
    _regular(final)
    if len(data) > _MAX_FILE_BYTES or _identity(before) != _identity(after) or _identity(after) != _identity(final):
        raise WorkspaceError("file changed while being read")
    return data


def _hash_or_none(path: Path) -> str | None:
    try:
        return _sha(_read(path))
    except FileNotFoundError:
        return None


def _fsync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _move(source: Path, destination: Path, *, replace: bool = False) -> None:
    """Durable rename. Publication never uses an overwrite-capable rename."""
    reject_links(source)
    reject_links(destination)
    _regular(source.lstat())
    _mkdir(destination.parent)
    if os.name == "nt":
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move.restype = ctypes.c_int
        if not move(str(source), str(destination), 8 | (1 if replace else 0)):
            raise ctypes.WinError(ctypes.get_last_error())
    elif replace:
        os.replace(source, destination)
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise WorkspaceError("atomic no-replace rename is unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    elif sys.platform == "darwin":
        rename = ctypes.CDLL(None, use_errno=True).renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(destination), 4):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    else:
        raise WorkspaceError("atomic no-replace rename is unavailable on this platform")
    _fsync_dir(source.parent)
    if source.parent != destination.parent:
        _fsync_dir(destination.parent)


def _atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    reject_links(path)
    _mkdir(path.parent)
    fd, name = tempfile.mkstemp(prefix=".repair-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode & 0o777)
        reject_links(path)
        _move(temporary, path, replace=True)
    finally:
        if temporary.exists():
            temporary.unlink()


def _save(path: Path, value: Any) -> None:
    _atomic(path, _json_bytes(value))


def _unlink(path: Path) -> None:
    reject_links(path)
    _regular(path.lstat())
    path.unlink()
    _fsync_dir(path.parent)


def _readonly(path: Path) -> None:
    reject_links(path)
    os.chmod(path, stat.S_IMODE(path.stat().st_mode) & ~0o222)


def _under(relative: str, prefix: str) -> bool:
    return relative == prefix or relative.startswith(prefix + "/")


def _source_path(relative: str) -> bool:
    parts = relative.split("/")
    return (relative.endswith(".py") and parts[-1].lower() not in _SECRET_NAMES
            and not any(part.startswith(".") or part.lower() in _SKIP_DIRS for part in parts)
            and any(_under(relative, allowed) for allowed in DEFAULT_ALLOWED_PATHS))


def _editable(relative: str, allowed_paths: tuple[str, ...] | list[str]) -> bool:
    name = relative.rsplit("/", 1)[-1].lower()
    return (_source_path(relative) and any(_under(relative, allowed) for allowed in allowed_paths)
            and not any(part.lower() in {"personas", "characters", "prompts", "tests", "scripts", "catty_self_repair"} for part in relative.split("/"))
            and not name.startswith(("persona", "character", "test_")) and name not in _PERSONA_NAMES)


def _input_allowed(relative: str) -> bool:
    return (_source_path(relative) or relative in _SUPPORT_FILES
            or (relative.endswith(".py") and relative.rsplit("/", 1)[-1].lower() not in _SECRET_NAMES
                and any(_under(relative, prefix) for prefix in _SUPPORT_DIRS)))


def _walk(directory: Path, *, filtered: bool) -> list[Path]:
    reject_links(directory)
    result: list[Path] = []
    if not directory.exists():
        return result
    if not directory.is_dir():
        raise WorkspaceError("snapshot input directory is not a directory")
    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        if filtered and (entry.name.startswith(".") or entry.name.lower() in _SKIP_DIRS):
            continue
        reject_links(entry)
        info = entry.lstat()
        if stat.S_ISDIR(info.st_mode):
            result.extend(_walk(entry, filtered=filtered))
        elif not filtered or (entry.suffix == ".py" and entry.name.lower() not in _SECRET_NAMES):
            _regular(info)
            result.append(entry)
        if len(result) > _MAX_SNAPSHOT_FILES:
            raise WorkspaceError("too many snapshot files")
    return result


def _input_names(root: Path) -> list[str]:
    paths: set[str] = set()
    for relative in (*DEFAULT_ALLOWED_PATHS, *_SUPPORT_DIRS):
        path = _child(root, relative)
        if not path.exists():
            continue
        if path.is_dir():
            for child in _walk(path, filtered=True):
                name = _relative(child.relative_to(root).as_posix())
                if _input_allowed(name):
                    paths.add(name)
        elif _input_allowed(relative):
            paths.add(relative)
    for relative in _SUPPORT_FILES:
        if _child(root, relative).exists():
            paths.add(relative)
    if len(paths) > _MAX_SNAPSHOT_FILES:
        raise WorkspaceError("too many snapshot files")
    return sorted(paths)


def _hashes(root: Path, names: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    total = 0
    aliases: set[str] = set()
    for name in names:
        if name.casefold() in aliases:
            raise WorkspaceError("case-insensitive path collision")
        aliases.add(name.casefold())
        data = _read(_child(root, name))
        total += len(data)
        if total > _MAX_SNAPSHOT_BYTES:
            raise WorkspaceError("snapshot exceeds byte limit")
        result[name] = _sha(data)
    return result


def _live_hashes(root: Path) -> dict[str, str]:
    return _hashes(root, _input_names(root))


def _tree_hashes(root: Path) -> dict[str, str]:
    names = [_relative(path.relative_to(root).as_posix()) for path in _walk(root, filtered=False)]
    return _hashes(root, sorted(names))


def _hash_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value or len(value) > _MAX_SNAPSHOT_FILES:
        raise WorkspaceError("missing or invalid full file manifest")
    aliases: set[str] = set()
    for name, digest in value.items():
        _relative(name)
        if not _input_allowed(name) or not isinstance(digest, str) or not _HEX.fullmatch(digest) or name.casefold() in aliases:
            raise WorkspaceError("invalid file manifest entry")
        aliases.add(name.casefold())
    return value


def _locations(root: Path, run_dir: Path) -> tuple[Path, Path]:
    root, run_dir = _absolute(root), _absolute(run_dir)
    if not root.is_dir():
        raise WorkspaceError("repository root is missing")
    runs = _absolute(state_dir(root) / "runs")
    if run_dir.parent != runs or not _RUN_ID.fullmatch(run_dir.name):
        raise WorkspaceError("run_dir must be data/self_repair/runs/<run_id>")
    return root, run_dir


def _check_config(config: RepairConfig) -> None:
    config.validate()
    if (not config.allowed_paths or not config.test_modules
            or type(config.max_files) is not int or not 1 <= config.max_files <= 20
            or type(config.max_patch_bytes) is not int or config.max_patch_bytes <= 0
            or not math.isfinite(config.test_timeout_seconds) or config.test_timeout_seconds <= 0
            or not math.isfinite(config.validation_timeout_seconds) or config.validation_timeout_seconds <= 0):
        raise WorkspaceError("empty policy or invalid validation limits")
    for name in config.allowed_paths:
        _relative(name)


def _assert_example(data: bytes) -> None:
    """Refuse a template containing real credentials, rather than copying them."""
    value = json.loads(data.decode("utf-8-sig"))
    sensitive = re.compile(r"api.?key|secret|password|cookie|authorization|access.?token|extra_headers", re.I)

    def visit(item: Any, private: bool = False) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, private or bool(sensitive.search(str(key))))
        elif isinstance(item, list):
            for child in item:
                visit(child, private)
        elif private and isinstance(item, str) and item.strip() not in {"", "TOKEN", "HOST", "TARGET", "YOUR_API_KEY", "YOUR_TOKEN", "CHANGE_ME", "ollama"}:
            raise WorkspaceError("config.example.json contains a non-placeholder credential")

    visit(value)


def _patch_size(before: bytes, after: bytes) -> int:
    from difflib import SequenceMatcher
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    return sum(sum(map(len, old[a:b])) + sum(map(len, new[c:d]))
               for tag, a, b, c, d in SequenceMatcher(None, old, new, autojunk=True).get_opcodes() if tag != "equal")


def create_snapshot(root: Path, run_dir: Path, config: RepairConfig) -> "Workspace":
    _check_config(config)
    root, run_dir = _locations(root, run_dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise WorkspaceError("run directory is not empty; reopen it instead")
    names = _input_names(root)
    if not any(_editable(name, config.allowed_paths) for name in names):
        raise WorkspaceError("no allowlisted source files")
    for module in config.test_modules:
        if f"tests/{module}.py" not in names:
            raise WorkspaceError(f"frozen test module is missing: {module}")
    _mkdir(run_dir)
    before: dict[str, str] = {}
    modes: dict[str, int] = {}
    total = 0
    for name in names:
        source = _child(root, name)
        data = _read(source)
        if name == "config.example.json":
            _assert_example(data)
        total += len(data)
        if total > _MAX_SNAPSHOT_BYTES:
            raise WorkspaceError("snapshot exceeds byte limit")
        before[name] = _sha(data)
        modes[name] = stat.S_IMODE(source.stat().st_mode) & 0o777
        for label in ("baseline", "candidate"):
            target = _child(run_dir / label, name)
            _atomic(target, data, mode=0o600)
            if label == "baseline" or not _editable(name, config.allowed_paths):
                _readonly(target)
    if _live_hashes(root) != before:
        raise WorkspaceError("live inputs changed during snapshot creation")
    _save(run_dir / "workspace.json", {
        "schema": _SCHEMA, "root": str(root), "run_id": run_dir.name,
        "config_fingerprint": config.fingerprint(), "allowed_paths": list(config.allowed_paths),
        "test_modules": list(config.test_modules), "before": before, "modes": modes,
        "snapshot_sha256": _digest(before), "validation": None, "release_sha256": None,
    })
    result = Workspace(root, run_dir, config)
    result._checked_images()
    return result


class Workspace:
    def __init__(self, root: Path, run_dir: Path, config: RepairConfig) -> None:
        _check_config(config)
        self.root, self.run_dir = _locations(root, run_dir)
        self.config = config
        self._meta = read_json(_child(self.run_dir, "workspace.json"))
        self._check_metadata()

    @property
    def candidate(self) -> Path:
        return _child(self.run_dir, "candidate")

    @property
    def baseline(self) -> Path:
        return _child(self.run_dir, "baseline")

    def _check_metadata(self) -> None:
        meta = self._meta
        if (meta.get("schema") != _SCHEMA or meta.get("root") != str(self.root) or meta.get("run_id") != self.run_dir.name
                or meta.get("config_fingerprint") != self.config.fingerprint()
                or meta.get("allowed_paths") != list(self.config.allowed_paths) or meta.get("test_modules") != list(self.config.test_modules)):
            raise WorkspaceError("workspace identity or configuration changed")
        before = _hash_map(meta.get("before"))
        if meta.get("snapshot_sha256") != _digest(before):
            raise WorkspaceError("invalid snapshot binding")
        modes = meta.get("modes")
        if not isinstance(modes, dict) or set(modes) != set(before) or any(type(mode) is not int or not 0 <= mode <= 0o777 for mode in modes.values()):
            raise WorkspaceError("invalid original file modes")

    def _refresh(self) -> None:
        self._meta = read_json(_child(self.run_dir, "workspace.json"))
        self._check_metadata()

    def _persist(self) -> None:
        _save(_child(self.run_dir, "workspace.json"), self._meta)

    def _invalidate(self) -> None:
        self._meta["validation"] = None
        self._meta["release_sha256"] = None
        self._persist()

    def _checked_images(self) -> tuple[dict[str, str], dict[str, str]]:
        self._refresh()
        before, after = _tree_hashes(self.baseline), _tree_hashes(self.candidate)
        if before != self._meta["before"]:
            raise WorkspaceError("baseline/frozen test integrity changed")
        if not set(before) <= set(after):
            raise WorkspaceError("deleting snapshot files is forbidden")
        for name, digest in after.items():
            if not _editable(name, self.config.allowed_paths) and before.get(name) != digest:
                raise WorkspaceError(f"immutable or out-of-policy candidate changed: {name}")
        self._limits(before, after)
        return before, after

    def _limits(self, before: dict[str, str], after: dict[str, str], replacement: tuple[str, bytes] | None = None) -> None:
        changed = [name for name in after if before.get(name) != after[name]]
        if len(changed) > self.config.max_files:
            raise WorkspaceError("max_files exceeded")
        size = 0
        for name in changed:
            old = _read(_child(self.baseline, name)) if name in before else b""
            new = replacement[1] if replacement and replacement[0] == name else _read(_child(self.candidate, name))
            size += _patch_size(old, new)
        if size > self.config.max_patch_bytes:
            raise WorkspaceError("max_patch_bytes exceeded (changed line bytes)")

    def _model_path(self, path: str | Path) -> tuple[str, Path]:
        name = _relative(path)
        if not _editable(name, self.config.allowed_paths):
            raise WorkspaceError("model access is restricted to allowlisted non-persona Python sources")
        return name, _child(self.candidate, name)

    def list_files(self) -> list[str]:
        _, after = self._checked_images()
        return sorted(name for name in after if _editable(name, self.config.allowed_paths))

    def read_file(self, path: str | Path, start: int = 1, end: int = 200) -> str:
        _, target = self._model_path(path)
        if type(start) is not int or type(end) is not int or start < 1 or end < start or end - start >= 1000:
            raise WorkspaceError("read range must be 1-based and at most 1000 lines")
        self._checked_images()
        return "".join(_read(target).decode("utf-8-sig").splitlines(keepends=True)[start - 1:end])[:65536]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        if not isinstance(query, str) or not query or len(query) > 512 or type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError("search requires a bounded literal query and limit")
        found: list[dict] = []
        for name in self.list_files():
            for number, line in enumerate(_read(_child(self.candidate, name)).decode("utf-8-sig").splitlines(), 1):
                if query in line:
                    found.append({"path": name, "line": number, "text": line[:1000]})
                    if len(found) == limit:
                        return found
        return found

    def _edit(self, name: str, target: Path, data: bytes, *, new: bool, expected: str | None = None) -> dict:
        before, after = self._checked_images()
        if len(data) > _MAX_FILE_BYTES:
            raise WorkspaceError("source file is too large")
        if new and name in after:
            raise WorkspaceError("write_file only creates new sources")
        if not new and name not in after:
            raise WorkspaceError("replace_text requires an existing source")
        if not new and after[name] != expected:
            raise WorkspaceError("candidate changed since replacement was read")
        if any(other.casefold() == name.casefold() and other != name for other in after):
            raise WorkspaceError("case-insensitive path collision")
        previous = after.get(name)
        after[name] = _sha(data)
        self._limits(before, after, (name, data))
        self._invalidate()
        if _hash_or_none(target) != previous:
            raise WorkspaceError("candidate changed concurrently")
        if new:
            _mkdir(target.parent)
            staged = self.run_dir / "new-source.tmp"
            _atomic(staged, data)
            try:
                _move(staged, target)
            finally:
                if staged.exists():
                    _unlink(staged)
        else:
            _atomic(target, data)
        return {"path": name, "before_sha256": before.get(name), "after_sha256": _sha(data)}

    def replace_text(self, path: str | Path, old: str, new: str) -> dict:
        name, target = self._model_path(path)
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise WorkspaceError("old must be nonempty text and new must be text")
        original = _read(target)
        source = original.decode("utf-8-sig")
        if source.find(old) < 0 or source.find(old) != source.rfind(old):
            raise WorkspaceError("replacement must match exactly once")
        encoded = source.replace(old, new, 1).encode("utf-8")
        if original.startswith(b"\xef\xbb\xbf"):
            encoded = b"\xef\xbb\xbf" + encoded
        return self._edit(name, target, encoded, new=False, expected=_sha(original))

    def write_file(self, path: str | Path, content: str) -> dict:
        name, target = self._model_path(path)
        if not isinstance(content, str):
            raise WorkspaceError("source content must be text")
        return self._edit(name, target, content.encode("utf-8"), new=True)

    def changes(self) -> list[dict]:
        before, after = self._checked_images()
        return [{"path": name, "before_sha256": before.get(name), "after_sha256": after[name]} for name in sorted(after) if before.get(name) != after[name]]

    def validate(self, *, deadline: float | None = None) -> dict:
        from .validation import validate_workspace
        self._refresh()
        self._invalidate()
        report = validate_workspace(self, deadline=deadline)
        data = _json_bytes(report)
        _atomic(_child(self.run_dir, "validation.json"), data)
        if report.get("ok") is True:
            before, after = self._checked_images()
            if report.get("before") != before or report.get("after") != after:
                raise WorkspaceError("candidate changed at the validation boundary")
            self._meta["validation"] = {"report_sha256": _sha(data), "before_sha256": _digest(before), "after_sha256": _digest(after), "config_fingerprint": self.config.fingerprint()}
            self._persist()
        return report

    def prepare_release(self, summary: str) -> Path:
        before, after = self._checked_images()
        seal = self._meta.get("validation")
        if not isinstance(seal, dict):
            raise WorkspaceError("successful validation is required before release")
        data = _read(_child(self.run_dir, "validation.json"))
        report = json.loads(data)
        if (seal.get("report_sha256") != _sha(data) or report.get("ok") is not True or report.get("before") != before or report.get("after") != after
                or seal.get("before_sha256") != _digest(before) or seal.get("after_sha256") != _digest(after)
                or seal.get("config_fingerprint") != self.config.fingerprint()):
            raise WorkspaceError("validation does not bind these complete images")
        changes = self.changes()
        if not changes:
            raise WorkspaceError("no source changes to release")
        if _live_hashes(self.root) != before:
            raise WorkspaceError("live inputs changed since the snapshot")
        path = _child(self.run_dir, "release.json")
        if path.exists():
            if self._meta.get("release_sha256") == _sha(_read(path)):
                return path
            raise WorkspaceError("an immutable release already exists; start a new run")
        for change in changes:
            name = change["path"]
            payload = _read(_child(self.candidate, name))
            if _sha(payload) != after[name]:
                raise WorkspaceError("candidate changed while preparing release")
            target = _child(self.run_dir / "release/payload", name)
            _atomic(target, payload)
            _readonly(target)
        if self._checked_images() != (before, after):
            raise WorkspaceError("workspace changed while preparing release")
        release = {"schema": _SCHEMA, "run_id": self.run_dir.name, "root": str(self.root), "config_fingerprint": self.config.fingerprint(),
                   "summary": str(summary)[:4000], "before": before, "after": after, "changes": changes,
                   "validation_sha256": _sha(data), "snapshot_sha256": self._meta["snapshot_sha256"]}
        encoded = _json_bytes(release)
        _atomic(path, encoded)
        _readonly(path)
        self._meta["release_sha256"] = _sha(encoded)
        self._persist()
        return path


def _active_path(root: Path) -> Path:
    return _child(state_dir(root), "active_release.json")


def _journal_save(root: Path, journal: dict) -> None:
    _save(_active_path(root), journal)


def _journal_load(root: Path) -> dict | None:
    path = _active_path(root)
    if not path.exists():
        return None
    journal = read_json(path)
    run_id = journal.get("run_id")
    if (journal.get("schema") != _SCHEMA or journal.get("root") != str(root) or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)
            or journal.get("phase") not in {"applying", "applied", "rolling_back", "rolled_back", "confirmed"}):
        raise WorkspaceError("invalid active release identity/state; journal preserved")
    before, after = _hash_map(journal.get("before")), _hash_map(journal.get("after"))
    allowed = journal.get("allowed_paths")
    if not isinstance(allowed, list) or not allowed:
        raise WorkspaceError("invalid active release policy")
    for name in allowed:
        _relative(name)
        if not any(_under(name, base) for base in DEFAULT_ALLOWED_PATHS):
            raise WorkspaceError("active release policy exceeds fixed source roots")
    if not set(before) <= set(after):
        raise WorkspaceError("active release contains forbidden deletions")
    changed = sorted(name for name in after if before.get(name) != after[name])
    entries = journal.get("entries")
    if not changed or len(changed) > 20 or not isinstance(entries, list) or len(entries) != len(changed):
        raise WorkspaceError("invalid active release entries")
    for name, entry in zip(changed, entries):
        if (not isinstance(entry, dict) or entry.get("path") != name or not _editable(name, allowed)
                or entry.get("before_sha256") != before.get(name) or entry.get("after_sha256") != after[name]
                or type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o777
                or entry.get("state") not in {"pending", "moving", "installing", "applied", "undoing", "restoring", "restored"}):
            raise WorkspaceError("invalid active release entry; journal preserved")
    _locations(root, state_dir(root) / "runs" / run_id)
    return journal


def _transaction_path(root: Path, journal: dict, kind: str, name: str) -> Path:
    if kind not in {"backup", "staged", "held", "discarded", "restoring"}:
        raise WorkspaceError("invalid transaction artifact")
    return _child(state_dir(root) / "runs" / journal["run_id"] / "transaction" / kind, name)


def _finish(root: Path, journal: dict) -> dict:
    phase = journal["phase"]
    if phase not in {"confirmed", "rolled_back"}:
        raise WorkspaceError("cannot finish a nonterminal transaction")
    run = state_dir(root) / "runs" / journal["run_id"]
    _save(_child(run, "release_receipt.json"), journal)
    _unlink(_active_path(root))
    return {"status": phase, "run_id": journal["run_id"], "reason": journal.get("reason", ""), "receipt": (run / "release_receipt.json").relative_to(root).as_posix()}


def _conflict(root: Path, journal: dict, conflicts: list[dict]) -> dict:
    journal["conflicts"] = conflicts
    _journal_save(root, journal)
    return {"status": "conflict", "run_id": journal["run_id"], "conflicts": conflicts}


def _restore_original(root: Path, journal: dict, entry: dict) -> None:
    name, expected = entry["path"], entry["before_sha256"]
    live = _child(root, name)
    held = _transaction_path(root, journal, "held", name)
    backup = _transaction_path(root, journal, "backup", name)
    if expected is None:
        if _hash_or_none(live) is not None:
            raise WorkspaceError("operator created a file during rollback")
        return
    if _hash_or_none(backup) != expected:
        raise WorkspaceError("rollback backup is missing or changed")
    entry["state"] = "restoring"
    _journal_save(root, journal)
    if _hash_or_none(held) == expected:
        _move(held, live)
    else:
        staged = _transaction_path(root, journal, "restoring", name)
        value = _hash_or_none(staged)
        if value not in {None, expected}:
            raise WorkspaceError("rollback staging file changed")
        if value is None:
            _atomic(staged, _read(backup), mode=entry["mode"])
        _move(staged, live)
    if _hash_or_none(live) != expected:
        raise WorkspaceError("restored file changed concurrently")


def _rollback_entry(root: Path, journal: dict, entry: dict) -> None:
    name = entry["path"]
    before, after = entry["before_sha256"], entry["after_sha256"]
    live = _child(root, name)
    held = _transaction_path(root, journal, "held", name)
    discarded = _transaction_path(root, journal, "discarded", name)
    current, held_hash, discarded_hash = (_hash_or_none(path) for path in (live, held, discarded))
    if held_hash not in {None, before}:
        if current is None and entry["state"] in {"moving", "installing"}:
            _move(held, live)
        raise WorkspaceError("moved original contains an operator edit; preserved")
    if discarded_hash not in {None, after}:
        if current is None and entry["state"] == "undoing":
            _move(discarded, live)
        raise WorkspaceError("moved rollback postimage contains an operator edit; preserved")
    if current == before:
        entry["state"] = "restored"
        _journal_save(root, journal)
        return
    if current is None:
        if entry["state"] == "pending" or (entry["state"] == "applied" and discarded_hash is None) or (held_hash is None and discarded_hash is None):
            raise WorkspaceError("live file was deleted outside a release-owned gap")
        _restore_original(root, journal, entry)
    elif current == after:
        if discarded_hash is not None:
            raise WorkspaceError("rollback destination is occupied; preserved both copies")
        entry["state"] = "undoing"
        _journal_save(root, journal)
        _move(live, discarded)
        if _hash_or_none(discarded) != after:
            if _hash_or_none(live) is None:
                _move(discarded, live)
            raise WorkspaceError("operator edit raced with rollback; preserved")
        _restore_original(root, journal, entry)
    else:
        raise WorkspaceError("live file differs from both release images; operator edit preserved")
    if _hash_or_none(live) != before:
        raise WorkspaceError("live file changed during rollback")
    entry["state"] = "restored"
    _journal_save(root, journal)


def _rollback_locked(root: Path, journal: dict, reason: str) -> dict:
    if journal["phase"] in {"confirmed", "rolled_back"}:
        return _finish(root, journal)
    journal["phase"], journal["reason"], journal["conflicts"] = "rolling_back", str(reason)[:2000], []
    _journal_save(root, journal)
    conflicts: list[dict] = []
    for entry in reversed(journal["entries"]):
        try:
            _rollback_entry(root, journal, entry)
        except (OSError, ValueError) as exc:
            conflicts.append({"path": entry["path"], "reason": str(exc)[:1000]})
    if conflicts:
        return _conflict(root, journal, conflicts)
    for entry in journal["entries"]:
        try:
            if _hash_or_none(_child(root, entry["path"])) != entry["before_sha256"]:
                conflicts.append({"path": entry["path"], "reason": "changed after rollback"})
        except (OSError, ValueError) as exc:
            conflicts.append({"path": entry["path"], "reason": str(exc)[:1000]})
    if conflicts:
        return _conflict(root, journal, conflicts)
    journal["phase"], journal["conflicts"] = "rolled_back", []
    _journal_save(root, journal)
    return _finish(root, journal)


def _release_workspace(root: Path, release_path: Path, config: RepairConfig) -> tuple[Workspace, dict]:
    if not release_path.is_absolute():
        release_path = _child(root, release_path)
    release_path = _absolute(release_path)
    if release_path.name != "release.json":
        raise WorkspaceError("expected the fixed per-run release.json")
    workspace = Workspace(root, release_path.parent, config)
    before, after = workspace._checked_images()
    data = _read(release_path)
    release = json.loads(data)
    seal = workspace._meta.get("validation")
    if (not isinstance(release, dict) or release.get("schema") != _SCHEMA or release.get("root") != str(root)
            or release.get("run_id") != workspace.run_dir.name or release.get("config_fingerprint") != config.fingerprint()
            or workspace._meta.get("release_sha256") != _sha(data) or release.get("before") != before or release.get("after") != after
            or release.get("changes") != workspace.changes() or release.get("snapshot_sha256") != workspace._meta["snapshot_sha256"]
            or not isinstance(seal, dict) or release.get("validation_sha256") != seal.get("report_sha256")):
        raise WorkspaceError("release no longer binds the validated workspace")
    report_data = _read(_child(workspace.run_dir, "validation.json"))
    report = json.loads(report_data)
    if (not isinstance(report, dict) or report.get("ok") is not True or _sha(report_data) != seal.get("report_sha256")
            or report.get("before") != before or report.get("after") != after
            or seal.get("config_fingerprint") != config.fingerprint() or seal.get("before_sha256") != _digest(before) or seal.get("after_sha256") != _digest(after)):
        raise WorkspaceError("release validation evidence changed")
    payload = _tree_hashes(_child(workspace.run_dir, "release/payload"))
    if payload != {row["path"]: row["after_sha256"] for row in release["changes"]}:
        raise WorkspaceError("release payload changed")
    return workspace, release


def apply_release(root: Path, release_path: Path, config: RepairConfig) -> dict:
    """Publish only after the parent has stopped the bot; never start processes."""
    root = _absolute(root)
    _check_config(config)
    with FileLock(_child(state_dir(root), "release.lock")):
        active = _journal_load(root)
        workspace, release = _release_workspace(root, Path(release_path), config)
        if active is not None:
            if (active["phase"] == "applied" and workspace.run_dir.name == active["run_id"]
                    and active.get("config_fingerprint") == config.fingerprint()
                    and active.get("release_sha256") == workspace._meta["release_sha256"] and _live_hashes(root) == active["after"]):
                return {"status": "applied", "run_id": active["run_id"]}
            raise WorkspaceError("an active release must be recovered before applying another")
        receipt = _child(workspace.run_dir, "release_receipt.json")
        if receipt.exists():
            completed = read_json(receipt)
            if (completed.get("phase") in {"confirmed", "rolled_back"} and completed.get("run_id") == workspace.run_dir.name
                    and completed.get("release_sha256") == workspace._meta["release_sha256"]):
                return {"status": completed["phase"], "run_id": workspace.run_dir.name}
            raise WorkspaceError("invalid previous release receipt")
        if not release["changes"]:
            raise WorkspaceError("empty release")
        if _live_hashes(root) != release["before"]:
            raise WorkspaceError("full live preimage changed; no files were published")
        journal = {"schema": _SCHEMA, "root": str(root), "run_id": workspace.run_dir.name,
                   "phase": "applying", "config_fingerprint": config.fingerprint(), "allowed_paths": list(config.allowed_paths),
                   "before": release["before"], "after": release["after"], "release_sha256": workspace._meta["release_sha256"],
                   "entries": [], "reason": "", "conflicts": []}
        if _child(workspace.run_dir, "transaction").exists():
            raise WorkspaceError("unjournalled transaction artifacts exist; create a fresh run")
        for change in release["changes"]:
            name = change["path"]
            entry = {**change, "state": "pending", "mode": workspace._meta["modes"].get(name, 0o644)}
            journal["entries"].append(entry)
            if change["before_sha256"] is not None:
                original = _read(_child(root, name))
                if _sha(original) != change["before_sha256"]:
                    raise WorkspaceError("live preimage changed while preparing backups")
                backup = _transaction_path(root, journal, "backup", name)
                _atomic(backup, original)
                _readonly(backup)
            staged = _transaction_path(root, journal, "staged", name)
            payload = _read(_child(workspace.run_dir / "release/payload", name))
            if _sha(payload) != change["after_sha256"]:
                raise WorkspaceError("release payload changed during staging")
            _atomic(staged, payload, mode=entry["mode"])
            for kind in ("held", "discarded", "restoring"):
                _mkdir(_transaction_path(root, journal, kind, name).parent)
        if _live_hashes(root) != release["before"] or workspace._checked_images() != (release["before"], release["after"]):
            raise WorkspaceError("full release images changed during staging")
        _journal_save(root, journal)
        try:
            for entry in journal["entries"]:
                name = entry["path"]
                live, held, staged = _child(root, name), _transaction_path(root, journal, "held", name), _transaction_path(root, journal, "staged", name)
                if _hash_or_none(live) != entry["before_sha256"]:
                    raise WorkspaceError("operator changed a pending live preimage")
                if _hash_or_none(staged) != entry["after_sha256"]:
                    raise WorkspaceError("staged postimage changed")
                entry["state"] = "moving"
                _journal_save(root, journal)
                if entry["before_sha256"] is not None:
                    _move(live, held)
                    if _hash_or_none(held) != entry["before_sha256"]:
                        raise WorkspaceError("operator changed the file during its move")
                entry["state"] = "installing"
                _journal_save(root, journal)
                _move(staged, live)
                if _hash_or_none(live) != entry["after_sha256"]:
                    raise WorkspaceError("live postimage changed immediately after install")
                entry["state"] = "applied"
                _journal_save(root, journal)
            if (_live_hashes(root) != release["after"] or workspace._checked_images() != (release["before"], release["after"])
                    or config.fingerprint() != journal["config_fingerprint"]):
                raise WorkspaceError("full postimage/configuration changed before completion")
            for entry in journal["entries"]:
                if entry["before_sha256"] is not None and _hash_or_none(_transaction_path(root, journal, "held", entry["path"])) != entry["before_sha256"]:
                    raise WorkspaceError("held original changed before completion")
            journal["phase"] = "applied"
            _journal_save(root, journal)
            return {"status": "applied", "run_id": workspace.run_dir.name}
        except Exception as exc:
            durable = _journal_load(root)
            if durable is None:
                raise WorkspaceError("active journal disappeared during publication") from exc
            return _rollback_locked(root, durable, f"apply failed: {type(exc).__name__}: {str(exc)[:1000]}")


def rollback_release(root: Path, reason: str) -> dict:
    root = _absolute(root)
    with FileLock(_child(state_dir(root), "release.lock")):
        journal = _journal_load(root)
        return {"status": "none"} if journal is None else _rollback_locked(root, journal, reason)


def confirm_release(root: Path) -> dict:
    root = _absolute(root)
    with FileLock(_child(state_dir(root), "release.lock")):
        journal = _journal_load(root)
        if journal is None:
            return {"status": "none"}
        if journal["phase"] in {"confirmed", "rolled_back"}:
            return _finish(root, journal)
        if journal["phase"] != "applied":
            raise WorkspaceError("only a fully applied release may be confirmed")
        conflicts: list[dict] = []
        try:
            current = _live_hashes(root)
            conflicts.extend({"path": name, "reason": "live postimage changed before confirmation"}
                             for name in sorted(current.keys() | journal["after"].keys()) if current.get(name) != journal["after"].get(name))
            for entry in journal["entries"]:
                if entry["before_sha256"] is not None and _hash_or_none(_transaction_path(root, journal, "held", entry["path"])) != entry["before_sha256"]:
                    conflicts.append({"path": entry["path"], "reason": "held original changed"})
        except (OSError, ValueError) as exc:
            conflicts.append({"path": "", "reason": str(exc)[:1000]})
        if conflicts:
            return _conflict(root, journal, conflicts)
        journal["phase"] = "confirmed"
        _journal_save(root, journal)
        return _finish(root, journal)


def recover_release(root: Path) -> dict:
    root = _absolute(root)
    with FileLock(_child(state_dir(root), "release.lock")):
        journal = _journal_load(root)
        if journal is None:
            return {"status": "none"}
        if journal["phase"] in {"confirmed", "rolled_back"}:
            return _finish(root, journal)
        return _rollback_locked(root, journal, "recovering unfinished release")