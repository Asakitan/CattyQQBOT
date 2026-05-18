"""Async SLP probe for Minecraft server load status.

Thin asyncio version of scripts/mc_idle_ping.py, kept short and in-tree so
the bot's main event loop can ping the local MC server without blocking.
Result is cached for `cache_ttl_seconds` so 100 rapid lookups don't fan out
to 100 socket connects.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time


_PROTOCOL_VERSION = 763  # Generic value accepted by Spigot/Arclight/Vanilla


def _encode_varint(value: int) -> bytes:
    if value < 0:
        value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _encode_varint(len(encoded)) + encoded


def _encode_packet(packet_id: int, payload: bytes) -> bytes:
    body = _encode_varint(packet_id) + payload
    return _encode_varint(len(body)) + body


async def _read_varint(reader: asyncio.StreamReader) -> int:
    result = 0
    for shift in range(0, 35, 7):
        chunk = await reader.readexactly(1)
        value = chunk[0]
        result |= (value & 0x7F) << shift
        if not (value & 0x80):
            return result
    raise ValueError("VarInt too long (>5 bytes)")


async def _ping_once(host: str, port: int, timeout: float) -> tuple[bool, int]:
    """One-shot SLP probe. Returns (online_reachable, players_online).

    On any error returns (False, 0). False is treated as 'unknown / unsafe' by
    callers — they should err on the side of NOT running local models.
    """
    if not host or port <= 0 or port > 65535:
        return (False, 0)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return (False, 0)
    try:
        handshake = (
            _encode_varint(_PROTOCOL_VERSION)
            + _encode_string(host)
            + struct.pack(">H", port)
            + _encode_varint(1)
        )
        writer.write(_encode_packet(0x00, handshake))
        writer.write(_encode_packet(0x00, b""))
        await writer.drain()

        await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        packet_id = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        if packet_id != 0x00:
            return (False, 0)
        json_len = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        if json_len <= 0 or json_len > 1_000_000:
            return (False, 0)
        json_bytes = await asyncio.wait_for(reader.readexactly(json_len), timeout=timeout)
        data = json.loads(json_bytes.decode("utf-8"))
    except (asyncio.TimeoutError, OSError, ValueError, json.JSONDecodeError, asyncio.IncompleteReadError):
        return (False, 0)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    if not isinstance(data, dict):
        return (False, 0)
    players = data.get("players")
    if not isinstance(players, dict):
        return (False, 0)
    try:
        return (True, int(players.get("online") or 0))
    except (TypeError, ValueError):
        return (False, 0)


class MCStatusProbe:
    """Cached SLP probe. Pings at most once every `cache_ttl_seconds`."""

    def __init__(self, *, cache_ttl_seconds: float = 30.0) -> None:
        self._cache_ttl = max(cache_ttl_seconds, 0.0)
        self._lock = asyncio.Lock()
        self._cached_at: float = 0.0
        self._cached_result: tuple[bool, int] = (False, 0)
        self._cached_key: tuple[str, int] | None = None

    async def status(self, host: str, port: int, timeout: float = 3.0) -> tuple[bool, int]:
        now = time.monotonic()
        key = (host, port)
        if (
            self._cached_key == key
            and (now - self._cached_at) < self._cache_ttl
        ):
            return self._cached_result
        async with self._lock:
            now = time.monotonic()
            if (
                self._cached_key == key
                and (now - self._cached_at) < self._cache_ttl
            ):
                return self._cached_result
            result = await _ping_once(host, port, timeout)
            self._cached_at = time.monotonic()
            self._cached_result = result
            self._cached_key = key
            return result

    def invalidate(self) -> None:
        self._cached_at = 0.0


# Module-level singleton used by chat_completion's fallback gate.
_default_probe = MCStatusProbe(cache_ttl_seconds=30.0)


async def mc_has_players(host: str, port: int, *, timeout: float = 3.0) -> bool:
    """True iff MC is reachable AND online > 0. Unknown = False (treat as no-players)."""
    online, players = await _default_probe.status(host, port, timeout=timeout)
    return online and players > 0


async def mc_is_reachable(host: str, port: int, *, timeout: float = 3.0) -> bool:
    online, _ = await _default_probe.status(host, port, timeout=timeout)
    return online


def reset_cache_for_tests() -> None:
    _default_probe.invalidate()
