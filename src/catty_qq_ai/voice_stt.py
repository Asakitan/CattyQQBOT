"""把 QQ 语音消息转成文字。

QQ 客户端的“语音转文字”只在客户端内部，OneBot/NapCat 不会把结果发过来；这里自己拿
音频（record 段 url / 本地路径 / get_record）再交给独立 faster-whisper 子进程转写。
任何一步失败都返回空串，由调用方决定如何提示，绝不编造内容。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"RIFF", "wav"),
    (b"#!AMR", "amr"),
    (b"#!SILK_V3", "silk"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
    (b"ID3", "mp3"),
)
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 600.0


def _setting(config: Any, field: str, default: str = "") -> str:
    return str(getattr(config, field, default) or default).strip()


def sniff_suffix(data: bytes) -> str:
    for magic, suffix in _SIGNATURES:
        if data.startswith(magic):
            return suffix
    # 无标签的 MPEG 帧同步 (0xFFEx)
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0:
        return "mp3"
    return "bin"


def inbox_dir(config: Any) -> Path:
    raw = _setting(config, "catty_voice_stt_dir", "data/voice_inbox")
    return Path(raw).expanduser() if raw else Path("data/voice_inbox")


def worker_script() -> Path:
    return Path(__file__).resolve().with_name("voice_stt_worker.py")


def format_block(text: str) -> str:
    clean = " ".join(str(text or "").split())
    if not clean:
        return "【语音】对方发来一条语音（未能识别内容）。"
    return f"【语音转写】{clean}"


async def _download(url: str, max_bytes: int) -> bytes:
    import httpx

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("voice file too large")
                chunks.append(chunk)
    return b"".join(chunks)


async def _resolve_audio(config: Any, bot: Any, segment: dict[str, Any], max_bytes: int) -> bytes:
    url = str(segment.get("url") or "").strip()
    if url.startswith("http"):
        return await _download(url, max_bytes)
    raw_file = str(segment.get("file") or "").strip()
    if raw_file:
        path = Path(raw_file).expanduser()
        if path.is_absolute() and path.is_file():
            if path.stat().st_size <= max_bytes:
                return await asyncio.to_thread(path.read_bytes)
            raise ValueError("voice file too large")
    if bot is not None and (raw_file or segment.get("file_id")):
        payload = {"file": raw_file or str(segment.get("file_id") or ""), "out_format": "wav"}
        result = await bot.call_api("get_record", **payload)
        returned = result.get("file") if isinstance(result, dict) else result
        if isinstance(returned, str) and returned.strip():
            path = Path(returned.strip())
            if path.is_file() and path.stat().st_size <= max_bytes:
                return await asyncio.to_thread(path.read_bytes)
    raise FileNotFoundError("no downloadable voice source")


async def _run_worker(config: Any, audio_path: Path) -> str:
    python = _setting(config, "catty_voice_stt_python") or sys.executable
    model = _setting(config, "catty_voice_stt_model")
    if not model:
        return ""
    seconds = float(getattr(config, "catty_voice_stt_timeout_seconds", 60.0) or 60.0)
    process = await asyncio.create_subprocess_exec(
        python,
        "-B",
        "-X",
        "utf8",
        str(worker_script()),
        "--audio",
        str(audio_path),
        "--model",
        model,
        "--language",
        _setting(config, "catty_voice_stt_language", "zh") or "zh",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=max(seconds, 5.0))
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return ""
    if process.returncode != 0 or not output:
        return ""
    try:
        payload = json.loads(output.decode("utf-8", errors="replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return ""
    if not isinstance(payload, dict) or not payload.get("ok"):
        return ""
    return " ".join(str(payload.get("text") or "").split())


async def transcribe_event(config: Any, bot: Any, segments: list[dict[str, Any]]) -> str:
    """转写一条消息里的第一个语音段; 失败返回空串。结果按音频哈希缓存 10 分钟。"""
    if not segments:
        return ""
    try:
        max_bytes = int(getattr(config, "catty_voice_stt_max_bytes", 8 * 1024 * 1024) or 8 * 1024 * 1024)
    except (TypeError, ValueError):
        max_bytes = 8 * 1024 * 1024
    segment = segments[0]
    try:
        data = await _resolve_audio(config, bot, segment, max_bytes)
    except Exception:  # noqa: BLE001
        return ""
    digest = hashlib.sha1(data).hexdigest()
    now = time.time()
    cached = _CACHE.get(digest)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    suffix = sniff_suffix(data)
    if suffix == "silk":
        # QQ 原生 SILK 需要专门解码器; 当前运行时没有, 直接放弃而不是喂给 whisper。
        return ""
    directory = inbox_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    audio_path = directory / f"voice-{digest[:12]}.{suffix}"
    if not audio_path.is_file():
        await asyncio.to_thread(audio_path.write_bytes, data)
    text = await _run_worker(config, audio_path)
    _CACHE[digest] = (now, text)
    return text
