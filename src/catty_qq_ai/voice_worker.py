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
import json
import os
import random
import sys
import wave
from pathlib import Path

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
            with contextlib.redirect_stdout(sys.stderr):
                chunks = list(pipeline.run({
                    "text": text, "text_lang": "all_zh", "ref_audio_path": str(reference),
                    "prompt_text": prompt_text, "prompt_lang": "all_zh",
                    "text_split_method": "cut0", "batch_size": 1,
                    "seed": random.randint(1, 10**6), "top_k": 15, "top_p": 1.0,
                    "temperature": 1.0, "repetition_penalty": 1.35,
                    "fragment_interval": 0.0, "parallel_infer": False,
                    "use_cuda_graph": False, "use_flash_attention": False,
                }))
            import numpy as np

            if not chunks:
                raise ValueError("没有生成音频")
            rate = int(chunks[0][0])
            audio = np.concatenate([np.asarray(item).reshape(-1) for _, item in chunks])
            audio = audio.astype(np.float64) / 32768 if audio.dtype.kind in "iu" else audio.astype(np.float64)
            if not np.isfinite(audio).all() or np.std(audio) < 1e-4:
                raise ValueError("生成了无效音频")
            audio = _trim(audio, rate)
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
