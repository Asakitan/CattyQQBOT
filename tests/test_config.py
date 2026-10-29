from pathlib import Path
import importlib.util
import unittest
from unittest.mock import patch

from pydantic import ValidationError


_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "config.py"
_SPEC = importlib.util.spec_from_file_location("config", _MODULE_PATH)
_config = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_config)

_BILLING_PATH = _MODULE_PATH.with_name("token_billing.py")
_BILLING_SPEC = importlib.util.spec_from_file_location("token_billing", _BILLING_PATH)
_billing = importlib.util.module_from_spec(_BILLING_SPEC)
assert _BILLING_SPEC.loader is not None
_BILLING_SPEC.loader.exec_module(_billing)


class ConfigTests(unittest.TestCase):
    def test_vision_image_direct_download_defaults_off(self) -> None:
        self.assertFalse(_config.Config().catty_vision_image_download_direct)
        self.assertTrue(_config.Config(catty_vision_image_download_direct=True).catty_vision_image_download_direct)

    def test_group_hourly_quota_default_is_ten_times_previous_limit(self) -> None:
        quota = _config.Config().catty_group_hourly_token_quota
        self.assertEqual(quota, 30_000_000)
        self.assertEqual(quota, 10 * 3_000_000)

    def test_group_hourly_quota_preserves_explicit_values(self) -> None:
        for quota in (0, 1, 3_000_000, 30_000_001):
            with self.subTest(quota=quota):
                config = _config.Config(catty_group_hourly_token_quota=quota)
                self.assertEqual(config.catty_group_hourly_token_quota, quota)

    def test_group_hourly_quota_boundary_and_reset(self) -> None:
        quota = _config.Config().catty_group_hourly_token_quota
        with patch.dict(_billing._GROUP_HOURLY, {}, clear=True), \
             patch.object(_billing.time, "time", return_value=3600) as clock:
            first_user = "7"
            second_user = "8"
            self.assertEqual(_billing.add_group_usage(first_user, quota - 1), quota - 1)
            self.assertEqual(_billing.group_quota_exceeded(first_user, quota), (False, False))
            self.assertEqual(_billing.group_quota_exceeded(second_user, quota), (False, False))
            self.assertEqual(_billing.add_group_usage(first_user, 1), quota)
            self.assertEqual(_billing.group_quota_exceeded(first_user, quota), (True, False))
            _billing.group_quota_mark_notified(first_user)
            self.assertEqual(_billing.group_quota_exceeded(first_user, quota), (True, True))
            clock.return_value = 7200
            self.assertEqual(_billing.group_quota_exceeded(first_user, quota), (False, False))
            self.assertEqual(_billing.group_quota_used(first_user), 0)

    def test_removed_system_prompt_is_not_a_config_field(self) -> None:
        config = _config.Config()

        self.assertNotIn("catty_system_prompt", _config.Config.model_fields)
        self.assertNotIn("catty_system_prompt", _config.Config.model_json_schema()["properties"])
        self.assertFalse(hasattr(config, "catty_system_prompt"))
        self.assertNotIn("catty_system_prompt", config.model_dump())

    def test_legacy_system_prompt_input_is_ignored_without_changing_other_fields(self) -> None:
        values = {"catty_trigger_prefixes": ["TARGET"], "catty_reply_max_chars": 1200}
        expected = _config.Config(**values)
        actual = _config.Config(catty_system_prompt="legacy prompt", **values)

        self.assertEqual(actual.model_dump(), expected.model_dump())
        self.assertFalse(hasattr(actual, "catty_system_prompt"))

    def test_evolution_judge_defaults_to_deepseek(self) -> None:
        self.assertEqual(_config.Config().catty_evolution_judge_model, "deepseek-v4.1-flash")
        self.assertEqual(_config.Config().catty_evolution_judge_base_url, "")

    def test_model_routes_are_config_backed(self) -> None:
        config = _config.Config()
        self.assertEqual(config.catty_rag_local_bge_model, "BAAI/bge-small-zh-v1.5")
        self.assertEqual(config.catty_embedding_model, "text-embedding-3-small")
        self.assertEqual(config.catty_imagegen_pixelbunny_model, "seedream-5-pro")
        self.assertEqual(
            config.catty_imagegen_nai_endpoint,
            "https://image.novelai.net/ai/generate-image",
        )
        self.assertEqual(
            config.catty_imagegen_nai_augment_endpoint,
            "https://image.novelai.net/ai/augment-image",
        )
        self.assertEqual(config.catty_cpu_engine_l4_ollama_base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(config.catty_cpu_engine_l4_ollama_model, "qwen2.5:7b")

    def test_session_context_defaults_match_runtime_config(self) -> None:
        config = _config.Config()

        self.assertTrue(config.catty_session_context_enabled)
        self.assertEqual(config.catty_group_history_scope, "group")
        # 主人 2026-08-18: 群聊历史预算收紧 720K→64K (防 prompt 膨胀 160K+)
        self.assertEqual(config.catty_session_context_target_tokens, 64_000)
        self.assertEqual(config.catty_session_context_trim_to_tokens, 48_000)
        self.assertEqual(config.catty_session_context_headroom_tokens, 8_000)
        self.assertEqual(config.catty_session_ai_compact_trigger_tokens, 40_000)
        self.assertTrue(config.catty_fadianji_static_scene_bank_enabled)
        self.assertEqual(config.catty_fadianji_static_scene_bank_max_chars, 140_000)

    def test_session_context_budget_rejects_invalid_watermarks(self) -> None:
        invalid_cases = (
            {"catty_session_context_trim_to_tokens": 0},
            {
                "catty_session_context_target_tokens": 192_000,
                "catty_session_context_trim_to_tokens": 192_000,
            },
            {
                "catty_session_context_target_tokens": 192_000,
                "catty_session_context_trim_to_tokens": 193_000,
            },
            {
                "catty_model_context_tokens": 255_999,
                "catty_session_context_target_tokens": 256_000,
            },
            {"catty_session_context_headroom_tokens": 0},
            {"catty_session_context_headroom_tokens": 80_000},
            {"catty_session_context_headroom_tokens": 80_001},
        )
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                _config.Config(**values)

    def test_session_context_budget_allows_target_equal_model_limit(self) -> None:
        config = _config.Config(
            catty_model_context_tokens=256_000,
            catty_session_context_target_tokens=256_000,
            catty_session_context_trim_to_tokens=192_000,
            catty_session_context_headroom_tokens=32_000,
        )

        self.assertEqual(config.catty_session_context_target_tokens, 256_000)
        self.assertEqual(config.catty_model_context_tokens, 256_000)

    def test_group_history_scope_normalizes_whitespace_and_case(self) -> None:
        self.assertEqual(
            _config.Config(catty_group_history_scope=" USER ").catty_group_history_scope,
            "user",
        )

    def test_reply_delivery_defaults_enable_group_quotes_and_mixed_emoji(self) -> None:
        config = _config.Config()

        self.assertTrue(config.catty_reply_mix_emoji_with_text)
        self.assertTrue(config.catty_reply_quote_enabled)
        self.assertFalse(config.catty_reply_quote_private_enabled)

    def test_emoji_diversity_defaults_are_enabled(self) -> None:
        config = _config.Config()

        self.assertFalse(config.catty_emoji_auto_fallback_enabled)
        self.assertTrue(config.catty_emoji_diversity_enabled)
        self.assertEqual(config.catty_emoji_diversity_recent_window, 8)
        self.assertEqual(config.catty_emoji_diversity_candidate_pool, 6)

    def test_hot_reload_defaults_are_enabled(self) -> None:
        config = _config.Config()

        self.assertTrue(config.catty_hot_reload_enabled)
        self.assertEqual(config.catty_hot_reload_poll_seconds, 1.5)
        self.assertEqual(config.catty_hot_reload_debounce_seconds, 1.0)
        self.assertTrue(config.catty_hot_reload_restart_on_code_change)

    def test_special_care_user_ids_parse_from_json_strings(self) -> None:
        config = _config.Config(
            catty_special_care_user_ids="[1001, 1002]",
            catty_group_special_care_user_ids='{"12345":[1003,"1004"],"67890":"1005,1006"}',
        )

        self.assertEqual(config.catty_special_care_user_ids, {1001, 1002})
        self.assertEqual(config.catty_group_special_care_user_ids["12345"], {1003, 1004})
        self.assertEqual(config.catty_group_special_care_user_ids["67890"], {1005, 1006})

    def test_game_context_group_ids_parse_from_strings(self) -> None:
        config = _config.Config(
            catty_game_context_star_resonance_group_ids="[477970838, 578305908]",
            catty_game_context_strinova_group_ids="1001,1002",
        )

        self.assertEqual(config.catty_game_context_star_resonance_group_ids, {477970838, 578305908})
        self.assertEqual(config.catty_game_context_strinova_group_ids, {1001, 1002})

    def test_local_critic_config_parses_json_fields(self) -> None:
        config = _config.Config(
            catty_local_critic_base_url="http://127.0.0.1:11434/v1/",
            catty_local_critic_mode="gate",
            catty_local_critic_extra_headers='{"X-Test":"ok"}',
            catty_local_critic_extra_body='{"num_ctx":2048,"keep_alive":"45m"}',
            catty_local_critic_rewrite_when_score_below="70",
            catty_local_critic_reply_gate_min_confidence="60",
            catty_local_critic_reply_gate_examples="8",
            catty_local_critic_reply_gate_max_tokens="24",
            catty_local_critic_reply_gate_request_timeout="3.5",
            catty_local_critic_reply_gate_user_message_chars="180",
            catty_local_critic_reply_gate_plain_text_chars="90",
            catty_local_critic_reply_gate_context_chars="120",
            catty_local_critic_warmup_enabled="true",
            catty_local_critic_warmup_keep_alive="45m",
            catty_local_critic_warmup_interval_seconds="900",
            catty_local_critic_warmup_request_timeout="30",
            catty_local_training_enabled="true",
            catty_local_training_collect_assistant_samples="true",
            catty_local_training_assistant_samples_path="training/main.jsonl",
        )

        self.assertEqual(config.catty_local_critic_base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(config.catty_local_critic_mode, "reply_gate_only")
        self.assertEqual(config.catty_local_critic_extra_headers, {"X-Test": "ok"})
        self.assertEqual(config.catty_local_critic_extra_body, {"num_ctx": 2048, "keep_alive": "45m"})
        self.assertEqual(config.catty_local_critic_rewrite_when_score_below, 70)
        self.assertEqual(config.catty_local_critic_reply_gate_min_confidence, 60)
        self.assertEqual(config.catty_local_critic_reply_gate_examples, 8)
        self.assertEqual(config.catty_local_critic_reply_gate_max_tokens, 24)
        self.assertEqual(config.catty_local_critic_reply_gate_request_timeout, 3.5)
        self.assertEqual(config.catty_local_critic_reply_gate_user_message_chars, 180)
        self.assertEqual(config.catty_local_critic_reply_gate_plain_text_chars, 90)
        self.assertEqual(config.catty_local_critic_reply_gate_context_chars, 120)
        self.assertTrue(config.catty_local_critic_warmup_enabled)
        self.assertEqual(config.catty_local_critic_warmup_keep_alive, "45m")
        self.assertEqual(config.catty_local_critic_warmup_interval_seconds, 900)
        self.assertEqual(config.catty_local_critic_warmup_request_timeout, 30)
        self.assertTrue(config.catty_local_training_enabled)
        self.assertTrue(config.catty_local_training_collect_assistant_samples)
        self.assertEqual(config.catty_local_training_assistant_samples_path, "training/main.jsonl")


if __name__ == "__main__":
    unittest.main()
