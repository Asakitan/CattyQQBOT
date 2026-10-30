"""Explicitly enabled, trusted Python extensions; this is not a sandbox."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import sys
import time
import types
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any

from .task_contracts import TaskError, TaskIdentity


EXTENSION_API_VERSION = 1
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,23}\Z")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.+-]+)?\Z")
_HOOKS = frozenset({"request", "tool", "result"})
_SCHEMA_KEYS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "const", "description", "title", "default", "minLength", "maxLength",
    "minimum", "maximum", "minItems", "maxItems",
})
_JSON_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


class ExtensionError(TaskError):
    def __init__(self, code: str, error_type: str = "ExtensionError"):
        super().__init__(code, error_type)
        self.error_type = error_type


def _identifier(value: Any, *, tool: bool = False) -> str:
    pattern = _TOOL_NAME if tool else _IDENTIFIER
    if not isinstance(value, str) or not pattern.fullmatch(value) or "__" in value:
        raise ValueError("invalid identifier")
    if value.startswith(("catty_", "mcp_", "ext_")):
        raise ValueError("reserved namespace")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_text(value: Any, limit: int) -> str:
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    chunks: list[str] = []
    length = 0
    for chunk in encoder.iterencode(value):
        length += len(chunk)
        if length > limit:
            raise ValueError("JSON size exceeded")
        chunks.append(chunk)
    return "".join(chunks)


def _json_copy(value: Any, limit: int = 65_536) -> Any:
    return json.loads(_json_text(value, limit))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _schema(schema: Any, depth: int = 0) -> None:
    if depth > 12 or not isinstance(schema, dict) or set(schema) - _SCHEMA_KEYS:
        raise ValueError("unsupported parameter schema")
    if schema.get("type") not in _JSON_TYPES:
        raise ValueError("a concrete JSON type is required")
    for key in ("description", "title"):
        if key in schema and (not isinstance(schema[key], str) or len(schema[key]) > 4096):
            raise ValueError("invalid schema text")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or not 0 <= schema[key] <= 1_000_000):
            raise ValueError("invalid schema bound")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError("invalid numeric bound")
    for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems"), ("minimum", "maximum")):
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise ValueError("inverted schema bound")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 256):
        raise ValueError("invalid enum")
    if schema["type"] == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or len(properties) > 128:
            raise ValueError("invalid properties")
        if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            raise ValueError("invalid required properties")
        if len(required) != len(set(required)) or set(required) - set(properties):
            raise ValueError("unknown required property")
        if type(schema.get("additionalProperties", True)) is not bool:
            raise ValueError("additionalProperties must be boolean")
        for name, child in properties.items():
            if not isinstance(name, str) or not name or len(name) > 128:
                raise ValueError("invalid property name")
            _schema(child, depth + 1)
    elif any(key in schema for key in ("properties", "required", "additionalProperties")):
        raise ValueError("object keywords require an object")
    if schema["type"] == "array":
        _schema(schema.get("items"), depth + 1)
    elif "items" in schema:
        raise ValueError("items requires an array")


def _validate(value: Any, schema: dict[str, Any], depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("argument nesting exceeded")
    type_name = schema["type"]
    valid = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "integer": type(value) is int,
        "number": type(value) in (int, float), "boolean": type(value) is bool,
        "null": value is None,
    }[type_name]
    if not valid:
        raise ValueError("argument type mismatch")
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        raise ValueError("argument is not in enum")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError("argument differs from const")
    if type_name == "object":
        if set(schema.get("required", [])) - set(value):
            raise ValueError("missing argument")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties", True) is False and set(value) - set(properties):
            raise ValueError("unexpected argument")
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], depth + 1)
    elif type_name == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1_000_000):
            raise ValueError("argument array length exceeded")
        for item in value:
            _validate(item, schema["items"], depth + 1)
    elif type_name == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1_000_000):
            raise ValueError("argument string length exceeded")
    elif type_name in ("number", "integer"):
        if not math.isfinite(value) or not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            raise ValueError("argument number exceeded")


@dataclass(frozen=True, slots=True)
class _Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    allow_guests: bool


@dataclass(frozen=True, slots=True)
class _Manifest:
    name: str
    version: str
    module: str
    tools: tuple[_Tool, ...]


@dataclass(slots=True, eq=False)
class _Loaded:
    manifest: _Manifest
    handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    hooks: dict[str, Callable[..., Any]] = field(default_factory=dict)
    module_name: str = ""
    active: set[asyncio.Task[Any]] = field(default_factory=set)
    transitioning: bool = False
    unload_task: asyncio.Task[Any] | None = None


def _manifest(raw: Any) -> _Manifest:
    if not isinstance(raw, dict) or set(raw) - {"name", "version", "api_version", "module", "tools"}:
        raise ValueError("invalid manifest")
    name = _identifier(raw.get("name"))
    version = raw.get("version")
    if not isinstance(version, str) or len(version) > 64 or not _VERSION.fullmatch(version):
        raise ValueError("invalid extension version")
    if type(raw.get("api_version")) not in (int, str) or raw["api_version"] not in (1, "1"):
        raise ValueError("unsupported extension API version")
    module = raw.get("module")
    if not isinstance(module, str) or not module or len(module) > 256:
        raise ValueError("invalid module path")
    path = Path(module.replace("\\", "/"))
    if path.is_absolute() or PureWindowsPath(module).drive or ".." in path.parts or path.suffix != ".py":
        raise ValueError("module must be a relative Python source path")
    raw_tools = raw.get("tools")
    if not isinstance(raw_tools, list) or len(raw_tools) > 64:
        raise ValueError("invalid tool declarations")
    tools: list[_Tool] = []
    names: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict) or set(item) != {"name", "description", "parameters", "allow_guests"}:
            raise ValueError("invalid tool declaration")
        tool_name = _identifier(item["name"], tool=True)
        if tool_name in names:
            raise ValueError("duplicate tool name")
        names.add(tool_name)
        description = item["description"]
        if not isinstance(description, str) or not description.strip() or len(description) > 4096:
            raise ValueError("invalid tool description")
        if type(item["allow_guests"]) is not bool:
            raise ValueError("allow_guests must be boolean")
        parameters = _json_copy(item["parameters"])
        _schema(parameters)
        if parameters["type"] != "object":
            raise ValueError("tool parameters must be an object")
        tools.append(_Tool(tool_name, description, parameters, item["allow_guests"]))
    return _Manifest(name, version, module, tuple(tools))


class ExtensionRegistry:
    """Host-only administration and observer hooks for trusted extension code.

    Construction/discovery never executes extensions. Callbacks receive JSON
    arguments and a read-only context, never a Config instance. Parameter schemas
    use the bounded, local JSON Schema subset implemented in this module.
    """

    def __init__(self, root: str | Path, settings: Mapping[str, Any] | None = None):
        settings = {} if settings is None else settings
        if not isinstance(settings, Mapping):
            raise ValueError("extension settings must be a mapping")
        self.root = Path(root).expanduser().resolve()
        enabled = settings.get("enabled_ids", ())
        if not isinstance(enabled, (list, tuple, set, frozenset)):
            raise ValueError("enabled_ids must be a collection")
        self._enabled_ids = {_identifier(name) for name in enabled}
        self._settings: dict[str, Any] = {}
        for name, default, minimum, maximum in (
            ("timeout_seconds", 30.0, 0.01, 300.0),
            ("hook_timeout_seconds", 1.0, 0.01, 30.0),
            ("lifecycle_timeout_seconds", 5.0, 0.01, 60.0),
            ("close_timeout_seconds", 5.0, 0.01, 60.0),
            ("max_args_chars", 16_384, 128, 1_048_576),
            ("max_result_chars", 8_000, 128, 65_536),
            ("max_stored_result_chars", 262_144, 128, 1_048_576),
        ):
            value = settings.get(name, default)
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError("invalid extension limit")
            self._settings[name] = float(value) if "seconds" in name else int(value)
        for name in ("task_context", "authorize", "result_reference"):
            callback = settings.get(name)
            if callback is not None and (not callable(callback) or inspect.iscoroutinefunction(callback) and name != "result_reference"):
                raise ValueError("invalid host callback")
            self._settings[name] = callback
        self._builtins: dict[str, _Loaded] = {}
        self._loaded: dict[str, _Loaded] = {}
        self._retired: list[_Loaded] = []
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def enabled_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._enabled_ids))

    def _inside(self, path: Path, parent: Path | None = None) -> Path:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root) or parent is not None and not resolved.is_relative_to(parent):
            raise ValueError("extension path escapes configured root")
        return resolved

    def _read_manifest(self, name: str) -> tuple[_Manifest, Path]:
        directory = self._inside(self.root / _identifier(name))
        manifest_path = self._inside(directory / "manifest.json", directory)
        with manifest_path.open("rb") as stream:
            contents = stream.read(65_537)
        if len(contents) > 65_536:
            raise ValueError("manifest size exceeded")
        manifest = _manifest(json.loads(contents.decode("utf-8"), object_pairs_hook=_unique_object))
        if manifest.name != name:
            raise ValueError("manifest name differs from directory")
        module_path = self._inside(directory / manifest.module.replace("\\", "/"), directory)
        if not module_path.is_file():
            raise ValueError("module is not a file")
        return manifest, module_path

    @staticmethod
    def _status(manifest: _Manifest, *, enabled: bool = False, state: str = "disabled") -> dict[str, Any]:
        return {
            "id": manifest.name, "name": manifest.name, "version": manifest.version,
            "api_version": EXTENSION_API_VERSION, "enabled": enabled, "state": state,
            "tools": [f"ext__{manifest.name}__{tool.name}" for tool in manifest.tools],
        }

    def discover(self) -> list[dict[str, Any]]:
        discovered = {name: self._status(record.manifest) for name, record in self._builtins.items()}
        if not self.root.exists():
            return list(discovered.values())
        try:
            entries = iter(self.root.iterdir())
            for index, entry in enumerate(entries):
                if index >= 256:
                    break
                if not entry.is_dir():
                    continue
                name = entry.name
                try:
                    _identifier(name)
                except ValueError:
                    continue
                if name in discovered:
                    continue
                try:
                    manifest, unused_path = self._read_manifest(name)
                    discovered[name] = self._status(manifest)
                except Exception as error:
                    discovered[name] = {"id": name, "enabled": False, "state": "invalid", "error": type(error).__name__}
        except OSError as error:
            raise ExtensionError("discovery_failed", type(error).__name__) from None
        return [discovered[name] for name in sorted(discovered)]

    def list_extensions(self) -> list[dict[str, Any]]:
        statuses = {item["id"]: item for item in self.discover()}
        for name, record in self._loaded.items():
            statuses[name] = self._status(
                record.manifest, enabled=name in self._enabled_ids and not record.transitioning,
                state="draining" if record.transitioning else "enabled",
            )
            statuses[name]["in_flight"] = len(record.active)
        return [statuses[name] for name in sorted(statuses)]

    def register_builtin(
        self, manifest: Mapping[str, Any], handlers: Mapping[str, Callable[..., Any]],
        *, hooks: Mapping[str, Callable[..., Any]] | None = None,
    ) -> str:
        if self._closed:
            raise ExtensionError("registry_closed")
        declaration = _manifest(_json_copy(dict(manifest)))
        if declaration.name in self._builtins or declaration.name in self._loaded or (self.root / declaration.name).exists():
            raise ExtensionError("duplicate_extension")
        if set(handlers) != {tool.name for tool in declaration.tools} or any(not callable(handler) for handler in handlers.values()):
            raise ValueError("handlers must exactly match declared tools")
        callbacks = dict(hooks or {})
        if set(callbacks) - (_HOOKS | {"load", "unload"}) or any(not callable(callback) for callback in callbacks.values()):
            raise ValueError("invalid extension hooks")
        self._validate_callbacks(handlers, callbacks)
        self._builtins[declaration.name] = _Loaded(declaration, dict(handlers), callbacks)
        return declaration.name

    @staticmethod
    def _validate_callbacks(handlers: Mapping[str, Callable[..., Any]], hooks: Mapping[str, Callable[..., Any]]) -> None:
        for callback in handlers.values():
            inspect.signature(callback).bind({}, MappingProxyType({}))
        for callback in hooks.values():
            inspect.signature(callback).bind(MappingProxyType({}))

    @staticmethod
    def _identity(identity: TaskIdentity, *, admin: bool = False) -> None:
        if not isinstance(identity, TaskIdentity) or admin and not identity.is_owner:
            raise PermissionError("authenticated owner required" if admin else "authenticated identity required")

    @staticmethod
    def _error_type(error: BaseException) -> str:
        return type(error).__name__[:80]

    def _budget(self, identity: TaskIdentity, supplied: Mapping[str, Any] | None) -> dict[str, Any] | None:
        self._identity(identity)
        raw = supplied
        provider = self._settings["task_context"]
        if raw is None and provider is not None:
            raw = provider(identity)
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise PermissionError("invalid task context")
        root_task_id = raw.get("root_task_id")
        authenticated = raw.get("identity")
        allowed = raw.get("allowed_tools")
        if (
            raw.get("confirmed") is not True
            or not isinstance(root_task_id, str) or not root_task_id.strip() or len(root_task_id) > 128
            or not isinstance(authenticated, Mapping) or dict(authenticated) != identity.as_dict()
            or not isinstance(allowed, (list, tuple, set, frozenset)) or len(allowed) > 256
            or any(not isinstance(name, str) or not name or len(name) > 64 for name in allowed)
        ):
            raise PermissionError("invalid confirmed task context")
        snapshot = {
            "confirmed": True, "root_task_id": root_task_id,
            "identity": identity.as_dict(), "allowed_tools": sorted(set(allowed)),
        }
        for name in ("timeout_seconds", "remaining_tokens", "remaining_steps", "max_result_chars"):
            if name in raw:
                value = raw[name]
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise PermissionError("exhausted task budget")
                if name == "max_result_chars" and (type(value) is not int or value < 128):
                    raise PermissionError("invalid result budget")
                snapshot[name] = value
        return snapshot

    def _allowed(
        self, identity: TaskIdentity, record: _Loaded, tool: _Tool,
        budget: dict[str, Any] | None, *, execution: bool,
    ) -> bool:
        if self._closed or record.transitioning or record.manifest.name not in self._enabled_ids:
            return False
        if self._loaded.get(record.manifest.name) is not record:
            return False
        qualified_name = f"ext__{record.manifest.name}__{tool.name}"
        if not identity.is_owner and (not tool.allow_guests or execution and budget is None):
            return False
        if budget is not None and qualified_name not in budget["allowed_tools"]:
            return False
        authorizer = self._settings["authorize"]
        if authorizer is not None:
            decision = authorizer(identity, qualified_name, _freeze(_json_copy(budget)))
            if inspect.isawaitable(decision):
                if inspect.iscoroutine(decision):
                    decision.close()
                return False
            if decision is not True:
                return False
        return True

    def schemas(self, identity: TaskIdentity, budget: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        try:
            self._identity(identity)
            context = self._budget(identity, budget)
            result = []
            for name in sorted(self._loaded):
                record = self._loaded[name]
                for tool in record.manifest.tools:
                    if self._allowed(identity, record, tool, context, execution=False):
                        result.append({
                            "type": "function",
                            "function": {
                                "name": f"ext__{name}__{tool.name}", "description": tool.description,
                                "parameters": _json_copy(tool.parameters),
                            },
                        })
            return result
        except Exception:
            return []

    @staticmethod
    async def _execute(callback: Callable[..., Any], *arguments: Any) -> Any:
        if inspect.iscoroutinefunction(callback):
            return await callback(*arguments)
        result = await asyncio.to_thread(callback, *arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    def _start(self, record: _Loaded, callback: Callable[..., Any], *arguments: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._execute(callback, *arguments))
        record.active.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            record.active.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return task

    @staticmethod
    async def _wait(task: asyncio.Task[Any], timeout: float) -> Any:
        done, pending = await asyncio.wait({task}, timeout=max(0.0, timeout))
        if not done:
            raise TimeoutError("extension callback timed out")
        return task.result()

    async def _run(self, record: _Loaded, callback: Callable[..., Any], *arguments: Any, timeout: float) -> Any:
        return await self._wait(self._start(record, callback, *arguments), timeout)

    @staticmethod
    def _lifecycle_context(record: _Loaded) -> Mapping[str, Any]:
        return MappingProxyType({"extension_id": record.manifest.name, "api_version": EXTENSION_API_VERSION})

    def _import_source(self, record: _Loaded, path: Path) -> None:
        directory = self._inside(self.root / record.manifest.name)
        source_path = self._inside(path, directory)
        with source_path.open("rb") as stream:
            source = stream.read(1_048_577)
        if len(source) > 1_048_576:
            raise ValueError("extension source size exceeded")
        module = types.ModuleType(record.module_name)
        module.__file__ = str(source_path)
        module.__package__ = record.module_name
        module.__path__ = [str(directory)]
        sys.modules[record.module_name] = module
        exec(compile(source, str(source_path), "exec"), module.__dict__)
        supplied = getattr(module, "TOOLS", None)
        if supplied is None:
            supplied = {tool.name: getattr(module, tool.name, None) for tool in record.manifest.tools}
        if not isinstance(supplied, Mapping) or set(supplied) != {tool.name for tool in record.manifest.tools}:
            raise ValueError("module tools differ from manifest")
        if any(not callable(callback) for callback in supplied.values()):
            raise ValueError("module tool is not callable")
        record.handlers = dict(supplied)
        hooks = getattr(module, "HOOKS", {})
        if not isinstance(hooks, Mapping) or set(hooks) - (_HOOKS | {"load", "unload"}):
            raise ValueError("invalid module hooks")
        record.hooks = dict(hooks)
        for name in _HOOKS | {"load", "unload"}:
            callback = getattr(module, f"on_{name}", None)
            if callback is not None:
                if name in record.hooks:
                    raise ValueError("duplicate hook")
                record.hooks[name] = callback
        if any(not callable(callback) for callback in record.hooks.values()):
            raise ValueError("module hook is not callable")
        self._validate_callbacks(record.handlers, record.hooks)

    @staticmethod
    def _remove_module(record: _Loaded) -> None:
        if record.module_name:
            for name in tuple(sys.modules):
                if name == record.module_name or name.startswith(record.module_name + "."):
                    sys.modules.pop(name, None)

    async def _unload(self, record: _Loaded, *, wait: bool, timeout: float) -> None:
        previous = record.transitioning
        record.transitioning = True
        deadline = time.monotonic() + timeout
        while True:
            pending = {task for task in record.active if not task.done() and task is not record.unload_task}
            if not pending:
                break
            if not wait or time.monotonic() >= deadline:
                record.transitioning = previous
                raise ExtensionError("extension_busy")
            await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()))
        callback = record.hooks.get("unload")
        if callback is not None:
            if record.unload_task is None:
                record.unload_task = self._start(record, callback, self._lifecycle_context(record))
            try:
                await self._wait(record.unload_task, deadline - time.monotonic())
            except Exception:
                if record.unload_task.done():
                    record.unload_task = None
                raise
        self._remove_module(record)

    async def _reap_retired(self) -> None:
        for record in tuple(self._retired):
            if any(not task.done() for task in record.active):
                continue
            try:
                await self._unload(record, wait=False, timeout=self._settings["lifecycle_timeout_seconds"])
            except Exception:
                continue
            self._retired.remove(record)

    async def _prepare(self, name: str) -> _Loaded:
        await self._reap_retired()
        if len(self._retired) >= 32 or len(self._loaded) >= 64:
            raise ExtensionError("extension_capacity")
        if name in self._builtins:
            prototype = self._builtins[name]
            record = _Loaded(prototype.manifest, dict(prototype.handlers), dict(prototype.hooks))
            source_path = None
        else:
            declaration, source_path = self._read_manifest(name)
            record = _Loaded(declaration, module_name=f"_catty_ext_{name}_{uuid.uuid4().hex}")
        self._retired.append(record)
        try:
            if source_path is not None:
                await self._run(record, self._import_source, record, source_path, timeout=self._settings["lifecycle_timeout_seconds"])
            callback = record.hooks.get("load")
            if callback is not None:
                await self._run(record, callback, self._lifecycle_context(record), timeout=self._settings["lifecycle_timeout_seconds"])
        except Exception:
            await self._reap_retired()
            raise
        return record

    async def enable(self, name: str, *, identity: TaskIdentity) -> dict[str, Any]:
        self._identity(identity, admin=True)
        name = _identifier(name)
        async with self._lock:
            if self._closed:
                raise ExtensionError("registry_closed")
            old = self._loaded.get(name)
            if old is not None:
                if old.transitioning:
                    raise ExtensionError("extension_busy")
                return self._status(old.manifest, enabled=True, state="enabled")
            try:
                record = await self._prepare(name)
            except Exception as error:
                raise ExtensionError("extension_load_failed", self._error_type(error)) from None
            self._loaded[name] = record
            self._retired.remove(record)
            self._enabled_ids.add(name)
            return self._status(record.manifest, enabled=True, state="enabled")

    async def disable(self, name: str, *, identity: TaskIdentity, wait: bool = False) -> dict[str, Any]:
        self._identity(identity, admin=True)
        name = _identifier(name)
        async with self._lock:
            record = self._loaded.get(name)
            if record is not None:
                try:
                    await self._unload(record, wait=wait, timeout=self._settings["lifecycle_timeout_seconds"])
                except ExtensionError:
                    raise
                except Exception as error:
                    self._enabled_ids.discard(name)
                    raise ExtensionError("extension_unload_failed", self._error_type(error)) from None
                self._loaded.pop(name, None)
            self._enabled_ids.discard(name)
            return {"id": name, "enabled": False, "state": "disabled"}

    async def reload(self, name: str, *, identity: TaskIdentity) -> dict[str, Any]:
        self._identity(identity, admin=True)
        name = _identifier(name)
        async with self._lock:
            if self._closed:
                raise ExtensionError("registry_closed")
            old = self._loaded.get(name)
            if old is None:
                raise ExtensionError("extension_not_enabled")
            if old.transitioning or any(not task.done() for task in old.active):
                raise ExtensionError("extension_busy")
            old.transitioning = True
            try:
                replacement = await self._prepare(name)
            except BaseException as error:
                old.transitioning = False
                if not isinstance(error, Exception):
                    raise
                raise ExtensionError("extension_load_failed", self._error_type(error)) from None
            try:
                await self._unload(old, wait=False, timeout=self._settings["lifecycle_timeout_seconds"])
            except Exception as error:
                await self._reap_retired()
                raise ExtensionError("extension_unload_failed", self._error_type(error)) from None
            self._loaded[name] = replacement
            self._retired.remove(replacement)
            self._enabled_ids.add(name)
            return self._status(replacement.manifest, enabled=True, state="enabled")

    async def emit(self, hook: str, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        return await self._emit(hook, event, time.monotonic() + self._settings["timeout_seconds"])

    async def _emit(self, hook: str, event: Mapping[str, Any], deadline: float) -> list[dict[str, Any]]:
        if hook not in _HOOKS:
            raise ValueError("unknown extension hook")
        if not isinstance(event, Mapping):
            raise ValueError("hook event must be a mapping")
        snapshot = _json_copy(dict(event))
        reports = []
        for name, record in tuple(self._loaded.items()):
            callback = record.hooks.get(hook)
            if self._closed or record.transitioning or name not in self._enabled_ids or callback is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await self._run(
                    record, callback, _freeze(_json_copy(snapshot)),
                    timeout=min(remaining, self._settings["hook_timeout_seconds"]),
                )
                reports.append({"id": name, "ok": True})
            except Exception as error:
                reports.append({"id": name, "ok": False, "error": self._error_type(error)})
        return reports

    async def _result(
        self, record: _Loaded, identity: TaskIdentity, qualified_name: str,
        value: Any, budget: dict[str, Any] | None, timeout: float,
    ) -> Any:
        text = await self._run(
            record, _json_text, value, self._settings["max_stored_result_chars"], timeout=timeout,
        )
        limit = self._settings["max_result_chars"]
        if budget is not None and "max_result_chars" in budget:
            limit = min(limit, int(budget["max_result_chars"]))
        if len(text) <= limit:
            return json.loads(text)
        result = {"preview": text[:max(0, limit - 128)], "truncated": True}
        callback = self._settings["result_reference"]
        if callback is not None:
            try:
                reference = await self._run(
                    record, callback, identity, qualified_name, text,
                    timeout=min(timeout, self._settings["hook_timeout_seconds"]),
                )
                if not isinstance(reference, str) or not reference or len(reference) > min(1024, limit // 2):
                    raise ValueError("invalid result reference")
                result["reference"] = reference
                result["preview"] = text[:max(0, limit - len(reference) - 128)]
            except Exception as error:
                result["reference_error"] = self._error_type(error)
        while True:
            try:
                _json_text(result, limit)
                break
            except ValueError:
                if result["preview"]:
                    result["preview"] = result["preview"][:len(result["preview"]) // 2]
                elif "reference" in result:
                    result.pop("reference")
                elif "reference_error" in result:
                    result.pop("reference_error")
                else:
                    raise
        return result

    async def _perform_call(
        self, record: _Loaded, tool: _Tool, identity: TaskIdentity,
        arguments: dict[str, Any], budget: dict[str, Any] | None, deadline: float,
    ) -> dict[str, Any]:
        qualified_name = f"ext__{record.manifest.name}__{tool.name}"
        event = {
            "identity": identity.as_dict(), "budget": _json_copy(budget),
            "tool": qualified_name, "arguments": _json_copy(arguments),
        }
        await self._emit("request", event, deadline)
        await self._emit("tool", event, deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("tool deadline reached")
        if not self._allowed(identity, record, tool, budget, execution=True):
            raise PermissionError("tool authorization expired")
        context = _freeze({
            "identity": identity.as_dict(), "budget": _json_copy(budget),
            "extension_id": record.manifest.name, "tool": qualified_name,
        })
        value = await self._run(
            record, record.handlers[tool.name], _json_copy(arguments), context,
            timeout=deadline - time.monotonic(),
        )
        result = await self._result(record, identity, qualified_name, value, budget, deadline - time.monotonic())
        await self._emit("result", {"identity": identity.as_dict(), "budget": _json_copy(budget), "tool": qualified_name, "result": result}, deadline)
        return {"ok": True, "result": result}

    async def call_tool(
        self, identity: TaskIdentity, qualified_name: str, args: Mapping[str, Any],
        budget: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            self._identity(identity)
            if not isinstance(qualified_name, str):
                raise PermissionError("invalid tool name")
            parts = qualified_name.split("__")
            if len(parts) != 3 or parts[0] != "ext":
                raise PermissionError("invalid tool namespace")
            name, tool_name = _identifier(parts[1]), _identifier(parts[2], tool=True)
            record = self._loaded.get(name)
            tool = next((item for item in record.manifest.tools if item.name == tool_name), None) if record is not None else None
            context = self._budget(identity, budget)
            if record is None or tool is None or not self._allowed(identity, record, tool, context, execution=True):
                raise PermissionError("tool is not authorized")
            if not isinstance(args, Mapping):
                raise ValueError("tool arguments must be an object")
            arguments = _json_copy(dict(args), self._settings["max_args_chars"])
            _validate(arguments, tool.parameters)
            timeout = self._settings["timeout_seconds"]
            if context is not None:
                timeout = min(timeout, context.get("timeout_seconds", timeout))
            task = self._start(record, self._perform_call, record, tool, identity, arguments, context, time.monotonic() + timeout)
            return await self._wait(task, timeout)
        except Exception as error:
            return {"ok": False, "error": self._error_type(error)}

    async def close(self) -> dict[str, Any]:
        async with self._lock:
            self._closed = True
            deadline = time.monotonic() + self._settings["close_timeout_seconds"]
            records = list(self._loaded.values()) + list(self._retired)
            pending: list[str] = []
            errors: list[dict[str, str]] = []
            for record in records:
                try:
                    await self._unload(record, wait=True, timeout=max(0.0, deadline - time.monotonic()))
                except Exception as error:
                    pending.append(record.manifest.name)
                    errors.append({"id": record.manifest.name, "error": self._error_type(error)})
                    continue
                if self._loaded.get(record.manifest.name) is record:
                    self._loaded.pop(record.manifest.name)
                if record in self._retired:
                    self._retired.remove(record)
            self._enabled_ids.clear()
            return {"ok": not pending, "pending": sorted(set(pending)), "errors": errors}