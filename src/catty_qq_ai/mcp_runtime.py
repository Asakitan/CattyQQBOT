from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import logging
import math
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from .task_contracts import TaskError, TaskIdentity, TaskPolicy


_SERVER_ID = re.compile(r"[a-z][a-z0-9_]{0,23}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|cookie|env)\Z", re.I)
_SECRET_TEXT = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization|cookie|session[-_ ]?id)(\s*[:=]\s*)[^\s,;'\"&}]+")
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_SCHEMA_BYTES = 64 * 1024
_MAX_TOOLS = 256
_LOG_NAMES = (
    "client", "mcp.client.stdio", "mcp.client.streamable_http", "mcp.shared.session",
    "mcp.os.win32.utilities", "mcp.os.posix.utilities",
    "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
)

ArtifactWriter = Callable[[str, bytes, str], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class MCPCallContext:
    identity: TaskIdentity
    task_id: str
    confirmed: bool
    timeout_seconds: float | None = None
    artifact_writer: ArtifactWriter | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.identity, TaskIdentity) or type(self.confirmed) is not bool:
            raise ValueError("MCP context requires a verified identity and confirmation")
        if not isinstance(self.task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.task_id):
            raise ValueError("MCP context requires a bounded root task id")
        if self.timeout_seconds is not None:
            _number(self.timeout_seconds, 0.001, 3600, "context timeout")
        if self.artifact_writer is not None and not callable(self.artifact_writer):
            raise ValueError("artifact_writer must be callable")


def _number(value: Any, minimum: float, maximum: float, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"invalid MCP {label}")
    return float(value)


def _name(value: Any, server: bool = False) -> str:
    pattern = _SERVER_ID if server else _TOOL_NAME
    if not isinstance(value, str) or "__" in value or pattern.fullmatch(value) is None:
        raise TaskError("mcp_invalid_name", "invalid MCP namespace")
    return value


def _json_bytes(value: Any, limit: int, code: str) -> bytes:
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise TaskError(code, "invalid MCP JSON value") from None
    if len(data) > limit:
        raise TaskError(code, "MCP value exceeds its size limit")
    return data


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    raise TaskError("mcp_invalid_response", "invalid MCP response object")


def _check_schema(schema: Any) -> Any:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise TaskError("mcp_invalid_schema", "MCP tool schema must describe an object")
    _json_bytes(schema, _MAX_SCHEMA_BYTES, "mcp_invalid_schema")
    pending = [(schema, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 4096 or depth > 32:
            raise TaskError("mcp_invalid_schema", "MCP schema is too complex")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    if not isinstance(child, str) or not child.startswith("#"):
                        raise TaskError("mcp_invalid_schema", "external MCP schema references are not allowed")
                if key == "$id" and child not in ("", "#"):
                    raise TaskError("mcp_invalid_schema", "external MCP schema identifiers are not allowed")
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    try:
        from jsonschema.validators import validator_for

        validator = validator_for(schema)
        validator.check_schema(schema)
        return validator(schema)
    except ImportError:
        raise TaskError("mcp_sdk_unavailable", "MCP JSON schema dependency is unavailable") from None
    except Exception:
        raise TaskError("mcp_invalid_schema", "invalid MCP tool schema") from None


@dataclass(frozen=True, slots=True)
class _Server:
    id: str
    transport: str
    url: str = field(repr=False)
    command: str = field(repr=False)
    args: tuple[str, ...] = field(repr=False)
    env: Mapping[str, str] = field(repr=False)
    enabled: bool
    allow_guests: bool
    allowed_tools: frozenset[str]
    timeout_seconds: float
    max_result_bytes: int

    @classmethod
    def parse(cls, raw: Any) -> _Server:
        fields = {"id", "transport", "url", "command", "args", "env", "enabled", "allow_guests", "allowed_tools", "timeout_seconds", "max_result_bytes"}
        if not isinstance(raw, Mapping) or set(raw) - fields:
            raise ValueError("invalid MCP server configuration")
        server_id = _name(raw.get("id"), server=True)
        transport = raw.get("transport", "stdio")
        if transport in ("http", "streamable-http"):
            transport = "streamable_http"
        if transport not in ("stdio", "streamable_http"):
            raise ValueError("MCP transport must be stdio or streamable_http")
        enabled, guests = raw.get("enabled", True), raw.get("allow_guests", False)
        if type(enabled) is not bool or type(guests) is not bool:
            raise ValueError("MCP enable and guest flags must be booleans")
        allowed = raw.get("allowed_tools", [])
        if not isinstance(allowed, list) or len(allowed) > _MAX_TOOLS:
            raise ValueError("MCP allowed_tools must be a bounded list")
        for item in allowed:
            if item != "*":
                _name(item)
        timeout = _number(raw.get("timeout_seconds", 30), 0.01, 120, "server timeout")
        max_bytes = raw.get("max_result_bytes", 64 * 1024)
        if type(max_bytes) is not int or not 128 <= max_bytes <= 1024 * 1024:
            raise ValueError("invalid MCP result limit")
        command, url = raw.get("command", ""), raw.get("url", "")
        args, env = raw.get("args", []), raw.get("env", {})
        if not isinstance(args, list) or len(args) > 128 or any(not isinstance(item, str) or "\x00" in item or len(item) > 8192 for item in args):
            raise ValueError("invalid MCP command arguments")
        if not isinstance(env, Mapping) or len(env) > 128 or any(not isinstance(key, str) or _ENV_NAME.fullmatch(key) is None or not isinstance(value, str) or "\x00" in value or len(value) > 8192 for key, value in env.items()):
            raise ValueError("invalid MCP environment")
        if transport == "stdio":
            if not isinstance(command, str) or not command.strip() or len(command) > 4096 or any(ord(char) < 32 for char in command) or url:
                raise ValueError("stdio requires an administrator configured executable")
        else:
            if not isinstance(url, str) or len(url) > 4096 or any(ord(char) < 32 for char in url):
                raise ValueError("invalid MCP URL")
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and not parsed.fragment
                parsed.port
            except ValueError:
                valid = False
            if not valid or command or args or env:
                raise ValueError("HTTP MCP requires a configured http(s) URL without stdio settings")
        return cls(server_id, transport, url, command, tuple(args), dict(env), enabled, guests, frozenset(allowed), timeout, max_bytes)

    def permits(self, identity: TaskIdentity, tool: str) -> bool:
        return self.enabled and (identity.is_owner or self.allow_guests) and (tool in self.allowed_tools or "*" in self.allowed_tools)


def _load_sdk() -> Any:
    try:
        installed = version("mcp").split(".")
        if int(installed[0]) != 1 or int(installed[1]) < 27:
            raise ImportError("unsupported SDK")
        import httpx
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.types import PaginatedRequestParams

        return SimpleNamespace(ClientSession=ClientSession, StdioServerParameters=StdioServerParameters,
                               stdio_client=stdio_client, streamable_http_client=streamable_http_client,
                               AsyncClient=httpx.AsyncClient, PaginatedRequestParams=PaginatedRequestParams)
    except (ImportError, ValueError):
        raise TaskError("mcp_sdk_unavailable", "MCP requires the official Python SDK >=1.27,<2") from None


class _LogRedactor(logging.Filter):
    def __init__(self, runtime: MCPRuntime):
        super().__init__()
        self.runtime = runtime

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.runtime._redact_text(record.getMessage())
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


@dataclass(slots=True)
class _Request:
    operation: str
    identity: TaskIdentity
    name: str
    arguments: dict[str, Any]
    deadline: float
    future: asyncio.Future


class _Connection:
    """Only the owner coroutine enters and exits SDK cancel scopes."""

    def __init__(self, runtime: MCPRuntime, server: _Server):
        self.runtime, self.server = runtime, server
        self.queue: asyncio.Queue[_Request] = asyncio.Queue(maxsize=16)
        self.ready = asyncio.get_running_loop().create_future()
        self.connected = False
        self.exiting = False
        self.tools: dict[str, dict[str, Any]] = {}
        self.active: _Request | None = None
        self.task = asyncio.create_task(self._serve(), name=f"mcp:{server.id}")

    async def wait_ready(self, timeout: float | None = None):
        try:
            duration = self.server.timeout_seconds * 2
            if timeout is not None:
                duration = min(duration, max(0, timeout))
            await asyncio.wait_for(asyncio.shield(self.ready), duration)
        except asyncio.TimeoutError:
            await self.stop()
            raise TaskError("mcp_timeout", "MCP connection timed out") from None
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self):
        if not self.task.done() and not self.exiting:
            self.task.cancel()
        await asyncio.shield(self.task)

    async def request(self, operation: str, identity: TaskIdentity, name: str = "", arguments: dict[str, Any] | None = None, timeout: float | None = None):
        await self.wait_ready()
        if self.task.done() or self.exiting:
            raise TaskError("mcp_disconnected", "MCP connection is closed")
        duration = min(timeout or self.server.timeout_seconds, self.server.timeout_seconds)
        future = asyncio.get_running_loop().create_future()
        request = _Request(operation, identity, name, arguments or {}, asyncio.get_running_loop().time() + duration, future)
        try:
            self.queue.put_nowait(request)
        except asyncio.QueueFull:
            raise TaskError("mcp_busy", "MCP request queue is full") from None
        try:
            return await asyncio.wait_for(asyncio.shield(future), duration)
        except asyncio.TimeoutError:
            if not future.cancel():
                future.exception()
            if self.active is request:
                await self.stop()
            raise TaskError("mcp_timeout", "MCP operation timed out; it was not replayed") from None
        except asyncio.CancelledError:
            if not future.cancel() and not future.cancelled():
                future.exception()
            await self.stop()
            raise
        except TaskError as error:
            if error.code in {"mcp_timeout", "mcp_call_failed", "mcp_disconnected"}:
                await self.stop()
            raise

    async def _read_tools(self, session: Any):
        tools: dict[str, dict[str, Any]] = {}
        cursor = None
        seen = set()
        for _ in range(8):
            response = await session.list_tools() if cursor is None else await session.list_tools(params=self.runtime._sdk.PaginatedRequestParams(cursor=cursor))
            result = _mapping(response)
            entries = result.get("tools")
            if not isinstance(entries, list) or len(entries) + len(tools) > _MAX_TOOLS:
                raise TaskError("mcp_invalid_schema", "invalid MCP tool inventory")
            for entry in entries:
                tool = _mapping(entry)
                name = _name(tool.get("name"))
                if name in tools:
                    raise TaskError("mcp_invalid_schema", "duplicate MCP tool name")
                schema = copy.deepcopy(tool.get("inputSchema"))
                validator = _check_schema(schema)
                if tool.get("outputSchema") is not None:
                    _check_schema(tool["outputSchema"])
                description = tool.get("description", "")
                if not isinstance(description, str) or len(description) > 8192:
                    raise TaskError("mcp_invalid_schema", "invalid MCP tool description")
                annotations = tool.get("annotations") or {}
                if not isinstance(annotations, dict):
                    raise TaskError("mcp_invalid_schema", "invalid MCP tool annotations")
                hints = {key: value for key, value in annotations.items() if key in {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"} and type(value) is bool}
                tools[name] = {"schema": schema, "validator": validator, "description": description, "annotations": hints}
            cursor = result.get("nextCursor")
            if cursor is None:
                self.tools = tools
                return
            if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen:
                raise TaskError("mcp_invalid_schema", "invalid MCP tool pagination")
            seen.add(cursor)
        raise TaskError("mcp_invalid_schema", "MCP tool pagination exceeds its limit")

    async def _perform(self, session: Any, request: _Request):
        self.runtime._identity(request.identity)
        if request.operation == "list":
            await self._read_tools(session)
            return [{"server": self.server.id, "tool": name, "name": f"mcp__{self.server.id}__{name}",
                     "description": self.runtime._redact_text(tool["description"]),
                     "inputSchema": self.runtime._redact_tree(tool["schema"], redact_keys=False),
                     "annotations": dict(tool["annotations"])}
                    for name, tool in self.tools.items() if self.server.permits(request.identity, name)]
        if not self.server.permits(request.identity, request.name):
            raise TaskError("mcp_forbidden", "MCP tool is not allowed for this identity")
        tool = self.tools.get(request.name)
        if tool is None:
            raise TaskError("mcp_unknown_tool", "unknown MCP tool")
        try:
            tool["validator"].validate(request.arguments)
        except Exception:
            raise TaskError("mcp_invalid_arguments", "arguments do not match the MCP tool schema") from None
        return await session.call_tool(request.name, arguments=request.arguments)

    async def _serve(self):
        try:
            for attempt in range(2):
                self.exiting = False
                try:
                    async with AsyncExitStack() as stack:
                        try:
                            session = await self.runtime._open(self.server, stack)
                            await session.initialize()
                            await self._read_tools(session)
                            self.connected = True
                            if not self.ready.done():
                                self.ready.set_result(None)
                            while True:
                                request = await self.queue.get()
                                self.active = request
                                if request.future.cancelled():
                                    continue
                                remaining = request.deadline - asyncio.get_running_loop().time()
                                try:
                                    if remaining <= 0:
                                        raise asyncio.TimeoutError
                                    result = await asyncio.wait_for(self._perform(session, request), remaining)
                                except asyncio.CancelledError:
                                    if not request.future.done():
                                        request.future.set_exception(TaskError("mcp_disconnected", "MCP connection is closed"))
                                    raise
                                except asyncio.TimeoutError:
                                    if not request.future.done():
                                        request.future.set_exception(TaskError("mcp_timeout", "MCP operation timed out; it was not replayed"))
                                    return
                                except TaskError as error:
                                    if not request.future.done():
                                        request.future.set_exception(TaskError(error.code, str(error)))
                                except Exception:
                                    if not request.future.done():
                                        request.future.set_exception(TaskError("mcp_call_failed", "MCP operation failed; it was not replayed"))
                                    return
                                else:
                                    if not request.future.done():
                                        request.future.set_result(result)
                                finally:
                                    self.active = None
                        finally:
                            self.exiting = True
                            self.connected = False
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if self.ready.done():
                        return
                    if isinstance(error, TaskError) or attempt == 1:
                        self.ready.set_exception(TaskError(error.code, str(error)) if isinstance(error, TaskError) else TaskError("mcp_connection_failed", "MCP connection failed"))
                        return
        except asyncio.CancelledError:
            pass
        finally:
            self.connected = False
            self.exiting = True
            if not self.ready.done():
                self.ready.set_exception(TaskError("mcp_disconnected", "MCP connection is closed"))
                self.ready.exception()
            if self.active is not None and not self.active.future.done():
                self.active.future.set_exception(TaskError("mcp_disconnected", "MCP connection is closed"))
            while not self.queue.empty():
                request = self.queue.get_nowait()
                if not request.future.done():
                    request.future.set_exception(TaskError("mcp_disconnected", "MCP connection is closed"))


class MCPRuntime:
    def __init__(self, settings: Mapping[str, Any] | None, policy: TaskPolicy):
        raw = {} if settings is None else settings
        if not isinstance(raw, Mapping) or set(raw) - {"enabled", "servers"}:
            raise ValueError("MCP settings must contain only enabled and servers")
        if not isinstance(policy, TaskPolicy) or type(raw.get("enabled", False)) is not bool:
            raise ValueError("invalid MCP policy")
        servers = raw.get("servers", [])
        if not isinstance(servers, list) or len(servers) > 16:
            raise ValueError("MCP servers must be a bounded administrator configured list")
        parsed = [_Server.parse(item) for item in servers]
        if len({server.id for server in parsed}) != len(parsed):
            raise ValueError("duplicate MCP server id")
        self.enabled = policy.enabled and raw.get("enabled", False)
        self._servers = {server.id: server for server in parsed}
        self._connections: dict[str, _Connection] = {}
        self._sdk = None
        self._started = False
        self._closed = False
        self._lock = asyncio.Lock()
        secrets = {value for server in parsed for value in server.env.values() if value}
        for server in parsed:
            if server.url:
                secrets.add(server.url)
                secrets.update(value for _, value in parse_qsl(urlsplit(server.url).query) if value)
            for index, argument in enumerate(server.args):
                match = re.fullmatch(r"(?i)[-/]{1,2}(api[-_]?key|token|secret|password|authorization|cookie)(?:[=:](.*))?", argument)
                if match is not None:
                    secret = match.group(2) if match.group(2) is not None else (server.args[index + 1] if index + 1 < len(server.args) else "")
                    if secret:
                        secrets.add(secret)
        self._secrets = sorted(secrets, key=len, reverse=True)
        self._log_filter = _LogRedactor(self)
        self._log_targets: list[Any] = []

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()

    async def start(self):
        if self._closed:
            raise TaskError("mcp_closed", "MCP runtime is closed")
        if self._started:
            return
        self._started = True
        if not self.enabled or not any(server.enabled for server in self._servers.values()):
            return
        try:
            self._sdk = _load_sdk()
            self._log_targets = [logging.getLogger(name) for name in _LOG_NAMES]
            for logger in [logging.getLogger(), *self._log_targets]:
                for handler in logger.handlers:
                    if handler not in self._log_targets:
                        self._log_targets.append(handler)
            for target in self._log_targets:
                target.addFilter(self._log_filter)
            await asyncio.gather(*(self._connection(server) for server in self._servers.values() if server.enabled))
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self._closed = True
        connections = tuple(self._connections.values())
        if connections:
            await asyncio.gather(*(connection.stop() for connection in connections))
        self._connections.clear()
        for target in self._log_targets:
            target.removeFilter(self._log_filter)
        self._log_targets.clear()

    def _identity(self, identity: TaskIdentity):
        if not isinstance(identity, TaskIdentity):
            raise TaskError("mcp_forbidden", "MCP requires a verified task identity")

    async def _connection(self, server: _Server, deadline: float | None = None) -> _Connection:
        if self._closed or not self._started:
            raise TaskError("mcp_closed", "MCP runtime is not running")
        async with self._lock:
            connection = self._connections.get(server.id)
            if connection is None or connection.task.done():
                connection = _Connection(self, server)
                self._connections[server.id] = connection
        await connection.wait_ready(None if deadline is None else deadline - asyncio.get_running_loop().time())
        return connection

    async def _open(self, server: _Server, stack: AsyncExitStack):
        if server.transport == "stdio":
            errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            params = self._sdk.StdioServerParameters(command=server.command, args=list(server.args), env=dict(server.env))
            streams = await stack.enter_async_context(self._sdk.stdio_client(params, errlog=errlog))
        else:
            client = await stack.enter_async_context(self._sdk.AsyncClient(timeout=server.timeout_seconds, follow_redirects=False, trust_env=False))
            streams = await stack.enter_async_context(self._sdk.streamable_http_client(server.url, http_client=client, terminate_on_close=True))
        return await stack.enter_async_context(self._sdk.ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=server.timeout_seconds)))

    async def list_tools(self, identity: TaskIdentity) -> list[dict[str, Any]]:
        self._identity(identity)
        if not self.enabled:
            return []
        tools = []
        for server in self._servers.values():
            if server.enabled and server.allowed_tools and (identity.is_owner or server.allow_guests):
                connection = await self._connection(server)
                tools.extend(await connection.request("list", identity))
        return tools

    async def call_tool(self, identity: TaskIdentity, server: str, name: str, arguments: Mapping[str, Any], budget: MCPCallContext | None = None) -> dict[str, Any]:
        self._identity(identity)
        _name(server, server=True)
        config = self._servers.get(server)
        if config is None:
            raise TaskError("mcp_unknown_server", "unknown MCP server")
        prefix = f"mcp__{server}__"
        if isinstance(name, str) and name.startswith(prefix):
            name = name[len(prefix):]
        _name(name)
        if not self.enabled or not config.permits(identity, name):
            raise TaskError("mcp_forbidden", "MCP tool is not allowed for this identity")
        if budget is not None and (not isinstance(budget, MCPCallContext) or budget.identity != identity or not budget.confirmed):
            raise TaskError("mcp_unconfirmed_task", "MCP context does not match a confirmed root task")
        if not identity.is_owner and budget is None:
            raise TaskError("mcp_unconfirmed_task", "MCP guest calls require a confirmed root task")
        if not isinstance(arguments, Mapping):
            raise TaskError("mcp_invalid_arguments", "MCP arguments must be an object")
        arguments = json.loads(_json_bytes(dict(arguments), _MAX_SCHEMA_BYTES, "mcp_invalid_arguments"))
        duration = min(config.timeout_seconds, budget.timeout_seconds) if budget and budget.timeout_seconds is not None else config.timeout_seconds
        deadline = asyncio.get_running_loop().time() + duration
        connection = await self._connection(config, deadline)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TaskError("mcp_timeout", "MCP task budget has expired")
        result = await connection.request("call", identity, name, arguments, remaining)
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(self._result(config, name, result, budget), remaining)
        except asyncio.CancelledError:
            await connection.stop()
            raise
        except asyncio.TimeoutError:
            await connection.stop()
            raise TaskError("mcp_timeout", "MCP result handling exceeded the task budget") from None

    def _redact_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        text = _SECRET_TEXT.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", text)
        return re.sub(r"(?i)\bBearer\s+[^\s,'\";]+", "Bearer [REDACTED]", text)

    def _redact_tree(self, value: Any, depth: int = 0, *, redact_keys: bool = True):
        if depth > 64:
            raise TaskError("mcp_invalid_response", "MCP response is too complex")
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, list):
            return [self._redact_tree(item, depth + 1, redact_keys=redact_keys) for item in value]
        if isinstance(value, dict):
            return {key: "[REDACTED]" if redact_keys and _SECRET_KEY.fullmatch(key) else self._redact_tree(item, depth + 1, redact_keys=redact_keys) for key, item in value.items()}
        return value

    async def _artifact(self, data: bytes, mime_type: str, budget: MCPCallContext | None) -> dict[str, Any]:
        if budget is None or budget.artifact_writer is None:
            raise TaskError("mcp_artifact_required", "MCP binary or large results require a task artifact writer")
        if len(data) > _MAX_RESPONSE_BYTES:
            raise TaskError("mcp_result_too_large", "MCP artifact exceeds its hard limit")
        try:
            reference = await budget.artifact_writer(f"mcp-{uuid4().hex}", data, mime_type)
            if not isinstance(reference, Mapping) or not reference:
                raise ValueError("invalid artifact reference")
            reference = self._redact_tree(dict(reference))
            _json_bytes(reference, 4096, "mcp_artifact_failed")
            return reference
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TaskError("mcp_artifact_failed", "MCP artifact could not be stored") from None

    async def _result(self, server: _Server, name: str, result: Any, budget: MCPCallContext | None):
        raw = _mapping(result)
        _json_bytes(raw, _MAX_RESPONSE_BYTES, "mcp_result_too_large")
        content = raw.get("content", [])
        if not isinstance(content, list) or len(content) > 512 or type(raw.get("isError", False)) is not bool:
            raise TaskError("mcp_invalid_response", "invalid MCP result content")
        output = {"server": server.id, "tool": name, "isError": raw.get("isError", False), "content": []}
        for item in content:
            item = copy.deepcopy(_mapping(item))
            if item.get("type") not in {"text", "image", "audio", "resource", "resource_link"}:
                raise TaskError("mcp_invalid_response", "unsupported MCP content type")
            if item["type"] == "text" and not isinstance(item.get("text"), str):
                raise TaskError("mcp_invalid_response", "invalid MCP text content")
            if item["type"] in {"image", "audio"} and not isinstance(item.get("data"), str):
                raise TaskError("mcp_invalid_response", "invalid MCP binary content")
            binary, field_name = item, "data"
            if item["type"] == "resource":
                binary, field_name = item.get("resource"), "blob"
                if not isinstance(binary, dict) or not isinstance(binary.get("uri"), str) or ("blob" in binary) == ("text" in binary):
                    raise TaskError("mcp_invalid_response", "invalid MCP embedded resource")
                if "text" in binary and not isinstance(binary["text"], str):
                    raise TaskError("mcp_invalid_response", "invalid MCP resource text")
            if item["type"] == "resource_link" and not isinstance(item.get("uri"), str):
                raise TaskError("mcp_invalid_response", "invalid MCP resource link")
            if isinstance(binary, dict) and field_name in binary:
                encoded = binary.pop(field_name)
                try:
                    payload = base64.b64decode(encoded, validate=True)
                except (binascii.Error, TypeError, ValueError):
                    raise TaskError("mcp_invalid_response", "invalid MCP binary content") from None
                mime_type = binary.get("mimeType", "application/octet-stream")
                if not isinstance(mime_type, str) or len(mime_type) > 256:
                    raise TaskError("mcp_invalid_response", "invalid MCP MIME type")
                binary["artifact"] = await self._artifact(payload, mime_type, budget)
                binary["size_bytes"] = len(payload)
            output["content"].append(self._redact_tree(item))
        if "structuredContent" in raw:
            if not isinstance(raw["structuredContent"], dict):
                raise TaskError("mcp_invalid_response", "invalid MCP structured content")
            output["structuredContent"] = self._redact_tree(raw["structuredContent"])
        serialized = _json_bytes(output, _MAX_RESPONSE_BYTES, "mcp_result_too_large")
        if len(serialized) > server.max_result_bytes:
            artifact = await self._artifact(serialized, "application/json", budget)
            output = {"server": server.id, "tool": name, "isError": output["isError"], "content": [], "artifact": artifact, "size_bytes": len(serialized)}
        _json_bytes(output, server.max_result_bytes, "mcp_result_too_large")
        return output