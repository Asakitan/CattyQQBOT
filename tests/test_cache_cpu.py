"""Offline cache/CPU regressions. Never imports or starts the bot plugin."""
from __future__ import annotations

import importlib.util
import json
import logging
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_module(name: str, relative_path: str):
    path = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    logger = logging.getLogger(name)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    stubs = {}
    for dependency in ("nonebot", "loguru"):
        stub = types.ModuleType(dependency)
        stub.logger = logger
        stubs[dependency] = stub
    # Restore only our stubs, without removing newly imported optional packages.
    # Dataclasses resolve the private module during class creation.
    missing = object()
    previous = {key: sys.modules.get(key, missing) for key in stubs}
    sys.modules[name] = module
    sys.modules.update(stubs)
    try:
        spec.loader.exec_module(module)
    finally:
        for key, value in previous.items():
            if value is missing:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
    return module


session = _load_module("_catty_session_cache_tests", "session_cache.py")
semantic = _load_module("_catty_semantic_route_tests", "cpu_engine/semantic_route.py")
np = semantic.np


class SessionCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def write_session(self, filename: str, key: str, access: float, **extra):
        payload = {
            "key": key,
            "messages": [{"role": "user", "content": filename}],
            "last_access": access,
            "last_turn": access - 1,
            "history_tokens_estimate": 321,
            "trim_epoch": 2,
            "trim_count": 3,
            "context_updated_at": access - 2,
        }
        payload.update(extra)
        path = self.directory / filename
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_valid_token_metadata_does_not_reserialize_history(self) -> None:
        cache = session.SessionCache(self.directory)
        for stored, expected in [(0, 0), (123, 123), ("123", 123), (12.5, 12), (-5, 0)]:
            with self.subTest(stored=stored):
                path = self.write_session("one.json", "one", 10, history_tokens_estimate=stored)
                with patch.object(session, "_estimate_history_tokens", side_effect=AssertionError("unneeded estimate")):
                    entry = cache._read_session_file(path)
                self.assertEqual(entry[4]["history_tokens_estimate"], expected)

    def test_missing_or_invalid_token_metadata_uses_legacy_estimate(self) -> None:
        cache = session.SessionCache(self.directory)
        for value in [None, "invalid", {}, [], float("nan"), float("inf")]:
            with self.subTest(value=value):
                path = self.write_session("one.json", "one", 10, history_tokens_estimate=value)
                with patch.object(session, "_estimate_history_tokens", return_value=456) as estimate:
                    entry = cache._read_session_file(path)
                self.assertEqual(entry[4]["history_tokens_estimate"], 456)
                estimate.assert_called_once_with(entry[3])
        path = self.write_session("one.json", "one", 10)
        payload = json.loads(path.read_text())
        payload.pop("history_tokens_estimate")
        path.write_text(json.dumps(payload))
        entry = cache._read_session_file(path)
        self.assertEqual(entry[4]["history_tokens_estimate"], session._estimate_history_tokens(entry[3]))

    def test_invalid_optional_metadata_and_message_cleaning(self) -> None:
        path = self.write_session(
            "one.json", "one", float("nan"), last_turn=-10,
            context_updated_at="bad", trim_count=-5, trim_epoch="bad",
            messages=[None, {"role": "user"}, {"role": 3, "content": "ok"}],
        )
        entry = session.SessionCache(self.directory)._read_session_file(path)
        self.assertEqual(entry[3], [{"role": "3", "content": "ok"}])
        self.assertEqual(entry[0], path.stat().st_mtime)
        self.assertEqual(entry[1], entry[0])
        self.assertEqual(entry[4]["context_updated_at"], entry[1])
        self.assertEqual(entry[4]["trim_count"], 0)
        self.assertEqual(entry[4]["trim_epoch"], 0)

    def test_startup_reads_all_indexes_but_only_reloads_hot_bodies(self) -> None:
        for i in range(8):
            self.write_session(f"{i}.json", f"group:{i}", i + 10)
        cache = session.SessionCache(self.directory, max_sessions=2)
        with patch.object(cache, "_read_session_file", wraps=cache._read_session_file) as read:
            self.assertEqual(cache.load_from_disk(), 2)
        self.assertEqual(read.call_count, 10)
        self.assertEqual(list(cache._sessions), ["group:6", "group:7"])
        self.assertEqual(len(cache._metadata), 8)
        self.assertEqual(cache.get_metadata("group:0")["history_tokens_estimate"], 321)
        self.assertNotIn("group:0", cache._sessions)
        self.assertEqual(cache.get("group:0"), [{"role": "user", "content": "0.json"}])
        self.assertEqual(list(cache._sessions), ["group:7", "group:0"])
        self.assertEqual(cache._dirty, set())
        self.assertEqual(cache.last_turn_at("group:0"), 9)
        with patch.object(cache, "_read_session_file", side_effect=AssertionError("already loaded")):
            self.assertEqual(cache.load_from_disk(), 2)

    def test_startup_does_not_retain_every_cold_message_list(self) -> None:
        class TrackedMessages(list):
            live = 0
            peak = 0

            def __init__(self, values):
                super().__init__(values)
                type(self).live += 1
                type(self).peak = max(type(self).peak, type(self).live)

            def __del__(self):
                type(self).live -= 1

        for i in range(60):
            self.write_session(f"{i}.json", str(i), i + 10)
        cache = session.SessionCache(self.directory, max_sessions=3)
        read = cache._read_session_file

        def tracked(path):
            entry = read(path)
            return (*entry[:3], TrackedMessages(entry[3]), entry[4])

        with patch.object(cache, "_read_session_file", side_effect=tracked):
            cache.load_from_disk()
        self.assertLessEqual(TrackedMessages.peak, cache.max_sessions + 2)
        self.assertEqual(TrackedMessages.live, cache.max_sessions)

    def test_duplicate_key_uses_access_then_filename_tiebreak(self) -> None:
        self.write_session("a.json", "duplicate", 20)
        self.write_session("z.json", "duplicate", 20)
        self.write_session("zz.json", "duplicate", 19)
        self.write_session("other.json", "other", 30)
        cache = session.SessionCache(self.directory, max_sessions=1)
        cache.load_from_disk()
        self.assertEqual(cache._paths["duplicate"].name, "z.json")
        self.assertEqual(cache.get("duplicate"), [{"role": "user", "content": "z.json"}])
        self.assertEqual(len(cache._metadata), 2)

    def test_equal_access_uses_key_order_for_hot_selection(self) -> None:
        for key in ("c", "a", "b"):
            self.write_session(f"{key}.json", key, 10)
        cache = session.SessionCache(self.directory, max_sessions=2)
        cache.load_from_disk()
        self.assertEqual(list(cache._sessions), ["b", "c"])

    def test_existing_unflushed_session_is_preserved_at_startup(self) -> None:
        self.write_session("existing.json", "existing", 1000)
        self.write_session("cold.json", "cold", 1001)
        cache = session.SessionCache(self.directory, max_sessions=1)
        live = [{"role": "user", "content": "not on disk yet"}]
        cache.set("existing", live)
        metadata = cache.get_metadata("existing")
        with patch.object(cache, "_read_session_file", wraps=cache._read_session_file) as read:
            cache.load_from_disk()
        self.assertEqual(read.call_count, 2)
        self.assertEqual(cache.get("existing"), live)
        self.assertEqual(cache.get_metadata("existing"), metadata)
        self.assertEqual(cache._dirty, {"existing"})
        self.assertTrue(cache.has("cold"))

    def test_file_invalidated_between_startup_passes_is_forgotten(self) -> None:
        for replacement in [None, {"key": "different", "messages": []}]:
            with self.subTest(replacement=replacement):
                path = self.write_session("one.json", "one", 10)
                cache = session.SessionCache(self.directory)
                read = cache._read_session_file
                calls = 0

                def changing_read(file):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if replacement is None:
                            path.unlink()
                        else:
                            path.write_text(json.dumps(replacement))
                    return read(file)

                with patch.object(cache, "_read_session_file", side_effect=changing_read):
                    self.assertEqual(cache.load_from_disk(), 0)
                self.assertFalse(cache.has("one"))
                self.assertEqual(cache.get("one"), [])

    def test_missing_or_disabled_directory_is_safe(self) -> None:
        cache = session.SessionCache(self.directory / "missing")
        self.assertEqual(cache.load_from_disk(), 0)
        disabled = session.SessionCache(self.directory, persistence_enabled=False)
        with patch.object(disabled, "_read_session_file", side_effect=AssertionError("disabled")):
            self.assertEqual(disabled.load_from_disk(), 0)

    def test_corrupt_files_are_skipped(self) -> None:
        for name, contents in [("bad.json", "not json"), ("list.json", "[]"), ("shape.json", '{"key": 3, "messages": []}')]:
            (self.directory / name).write_text(contents)
        self.write_session("valid.json", "valid", 10)
        cache = session.SessionCache(self.directory)
        self.assertEqual(cache.load_from_disk(), 1)
        self.assertEqual(list(cache._metadata), ["valid"])

    def test_failed_dirty_eviction_retains_data_then_flush_reclaims_capacity(self) -> None:
        cache = session.SessionCache(self.directory, max_sessions=2)
        expected = {str(i): [{"role": "user", "content": f"message {i}"}] for i in range(6)}
        with patch.object(cache, "_write_one", return_value=False):
            for key, messages in expected.items():
                cache.set(key, messages)
            self.assertEqual(cache.total_sessions(), 6)
            self.assertEqual(cache.flush_sync(), 0)
            self.assertEqual(cache._dirty, set(expected))
            self.assertEqual(cache.total_sessions(), 6)
        self.assertEqual(cache.flush_sync(), 6)
        self.assertEqual(cache.total_sessions(), 2)
        self.assertEqual(cache._dirty, set())
        self.assertEqual(list(cache._sessions), ["4", "5"])
        for key, messages in expected.items():
            self.assertEqual(cache.get(key), messages)
            self.assertLessEqual(cache.total_sessions(), 2)
        reloaded = session.SessionCache(self.directory, max_sessions=2)
        reloaded.load_from_disk()
        for key, messages in expected.items():
            self.assertEqual(reloaded.get(key), messages)

    def test_partial_flush_does_not_retry_or_clear_failed_dirty_session(self) -> None:
        cache = session.SessionCache(self.directory, max_sessions=1)
        with patch.object(cache, "_write_one", return_value=False):
            for key in ("first", "second", "third"):
                cache.set(key, [{"role": "user", "content": key}])
        original = cache._write_one

        def fail_oldest(key):
            return False if key == "first" else original(key)

        with patch.object(cache, "_write_one", side_effect=fail_oldest) as write:
            self.assertEqual(cache.flush_sync(), 2)
        self.assertEqual(write.call_count, 3)
        self.assertEqual(cache._dirty, {"first"})
        self.assertEqual(cache.total_sessions(), 3)
        self.assertEqual(cache.flush_sync(), 1)
        self.assertEqual(cache._dirty, set())
        self.assertEqual(cache.total_sessions(), 1)


@unittest.skipUnless(semantic._HAS_NUMPY, "optional numpy dependency is unavailable")
class SemanticRouterTests(unittest.TestCase):
    def make_router(self, weights=(1.0, 1.0), vectors=None, *, cache_dir=None, embed=None):
        routes = [
            semantic.SemanticRoute(str(i), "ordinary", [f"{i}:a", f"{i}:b"], [f"response {i}"], weight)
            for i, weight in enumerate(weights)
        ]
        if vectors is None:
            vectors = np.eye(len(routes) * 2, dtype=np.float32)
        router = semantic.SemanticRouter(
            routes, embed or (lambda texts: vectors), cache_dir=cache_dir, routes_mtime_sig="test"
        )
        self.assertTrue(router.prepare())
        return router

    @staticmethod
    def reference(router, scores):
        best = {}
        for i, score in enumerate(scores):
            ri = router._flat_route_idx[i]
            weighted = float(score) * router._routes[ri].weight
            previous = best.get(ri)
            if previous is None or weighted > previous[0]:
                best[ri] = (weighted, i)
        if not best:
            return None
        ri, (score, i) = max(best.items(), key=lambda item: item[1][0])
        return ri, score, i

    def assert_same_winner(self, router, scores):
        expected = self.reference(router, scores)
        actual = router._select_winner(scores)
        if expected is None:
            self.assertIsNone(actual)
            return
        self.assertEqual((actual[0], actual[2]), (expected[0], expected[2]))
        if math.isnan(expected[1]):
            self.assertTrue(math.isnan(actual[1]))
        else:
            self.assertEqual(actual[1], expected[1])

    def test_exact_equivalence_for_random_scores_and_weights(self) -> None:
        rng = np.random.default_rng(20260930)
        for _ in range(50):
            weights = rng.uniform(-2, 2, 7).tolist()
            router = self.make_router(weights)
            self.assert_same_winner(router, rng.uniform(-1, 1, 14).astype(np.float32))

    def test_ties_zero_and_negative_weights_preserve_first_winner(self) -> None:
        for weights in [(1, 1), (0, 0), (-1, -1), (1, 2), (1e200, 1e-200)]:
            router = self.make_router(weights)
            for scores in [[0, 0, 0, 0], [1, 1, 1, 1], [-1, -1, -1, -1], [0.8, 0.4, 0.4, 0.1]]:
                self.assert_same_winner(router, np.asarray(scores, dtype=np.float32))
        winner = self.make_router()._select_winner(np.asarray([0.8, 0.8, 0.8, 0.8], dtype=np.float32))
        self.assertEqual((winner[0], winner[2]), (0, 0))

    def test_nonfinite_scores_and_weights_keep_python_comparison_semantics(self) -> None:
        for weights in [(1, 1), (0, 1), (float("nan"), 1), (float("inf"), -1)]:
            router = self.make_router(weights)
            for scores in [[float("nan"), 0.9, 0.8, 0.7], [0.2, float("nan"), 0.8, 0.7], [float("inf"), 0, 1, float("-inf")], [0, 0, 0, 0]]:
                self.assert_same_winner(router, np.asarray(scores, dtype=np.float32))

    def test_query_thresholds_and_response_fields_are_unchanged(self) -> None:
        router = self.make_router(weights=(1, 2))
        query = np.asarray([0.1, 0.2, 0.4, 0.3], dtype=np.float32)
        result = router.match("query", embed_query_fn=lambda _: query, candidate_threshold=0.7, direct_threshold=0.9)
        self.assertEqual((result.route_name, result.response, result.intent, result.matched_utterance), ("1", "response 1", "ordinary", "1:a"))
        self.assertEqual(result.confidence, float(query[2]) * 2)
        self.assertFalse(result.is_direct)
        self.assertTrue(router.match("query", embed_query_fn=lambda _: query, candidate_threshold=0.7, direct_threshold=0.8).is_direct)
        self.assertIsNone(router.match("query", embed_query_fn=lambda _: query, candidate_threshold=0.9))
        self.assertEqual(router.match("query", embed_query_fn=lambda _: query * 2).confidence, 1.0)

    def test_unprepared_missing_or_wrong_dimension_queries_are_safe(self) -> None:
        empty = semantic.SemanticRouter([], lambda _: None)
        self.assertIsNone(empty.match("query", embed_query_fn=lambda _: np.ones(2)))
        self.assertIsNone(empty._select_winner(np.asarray([])))
        router = self.make_router()
        for query in [None, np.ones(2), np.ones((1, 4))]:
            self.assertIsNone(router.match("query", embed_query_fn=lambda _: query))

    def test_disk_cache_hit_still_initializes_weights(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.make_router(weights=(1, 2), cache_dir=directory)
            with patch.object(semantic, "logger"):
                router = self.make_router(
                    weights=(1, 2), cache_dir=directory,
                    embed=lambda _: self.fail("cache hit must not embed"),
                )
            self.assertEqual(router._flat_route_weights.dtype, np.float64)
            np.testing.assert_array_equal(router._flat_route_weights, [1, 1, 2, 2])
            self.assert_same_winner(router, np.asarray([0.1, 0.9, 0.5, 0.2], dtype=np.float32))

    def test_empty_utterances_do_not_shift_route_weights_or_indices(self) -> None:
        routes = [
            semantic.SemanticRoute("empty", "ordinary", [" "], ["empty"], 100),
            semantic.SemanticRoute("valid", "ordinary", ["hello", ""], ["hi"], 2),
        ]
        router = semantic.SemanticRouter(routes, lambda _: np.asarray([[1, 0]], dtype=np.float32))
        self.assertTrue(router.prepare())
        self.assertEqual(router._flat_route_idx, [1])
        self.assertEqual(router._select_winner(np.asarray([0.5], dtype=np.float32)), (1, 1.0, 0))


if __name__ == "__main__":
    unittest.main()
