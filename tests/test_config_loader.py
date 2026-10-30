from pathlib import Path
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


_MODULE_PATH = Path(__file__).resolve().parents[1] / "catty_config_loader.py"
_SPEC = importlib.util.spec_from_file_location("catty_config_loader", _MODULE_PATH)
_loader = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = _loader
_SPEC.loader.exec_module(_loader)


class ConfigLoaderTests(unittest.TestCase):
    def test_task_settings_export_as_one_resolved_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            _loader._apply_config({"agent_tasks": {"daily_token_limit": 1234}}, Path(directory))
            actual = json.loads(os.environ["CATTY_AGENT_TASKS"])
            self.assertEqual(actual["daily_token_limit"], 1234)
            self.assertEqual(Path(actual["data_dir"]), (Path(directory) / "data/agent_tasks").resolve())
        example = json.loads((_MODULE_PATH.parent / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["agent_tasks"]["worker_model"], "deepseek-v4.1-flash")

    def test_vision_image_direct_download_is_opt_in_and_exported(self) -> None:
        example = json.loads((_MODULE_PATH.parent / "config.example.json").read_text(encoding="utf-8"))
        self.assertFalse(_loader.DEFAULT_CONFIG["vision"]["image_download_direct"])
        self.assertFalse(example["vision"]["image_download_direct"])
        with tempfile.TemporaryDirectory() as directory:
            for enabled in (False, True):
                with self.subTest(enabled=enabled), patch.dict(os.environ, {}, clear=True):
                    _loader._apply_config({"vision": {"image_download_direct": enabled}}, Path(directory))
                    self.assertEqual(os.environ["CATTY_VISION_IMAGE_DOWNLOAD_DIRECT"], str(enabled).lower())

    def test_separate_nsfw_image_provider_is_exported(self) -> None:
        data = {
            "imagegen": {
                "backend": "openai",
                "nsfw_backend": "pixelbunny",
                "pixelbunny": {"base_url": "https://example.test/v1", "api_key": "TOKEN"},
            },
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            _loader._apply_config(data, Path(directory))
            self.assertEqual(os.environ["CATTY_IMAGEGEN_NSFW_BACKEND"], "pixelbunny")
            self.assertEqual(os.environ["CATTY_IMAGEGEN_PIXELBUNNY_BASE_URL"], "https://example.test/v1")
            self.assertEqual(os.environ["CATTY_IMAGEGEN_PIXELBUNNY_API_KEY"], "TOKEN")

    def test_conversation_tool_switches_are_exported(self) -> None:
        keys = ("session_messages_enabled", "wait_messages_enabled", "close_conversation_enabled")
        example = json.loads((_MODULE_PATH.parent / "config.example.json").read_text(encoding="utf-8"))
        for key in keys:
            self.assertTrue(_loader.DEFAULT_CONFIG["tools"][key])
            self.assertTrue(example["tools"][key])
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            _loader._apply_config({"tools": {key: False for key in keys}}, Path(directory))
            for key in keys:
                self.assertEqual(os.environ[f"CATTY_{key.upper()}"], "false")

    def test_rag_local_bge_switch_is_exported(self) -> None:
        self.assertTrue(_loader.DEFAULT_CONFIG["rag"]["local_bge_enabled"])
        example = json.loads((_MODULE_PATH.parent / "config.example.json").read_text(encoding="utf-8"))
        self.assertTrue(example["rag"]["local_bge_enabled"])
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            _loader._apply_config({"rag": {"local_bge_enabled": False}}, Path(directory))
            self.assertEqual(os.environ["CATTY_RAG_LOCAL_BGE_ENABLED"], "false")

    def test_group_hourly_quota_exports_zero_and_explicit_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for quota in (0, 3_000_000, 30_000_000):
                with self.subTest(quota=quota), patch.dict(os.environ, {}, clear=True):
                    _loader._apply_config(
                        {"token_billing": {"group_hourly_token_quota": quota}},
                        Path(directory),
                    )
                    self.assertEqual(os.environ["CATTY_GROUP_HOURLY_TOKEN_QUOTA"], str(quota))

    def test_missing_group_hourly_quota_does_not_export_a_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            _loader._apply_config({}, Path(directory))
            self.assertNotIn("CATTY_GROUP_HOURLY_TOKEN_QUOTA", os.environ)

    def test_local_training_master_switch_and_collection_flag_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {
                    "enabled": False,
                    "collect_assistant_samples": True,
                },
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                enabled = os.environ["CATTY_LOCAL_TRAINING_ENABLED"]
                collect = os.environ["CATTY_LOCAL_TRAINING_COLLECT_ASSISTANT_SAMPLES"]

        self.assertEqual(enabled, "false")
        self.assertEqual(collect, "true")

    def test_local_critic_extra_body_stays_gate_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {"extra_body": {"think": False}},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                extra_body = json.loads(os.environ["CATTY_LOCAL_CRITIC_EXTRA_BODY"])
                mode = os.environ["CATTY_LOCAL_CRITIC_MODE"]

        self.assertFalse(extra_body["think"])
        self.assertEqual(extra_body, {"think": False})
        self.assertEqual(mode, "reply_gate_only")

    def test_reply_delivery_flags_are_exported_from_chat_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {
                    "reply_mix_emoji_with_text": False,
                    "reply_quote_enabled": True,
                    "reply_quote_private_enabled": True,
                },
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                mixed = os.environ["CATTY_REPLY_MIX_EMOJI_WITH_TEXT"]
                quote = os.environ["CATTY_REPLY_QUOTE_ENABLED"]
                private_quote = os.environ["CATTY_REPLY_QUOTE_PRIVATE_ENABLED"]

        self.assertEqual(mixed, "false")
        self.assertEqual(quote, "true")
        self.assertEqual(private_quote, "true")

    def test_file_delivery_flags_and_dir_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
                "file_delivery": {
                    "enabled": True,
                    "dir": "outbox",
                    "ttl_hours": 24,
                    "cleanup_interval_seconds": 3600,
                    "max_chars": 1000,
                    "max_per_turn": 2,
                },
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                enabled = os.environ["CATTY_FILE_DELIVERY_ENABLED"]
                outbox_dir = Path(os.environ["CATTY_FILE_DELIVERY_DIR"])
                ttl_hours = os.environ["CATTY_FILE_DELIVERY_TTL_HOURS"]
                max_chars = os.environ["CATTY_FILE_DELIVERY_MAX_CHARS"]

        self.assertEqual(enabled, "true")
        self.assertEqual(outbox_dir, base_dir / "outbox")
        self.assertEqual(ttl_hours, "24")
        self.assertEqual(max_chars, "1000")

    def test_code_sandbox_flags_and_dir_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
                "code_sandbox": {
                    "enabled": True,
                    "dir": "pybox",
                    "allow_guests": False,
                    "timeout_seconds": 30,
                    "max_output_chars": 8000,
                    "max_code_chars": 60000,
                    "max_files": 5,
                    "max_file_bytes": 10000000,
                    "max_work_bytes": 50000000,
                    "ttl_hours": 24,
                    "cleanup_interval_seconds": 3600,
                    "max_packages": 5,
                    "pip_timeout_seconds": 120,
                    "pip_index_url": "https://pypi.org/simple",
                    "legacy_run_code_enabled": False,
                    "allowed_packages": ["numpy", "pandas"],
                },
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                enabled = os.environ["CATTY_CODE_SANDBOX_ENABLED"]
                sandbox_dir = Path(os.environ["CATTY_CODE_SANDBOX_DIR"])
                packages = json.loads(os.environ["CATTY_CODE_SANDBOX_ALLOWED_PACKAGES"])
                legacy = os.environ["CATTY_CODE_SANDBOX_LEGACY_RUN_CODE_ENABLED"]

        self.assertEqual(enabled, "true")
        self.assertEqual(sandbox_dir, base_dir / "pybox")
        self.assertEqual(packages, ["numpy", "pandas"])
        self.assertEqual(legacy, "false")

    def test_keyword_replies_are_exported_from_chat_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {
                    "keyword_replies": [
                        {
                            "keywords": ["MC", "我的世界"],
                            "reply": "开服啦喵",
                        }
                    ],
                },
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                keyword_replies = json.loads(os.environ["CATTY_KEYWORD_REPLIES"])

        self.assertEqual(keyword_replies[0]["keywords"], ["MC", "我的世界"])
        self.assertEqual(keyword_replies[0]["reply"], "开服啦喵")

    def test_emoji_diversity_flags_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {
                    "auto_fallback_enabled": False,
                    "diversity_enabled": True,
                    "diversity_recent_window": 10,
                    "diversity_candidate_pool": 8,
                },
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                enabled = os.environ["CATTY_EMOJI_DIVERSITY_ENABLED"]
                fallback = os.environ["CATTY_EMOJI_AUTO_FALLBACK_ENABLED"]
                window = os.environ["CATTY_EMOJI_DIVERSITY_RECENT_WINDOW"]
                pool = os.environ["CATTY_EMOJI_DIVERSITY_CANDIDATE_POOL"]

        self.assertEqual(enabled, "true")
        self.assertEqual(fallback, "false")
        self.assertEqual(window, "10")
        self.assertEqual(pool, "8")

    def test_hot_reload_flags_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
                "hot_reload": {
                    "enabled": True,
                    "poll_seconds": 2,
                    "debounce_seconds": 0.5,
                    "restart_on_code_change": False,
                },
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                enabled = os.environ["CATTY_HOT_RELOAD_ENABLED"]
                poll = os.environ["CATTY_HOT_RELOAD_POLL_SECONDS"]
                debounce = os.environ["CATTY_HOT_RELOAD_DEBOUNCE_SECONDS"]
                restart = os.environ["CATTY_HOT_RELOAD_RESTART_ON_CODE_CHANGE"]

        self.assertEqual(enabled, "true")
        self.assertEqual(poll, "2")
        self.assertEqual(debounce, "0.5")
        self.assertEqual(restart, "false")

    def test_web_search_engines_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {"engines": ["google", "bing"]},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                engines = json.loads(os.environ["CATTY_WEB_SEARCH_ENGINES"])

        self.assertEqual(engines, ["google", "bing"])

    def test_intelligence_write_tool_flags_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "tools": {
                    "profile_memory_enabled": False,
                    "scope_meme_enabled": True,
                    "timeline_enabled": False,
                    "adaptive_prompt_enabled": True,
                },
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                values = {
                    name: os.environ[name]
                    for name in (
                        "CATTY_PROFILE_MEMORY_ENABLED",
                        "CATTY_SCOPE_MEME_ENABLED",
                        "CATTY_TIMELINE_ENABLED",
                        "CATTY_ADAPTIVE_PROMPT_ENABLED",
                    )
                }

        self.assertEqual(values["CATTY_PROFILE_MEMORY_ENABLED"], "false")
        self.assertEqual(values["CATTY_SCOPE_MEME_ENABLED"], "true")
        self.assertEqual(values["CATTY_TIMELINE_ENABLED"], "false")
        self.assertEqual(values["CATTY_ADAPTIVE_PROMPT_ENABLED"], "true")
        example = json.loads((_MODULE_PATH.parent / "config.example.json").read_text(encoding="utf-8"))
        current = json.loads((_MODULE_PATH.parent / "config.json").read_text(encoding="utf-8"))
        for key in (
            "profile_memory_enabled",
            "scope_meme_enabled",
            "timeline_enabled",
            "adaptive_prompt_enabled",
        ):
            self.assertTrue(_loader.DEFAULT_CONFIG["tools"][key])
            self.assertTrue(example["tools"][key])
            self.assertTrue(current["tools"][key])

    def test_game_context_group_ids_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "game_context": {
                    "star_resonance_group_ids": [477970838, 578305908],
                    "strinova_group_ids": [1001],
                },
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                star_group_ids = json.loads(os.environ["CATTY_GAME_CONTEXT_STAR_RESONANCE_GROUP_IDS"])
                strinova_group_ids = json.loads(os.environ["CATTY_GAME_CONTEXT_STRINOVA_GROUP_IDS"])

        self.assertEqual(star_group_ids, [477970838, 578305908])
        self.assertEqual(strinova_group_ids, [1001])


    def test_session_context_and_cache_pricing_are_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {
                    "model_context_tokens": 1_000_000,
                    "cache_hit_input_price_ratio": 0.02,
                },
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {
                    "group_history_scope": "group",
                    "history_turns": 16,
                    "session_context_enabled": True,
                    "session_context_target_tokens": 256_000,
                    "session_context_trim_to_tokens": 192_000,
                    "session_context_headroom_tokens": 32_000,
                    "session_ai_compact_enabled": True,
                    "session_ai_compact_trigger_tokens": 512_000,
                    "fadianji_static_scene_bank_enabled": True,
                    "fadianji_static_scene_bank_max_chars": 140_000,
                    "session_cache_persistence_enabled": False,
                    "session_cache_dir": "tmp/sessions",
                    "session_cache_max_sessions": 7,
                    "session_cache_save_debounce_seconds": 0.5,
                },
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                values = {
                    name: os.environ[name]
                    for name in (
                        "CATTY_MODEL_CONTEXT_TOKENS",
                        "CATTY_CACHE_HIT_INPUT_PRICE_RATIO",
                        "CATTY_GROUP_HISTORY_SCOPE",
                        "CATTY_HISTORY_TURNS",
                        "CATTY_SESSION_CONTEXT_ENABLED",
                        "CATTY_SESSION_CONTEXT_TARGET_TOKENS",
                        "CATTY_SESSION_CONTEXT_TRIM_TO_TOKENS",
                        "CATTY_SESSION_CONTEXT_HEADROOM_TOKENS",
                        "CATTY_SESSION_AI_COMPACT_ENABLED",
                        "CATTY_SESSION_AI_COMPACT_TRIGGER_TOKENS",
                        "CATTY_FADIANJI_STATIC_SCENE_BANK_ENABLED",
                        "CATTY_FADIANJI_STATIC_SCENE_BANK_MAX_CHARS",
                        "CATTY_SESSION_CACHE_PERSISTENCE_ENABLED",
                        "CATTY_SESSION_CACHE_DIR",
                        "CATTY_SESSION_CACHE_MAX_SESSIONS",
                        "CATTY_SESSION_CACHE_SAVE_DEBOUNCE_SECONDS",
                    )
                }

        self.assertEqual(values["CATTY_MODEL_CONTEXT_TOKENS"], "1000000")
        self.assertEqual(values["CATTY_CACHE_HIT_INPUT_PRICE_RATIO"], "0.02")
        self.assertEqual(values["CATTY_GROUP_HISTORY_SCOPE"], "group")
        self.assertEqual(values["CATTY_HISTORY_TURNS"], "16")
        self.assertEqual(values["CATTY_SESSION_CONTEXT_ENABLED"], "true")
        self.assertEqual(values["CATTY_SESSION_CONTEXT_TARGET_TOKENS"], "256000")
        self.assertEqual(values["CATTY_SESSION_CONTEXT_TRIM_TO_TOKENS"], "192000")
        self.assertEqual(values["CATTY_SESSION_CONTEXT_HEADROOM_TOKENS"], "32000")
        self.assertEqual(values["CATTY_SESSION_AI_COMPACT_ENABLED"], "true")
        self.assertEqual(values["CATTY_SESSION_AI_COMPACT_TRIGGER_TOKENS"], "512000")
        self.assertEqual(values["CATTY_FADIANJI_STATIC_SCENE_BANK_ENABLED"], "true")
        self.assertEqual(values["CATTY_FADIANJI_STATIC_SCENE_BANK_MAX_CHARS"], "140000")
        self.assertEqual(values["CATTY_SESSION_CACHE_PERSISTENCE_ENABLED"], "false")
        self.assertEqual(values["CATTY_SESSION_CACHE_DIR"], "tmp/sessions")
        self.assertEqual(values["CATTY_SESSION_CACHE_MAX_SESSIONS"], "7")
        self.assertEqual(values["CATTY_SESSION_CACHE_SAVE_DEBOUNCE_SECONDS"], "0.5")

        self.assertEqual(_loader.DEFAULT_CONFIG["chat"]["group_history_scope"], "group")
        self.assertTrue(_loader.DEFAULT_CONFIG["chat"]["session_context_enabled"])
        # 主人 2026-08-18: 历史预算收紧 720K→64K
        self.assertEqual(_loader.DEFAULT_CONFIG["chat"]["session_context_target_tokens"], 64_000)
        self.assertEqual(_loader.DEFAULT_CONFIG["chat"]["session_ai_compact_trigger_tokens"], 40_000)
        self.assertTrue(_loader.DEFAULT_CONFIG["chat"]["fadianji_static_scene_bank_enabled"])
        self.assertEqual(_loader.DEFAULT_CONFIG["ai"]["model_context_tokens"], 1_000_000)
        self.assertEqual(_loader.DEFAULT_CONFIG["ai"]["cache_hit_input_price_ratio"], 0.02)

    def test_session_cache_defaults_match_example_config(self) -> None:
        example = json.loads(
            (_MODULE_PATH.parent / "config.example.json").read_text(encoding="utf-8")
        )
        current = json.loads(
            (_MODULE_PATH.parent / "config.json").read_text(encoding="utf-8")
        )
        expected = {
            "session_cache_persistence_enabled": True,
            "session_cache_dir": "sessions",
            "session_cache_max_sessions": 200,
            "session_cache_save_debounce_seconds": 2.0,
        }

        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(_loader.DEFAULT_CONFIG["chat"][key], value)
                self.assertEqual(example["chat"][key], value)
                self.assertEqual(current["chat"][key], value)

if __name__ == "__main__":
    unittest.main()
