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
corpus = _load_module("_catty_txtai_cache_tests", "cpu_engine/corpus_txtai.py")
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
    def make_router(self, weights=(1.0, 1.0), vectors=None, *, cache_dir=None, embed=None, cache_model_id="fake-v1"):
        routes = [
            semantic.SemanticRoute(str(i), "ordinary", [f"{i}:a", f"{i}:b"], [f"response {i}"], weight)
            for i, weight in enumerate(weights)
        ]
        if vectors is None:
            vectors = np.eye(len(routes) * 2, dtype=np.float32)
        router = semantic.SemanticRouter(
            routes, embed or (lambda texts: vectors), cache_dir=cache_dir,
            routes_mtime_sig="test", cache_model_id=cache_model_id,
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

    def test_cache_changes_with_model_content_and_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def embed(vector):
                def run(texts):
                    calls.append(list(texts))
                    return np.tile(np.asarray(vector, dtype=np.float32), (len(texts), 1))
                return run

            def build(text, model, vector):
                routes = [semantic.SemanticRoute("r", "ordinary", [text], [text])]
                result = semantic.SemanticRouter(
                    routes, embed(vector), cache_dir=directory,
                    routes_mtime_sig="same", cache_model_id=model,
                )
                self.assertTrue(result.prepare())
                return result

            build("one", "model-a", [1, 0])
            same = build("one", "model-a", [0, 1])
            self.assertEqual(len(calls), 1)
            self.assertEqual(same._flat_embeddings.tolist(), [[1, 0]])
            changed = build("one", "model-b", [0, 1])
            self.assertEqual(len(calls), 2)
            self.assertEqual(changed._flat_embeddings.tolist(), [[0, 1]])
            self.assertIsNotNone(changed.match("q", embed_query_fn=lambda _: np.array([0, 1], dtype=np.float32)))
            build("two", "model-b", [1, 0])
            self.assertEqual(len(calls), 3)
            np.save(str(Path(directory) / "semantic_l2_embeddings.npy"), np.ones((1, 3), dtype=np.float32))
            rebuilt = build("two", "model-b", [1, 0])
            self.assertEqual(len(calls), 4)
            self.assertEqual(rebuilt._flat_embeddings.shape, (1, 2))
            self.assertIsNone(rebuilt.match("q", embed_query_fn=lambda _: np.ones(3)))
            resized = build("two", "model-c", [1, 0, 0])
            self.assertEqual(len(calls), 5)
            self.assertEqual(resized._flat_embeddings.shape, (1, 3))
            meta = Path(directory) / "semantic_l2_metadata.json"
            meta.write_text(json.dumps({"n_utts": 1, "mtime_sig": "same", "dim": 3}), encoding="utf-8")
            build("two", "model-c", [1, 0, 0])
            self.assertEqual(len(calls), 6)

    def test_unknown_model_id_recomputes_without_disabling_or_overwriting_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.make_router(cache_dir=directory)
            path = Path(directory) / "semantic_l2_metadata.json"
            before = path.read_bytes()
            calls = []

            def embed(texts):
                calls.append(list(texts))
                return np.eye(len(texts), dtype=np.float32)

            routes = [semantic.SemanticRoute("r", "ordinary", ["q"], ["a"])]
            for _ in range(2):
                router = semantic.SemanticRouter(routes, embed, cache_dir=directory, routes_mtime_sig="test")
                self.assertTrue(router.prepare())
                self.assertIsNotNone(router.match("q", embed_query_fn=lambda _: np.ones(1)))
            self.assertEqual(len(calls), 2)
            self.assertEqual(path.read_bytes(), before)

    def test_reordered_utterances_rebuild_even_with_same_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def embed(texts):
                calls.append(list(texts))
                return np.array([[1, 0] if text == "a" else [0, 1] for text in texts], dtype=np.float32)

            for words in (["a", "b"], ["b", "a"]):
                routes = [semantic.SemanticRoute("r", "ordinary", list(words), ["answer"])]
                router = semantic.SemanticRouter(
                    routes, embed, cache_dir=directory, routes_mtime_sig="same", cache_model_id="stable",
                )
                self.assertTrue(router.prepare())
                result = router.match("q", embed_query_fn=lambda _: np.array([1, 0], dtype=np.float32))
                self.assertEqual(result.matched_utterance, "a")
            self.assertEqual(calls, [["a", "b"], ["b", "a"]])

    def test_actual_router_legacy_identity_and_layer_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            routes_dir = root / "routes"
            routes_dir.mkdir()
            corpus_path = root / "corpus.jsonl"
            corpus_path.write_text(json.dumps({"q": "target", "a": "l3-answer"}), encoding="utf-8")
            source = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai"
            name = "_catty_cpu_route_test"
            parent = types.ModuleType(name)
            parent.__path__ = [str(source)]
            package = types.ModuleType(name + ".cpu_engine")
            package.__path__ = [str(source / "cpu_engine")]
            nlu = types.ModuleType(name + ".nlu")
            nlu.__path__ = []
            encoder = types.ModuleType(name + ".nlu.text2vec_engine")
            beg = types.ModuleType(name + ".cpu_engine.beg_template")
            beg.load_pool_from_dir = lambda _: None
            calls = []
            query = [np.array([0.9, 0], dtype=np.float32)]

            def embed_batch(texts):
                calls.append(list(texts))
                return np.tile(np.array([1, 0], dtype=np.float32), (len(texts), 1))

            encoder.embed_sync_batch = embed_batch
            encoder.embed_sync = lambda _: query[0]
            modules = {
                name: parent,
                name + ".cpu_engine": package,
                name + ".nlu": nlu,
                name + ".nlu.text2vec_engine": encoder,
                name + ".cpu_engine.beg_template": beg,
            }
            with patch.dict(sys.modules, modules):
                module = _load_module(name + ".cpu_engine.router", "cpu_engine/router.py")
                corpus_module = sys.modules[name + ".cpu_engine.corpus_txtai"]
                routes = [semantic.SemanticRoute("semantic", "ordinary", ["sample"], ["l2-answer"])]
                config = types.SimpleNamespace(
                    catty_cpu_engine_enabled=True,
                    catty_cpu_engine_routes_dir=str(routes_dir),
                    catty_cpu_engine_corpus_path=str(corpus_path),
                    catty_cpu_engine_txtai_index_path=str(root / "index"),
                    catty_text2vec_model_name="configured-but-not-a-runtime-id",
                )
                with patch.object(corpus_module, "_HAS_TXTAI", True), \
                     patch.object(corpus_module, "Embeddings", FakeTxtai), \
                     patch.object(module, "load_kw_routes", return_value=[]), \
                     patch.object(module, "load_sem_routes", return_value=routes), \
                     patch.object(module, "normalize", side_effect=lambda text: types.SimpleNamespace(cleaned=text, has_image=False, has_voice=False)):
                    engine = module.CPUEngineRouter(config)
                    engine.prepare()
                    self.assertTrue(engine.ready)
                    self.assertEqual(engine._semantic_router._cache_model_id, "")
                    self.assertEqual(engine._txtai_corpus._cache_model_id, "")
                    result = engine.route_sync("target", "private:1")
                    self.assertEqual((result.layer, result.reply, result.is_low_confidence), ("L2", "l2-answer", False))
                    query[0] = np.array([0.75, 0], dtype=np.float32)
                    result = engine.route_sync("target", "private:1")
                    self.assertEqual((result.layer, result.reply, result.is_low_confidence), ("L3", "l3-answer", False))
                    result = engine.route_sync("other", "private:1")
                    self.assertEqual((result.layer, result.is_low_confidence), ("L2", True))
                    query[0] = np.array([0.72, 0], dtype=np.float32)
                    self.assertIsNone(engine.route_sync("other", "group:1"))
                    with patch.object(engine, "_compute_routes_mtime_signature", return_value="changed"):
                        self.assertTrue(engine.reload_routes_if_changed())
                    self.assertEqual(len(calls), 2)
                    hit = types.SimpleNamespace(route_name="keyword", response="l1-answer", intent="ordinary", matched_keyword="target")
                    engine._keyword_matcher = types.SimpleNamespace(match=lambda text, scope: hit)
                    result = engine.route_sync("target", "private:1")
                    self.assertEqual((result.layer, result.reply), ("L1", "l1-answer"))


class FakeTxtai:
    builds = 0
    loads = 0

    def __init__(self, config=None):
        self.config = config
        self.rows = []

    def index(self, rows):
        type(self).builds += 1
        self.rows = list(rows)

    def save(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / "config").write_text(json.dumps(self.rows), encoding="utf-8")

    def load(self, path):
        type(self).loads += 1
        self.rows = json.loads((Path(path) / "config").read_text(encoding="utf-8"))

    def count(self):
        return len(self.rows)

    def search(self, text, limit=5):
        return [(qid, 1.0) for qid, q, _ in self.rows if q == text][:limit]


class TxtaiCacheIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.corpus_path = self.root / "corpus.jsonl"
        self.live_path = self.root / "live.jsonl"
        self.index_path = self.root / "index"
        FakeTxtai.builds = FakeTxtai.loads = 0
        p1 = patch.object(corpus, "_HAS_TXTAI", True)
        p2 = patch.object(corpus, "Embeddings", FakeTxtai)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def write(self, path, rows):
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    def make(self, model="model-a", identity="model-a@sha256:aaa"):
        result = corpus.TxtaiCorpus(
            self.corpus_path, self.index_path, model_name=model,
            live_corpus_path=self.live_path, cache_model_id=identity,
        )
        self.assertTrue(result.prepare())
        return result

    def test_order_content_model_and_live_invalidation_then_reuse(self) -> None:
        a = {"q": "alpha", "a": "answer-a"}
        b = {"q": "beta", "a": "answer-b"}
        c = {"q": "gamma", "a": "answer-c"}
        self.write(self.corpus_path, [a])
        self.write(self.live_path, [b])
        first = self.make()
        self.assertEqual((first.match("beta").qid, first.match("beta").a), (1, "answer-b"))
        self.make()
        self.assertEqual((FakeTxtai.builds, FakeTxtai.loads), (1, 1))
        self.write(self.live_path, [c])
        updated = self.make()
        self.assertEqual(FakeTxtai.builds, 2)
        self.assertIsNone(updated.match("beta"))
        self.assertEqual((updated.match("gamma").qid, updated.match("gamma").a), (1, "answer-c"))
        self.write(self.corpus_path, [c])
        self.write(self.live_path, [a])
        reordered = self.make()
        self.assertEqual(FakeTxtai.builds, 3)
        self.assertEqual((reordered.match("gamma").qid, reordered.match("gamma").a), (0, "answer-c"))
        self.make()
        self.assertEqual((FakeTxtai.builds, FakeTxtai.loads), (3, 2))
        changed_model = self.make(model="model-b", identity="model-b@sha256:bbb")
        self.assertEqual(FakeTxtai.builds, 4)
        self.assertEqual(changed_model.match("gamma").a, "answer-c")
        self.make(model="model-c", identity="model-b@sha256:bbb")
        self.assertEqual(FakeTxtai.builds, 5)

    def test_answer_and_legacy_metadata_changes_invalidate(self) -> None:
        self.write(self.corpus_path, [{"q": "alpha", "a": "first"}])
        self.make()
        self.write(self.corpus_path, [{"q": "alpha", "a": "second"}])
        self.assertEqual(self.make().match("alpha").a, "second")
        self.assertEqual(FakeTxtai.builds, 2)
        (self.index_path / "catty_index_identity.json").write_text('{"count": 1}', encoding="utf-8")
        self.make()
        self.assertEqual(FakeTxtai.builds, 3)

    def test_legacy_id_absent_rebuilds_and_keeps_routing(self) -> None:
        self.write(self.corpus_path, [{"q": "alpha", "a": "answer-a"}])
        self.make()
        before = (self.index_path / "catty_index_identity.json").read_bytes()
        for _ in range(2):
            current = self.make(identity=None)
            self.assertEqual(current.match("alpha").a, "answer-a")
        self.assertEqual(FakeTxtai.builds, 3)
        self.assertEqual((self.index_path / "catty_index_identity.json").read_bytes(), before)

    def test_reload_updates_index_identity(self) -> None:
        self.write(self.corpus_path, [{"q": "alpha", "a": "answer-a"}])
        current = self.make()
        self.write(self.corpus_path, [{"q": "beta", "a": "answer-b"}])
        self.assertTrue(current.reload())
        self.assertEqual(current.match("beta").a, "answer-b")
        resumed = self.make()
        self.assertEqual(FakeTxtai.builds, 2)
        self.assertEqual(resumed.match("beta").a, "answer-b")

    def test_failed_cache_save_keeps_memory_index_usable_and_invalidates_marker(self) -> None:
        self.write(self.corpus_path, [{"q": "alpha", "a": "answer-a"}])
        self.make()
        self.write(self.corpus_path, [{"q": "beta", "a": "answer-b"}])
        with patch.object(FakeTxtai, "save", side_effect=OSError("simulated cache failure")):
            current = self.make()
        self.assertTrue(current.ready)
        self.assertEqual(current.match("beta").a, "answer-b")
        self.assertFalse((self.index_path / "catty_index_identity.json").exists())
        self.make()
        self.assertEqual(FakeTxtai.builds, 3)


if __name__ == "__main__":
    unittest.main()
