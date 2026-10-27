import ast
import asyncio
import copy
from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_persona_memory_isolation_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)


def _load_module(name: str):
    path = _ROOT / "src" / "catty_qq_ai" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_scope_lorebook = _load_module("scope_lorebook")
_catty_rag = _load_module("catty_rag")
_context_buckets = _load_module("context_buckets")
ScopeLoreEntry = _scope_lorebook.ScopeLoreEntry
ScopeLorebookStore = _scope_lorebook.ScopeLorebookStore
CattyRAGStore = _catty_rag.CattyRAGStore
build_rag_recall_prompt = _catty_rag.build_rag_recall_prompt
TimeBucketContextStore = _context_buckets.TimeBucketContextStore


class _NoopLogger:
    def debug(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass


def _load_init_functions(*names: str, extra_globals: dict[str, object] | None = None) -> dict[str, object]:
    source_path = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [name for name in names if name not in functions]
    if missing:
        raise AssertionError(f"missing init helper(s): {missing}")
    function_nodes = []
    for name in names:
        node = copy.deepcopy(functions[name])
        node.decorator_list = []
        function_nodes.append(node)
    module = ast.Module(body=function_nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {
        "__name__": f"{_PACKAGE_NAME}.isolated_init_helpers",
        "__package__": _PACKAGE_NAME,
        "Config": object,
        "Any": object,
        "Path": Path,
        "json": json,
    }
    if extra_globals:
        namespace.update(extra_globals)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace


class _FakeSessionCache:
    def __init__(self, keys: list[str]) -> None:
        self._keys = set(keys)
        self.popped: list[str] = []

    def list_sessions(self):
        return [(key, 1, 0.0) for key in sorted(self._keys)]

    def pop(self, key: str):
        self.popped.append(key)
        if key in self._keys:
            self._keys.remove(key)
            return []
        return None


class _FakeBucketStore:
    def __init__(self, scopes: list[str]) -> None:
        self._scopes = set(scopes)
        self.reset_scope_calls: list[str] = []
        self.reset_matching_calls: list[str] = []

    def known_scopes(self, prefix: str = "") -> list[str]:
        return sorted(scope for scope in self._scopes if scope.startswith(prefix))

    def reset_scope(self, scope: str) -> bool:
        self.reset_scope_calls.append(scope)
        existed = scope in self._scopes
        self._scopes.discard(scope)
        return existed

    def reset_matching(self, prefix: str) -> int:
        self.reset_matching_calls.append(prefix)
        matches = [scope for scope in self._scopes if scope.startswith(prefix)]
        for scope in matches:
            self._scopes.remove(scope)
        return len(matches)


def _make_persona_isolation_runtime(
    memory_path: Path,
    *,
    session_keys: list[str],
    bucket_scopes: list[str],
):
    cache = _FakeSessionCache(session_keys)
    buckets = _FakeBucketStore(bucket_scopes)
    reminder_calls: list[str] = []
    reminder_module_name = f"{_PACKAGE_NAME}.catty_persona_reminder"
    previous_reminder_module = sys.modules.get(reminder_module_name)
    reminder_module = types.ModuleType(reminder_module_name)
    reminder_module.reset_scope = reminder_calls.append
    sys.modules[reminder_module_name] = reminder_module

    namespace = _load_init_functions(
        "_base_persona_scope",
        "_persona_baseline_scope",
        "_session_persona_baseline_path",
        "_invalidate_session_persona_baselines",
        "_load_session_persona_baselines",
        "_save_session_persona_baselines",
        "_record_session_persona_baselines",
        "_record_session_persona_baseline",
        "_clear_persona_runtime_state",
        "_ensure_session_persona_isolation",
        extra_globals={
            "config": types.SimpleNamespace(catty_memory_path=str(memory_path)),
            "logger": _NoopLogger(),
            "_session_persona_names": {},
            "_MAX_TRACKED_SESSION_PERSONAS": 2048,
            "_SESSION_PERSONA_BASELINES_FILENAME": "session_persona_baselines.json",
            "_session_persona_baseline_file": None,
            "_session_persona_baselines_loaded": False,
            "_session_persona_baseline_write_lock": threading.RLock(),
            "uuid": uuid,
            "_get_session_cache": lambda: cache,
            "_get_time_bucket_context_store": lambda: buckets,
        },
    )

    def restore() -> None:
        if previous_reminder_module is None:
            sys.modules.pop(reminder_module_name, None)
        else:
            sys.modules[reminder_module_name] = previous_reminder_module

    return namespace, cache, buckets, reminder_calls, restore


class _FakeCollection:
    def __init__(self, documents=None, metadatas=None, distances=None) -> None:
        self.documents = list(documents or [])
        self.metadatas = list(metadatas or [])
        self.distances = list(distances or [])
        self.upserts: list[dict[str, object]] = []
        self.query_calls: list[dict[str, object]] = []

    def upsert(self, *, ids, documents, metadatas) -> None:
        self.upserts.append({
            "ids": list(ids),
            "documents": list(documents),
            "metadatas": list(metadatas),
        })

    def query(self, **kwargs):
        self.query_calls.append(dict(kwargs))
        return {
            "documents": [self.documents],
            "metadatas": [self.metadatas],
            "distances": [self.distances],
        }


def _rag_store(collection: _FakeCollection):
    store = CattyRAGStore.__new__(CattyRAGStore)
    store._enabled = True
    store._get_collection = lambda _scope: collection
    return store


class ScopeLorePersonaIsolationTests(unittest.TestCase):
    def test_legacy_lore_payload_is_catty_and_hidden_from_fadianji(self) -> None:
        legacy_entry_payload = {
            "identifier": "scope_lore_legacy",
            "keys": ["旧梗"],
            "content": "旧 lore 只属于 Catty",
            "created_at": 1.0,
            "last_hit_at": 0.0,
            "hit_count": 0,
        }
        entry = ScopeLoreEntry.from_payload(legacy_entry_payload)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.persona, "catty")
        self.assertEqual(entry.to_payload()["persona"], "catty")

        with tempfile.TemporaryDirectory() as directory:
            lore_path = Path(directory) / "scope_lorebooks.json"
            lore_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "scopes": {"group:10001": [legacy_entry_payload]},
                        "meta": {"group:10001": {"last_summary_date": "2026-08-03"}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = ScopeLorebookStore(Path(directory) / "memory.json")

            self.assertEqual(
                [item.identifier for item in store.list_entries("group:10001", persona="catty")],
                ["scope_lore_legacy"],
            )
            self.assertEqual(store.list_entries("group:10001", persona="fadianji"), [])
            self.assertEqual(store.list_entries("group:10001")[0].persona, "catty")
            self.assertEqual(store.last_summary_date("group:10001", persona="catty"), "2026-08-03")
            self.assertEqual(store.last_summary_date("group:10001", persona="fadianji"), "")

    def test_meme_kind_remains_persona_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ScopeLorebookStore(Path(directory) / "memory.json")
            store.add_entry("group:10001", ["梗"], "Catty meme", persona="catty", kind="meme")
            store.add_entry("group:10001", ["梗"], "Fadianji meme", persona="fadianji", kind="meme")
            self.assertEqual([item.content for item in store.list_entries("group:10001", persona="catty", kind="meme")], ["Catty meme"])
            self.assertEqual([item.content for item in store.list_entries("group:10001", persona="fadianji", kind="meme")], ["Fadianji meme"])

    def test_new_lore_and_summary_throttle_are_persona_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.json"
            store = ScopeLorebookStore(memory_path)
            catty_entry = store.add_entry(
                "group:10001",
                ["猫梗"],
                "Catty 专属 lore",
                persona="catty",
            )
            fadianji_entry = store.add_entry(
                "group:10001",
                ["机梗"],
                "Fadianji 专属 lore",
                persona="fadianji",
            )

            self.assertIsNotNone(catty_entry)
            self.assertIsNotNone(fadianji_entry)
            assert catty_entry is not None and fadianji_entry is not None
            self.assertNotEqual(catty_entry.identifier, fadianji_entry.identifier)
            self.assertEqual(
                [item.content for item in store.list_entries("group:10001", persona="catty")],
                ["Catty 专属 lore"],
            )
            self.assertEqual(
                [item.content for item in store.list_entries("group:10001", persona="fadianji")],
                ["Fadianji 专属 lore"],
            )

            store.mark_summarized_on("group:10001", "2026-08-03", persona="catty")
            self.assertTrue(store.was_summarized_on("group:10001", "2026-08-03", persona="catty"))
            self.assertFalse(store.was_summarized_on("group:10001", "2026-08-03", persona="fadianji"))
            store.mark_summarized_on("group:10001", "2026-08-03", persona="fadianji")
            self.assertTrue(store.was_summarized_on("group:10001", "2026-08-03", persona="fadianji"))

            self.assertTrue(store.flush_sync())
            reloaded = ScopeLorebookStore(memory_path)
            self.assertEqual(
                [item.content for item in reloaded.list_entries("group:10001", persona="catty")],
                ["Catty 专属 lore"],
            )
            self.assertEqual(
                [item.content for item in reloaded.list_entries("group:10001", persona="fadianji")],
                ["Fadianji 专属 lore"],
            )
            self.assertTrue(reloaded.was_summarized_on("group:10001", "2026-08-03", persona="catty"))
            self.assertTrue(reloaded.was_summarized_on("group:10001", "2026-08-03", persona="fadianji"))


class RagPersonaIsolationTests(unittest.TestCase):
    def test_add_metadata_and_lore_backfill_keep_persona(self) -> None:
        collection = _FakeCollection()
        store = _rag_store(collection)

        store.add("group:10001", "Catty 回复", role="assistant", user_id="7", ts=1.0, persona="catty")
        store.add("group:10001", "Fadianji 回复", role="assistant", user_id="7", ts=1.0, persona="fadianji")
        self.assertEqual(len(collection.upserts), 2)
        first_id = collection.upserts[0]["ids"][0]
        second_id = collection.upserts[1]["ids"][0]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(first_id, "1000:assistant:7")
        self.assertEqual(second_id, "1000:assistant:7:fadianji")
        self.assertEqual(collection.upserts[0]["metadatas"][0]["persona"], "catty")
        self.assertEqual(collection.upserts[1]["metadatas"][0]["persona"], "fadianji")

        lorebook = types.SimpleNamespace(
            _data={
                "group:10001": [
                    ScopeLoreEntry(
                        identifier="scope_lore_catty",
                        keys=("catty",),
                        content="Catty lore",
                        created_at=2.0,
                        persona="catty",
                    ),
                    ScopeLoreEntry(
                        identifier="scope_lore_fdj",
                        keys=("机梗",),
                        content="Fadianji lore",
                        created_at=2.0,
                        persona="fadianji",
                    )
                ]
            }
        )
        self.assertEqual(store.backfill_lorebook(lorebook), 2)
        catty_lore = collection.upserts[-2]
        fadianji_lore = collection.upserts[-1]
        self.assertEqual(catty_lore["ids"][0], "2000:lore_entry:na")
        self.assertEqual(fadianji_lore["ids"][0], "2000:lore_entry:na:fadianji")
        self.assertEqual(catty_lore["metadatas"][0]["role"], "lore_entry")
        self.assertEqual(catty_lore["metadatas"][0]["persona"], "catty")
        self.assertEqual(fadianji_lore["metadatas"][0]["role"], "lore_entry")
        self.assertEqual(fadianji_lore["metadatas"][0]["persona"], "fadianji")

    def test_query_shares_facts_but_never_leaks_other_persona_memory(self) -> None:
        collection = _FakeCollection(
            documents=[
                "shared user fact",
                "shared memory summary",
                "legacy Catty assistant",
                "Catty assistant",
                "Fadianji assistant",
                "legacy Catty lore",
                "Catty lore",
                "Fadianji lore",
                "unknown-role record",
            ],
            metadatas=[
                {"role": "user", "ts": 1.0},
                {"role": "memory_summary", "ts": 1.0},
                {"role": "assistant", "ts": 1.0},
                {"role": "assistant", "persona": "catty", "ts": 1.0},
                {"role": "assistant", "persona": "fadianji", "ts": 1.0},
                {"role": "lore_entry", "ts": 1.0},
                {"role": "lore_entry", "persona": "catty", "ts": 1.0},
                {"role": "lore_entry", "persona": "fadianji", "ts": 1.0},
                {"role": "unexpected_role", "ts": 1.0},
            ],
            distances=[0.1] * 9,
        )
        store = _rag_store(collection)

        catty_texts = {
            text for _score, text, _metadata in store.query(
                "group:10001",
                "旧事",
                top_k=10,
                min_similarity=0.0,
                boost_recency=False,
                persona="catty",
            )
        }
        fadianji_texts = {
            text for _score, text, _metadata in store.query(
                "group:10001",
                "旧事",
                top_k=10,
                min_similarity=0.0,
                boost_recency=False,
                persona="fadianji",
            )
        }

        self.assertTrue({"shared user fact", "shared memory summary"}.issubset(catty_texts))
        self.assertTrue({"shared user fact", "shared memory summary"}.issubset(fadianji_texts))
        self.assertNotIn("unknown-role record", catty_texts)
        self.assertNotIn("unknown-role record", fadianji_texts)
        self.assertTrue({"legacy Catty assistant", "Catty assistant", "legacy Catty lore", "Catty lore"}.issubset(catty_texts))
        self.assertNotIn("Fadianji assistant", catty_texts)
        self.assertNotIn("Fadianji lore", catty_texts)
        self.assertTrue({"Fadianji assistant", "Fadianji lore"}.issubset(fadianji_texts))
        self.assertNotIn("legacy Catty assistant", fadianji_texts)
        self.assertNotIn("legacy Catty lore", fadianji_texts)
        self.assertNotIn("Catty assistant", fadianji_texts)
        self.assertNotIn("Catty lore", fadianji_texts)
        self.assertTrue(collection.query_calls[-1].get("where"))

    def test_recall_prompt_forwards_requested_persona(self) -> None:
        class _RecordingStore:
            enabled = True

            def __init__(self) -> None:
                self.persona = None

            def query(self, scope, user_text, *, top_k, persona):
                self.persona = persona
                return []

        store = _RecordingStore()
        self.assertEqual(
            build_rag_recall_prompt(store, "group:10001", "旧事", persona="fadianji"),
            "",
        )
        self.assertEqual(store.persona, "fadianji")


class PromptRoutingTests(unittest.TestCase):
    def test_prompt_manager_forwards_current_persona_to_lore_and_rag(self) -> None:
        source = (_ROOT / "src" / "catty_qq_ai" / "prompt_manager.py").read_text(encoding="utf-8")
        self.assertIn("scope_lore_store.list_entries(scope, persona=persona.name)", source)
        self.assertIn("scope_lore_store.mark_hit(", source)
        self.assertIn("build_rag_recall_prompt(", source)
        self.assertIn("persona=persona.name,", source)


class TimeBucketPersonaCleanupTests(unittest.TestCase):
    def test_reset_matching_discovers_disk_only_per_user_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TimeBucketContextStore(directory)
            scope = "group:10001"
            disk_only_scope = f"{scope}:user:20002"
            memory_only_scope = f"{scope}:user:20003"
            other_scope = "group:10002:user:20002"

            for key in (scope, disk_only_scope, other_scope):
                path = store._path(key)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"key": key, "current": None, "finalized": []}),
                    encoding="utf-8",
                )
            (Path(directory) / "broken.json").write_text("{bad json", encoding="utf-8")
            store._data[memory_only_scope] = {
                "key": memory_only_scope,
                "current": None,
                "finalized": [],
            }

            self.assertEqual(
                store.known_scopes(f"{scope}:user:"),
                [disk_only_scope, memory_only_scope],
            )
            self.assertEqual(store.reset_matching(f"{scope}:user:"), 2)
            self.assertFalse(store._path(disk_only_scope).exists())
            self.assertNotIn(memory_only_scope, store._data)
            self.assertTrue(store._path(scope).exists())
            self.assertTrue(store._path(other_scope).exists())

            self.assertTrue(store.reset_scope(scope))
            self.assertFalse(store._path(scope).exists())

    def test_hot_switch_uses_exact_scope_cleanup_and_baseline(self) -> None:
        source = (_ROOT / "src" / "catty_qq_ai" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_ensure_session_persona_isolation"
        )
        lines = source.splitlines()
        function_source = "\n".join(lines[function.lineno - 1:function.end_lineno])

        self.assertIn("baseline_scope = _persona_baseline_scope(key)", function_source)
        self.assertIn("_load_session_persona_baselines()", function_source)
        self.assertIn('previous_name = "catty"', function_source)
        self.assertIn("_clear_persona_runtime_state(baseline_scope)", function_source)
        self.assertIn("_record_session_persona_baseline(baseline_scope, current_name)", function_source)
        self.assertNotIn("_clear_persona_runtime_state(_base_persona_scope(key))", function_source)


class SessionPersonaBaselinePersistenceTests(unittest.TestCase):
    def test_missing_baseline_treats_legacy_as_catty_and_persists_exact_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catty_memory_path = root / "catty" / "memory.json"
            runtime, cache, _buckets, _reminders, restore = _make_persona_isolation_runtime(
                catty_memory_path,
                session_keys=["group:10001", "group:10001:user:7"],
                bucket_scopes=["group:10001", "group:10001:user:8"],
            )
            try:
                runtime["_ensure_session_persona_isolation"](
                    "group:10001:user:7",
                    types.SimpleNamespace(name="catty"),
                )
                self.assertEqual(cache.popped, [])
                payload = json.loads(
                    (catty_memory_path.parent / "session_persona_baselines.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(payload["scopes"], {"group:10001:user:7": "catty"})
            finally:
                restore()

            fadianji_memory_path = root / "fadianji" / "memory.json"
            runtime, cache, buckets, reminders, restore = _make_persona_isolation_runtime(
                fadianji_memory_path,
                session_keys=["group:10001", "group:10001:user:7"],
                bucket_scopes=["group:10001", "group:10001:user:8"],
            )
            try:
                runtime["_ensure_session_persona_isolation"](
                    "group:10001:user:7",
                    types.SimpleNamespace(name="fadianji"),
                )
                self.assertEqual(cache.popped, ["group:10001:user:7"])
                self.assertEqual(buckets.reset_scope_calls, ["group:10001:user:7"])
                self.assertEqual(buckets.reset_matching_calls, [])
                self.assertEqual(reminders, ["group:10001:user:7"])
                payload = json.loads(
                    (fadianji_memory_path.parent / "session_persona_baselines.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(payload["scopes"], {"group:10001:user:7": "fadianji"})
                self.assertEqual(
                    list(fadianji_memory_path.parent.glob("*.tmp")),
                    [],
                )
            finally:
                restore()

    def test_cold_start_loads_persisted_exact_baseline_before_clearing_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.json"
            baseline_path = memory_path.parent / "session_persona_baselines.json"
            baseline_path.write_text(
                json.dumps(
                    {"version": 1, "scopes": {"group:10001:user:7": "catty"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            runtime, cache, _buckets, _reminders, restore = _make_persona_isolation_runtime(
                memory_path,
                session_keys=["group:10001", "group:10001:user:7"],
                bucket_scopes=["group:10001:user:8"],
            )
            try:
                runtime["_ensure_session_persona_isolation"](
                    "group:10001:user:7",
                    types.SimpleNamespace(name="fadianji"),
                )
                self.assertEqual(cache.popped, ["group:10001:user:7"])
                payload = json.loads(baseline_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["scopes"]["group:10001:user:7"], "fadianji")
            finally:
                restore()

    def test_per_user_switch_does_not_clear_base_scope_or_sibling_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.json"
            baseline_path = memory_path.parent / "session_persona_baselines.json"
            baseline_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "scopes": {
                            "group:10001": "catty",
                            "group:10001:user:7": "catty",
                            "group:10001:user:8": "catty",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            runtime, cache, buckets, reminders, restore = _make_persona_isolation_runtime(
                memory_path,
                session_keys=[
                    "group:10001",
                    "group:10001:user:7",
                    "group:10001:user:8",
                ],
                bucket_scopes=[
                    "group:10001",
                    "group:10001:user:7",
                    "group:10001:user:8",
                ],
            )
            try:
                runtime["_ensure_session_persona_isolation"](
                    "group:10001:user:7",
                    types.SimpleNamespace(name="fadianji"),
                )
                self.assertEqual(cache.popped, ["group:10001:user:7"])
                self.assertEqual(
                    cache._keys,
                    {"group:10001", "group:10001:user:8"},
                )
                self.assertEqual(buckets.reset_scope_calls, ["group:10001:user:7"])
                self.assertEqual(buckets.reset_matching_calls, [])
                self.assertEqual(reminders, ["group:10001:user:7"])
                payload = json.loads(baseline_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    payload["scopes"],
                    {
                        "group:10001": "catty",
                        "group:10001:user:7": "fadianji",
                        "group:10001:user:8": "catty",
                    },
                )
            finally:
                restore()

    def test_baseline_sidecar_force_reload_invalidate_and_unique_tmp_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.json"
            baseline_path = memory_path.parent / "session_persona_baselines.json"
            runtime, _cache, _buckets, _reminders, restore = _make_persona_isolation_runtime(
                memory_path,
                session_keys=[],
                bucket_scopes=[],
            )
            try:
                tokens = iter(("first", "second"))
                runtime["uuid"] = types.SimpleNamespace(
                    uuid4=lambda: types.SimpleNamespace(hex=next(tokens)),
                )
                temp_paths: list[Path] = []
                original_replace = Path.replace

                def record_replace(source: Path, target: Path) -> Path:
                    temp_paths.append(source)
                    return original_replace(source, target)

                with mock.patch.object(Path, "replace", new=record_replace):
                    runtime["_record_session_persona_baseline"](
                        "group:10001:user:7",
                        "catty",
                    )
                    runtime["_record_session_persona_baseline"](
                        "group:10001:user:8",
                        "fadianji",
                    )

                self.assertEqual(len(temp_paths), 2)
                self.assertNotEqual(temp_paths[0].name, temp_paths[1].name)
                self.assertTrue(all(not temp_path.exists() for temp_path in temp_paths))

                baseline_path.write_text(
                    json.dumps(
                        {"version": 1, "scopes": {"group:10001:user:7": "fadianji"}},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                runtime["_load_session_persona_baselines"](force_reload=True)
                self.assertEqual(
                    runtime["_session_persona_names"],
                    {"group:10001:user:7": "fadianji"},
                )

                baseline_path.write_text(
                    json.dumps(
                        {"version": 1, "scopes": {"group:10001:user:7": "catty"}},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                runtime["_invalidate_session_persona_baselines"]()
                self.assertEqual(runtime["_session_persona_names"], {})
                runtime["_load_session_persona_baselines"]()
                self.assertEqual(
                    runtime["_session_persona_names"],
                    {"group:10001:user:7": "catty"},
                )
            finally:
                restore()


class HandleChatContextLifecycleTests(unittest.TestCase):
    def test_context_callbacks_reset_tokens_when_registration_fails(self) -> None:
        source = (_ROOT / "src" / "catty_qq_ai" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        handle_chat = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_chat"
        )
        lines = source.splitlines()
        handle_source = "\n".join(lines[handle_chat.lineno - 1:handle_chat.end_lineno])
        start = handle_source.index("from .openai_client import")
        end = handle_source.index("# ── 主人 2026-07-06: token 计费门", start)
        lifecycle_source = handle_source[start:end]
        compact = re.sub(r"\s+", " ", lifecycle_source)
        compact = re.sub(r"\(\s+", "(", compact)
        compact = re.sub(r"\s+\)", ")", compact)

        self.assertIn("except Exception as _request_context_import_exc", lifecycle_source)
        self.assertNotIn("except Exception: # noqa: BLE001 pass", compact)
        for reset_name, token_name in (
            ("reset_current_scope_key", "_scope_key_token"),
            ("reset_current_model_override", "_model_override_token"),
            ("reset_current_persona_reply_context", "_persona_reply_context_token"),
        ):
            self.assertRegex(
                lifecycle_source,
                rf"_lock_stack\.callback\(\s*{reset_name},\s*{token_name},?\s*\)",
            )
            self.assertIn(f"{reset_name}({token_name})", lifecycle_source)
        self.assertIn("_distill_context_cleanup = set_current_distill_context(", lifecycle_source)
        self.assertIn("_lock_stack.callback(_distill_context_cleanup)", lifecycle_source)
        self.assertIn("_distill_context_cleanup()", lifecycle_source)


class CattyCatalogTextTests(unittest.TestCase):
    def test_web_search_cooldown_reply_uses_dynamic_remaining_time(self) -> None:
        source = (_ROOT / "src" / "catty_qq_ai" / "personas" / "catty.py").read_text(encoding="utf-8")
        start = source.index("web_search_cooldown_reply=")
        end = source.index("web_search_failure_instruction=", start)
        cooldown_source = source[start:end]

        self.assertIn("冷却中", cooldown_source)
        self.assertIn("{remaining}", cooldown_source)
        self.assertNotIn("每个人 10 分钟", cooldown_source)
        self.assertNotRegex(cooldown_source, r"\d+\s*(?:秒|s)")


class ReplySplitPolicyTests(unittest.TestCase):
    def _load_reply_chunk_helpers(self) -> dict[str, object]:
        return _load_init_functions(
            "_reply_chunks",
            "_looks_like_qq_short_chat",
            "_looks_like_nsfw_segment",
            "_cap_reply_chunks",
            "_newline_bubble_chunks",
            extra_globals={
                "config": types.SimpleNamespace(
                    catty_reply_human_split_max_chunks=4,
                    catty_reply_max_chars=1800,
                ),
                "re": re,
                "split_reply": lambda text, _max_chars, *, max_chunks=2: [text.strip()] if text.strip() else [],
                "_strip_unicode_emoji_for_send": lambda text: text,
                "_TECHNICAL_FORMATTING_PATTERNS": ("```", "$$", r"\frac", r"\sqrt"),
                "TRAILING_CHAT_PUNCTUATION": " \t\r\n。！？!?；;，,、：:….",
                "REPLY_SPLIT_MARKER": "<<<CATTY_REPLY_SPLIT>>>",
                "_LINE_BUBBLE_MAX_CHARS": 80,
            },
        )

    def test_fadianji_plain_text_stays_one_bubble(self) -> None:
        # 主人 2026-10-25: marker 显式分条; 没 marker 时短句逐行也按行发, 空格/斜杠/长行不拆。
        helpers = self._load_reply_chunk_helpers()
        persona = types.SimpleNamespace(name="fadianji")
        for text in (
            "在呢 咋了 想我了",
            "在呢　咋了　想我了",
            "活了 活蹦乱跳的 咋 刚才吓着你了",
            "赞了赞了 不过那条动态夸一半就开始踩机 几个意思",
            "config.json 路径 错了",
            "今天 下午 吃饭 了吗",
            "我靠 / 什么神人啊",
            "https://example.com/a/b",
            "1) 先看路径\n2) 再改配置",
        ):
            with self.subTest(text=text):
                self.assertEqual(helpers["_reply_chunks"](text, persona=persona), [text])

    def test_fadianji_short_lines_split_without_marker(self) -> None:
        helpers = self._load_reply_chunk_helpers()
        persona = types.SimpleNamespace(name="fadianji")
        self.assertEqual(
            helpers["_reply_chunks"]("我去\n好厉害", persona=persona),
            ["我去", "好厉害"],
        )
        self.assertEqual(
            helpers["_reply_chunks"]("我觉得这件事情\n其实没有\n想象中那么复杂", persona=persona),
            ["我觉得这件事情", "其实没有", "想象中那么复杂"],
        )

    def test_explicit_split_marker_sends_separate_messages(self) -> None:
        helpers = self._load_reply_chunk_helpers()
        for name in ("fadianji", "catty"):
            persona = types.SimpleNamespace(name=name)
            with self.subTest(persona=name):
                self.assertEqual(
                    helpers["_reply_chunks"](
                        "我去<<<CATTY_REPLY_SPLIT>>>好厉害",
                        persona=persona,
                    ),
                    ["我去", "好厉害"],
                )

    def test_catty_plain_newline_also_splits(self) -> None:
        helpers = self._load_reply_chunk_helpers()
        persona = types.SimpleNamespace(name="catty")

        self.assertEqual(
            helpers["_reply_chunks"]("猫猫看到了\n主人别急", persona=persona),
            ["猫猫看到了", "主人别急"],
        )


class PersonaCommandLockingTests(unittest.TestCase):
    def test_persona_mutations_share_handle_chat_queueing_primitives(self) -> None:
        source = (_ROOT / "src" / "catty_qq_ai" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }
        lines = source.splitlines()

        def _function_source(name: str) -> str:
            node = functions[name]
            return "\n".join(lines[node.lineno - 1:node.end_lineno])

        chat_source = _function_source("handle_chat")
        persona_source = _function_source("handle_persona_command")
        user_lock = "user_lock = _user_in_scope_locks[_user_in_scope_lock_key(event)]"
        group_sema = "group_sema = _group_concurrency_sema_for(event)"
        chat_transition_gate = "transition_lock = _persona_transition_locks[transition_scope]"
        persona_transition_gate = "persona_transition_lock = _persona_transition_locks[transition_scope]"

        for primitive in (
            user_lock,
            group_sema,
            "await _lock_stack.enter_async_context(user_lock)",
            "await _lock_stack.enter_async_context(group_sema)",
        ):
            self.assertIn(primitive, chat_source)
            self.assertIn(primitive, persona_source)
        self.assertIn(chat_transition_gate, chat_source)
        self.assertIn(persona_transition_gate, persona_source)
        self.assertLess(
            chat_source.index("await transition_lock.acquire()"),
            chat_source.index("await _lock_stack.enter_async_context(user_lock)"),
        )
        self.assertLess(
            persona_source.index("await _lock_stack.enter_async_context(persona_transition_lock)"),
            persona_source.index("await _lock_stack.enter_async_context(user_lock)"),
        )

        self.assertLess(persona_source.index("if not arg:"), persona_source.index(user_lock))
        self.assertLess(persona_source.index("if name == _effective() and"), persona_source.index(user_lock))
        self.assertLess(
            persona_source.index("await _lock_stack.enter_async_context(user_lock)"),
            persona_source.index("asyncio.to_thread(persona_override_store.clear"),
        )
        self.assertIn("for _ in range(group_sema_slots):", persona_source)
        self.assertIn("_clear_persona_runtime_state(scope)", persona_source)
        self.assertIn("_record_session_persona_baseline(scope, effective_persona)", persona_source)
        self.assertLess(
            persona_source.index("_clear_persona_runtime_state(scope)"),
            persona_source.index("_record_session_persona_baseline(scope, effective_persona)"),
        )

    def test_persona_mutation_records_the_effective_baseline_inside_its_lock(self) -> None:
        class _Finished(Exception):
            pass

        class _Matcher:
            async def finish(self, _message) -> None:
                raise _Finished

        class _GroupEvent:
            group_id = 10001
            user_id = 7

        class _OverrideStore:
            def __init__(self) -> None:
                self.value = ""

            def get(self, _scope: str):
                return self.value or None

            def set(self, _scope: str, name: str, *, set_by: str = "") -> None:
                self.value = name

            def clear(self, _scope: str) -> bool:
                existed = bool(self.value)
                self.value = ""
                return existed

        async def scenario() -> None:
            override_store = _OverrideStore()
            cleared: list[str] = []
            baselines: list[tuple[str, str]] = []
            personas_module_name = f"{_PACKAGE_NAME}.personas"
            previous_personas_module = sys.modules.get(personas_module_name)
            personas_module = types.ModuleType(personas_module_name)
            personas_module.PERSONAS = {"catty": object(), "fadianji": object()}
            personas_module.PERSONA_ALIASES = {}
            personas_module.normalize_persona_name = lambda name: (
                name if name in personas_module.PERSONAS else None
            )
            personas_module.resolve_persona_name = lambda _scope, *, config, override_store: (
                personas_module.normalize_persona_name(override_store.get(_scope)) or config.catty_default_persona
            )
            sys.modules[personas_module_name] = personas_module
            try:
                runtime = _load_init_functions(
                    "handle_persona_command",
                    extra_globals={
                        "Matcher": object,
                        "asyncio": asyncio,
                        "MessageEvent": object,
                        "T_State": object,
                        "Message": lambda value: value,
                        "GroupMessageEvent": _GroupEvent,
                        "config": types.SimpleNamespace(
                            catty_default_persona="catty",
                            catty_reply_group_concurrency=0,
                        ),
                        "logger": _NoopLogger(),
                        "_event_is_owner": lambda _event: True,
                        "_conversation_queue_key": lambda _event: "group:10001",
                        "persona_override_store": override_store,
                        "_persona_config_transition_lock": asyncio.Lock(),
                        "_persona_transition_locks": defaultdict(asyncio.Lock),
                        "_user_in_scope_locks": defaultdict(asyncio.Lock),
                        "_group_concurrency_semas": {},
                        "_group_concurrency_sema_capacities": {},
                        "_locks": defaultdict(asyncio.Lock),
                        "_base_persona_scope": lambda scope: scope,
                        "_persona_baseline_scope": lambda scope: scope,
                        "_configured_group_concurrency": lambda _config=None: 0,
                        "_user_in_scope_lock_key": lambda _event: "group:10001:user:7",
                        "_group_concurrency_sema_for": lambda _event: None,
                        "_clear_persona_runtime_state": lambda scope: cleared.append(scope) or 1,
                        "_record_session_persona_baseline": (
                            lambda scope, name: baselines.append((scope, name))
                        ),
                    },
                )
                override_store.value = "missing_persona"
                with self.assertRaises(_Finished):
                    await runtime["handle_persona_command"](
                        _Matcher(), _GroupEvent(), {"catty_persona_cmd_arg": "catty"},
                    )
                self.assertEqual(override_store.value, "catty")
                cleared.clear()
                baselines.clear()
                with self.assertRaises(_Finished):
                    await runtime["handle_persona_command"](
                        _Matcher(),
                        _GroupEvent(),
                        {"catty_persona_cmd_arg": "fadianji"},
                    )
            finally:
                if previous_personas_module is None:
                    sys.modules.pop(personas_module_name, None)
                else:
                    sys.modules[personas_module_name] = previous_personas_module

            self.assertEqual(override_store.value, "fadianji")
            self.assertEqual(cleared, ["group:10001"])
            self.assertEqual(baselines, [("group:10001", "fadianji")])

        asyncio.run(scenario())

    def test_group_lock_is_used_when_group_concurrency_is_disabled(self) -> None:
        source = (_ROOT / "src" / "catty_qq_ai" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }
        lines = source.splitlines()

        def _function_source(name: str) -> str:
            node = functions[name]
            return "\n".join(lines[node.lineno - 1:node.end_lineno])

        chat_source = _function_source("handle_chat")
        persona_source = _function_source("handle_persona_command")
        for function_source, queue_key in (
            (chat_source, "queue_key"),
            (persona_source, "transition_scope"),
        ):
            self.assertIn(f"_locks[{queue_key}]", function_source)
            self.assertIn("group_sema is None", function_source)
            self.assertIn("if group_lock is not None:", function_source)
            self.assertIn(
                "await _lock_stack.enter_async_context(group_lock)",
                function_source,
            )


if __name__ == "__main__":
    unittest.main()