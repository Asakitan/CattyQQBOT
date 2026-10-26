"""Data-only custom persona definitions; never import the live plugin/registry.

The schema mirrors the serializable Persona fields. Runtime materialization and
activation belong to the registry/caller, not to this persistence layer.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Iterator


MAX_PERSONA_BYTES = 2 * 1024 * 1024
BUILTIN_PERSONA_NAMES = frozenset({"catty", "fadianji"})
BUILTIN_PERSONA_ALIASES = {
    "catty": "catty", "笨猫": "catty", "猫猫": "catty",
    "fadianji": "fadianji", "机机": "fadianji", "小机": "fadianji",
    "发电机": "fadianji", "不稳定发电机": "fadianji",
}
_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)}
)

# Exact command heads from __init__._*_CMD_RE/_COGNITIVE_ADMIN_RE and the
# reset/session/cache/memory/emoji command parsers. This is not a slash ban.
RESERVED_COMMAND_HEADS = frozenset({
    "人格", "catty_status", "status", "笨猫状态", "猫猫状态", "机机状态", "发电机状态",
    "vibe_show", "vibe_reset", "aff_show", "aff_reset_signin", "aff_set_points",
    "aff_add_points", "aff_set_exp", "aff_reset", "aff_force_checkin",
    "lore_show", "lore_list", "lore_remove", "lore_delete", "lore_clear", "lore_summarize",
    "profile_show", "profile_name_set", "profile_name_clear", "profile_fact_add",
    "profile_fact_delete", "profile_fact_clear", "note_list", "note_add", "note_delete",
    "note_clear", "meme_list", "meme_add", "meme_delete", "meme_clear", "timeline_list",
    "timeline_add", "timeline_update", "timeline_done", "timeline_delete", "timeline_clear",
    "adaptive_list", "adaptive_set", "adaptive_delete", "adaptive_clear", "evolve",
    "rollback_evolution", "reset", "clear", "sessions", "clearcache", "memory",
    "saveemoji", "emoji", "会话列表", "会话", "checkin", "签到", "points", "积分",
    "affection", "好感", "level", "lv",
    # owner_forward.py matches these before the persona matcher.
    "list", "pending", "待办", "同意", "拒绝", "approve", "accept", "y", "reject", "deny", "n",
})
_EMOJI_COMMAND_PREFIXES = (
    "收藏表情", "收藏这个表情", "收藏这个", "收藏图", "收藏", "存表情", "存这个表情",
    "存这个", "存图", "加表情", "加表情库", "入库", "/saveemoji", "/emoji",
)
_RESERVED_EXACT_COMMANDS = frozenset({
    "清空", "清空上下文", "重置", "重置上下文", "忘掉上文", "ai会话列表", "ai列会话",
    "ai列出会话", "ai查看会话", "ai看看会话", "ai会话", "ai所有会话", "aisessions",
    "列会话", "查看会话", "看看会话", "清空缓存", "清除缓存", "清理缓存",
    "清空记忆缓存", "清除记忆缓存", "清空当前缓存", "清空当前记忆缓存", "记忆",
    "查看记忆", "看看记忆", "显示记忆", "读取记忆", "查看存储", "查看人物信息",
    "查看群友信息", "查看人物画像", "查看群友画像", "你记得我什么",
})


class PersonaValidationError(ValueError):
    """Safe diagnostic: field identifiers and error codes, never input values."""

    def __init__(self, field: str, code: str = "invalid_value") -> None:
        self.field = field
        self.code = code
        super().__init__(f"{code}: {field}")


def _array(items: dict[str, Any], *, unique: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": items}
    if unique:
        schema["uniqueItems"] = True
        schema["x-set"] = True
    return schema


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


_STRING = {"type": "string"}
_NULL_STRING = {"type": ["string", "null"]}
_BOOL = {"type": "boolean"}
_PAIR = {"type": "array", "items": _STRING, "minItems": 2, "maxItems": 2}
_BOOK_ENTRY_SCHEMA = _object({
    "identifier": _STRING, "keys": _array(_STRING), "content": _STRING,
    "order": {"type": "integer"}, "constant": _BOOL, "case_sensitive": _BOOL,
}, ("identifier", "keys", "content"))
_IMAGEGEN_SCHEMA = _object({
    **{key: _STRING for key in (
        "girl_tags", "ref_path", "ref_nsfw_path", "nsfw_girl_tags", "planner_brief",
        "short_review_style",
    )},
    "ref_path_extra": _array(_STRING),
    "ref_strength": {"type": "number"}, "ref_fidelity": {"type": "number"},
})
_CATALOG_SCHEMA = _object({
    **{key: _array(_STRING) for key in (
        "slow_reply_placeholders", "slow_reply_owner_placeholders",
    )},
    **{key: _STRING for key in (
        "force_reply_instruction", "no_reply_image_fallback", "no_reply_reply_fallback",
        "no_reply_mention_fallback", "no_reply_default_fallback", "api_timeout_reply",
        "api_transport_reply", "image_send_failure_reply", "tool_result_follow_up_instruction",
        "turtle_soup_cooldown_reply", "turtle_soup_rule_line", "api_key_missing_reply",
        "web_search_cooldown_reply", "web_search_failure_instruction", "web_search_disabled_instruction",
        "busy_fallback_reply", "signin_success_fallback", "signin_already_fallback",
        "points_summary_fallback",
    )},
})

PERSONA_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "PersonaDefinition",
    "description": "Flat Persona fields. Encoded UTF-8 JSON is limited to 2 MiB; saving does not activate.",
    "x-max-encoded-bytes": MAX_PERSONA_BYTES,
    **_object({
        "schema_version": {"type": "integer", "const": 1},
        "name": {"type": "string", "pattern": _NAME_PATTERN},
        "char_name": {"type": "string", "minLength": 1},
        "core_persona": {"type": "string", "minLength": 1},
        "aliases": _array({"type": "string", "minLength": 1}, unique=True),
        **{key: _NULL_STRING for key in (
            "group_silence", "first_mes", "mes_example", "style_examples", "disambiguation_examples",
            "persona_reminder_text", "reply_gate_style", "chat_rhythm", "emoji_root",
            "emoji_manifest_path", "emoji_download_dir", "emoji_guidance", "model_override",
            "nsfw_gate_skeleton", "nsfw_style_override", "nsfw_prefill",
        )},
        **{key: _BOOL for key in (
            "owner_concept", "cache_full_character_book", "semantic_harness_enabled", "mention_only_trigger",
            "nsfw_gate_private_only", "nsfw_freeform", "pregnancy_private_only",
        )},
        "emoji_allow_downloads": {"type": ["boolean", "null"]},
        "followup_reply_budget": {"type": ["integer", "null"]},
        "disabled_prompt_segments": _array(_STRING, unique=True),
        "disabled_features": _array(_STRING, unique=True),
        "nsfw_scopes": _array({"type": "string", "enum": ["group", "private"]}, unique=True),
        "nsfw_lexicon": _array(_PAIR),
        "conversation_prompts": _array(_PAIR),
        "trigger_prefixes": {**_array(_STRING), "type": ["array", "null"]},
        "directed_keywords": {**_array(_STRING), "type": ["array", "null"]},
        "character_book": {**_array(_BOOK_ENTRY_SCHEMA), "type": ["array", "null"]},
        "imagegen": {**_IMAGEGEN_SCHEMA, "type": ["object", "null"]},
        "reply_catalog": {**_CATALOG_SCHEMA, "type": ["object", "null"]},
    }, ("schema_version", "name", "char_name", "core_persona")),
}


def _validate(value: Any, schema: dict[str, Any], field: str) -> Any:
    kinds = schema["type"]
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if value is None and "null" in kinds:
        return None
    if "object" in kinds and isinstance(value, dict):
        properties = schema["properties"]
        if any(not isinstance(key, str) or key not in properties for key in value):
            raise PersonaValidationError(field, "unknown_field")
        for key in schema.get("required", ()):
            if key not in value:
                raise PersonaValidationError(f"{field}.{key}", "required")
        return {key: _validate(value[key], properties[key], f"{field}.{key}") for key in sorted(value)}
    if "array" in kinds and (
        isinstance(value, (list, tuple)) or schema.get("x-set") and isinstance(value, (set, frozenset))
    ):
        result = [_validate(item, schema["items"], f"{field}[]") for item in value]
        if len(result) < schema.get("minItems", 0) or len(result) > schema.get("maxItems", len(result)):
            raise PersonaValidationError(field, "item_count")
        if schema.get("uniqueItems"):
            if len(set(result)) != len(result):
                raise PersonaValidationError(field, "duplicate")
            result.sort()
        return result
    if "string" in kinds and isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise PersonaValidationError(field, "empty")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise PersonaValidationError(field, "invalid_name")
    elif "boolean" in kinds and type(value) is bool:
        pass
    elif "integer" in kinds and type(value) is int:
        pass
    elif "number" in kinds and type(value) in (int, float):
        if isinstance(value, float) and not math.isfinite(value):
            raise PersonaValidationError(field, "non_finite")
    else:
        raise PersonaValidationError(field, "type")
    if "const" in schema and value != schema["const"]:
        raise PersonaValidationError(field, "version")
    if "enum" in schema and value not in schema["enum"]:
        raise PersonaValidationError(field, "enum")
    return value


def _encode(value: Any) -> bytes:
    try:
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PersonaValidationError("$", "json_encoding") from None
    if len(encoded) > MAX_PERSONA_BYTES:
        raise PersonaValidationError("$", "too_large")
    return encoded


def _alias_key(value: str) -> str:
    return value.strip().casefold()


def _safe_name(value: Any) -> str:
    if not isinstance(value, str):
        raise PersonaValidationError("$.name", "type")
    name = value.strip().lower()
    if re.fullmatch(_NAME_PATTERN, name) is None or name in _WINDOWS_RESERVED:
        raise PersonaValidationError("$.name", "invalid_name")
    return name


def validate_persona_definition(value: Any) -> dict[str, Any]:
    """Return a deterministic plain-JSON copy, preserving prompt strings verbatim.

Only identifiers/aliases are normalized. Missing optional fields stay missing;
no NSFW policy or content defaults are introduced by this module.
"""
    if not isinstance(value, dict):
        raise PersonaValidationError("$", "type")
    candidate = dict(value)
    if "name" in candidate:
        candidate["name"] = _safe_name(candidate["name"])
    result = _validate(candidate, PERSONA_SCHEMA, "$")
    if not result["char_name"].strip():
        raise PersonaValidationError("$.char_name", "empty")
    if not result["core_persona"].strip():
        raise PersonaValidationError("$.core_persona", "empty")
    aliases = result.get("aliases", [])
    aliases = [alias.strip() for alias in aliases]
    if any(not alias or any(ord(char) < 32 for char in alias) for alias in aliases):
        raise PersonaValidationError("$.aliases", "invalid_alias")
    if len({_alias_key(alias) for alias in aliases}) != len(aliases):
        raise PersonaValidationError("$.aliases", "duplicate")
    if "aliases" in result:
        result["aliases"] = sorted(aliases, key=lambda alias: (_alias_key(alias), alias))
    if any(key in BUILTIN_PERSONA_ALIASES for key in _definition_keys(result)):
        raise PersonaValidationError("$.aliases", "builtin_collision")
    _encode(result)
    return result


def _definition_keys(definition: dict[str, Any]) -> set[str]:
    return {_alias_key(key) for key in (
        definition["name"], definition["char_name"], *definition.get("aliases", []),
    )}


def _check_collisions(definitions: dict[str, dict[str, Any]]) -> None:
    owners = dict(BUILTIN_PERSONA_ALIASES)
    for name in sorted(definitions):
        for key in _definition_keys(definitions[name]):
            if key in owners and owners[key] != name:
                raise PersonaValidationError("$.aliases", "alias_collision")
            owners[key] = name


def normalize_switch_command(command: Any) -> str:
    """Normalize a full command; callers must still match the entire message."""
    if not isinstance(command, str):
        raise PersonaValidationError("command", "type")
    if any(ord(char) < 32 and char != "\t" for char in command):
        raise PersonaValidationError("command", "invalid_command")
    normalized = " ".join(command.split()).casefold()
    if not normalized:
        raise PersonaValidationError("command", "empty")
    return normalized


def _check_command(command: str) -> None:
    head = command.split(" ", 1)[0].lstrip("/")
    if head in RESERVED_COMMAND_HEADS or command.replace(" ", "") in _RESERVED_EXACT_COMMANDS:
        raise PersonaValidationError("command", "command_collision")
    if head in {"#evolve", "#rollback_evolution"}:
        raise PersonaValidationError("command", "command_collision")
    for prefix in _EMOJI_COMMAND_PREFIXES:
        if command.startswith(prefix):
            tail = command[len(prefix):]
            if not tail or tail[0] in " ,，.。:：;；#":
                raise PersonaValidationError("command", "command_collision")


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PersonaValidationError("$", "duplicate_key")
        result[key] = value
    return result


def _bad_constant(_: str) -> None:
    raise PersonaValidationError("$", "non_finite")


def _read_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise PersonaValidationError("path", "symlink")
    with path.open("rb") as handle:
        raw = handle.read(MAX_PERSONA_BYTES + 1)
    if len(raw) > MAX_PERSONA_BYTES:
        raise PersonaValidationError("$", "too_large")
    return raw


def _read_json(path: Path) -> tuple[Any, str]:
    raw = _read_bytes(path)
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_json_pairs, parse_constant=_bad_constant)
    return value, hashlib.sha256(raw).hexdigest()


def _error_receipt(exc: Exception) -> dict[str, Any]:
    code = exc.code if isinstance(exc, PersonaValidationError) else type(exc).__name__
    return {"ok": False, "status": "conflict" if code in {
        "sha_conflict", "already_exists", "alias_collision", "command_collision",
    } else "error", "error": code,
            "field": exc.field if isinstance(exc, PersonaValidationError) else "storage"}


_ROOT_LOCKS: dict[str, Any] = {}
_ROOT_LOCKS_GUARD = threading.Lock()
_READ_ERRORS = (OSError, ValueError, UnicodeError, RecursionError)


@contextmanager
def _writer_lock(root: Path) -> Iterator[None]:
    """Serialize cooperating stores/processes; the OS releases locks on exit."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".write.lock"
    if path.is_symlink():
        raise PersonaValidationError("path", "symlink")
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, encoded: bytes, *, before_replace: Any = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class PersonaDefinitionStore:
    """Thread-safe last-good snapshots plus optimistic disk revision checks."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._directory = self.root / "definitions"
        self._commands_path = self.root / "switch_commands.json"
        with _ROOT_LOCKS_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(os.path.normcase(str(self.root)), threading.RLock())
        self._records: dict[str, dict[str, Any]] = {}
        self._commands: dict[str, str] = {}
        self.reload()

    def _definition_path(self, name: str) -> Path:
        if self._directory.is_symlink():
            raise PersonaValidationError("path", "symlink")
        path = self._directory / f"{_safe_name(name)}.json"
        if path.is_symlink():
            raise PersonaValidationError("path", "symlink")
        return path

    def _snapshot(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        if self._directory.is_symlink():
            raise PersonaValidationError("path", "symlink")
        try:
            paths = sorted((path for path in self._directory.iterdir() if path.suffix.lower() == ".json"),
                           key=lambda path: path.name)
        except FileNotFoundError:
            paths = []
        present = {path.stem for path in paths}
        records = {name: record for name, record in self._records.items() if name in present}
        errors: list[dict[str, Any]] = []
        for path in paths:
            try:
                raw, sha = _read_json(path)
                definition = validate_persona_definition(raw)
                name = definition["name"]
                if path.name != f"{name}.json":
                    raise PersonaValidationError("$.name", "filename_mismatch")
                definitions = {key: record["definition"] for key, record in records.items()}
                definitions[name] = definition
                _check_collisions(definitions)
                records[name] = {"definition": definition, "sha256": sha, "path": str(path)}
            except _READ_ERRORS as exc:
                errors.append(_error_receipt(exc))
        return records, errors

    @staticmethod
    def _resolve_target(name: Any, records: dict[str, dict[str, Any]]) -> str:
        if not isinstance(name, str):
            raise PersonaValidationError("name", "type")
        key = _alias_key(name)
        if key in BUILTIN_PERSONA_ALIASES:
            return BUILTIN_PERSONA_ALIASES[key]
        for canonical, record in records.items():
            if key in _definition_keys(record["definition"]):
                return canonical
        raise PersonaValidationError("name", "unknown_persona")

    def _read_commands(self, records: dict[str, dict[str, Any]]) -> dict[str, str]:
        try:
            raw, _ = _read_json(self._commands_path)
        except FileNotFoundError:
            return {}
        if not isinstance(raw, dict):
            raise PersonaValidationError("switch_commands", "type")
        commands: dict[str, str] = {}
        for command, name in raw.items():
            normalized = normalize_switch_command(command)
            _check_command(normalized)
            if normalized in commands:
                raise PersonaValidationError("command", "duplicate")
            commands[normalized] = self._resolve_target(name, records)
        return dict(sorted(commands.items()))

    def reload(self) -> dict[str, Any]:
        with self._lock:
            try:
                records, errors = self._snapshot()
            except _READ_ERRORS as exc:
                return {**_error_receipt(exc), "errors": [_error_receipt(exc)], "activated": False}
            commands = self._commands
            try:
                commands = self._read_commands(records)
            except _READ_ERRORS as exc:
                errors.append(_error_receipt(exc))
            self._records = records
            self._commands = commands
            return {"ok": not errors, "status": "reloaded" if not errors else "partial",
                    "count": len(records), "errors": errors, "activated": False}

    def definitions(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy({name: record["definition"] for name, record in self._records.items()})

    def get(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                canonical = self._resolve_target(name, self._records)
            except PersonaValidationError:
                return None
            record = self._records.get(canonical)
            return {"name": canonical, **deepcopy(record)} if record else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": name, "char_name": record["definition"]["char_name"],
                     "aliases": deepcopy(record["definition"].get("aliases", [])),
                     "path": record["path"], "sha256": record["sha256"], "builtin": False}
                    for name, record in sorted(self._records.items())]

    def save(self, definition: Any, overwrite: bool = False,
             expected_sha256: str | None = None) -> dict[str, Any]:
        try:
            normalized = validate_persona_definition(definition)
            encoded = _encode(normalized)
            if type(overwrite) is not bool:
                raise PersonaValidationError("overwrite", "type")
            name = normalized["name"]
            with self._lock, _writer_lock(self.root):
                path = self._definition_path(name)

                def check_revision() -> None:
                    self._definition_path(name)
                    exists = path.exists()
                    if not overwrite:
                        if exists or name in self._records:
                            raise PersonaValidationError("$.name", "already_exists")
                        if expected_sha256 is not None:
                            raise PersonaValidationError("expected_sha256", "unexpected_revision")
                    else:
                        previous = self._records.get(name)
                        if not isinstance(expected_sha256, str) or not previous or not exists:
                            raise PersonaValidationError("expected_sha256", "sha_conflict")
                        disk_sha = hashlib.sha256(_read_bytes(path)).hexdigest()
                        if expected_sha256 != previous["sha256"] or expected_sha256 != disk_sha:
                            raise PersonaValidationError("expected_sha256", "sha_conflict")

                check_revision()
                records, _ = self._snapshot()
                definitions = {key: record["definition"] for key, record in records.items()}
                definitions[name] = normalized
                _check_collisions(definitions)
                _atomic_write(path, encoded, before_replace=check_revision)
                sha = hashlib.sha256(encoded).hexdigest()
                records[name] = {"definition": normalized, "sha256": sha, "path": str(path)}
                self._records = records
                return {"ok": True, "status": "saved", "name": name, "path": str(path),
                        "sha256": sha, "activated": False}
        except _READ_ERRORS as exc:
            return {**_error_receipt(exc), "activated": False}

    def bind(self, command: str, name: str, overwrite: bool = False) -> dict[str, Any]:
        try:
            normalized = normalize_switch_command(command)
            _check_command(normalized)
            if type(overwrite) is not bool:
                raise PersonaValidationError("overwrite", "type")
            with self._lock, _writer_lock(self.root):
                records, _ = self._snapshot()
                target = self._resolve_target(name, records)
                commands = self._read_commands(records)
                if normalized in commands and not overwrite:
                    raise PersonaValidationError("command", "command_collision")
                commands[normalized] = target
                _atomic_write(self._commands_path, _encode(commands))
                self._records = records
                self._commands = dict(sorted(commands.items()))
                return {"ok": True, "status": "bound", "command": normalized, "name": target,
                        "activated": False}
        except _READ_ERRORS as exc:
            return {**_error_receipt(exc), "activated": False}

    def unbind(self, command: str) -> dict[str, Any]:
        try:
            normalized = normalize_switch_command(command)
            with self._lock, _writer_lock(self.root):
                records, _ = self._snapshot()
                commands = self._read_commands(records)
                existed = normalized in commands
                if existed:
                    del commands[normalized]
                    _atomic_write(self._commands_path, _encode(commands))
                self._records = records
                self._commands = commands
                return {"ok": True, "status": "unbound", "command": normalized,
                        "removed": existed, "activated": False}
        except _READ_ERRORS as exc:
            return {**_error_receipt(exc), "activated": False}

    def switch_commands(self) -> dict[str, str]:
        with self._lock:
            return dict(self._commands)


__all__ = [
    "BUILTIN_PERSONA_ALIASES", "BUILTIN_PERSONA_NAMES", "MAX_PERSONA_BYTES", "PERSONA_SCHEMA",
    "PersonaDefinitionStore", "PersonaValidationError", "RESERVED_COMMAND_HEADS",
    "normalize_switch_command", "validate_persona_definition",
]