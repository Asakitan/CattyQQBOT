"""短语音回复: 预渲染语音库选择 + 可选本地 TTS worker 客户端。

设计边界:
- 不依赖 nonebot / torch, 可被 tools 与测试直接导入;
- 语音库是随源码部署的离线片段, 默认后端, 秒级返回;
- worker 后端在独立子进程里加载 GPT-SoVITS, 失败或未配置时退回语音库;
- 冷却与概率在本模块集中管理, 让"偶尔"不依赖模型自觉。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

MOODS = ("shy", "tease", "soft", "happy", "tsundere")
_DEFAULT_BANK_DIR = Path(__file__).resolve().parent / "data" / "voice_bank"
_BANK_CACHE: dict[tuple[str, int], list["VoiceClip"]] = {}
_LAST_CLIP: dict[str, str] = {}
_COOLDOWNS: dict[str, float] = {}


@dataclass(frozen=True)
class VoiceClip:
    clip_id: str
    mood: str
    text: str
    path: Path
    seconds: float
    sha256: str


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            result.update(chunk)
    return result.hexdigest()


def voice_enabled(config: Any) -> bool:
    return bool(getattr(config, "catty_voice_enabled", True))


def persona_name(persona: Any) -> str:
    if persona is None:
        return ""
    return str(getattr(persona, "name", "") or "").strip()


def persona_allowed(config: Any, persona: Any) -> bool:
    allowed = getattr(config, "catty_voice_personas", None)
    names = {str(item).strip() for item in (allowed or []) if str(item).strip()}
    if not names:
        return False
    return persona_name(persona) in names


def backend(config: Any) -> str:
    value = str(getattr(config, "catty_voice_backend", "bank") or "bank").strip().lower()
    return value if value in {"bank", "worker"} else "bank"


def bank_dir(config: Any, persona: Any = None) -> Path:
    raw = str(getattr(config, "catty_voice_bank_dir", "") or "").strip()
    base = Path(raw).expanduser() if raw else _DEFAULT_BANK_DIR
    name = persona_name(persona)
    if name and (base / name).is_dir():
        return base / name
    default_persona = base / "fadianji"
    if name and not (base / name).is_dir() and default_persona.is_dir():
        return base
    return base


def _load_manifest(directory: Path) -> list[VoiceClip]:
    manifest = directory / "bank.json"
    if not manifest.is_file():
        return []
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    clips: list[VoiceClip] = []
    for entry in raw.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("file") or "").strip()
        mood = str(entry.get("mood") or "").strip().lower()
        text = str(entry.get("text") or "").strip()
        if not name or mood not in MOODS or not text:
            continue
        path = (directory / name).resolve()
        if not path.is_file():
            continue
        clips.append(
            VoiceClip(
                clip_id=str(entry.get("id") or path.stem),
                mood=mood,
                text=text,
                path=path,
                seconds=float(entry.get("seconds") or 0.0),
                sha256=str(entry.get("sha256") or ""),
            )
        )
    return clips


def load_bank(config: Any, persona: Any = None, *, verify: bool = False) -> list[VoiceClip]:
    directory = bank_dir(config, persona)
    manifest = directory / "bank.json"
    if not manifest.is_file():
        return []
    key = (str(directory), manifest.stat().st_mtime_ns)
    clips = _BANK_CACHE.get(key)
    if clips is None:
        clips = _load_manifest(directory)
        _BANK_CACHE[key] = clips
    if verify:
        clips = [clip for clip in clips if clip.sha256 and digest(clip.path) == clip.sha256]
    return clips


def tool_available(config: Any, persona: Any = None) -> bool:
    if not voice_enabled(config):
        return False
    if persona is not None and not persona_allowed(config, persona):
        return False
    if backend(config) == "worker" and worker_configured(config):
        return True
    return bool(load_bank(config, persona))


def cooldown_remaining(config: Any, scope: str) -> float:
    try:
        seconds = float(getattr(config, "catty_voice_cooldown_seconds", 300.0) or 0.0)
    except (TypeError, ValueError):
        seconds = 300.0
    if seconds <= 0 or not scope:
        return 0.0
    last = _COOLDOWNS.get(scope, 0.0)
    return max(last + seconds - time.monotonic(), 0.0)


def reserve_slot(
    config: Any,
    scope: str,
    *,
    rng: Callable[[], float] | None = None,
    is_owner: bool = False,
) -> tuple[bool, str]:
    """占用一次发声机会: 冷却 + 概率。被概率挡下时同样进入冷却, 避免同一轮反复重试。

    主人不受冷却和概率限制 (主人 2026-10-29); 其他人格/群友每 5 分钟最多一条。
    """
    if is_owner:
        _COOLDOWNS[scope] = time.monotonic()
        return True, ""
    remaining = cooldown_remaining(config, scope)
    if remaining > 0:
        return False, f"语音冷却剩 {int(remaining)} 秒, 这轮只用文字"
    try:
        chance = float(getattr(config, "catty_voice_chance", 0.6))
    except (TypeError, ValueError):
        chance = 0.6
    chance = min(max(chance, 0.0), 1.0)
    draw = (rng or random.random)()
    _COOLDOWNS[scope] = time.monotonic()
    if draw >= chance:
        return False, "这次先只发文字, 不要重复调用语音工具"
    return True, ""


def mood_of(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in MOODS:
        return raw
    aliases = {
        "害羞": "shy", "撒娇": "shy", "羞": "shy",
        "调侃": "tease", "撩": "tease", "调情": "tease",
        "温柔": "soft", "软": "soft", "轻声": "soft",
        "开心": "happy", "高兴": "happy", "得意": "happy",
        "傲娇": "tsundere", "嘴硬": "tsundere",
    }
    return aliases.get(raw, "soft")


def select_clip(clips: list[VoiceClip], mood: str, scope: str) -> VoiceClip | None:
    if not clips:
        return None
    mood = mood if mood in MOODS else ""
    pool = [clip for clip in clips if clip.mood == mood] or list(clips)
    last = _LAST_CLIP.get(scope)
    if len(pool) > 1:
        pool = [clip for clip in pool if clip.clip_id != last] or pool
    chosen = random.choice(pool)
    _LAST_CLIP[scope] = chosen.clip_id
    return chosen


def clip_payload(clip: VoiceClip) -> dict[str, Any]:
    return {
        "path": str(clip.path),
        "text": clip.text,
        "mood": clip.mood,
        "seconds": clip.seconds,
    }


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _worker_setting(config: Any, field: str, default: str = "") -> str:
    return str(getattr(config, field, default) or default).strip()


def worker_configured(config: Any) -> bool:
    required = (
        "catty_voice_repo_dir",
        "catty_voice_gpt_path",
        "catty_voice_sovits_path",
        "catty_voice_reference_audio",
        "catty_voice_reference_text",
    )
    return all(_worker_setting(config, field) for field in required)


def outbox_dir(config: Any) -> Path:
    raw = _worker_setting(config, "catty_voice_outbox_dir", "data/voice_outbox")
    return Path(raw).expanduser() if raw else Path("data/voice_outbox")


def worker_script(config: Any) -> Path:
    raw = _worker_setting(config, "catty_voice_worker_script")
    return Path(raw).expanduser() if raw else Path(__file__).resolve().with_name("voice_worker.py")


def _free_memory_mb() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys // (1024 * 1024))
    except Exception:  # noqa: BLE001
        return None


def validate_audio(path: Path, *, min_seconds: float = 0.4, max_seconds: float = 20.0) -> float:
    with wave.open(str(path), "rb") as handle:
        frames, rate = handle.getnframes(), handle.getframerate()
        channels = handle.getnchannels()
    if rate <= 0 or frames <= 0:
        raise ValueError("empty audio")
    seconds = frames / float(rate)
    if channels != 1:
        raise ValueError(f"expected mono audio, got {channels} channel(s)")
    if not min_seconds <= seconds <= max_seconds:
        raise ValueError(f"unexpected audio duration {seconds:.2f}s")
    return seconds


@dataclass
class _Worker:
    process: asyncio.subprocess.Process
    log_handle: Any
    signature: str
    idle_task: asyncio.Task | None = None


_WORKER: _Worker | None = None
_WORKER_LOCK: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _WORKER_LOCK
    if _WORKER_LOCK is None:
        _WORKER_LOCK = asyncio.Lock()
    return _WORKER_LOCK


def _worker_signature(config: Any) -> str:
    parts = [
        _worker_setting(config, "catty_voice_python") or sys.executable,
        str(worker_script(config)),
        _worker_setting(config, "catty_voice_repo_dir"),
        _worker_setting(config, "catty_voice_gpt_path"),
        _worker_setting(config, "catty_voice_sovits_path"),
        _worker_setting(config, "catty_voice_bert_dir"),
        _worker_setting(config, "catty_voice_hubert_dir"),
        _worker_setting(config, "catty_voice_reference_audio"),
        _worker_setting(config, "catty_voice_reference_text"),
        str(int(_as_float(getattr(config, "catty_voice_worker_threads", 2), 2))),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _worker_config_path(config: Any) -> Path:
    directory = outbox_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"_worker-{_worker_signature(config)}.json"
    payload = {
        "repo_dir": _worker_setting(config, "catty_voice_repo_dir"),
        "gpt_path": _worker_setting(config, "catty_voice_gpt_path"),
        "sovits_path": _worker_setting(config, "catty_voice_sovits_path"),
        "bert_dir": _worker_setting(config, "catty_voice_bert_dir"),
        "hubert_dir": _worker_setting(config, "catty_voice_hubert_dir"),
        "reference_audio": _worker_setting(config, "catty_voice_reference_audio"),
        "reference_text": _worker_setting(config, "catty_voice_reference_text"),
        "threads": int(_as_float(getattr(config, "catty_voice_worker_threads", 2), 2)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if not path.is_file() or path.read_text(encoding="utf-8") != encoded:
        path.write_text(encoded, encoding="utf-8")
    return path


async def _stop_worker() -> None:
    global _WORKER
    worker = _WORKER
    _WORKER = None
    if worker is None:
        return
    if worker.idle_task is not None and not worker.idle_task.done():
        worker.idle_task.cancel()
    process = worker.process
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None and not stream.is_closing():
                stream.close()
        except Exception:  # noqa: BLE001
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=10.0)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    try:
        worker.log_handle.close()
    except Exception:  # noqa: BLE001
        pass


async def _schedule_idle_unload(config: Any) -> None:
    seconds = _as_float(getattr(config, "catty_voice_worker_idle_unload_seconds", 180.0), 180.0)
    if seconds <= 0:
        return
    worker = _WORKER
    if worker is None:
        return
    if worker.idle_task is not None and not worker.idle_task.done():
        worker.idle_task.cancel()

    async def _unload() -> None:
        try:
            await asyncio.sleep(seconds)
            async with _lock():
                if _WORKER is worker and worker.process.returncode is None:
                    await _stop_worker()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    worker.idle_task = asyncio.create_task(_unload(), name="catty-voice-idle-unload")


async def _ensure_worker(config: Any) -> _Worker:
    global _WORKER
    signature = _worker_signature(config)
    worker = _WORKER
    if worker is not None and worker.signature == signature and worker.process.returncode is None:
        return worker
    await _stop_worker()
    script = worker_script(config)
    if not script.is_file():
        raise FileNotFoundError(f"voice worker script missing: {script}")
    executable = _worker_setting(config, "catty_voice_python") or sys.executable
    config_path = _worker_config_path(config)
    log_path = outbox_dir(config) / "_worker.log"
    log_handle = log_path.open("ab")
    process = await asyncio.create_subprocess_exec(
        executable,
        "-B",
        "-X",
        "utf8",
        str(script),
        "--config",
        str(config_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=log_handle,
        cwd=str(script.parent),
    )
    worker = _Worker(process=process, log_handle=log_handle, signature=signature)
    _WORKER = worker
    # 首次加载模型前 worker 会先回一条 ready 回执; 不读完它会污染下一次请求的读取。
    startup_timeout = max(_as_float(getattr(config, "catty_voice_worker_timeout_seconds", 120.0), 120.0), 10.0)
    try:
        assert process.stdout is not None
        ready_line = await asyncio.wait_for(process.stdout.readline(), timeout=startup_timeout)
    except asyncio.TimeoutError:
        await _stop_worker()
        raise TimeoutError(f"voice worker 启动超过 {int(startup_timeout)} 秒")
    if not ready_line:
        await _stop_worker()
        raise RuntimeError("voice worker 启动即退出")
    try:
        ready = json.loads(ready_line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        await _stop_worker()
        raise RuntimeError("voice worker 启动回执无法解析")
    if not isinstance(ready, dict) or not ready.get("ok"):
        message = str((ready or {}).get("error") or "voice worker 启动失败") if isinstance(ready, dict) else "voice worker 启动失败"
        await _stop_worker()
        raise RuntimeError(message)
    return worker


async def synthesize(config: Any, text: str, scope: str = "") -> dict[str, Any]:
    """用独立 worker 合成一句话; 失败返回 ok=False, 由调用方决定是否退回语音库。"""
    clean = " ".join(str(text or "").split())
    try:
        max_chars = int(getattr(config, "catty_voice_max_chars", 40) or 40)
    except (TypeError, ValueError):
        max_chars = 40
    max_chars = max(4, min(max_chars, 120))
    if not clean:
        return {"ok": False, "error": "text 不能为空", "code": "invalid_text"}
    if len(clean) > max_chars:
        return {
            "ok": False,
            "error": f"语音文本超过上限 ({len(clean)} > {max_chars} 字)",
            "code": "text_too_long",
        }
    if not worker_configured(config):
        return {"ok": False, "error": "本地语音合成未配置", "code": "not_configured"}
    min_free = int(_as_float(getattr(config, "catty_voice_min_free_mb", 4800), 4800))
    free = _free_memory_mb()
    if free is not None and min_free > 0 and free < min_free:
        return {
            "ok": False,
            "error": f"内存不足 ({free} MiB < {min_free} MiB), 这轮不发语音",
            "code": "low_memory",
        }
    timeout = max(_as_float(getattr(config, "catty_voice_worker_timeout_seconds", 120.0), 120.0), 5.0)
    directory = outbox_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"voice-{time.strftime('%Y%m%d-%H%M%S')}-{hashlib.sha1(clean.encode('utf-8')).hexdigest()[:8]}.wav"
    if output.exists():
        output = output.with_name(f"{output.stem}-{int(time.time() * 1000) % 1000}.wav")
    async with _lock():
        try:
            worker = await _ensure_worker(config)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"语音进程启动失败: {type(exc).__name__}: {exc}", "code": "spawn_failed"}
        request = json.dumps({"text": clean, "out": str(output)}, ensure_ascii=False) + "\n"
        try:
            assert worker.process.stdin is not None and worker.process.stdout is not None
            worker.process.stdin.write(request.encode("utf-8"))
            await worker.process.stdin.drain()
            line = await asyncio.wait_for(worker.process.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            await _stop_worker()
            return {"ok": False, "error": f"语音合成超时 ({int(timeout)} 秒)", "code": "timeout"}
        except Exception as exc:  # noqa: BLE001
            await _stop_worker()
            return {"ok": False, "error": f"语音合成失败: {type(exc).__name__}: {exc}", "code": "io_error"}
        if not line:
            await _stop_worker()
            return {"ok": False, "error": "语音进程退出", "code": "worker_exit"}
        try:
            response = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            await _stop_worker()
            return {"ok": False, "error": "语音进程返回无法解析的结果", "code": "bad_response"}
        if not isinstance(response, dict) or not response.get("ok"):
            error = str((response or {}).get("error") or "语音合成失败") if isinstance(response, dict) else "语音合成失败"
            return {"ok": False, "error": error, "code": "worker_error"}
        path = output
        if not path.is_file():
            return {"ok": False, "error": "语音文件未生成", "code": "missing_output"}
        try:
            seconds = validate_audio(path)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"语音文件无效: {exc}", "code": "invalid_audio"}
        await _schedule_idle_unload(config)
        return {
            "ok": True,
            "path": str(path),
            "seconds": round(seconds, 3),
            "sha256": digest(path),
            "backend": "worker",
        }


def cleanup_outbox(config: Any) -> int:
    directory = outbox_dir(config)
    if not directory.is_dir():
        return 0
    ttl = _as_float(getattr(config, "catty_voice_outbox_ttl_hours", 24.0), 24.0) * 3600.0
    if ttl <= 0:
        return 0
    deadline = time.time() - ttl
    removed = 0
    for path in directory.glob("voice-*.wav"):
        try:
            if path.stat().st_mtime < deadline:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


async def aclose() -> None:
    async with _lock():
        await _stop_worker()
