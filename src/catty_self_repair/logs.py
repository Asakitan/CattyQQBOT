"""Bounded, durable log tailing; evidence is data, never executable instructions."""
from __future__ import annotations

import codecs
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from .config import RepairConfig, state_dir
from .storage import atomic_json, read_json, reject_links

_REDACTED = "[REDACTED]"
_LEVELS = {"WARNING", "ERROR", "CRITICAL"}
_TIME = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?"
_HEADER = re.compile(
    rf"^\ufeff?{_TIME}\s*\|\s*(?P<level>[A-Z]+)\s*\|\s*"
    r"(?P<logger>[\w.<>-]+):(?P<function>[^:\s|]+):(?P<line>\d+)\s*-\s?(?P<message>.*)$"
)
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LOCAL_VALUE = re.compile(r"^[ \t]*[│┃┆┊┇┋╎╏└├┌┐┘┬┴┼╭╰─]")
_FRAME = re.compile(r'^\s*(?:>\s*)?File "[^"\r\n]+", line \d+')
_OWN = re.compile(r"(?:^|\.)(?:catty_)?self_repair(?:\.|$)", re.I)
_OWN_FRAME = re.compile(r'File "[^"\r\n]*[/\\]catty_self_repair(?:[/\\]|\.py")', re.I)
_URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.I)
_KEY = r"(?:[a-z][a-z0-9_.-]*[_-])?(?:api[_-]?key|tokens?|cookies?|password|passwd|pwd|secret|authorization|credentials?)"
_VALUE = r'''(?:\[REDACTED\]|"(?:\\.|[^"\\\r\n])*(?:"|$)|'(?:\\.|[^'\\\r\n])*(?:'|$)|[^\s,;}\])'"]+)'''
_ASSIGNMENT = re.compile(rf'''(?P<prefix>(?<![\w.-])["']?{_KEY}["']?[ \t]*[:=][ \t]*)(?P<value>{_VALUE})''', re.I | re.M)
_HEADER_SECRET = re.compile(
    r'''(?P<prefix>(?<![\w.-])["']?(?:set-cookie|cookies?|proxy-authorization|authorization)["']?[ \t]*[:=][ \t]*)'''
    r'''(?P<value>"(?:\\.|[^"\\\r\n])*(?:"|$)|'(?:\\.|[^'\\\r\n])*(?:'|$)|[^\r\n]+)''', re.I | re.M,
)
_BEARER = re.compile(r"\b(Bearer|Basic)[ \t]+[A-Za-z0-9._~+/=:%-]+", re.I)
_BARE_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|oc_sk_[A-Za-z0-9_-]{8,}|pb_live_[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.I)
_MAX_PENDING = 128
_MAX_PENDING_CHARS = 1_048_576
_MAX_HISTORY = 2048
_MAX_STATE_BYTES = 16_777_216
_ANCHOR = 64


class LogStateError(RuntimeError):
    """Unavailable/corrupt state must not silently replay logs into paid requests."""


def _plain(text: str) -> str:
    return _CONTROLS.sub("", _ANSI.sub("", text))


def _without_local_value(line: str, in_values: bool) -> tuple[str | None, bool]:
    if _LOCAL_VALUE.match(line):
        return None, True
    if not line.strip():
        return line, in_values
    if in_values and line[:1].isspace() and not _FRAME.match(line):
        return None, True
    return line, False


def _mask(match: re.Match[str]) -> str:
    value = match["value"]
    if value in {'""', "''"}:
        return match[0]
    # Do not turn harmless Python type annotations into fake hidden credentials.
    if ":" in match["prefix"] and value in {"str", "int", "bool", "None", "Any"}:
        return match[0]
    delimiter = value[0] if value[:1] in {'"', "'"} else ""
    return match["prefix"] + delimiter + _REDACTED + delimiter


def _safe_url(match: re.Match[str]) -> str:
    try:
        parts = urlsplit(match[0])
        authority = parts.netloc.rsplit("@", 1)[-1]
        path = re.sub(r"(/(?:token|api[_-]?key|cookie|bearer|secret|password)/)[^/]+", lambda item: item[1] + _REDACTED, parts.path, flags=re.I)
        path = re.sub(r"/bot\d+:[^/]+", "/bot" + _REDACTED, path, flags=re.I)
        return urlunsplit((parts.scheme, authority, path, _REDACTED if parts.query else "", _REDACTED if parts.fragment else ""))
    except ValueError:
        return "[REDACTED_URL]"


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    text = _plain(text)
    lines: list[str] = []
    locals_seen = False
    for line in text.splitlines(keepends=True):
        line, locals_seen = _without_local_value(line, locals_seen)
        if line is not None:
            lines.append(line)
    text = "".join(lines)
    values: set[str] = set()
    for value in secrets:
        if isinstance(value, str) and value and value != _REDACTED:
            values.update((value, quote(value, safe=""), quote_plus(value, safe="")))
            if "\n" in value or "\r" in value:
                values.update(part for part in value.splitlines() if part)
    for value in sorted(values, key=len, reverse=True):
        text = _REDACTED.join(part.replace(value, _REDACTED) for part in text.split(_REDACTED))
    text = _URL.sub(_safe_url, text)
    text = _HEADER_SECRET.sub(_mask, text)
    text = _BEARER.sub(lambda item: item[1] + " " + _REDACTED, text)
    text = _BARE_TOKEN.sub(_REDACTED, text)
    return _ASSIGNMENT.sub(_mask, text)


def _mask_unfinished(text: str, secrets: tuple[str, ...]) -> str:
    for value in sorted(secrets, key=len, reverse=True):
        if not value or len(value) < 5:
            continue
        pattern = list(value) + [None] + list(text[-(len(value) - 1):])
        lengths = [0] * len(pattern)
        for index in range(1, len(pattern)):
            size = lengths[index - 1]
            while size and pattern[index] != pattern[size]:
                size = lengths[size - 1]
            if pattern[index] == pattern[size]:
                size += 1
            lengths[index] = size
        if lengths[-1] >= 4:
            text = text[:-lengths[-1]] + _REDACTED
    return text


def _clip(text: str, limit: int) -> str:
    marker = "\n[log evidence truncated]"
    return text if len(text) <= limit else text[:max(0, limit - len(marker))] + marker[:limit]


def _fingerprint(text: str) -> str:
    text = re.sub(_TIME, "<time>", text)
    text = re.sub(r"(:[^:\s|]+):\d+(?=\s+-)", r"\1:<line>", text)
    text = re.sub(r'(File "[^"\n]+", line )\d+', r"\1<line>", text)
    text = re.sub(r"\b(pid|process_id|thread_id|request_id|trace_id|run_id)([=: ]+)\S+", r"\1\2<id>", text, flags=re.I)
    text = re.sub(r"(?<= at )0x[0-9a-fA-F]{6,}\b", "<address>", text)
    text = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "<uuid>", text)
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _relative(source: str) -> tuple[str, ...]:
    parts = source.split("/")
    if len(parts) < 2 or parts[0] != "logs" or any(
        not item or item in {".", ".."} or item.endswith((" ", "."))
        or re.search(r'[<>:"|?*\\\x00-\x1f]', item) or _DEVICE.fullmatch(item) for item in parts
    ):
        raise ValueError("log sources must be ordinary root-relative files under logs/")
    return tuple(parts)


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


class LogMonitor:
    """The caller holds worker.lock. Emission is committed before returning."""

    def __init__(self, root: Path, config: RepairConfig, secrets: tuple[str, ...] = ()) -> None:
        reject_links(root)
        self.root = Path(root).absolute().resolve()
        config.validate()
        if not 0 < len(config.log_paths) <= 64:
            raise ValueError("log source count must be 1..64")
        for source in config.log_paths:
            _relative(source)
        self.config, self.secrets = config, tuple(secrets)
        self.path = state_dir(self.root) / "log_state.json"
        self.bytes_read = 0
        self.diagnostics: list[dict[str, str]] = []
        self._state: dict[str, Any] | None = self._load()

    def _note(self, code: str, source: str = "") -> None:
        if len(self.diagnostics) < 64:
            self.diagnostics.append({"code": code, "source": source})

    def _load(self) -> dict[str, Any]:
        try:
            reject_links(self.path)
            if not self.path.exists():
                return {"version": 1, "files": {}, "pending": {}, "emitted": {}, "last_now": 0.0, "next_path": 0}
            if self.path.stat().st_size > _MAX_STATE_BYTES:
                raise ValueError("oversize state")
            state = read_json(self.path)
            self._validate(state)
            for item in list(state["pending"].values()) + [item["record"] for item in state["files"].values() if item["record"]]:
                item["text"] = _clip(redact(item["text"], self.secrets), self.config.max_incident_chars)
            return state
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise LogStateError("log state is invalid or unavailable; repair is paused") from exc

    @staticmethod
    def _validate(state: dict[str, Any]) -> None:
        if set(state) != {"version", "files", "pending", "emitted", "last_now", "next_path"} or type(state["version"]) is not int or state["version"] != 1:
            raise ValueError("invalid state schema")
        if not _number(state["last_now"]) or type(state["next_path"]) is not int or state["next_path"] < 0:
            raise ValueError("invalid state clock")
        for name, bound in (("files", 64), ("pending", _MAX_PENDING), ("emitted", _MAX_HISTORY)):
            if not isinstance(state[name], dict) or len(state[name]) > bound:
                raise ValueError("invalid state collection")
        total = 0
        for source, cursor in state["files"].items():
            _relative(source)
            if set(cursor) != {"identity", "offset", "anchor", "anchor_size", "skip_line", "record", "fragment_hash", "fragment_since"}:
                raise ValueError("invalid cursor fields")
            if not isinstance(cursor["identity"], list) or len(cursor["identity"]) != 2 or any(type(x) is not int or x < 0 for x in cursor["identity"]):
                raise ValueError("invalid file identity")
            if type(cursor["offset"]) is not int or cursor["offset"] < 0 or type(cursor["anchor_size"]) is not int or not 0 <= cursor["anchor_size"] <= min(64, cursor["offset"]):
                raise ValueError("invalid cursor offset")
            if type(cursor["skip_line"]) is not bool or not _number(cursor["fragment_since"]):
                raise ValueError("invalid cursor flags")
            for key in ("anchor", "fragment_hash"):
                if not isinstance(cursor[key], str) or (cursor[key] and not re.fullmatch(r"[0-9a-f]{64}", cursor[key])):
                    raise ValueError("invalid cursor digest")
            if bool(cursor["anchor_size"]) != bool(cursor["anchor"]):
                raise ValueError("invalid anchor binding")
            if cursor["record"] is not None:
                LogMonitor._record_valid(cursor["record"])
        for key, item in state["pending"].items():
            if not re.fullmatch(r"[0-9a-f]{64}", key) or item.get("id") != key:
                raise ValueError("invalid incident identity")
            LogMonitor._record_valid(item)
            _relative(item["source"])
            if type(item["occurrences"]) is not int or item["occurrences"] < 1:
                raise ValueError("invalid incident count")
            total += len(item["text"])
        if total > _MAX_PENDING_CHARS:
            raise ValueError("oversize pending evidence")
        for key, emitted in state["emitted"].items():
            if not re.fullmatch(r"[0-9a-f]{64}", key) or not _number(emitted):
                raise ValueError("invalid emission record")

    @staticmethod
    def _record_valid(item: Any) -> None:
        if not isinstance(item, dict) or item.get("level") not in _LEVELS or not isinstance(item.get("text"), str) or len(item["text"]) > 65536:
            raise ValueError("invalid incident")
        if not _number(item.get("first_seen")) or not _number(item.get("last_seen")) or item["last_seen"] < item["first_seen"]:
            raise ValueError("invalid incident clock")

    @staticmethod
    def _cursor(info: os.stat_result, offset: int = 0, skip: bool = False) -> dict[str, Any]:
        return {"identity": [info.st_dev, info.st_ino], "offset": offset, "anchor": "", "anchor_size": 0,
                "skip_line": skip, "record": None, "fragment_hash": "", "fragment_since": 0.0}

    def _finish(self, source: str, cursor: dict[str, Any]) -> bool:
        item = cursor["record"]
        if item is None:
            return True
        pending = self._state["pending"]
        key = _fingerprint(item["text"])
        if key in pending:
            old = pending[key]
            old["occurrences"] = min(2**63 - 1, old["occurrences"] + 1)
            old["last_seen"] = max(old["last_seen"], item["last_seen"])
        else:
            if len(pending) >= _MAX_PENDING or sum(len(x["text"]) for x in pending.values()) + len(item["text"]) > _MAX_PENDING_CHARS:
                self._note("pending_backpressure", source)
                return False
            pending[key] = {key_name: item[key_name] for key_name in ("level", "text", "first_seen", "last_seen")}
            pending[key].update(id=key, source=source, occurrences=1)
        cursor["record"] = None
        return True

    def _line(self, source: str, cursor: dict[str, Any], line: str, now: float) -> bool:
        line = _plain(line.rstrip("\r\n"))
        header = _HEADER.fullmatch(line)
        if header:
            if not self._finish(source, cursor):
                return False
            if _OWN.search(header["logger"]) or Path(source).stem.lower().startswith(("self_repair", "catty_self_repair")):
                self._note("repair_feedback_ignored", source)
                return True
            if header["level"] in _LEVELS:
                text = redact(f'{header["level"]} | {header["logger"]}:{header["function"]}:{header["line"]} - {header["message"]}', self.secrets)
                cursor["record"] = {"level": header["level"], "text": _clip(text, self.config.max_incident_chars),
                                    "first_seen": now, "last_seen": now, "locals": False}
            return True
        item = cursor["record"]
        if item is not None:
            if _OWN_FRAME.search(line):
                cursor["record"] = None
                self._note("repair_feedback_ignored", source)
                return True
            item["last_seen"] = now
            line, item["locals"] = _without_local_value(line, bool(item.get("locals")))
            if line is not None:
                item["text"] = _clip(item["text"] + "\n" + redact(line, self.secrets), self.config.max_incident_chars)
        return True

    def _read(self, stream, count: int) -> bytes:
        count = max(0, min(count, self.config.max_log_bytes - self.bytes_read))
        data = stream.read(count)
        self.bytes_read += len(data)
        return data

    def _tail(self, source: str, now: float) -> None:
        path = self.root.joinpath(*_relative(source))
        reject_links(path)
        if not path.resolve().is_relative_to(self.root / "logs"):
            raise ValueError("log source escaped logs/")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("log source must be a single-link regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            reject_links(path)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("log changed while being opened")
            cursor = self._state["files"].get(source)
            if cursor is None:
                offset = max(0, opened.st_size - self.config.initial_tail_bytes)
                skip = False
                if offset and self.config.initial_tail_bytes:
                    stream.seek(offset - 1)
                    skip = self._read(stream, 1) != b"\n"
                cursor = self._cursor(opened, offset, skip)
            elif cursor["identity"] != [opened.st_dev, opened.st_ino] or opened.st_size < cursor["offset"]:
                if not self._finish(source, cursor):
                    return
                cursor = self._cursor(opened)
            elif cursor["anchor_size"]:
                stream.seek(cursor["offset"] - cursor["anchor_size"])
                anchor = self._read(stream, cursor["anchor_size"])
                if len(anchor) < cursor["anchor_size"]:
                    return
                if hashlib.sha256(anchor).hexdigest() != cursor["anchor"]:
                    if not self._finish(source, cursor):
                        return
                    cursor = self._cursor(opened)
            self._state["files"][source] = cursor
            start = cursor["offset"]
            stream.seek(start)
            # Keep enough read budget to bind the new checkpoint to preceding bytes.
            raw = self._read(stream, max(0, self.config.max_log_bytes - self.bytes_read - _ANCHOR))
            consumed = 0
            line_limit = min(4 * self.config.max_incident_chars + 1024, max(1, self.config.max_log_bytes - 3 * _ANCHOR - 1))
            while consumed < len(raw):
                newline = raw.find(b"\n", consumed)
                if newline < 0:
                    break
                chunk = raw[consumed:newline + 1]
                if cursor["skip_line"]:
                    cursor["skip_line"] = False
                elif len(chunk) > line_limit:
                    header = _HEADER.fullmatch(_plain(chunk[:1024].decode("utf-8", errors="replace")))
                    line = f'2000-01-01 00:00:00 | {header["level"]} | {header["logger"]}:{header["function"]}:0 - [oversized physical log line omitted]' if header else "[oversized physical log line omitted]"
                    if not self._line(source, cursor, line, now):
                        break
                elif not self._line(source, cursor, chunk.decode("utf-8", errors="replace"), now):
                    break
                consumed = newline + 1
                cursor["fragment_hash"] = ""
                cursor["fragment_since"] = 0.0
            tail = raw[consumed:]
            at_end = start + len(raw) >= opened.st_size
            # A buffer ending before the file's LF is not a completed physical line.
            if b"\n" not in tail and tail:
                if cursor["skip_line"]:
                    consumed = len(raw)
                elif len(tail) > line_limit:
                    header = _HEADER.fullmatch(_plain(tail[:1024].decode("utf-8", errors="replace")))
                    line = f'2000-01-01 00:00:00 | {header["level"]} | {header["logger"]}:{header["function"]}:0 - [oversized physical log line omitted]' if header else "[oversized physical log line omitted]"
                    if self._line(source, cursor, line, now):
                        consumed = len(raw)
                        cursor["skip_line"] = True
                elif at_end:
                    digest = hashlib.sha256(tail).hexdigest()
                    if digest != cursor["fragment_hash"]:
                        cursor["fragment_hash"], cursor["fragment_since"] = digest, now
                    elif now - cursor["fragment_since"] >= self.config.debounce_seconds:
                        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                        line = decoder.decode(tail, final=False)
                        if not decoder.getstate()[0] and self._line(source, cursor, _mask_unfinished(line, self.secrets), now):
                            consumed = len(raw)
                            cursor["skip_line"] = True
            cursor["offset"] = start + consumed
            if consumed or not cursor["anchor_size"]:
                size = min(_ANCHOR, cursor["offset"])
                stream.seek(cursor["offset"] - size)
                anchor = self._read(stream, size)
                cursor["anchor_size"] = len(anchor)
                cursor["anchor"] = hashlib.sha256(anchor).hexdigest() if anchor else ""
            item = cursor["record"]
            if item and at_end and cursor["offset"] >= opened.st_size and now - item["last_seen"] >= self.config.debounce_seconds:
                self._finish(source, cursor)

    def poll(self, *, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        if not _number(now):
            raise ValueError("poll time must be finite and nonnegative")
        if self._state is None:
            self._state = self._load()
        now = max(float(now), self._state["last_now"])
        self._state["last_now"] = now
        self.bytes_read, self.diagnostics = 0, []
        sources = self.config.log_paths
        self._state["files"] = {key: value for key, value in self._state["files"].items() if key in sources}
        start = self._state["next_path"] % len(sources)
        for offset in range(len(sources)):
            source = sources[(start + offset) % len(sources)]
            if self.config.max_log_bytes - self.bytes_read <= 2 * _ANCHOR:
                break
            try:
                self._tail(source, now)
            except FileNotFoundError:
                self._note("source_missing", source)
            except (ValueError, OSError):
                self._note("source_unavailable", source)
        self._state["next_path"] = (start + 1) % len(sources)
        emitted = self._state["emitted"]
        for key, at in list(emitted.items()):
            if now - at >= self.config.cooldown_seconds:
                del emitted[key]
        selected = []
        pending = self._state["pending"]
        for key, item in sorted(pending.items(), key=lambda pair: (pair[1]["first_seen"], pair[0])):
            if len(selected) >= self.config.max_incidents_per_run:
                break
            if now - item["first_seen"] < self.config.debounce_seconds or key in emitted:
                continue
            if len(emitted) >= _MAX_HISTORY:
                self._note("cooldown_history_backpressure")
                break
            selected.append(dict(item))
            emitted[key] = now
        for item in selected:
            del pending[item["id"]]
        try:
            self._validate(self._state)
            atomic_json(self.path, self._state)
        except (OSError, ValueError, TypeError) as exc:
            self._state = None
            raise LogStateError("log checkpoint was not committed; no incidents handed off") from exc
        return selected