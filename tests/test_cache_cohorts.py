from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


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


metrics = load_module("catty_qq_ai.cache_metrics", "src/catty_qq_ai/cache_metrics.py")
compressor = load_module(
    "catty_qq_ai.nlu.prompt_compressor",
    "src/catty_qq_ai/nlu/prompt_compressor.py",
)
client = load_module("catty_qq_ai.openai_client", "src/catty_qq_ai/openai_client.py")
dashboard_state = load_module(
    "catty_qq_ai.dashboard_state",
    "src/catty_qq_ai/dashboard_state.py",
)
native_client = load_module(
    "catty_qq_ai.anthropic_native_client",
    "src/catty_qq_ai/anthropic_native_client.py",
)


class CacheCohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self._module_restore = {
            "catty_qq_ai.cache_metrics": sys.modules.get("catty_qq_ai.cache_metrics"),
            "catty_qq_ai.nlu.prompt_compressor": sys.modules.get(
                "catty_qq_ai.nlu.prompt_compressor"
            ),
            "catty_qq_ai.openai_client": sys.modules.get("catty_qq_ai.openai_client"),
            "catty_qq_ai.dashboard_state": sys.modules.get("catty_qq_ai.dashboard_state"),
            "catty_qq_ai.anthropic_native_client": sys.modules.get(
                "catty_qq_ai.anthropic_native_client"
            ),
        }
        sys.modules["catty_qq_ai.cache_metrics"] = metrics
        sys.modules["catty_qq_ai.nlu.prompt_compressor"] = compressor
        sys.modules["catty_qq_ai.openai_client"] = client
        sys.modules["catty_qq_ai.dashboard_state"] = dashboard_state
        sys.modules["catty_qq_ai.anthropic_native_client"] = native_client
        metrics._ROLLING.clear()
        metrics._LOW_STREAK.clear()
        metrics._COHORT_ROLLING.clear()
        metrics._COHORT_METADATA.clear()
        metrics._COHORT_LAST.clear()
        client._SESSION_TOKEN_RATIO_SAMPLES.clear()
        compressor.reset_scope_anchor()
        compressor.clear_anchor_observation()

    def tearDown(self) -> None:
        for name, module in self._module_restore.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def metadata(self, *, tool_hash: str = "tool-a", **overrides: object) -> dict:
        values = {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "route": "openai_compat",
            "prompt_variant": "session",
            "scope_type": "private",
            "persona": "catty",
            "warm": 1,
            "tool_set_hash": tool_hash,
            "trim_epoch": 0,
            "request_kind": "chat",
            "request_class": "chat",
            "anchor_changed": False,
            "anchor_observed": True,
        }
        values.update(overrides)
        return metrics.build_cohort_metadata(**values)

    def test_hot99_cohort_partitions_new_stable_dimensions(self) -> None:
        legacy = metrics.build_cohort_metadata(
            provider="deepseek",
            model="deepseek-v4-flash",
            route="openai_compat",
            scope_type="private",
            persona="catty",
            warm=1,
            tool_set_hash="tool-a",
            anchor_changed=False,
        )
        self.assertEqual(legacy["prompt_variant"], "-")
        self.assertEqual(legacy["trim_epoch"], 0)
        self.assertEqual(legacy["request_kind"], "chat")
        self.assertEqual(legacy["request_class"], "-")
        self.assertEqual(legacy["anchor_observed"], 0)

        variants = (
            self.metadata(prompt_variant="session"),
            self.metadata(prompt_variant="legacy"),
            self.metadata(trim_epoch=1),
            self.metadata(request_kind="chat_followup"),
        )
        stats = [metrics.record_cohort_hit(metadata, 100_000, 0) for metadata in variants]
        self.assertEqual(len({item.cohort_key for item in stats}), len(stats))
        self.assertTrue(all(item.roll_n == 1 for item in stats))

    def test_cohort_key_ignores_eligibility_metadata_flags(self) -> None:
        base_key = metrics.build_cohort_key(self.metadata())

        self.assertEqual(base_key, metrics.build_cohort_key(self.metadata(warm=0)))
        self.assertEqual(base_key, metrics.build_cohort_key(self.metadata(anchor_changed=True)))
        self.assertEqual(base_key, metrics.build_cohort_key(self.metadata(anchor_observed=False)))
        self.assertEqual(base_key, metrics.build_cohort_key(self.metadata(request_class="auxiliary")))
        self.assertNotEqual(
            base_key,
            metrics.build_cohort_key(self.metadata(request_kind="chat_followup")),
        )

    def test_request_class_does_not_rewrite_request_kind_dimension(self) -> None:
        metadata = metrics.build_cohort_metadata(
            provider="deepseek",
            model="deepseek-v4-flash",
            route="main",
            scope_type="private",
            persona="catty",
            warm=1,
            tool_set_hash="tool-a",
            anchor_changed=False,
            request_kind="chat",
            request_class="auxiliary",
        )

        self.assertEqual(metadata["request_kind"], "chat")
        self.assertEqual(metadata["request_class"], "auxiliary")

    def test_hot99_requires_ten_eligible_samples(self) -> None:
        stats = None
        for _ in range(9):
            stats = metrics.record_cohort_hit(self.metadata(), 99_000, 1_000)
        assert stats is not None
        self.assertEqual(stats.hot99_eligible, 1)
        self.assertEqual(stats.hot99_eligible_count, 9)
        self.assertEqual(stats.hot99_raw_rate, 0.99)
        self.assertEqual(stats.hot99_status, "N/A")

    def test_hot99_passes_at_ten_token_weighted_raw_samples(self) -> None:
        stats = None
        for _ in range(10):
            stats = metrics.record_cohort_hit(self.metadata(), 99_000, 1_000)
        assert stats is not None
        self.assertEqual(stats.hot99_eligible_count, 10)
        self.assertEqual(stats.hot99_raw_rate, 0.99)
        self.assertEqual(stats.hot99_status, "PASS")
        self.assertEqual(stats.hot99_target, 0.99)
        snapshot = metrics.cohort_stats_snapshot()[stats.cohort_key]
        self.assertEqual(snapshot["hot99_eligible"], 1)
        self.assertEqual(snapshot["hot99_eligible_count"], 10)
        self.assertEqual(snapshot["hot99_raw_rate"], 0.99)
        self.assertEqual(snapshot["hot99_status"], "PASS")
        self.assertEqual(snapshot["hot99_target"], 0.99)

    def test_request_diagnostics_receive_dashboard_hot99_fields(self) -> None:
        diagnostics = {
            "cohort_metadata": self.metadata(),
            "unavoidable_current_turn_tokens": 1_000,
        }
        result = None
        for _ in range(10):
            result = client._record_cache_cohort_diagnostics(
                diagnostics,
                hit_tok=99_000,
                miss_tok=1_000,
                create_tok=0,
            )
        assert result is not None
        self.assertEqual(result["hot99_eligible"], 1)
        self.assertEqual(result["hot99_eligible_count"], 10)
        self.assertEqual(result["hot99_rate"], 0.99)
        self.assertEqual(result["hot99_raw_rate"], 0.99)
        self.assertEqual(result["hot99_status"], "PASS")
        self.assertEqual(result["hot99_target"], 0.99)

    def test_hot99_uses_raw_provider_rate_not_normalized_kpi(self) -> None:
        stats = None
        for _ in range(10):
            stats = metrics.record_cohort_hit(
                self.metadata(prompt_variant="normalized"),
                98_000,
                2_000,
                unavoidable_current_turn_tokens=2_000,
            )
        assert stats is not None
        self.assertEqual(stats.normalized_rate, 1.0)
        self.assertEqual(stats.hot99_raw_rate, 0.98)
        self.assertEqual(stats.hot99_status, "FAIL")

    def test_hot99_rejects_ineligible_samples_without_counting_them(self) -> None:
        cases = (
            (self.metadata(warm=0), 100_000, 0),
            (self.metadata(anchor_changed=True), 100_000, 0),
            (self.metadata(prompt_variant="short"), 99_999, 0),
            (self.metadata(request_kind="summary"), 100_000, 0),
            (self.metadata(request_class=None), 100_000, 0),
            (self.metadata(request_class="user_chat"), 100_000, 0),
        )
        for metadata, hit_tok, miss_tok in cases:
            stats = metrics.record_cohort_hit(metadata, hit_tok, miss_tok)
            self.assertEqual(stats.hot99_eligible, 0)
            self.assertEqual(stats.hot99_eligible_count, 0)
            self.assertEqual(stats.hot99_status, "N/A")

        auxiliary_class = dict(self.metadata(prompt_variant="auxiliary-class"))
        auxiliary_class["request_class"] = "auxiliary"
        stats = metrics.record_cohort_hit(auxiliary_class, 100_000, 0)
        self.assertEqual(stats.hot99_eligible, 0)
        self.assertEqual(stats.hot99_eligible_count, 0)

        missing_class_alias = dict(self.metadata(prompt_variant="missing-class-alias"))
        missing_class_alias.pop("request_class", None)
        missing_class_alias["request_type"] = "chat"
        stats = metrics.record_cohort_hit(missing_class_alias, 100_000, 0)
        self.assertEqual(stats.hot99_eligible, 0)
        self.assertEqual(stats.hot99_eligible_count, 0)

    def test_hot99_requires_an_explicit_anchor_observation(self) -> None:
        missing = self.metadata(anchor_observed=False)
        observed = self.metadata(anchor_observed=True)
        missing_stats = metrics.record_cohort_hit(missing, 100_000, 0)
        observed_stats = metrics.record_cohort_hit(observed, 100_000, 0)

        self.assertEqual(metrics.build_cohort_key(missing), metrics.build_cohort_key(observed))
        self.assertEqual(missing_stats.hot99_eligible, 0)
        self.assertEqual(missing_stats.hot99_eligible_count, 0)
        self.assertEqual(missing_stats.hot99_status, "N/A")
        self.assertEqual(observed_stats.hot99_eligible, 1)

    def test_missing_runtime_anchor_is_marked_ineligible(self) -> None:
        with patch.object(compressor, "get_anchor_observation", return_value=None):
            diagnostics = client._build_cache_request_diagnostics(
                base_url="https://api.deepseek.com/v1",
                model="deepseek-v4-flash",
                payload={"messages": [{"role": "user", "content": "ping"}]},
            )

        self.assertFalse(diagnostics["anchor_observed"])
        self.assertEqual(diagnostics["cohort_metadata"]["anchor_observed"], 0)

    def test_openai_write_lifetime_log_uses_receipt_or_explicit_unknown(self) -> None:
        for request_class, breakdown, expected in (
            ("chat", {"ttl_30m_tokens": 4818, "ttl_12h_tokens": 30}, "write_30m=4818 write_12h=30"),
            ("auxiliary", {"ttl_30m_tokens": 4818, "ttl_12h_tokens": 30}, "write_30m=4818 write_12h=30"),
            ("chat", None, "write_30m=unknown write_12h=unknown"),
        ):
            with self.subTest(request_class=request_class, breakdown=breakdown):
                details = {"cached_tokens": 169950, "cache_write_tokens": 4848}
                if breakdown is not None:
                    details["cache_write_tokens_details"] = breakdown
                token = client._current_cache_request_diagnostics_var.set({
                    "request_class": request_class, "request_route": "receipt_test",
                })
                try:
                    with patch.object(client._logger, "info") as logged:
                        client._log_cache_stats(
                            {"id": "resp_fixture", "usage": {"prompt_tokens": 182468, "prompt_tokens_details": details}},
                            "gpt-6.1-terra", base_url="https://api.openai.com/v1/responses",
                        )
                finally:
                    client._current_cache_request_diagnostics_var.reset(token)
                lines = [str(call.args[0]) for call in logged.call_args_list if call.args]
                receipts = [line for line in lines if line.startswith("responses cache writes ")]
                self.assertEqual(len(receipts), 1)
                self.assertIn("route=receipt_test", receipts[0])
                self.assertIn(expected, receipts[0])
                self.assertIn("response_id=resp_fixture", receipts[0])

    def test_auxiliary_usage_stays_out_of_shared_rolling_metrics(self) -> None:
        diagnostics = {
            "request_class": "auxiliary",
            "request_route": "vision",
            "local_input_tokens": 100,
            "cohort_metadata": self.metadata(
                provider="deepseek",
                route="vision",
                request_class="auxiliary",
            ),
        }
        token = client._current_cache_request_diagnostics_var.set(diagnostics)
        try:
            with (
                patch.object(client._logger, "info") as openai_log,
                patch.object(dashboard_state, "push_cache_stats") as openai_dashboard,
            ):
                client._log_cache_stats(
                    {
                        "usage": {
                            "prompt_cache_hit_tokens": 75,
                            "prompt_cache_miss_tokens": 25,
                        },
                    },
                    "deepseek-v4-flash",
                    messages=[{"role": "user", "content": "ping"}],
                    base_url="https://api.deepseek.com/v1",
                )
        finally:
            client._current_cache_request_diagnostics_var.reset(token)

        self.assertEqual(metrics._ROLLING, {})
        self.assertEqual(client._SESSION_TOKEN_RATIO_SAMPLES, {})
        self.assertEqual(len(metrics._COHORT_ROLLING), 1)
        openai_dashboard.assert_called_once()
        self.assertFalse(any(
            call.args and str(call.args[0]).startswith("HIT_TARGET")
            for call in openai_log.call_args_list
        ))

    def test_auxiliary_native_usage_stays_out_of_shared_rolling_metrics(self) -> None:
        diagnostics = {
            "provider": "claude",
            "request_class": "auxiliary",
            "request_route": "vision",
            "local_input_tokens": 100,
            "cohort_metadata": self.metadata(
                provider="claude",
                model="claude-sonnet",
                route="vision",
                request_class="auxiliary",
            ),
        }
        token = client._current_cache_request_diagnostics_var.set(diagnostics)
        try:
            with (
                patch.object(native_client.logger, "info") as native_log,
                patch.object(dashboard_state, "push_cache_stats") as native_dashboard,
            ):
                native_client._log_native_usage(
                    {
                        "usage": {
                            "cache_read_input_tokens": 75,
                            "input_tokens": 25,
                        },
                    },
                    "claude-sonnet",
                    messages=[{"role": "user", "content": "ping"}],
                )
        finally:
            client._current_cache_request_diagnostics_var.reset(token)

        self.assertEqual(metrics._ROLLING, {})
        self.assertEqual(client._SESSION_TOKEN_RATIO_SAMPLES, {})
        self.assertEqual(len(metrics._COHORT_ROLLING), 1)
        native_dashboard.assert_called_once()
        self.assertFalse(any(
            call.args and str(call.args[0]).startswith("HIT_TARGET")
            for call in native_log.call_args_list
        ))

    def test_canonical_hash_and_cohort_key_are_stable(self) -> None:
        left = {
            "function": {
                "name": "lookup",
                "parameters": {"properties": {"b": {"type": "string"}, "a": {"type": "number"}}},
            }
        }
        right = {
            "function": {
                "parameters": {"properties": {"a": {"type": "number"}, "b": {"type": "string"}}},
                "name": "lookup",
            }
        }
        self.assertEqual(metrics.canonical_json(left), metrics.canonical_json(right))
        self.assertEqual(metrics.canonical_sha256(left), metrics.canonical_sha256(right))
        self.assertEqual(metrics.build_cohort_key(self.metadata()), metrics.build_cohort_key(dict(self.metadata())))

    def test_normalized_kpi_excludes_only_explicit_current_turn_tokens(self) -> None:
        self.assertEqual(metrics.actual_hit_rate(900, 100), 0.9)
        self.assertEqual(
            metrics.normalized_cache_kpi(900, 100, unavoidable_current_turn_tokens=100),
            1.0,
        )
        self.assertIsNone(metrics.normalized_cache_kpi(0, 100, unavoidable_current_turn_tokens=100))

    def test_cohort_rolling_is_isolated_and_snapshot_is_detached(self) -> None:
        first = metrics.record_cohort_hit(self.metadata(tool_hash="a"), 90, 10)
        second = metrics.record_cohort_hit(self.metadata(tool_hash="a"), 80, 20)
        isolated = metrics.record_cohort_hit(self.metadata(tool_hash="b"), 0, 100)
        self.assertEqual(first.roll_n, 1)
        self.assertEqual(second.roll_n, 2)
        self.assertEqual(isolated.roll_n, 1)
        snapshot = metrics.get_cohort_stats_snapshot()
        self.assertEqual(snapshot[isolated.cohort_key]["roll_n"], 1)
        snapshot[isolated.cohort_key]["metadata"]["persona"] = "mutated"
        self.assertEqual(metrics.get_cohort_stats_snapshot()[isolated.cohort_key]["metadata"]["persona"], "catty")

    def test_hit_target_legacy_fields_stay_intact_with_tail_diagnostics(self) -> None:
        stats = metrics.RollingStats(0.9, 0.95, 2, "OK", False)
        kwargs = dict(
            provider="deepseek", model="deepseek-v4-flash", stats=stats,
            hit_tok=90, miss_tok=10, create_tok=0, msgs=4, hist=2, warm=1, scope="private:42",
        )
        legacy = metrics.format_hit_target_line(**kwargs)
        self.assertEqual(
            legacy,
            "HIT_TARGET model=deepseek-v4-flash this=90.0% rolling2=95.0% "
            "target=95-98% status=OK hit_tok=90 miss_tok=10 create_tok=0 "
            "prompt_tok=100 provider=deepseek msgs=4 hist=2 warm=1 scope=private:42",
        )
        diagnosed = metrics.format_hit_target_line(
            **kwargs,
            diagnostics={
                "cohort": "abcdef0123456789", "route": "openai_compat", "scope_type": "private",
                "persona": "catty", "anchor": {"anchor_before": 0, "anchor_after": 4, "anchor_changed": True, "reset_reason": "window_exceeded_trigger"},
                "wire_tool_hash": "1234567890abcdef", "tool_count": 1,
                "prefix_sys_hash": "fedcba9876543210", "prefix_first_hash": "0011223344556677",
                "message_end_role": "user", "stream": True, "actual_hit_rate": 0.9, "normalized_kpi": 1.0,
                "cohort_metadata": self.metadata(
                    prompt_variant="session", trim_epoch=2, request_kind="chat_followup",
                ),
                "anchor_observed": True,
                "request_class": "chat",
                "hot99_status": "PASS", "hot99_raw_rate": 0.99, "hot99_eligible_count": 10,
            },
        )
        self.assertTrue(diagnosed.startswith(legacy + " cohort=abcdef012345"))
        self.assertIn("anchor=0>4", diagnosed)
        self.assertIn("tool_hash=1234567890ab", diagnosed)
        self.assertIn("prefix=fedcba987654/001122334455", diagnosed)
        self.assertIn("prompt_variant=session", diagnosed)
        self.assertIn("trim_epoch=2", diagnosed)
        self.assertIn("request_kind=chat_followup", diagnosed)
        self.assertIn("anchor_observed=True", diagnosed)
        self.assertIn("request_class=chat", diagnosed)
        self.assertIn("hot99_status=PASS", diagnosed)
        self.assertIn("hot99_rate=99.0%", diagnosed)
        self.assertIn("hot99_count=10", diagnosed)

    def test_anchor_observation_preserves_under_budget_trim_output(self) -> None:
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"turn-{index}"}
            for index in range(6)
        ]
        trimmed = compressor.monotonic_history_trim(
            history,
            scope_id="private:cohort-observation",
            target_tokens=10_000,
            keep_recent=2,
        )
        observation = compressor.get_anchor_observation()
        self.assertEqual(trimmed, history)
        self.assertEqual(observation["scope_id"], "private:cohort-observation")
        self.assertEqual(observation["anchor_before"], 0)
        self.assertEqual(observation["anchor_after"], 0)
        self.assertFalse(observation["anchor_changed"])
        self.assertEqual(observation["history_len"], len(history))
        compressor.clear_anchor_observation()
        self.assertIsNone(compressor.get_anchor_observation())

    def test_wire_tool_hash_is_based_on_normalized_schemas(self) -> None:
        openai_shape = [{
            "type": "function",
            "function": {
                "name": "lookup", "description": "find something",
                "parameters": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"b": {"type": "string"}, "a": {"type": "integer"}},
                },
            },
        }]
        anthropic_shape = [{
            "name": "lookup", "description": "find something",
            "input_schema": {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
            },
        }]
        self.assertEqual(client._wire_tool_hash(openai_shape), client._wire_tool_hash(anthropic_shape))

    def test_new_responses_share_automatic_key_without_changing_explicit_keys(self) -> None:
        endpoint = "https://api.openai.com/v1/responses"
        model = "gpt-6.1-terra"
        for scope in ("group:TARGET_A", "group:TARGET_B", "private:TARGET", ""):
            with self.subTest(scope=scope):
                payload = {}
                client._apply_openai_prompt_cache_key(payload, base_url=endpoint, model=model, scope=scope)
                self.assertEqual(payload, {"prompt_cache_key": "catty:shared"})
        payload = {"prompt_cache_key": "explicit-customer-key"}
        client._apply_openai_prompt_cache_key(payload, base_url=endpoint, model=model, scope="group:TARGET")
        self.assertEqual(payload["prompt_cache_key"], "explicit-customer-key")

    def test_legacy_and_blocked_cache_keys_keep_existing_rules(self) -> None:
        for endpoint, model in (
            ("https://api.openai.com/v1/responses", "gpt-5.5"),
            ("https://api.openai.com/v1/chat/completions", "gpt-6.1-terra"),
        ):
            with self.subTest(endpoint=endpoint, model=model):
                payload = {}
                client._apply_openai_prompt_cache_key(payload, base_url=endpoint, model=model, scope="group:TARGET")
                self.assertEqual(payload["prompt_cache_key"], "catty:group:TARGET")
                with patch.object(client, "_PROMPT_CACHE_KEY_BLOCKED", {f"{endpoint}|{model}"}):
                    blocked = {}
                    client._apply_openai_prompt_cache_key(blocked, base_url=endpoint, model=model, scope="group:TARGET")
                    self.assertEqual(blocked, {})

    def test_deepseek_payload_still_has_no_user_or_prompt_cache_key(self) -> None:
        captured: list[dict] = []

        class Response:
            status_code = 200
            text = ""

            def json(self) -> dict:
                return {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 2},
                }

        class AsyncClient:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc_info: object) -> bool:
                return False

            async def post(self, _url: str, *, headers: dict, json: dict) -> Response:
                del headers
                captured.append(dict(json))
                return Response()

        runtime_config = types.SimpleNamespace(
            catty_openai_prompt_cache_key_enabled=True,
            catty_cache_diag_enabled=False,
        )
        with (
            patch.object(client.httpx, "AsyncClient", AsyncClient),
            patch.object(client, "_get_runtime_config", return_value=runtime_config),
            patch.object(client, "get_current_scope_key", return_value="private:42"),
        ):
            asyncio.run(
                client._post_chat_completion_raw(
                    base_url="https://api.deepseek.com/v1",
                    api_key="test-key",
                    model="deepseek-v4-flash",
                    messages=[{"role": "user", "content": "ping"}],
                    timeout=1,
                    proxy="",
                    temperature=0.1,
                    max_tokens=16,
                    extra_headers={},
                    extra_body={},
                    enable_cache=False,
                )
            )
        self.assertEqual(len(captured), 1)
        self.assertNotIn("user", captured[0])
        self.assertNotIn("prompt_cache_key", captured[0])

    def test_request_dump_defaults_off(self) -> None:
        with patch.object(client, "_get_runtime_config", return_value=None):
            self.assertFalse(client._cache_request_dump_enabled())


if __name__ == "__main__":
    unittest.main()
