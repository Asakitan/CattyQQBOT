"""QQ 语音转文字: 格式嗅探、块格式与转写缓存 (2026-10-29)。"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "voice_stt_test", _ROOT / "src" / "catty_qq_ai" / "voice_stt.py"
)
assert _SPEC is not None and _SPEC.loader is not None
stt = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = stt
_SPEC.loader.exec_module(stt)


def _config(**overrides):
    base = dict(
        catty_voice_stt_enabled=True,
        catty_voice_stt_python="",
        catty_voice_stt_model=str(_ROOT / "tmp" / "fake-model"),
        catty_voice_stt_dir=str(_ROOT / "tmp" / "test-voice-inbox"),
        catty_voice_stt_timeout_seconds=30.0,
        catty_voice_stt_max_bytes=1024 * 1024,
        catty_voice_stt_language="zh",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class SniffTests(unittest.TestCase):
    def test_signatures(self) -> None:
        cases = {
            b"RIFF....WAVE": "wav",
            b"#!AMR\n\x01": "amr",
            b"#!SILK_V3": "silk",
            b"OggS\x00": "ogg",
            b"fLaC\x00": "flac",
            b"ID3\x03": "mp3",
            b"\xff\xfb\x90\x00": "mp3",
            b"random": "bin",
        }
        for data, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(stt.sniff_suffix(data), expected)


class BlockTests(unittest.TestCase):
    def test_format_block(self) -> None:
        self.assertEqual(stt.format_block("你好 呀"), "【语音转写】你好 呀")
        self.assertIn("未能识别", stt.format_block("   "))


class TranscribeTests(unittest.TestCase):
    def setUp(self) -> None:
        stt._CACHE.clear()

    def test_no_segments(self) -> None:
        self.assertEqual(asyncio.run(stt.transcribe_event(_config(), None, [])), "")

    def test_silk_is_skipped_without_worker(self) -> None:
        async def fake_resolve(config, bot, segment, max_bytes):
            return b"#!SILK_V3" + b"\x00" * 64

        with patch.object(stt, "_resolve_audio", side_effect=fake_resolve):
            with patch.object(stt, "_run_worker") as worker:
                self.assertEqual(asyncio.run(stt.transcribe_event(_config(), None, [{"url": "x"}])), "")
                worker.assert_not_called()

    def test_result_cached_by_audio_hash(self) -> None:
        async def fake_resolve(config, bot, segment, max_bytes):
            return b"RIFF" + b"\x00" * 128

        async def fake_worker(config, path):
            return "晚安啦"

        with patch.object(stt, "_resolve_audio", side_effect=fake_resolve):
            with patch.object(stt, "_run_worker", side_effect=fake_worker) as worker:
                first = asyncio.run(stt.transcribe_event(_config(), None, [{"url": "a"}]))
                second = asyncio.run(stt.transcribe_event(_config(), None, [{"url": "b"}]))
        self.assertEqual(first, "晚安啦")
        self.assertEqual(second, "晚安啦")
        self.assertEqual(worker.call_count, 1)

    def test_resolve_failure_returns_empty(self) -> None:
        async def boom(config, bot, segment, max_bytes):
            raise FileNotFoundError("nope")

        with patch.object(stt, "_resolve_audio", side_effect=boom):
            self.assertEqual(asyncio.run(stt.transcribe_event(_config(), None, [{"file": "x"}])), "")


if __name__ == "__main__":
    unittest.main()
