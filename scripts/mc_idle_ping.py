"""Minecraft Server List Ping (SLP) 客户端，纯标准库手撸。

用法：
    online, players = ping_mc_server("localhost", 26843, timeout=5.0)

返回 (online, players_count)：
- online=False 表示连不上/解析失败/超时；调用方按"不算闲"处理。
- online=True 时 players_count 是 status 响应里 players.online 的值。

协议参考：https://wiki.vg/Server_List_Ping
"""
from __future__ import annotations

import json
import socket
import struct


_PROTOCOL_VERSION = 763  # 1.20.1+ 通用值；服务端通常忽略 ping 用的版本号


def _encode_varint(value: int) -> bytes:
    if value < 0:
        # Minecraft VarInt 把负数当 unsigned 32-bit 编码
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


def _read_varint(sock: socket.socket) -> int:
    result = 0
    for shift in range(0, 35, 7):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("EOF while reading VarInt")
        value = byte[0]
        result |= (value & 0x7F) << shift
        if not (value & 0x80):
            return result
    raise ValueError("VarInt too long (>5 bytes)")


def _read_exact(sock: socket.socket, length: int) -> bytes:
    buf = bytearray()
    while len(buf) < length:
        chunk = sock.recv(length - len(buf))
        if not chunk:
            raise ConnectionError(f"EOF while reading {length} bytes (got {len(buf)})")
        buf.extend(chunk)
    return bytes(buf)


def ping_mc_server(
    host: str,
    port: int,
    *,
    timeout: float = 5.0,
) -> tuple[bool, int]:
    """Send SLP handshake + status request, return (online, players_online).

    On any failure returns (False, 0). Caller should treat False as
    "MC unreachable" and play it safe (don't start training).
    """
    if not host or port <= 0 or port > 65535:
        return (False, 0)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            handshake_payload = (
                _encode_varint(_PROTOCOL_VERSION)
                + _encode_string(host)
                + struct.pack(">H", port)
                + _encode_varint(1)  # next state: 1 = status
            )
            sock.sendall(_encode_packet(0x00, handshake_payload))
            sock.sendall(_encode_packet(0x00, b""))  # status request

            _read_varint(sock)  # full packet length (unused)
            packet_id = _read_varint(sock)
            if packet_id != 0x00:
                return (False, 0)
            json_length = _read_varint(sock)
            if json_length <= 0 or json_length > 1_000_000:
                return (False, 0)
            json_bytes = _read_exact(sock, json_length)
            data = json.loads(json_bytes.decode("utf-8"))
    except (OSError, ConnectionError, ValueError, json.JSONDecodeError) as exc:  # noqa: BLE001
        return (False, 0)

    if not isinstance(data, dict):
        return (False, 0)
    players = data.get("players")
    if not isinstance(players, dict):
        return (False, 0)
    online = players.get("online")
    try:
        return (True, int(online or 0))
    except (TypeError, ValueError):
        return (False, 0)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ping a Minecraft server via SLP")
    parser.add_argument("host", help="MC server host")
    parser.add_argument("port", type=int, nargs="?", default=26843, help="MC server port (default 26843)")
    parser.add_argument("--timeout", type=float, default=5.0, help="socket timeout seconds")
    args = parser.parse_args()
    ok, count = ping_mc_server(args.host, args.port, timeout=args.timeout)
    if ok:
        print(f"online: {count} players")
    else:
        print(f"unreachable: {args.host}:{args.port}")
