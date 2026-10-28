"""调情短语音: 语音库、冷却/概率、worker 校验与工具接线 (2026-10-29)。"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_voice_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)

voice = importlib.import_module(f"{_PACKAGE_NAME}.voice_reply")
_BANK_DIR = _ROOT / "src" / "catty_qq_ai" / "data" / "voice_bank" / "fadianji"


def _config(**overrides):
    base = dict(
        catty_voice_enabled=True,
        catty_voice_backend="bank",
        catty_voice_personas=["fadianji"],
        catty_voice_bank_dir="",
        catty_voice_max_chars=40,
        catty_voice_max_per_turn=1,
        catty_voice_cooldown_seconds=600.0,
        catty_voice_chance=1.0,
        catty_voice_outbox_dir=str(_ROOT / "tmp" / "test-voice-outbox"),
        catty_voice_outbox_ttl_hours=24.0,
        catty_voice_worker_threads=1,
        catty_voice_worker_timeout_seconds=30.0,
        catty_voice_worker_idle_unload_seconds=0.0,
        catty_voice_min_free_mb=4800,
        catty_voice_python="",
        catty_voice_worker_script="",
        catty_voice_repo_dir="",
        catty_voice_gpt_path="",
        catty_voice_sovits_path="",
        catty_voice_bert_dir="",
        catty_voice_hubert_dir="",
        catty_voice_reference_audio="",
        catty_voice_reference_text="",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _write_wav(path: Path, *, seconds: float = 1.0, channels: int = 1, rate: int = 32000) -> None:
    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x01\x00" * frames * channels)


class BankTests(unittest.TestCase):
    def test_bank_clips_match_manifest(self) -> None:
        clips = voice.load_bank(_config(), types.SimpleNamespace(name="fadianji"), verify=True)
        self.assertGreaterEqual(len(clips), 6)
        self.assertEqual({clip.mood for clip in clips}, set(voice.MOODS))
        for clip in clips:
            self.assertTrue(clip.text.strip())
            self.assertTrue(clip.path.is_file())
            self.assertGreater(clip.seconds, 0.5)

    def test_persona_gate(self) -> None:
        config = _config()
        self.assertTrue(voice.persona_allowed(config, types.SimpleNamespace(name="fadianji")))
        self.assertFalse(voice.persona_allowed(config, types.SimpleNamespace(name="catty")))
        self.assertTrue(voice.tool_available(config, types.SimpleNamespace(name="fadianji")))
        self.assertFalse(voice.tool_available(config, types.SimpleNamespace(name="catty")))
        self.assertFalse(voice.tool_available(_config(catty_voice_enabled=False),
                                             types.SimpleNamespace(name="fadianji")))

    def test_mood_alias_and_selection(self) -> None:
        self.assertEqual(voice.mood_of("傲娇"), "tsundere")
        self.assertEqual(voice.mood_of("nonsense"), "soft")
        clips = voice.load_bank(_config(), types.SimpleNamespace(name="fadianji"))
        first = voice.select_clip(clips, "tease", "group:1")
        self.assertEqual(first.mood, "tease")
        second = voice.select_clip(clips, "tease", "group:1")
        self.assertNotEqual(first.clip_id, second.clip_id)


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        voice._COOLDOWNS.clear()

    def test_chance_rejection_consumes_cooldown(self) -> None:
        config = _config(catty_voice_chance=0.0)
        allowed, reason = voice.reserve_slot(config, "group:chance")
        self.assertFalse(allowed)
        self.assertIn("文字", reason)
        self.assertGreater(voice.cooldown_remaining(config, "group:chance"), 500)

    def test_cooldown_blocks_second_call(self) -> None:
        config = _config()
        self.assertEqual(voice.reserve_slot(config, "group:cool"), (True, ""))
        allowed, reason = voice.reserve_slot(config, "group:cool")
        self.assertFalse(allowed)
        self.assertIn("冷却", reason)

    def test_zero_cooldown_allows_repeat(self) -> None:
        config = _config(catty_voice_cooldown_seconds=0)
        self.assertTrue(voice.reserve_slot(config, "group:zero")[0])
        self.assertTrue(voice.reserve_slot(config, "group:zero")[0])

    def test_owner_bypasses_cooldown_and_chance(self) -> None:
        config = _config(catty_voice_chance=0.0)
        self.assertTrue(voice.reserve_slot(config, "group:owner", is_owner=True)[0])
        self.assertTrue(voice.reserve_slot(config, "group:owner", is_owner=True)[0])
        # 主人不受限, 但普通群友仍被概率/冷却挡下
        self.assertFalse(voice.reserve_slot(config, "group:guest")[0])

    def test_default_cooldown_is_five_minutes(self) -> None:
        loader = importlib.import_module("catty_config_loader")
        self.assertEqual(loader.DEFAULT_CONFIG["voice"]["cooldown_seconds"], 300)
        self.assertEqual(loader.DEFAULT_CONFIG["voice"]["max_per_turn"], 1)


class AudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = _ROOT / "tmp" / "test-voice-outbox"
        self.directory.mkdir(parents=True, exist_ok=True)

    def test_validate_audio(self) -> None:
        path = self.directory / "ok.wav"
        _write_wav(path, seconds=2.0)
        self.assertAlmostEqual(voice.validate_audio(path), 2.0, places=2)
        stereo = self.directory / "stereo.wav"
        _write_wav(stereo, channels=2)
        with self.assertRaises(ValueError):
            voice.validate_audio(stereo)
        long_path = self.directory / "long.wav"
        _write_wav(long_path, seconds=25.0)
        with self.assertRaises(ValueError):
            voice.validate_audio(long_path)

    def test_synthesize_rejects_bad_input(self) -> None:
        config = _config()
        self.assertEqual(asyncio.run(voice.synthesize(config, "  "))["code"], "invalid_text")
        long_text = "啊" * 60
        self.assertEqual(asyncio.run(voice.synthesize(config, long_text))["code"], "text_too_long")
        self.assertEqual(asyncio.run(voice.synthesize(config, "在呢"))["code"], "not_configured")

    def test_synthesize_memory_guard(self) -> None:
        config = _config(
            catty_voice_repo_dir=str(self.directory),
            catty_voice_gpt_path=str(_BANK_DIR / "bank.json"),
            catty_voice_sovits_path=str(_BANK_DIR / "bank.json"),
            catty_voice_reference_audio=str(_BANK_DIR / "bank.json"),
            catty_voice_reference_text="参考",
        )
        with patch.object(voice, "_free_memory_mb", return_value=100):
            result = asyncio.run(voice.synthesize(config, "在呢"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "low_memory")

    def test_cleanup_outbox_removes_expired(self) -> None:
        directory = self.directory / "cleanup"
        directory.mkdir(parents=True, exist_ok=True)
        stale = directory / "voice-old.wav"
        _write_wav(stale)
        old = stale.stat().st_mtime - 3 * 86400
        import os

        os.utime(stale, (old, old))
        removed = voice.cleanup_outbox(_config(catty_voice_outbox_dir=str(directory)))
        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())


class ToolWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            f"{_PACKAGE_NAME}.tools", _ROOT / "src" / "catty_qq_ai" / "tools.py"
        )
        assert spec is not None and spec.loader is not None
        cls.tools = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.tools
        spec.loader.exec_module(cls.tools)

    def setUp(self) -> None:
        voice._COOLDOWNS.clear()
        voice._LAST_CLIP.clear()

    def _ctx(self, config, persona="fadianji"):
        return self.tools.ToolContext(
            config=config,
            memory_store=None,
            event=None,
            persona=types.SimpleNamespace(name=persona),
            scope_key="group:42",
        )

    def test_executor_stages_bank_clip(self) -> None:
        ctx = self._ctx(_config())
        result = asyncio.run(self.tools._exec_voice_say({"mood": "shy"}, ctx))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "staged")
        self.assertEqual(len(ctx.pending_voice_deliveries), 1)
        self.assertTrue(Path(result["_staged_voice"]["path"]).is_file())
        self.assertEqual(result["line"], ctx.pending_voice_deliveries[0]["text"])

    def test_executor_rejects_wrong_persona_and_repeat(self) -> None:
        wrong = asyncio.run(
            self.tools._exec_voice_say({"mood": "shy"}, self._ctx(_config(), persona="catty"))
        )
        self.assertFalse(wrong["ok"])
        ctx = self._ctx(_config())
        self.assertTrue(asyncio.run(self.tools._exec_voice_say({"mood": "soft"}, ctx))["ok"])
        second = asyncio.run(self.tools._exec_voice_say({"mood": "soft"}, ctx))
        self.assertFalse(second["ok"])
        self.assertIn("最多", second["error"])

    def test_schema_exposure_follows_persona(self) -> None:
        config = _config(catty_tools_enabled=True, catty_tools_full_exposure=True,
                         catty_tools_lazy_schema_enabled=False, catty_tools_disabled_in_private=[])
        names = {
            schema["function"]["name"]
            for schema in self.tools.available_tool_schemas(
                config, is_private=False, persona=types.SimpleNamespace(name="fadianji")
            )
        }
        self.assertIn("catty_voice_say", names)
        hidden = {
            schema["function"]["name"]
            for schema in self.tools.available_tool_schemas(
                config, is_private=False, persona=types.SimpleNamespace(name="catty")
            )
        }
        self.assertNotIn("catty_voice_say", hidden)
        disabled = {
            schema["function"]["name"]
            for schema in self.tools.available_tool_schemas(
                _config(catty_voice_enabled=False, catty_tools_enabled=True,
                        catty_tools_full_exposure=True, catty_tools_lazy_schema_enabled=False,
                        catty_tools_disabled_in_private=[]),
                is_private=False,
                persona=types.SimpleNamespace(name="fadianji"),
            )
        }
        self.assertNotIn("catty_voice_say", disabled)


if __name__ == "__main__":
    unittest.main()
