from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from catty_self_repair.config import (
    ConfigError, DEFAULT_SELF_REPAIR, HARD_WEEKLY_LIMIT, RepairConfig,
    load_config, load_secret_values, state_dir,
)


class RepairConfigTests(unittest.TestCase):
    def test_default_is_disabled_independent_and_one_hundred_million(self):
        config = RepairConfig()
        self.assertFalse(config.enabled)
        self.assertFalse(config.configured)
        self.assertEqual(config.weekly_token_budget, 100_000_000)
        self.assertEqual(HARD_WEEKLY_LIMIT, 100_000_000)
        self.assertTrue(config.auto_apply)

    def test_loader_and_example_match_standalone_defaults(self):
        spec = importlib.util.spec_from_file_location("repair_test_config_loader", ROOT / "catty_config_loader.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(module.DEFAULT_CONFIG["self_repair"], DEFAULT_SELF_REPAIR)
        self.assertEqual(example["self_repair"], DEFAULT_SELF_REPAIR)

    def test_empty_local_key_is_allowed_for_local_llm_without_main_fallback(self):
        config = RepairConfig.from_mapping({"enabled": True, "base_url": "http://127.0.0.1:8000/v1", "model": "fixture"})
        self.assertTrue(config.configured)
        self.assertEqual(config.api_key, "")

    def test_placeholders_do_not_configure_a_route(self):
        config = RepairConfig.from_mapping({"enabled": True, "base_url": "HOST", "api_key": "TOKEN", "model": "TARGET"})
        self.assertFalse(config.configured)

    def test_invalid_settings_are_rejected_together(self):
        invalid = (
            {"enabled": "true"}, {"weekly_token_budget": 100_000_001},
            {"weekly_token_budget": -1}, {"weekly_token_budget": True},
            {"poll_seconds": float("nan")}, {"poll_seconds": 0},
            {"max_rounds": 0}, {"base_url": "file:///secret"},
            {"base_url": "https://u:p@example.test/v1"},
            {"base_url": "https://example.test/v1?token=secret"},
            {"base_url": "https://example.test/v1/messages"},
            {"http_proxy": "file:///proxy"},
            {"extra_body": {"max_tokens": 999999}},
            {"extra_headers": {"Host": "elsewhere"}},
            {"extra_headers": {"X-Test": "bad\nheader"}},
            {"log_paths": ["config.json"]}, {"log_paths": ["logs/../config.json"]},
            {"allowed_paths": ["src/catty_self_repair"]},
            {"allowed_paths": ["src\\catty_qq_ai"]},
            {"test_modules": []}, {"test_modules": ["os"]},
            {"health_grace_seconds": 200}, {"unknown": True},
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises((ConfigError, ValueError)):
                RepairConfig.from_mapping(raw)

    def test_disabled_config_does_not_inherit_ai_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({"ai": {"base_url": "https://main.test/v1", "api_key": "private", "model": "main"}}), encoding="utf-8")
            config = load_config(root)
            self.assertEqual(config, RepairConfig())
            self.assertFalse(config.configured)

    def test_enabled_missing_model_inherits_the_whole_main_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for missing in ("", "TARGET"):
                document = {"ai": {"base_url": "https://main.test/v1/responses", "api_key": "main-secret",
                                   "model": "main-model", "http_proxy": "socks5h://127.0.0.1:11080"},
                            "self_repair": {"enabled": True, "base_url": "HOST", "api_key": "TOKEN", "model": missing}}
                (root / "config.json").write_text(json.dumps(document), encoding="utf-8")
                config = load_config(root)
                self.assertTrue(config.configured)
                self.assertEqual((config.base_url, config.api_key, config.model, config.http_proxy),
                                 ("https://main.test/v1/responses", "main-secret", "main-model", "socks5h://127.0.0.1:11080"))
                self.assertEqual(config.weekly_token_budget, HARD_WEEKLY_LIMIT)
                self.assertNotIn("main-secret", repr(config))
                self.assertNotIn("http_proxy", config.public_dict())

    def test_explicit_model_never_mixes_main_route_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = {"ai": {"base_url": "https://main.test/v1", "api_key": "main-secret", "model": "main"},
                        "self_repair": {"enabled": True, "model": "explicit", "base_url": "HOST", "api_key": "TOKEN"}}
            (root / "config.json").write_text(json.dumps(document), encoding="utf-8")
            config = load_config(root)
            self.assertFalse(config.configured)
            self.assertEqual((config.base_url, config.api_key, config.model), ("HOST", "TOKEN", "explicit"))

    def test_inherited_route_changes_invalidate_config_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = {"ai": {"base_url": "https://main.test/v1/responses", "api_key": "first", "model": "main"},
                        "self_repair": {"enabled": True}}
            path = root / "config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            first = load_config(root).fingerprint()
            document["ai"]["api_key"] = "second"
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertNotEqual(first, load_config(root).fingerprint())

    def test_missing_main_model_stays_unconfigured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({"ai": {"base_url": "https://main.test/v1"},
                                                           "self_repair": {"enabled": True}}), encoding="utf-8")
            self.assertFalse(load_config(root).configured)

    def test_bad_file_is_not_silently_replaced_by_enabled_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(load_config(root).enabled)
            (root / "config.json").write_text("{bad", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_secrets_only_are_collected_and_public_status_has_none(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({
                "ai": {"api_key": "fixture-key", "model": "not-secret"},
                "nsfw_search": {"pixiv_cookie": "fixture-cookie"},
                "self_repair": {"extra_headers": {"X-Auth": "fixture-header"}},
            }), encoding="utf-8")
            self.assertEqual(set(load_secret_values(root)), {"fixture-key", "fixture-cookie", "fixture-header"})
            config = RepairConfig(api_key="fixture-key", extra_headers={"X-Auth": "fixture-header"})
            self.assertNotIn("fixture-key", repr(config))
            self.assertNotIn("fixture-header", json.dumps(config.public_dict()))
            self.assertNotEqual(config.fingerprint(), RepairConfig().fingerprint())
            self.assertEqual(state_dir(root), root / "data/self_repair")


if __name__ == "__main__":
    unittest.main()