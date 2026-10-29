"""调情短语音: 语音库、冷却/概率、worker 校验与工具接线 (2026-10-29)。"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import time
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
        catty_voice_default="michelle",
        catty_voice_persona_voices={"fadianji": "fadianji"},
        catty_voice_profiles={},
        catty_voice_bank_dir="",
        catty_voice_max_chars=40,
        catty_voice_max_per_turn=1,
        catty_voice_cooldown_seconds=600.0,
        catty_voice_chance=1.0,
        catty_voice_auto_chance=0.1,
        catty_voice_auto_max_reply_chars=50,
        catty_voice_only_hours="",
        catty_voice_bubble_max_chars=40,
        catty_voice_bubble_max_per_turn=3,
        catty_voice_bubble_budget_seconds=45.0,
        catty_voice_nsfw_bubble_chance=0.5,
        catty_voice_affection_trigger=True,
        catty_voice_outbox_dir=str(_ROOT / "tmp" / "test-voice-outbox"),
        catty_voice_outbox_ttl_hours=24.0,
        catty_voice_worker_threads=1,
        catty_voice_worker_timeout_seconds=30.0,
        catty_voice_worker_idle_unload_seconds=0.0,
        catty_voice_min_free_mb=4800,
        catty_voice_python="",
        catty_voice_worker_script="",
        catty_voice_repo_dir="",
        catty_voice_bert_dir="",
        catty_voice_hubert_dir="",
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
        fadianji = types.SimpleNamespace(name="fadianji")
        catty = types.SimpleNamespace(name="catty")
        self.assertEqual(voice.voice_name_for(config, fadianji), "fadianji")
        self.assertEqual(voice.voice_name_for(config, catty), "michelle")
        self.assertEqual(voice.voice_name_for(config, None), "michelle")
        self.assertEqual(voice.voice_name_for(_config(catty_voice_default="fadianji"), catty), "fadianji")
        self.assertTrue(voice.tool_available(config, fadianji))
        self.assertTrue(voice.tool_available(config, catty))
        self.assertFalse(voice.tool_available(_config(catty_voice_enabled=False), fadianji))

    def test_both_voice_banks_cover_all_moods(self) -> None:
        config = _config()
        for persona in ("fadianji", "catty"):
            clips = voice.load_bank(config, types.SimpleNamespace(name=persona), verify=True)
            self.assertGreaterEqual(len(clips), 6, persona)
            self.assertEqual({clip.mood for clip in clips}, set(voice.MOODS), persona)
        self.assertNotEqual(
            voice.bank_dir(config, types.SimpleNamespace(name="fadianji")),
            voice.bank_dir(config, types.SimpleNamespace(name="catty")),
        )

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

    def test_default_cooldown_is_ten_seconds(self) -> None:
        loader = importlib.import_module("catty_config_loader")
        self.assertEqual(loader.DEFAULT_CONFIG["voice"]["cooldown_seconds"], 10)
        self.assertEqual(loader.DEFAULT_CONFIG["voice"]["max_per_turn"], 1)

    def test_group_cooldown_is_shared(self) -> None:
        config = _config(catty_voice_cooldown_seconds=10)
        voice.mark_spoken("group:shared")
        self.assertGreater(voice.cooldown_remaining(config, "group:shared"), 0)
        self.assertEqual(voice.cooldown_remaining(config, "group:other"), 0.0)


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
        fake = str(_BANK_DIR / "bank.json")
        config = _config(
            catty_voice_repo_dir=str(self.directory),
            catty_voice_profiles={
                "michelle": {
                    "gpt_path": fake,
                    "sovits_path": fake,
                    "reference_audio": fake,
                    "reference_text": "参考",
                }
            },
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


class AutoVoiceTests(unittest.TestCase):
    def test_probability_roll(self) -> None:
        config = _config()
        self.assertTrue(voice.auto_eligible(config, "在呢", roll=0.05))
        self.assertFalse(voice.auto_eligible(config, "在呢", roll=0.5))
        self.assertFalse(voice.auto_eligible(_config(catty_voice_auto_chance=0.0), "在呢", roll=0.0))

    def test_long_or_technical_reply_is_skipped(self) -> None:
        config = _config()
        self.assertFalse(voice.auto_eligible(config, "字" * 51, roll=0.0))
        self.assertTrue(voice.auto_eligible(config, "字" * 50, roll=0.0))
        self.assertFalse(voice.auto_eligible(config, "```py\nprint(1)\n```", roll=0.0))
        self.assertFalse(voice.auto_eligible(config, "公式 $$x^2$$", roll=0.0))

    def test_staged_voice_wins(self) -> None:
        self.assertFalse(voice.auto_eligible(_config(), "在呢", has_staged_voice=True, roll=0.0))

    def test_auto_pick_returns_ready_clip(self) -> None:
        payload = voice.auto_pick(_config(), types.SimpleNamespace(name="fadianji"), "group:auto")
        self.assertIsNotNone(payload)
        self.assertTrue(Path(payload["path"]).is_file())
        self.assertEqual(payload["voice"], "fadianji")
        self.assertTrue(payload["text"])


class WindowAndBubbleTests(unittest.TestCase):
    @staticmethod
    def _at(hour: int, minute: int) -> float:
        return time.mktime((2026, 10, 29, hour, minute, 0, 0, 0, -1))

    def test_only_window_same_day(self) -> None:
        config = _config(catty_voice_only_hours="20:00-22:00")
        self.assertTrue(voice.only_window_active(config, self._at(20, 0)))
        self.assertTrue(voice.only_window_active(config, self._at(21, 59)))
        self.assertFalse(voice.only_window_active(config, self._at(22, 0)))
        self.assertFalse(voice.only_window_active(config, self._at(12, 0)))

    def test_only_window_overnight(self) -> None:
        config = _config(catty_voice_only_hours="23:00-01:00")
        self.assertTrue(voice.only_window_active(config, self._at(23, 30)))
        self.assertTrue(voice.only_window_active(config, self._at(0, 30)))
        self.assertFalse(voice.only_window_active(config, self._at(1, 0)))

    def test_only_window_invalid_or_empty_is_off(self) -> None:
        self.assertFalse(voice.only_window_active(_config(), self._at(23, 30)))
        self.assertFalse(voice.only_window_active(_config(catty_voice_only_hours="abc"), self._at(23, 30)))
        self.assertFalse(voice.only_window_active(_config(catty_voice_only_hours="20:00-20:00"), self._at(20, 0)))

    def test_bubble_eligible_short_text(self) -> None:
        config = _config()
        self.assertTrue(voice.bubble_voice_eligible(config, "喜欢啊 不然谁陪你熬夜"))
        self.assertTrue(voice.bubble_voice_eligible(config, "字" * 40))
        self.assertFalse(voice.bubble_voice_eligible(config, "字" * 41))
        self.assertFalse(voice.bubble_voice_eligible(config, ""))

    def test_bubble_rejects_code_formula_url_and_list(self) -> None:
        config = _config()
        self.assertFalse(voice.bubble_voice_eligible(config, "```py\nprint(1)\n```"))
        self.assertFalse(voice.bubble_voice_eligible(config, "答案 $$x^2$$"))
        self.assertFalse(voice.bubble_voice_eligible(config, "看 https://example.com/x"))
        self.assertFalse(voice.bubble_voice_eligible(config, "- 第一点"))
        self.assertFalse(voice.bubble_voice_eligible(config, "1. 第一点"))

    def test_spoken_text_strips_action_narration(self) -> None:
        config = _config()
        self.assertEqual(
            voice.spoken_voice_text(config, "（把被子往下拉了一点）……老公，草我"),
            "老公，草我",
        )
        self.assertEqual(
            voice.spoken_voice_text(config, "……老公，草我 （说完立刻别过脸）"),
            "老公，草我",
        )
        self.assertIsNone(voice.spoken_voice_text(config, "（脸红透了，抓起被子挡了一半）"))
        self.assertIsNone(voice.spoken_voice_text(config, ""))

    def test_nsfw_bubble_draw_probability(self) -> None:
        config = _config()
        self.assertTrue(voice.nsfw_bubble_draw(config, roll=0.1))
        self.assertFalse(voice.nsfw_bubble_draw(config, roll=0.9))
        self.assertFalse(
            voice.nsfw_bubble_draw(_config(catty_voice_nsfw_bubble_chance=0.0), roll=0.0)
        )
        self.assertFalse(
            voice.nsfw_bubble_draw(_config(catty_voice_enabled=False), roll=0.0)
        )

    def test_affection_triggers_voice(self) -> None:
        config = _config()
        self.assertTrue(voice.affection_voice_eligible(config, "机机我喜欢你"))
        self.assertTrue(voice.affection_voice_eligible(config, "我爱你"))
        self.assertTrue(voice.affection_voice_eligible(config, "好想你"))
        self.assertTrue(voice.affection_voice_eligible(config, "要抱抱"))
        self.assertFalse(voice.affection_voice_eligible(config, "我不喜欢你"))
        self.assertFalse(voice.affection_voice_eligible(config, "别爱我"))
        self.assertFalse(voice.affection_voice_eligible(config, "我喜欢这个游戏"))
        self.assertFalse(voice.affection_voice_eligible(config, ""))
        self.assertFalse(
            voice.affection_voice_eligible(
                _config(catty_voice_affection_trigger=False), "我喜欢你"
            )
        )
        self.assertFalse(
            voice.affection_voice_eligible(_config(catty_voice_enabled=False), "我喜欢你")
        )


class WorkerTailTests(unittest.TestCase):
    def test_pad_tail_adds_silence_and_fades(self) -> None:
        import numpy as np

        worker = importlib.import_module(f"{_PACKAGE_NAME}.voice_worker")
        rate = 32000
        audio = np.full(rate // 10, 0.5, dtype=np.float64)
        out = worker._pad_tail(audio, rate)
        self.assertEqual(len(out), len(audio) + int(rate * worker._TAIL_PAD_SECONDS))
        self.assertTrue(np.all(out[len(audio):] == 0.0))
        self.assertLess(out[len(audio) - 1], 0.5)
        self.assertAlmostEqual(out[0], 0.5)

    def test_trim_keeps_audio_end(self) -> None:
        import numpy as np

        worker = importlib.import_module(f"{_PACKAGE_NAME}.voice_worker")
        rate = 32000
        audio = np.zeros(rate * 2, dtype=np.float64)
        audio[:rate] = 0.4
        out = worker._trim(audio, rate)
        self.assertAlmostEqual(out[0], 0.4)
        self.assertAlmostEqual(len(out), rate * 1.08, delta=2)

    def test_mood_reference_joins_bank_clips(self) -> None:
        worker = importlib.import_module(f"{_PACKAGE_NAME}.voice_worker")
        groups = worker._mood_groups(_BANK_DIR)
        self.assertTrue(groups.get("shy"))
        work = _ROOT / "tmp" / "test-voice-ref"
        work.mkdir(parents=True, exist_ok=True)
        ref = worker._mood_reference("shy", groups, work, {})
        self.assertIsNotNone(ref)
        path, text = ref
        self.assertTrue(Path(path).is_file())
        self.assertGreater(len(text), 0)
        with wave.open(str(path), "rb") as handle:
            seconds = handle.getnframes() / handle.getframerate()
        self.assertGreater(seconds, 3.0)
        self.assertIsNone(worker._mood_reference("nope", groups, work, {}))


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
        for banned in ("生成", "稍后", "后台"):
            self.assertNotIn(banned, result["note"])

    def test_schema_lets_model_choose_voice_for_confession(self) -> None:
        description = self.tools.ALL_TOOL_SCHEMAS["catty_voice_say"]["function"]["description"]
        self.assertIn("你喜欢我吗", description)
        self.assertIn("只发语音", description)
        self.assertIn("先发出去", description)
        self.assertNotIn("自动发出", description)

    def test_schema_offers_emotion_segments(self) -> None:
        params = self.tools.ALL_TOOL_SCHEMAS["catty_voice_say"]["function"]["parameters"]
        segments = params["properties"]["segments"]
        self.assertEqual(segments["maxItems"], 4)
        item = segments["items"]
        self.assertEqual(item["required"], ["text", "emotion"])
        self.assertIn("tsundere", item["properties"]["emotion"]["enum"])

    def test_executor_rejects_wrong_persona_and_repeat(self) -> None:
        catty = asyncio.run(
            self.tools._exec_voice_say({"mood": "shy"}, self._ctx(_config(), persona="catty"))
        )
        self.assertTrue(catty["ok"])
        self.assertEqual(catty["voice"], "michelle")
        voice._COOLDOWNS.clear()
        ctx = self._ctx(_config())
        self.assertTrue(asyncio.run(self.tools._exec_voice_say({"mood": "soft"}, ctx))["ok"])
        second = asyncio.run(self.tools._exec_voice_say({"mood": "soft"}, ctx))
        self.assertFalse(second["ok"])
        self.assertIn("最多", second["error"])

    def test_schema_exposure_follows_persona(self) -> None:
        config = _config(catty_tools_enabled=True, catty_tools_full_exposure=True,
                         catty_tools_lazy_schema_enabled=False, catty_tools_disabled_in_private=[])
        for persona in ("fadianji", "catty", "custom"):
            names = {
                schema["function"]["name"]
                for schema in self.tools.available_tool_schemas(
                    config, is_private=False, persona=types.SimpleNamespace(name=persona)
                )
            }
            self.assertIn("catty_voice_say", names, persona)
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
