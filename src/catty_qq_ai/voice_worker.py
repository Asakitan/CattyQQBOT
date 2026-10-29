"""GPT-SoVITS 短语音 worker: stdin 逐行收 JSON, stdout 只输出协议回执。

启动: python voice_worker.py --config <worker.json>
请求: {"text": "...", "out": "...wav"}
回执: {"ok": true, "path": "...", "seconds": 3.1}
      {"ok": false, "error": "..."}
模型只在本进程加载一次; 框架日志全部重定向到 stderr, 不污染协议。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import random
import sys
import wave
from pathlib import Path
from typing import Any

PROTOCOL = sys.stdout


def _emit(payload: dict) -> None:
    PROTOCOL.write(json.dumps(payload, ensure_ascii=False) + "\n")
    PROTOCOL.flush()


def _trim(audio, rate: int, threshold: float = 10 ** (-45 / 20), tail: float = 0.08):
    import numpy as np

    index = np.flatnonzero(np.abs(audio) > threshold)
    if index.size == 0:
        raise ValueError("生成的音频是静音")
    start = max(int(index[0] - rate * 0.05), 0)
    end = min(int(index[-1] + rate * tail), len(audio))
    return audio[start:end]


# 模型在 fragment_interval=0 时不会留尾巴, 直接补静音并做短淡出, 避免尾音被削平。
_TAIL_PAD_SECONDS = 0.25
_FADE_OUT_SECONDS = 0.008


def _pad_tail(audio, rate: int):
    import numpy as np

    audio = np.asarray(audio, dtype=np.float64).copy()
    fade = min(int(rate * _FADE_OUT_SECONDS), len(audio))
    if fade > 1:
        audio[-fade:] *= np.linspace(1.0, 0.0, fade)
    pad = np.zeros(int(rate * _TAIL_PAD_SECONDS), dtype=np.float64)
    return np.concatenate([audio, pad])


def _write_wav(path: Path, pcm, rate: int) -> float:
    import numpy as np

    data = np.clip(np.asarray(pcm), -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data.tobytes())
    return len(data) / float(rate)


def _read_pcm(path: Path) -> tuple[int, bytes]:
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                return 0, b""
            return handle.getframerate(), handle.readframes(handle.getnframes())
    except (OSError, wave.Error):
        return 0, b""


def _mood_groups(bank_dir: Path) -> dict[str, list[tuple[Path, str]]]:
    manifest = bank_dir / "bank.json"
    if not manifest.is_file():
        return {}
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    groups: dict[str, list[tuple[Path, str]]] = {}
    for entry in raw.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        mood = str(entry.get("mood") or "").strip().lower()
        name = str(entry.get("file") or "").strip()
        text = str(entry.get("text") or "").strip()
        if not mood or not name or not text:
            continue
        path = bank_dir / name
        if path.is_file():
            groups.setdefault(mood, []).append((path, text))
    return groups


def _mood_reference(mood: str, groups, work_dir: Path, cache: dict):
    """把同情绪的多条语音库片段拼成 3-6 秒参考音, 让这一段的语气跟着情绪走。"""
    if mood in cache:
        return cache[mood]
    rate = None
    frames: list[bytes] = []
    texts: list[str] = []
    for path, text in groups.get(mood, []):
        clip_rate, data = _read_pcm(path)
        if not data:
            continue
        if rate is None:
            rate = clip_rate
        if clip_rate != rate:
            continue
        frames.append(data)
        texts.append(text)
        if sum(len(frame) for frame in frames) / (2 * rate) >= 6.0:
            break
    if not frames:
        cache[mood] = None
        return None
    key = hashlib.sha1("\n".join(texts).encode("utf-8")).hexdigest()[:10]
    out = work_dir / f"_ref-{mood}-{key}.wav"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"".join(frames))
    cache[mood] = (out, " ".join(texts))
    return cache[mood]


def _load_pipeline(config: dict):
    import torch

    from TTS_infer_pack.TTS import TTS

    threads = int(config.get("threads") or 2)
    torch.set_num_threads(max(threads, 1))
    return TTS({"custom": {
        "device": "cpu",
        "is_half": False,
        "version": "v2",
        "t2s_weights_path": config["gpt_path"],
        "vits_weights_path": config["sovits_path"],
        "bert_base_path": config["bert_dir"],
        "cnhuhbert_base_path": config["hubert_dir"],
    }})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    repo = Path(config["repo_dir"]).resolve()
    reference = Path(config["reference_audio"]).resolve()
    prompt_text = str(config["reference_text"] or "").strip()
    for key in ("repo_dir", "gpt_path", "sovits_path", "bert_dir", "hubert_dir", "reference_audio"):
        if not config.get(key):
            _emit({"ok": False, "error": f"worker 配置缺少 {key}", "fatal": True})
            return 2
    if not reference.is_file() or not prompt_text:
        _emit({"ok": False, "error": "参考音频或参考文本缺失", "fatal": True})
        return 2
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        version="v2",
        bert_path=str(config["bert_dir"]),
    )
    # 没有 G2PW 模型时回退 pypinyin 常规读音, 不阻断合成。
    g2pw_dir = repo / "GPT_SoVITS" / "text" / "G2PWModel"
    if not (g2pw_dir / "g2pW.onnx").is_file():
        os.environ.setdefault("is_g2pw", "0")
    os.chdir(repo)
    sys.path[:0] = [str(repo), str(repo / "GPT_SoVITS")]
    try:
        with contextlib.redirect_stdout(sys.stderr):
            pipeline = _load_pipeline(config)
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "error": f"模型加载失败: {type(exc).__name__}: {exc}", "fatal": True})
        return 3
    _emit({"ok": True, "ready": True})

    work_dir = Path(str(config.get("work_dir") or reference.parent)).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    bank_root = str(config.get("bank_dir") or "").strip()
    mood_groups = _mood_groups(Path(bank_root).resolve()) if bank_root else {}
    mood_refs: dict[str, Any] = {}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            text = " ".join(str(request.get("text") or "").split())
            output = Path(str(request.get("out") or "")).expanduser()
            if not text or not str(output):
                _emit({"ok": False, "error": "text/out 不能为空"})
                continue
            import numpy as np

            def _synth(part_text: str, ref_audio, ref_text: str):
                with contextlib.redirect_stdout(sys.stderr):
                    chunks = list(pipeline.run({
                        "text": part_text, "text_lang": "all_zh", "ref_audio_path": str(ref_audio),
                        "prompt_text": ref_text, "prompt_lang": "all_zh",
                        "text_split_method": "cut0", "batch_size": 1,
                        "seed": random.randint(1, 10**6), "top_k": 15, "top_p": 1.0,
                        "temperature": 1.0, "repetition_penalty": 1.35,
                        "fragment_interval": 0.0, "parallel_infer": False,
                        "use_cuda_graph": False, "use_flash_attention": False,
                    }))
                if not chunks:
                    raise ValueError("没有生成音频")
                pcm = np.concatenate([np.asarray(item).reshape(-1) for _, item in chunks])
                pcm = pcm.astype(np.float64) / 32768 if pcm.dtype.kind in "iu" else pcm.astype(np.float64)
                return int(chunks[0][0]), pcm

            parts = []
            rate = 0
            segments = request.get("segments")
            if isinstance(segments, list):
                for segment in segments[:4]:
                    if not isinstance(segment, dict):
                        continue
                    segment_text = " ".join(str(segment.get("text") or "").split())
                    if not segment_text:
                        continue
                    mood = str(segment.get("mood") or "").strip().lower()
                    chosen = _mood_reference(mood, mood_groups, work_dir, mood_refs) if mood else None
                    seg_rate, pcm = _synth(
                        segment_text,
                        chosen[0] if chosen else reference,
                        chosen[1] if chosen else prompt_text,
                    )
                    if not parts:
                        rate = seg_rate
                    parts.append(pcm)
                    parts.append(np.zeros(int(seg_rate * 0.09), dtype=np.float64))
            if not parts:
                whole_mood = str(request.get("mood") or "").strip().lower()
                chosen = _mood_reference(whole_mood, mood_groups, work_dir, mood_refs) if whole_mood else None
                rate, pcm = _synth(
                    text,
                    chosen[0] if chosen else reference,
                    chosen[1] if chosen else prompt_text,
                )
                parts.append(pcm)
            chunks = [(rate, np.clip(np.concatenate(parts) * 32768, -32768, 32767).astype(np.int16))]
            if not chunks:
                raise ValueError("没有生成音频")
            rate = int(chunks[0][0])
            audio = np.concatenate([np.asarray(item).reshape(-1) for _, item in chunks])
            audio = audio.astype(np.float64) / 32768 if audio.dtype.kind in "iu" else audio.astype(np.float64)
            if not np.isfinite(audio).all() or np.std(audio) < 1e-4:
                raise ValueError("生成了无效音频")
            audio = _trim(audio, rate)
            audio = _pad_tail(audio, rate)
            peak = float(np.max(np.abs(audio))) or 1.0
            audio = audio * min(0.89 / peak, 1.0)
            seconds = _write_wav(output, audio * 32768, rate)
        except Exception as exc:  # noqa: BLE001
            _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            continue
        _emit({"ok": True, "path": str(output), "seconds": round(seconds, 3)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
