from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "context_buckets.py"
    spec = importlib.util.spec_from_file_location("catty_context_buckets_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TimeBucketContextStoreTests(unittest.TestCase):
    def test_time_bucket_rollover_finalizes_previous_bucket(self) -> None:
        import tempfile

        mod = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            store = mod.TimeBucketContextStore(tmp, group_minutes=15, private_minutes=30)
            store.record_turn("group:1", "主人第一句", "猫猫第一句", is_group=True, now=1704110460)  # 12:01

            self.assertEqual(store.build_stable_summary_prompt("group:1"), "")
            self.assertTrue(store.roll_current_if_needed("group:1", is_group=True, now=1704111360))  # 12:16

            summary = store.build_stable_summary_prompt("group:1")
            self.assertIn("【时间桶上下文摘要】", summary)
            self.assertIn("bucket=", summary)
            self.assertIn("主人第一句", summary)
            params = store.build_current_params_prompt("group:1", is_group=True, now=1704111360)
            self.assertIn("【TIME_BUCKET】", params)
            self.assertIn("s=group", params)
            self.assertIn("cur=0", params)
            self.assertIn("fin=1", params)

    def test_time_bucket_same_bucket_does_not_roll(self) -> None:
        import tempfile

        mod = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            store = mod.TimeBucketContextStore(tmp, group_minutes=15)
            store.record_turn("group:2", "同桶第一句", "同桶回复", is_group=True, now=1704110460)

            self.assertFalse(store.roll_current_if_needed("group:2", is_group=True, now=1704110700))
            self.assertEqual(store.build_stable_summary_prompt("group:2"), "")
            params = store.build_current_params_prompt("group:2", is_group=True, now=1704110700)
            self.assertIn("cur=1", params)
            self.assertIn("fin=0", params)


if __name__ == "__main__":
    unittest.main()
