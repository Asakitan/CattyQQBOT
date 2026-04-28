from pathlib import Path
import importlib.util
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "config.py"
_SPEC = importlib.util.spec_from_file_location("config", _MODULE_PATH)
_config = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_config)


class ConfigTests(unittest.TestCase):
    def test_special_care_user_ids_parse_from_json_strings(self) -> None:
        config = _config.Config(
            catty_special_care_user_ids="[1001, 1002]",
            catty_group_special_care_user_ids='{"12345":[1003,"1004"],"67890":"1005,1006"}',
        )

        self.assertEqual(config.catty_special_care_user_ids, {1001, 1002})
        self.assertEqual(config.catty_group_special_care_user_ids["12345"], {1003, 1004})
        self.assertEqual(config.catty_group_special_care_user_ids["67890"], {1005, 1006})

    def test_local_critic_config_parses_json_fields(self) -> None:
        config = _config.Config(
            catty_local_critic_base_url="http://127.0.0.1:11434/v1/",
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
            catty_local_training_collect_assistant_samples="true",
            catty_local_training_assistant_samples_path="training/main.jsonl",
        )

        self.assertEqual(config.catty_local_critic_base_url, "http://127.0.0.1:11434/v1")
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
        self.assertTrue(config.catty_local_training_collect_assistant_samples)
        self.assertEqual(config.catty_local_training_assistant_samples_path, "training/main.jsonl")


if __name__ == "__main__":
    unittest.main()
