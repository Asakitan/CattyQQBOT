"""OpenAI Responses API WebSocket 传输。

协议（官方 WebSocket mode）：
- 连接 ``wss://api.openai.com/v1/responses``，发送 ``response.create`` 帧。
- 帧体与 HTTP Responses create body 相同，但 ``stream`` / ``background`` 不适用。
- 同一连接可顺序接收多个 ``response.create``；用 ``stream_id`` 区分并行 lane。
- 续聊：``previous_response_id`` + 只发新增 input items。
- ``store=false`` 时续聊状态只在该连接的本地内存缓存里；连接断开后必须用完整
  input 重放。``store=true`` 时可由服务端持久状态恢复。
- 终态事件：``response.completed`` / ``response.failed`` / ``response.incomplete``；
  错误帧为 ``{"type": "error", ...}``。

本模块只负责传输与本地 response-id 缓存；chat-completions 形状转换仍在
``openai_client`` 里完成。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("catty.responses_ws")

try:  # websockets >= 12
    from websockets.asyncio.client import connect as _ws_connect
    from websockets.exceptions import ConnectionClosed as _ConnectionClosed
except Exception:  # pragma: no cover - optional dependency
    _ws_connect = None  # type: ignore[assignment]

    try:
        from websockets.exceptions import ConnectionClosed as _ConnectionClosed
    except Exception:

        class _ConnectionClosed(Exception):  # type: ignore[no-redef]
            pass



class ResponsesWSError(RuntimeError):
    """Responses WebSocket 请求失败。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class ResponsesWSDisconnected(ResponsesWSError):
    """连接在终态事件之前断开。"""


class _Pending:
    __slots__ = ("queue",)

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()


def responses_ws_url(base_url: str) -> str:
    """把 HTTP(S) base_url 转成 Responses WebSocket URL。"""
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    path = parsed.path or ""
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")] + "/responses"
    elif not path.endswith("/responses"):
        path = path.rstrip("/") + "/responses"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def item_hash(item: Any) -> str:
    """续聊内容指纹：忽略输入块的缓存断点，保留其它所有字段。

    滚动断点的增删不改变模型看到的消息，不能因此废弃 append-only 续聊。
    仅处理协议内容块，不删除正文或工具参数中同名的业务数据。
    """
    normalized = item
    if isinstance(item, dict):
        item_type = item.get("type", "message")
        field = "content" if item_type == "message" else "output"
        parts = item.get(field)
        if item_type in {"message", "function_call_output"} and isinstance(parts, list):
            normalized_parts = [
                {key: value for key, value in part.items() if key != "prompt_cache_breakpoint"}
                if isinstance(part, dict)
                and part.get("type") in {"input_text", "input_image", "input_file"}
                and "prompt_cache_breakpoint" in part
                else part
                for part in parts
            ]
            normalized = {**item, field: normalized_parts}
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def common_prefix_len(left: list[str], right: list[str]) -> int:
    matched = 0
    for a, b in zip(left, right):
        if a != b:
            break
        matched += 1
    return matched


class ResponseChainCache:
    """按 scope 保存 previous_response_id 与上一轮 input item 指纹。

    ``store=false`` 时 response id 只对生成它的连接有效，因此 generation 不匹配
    就不续聊；``store=true`` 时允许跨连接/重启续聊。
    """

    def __init__(self, path: str | Path, ttl_seconds: float) -> None:
        self._path = Path(path) if str(path or "").strip() else Path(
            "data/responses_ws_cache.json"
        )
        self._ttl = max(float(ttl_seconds or 0.0), 0.0)
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for scope, entry in raw.items():
            if not isinstance(scope, str) or not isinstance(entry, dict):
                continue
            try:
                ts = float(entry.get("ts") or 0.0)
            except (TypeError, ValueError):
                continue
            if self._ttl > 0 and now - ts > self._ttl:
                continue
            response_id = str(entry.get("response_id") or "")
            hashes = entry.get("item_hashes")
            if not response_id or not isinstance(hashes, list):
                continue
            self._entries[scope] = {
                "response_id": response_id,
                "item_hashes": [str(value) for value in hashes],
                "generation": int(entry.get("generation") or 0),
                "store": bool(entry.get("store")),
                "ts": ts,
            }

    def get(self, scope: str) -> dict[str, Any] | None:
        if not scope:
            return None
        entry = self._entries.get(scope)
        if entry is None:
            return None
        if self._ttl > 0 and time.time() - float(entry.get("ts") or 0.0) > self._ttl:
            self._entries.pop(scope, None)
            self._write()
            return None
        return entry

    def put(
        self,
        scope: str,
        *,
        response_id: str,
        item_hashes: list[str],
        generation: int,
        store: bool,
    ) -> None:
        if not scope or not response_id:
            return
        self._entries[scope] = {
            "response_id": str(response_id),
            "item_hashes": [str(value) for value in item_hashes],
            "generation": int(generation),
            "store": bool(store),
            "ts": time.time(),
        }
        self._write()

    def clear(self, scope: str) -> None:
        if scope and self._entries.pop(scope, None) is not None:
            self._write()

    def _write(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            staging = self._path.with_name(f"{self._path.name}.tmp")
            staging.write_text(
                json.dumps(self._entries, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(staging, self._path)
        except OSError as exc:  # pragma: no cover - best effort cache
            logger.warning("responses ws cache write failed: %s", exc)


class ResponsesWebSocketTransport:
    """一个到 ``/v1/responses`` 的持久连接，按 lane 串行发送。"""

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        extra_headers: dict[str, str] | None = None,
        connect_timeout: float = 15.0,
        idle_timeout: float = 300.0,
        max_age_seconds: float = 3000.0,
        proxy: str = "",
        max_frame_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if _ws_connect is None:
            raise ResponsesWSError(
                "websockets 未安装；安装 websockets>=16 或关闭 Responses WebSocket"
            )
        self._url = url
        self._api_key = str(api_key or "").strip()
        self._extra_headers = dict(extra_headers or {})
        self._connect_timeout = max(float(connect_timeout or 15.0), 1.0)
        self._idle_timeout = max(float(idle_timeout or 300.0), 1.0)
        self._max_age = max(float(max_age_seconds or 3000.0), 1.0)
        self._proxy = str(proxy or "").strip()
        self._max_frame_bytes = int(max_frame_bytes)
        self._ws: Any = None
        self._reader: asyncio.Task[Any] | None = None
        self._opened_at = 0.0
        self._generation = uuid.uuid4().int
        self._closed = False
        self._connect_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._pending: dict[str, _Pending] = {}
        self._lane_locks: dict[str, asyncio.Lock] = {}

    @property
    def generation(self) -> int:
        return self._generation

    async def ensure_ready(self) -> int:
        """确保连接已建立，返回当前 connection generation。"""
        await self._ensure_connection()
        return self._generation

    async def _lane_lock(self, lane: str) -> asyncio.Lock:
        lock = self._lane_locks.get(lane)
        if lock is None:
            lock = asyncio.Lock()
            self._lane_locks[lane] = lock
        return lock

    async def _connect(self) -> Any:
        headers = [("Authorization", f"Bearer {self._api_key}")]
        for name, value in self._extra_headers.items():
            if name.lower() == "authorization":
                continue
            headers.append((str(name), str(value)))
        kwargs: dict[str, Any] = {
            "additional_headers": headers,
            "open_timeout": self._connect_timeout,
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": self._max_frame_bytes,
            "proxy": self._proxy or None,
        }
        try:
            return await _ws_connect(self._url, **kwargs)
        except TypeError:
            # 旧版 websockets 用 extra_headers 且没有 proxy 参数。
            kwargs.pop("additional_headers", None)
            kwargs.pop("proxy", None)
            return await _ws_connect(self._url, extra_headers=headers, **{
                key: value
                for key, value in kwargs.items()
                if key in {"open_timeout", "ping_interval", "ping_timeout", "max_size"}
            })

    async def _ensure_connection(self) -> None:
        async with self._connect_lock:
            if self._closed:
                raise ResponsesWSDisconnected("responses websocket 已关闭")
            now = time.monotonic()
            if self._ws is not None and now - self._opened_at < self._max_age:
                return
            await self._drop_connection("连接到期，准备重连")
            try:
                ws = await self._connect()
            except Exception as exc:  # noqa: BLE001
                raise ResponsesWSDisconnected(
                    f"responses websocket 连接失败: {exc.__class__.__name__}: {exc}"
                ) from exc
            self._ws = ws
            self._opened_at = time.monotonic()
            self._generation += 1
            self._reader = asyncio.create_task(
                self._read_loop(ws, self._generation),
                name="catty-responses-ws-reader",
            )

    async def _drop_connection(self, reason: str) -> None:
        ws = self._ws
        self._ws = None
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._fail_pending(reason)

    def _fail_pending(self, reason: str) -> None:
        event = {"type": "_catty_disconnect", "error": reason}
        for pending in list(self._pending.values()):
            try:
                pending.queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover
                pass

    async def _pending_for_lane(self, lane: str) -> _Pending | None:
        async with self._pending_lock:
            pending = self._pending.get(lane)
            if pending is None and not lane and len(self._pending) == 1:
                pending = next(iter(self._pending.values()))
            return pending

    async def _read_loop(self, ws: Any, generation: int) -> None:
        failure = "responses websocket 连接关闭"
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                lane = str(event.get("stream_id") or "")
                pending = await self._pending_for_lane(lane)
                if pending is None:
                    logger.debug("responses ws 未路由事件: %s", event.get("type"))
                    continue
                await pending.queue.put(event)
        except asyncio.CancelledError:
            raise
        except _ConnectionClosed as exc:
            failure = f"responses websocket 连接关闭: {exc}"
        except Exception as exc:  # noqa: BLE001
            failure = f"responses websocket 读取失败: {exc.__class__.__name__}: {exc}"
        finally:
            if self._ws is ws:
                self._ws = None
            if self._generation == generation:
                self._fail_pending(failure)

    async def iter_events(
        self,
        body: dict[str, Any],
        *,
        stream_id: str = "",
        previous_response_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """发送一帧 ``response.create`` 并逐条产出服务端事件。"""
        lane = str(stream_id or "")
        lock = await self._lane_lock(lane)
        async with lock:
            await self._ensure_connection()
            ws = self._ws
            if ws is None:
                raise ResponsesWSDisconnected("responses websocket 未连接")
            pending = _Pending()
            async with self._pending_lock:
                if lane in self._pending:
                    raise ResponsesWSError(f"responses websocket lane 忙: {lane!r}")
                self._pending[lane] = pending
            frame: dict[str, Any] = {"type": "response.create", **body}
            if lane:
                frame["stream_id"] = lane
            if previous_response_id:
                frame["previous_response_id"] = previous_response_id
            terminal = False
            try:
                try:
                    await ws.send(json.dumps(frame, ensure_ascii=False))
                except Exception as exc:  # noqa: BLE001
                    raise ResponsesWSDisconnected(
                        f"responses websocket 发送失败: {exc.__class__.__name__}: {exc}"
                    ) from exc
                while True:
                    try:
                        event = await asyncio.wait_for(
                            pending.queue.get(),
                            timeout=self._idle_timeout,
                        )
                    except asyncio.TimeoutError as exc:
                        raise ResponsesWSDisconnected(
                            f"responses websocket 等待事件超时 ({self._idle_timeout:.0f}s)"
                        ) from exc
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "_catty_disconnect":
                        raise ResponsesWSDisconnected(
                            str(event.get("error") or "responses websocket 断开")
                        )
                    terminal = event.get("type") in {
                        "response.completed",
                        "response.failed",
                        "response.incomplete",
                        "error",
                    }
                    yield event
                    if terminal:
                        break
            finally:
                async with self._pending_lock:
                    if self._pending.get(lane) is pending:
                        self._pending.pop(lane, None)
                if not terminal and self._ws is ws:
                    await self._drop_connection("请求未完成，关闭连接以丢弃迟到事件")

    async def close(self) -> None:
        self._closed = True
        await self._drop_connection("responses websocket 已关闭")
        self._lane_locks.clear()


__all__ = [
    "ResponseChainCache",
    "ResponsesWSError",
    "ResponsesWSDisconnected",
    "ResponsesWebSocketTransport",
    "common_prefix_len",
    "item_hash",
    "responses_ws_url",
]
