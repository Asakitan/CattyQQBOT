"""faster-whisper 语音转写 worker: 一次进程转一条语音, stdout 只输出 JSON。"""
from __future__ import annotations

import argparse
import json
import sys


def _decode(path: str, sample_rate: int = 16000):
    """用 PyAV 自行解码成 16k 单声道 float32, 绕开 faster-whisper 对 av.open 参数的版本依赖。"""
    import av
    import numpy as np

    chunks: list = []
    with av.open(path) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray())
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate([chunk.reshape(-1) for chunk in chunks]).astype(np.float32)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    try:
        from faster_whisper import WhisperModel

        audio = _decode(args.audio)
        if audio.size < 1600:
            raise ValueError("音频太短或无法解码")
        model = WhisperModel(
            args.model, device="cpu", compute_type="int8", cpu_threads=2, num_workers=1
        )
        segments, info = model.transcribe(
            audio,
            language=args.language or None,
            beam_size=3,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        print(
            json.dumps(
                {"ok": True, "text": text, "language": getattr(info, "language", "")},
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
