from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.get("catty_qq_ai")
if package is None:
    package = types.ModuleType("catty_qq_ai")
    package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
    sys.modules["catty_qq_ai"] = package


def load_module(name: str, relative_path: str):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


state = load_module("catty_qq_ai.dashboard_state", "src/catty_qq_ai/dashboard_state.py")
dashboard = load_module("catty_qq_ai.dashboard", "src/catty_qq_ai/dashboard.py")
client = load_module("catty_qq_ai.openai_client", "src/catty_qq_ai/openai_client.py")
native_client = load_module(
    "catty_qq_ai.anthropic_native_client",
    "src/catty_qq_ai/anthropic_native_client.py",
)


class DashboardStateTests(unittest.TestCase):
    def setUp(self) -> None:
        state._ACTIVE_STREAMS.clear()
        state._COMPLETED_STREAMS.clear()
        state._SSE_SUBSCRIBERS.clear()
        state._CREDIT_LEDGER.clear()
        state._USER_LATEST_CREDIT.clear()
        state._LATEST_USAGE_BY_SCOPE.clear()
        state._SCOPE_STATES.clear()

    def test_responses_cache_reads_writes_and_billing_reach_dashboard(self) -> None:
        data = client._convert_responses_data({
            "status": "completed",
            "output": [],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 10,
                "input_tokens_details": {
                    "cached_tokens": 400,
                    "cache_write_tokens": 300,
                    "cache_write_tokens_details": {"ttl_30m_tokens": 200, "ttl_12h_tokens": 100},
                },
            },
        })
        token = client.set_current_scope_key("private:cache-test")
        try:
            client._log_cache_stats(
                data,
                "gpt-6.1-terra",
                messages=[{"role": "user", "content": "synthetic"}],
                base_url="https://api.openai.com/v1/responses",
            )
        finally:
            client.reset_current_scope_key(token)
        latest = state.get_state_snapshot()["scope_state"]["private:cache-test"]["latest_cache_stats"]
        self.assertEqual(latest["cache_read"], 400)
        self.assertEqual(latest["cache_create"], 300)
        self.assertEqual(latest["input_tokens"], 300)
        self.assertEqual(latest["input_total"], 1000)
        self.assertAlmostEqual(latest["billed_input_equiv"], 820.0)

    def test_responses_nested_custom_cache_write_rate_is_preserved(self) -> None:
        normalized = state._normalize_cache_usage(
            {
                "prompt_tokens": 1000,
                "prompt_tokens_details": {
                    "cache_write_tokens": 300,
                    "cache_write_tokens_details": {"ttl_30m_tokens": 200, "ttl_12h_tokens": 100},
                },
            },
            model="gpt-6.1-terra",
            diagnostics={"billing_profile": {"name": "openai_terra", "cache_create_multiplier": 3.0}},
            billing_profile=None,
            cache_hit_billing_multiplier=None,
        )
        self.assertAlmostEqual(normalized["billed_input_equiv"], 1600.0)

    def test_deepseek_usage_keeps_actual_normalized_and_billing_separate(self) -> None:
        state.push_cache_stats(
            "private:1",
            {
                "prompt_cache_hit_tokens": 900,
                "prompt_cache_miss_tokens": 100,
                "completion_tokens": 7,
            },
            model="deepseek-v4-flash",
            diagnostics={
                "conversation_id": "private:1",
                "logical_turn_id": "turn-1",
                "request_kind": "chat",
                "request_seq": 1,
                "request_route": "main",
                "request_class": "chat",
                "unavoidable_current_turn_tokens": 100,
                "retained_input_tokens": 240_000,
                "history_tokens": 220_000,
                "target_context_tokens": 256_000,
                "model_context_tokens": 1_000_000,
                "cache_hit_billing_multiplier": 0.02,
                "hot99_eligible": 1,
                "hot99_eligible_count": 10,
                "hot99_rate": 0.99,
                "hot99_status": "PASS",
                "hot99_target": 0.99,
            },
        )

        latest = state.get_state_snapshot()["scope_state"]["private:1"]["latest_cache_stats"]
        self.assertEqual(latest["input_total"], 1_000)
        self.assertEqual(latest["output_tokens"], 7)
        self.assertAlmostEqual(latest["actual_hit_rate"], 0.9)
        self.assertAlmostEqual(latest["normalized_cache_kpi"], 1.0)
        self.assertAlmostEqual(latest["cache_read_billing_multiplier"], 0.02)
        self.assertAlmostEqual(latest["billed_input_equiv"], 118.0)
        self.assertEqual(latest["hot99_eligible"], 1)
        self.assertEqual(latest["hot99_eligible_count"], 10)
        self.assertEqual(latest["hot99_rate"], 0.99)
        self.assertEqual(latest["hot99_status"], "PASS")
        self.assertEqual(latest["hot99_target"], 0.99)
        self.assertEqual(latest["session_context"]["retained_input_tokens"], 240_000)
        self.assertEqual(latest["session_context"]["history_tokens"], 220_000)

    def test_auxiliary_request_does_not_overwrite_chat_or_settlement(self) -> None:
        state.push_cache_stats(
            "group:7",
            {
                "prompt_cache_hit_tokens": 90,
                "prompt_cache_miss_tokens": 10,
                "completion_tokens": 3,
            },
            model="deepseek-v4-flash",
            diagnostics={
                "conversation_id": "group:7",
                "logical_turn_id": "turn-7",
                "request_kind": "chat",
                "request_seq": 1,
                "request_route": "main",
                "request_class": "chat",
                "retained_input_tokens": 100_000,
            },
        )
        main_updated_at = state.get_state_snapshot()["scope_state"]["group:7"]["updated_at"]
        state.push_cache_stats(
            "group:7",
            {
                "prompt_cache_hit_tokens": 1,
                "prompt_cache_miss_tokens": 99,
                "completion_tokens": 11,
            },
            model="vision-model",
            diagnostics={
                "conversation_id": "group:7",
                "logical_turn_id": "turn-7",
                "request_kind": "auxiliary:vision",
                "request_seq": 2,
                "request_route": "vision",
                "request_class": "auxiliary",
                "retained_input_tokens": 1,
            },
        )

        snapshot = state.get_state_snapshot()["scope_state"]["group:7"]
        self.assertEqual(snapshot["model"], "deepseek-v4-flash")
        self.assertEqual(snapshot["updated_at"], main_updated_at)
        self.assertEqual(snapshot["latest_cache_stats"]["request_route"], "main")
        self.assertEqual(snapshot["latest_cache_stats"]["input_total"], 100)
        self.assertEqual(snapshot["session_context"]["retained_input_tokens"], 100_000)
        self.assertEqual(snapshot["latest_auxiliary_stats"]["vision"]["input_total"], 100)
        self.assertEqual(snapshot["latest_auxiliary_stats"]["vision"]["model"], "vision-model")
        self.assertEqual(snapshot["cache_rolling"]["rolling_n"], 1)
        self.assertEqual(len(snapshot["request_ledger"]), 2)
        self.assertIn(
            "vision-model",
            {entry["model"] for entry in snapshot["request_ledger"].values()},
        )
        self.assertEqual(
            state.pop_latest_usage("group:7"),
            {"prompt_tokens": 100, "completion_tokens": 3},
        )

    def test_native_zero_usage_does_not_overwrite_main_dashboard_state(self) -> None:
        state.push_cache_stats(
            "private:zero",
            {
                "prompt_cache_hit_tokens": 90,
                "prompt_cache_miss_tokens": 10,
            },
            model="deepseek-v4-flash",
            diagnostics={
                "conversation_id": "private:zero",
                "logical_turn_id": "turn-zero",
                "request_kind": "chat",
                "request_seq": 1,
                "request_route": "main",
                "request_class": "chat",
            },
        )
        before = state.get_state_snapshot()["scope_state"]["private:zero"]
        scope_token = client.set_current_scope_key("private:zero")
        try:
            native_client._log_native_usage({"usage": {}}, "claude-zero")
        finally:
            client.reset_current_scope_key(scope_token)
        after = state.get_state_snapshot()["scope_state"]["private:zero"]

        self.assertEqual(after["latest_cache_stats"], before["latest_cache_stats"])
        self.assertEqual(after["model"], before["model"])
        self.assertEqual(after["updated_at"], before["updated_at"])

    def test_same_logical_turn_aggregates_tool_round_usage(self) -> None:
        common = {
            "conversation_id": "private:2",
            "logical_turn_id": "turn-tools",
            "request_route": "main",
            "request_class": "chat",
        }
        state.push_cache_stats(
            "private:2",
            {"prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20, "completion_tokens": 4},
            model="deepseek-v4-flash",
            diagnostics={**common, "request_kind": "tool_initial", "request_seq": 1},
        )
        state.push_cache_stats(
            "private:2",
            {"prompt_cache_hit_tokens": 45, "prompt_cache_miss_tokens": 5, "completion_tokens": 6},
            model="deepseek-v4-flash",
            diagnostics={**common, "request_kind": "tool_followup", "request_seq": 2},
        )

        self.assertEqual(
            state.pop_latest_usage("private:2"),
            {"prompt_tokens": 150, "completion_tokens": 10},
        )

    def test_unknown_provider_uses_safe_default_profile(self) -> None:
        profile_name, profile = state._resolve_billing_profile(
            provider="other",
            model="custom-model",
            diagnostics=None,
            billing_profile=None,
            cache_hit_billing_multiplier=None,
        )
        self.assertEqual(profile_name, "other")
        self.assertAlmostEqual(profile["cache_read_multiplier"], 0.1)

    def test_dashboard_html_has_three_live_panels_and_best_effort_copy(self) -> None:
        html = asyncio.run(dashboard.dashboard_html())
        self.assertIn('id="ctx-live"', html)
        self.assertIn('id="session-live"', html)
        self.assertIn('id="cache-live"', html)
        self.assertIn("payload.request_class === 'auxiliary'", html)
        self.assertIn("best-effort", html)
        self.assertIn("数小时到数天", html)
        self.assertNotIn("cache_read 9折", html)
        self.assertNotIn("1h TTL", html)
        self.assertNotIn("10% 计费", html)


if __name__ == "__main__":
    unittest.main()
