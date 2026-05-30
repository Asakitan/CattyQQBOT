#!/usr/bin/env python3
"""ssh-mcp: stdio MCP server that drives a remote Linux box over SSH (paramiko).

Built for the Catty proxy box (Singapore Aliyun, gost socks5). Lets Claude run
shell commands + transfer files on the proxy without hand-writing paramiko
scripts each time.

Talks JSON-RPC 2.0 over stdin/stdout (newline-delimited), implementing the MCP
subset Claude Code needs: initialize / notifications/initialized / tools/list /
tools/call / ping. No MCP SDK dependency — only paramiko.

Credentials are read (in priority order):
  1. env: SSHMCP_HOST / SSHMCP_PORT / SSHMCP_USER / SSHMCP_PASSWORD / SSHMCP_KEYFILE
  2. JSON file at $SSHMCP_SECRET or scripts/.ssh_mcp_secret.json
     {"host": "...", "port": 22, "user": "root", "password": "...", "keyfile": null}

The secret file is gitignored — never commit the password.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
import time
from typing import Any

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.stderr.write("ssh-mcp: paramiko not installed (pip install paramiko)\n")
    raise

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ssh-mcp"
SERVER_VERSION = "1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SECRET = os.path.join(HERE, ".ssh_mcp_secret.json")


# ──────────────────────────── config ────────────────────────────
def load_conf() -> dict[str, Any]:
    conf: dict[str, Any] = {"host": None, "port": 22, "user": "root",
                            "password": None, "keyfile": None}
    secret_path = os.environ.get("SSHMCP_SECRET", DEFAULT_SECRET)
    if os.path.isfile(secret_path):
        try:
            with open(secret_path, encoding="utf-8") as f:
                conf.update({k: v for k, v in json.load(f).items() if v is not None})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"ssh-mcp: bad secret file {secret_path}: {exc}\n")
    env = os.environ
    if env.get("SSHMCP_HOST"):     conf["host"] = env["SSHMCP_HOST"]
    if env.get("SSHMCP_PORT"):     conf["port"] = int(env["SSHMCP_PORT"])
    if env.get("SSHMCP_USER"):     conf["user"] = env["SSHMCP_USER"]
    if env.get("SSHMCP_PASSWORD"): conf["password"] = env["SSHMCP_PASSWORD"]
    if env.get("SSHMCP_KEYFILE"):  conf["keyfile"] = env["SSHMCP_KEYFILE"]
    return conf


CONF = load_conf()


# ──────────────────────────── ssh connection (lazy, auto-reconnect) ────────────────────────────
_client: paramiko.SSHClient | None = None
_lock = threading.Lock()


def _connect() -> paramiko.SSHClient:
    global _client
    with _lock:
        if _client is not None:
            tr = _client.get_transport()
            if tr is not None and tr.is_active():
                return _client
            try:
                _client.close()
            except Exception:  # noqa: BLE001
                pass
            _client = None
        if not CONF.get("host"):
            raise RuntimeError("ssh-mcp: no host configured (set SSHMCP_HOST or secret file)")
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = dict(
            hostname=CONF["host"], port=int(CONF.get("port", 22)),
            username=CONF.get("user", "root"), timeout=15,
            banner_timeout=15, auth_timeout=15, allow_agent=False, look_for_keys=False,
        )
        if CONF.get("keyfile"):
            kwargs["key_filename"] = CONF["keyfile"]
        if CONF.get("password"):
            kwargs["password"] = CONF["password"]
        cli.connect(**kwargs)
        tr = cli.get_transport()
        if tr is not None:
            tr.set_keepalive(30)
        _client = cli
        return _client


def _sftp():
    return _connect().open_sftp()


# ──────────────────────────── tool implementations ────────────────────────────
def t_exec(args: dict[str, Any]) -> str:
    cmd = args["command"]
    timeout = float(args.get("timeout", 60))
    cwd = args.get("cwd")
    if cwd:
        cmd = f"cd {json.dumps(cwd)} && ( {cmd} )"
    cli = _connect()
    t0 = time.monotonic()
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout, get_pty=False)
    chan = stdout.channel
    chan.settimeout(timeout)
    out_b, err_b = b"", b""
    try:
        out_b = stdout.read()
        err_b = stderr.read()
        rc = chan.recv_exit_status()
        timed_out = False
    except socket.timeout:
        timed_out = True
        rc = -1
        try:
            chan.close()
        except Exception:  # noqa: BLE001
            pass
    el = time.monotonic() - t0
    out = out_b.decode("utf-8", "replace")
    err = err_b.decode("utf-8", "replace")
    cap = 200_000
    out_trunc = len(out) > cap
    err_trunc = len(err) > cap
    return json.dumps({
        "exit_code": rc, "timed_out": timed_out, "duration_s": round(el, 3),
        "stdout": out[:cap], "stdout_truncated": out_trunc,
        "stderr": err[:cap], "stderr_truncated": err_trunc,
    }, ensure_ascii=False)


def t_read_file(args: dict[str, Any]) -> str:
    path = args["path"]
    max_bytes = int(args.get("max_bytes", 1_048_576))
    max_bytes = min(max_bytes, 1_048_576)
    sftp = _sftp()
    try:
        with sftp.open(path, "rb") as f:
            data = f.read(max_bytes + 1)
    finally:
        sftp.close()
    trunc = len(data) > max_bytes
    text = data[:max_bytes].decode(args.get("encoding", "utf-8"), "replace")
    return json.dumps({"path": path, "bytes": len(data[:max_bytes]),
                       "truncated": trunc, "content": text}, ensure_ascii=False)


def t_write_file(args: dict[str, Any]) -> str:
    path = args["path"]
    content = args["content"]
    append = bool(args.get("append", False))
    data = content.encode(args.get("encoding", "utf-8"))
    sftp = _sftp()
    try:
        mode = "ab" if append else "wb"
        with sftp.open(path, mode) as f:
            f.write(data)
    finally:
        sftp.close()
    return json.dumps({"path": path, "bytes_written": len(data), "append": append})


def t_upload(args: dict[str, Any]) -> str:
    local = args["local_path"]
    remote = args["remote_path"]
    sftp = _sftp()
    try:
        sftp.put(local, remote)
        st = sftp.stat(remote)
    finally:
        sftp.close()
    return json.dumps({"local": local, "remote": remote, "bytes": st.st_size})


def t_download(args: dict[str, Any]) -> str:
    remote = args["remote_path"]
    local = args["local_path"]
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    sftp = _sftp()
    try:
        sftp.get(remote, local)
    finally:
        sftp.close()
    return json.dumps({"remote": remote, "local": local,
                       "bytes": os.path.getsize(local)})


def t_list(args: dict[str, Any]) -> str:
    path = args.get("path", ".")
    sftp = _sftp()
    try:
        entries = []
        for a in sftp.listdir_attr(path):
            entries.append({
                "name": a.filename,
                "size": a.st_size,
                "dir": stat.S_ISDIR(a.st_mode) if a.st_mode else False,
                "mtime": a.st_mtime,
            })
    finally:
        sftp.close()
    entries.sort(key=lambda e: (not e["dir"], e["name"]))
    return json.dumps({"path": path, "count": len(entries), "entries": entries},
                      ensure_ascii=False)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "ssh_exec",
        "description": ("Run a shell command on the remote Linux proxy box over SSH and "
                        "wait for it to finish. Returns exit_code, stdout, stderr, "
                        "duration_s, timed_out. Working directory does NOT persist between "
                        "calls; pass `cwd` or chain with &&."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command (bash)"},
                "timeout": {"type": "number", "description": "Timeout seconds (default 60)"},
                "cwd": {"type": "string", "description": "Optional working directory"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "ssh_read_file",
        "description": "Read a remote text file (capped 1MB). Returns content + truncated flag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "description": "Max bytes (cap 1MB)"},
                "encoding": {"type": "string", "description": "default utf-8"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "ssh_write_file",
        "description": "Write/append a remote text file via SFTP.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean", "description": "append instead of overwrite"},
                "encoding": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "ssh_upload",
        "description": "Upload a local file to the remote box via SFTP (binary safe).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_path": {"type": "string"},
                "remote_path": {"type": "string"},
            },
            "required": ["local_path", "remote_path"],
        },
    },
    {
        "name": "ssh_download",
        "description": "Download a remote file to a local path via SFTP (binary safe).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_path": {"type": "string"},
                "local_path": {"type": "string"},
            },
            "required": ["remote_path", "local_path"],
        },
    },
    {
        "name": "ssh_list",
        "description": "List a remote directory (name/size/dir/mtime).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "default '.'"}},
        },
    },
]

DISPATCH = {
    "ssh_exec": t_exec,
    "ssh_read_file": t_read_file,
    "ssh_write_file": t_write_file,
    "ssh_upload": t_upload,
    "ssh_download": t_download,
    "ssh_list": t_list,
}


# ──────────────────────────── JSON-RPC stdio loop ────────────────────────────
def send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(rid: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def err(rid: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(req: dict[str, Any]) -> None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        reply(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return
    if method in ("notifications/initialized", "initialized"):
        return
    if method == "ping":
        reply(rid, {})
        return
    if method == "tools/list":
        reply(rid, {"tools": TOOLS})
        return
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if fn is None:
            err(rid, -32601, f"unknown tool: {name}")
            return
        try:
            text = fn(args)
            reply(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # noqa: BLE001
            reply(rid, {"content": [{"type": "text",
                                      "text": f"{type(exc).__name__}: {exc}"}],
                        "isError": True})
        return
    if rid is not None:
        err(rid, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"ssh-mcp handle error: {exc}\n")


if __name__ == "__main__":
    main()
