from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "src/catty_qq_ai/task_contracts.py"
SPEC = importlib.util.spec_from_file_location("task_contracts_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class TaskContractTests(unittest.TestCase):
    def test_defaults_match_approved_proposal(self):
        policy = MODULE.TaskPolicy.from_mapping()
        self.assertEqual(policy.worker_model, "deepseek-v4.1-flash")
        self.assertEqual(policy.daily_token_limit, 600_000)
        self.assertEqual(policy.main_input_limit, 0)
        self.assertEqual(policy.main_input_target, 22_000)
        self.assertEqual(policy.tier("light").free_per_day, 3)
        self.assertEqual(policy.tier("standard").points, 80)
        self.assertEqual(policy.tier("deep").max_tokens, 256_000)

    def test_rejects_privilege_and_budget_shape_errors(self):
        for raw in ({"max_depth": 2}, {"daily_token_limit": True}, {"worker_model": "gpt-main"}, {"tiers": {"other": {}}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                MODULE.TaskPolicy.from_mapping(raw)
        with self.assertRaises(ValueError):
            MODULE.TaskIdentity("123", "group:1", is_owner="true")
        with self.assertRaises(ValueError):
            MODULE.TaskPolicy.from_mapping({"main_input_target": 24000, "main_input_limit": 22000})

    def test_tier_overrides_do_not_mutate_defaults(self):
        policy = MODULE.TaskPolicy.from_mapping({"tiers": {"light": {"points": 25}}})
        self.assertEqual(policy.tier("light").points, 25)
        self.assertEqual(MODULE.DEFAULT_TIERS["light"].points, 20)

    def test_identity_and_result_are_serializable(self):
        identity = MODULE.TaskIdentity("123", "group:456")
        self.assertEqual(identity.as_dict()["user_id"], "123")
        self.assertEqual(MODULE.TaskResult("done").as_dict()["summary"], "done")

    def test_storage_identity_excludes_permissions_but_preserves_scope(self):
        identity = MODULE.TaskIdentity("123", "group:456")
        owner = MODULE.TaskIdentity("123", "group:456", is_owner=True)
        other = MODULE.TaskIdentity("123", "private:123")
        self.assertEqual(identity.ownership_key, owner.ownership_key)
        self.assertNotEqual(identity.ownership_key, other.ownership_key)


if __name__ == "__main__":
    unittest.main()