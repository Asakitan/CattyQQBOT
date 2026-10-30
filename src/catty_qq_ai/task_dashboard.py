from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass
import hashlib
import importlib.metadata
import inspect
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .task_contracts import TaskError, TaskPolicy


REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:api_?key|secret|password|passwd|pwd|authorization|cookie|"
    r"credential|credentials|private_?key|access_?token|refresh_?token|token|env|environment)(?:_|$)",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(
    r"(?:^|_)(?:messages?|chat|history|prompt|query|content|raw|body|text|traceback|environment|env)(?:_|$)", re.IGNORECASE,
)
_TOKEN_TEXT = re.compile(
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=:-]+|"
    r"\b(?:sk-|oc_sk_|pb_live_|ghp_|github_pat_)[A-Za-z0-9_-]+|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)", re.IGNORECASE,
)
_INLINE_CREDENTIAL = re.compile(
    r"(?:\b(?:Bearer|Basic)\s+|--(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)\s+|"
    r"(?:--)?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)\s*[:=]\s*)"
    r"[\"']?([^\s\"'&,;]+)", re.IGNORECASE,
)
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_NO_CACHE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def _key_name(value: Any) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value)).replace("-", "_")


def _secret_values(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(_key_name(key)):
                if isinstance(child, str) and child:
                    found.add(child)
                elif isinstance(child, (list, tuple, dict)):
                    found.update(_all_strings(child))
            found.update(_secret_values(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_secret_values(child))
    elif isinstance(value, str):
        found.update(match.group(1) for match in _INLINE_CREDENTIAL.finditer(value))
    return found


def _all_strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, Mapping):
        return set().union(*(_all_strings(child) for child in value.values()), set())
    if isinstance(value, (list, tuple)):
        return set().union(*(_all_strings(child) for child in value), set())
    return set()


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = tuple(sorted({item for item in secrets if isinstance(item, str) and item}, key=len, reverse=True))

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, REDACTED)
        return _TOKEN_TEXT.sub(REDACTED, value)[:4096]

    def redact(self, value: Any, *, private: bool = True, depth: int = 0) -> Any:
        if depth > 16:
            return "[TRUNCATED]"
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, Mapping):
            result = {}
            for key, child in list(value.items())[:256]:
                name = str(key)
                normalized = _key_name(name)
                if _SECRET_KEY.search(normalized) or (private and _PRIVATE_KEY.search(normalized)):
                    result[self.text(name)] = REDACTED
                else:
                    result[self.text(name)] = self.redact(child, private=private, depth=depth + 1)
            return result
        if isinstance(value, (list, tuple, set)):
            return [self.redact(child, private=private, depth=depth + 1) for child in list(value)[:500]]
        if isinstance(value, str):
            return self.text(value)
        if value is None or type(value) in (bool, int):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return None


class ConfigError(ValueError):
    def __init__(self, code: str, status_code: int = 422):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _json_object(raw: bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("invalid JSON number")

    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConfigError("invalid_json") from exc
    if not isinstance(result, dict):
        raise ConfigError("object_required")
    return result


class ConfigEditor:
    def __init__(self, config_path, validator=None):
        self.path = Path(config_path).resolve()
        self.validator = validator
        self.backup_dir = self.path.parent / (self.path.name + ".task-backups")
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.path)), threading.RLock())

    def _load(self):
        with self.path.open("rb") as source:
            raw = source.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ConfigError("config_too_large")
        return raw, _json_object(raw), hashlib.sha256(raw).hexdigest()

    def secrets(self):
        with self._lock:
            return _secret_values(self._load()[1])

    def read(self):
        with self._lock:
            _, config, digest = self._load()
            return {
                "config": Redactor(_secret_values(config)).redact(config, private=False),
                "sha256": digest,
                "backups": [path.name for path in self._backups()],
            }

    def _backups(self):
        if not self.backup_dir.exists():
            return []
        return sorted((path for path in self.backup_dir.glob("*.json") if path.is_file() and not path.is_symlink()), key=lambda path: path.name, reverse=True)

    def _validate(self, value):
        if not isinstance(value, dict):
            raise ConfigError("agent_tasks_object_required")
        policy_fields = {field.name for field in fields(TaskPolicy)}
        if set(value) - policy_fields - {"mcp", "knowledge", "extensions"}:
            raise ConfigError("unknown_agent_tasks_field")
        try:
            json.dumps(value, allow_nan=False)
            TaskPolicy.from_mapping({key: child for key, child in value.items() if key in policy_fields})
            _validate_children(value)
            if self.validator is not None:
                if hasattr(self.validator, "model_validate"):
                    self.validator.model_validate(value, strict=True)
                elif self.validator(value) is False:
                    raise ValueError("validator rejected configuration")
        except Exception as exc:
            raise ConfigError("invalid_agent_tasks") from exc

    @staticmethod
    def _merge(original, patch):
        merged = dict(original)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = ConfigEditor._merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _check_cas(self, expected_sha256, actual):
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ConfigError("expected_sha256_required", 428)
        if expected_sha256 != actual:
            raise ConfigError("revision_conflict", 409)

    def _atomic(self, path: Path, raw: bytes):
        descriptor, filename = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            if path.exists():
                os.chmod(filename, path.stat().st_mode)
            os.replace(filename, path)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)

    def _commit(self, raw, config, digest):
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = self.backup_dir / (f"{time.time_ns():020d}-{uuid.uuid4().hex}.json")
        self._atomic(backup, raw)
        candidate = (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if self._load()[2] != digest:
            backup.unlink(missing_ok=True)
            raise ConfigError("revision_conflict", 409)
        self._atomic(self.path, candidate)
        for old in self._backups()[10:]:
            old.unlink()
        return self.read()

    def update(self, patch, expected_sha256):
        if not isinstance(patch, dict) or set(patch) != {"agent_tasks"} or not isinstance(patch["agent_tasks"], dict):
            raise ConfigError("only_agent_tasks_editable")
        with self._lock:
            raw, config, digest = self._load()
            self._check_cas(expected_sha256, digest)
            current = config.get("agent_tasks", {})
            if not isinstance(current, dict):
                raise ConfigError("agent_tasks_object_required")
            changes = _restore_secret_markers(current, patch["agent_tasks"], Redactor(_secret_values(config)))
            candidate = _preserve_secrets(current, self._merge(current, changes))
            if not _secret_values(current).issubset(_secret_values(candidate)):
                raise ConfigError("credentials_not_editable")
            self._validate(candidate)
            config["agent_tasks"] = candidate
            return self._commit(raw, config, digest)

    def rollback(self, backup_id, expected_sha256):
        if not isinstance(backup_id, str) or not re.fullmatch(r"\d{20}-[0-9a-f]{32}\.json", backup_id):
            raise ConfigError("invalid_backup")
        with self._lock:
            raw, config, digest = self._load()
            self._check_cas(expected_sha256, digest)
            available = {path.name: path for path in self._backups()}
            if backup_id not in available:
                raise ConfigError("backup_not_found", 404)
            with available[backup_id].open("rb") as source:
                saved = source.read(_MAX_CONFIG_BYTES + 1)
            if len(saved) > _MAX_CONFIG_BYTES:
                raise ConfigError("config_too_large")
            current = config.get("agent_tasks", {})
            candidate = _preserve_secrets(current, _json_object(saved).get("agent_tasks", {}))
            if not _secret_values(current).issubset(_secret_values(candidate)):
                raise ConfigError("credentials_not_editable")
            self._validate(candidate)
            config["agent_tasks"] = candidate
            return self._commit(raw, config, digest)


def _preserve_secrets(current, candidate):
    if isinstance(current, list) and isinstance(candidate, list):
        previous = {item.get("id"): item for item in current if isinstance(item, dict) and "id" in item}
        return [_preserve_secrets(previous.get(item.get("id"), {}), item) if isinstance(item, dict) else item for item in candidate]
    if not isinstance(current, Mapping) or not isinstance(candidate, Mapping):
        return candidate
    result = dict(candidate)
    for key, value in current.items():
        if _SECRET_KEY.search(_key_name(key)):
            result[key] = value
        elif isinstance(value, (Mapping, list)) and _has_secret_key(value):
            result[key] = _preserve_secrets(value, result.get(key, {}))
    return result


def _restore_secret_markers(current, patch, redactor):
    if isinstance(current, str) and isinstance(patch, str) and patch == redactor.text(current):
        return current
    if isinstance(patch, list):
        previous = {item.get("id"): item for item in current if isinstance(item, dict) and "id" in item} if isinstance(current, list) else {}
        restored = []
        for index, item in enumerate(patch):
            earlier = previous.get(item.get("id"), {}) if isinstance(item, dict) else current[index] if isinstance(current, list) and index < len(current) else None
            restored.append(_restore_secret_markers(earlier, item, redactor))
        return restored
    if not isinstance(patch, Mapping):
        return patch
    previous = current if isinstance(current, Mapping) else {}
    result = {}
    for key, value in patch.items():
        if _SECRET_KEY.search(_key_name(key)):
            if key not in previous or value not in (REDACTED, previous[key]):
                raise ConfigError("credentials_not_editable")
            result[key] = previous[key]
        else:
            result[key] = _restore_secret_markers(previous.get(key), value, redactor)
    return result


def _validate_children(value):
    if "mcp" in value:
        from .mcp_runtime import _Server

        settings = value["mcp"]
        if not isinstance(settings, dict) or set(settings) - {"enabled", "servers"}:
            raise ValueError("invalid MCP settings")
        if type(settings.get("enabled", False)) is not bool:
            raise ValueError("invalid MCP enabled flag")
        servers = settings.get("servers", [])
        if not isinstance(servers, list) or len(servers) > 64:
            raise ValueError("invalid MCP servers")
        parsed = [_Server.parse(server) for server in servers]
        if len({server.id for server in parsed}) != len(parsed):
            raise ValueError("duplicate MCP server")
    if "knowledge" in value:
        from .knowledge_base import KnowledgeBaseSettings

        settings = value["knowledge"]
        allowed = {field.name for field in fields(KnowledgeBaseSettings)} | {"enabled", "max_file_bytes"}
        if not isinstance(settings, dict) or set(settings) - allowed or type(settings.get("enabled", True)) is not bool:
            raise ValueError("invalid knowledge settings")
        checked = {key: child for key, child in settings.items() if key != "enabled"}
        if "max_file_bytes" in checked:
            maximum = checked.pop("max_file_bytes")
            if "max_upload_bytes" in checked and checked["max_upload_bytes"] != maximum:
                raise ValueError("conflicting upload limits")
            checked["max_upload_bytes"] = maximum
        KnowledgeBaseSettings.from_value(checked)
    if "extensions" in value:
        from .extension_runtime import _identifier as extension_identifier

        settings = value["extensions"]
        if not isinstance(settings, dict) or set(settings) - {"enabled_ids", "root"}:
            raise ValueError("invalid extension settings")
        enabled = settings.get("enabled_ids", [])
        if not isinstance(enabled, list) or len(enabled) > 256:
            raise ValueError("invalid enabled extensions")
        for item in enabled:
            extension_identifier(item)
        if len(set(enabled)) != len(enabled):
            raise ValueError("duplicate enabled extension")
        if "root" in settings:
            root = settings["root"]
            if not isinstance(root, str) or not root.strip() or len(root) > 4096 or any(ord(char) < 32 for char in root):
                raise ValueError("invalid extension root")


def _has_secret_key(value):
    if isinstance(value, Mapping):
        return any(_SECRET_KEY.search(_key_name(key)) or _has_secret_key(child) for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_secret_key(child) for child in value)
    return False


_TASK_FIELDS = frozenset({
    "title", "points", "max_tokens", "payment_state", "summary", "artifacts", "artifact_id", "name", "size", "sha256", "mime_type", "sources", "source_id", "url",
    "id", "task_id", "state", "status", "tier", "role", "scope", "persona", "user_id", "owner_id", "revision",
    "created_at", "updated_at", "started_at", "completed_at", "expires_at", "progress", "attempt", "attempts",
    "max_attempts", "total_tokens", "input_tokens", "output_tokens", "charged_points", "reserved_points",
    "billed_points", "error_code", "delivery_state", "delivery_attempts", "cancel_requested", "can_resume",
    "requires_confirmation", "delivered", "action", "accepted", "count",
})
_SCHEDULE_FIELDS = frozenset({
    "state", "next_run", "title", "last_task_id", "last_error", "persona",
    "id", "schedule_id", "task_id", "name", "status", "enabled", "next_run_at", "last_run_at", "revision",
    "interval_seconds", "cron", "timezone", "owner_id", "user_id", "scope", "action", "accepted",
})
_KNOWLEDGE_FIELDS = frozenset({
    "doc_id", "title", "version", "semantic_pending", "semantic_error", "citation", "page_end", "ordinal", "user_id", "scope", "persona",
    "id", "document_id", "chunk_id", "filename", "name", "status", "mime", "size", "sha256", "chunks",
    "chunk_count", "updated_at", "created_at", "revision", "score", "source", "format", "page", "excerpt",
    "snippet", "deleted", "accepted", "count", "rebuilding",
})
_EXTENSION_FIELDS = frozenset({
    "state",
    "id", "extension_id", "name", "status", "enabled", "version", "revision", "description", "capabilities",
    "tool_count", "error_code", "accepted",
})
_MCP_FIELDS = frozenset({
    "server", "tool", "allow_guests",
    "id", "server_id", "name", "status", "enabled", "revision", "transport", "tool_count", "connected",
    "error_code", "last_seen", "description", "input_schema", "inputSchema", "tools",
})
_TRACE_FIELDS = frozenset({
    "trace_id", "parent_id", "scope", "user_id", "persona", "tool", "model", "input_tokens", "output_tokens", "cached_tokens", "error_type", "data", "kind", "created_at", "event_id",
    "id", "seq", "event", "timestamp", "time", "task_id", "stage", "status", "attempt", "level",
    "duration_ms", "latency_ms", "total_tokens", "error_code", "code", "revision",
})


def _member(services, name):
    return services.get(name) if isinstance(services, Mapping) else getattr(services, name, None)


async def _await(value):
    return await value if inspect.isawaitable(value) else value


async def _call(services, method, **kwargs):
    function = _member(services, method)
    if not callable(function):
        raise HTTPException(503, detail={"code": "service_unavailable", "operation": method})
    try:
        if inspect.iscoroutinefunction(function):
            result = await function(**kwargs)
        else:
            result = await _await(await asyncio.to_thread(function, **kwargs))
        if is_dataclass(result) and not isinstance(result, type):
            result = asdict(result)
        if isinstance(result, (list, tuple)):
            result = {"items": list(result)}
        if not isinstance(result, Mapping):
            raise HTTPException(502, detail={"code": "invalid_service_result", "operation": method})
        return result
    except TaskError as exc:
        code = exc.code if re.fullmatch(r"[a-z_]{1,64}", str(exc.code)) else "service_rejected"
        status = 409 if any(part in code for part in ("conflict", "revision", "state", "delivery_unknown")) else 422
        if "not_found" in code:
            status = 404
        if any(part in code for part in ("permission", "forbidden", "unauthorized")):
            status = 403
        raise HTTPException(status, detail={"code": code}) from None
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") in {"invalid_service_result", "service_unavailable"}:
            raise
        raise HTTPException(exc.status_code, detail={"code": "service_rejected"}) from None
    except (ValueError, TypeError):
        raise HTTPException(422, detail={"code": "invalid_service_request", "operation": method}) from None
    except Exception:
        raise HTTPException(503, detail={"code": "service_failed", "operation": method}) from None


async def _redactor(services, payload=None):
    secrets = _secret_values(payload)
    editor = _member(services, "config_editor")
    if isinstance(editor, ConfigEditor):
        try:
            secrets.update(await asyncio.to_thread(editor.secrets))
        except Exception:
            raise HTTPException(503, detail={"code": "redaction_source_unavailable"}) from None
    extra = _member(services, "redaction_secrets")
    if callable(extra):
        try:
            extra = await _await(extra())
        except Exception:
            raise HTTPException(503, detail={"code": "redaction_source_unavailable"}) from None
    if isinstance(extra, str):
        secrets.add(extra)
    elif extra:
        secrets.update(item for item in extra if isinstance(item, str))
    return Redactor(secrets)


def _project(data, allowed, depth=0):
    if depth > 8:
        return None
    if is_dataclass(data) and not isinstance(data, type):
        data = asdict(data)
    if isinstance(data, Mapping):
        result = {}
        for key, value in data.items():
            if key == "items" and isinstance(value, (list, tuple)):
                result[key] = [_project(item, allowed, depth + 1) for item in value[:200]]
            elif key in {"total", "revision", "offset", "limit", "has_more"} or key in allowed:
                if key in {"tools", "chunks"} and isinstance(value, (list, tuple)):
                    result[key] = [_project(item, allowed, depth + 1) for item in value[:200]]
                else:
                    result[key] = value
        return result
    return None


async def _ok(services, data, allowed=None):
    redactor = await _redactor(services, data)
    safe = _project(data, allowed) if allowed is not None else data
    return JSONResponse({"ok": True, "data": redactor.redact(safe)}, headers=_NO_CACHE)


def _queries(request, allowed):
    if set(request.query_params) - set(allowed) or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(422, detail={"code": "invalid_query"})


def _number(value, default, maximum=200):
    if value is None:
        return default
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}", value) or not 0 <= int(value) <= maximum:
        raise HTTPException(422, detail={"code": "invalid_number"})
    return int(value)


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise HTTPException(422, detail={"code": "invalid_identifier"})
    return value


def _same_origin(request):
    origins = request.headers.getlist("origin")
    hosts = request.headers.getlist("host")
    if len(origins) != 1 or len(hosts) != 1:
        raise HTTPException(403, detail={"code": "same_origin_required"})
    try:
        origin = urlsplit(origins[0])
        host = urlsplit("//" + hosts[0])
        scheme = request.scope.get("scheme")
        default_port = 443 if scheme == "https" else 80
        valid = (
            scheme in {"http", "https"} and origin.scheme == scheme
            and origin.hostname and host.hostname and origin.hostname.lower() == host.hostname.lower()
            and (origin.port if origin.port is not None else default_port) == (host.port if host.port is not None else default_port)
            and not (origin.path or origin.query or origin.fragment or origin.username or origin.password)
            and not (host.path or host.query or host.fragment or host.username or host.password)
            and not any(char.isspace() or ord(char) < 32 for char in origins[0] + hosts[0])
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(403, detail={"code": "same_origin_required"})
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(403, detail={"code": "same_origin_required"})


async def _body(request, allowed, *, revision=True, max_bytes=6 * 1024 * 1024):
    _queries(request, ())
    _same_origin(request)
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, detail={"code": "json_required"})
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > max_bytes:
            raise HTTPException(413, detail={"code": "body_too_large"})
    try:
        data = _json_object(bytes(body))
    except ConfigError:
        raise HTTPException(422, detail={"code": "invalid_json"}) from None
    if set(data) - set(allowed):
        raise HTTPException(422, detail={"code": "unknown_field"})
    if revision:
        if "expected_revision" not in data:
            raise HTTPException(428, detail={"code": "expected_revision_required"})
        if type(data["expected_revision"]) is not int or data["expected_revision"] < 0:
            raise HTTPException(422, detail={"code": "invalid_revision"})
    return data


def _diagnostic_data(payload):
    modules = payload.get("modules", {})
    if isinstance(modules, Mapping):
        modules = [dict(value, name=name) if isinstance(value, Mapping) else {"name": name, "status": value} for name, value in list(modules.items())[:100]]
    if not isinstance(modules, (list, tuple)):
        modules = []
    safe_modules = [_project(item, {"name", "status", "enabled", "version", "error_code"}) for item in modules[:100]]
    counts = payload.get("task_counts", {})
    safe_counts = {str(key)[:64]: value for key, value in list(counts.items())[:100] if type(value) is int and value >= 0} if isinstance(counts, Mapping) else {}
    return {"modules": safe_modules, "task_counts": safe_counts}


def _diagnostic_zip(metadata, trace):
    packages = {}
    for name in ("fastapi", "pydantic", "httpx", "nonebot2", "uvicorn", "mcp", "jsonschema", "pypdf", "python-docx", "openpyxl"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    system = {"python": platform.python_version(), "system": platform.system(), "release": platform.release(), "machine": platform.machine(), "packages": packages}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in (("system.json", system), ("status.json", metadata), ("trace.json", trace)):
            archive.writestr(name, json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2))
    return output.getvalue()


def mount_task_dashboard(app, get_services, require_admin) -> bool:
    if not callable(getattr(app, "include_router", None)) or not callable(get_services) or not callable(require_admin):
        return False
    if getattr(app.state, "catty_task_dashboard_mounted", False):
        return True

    async def authenticate(request: Request):
        result = await _await(require_admin(request))
        if result is False:
            raise HTTPException(403, detail={"code": "admin_required"})

    async def services():
        result = await _await(get_services())
        if result is None:
            raise HTTPException(503, detail={"code": "service_unavailable"})
        return result

    router = APIRouter(prefix="/dashboard", dependencies=[Depends(authenticate)])

    @router.get("/tasks", response_class=HTMLResponse)
    async def page(request: Request):
        _queries(request, ())
        html = await asyncio.to_thread((Path(__file__).parent / "web" / "task-center.html").read_text, encoding="utf-8")
        headers = {**_NO_CACHE, "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'", "X-Frame-Options": "DENY"}
        return HTMLResponse(html, headers=headers)

    @router.get("/api/tasks")
    async def tasks(request: Request, service=Depends(services)):
        _queries(request, {"status", "owner_id", "limit", "offset"})
        params = request.query_params
        data = await _call(service, "list_tasks", status=_identifier(params["status"]) if params.get("status") else None, owner_id=_identifier(params["owner_id"]) if params.get("owner_id") else None, limit=_number(params.get("limit"), 50), offset=_number(params.get("offset"), 0, 1_000_000))
        return await _ok(service, data, _TASK_FIELDS)

    @router.get("/api/tasks/{task_id}")
    async def task(request: Request, task_id: str, service=Depends(services)):
        _queries(request, ())
        return await _ok(service, await _call(service, "get_task", task_id=_identifier(task_id)), _TASK_FIELDS)

    @router.post("/api/tasks/{task_id}/{action}")
    async def task_action(request: Request, task_id: str, action: str, service=Depends(services)):
        actions = {"cancel": "cancel", "resume": "resume", "confirm-delivery": "confirm_delivery"}
        if action not in actions:
            raise HTTPException(404, detail={"code": "unknown_action"})
        allowed = {"expected_revision", "delivered"} if action == "confirm-delivery" else {"expected_revision"}
        data = await _body(request, allowed)
        if action == "confirm-delivery" and type(data.get("delivered")) is not bool:
            raise HTTPException(422, detail={"code": "delivery_confirmation_required"})
        return await _ok(service, await _call(service, "mutate_task", task_id=_identifier(task_id), action=actions[action], **data), _TASK_FIELDS)

    @router.get("/api/schedules")
    async def schedules(request: Request, service=Depends(services)):
        _queries(request, ())
        return await _ok(service, await _call(service, "list_schedules"), _SCHEDULE_FIELDS)

    @router.post("/api/schedules/{schedule_id}/{action}")
    async def schedule_action(request: Request, schedule_id: str, action: str, service=Depends(services)):
        if action not in {"pause", "resume", "delete"}:
            raise HTTPException(404, detail={"code": "unknown_action"})
        data = await _body(request, {"expected_revision"})
        return await _ok(service, await _call(service, "mutate_schedule", schedule_id=_identifier(schedule_id), action=action, **data), _SCHEDULE_FIELDS)

    @router.get("/api/knowledge")
    async def knowledge(request: Request, service=Depends(services)):
        _queries(request, ())
        return await _ok(service, await _call(service, "list_knowledge"), _KNOWLEDGE_FIELDS)

    @router.get("/api/knowledge/search")
    async def knowledge_search(request: Request, service=Depends(services)):
        _queries(request, {"query", "limit"})
        query = request.query_params.get("query", "").strip()
        if not query or len(query) > 1000:
            raise HTTPException(422, detail={"code": "invalid_search_query"})
        return await _ok(service, await _call(service, "search_knowledge", query=query, limit=_number(request.query_params.get("limit"), 20)), _KNOWLEDGE_FIELDS)

    @router.post("/api/knowledge/upload")
    async def knowledge_upload(request: Request, service=Depends(services)):
        data = await _body(request, {"filename", "content_base64", "expected_revision"}, max_bytes=28 * 1024 * 1024)
        filename = _identifier(data.get("filename"))
        if any(char in filename for char in "/\\:") or filename in {".", ".."}:
            raise HTTPException(422, detail={"code": "invalid_filename"})
        try:
            content = base64.b64decode(data["content_base64"], validate=True)
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, detail={"code": "invalid_upload"}) from None
        if not content or len(content) > 20 * 1024 * 1024:
            raise HTTPException(413, detail={"code": "invalid_upload_size"})
        return await _ok(service, await _call(service, "upload_knowledge", filename=filename, content=content, expected_revision=data["expected_revision"]), _KNOWLEDGE_FIELDS)

    @router.post("/api/knowledge/rebuild")
    async def knowledge_rebuild(request: Request, service=Depends(services)):
        data = await _body(request, {"expected_revision"})
        return await _ok(service, await _call(service, "rebuild_knowledge", **data), _KNOWLEDGE_FIELDS)

    @router.delete("/api/knowledge/{document_id}")
    async def knowledge_delete(request: Request, document_id: str, service=Depends(services)):
        data = await _body(request, {"expected_revision"})
        return await _ok(service, await _call(service, "delete_knowledge", document_id=_identifier(document_id), **data), _KNOWLEDGE_FIELDS)

    @router.get("/api/extensions")
    async def extensions(request: Request, service=Depends(services)):
        _queries(request, ())
        return await _ok(service, await _call(service, "list_extensions"), _EXTENSION_FIELDS)

    @router.post("/api/extensions/{extension_id}/{action}")
    async def extension_action(request: Request, extension_id: str, action: str, service=Depends(services)):
        if action not in {"enable", "disable"}:
            raise HTTPException(404, detail={"code": "unknown_action"})
        data = await _body(request, {"expected_revision"})
        return await _ok(service, await _call(service, "set_extension_enabled", extension_id=_identifier(extension_id), enabled=action == "enable", **data), _EXTENSION_FIELDS)

    @router.get("/api/mcp")
    async def mcp(request: Request, service=Depends(services)):
        _queries(request, ())
        return await _ok(service, await _call(service, "list_mcp"), _MCP_FIELDS)

    @router.get("/api/mcp/{server_id}/tools")
    async def mcp_tools(request: Request, server_id: str, service=Depends(services)):
        _queries(request, ())
        return await _ok(service, await _call(service, "list_mcp_tools", server_id=_identifier(server_id)), _MCP_FIELDS)

    @router.get("/api/trace")
    async def trace(request: Request, service=Depends(services)):
        _queries(request, {"task_id", "event", "limit"})
        params = request.query_params
        data = await _call(service, "trace", task_id=_identifier(params["task_id"]) if params.get("task_id") else None, event=_identifier(params["event"]) if params.get("event") else None, limit=_number(params.get("limit"), 100))
        return await _ok(service, data, _TRACE_FIELDS)

    @router.get("/api/diagnostics")
    async def diagnostics(request: Request, service=Depends(services)):
        _queries(request, ())
        raw = await _call(service, "diagnostics")
        redactor = await _redactor(service, raw)
        return JSONResponse({"ok": True, "data": redactor.redact(_diagnostic_data(raw))}, headers=_NO_CACHE)

    @router.get("/api/diagnostics/download")
    async def diagnostic_download(request: Request, service=Depends(services)):
        _queries(request, ())
        raw = await _call(service, "diagnostics")
        events = await _call(service, "trace", task_id=None, event=None, limit=100)
        event_items = events.get("items", [])
        events = {"items": list(event_items[:100])} if isinstance(event_items, (list, tuple)) else {"items": []}
        redactor = await _redactor(service, {"metadata": raw, "events": events})
        metadata = redactor.redact(_diagnostic_data(raw))
        trace_data = redactor.redact(_project(events, _TRACE_FIELDS))
        if isinstance(trace_data.get("items"), list):
            trace_data["items"] = trace_data["items"][:100]
        archive = await asyncio.to_thread(_diagnostic_zip, metadata, trace_data)
        return Response(archive, media_type="application/zip", headers={**_NO_CACHE, "Content-Disposition": 'attachment; filename="catty-task-diagnostics.zip"'})

    def editor_for(service):
        editor = _member(service, "config_editor")
        if not isinstance(editor, ConfigEditor):
            raise HTTPException(503, detail={"code": "config_unavailable"})
        return editor

    async def edit_config(service, operation, *args):
        try:
            data = await asyncio.to_thread(getattr(editor_for(service), operation), *args)
        except ConfigError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code}) from None
        except OSError:
            raise HTTPException(503, detail={"code": "config_unavailable"}) from None
        return await _ok(service, data)

    @router.get("/api/config")
    async def config_read(request: Request, service=Depends(services)):
        _queries(request, ())
        return await edit_config(service, "read")

    @router.patch("/api/config")
    async def config_update(request: Request, service=Depends(services)):
        data = await _body(request, {"patch", "expected_sha256"}, revision=False)
        return await edit_config(service, "update", data.get("patch"), data.get("expected_sha256"))

    @router.post("/api/config/rollback")
    async def config_rollback(request: Request, service=Depends(services)):
        data = await _body(request, {"backup_id", "expected_sha256"}, revision=False)
        return await edit_config(service, "rollback", data.get("backup_id"), data.get("expected_sha256"))

    app.include_router(router)
    app.state.catty_task_dashboard_mounted = True
    return True