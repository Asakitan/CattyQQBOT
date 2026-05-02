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


if __name__ == "__main__":
    unittest.main()
