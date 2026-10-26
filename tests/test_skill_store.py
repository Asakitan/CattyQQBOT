"""Offline SkillStore contract tests; never import/activate the NoneBot plugin."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/catty_qq_ai/skill_store.py"
PACKAGE = "_catty_skill_store_tests"
shell = types.ModuleType(PACKAGE)
shell.__path__ = [str(MODULE_PATH.parent)]
sys.modules.setdefault(PACKAGE, shell)
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.skill_store", MODULE_PATH)
assert spec is not None and spec.loader is not None
store_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = store_module
spec.loader.exec_module(store_module)
SkillStore = store_module.SkillStore


def definition(skill_id="python-files", **changes):
    value = {
        "id": skill_id, "title": "Python 文件处理", "description": "安全读取文本文件",
        "keywords": ["python", "文件处理"], "body": "先检查编码，再读取文件。\n保留原始换行。",
        "sources": [{"url": "https://docs.example.org/python/files", "title": "文件指南",
                     "fetched_at": "2026-10-27T01:02:03Z", "content_sha256": "a" * 64}],
    }
    value.update(changes)
    return value


class SkillStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Resolve the test fixture only, not untrusted paths inside the store.
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "skills"
        self.store = SkillStore(self.root)

    def path(self, skill_id="python-files"):
        return self.root / f"{skill_id}.json"

    def saved(self, skill_id="python-files", **changes):
        result = self.store.save_verified(definition(skill_id, **changes))
        self.assertTrue(result["ok"], result)
        return result

    def assert_error(self, result, error):
        self.assertEqual(result, {"ok": False, "error": error})

    def assert_no_temporaries(self):
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_constructor_performs_no_filesystem_scan_or_load(self):
        with patch.object(store_module.os, "scandir", side_effect=AssertionError("scan")), \
                patch.object(store_module.Path, "lstat", side_effect=AssertionError("stat")), \
                patch.object(store_module.Path, "mkdir", side_effect=AssertionError("mkdir")), \
                patch.object(store_module.os, "open", side_effect=AssertionError("open")):
            fresh = SkillStore(self.root)
            self.assertEqual(fresh._metadata, {})
        self.assertFalse(self.root.exists())

    def test_empty_search_and_missing_load_do_not_create_root(self):
        self.assertEqual(self.store.search("python"), {"ok": True, "candidates": []})
        self.assert_error(self.store.load("missing", expected_sha256="0" * 64), "not_found")
        self.assertFalse(self.root.exists())

    def test_metadata_has_exact_fields_and_no_body_in_cache_or_search(self):
        sentinel = "BODY_ONLY_PRIVATE_SENTINEL_371"
        first = self.saved(body=sentinel)
        result = self.store.search("python")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["candidates"]), 1)
        item = result["candidates"][0]
        self.assertEqual(set(item), {"id", "title", "description", "keywords", "sha256",
                                     "body_chars", "updated_at", "sources"})
        self.assertEqual(item["sha256"], first["sha256"])
        self.assertEqual(item["body_chars"], len(sentinel))
        self.assertNotIn(sentinel, repr(result))
        self.assertNotIn(sentinel, repr(vars(self.store)))
        self.assertEqual(self.store.search(sentinel)["candidates"], [])
        loaded = self.store.load("python-files", expected_sha256=first["sha256"])
        self.assertEqual(loaded["skill"]["body"], sentinel)
        self.assertNotIn(sentinel, repr(vars(self.store)))

    def test_relevance_chinese_bigrams_keywords_and_no_zero_score_padding(self):
        self.saved()
        self.saved("photo-tools", title="相片压缩指南", description="图片压缩与格式转换",
                   keywords=["压缩图片", "jpeg"], body="python文件处理仅在正文出现")
        self.saved("garden", title="Garden", description="Seed growing", keywords=["soil"])
        self.assertEqual([item["id"] for item in self.store.search("Python 文件处理")["candidates"]],
                         ["python-files"])
        self.assertEqual([item["id"] for item in self.store.search("图片压缩")["candidates"]],
                         ["photo-tools"])
        self.assertEqual([item["id"] for item in self.store.search("JPEG")["candidates"]],
                         ["photo-tools"])
        self.assertEqual(self.store.search("unrelatedxyz")["candidates"], [])

    def test_source_url_title_and_body_are_not_search_keywords(self):
        self.saved(body="uniquehiddenbody", sources=[{
            "url": "https://uniquesourcehostname.example.org/uniquesourcepath",
            "title": "uniquesourcetitle", "fetched_at": "2026-10-27T00:00:00Z",
            "content_sha256": "b" * 64,
        }])
        for query in ("uniquehiddenbody", "uniquesourcetitle", "uniquesourcehostname", "uniquesourcepath"):
            self.assertEqual(self.store.search(query)["candidates"], [])

    def test_query_and_limit_validation_and_limit_cap(self):
        for query in ("", " \n", None, [], "x" * 1001):
            self.assert_error(self.store.search(query), "invalid_query")
        for limit in (0, -1, True, 2.5, "3", None):
            self.assert_error(self.store.search("python", limit=limit), "invalid_limit")
        for index in range(7):
            self.saved(f"item-{index}")
        self.assertEqual(len(self.store.search("python")["candidates"]), 3)
        self.assertEqual(len(self.store.search("python", limit=100)["candidates"]), 5)
        self.assertEqual(len(self.store.search("python", limit=1)["candidates"]), 1)
        self.assertEqual(self.store.search("!!!")["candidates"], [])

    def test_defensive_copies_do_not_poison_metadata(self):
        first = self.saved()
        result = self.store.search("python")
        result["candidates"][0]["keywords"].clear()
        result["candidates"][0]["sources"][0]["title"] = "changed"
        again = self.store.search("python")["candidates"][0]
        self.assertEqual(again["keywords"], ["python", "文件处理"])
        self.assertEqual(again["sources"][0]["title"], "文件指南")
        loaded = self.store.load("python-files", expected_sha256=first["sha256"])
        loaded["skill"]["body"] = "changed"
        self.assertNotEqual(self.store.load("python-files", expected_sha256=first["sha256"])["skill"]["body"],
                            "changed")

    def test_cross_instance_create_search_load_and_update(self):
        peer = SkillStore(self.root)
        self.assertEqual(peer.search("python")["candidates"], [])
        first = self.saved()
        metadata = peer.search("python")["candidates"][0]
        self.assertEqual(peer.load(metadata["id"], expected_sha256=metadata["sha256"])["skill"]["body"],
                         definition()["body"])
        second = peer.save_verified(definition(body="更新后的流程"), overwrite=True,
                                    expected_sha256=first["sha256"])
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["status"], "updated")
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assert_error(self.store.load("python-files", expected_sha256=first["sha256"]), "sha_conflict")
        self.assertEqual(self.store.search("python")["candidates"][0]["sha256"], second["sha256"])
        restarted = SkillStore(self.root)
        self.assertEqual(restarted.load("python-files", expected_sha256=second["sha256"])["skill"]["body"],
                         "更新后的流程")

    def test_exact_file_sha_global_visibility_and_server_timestamps(self):
        value = definition(visibility="private", visible="private",
                           created_at="1970-01-01T00:00:00Z", updated_at="1970-01-01T00:00:00Z")
        before_input = deepcopy(value)
        lower = datetime.now(timezone.utc)
        first = self.store.save_verified(value)
        self.assertTrue(first["ok"], first)
        raw = self.path().read_bytes()
        self.assertEqual(first["sha256"], hashlib.sha256(raw).hexdigest())
        skill = json.loads(raw)
        self.assertEqual(value, before_input)
        self.assertEqual(skill["visibility"], "global")
        self.assertNotIn("visible", skill)
        self.assertNotIn("sha256", skill)
        self.assertNotIn("body_chars", skill)
        created = datetime.fromisoformat(skill["created_at"].replace("Z", "+00:00"))
        self.assertGreaterEqual(created, lower)
        self.assertEqual(skill["created_at"], skill["updated_at"])
        second = self.store.save_verified(value, overwrite=True, expected_sha256=first["sha256"])
        self.assertTrue(second["ok"], second)
        updated = json.loads(self.path().read_bytes())
        self.assertEqual(updated["created_at"], skill["created_at"])
        self.assertGreater(updated["updated_at"], skill["updated_at"])
        self.assertEqual(updated["sources"][0]["fetched_at"], "2026-10-27T01:02:03.000000Z")

    def test_create_conflict_and_optimistic_update_errors_preserve_file(self):
        first = self.saved()
        original = self.path().read_bytes()
        cases = [
            ({}, "already_exists"),
            ({"overwrite": True}, "expected_sha256_required"),
            ({"overwrite": True, "expected_sha256": "0" * 64}, "sha_conflict"),
            ({"overwrite": True, "expected_sha256": "bogus"}, "invalid_sha256"),
            ({"expected_sha256": first["sha256"]}, "unexpected_sha256"),
            ({"overwrite": 1}, "invalid_overwrite"),
        ]
        for options, code in cases:
            self.assert_error(self.store.save_verified(definition(body="overwrite attempt"), **options), code)
            self.assertEqual(self.path().read_bytes(), original)
        self.assert_error(self.store.save_verified(definition("missing"), overwrite=True,
                                                   expected_sha256="a" * 64), "not_found")
        self.path().unlink()
        self.assert_error(self.store.load("python-files", expected_sha256=first["sha256"]), "not_found")
        self.assertEqual(self.store.search("python")["candidates"], [])

    def test_load_requires_valid_sha_and_accepts_uppercase_hex(self):
        first = self.saved()
        for sha in (None, "", "a", "x" * 64, 1):
            self.assert_error(self.store.load("python-files", expected_sha256=sha), "invalid_sha256")
        self.assertTrue(self.store.load("python-files", expected_sha256=first["sha256"].upper())["ok"])

    def test_required_fields_unknown_fields_and_types(self):
        for key in ("id", "title", "description", "keywords", "body", "sources"):
            value = definition()
            del value[key]
            self.assert_error(self.store.save_verified(value), "invalid_definition")
        for value in (None, [], "secret", {"secret": "payload"}, definition(sha256="a" * 64)):
            self.assert_error(self.store.save_verified(value), "invalid_definition")
        cases = [
            ({"title": ""}, "invalid_title"), ({"title": "x" * 101}, "invalid_title"),
            ({"description": " "}, "invalid_description"),
            ({"description": "x" * 301}, "invalid_description"),
            ({"body": "\n\t"}, "invalid_body"), ({"body": "x" * 6001}, "invalid_body"),
            ({"body": "bad\0body"}, "invalid_body"), ({"body": "bad\ud800"}, "invalid_body"),
            ({"keywords": "python"}, "invalid_keywords"),
            ({"keywords": ["x"] * 9}, "invalid_keywords"),
            ({"keywords": [""]}, "invalid_keywords"), ({"keywords": [1]}, "invalid_keywords"),
        ]
        for changes, error in cases:
            with self.subTest(changes=list(changes), error=error):
                self.assert_error(self.store.save_verified(definition(**changes)), error)
        self.assertFalse(self.root.exists())

    def test_character_and_byte_limits_are_independent(self):
        exact = self.saved(body="中" * 6000, title="题" * 100, description="述" * 300, keywords=[])
        self.assertTrue(exact["ok"])
        short = SkillStore(self.base / "short", max_body_chars=3)
        self.assert_error(short.save_verified(definition(body="四个汉字")), "invalid_body")
        tiny = SkillStore(self.base / "tiny", max_bytes=20)
        self.assert_error(tiny.save_verified(definition()), "record_too_large")
        self.assertFalse((tiny.root / "python-files.json").exists())
        for changes in ({"max_bytes": 0}, {"max_bytes": True}, {"max_body_chars": -1}):
            with self.assertRaisesRegex(ValueError, "invalid_limits"):
                SkillStore(self.root, **changes)

    def test_sources_are_strict_receipts_not_publication_dates(self):
        for sources in ([], [{}], [definition()["sources"][0]] * 4, "url"):
            self.assert_error(self.store.save_verified(definition(sources=sources)), "invalid_sources")
        for field, value in (
            ("url", "file:///secret"), ("url", "https://user:token@example.org/a"),
            ("url", "https://example.org:bad/a"), ("url", "https://example.org/\nsecret"),
            ("title", ""), ("content_sha256", "bad"),
            ("fetched_at", "2026-10-27"), ("fetched_at", "2026-10-27T00:00:00"),
        ):
            source = definition()["sources"][0]
            source[field] = value
            self.assert_error(self.store.save_verified(definition(sources=[source])), "invalid_sources")
        source = definition()["sources"][0]
        source["published_at"] = "2026-10-27"
        self.assert_error(self.store.save_verified(definition(sources=[source])), "invalid_sources")
        source = definition()["sources"][0]
        source["fetched_at"] = "2026-10-27T09:02:03+08:00"
        self.saved(sources=[source])
        self.assertEqual(json.loads(self.path().read_bytes())["sources"][0]["fetched_at"],
                         "2026-10-27T01:02:03.000000Z")

    def test_unsafe_ids_do_not_access_arbitrary_paths(self):
        ids = ["../escape", "..", "a/b", "a\\b", "E:secret", "/absolute", "a.json",
               "con", "aux", "nul", "prn", "com1", "lpt9", "CON", "Upper", "a_b",
               "-start", "end-", "a--b", "", "a\0b", "a" * 65, 123]
        for skill_id in ids:
            with self.subTest(skill_id=skill_id):
                self.assert_error(self.store.save_verified(definition(skill_id)), "invalid_id")
                self.assert_error(self.store.load(skill_id, expected_sha256="a" * 64), "invalid_id")
        self.assertFalse(self.root.exists())
        self.assertTrue(self.store.save_verified(definition("a" * 64))["ok"])

    def test_replace_and_fsync_failures_preserve_old_bytes_and_clean_temp(self):
        first = self.saved()
        original = self.path().read_bytes()
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation), \
                    patch.object(store_module.os, operation, side_effect=OSError("SECRET C:/private/path")):
                result = self.store.save_verified(definition(body="changed"), overwrite=True,
                                                  expected_sha256=first["sha256"])
            self.assert_error(result, "io_error")
            self.assertEqual(self.path().read_bytes(), original)
            self.assert_no_temporaries()
        with patch.object(store_module.os, "replace", side_effect=OSError("SECRET")):
            self.assert_error(self.store.save_verified(definition("new-skill")), "io_error")
        self.assertFalse(self.path("new-skill").exists())
        self.assert_no_temporaries()

    def test_invalid_update_and_size_failure_leave_original_bytes(self):
        first = self.saved()
        original = self.path().read_bytes()
        self.assert_error(self.store.save_verified(definition(body=""), overwrite=True,
                                                   expected_sha256=first["sha256"]), "invalid_body")
        bounded = SkillStore(self.root, max_bytes=len(original) + 5)
        self.assert_error(bounded.save_verified(definition(body="中" * 6000), overwrite=True,
                                                expected_sha256=first["sha256"]), "record_too_large")
        self.assertEqual(self.path().read_bytes(), original)
        self.assert_no_temporaries()

    def test_fsync_precedes_replace_with_unique_same_directory_temp(self):
        first = self.saved()
        fsync_original = store_module.os.fsync
        replace_original = store_module.os.replace
        events = []
        names = []

        def fsync(fd):
            events.append("fsync")
            return fsync_original(fd)

        def replace(source, target):
            self.assertEqual(events[-1], "fsync")
            self.assertEqual(Path(source).parent, Path(target).parent)
            self.assertEqual(Path(target), self.path())
            names.append(Path(source).name)
            events.append("replace")
            return replace_original(source, target)

        with patch.object(store_module.os, "fsync", side_effect=fsync), \
                patch.object(store_module.os, "replace", side_effect=replace):
            for index in range(2):
                first = self.store.save_verified(definition(body=f"change {index}"), overwrite=True,
                                                  expected_sha256=first["sha256"])
                self.assertTrue(first["ok"], first)
        self.assertEqual(events, ["fsync", "replace", "fsync", "replace"])
        self.assertEqual(len(set(names)), 2)
        self.assertNotIn("python-files.json.tmp", names)

    def test_revision_is_rechecked_immediately_before_replace(self):
        first = self.saved()
        foreign = self.path().read_bytes().replace("文件处理".encode(), "文件改写".encode())
        original_fsync = store_module.os.fsync

        def external_edit(fd):
            original_fsync(fd)
            self.path().write_bytes(foreign)

        with patch.object(store_module.os, "fsync", side_effect=external_edit):
            result = self.store.save_verified(definition(body="must not win"), overwrite=True,
                                              expected_sha256=first["sha256"])
        self.assert_error(result, "sha_conflict")
        self.assertEqual(self.path().read_bytes(), foreign)
        self.assert_no_temporaries()

    def test_bad_json_duplicate_keys_bad_schema_and_oversize_are_isolated(self):
        first = self.saved()
        valid = self.path().read_bytes()
        self.path("broken").write_bytes(b"{SECRET malformed")
        self.path("utf8").write_bytes(b"\xff\xfe")
        self.path("deep").write_bytes(b"[" * 1200 + b"]" * 1200)
        self.path("oversize").write_bytes(b"x" * (self.store.max_bytes + 1))
        self.path("wrong-id").write_bytes(valid)
        self.path("duplicate").write_bytes(b'{"id":"duplicate","id":"duplicate"}')
        self.path("wrong-schema").write_bytes(b'{"id":"wrong-schema","schema_version":true}')
        result = self.store.search("python")
        self.assertEqual([item["id"] for item in result["candidates"]], ["python-files"])
        self.assertEqual(result["candidates"][0]["sha256"], first["sha256"])
        raw = self.path("broken").read_bytes()
        self.assert_error(self.store.load("broken", expected_sha256=hashlib.sha256(raw).hexdigest()),
                          "invalid_record")
        self.assert_error(self.store.load("oversize", expected_sha256="a" * 64), "record_too_large")
        self.assertEqual(self.path("broken").read_bytes(), raw)
        self.path().write_bytes(b"{broken former valid")
        self.assertEqual(self.store.search("python")["candidates"], [])

    def test_scan_file_count_and_byte_bounds_are_explicit_errors(self):
        self.saved()
        self.saved("second")
        with patch.object(store_module, "_MAX_SCAN_ENTRIES", 1):
            self.assert_error(self.store.search("python"), "scan_limit")
        with patch.object(store_module, "_MAX_SCAN_BYTES", 1):
            self.assert_error(self.store.search("python"), "scan_limit")
        self.assertEqual(len(self.store.search("python")["candidates"]), 2)

    def _symlink(self, target: Path, link: Path, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {type(exc).__name__}")

    def test_file_symlink_is_rejected_for_read_write_and_search(self):
        first = self.saved()
        outside = self.base / "outside.json"
        outside.write_bytes(self.path().read_bytes())
        original = outside.read_bytes()
        self.path().unlink()
        self._symlink(outside, self.path())
        self.assert_error(self.store.load("python-files", expected_sha256=first["sha256"]), "unsafe_path")
        self.assert_error(self.store.save_verified(definition(), overwrite=True,
                                                   expected_sha256=first["sha256"]), "unsafe_path")
        self.assertEqual(self.store.search("python")["candidates"], [])
        self.assertEqual(outside.read_bytes(), original)

    def test_root_ancestor_and_lock_symlinks_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        alias = self.base / "alias"
        self._symlink(outside, alias, directory=True)
        for root in (alias, alias / "nested"):
            store = SkillStore(root)
            self.assert_error(store.search("python"), "unsafe_path")
            self.assert_error(store.save_verified(definition()), "unsafe_path")
            self.assert_error(store.load("python-files", expected_sha256="a" * 64), "unsafe_path")
        self.assertEqual(list(outside.iterdir()), [])
        self.root.mkdir()
        sentinel = self.base / "lock-target"
        sentinel.write_bytes(b"do not change")
        self._symlink(sentinel, self.root / ".skills.lock")
        self.assert_error(self.store.save_verified(definition()), "unsafe_path")
        self.assertEqual(sentinel.read_bytes(), b"do not change")

    def test_reparse_directory_attribute_is_rejected_even_without_symlink_mode(self):
        self.root.mkdir()
        original = store_module.Path.lstat

        def lstat(path, *args, **kwargs):
            if path == self.root:
                return types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path, *args, **kwargs)

        with patch.object(store_module.Path, "lstat", lstat):
            self.assert_error(self.store.search("python"), "unsafe_path")
            self.assert_error(self.store.save_verified(definition()), "unsafe_path")

    def test_hardlink_record_and_directory_record_are_rejected(self):
        first = self.saved()
        outside = self.base / "hardlinked.json"
        try:
            os.link(self.path(), outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hardlink unavailable: {type(exc).__name__}")
        self.assert_error(self.store.load("python-files", expected_sha256=first["sha256"]), "unsafe_path")
        self.assertEqual(self.store.search("python")["candidates"], [])
        self.path("directory").mkdir()
        self.assert_error(self.store.load("directory", expected_sha256="a" * 64), "unsafe_path")

    def test_same_root_instances_share_rlock(self):
        peer = SkillStore(str(self.root))
        self.assertIs(self.store._lock, peer._lock)

    def test_concurrent_threads_create_and_compare_swap_have_single_winner(self):
        workers = 8
        barrier = threading.Barrier(workers)

        def create(index):
            store = SkillStore(self.root)
            barrier.wait(timeout=10)
            return store.save_verified(definition(body=f"thread {index}"))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(create, range(workers)))
        winners = [item for item in results if item["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual([item["error"] for item in results if not item["ok"]], ["already_exists"] * 7)
        expected = winners[0]["sha256"]
        barrier = threading.Barrier(workers)

        def update(index):
            store = SkillStore(self.root)
            barrier.wait(timeout=10)
            return store.save_verified(definition(body=f"update {index}"), overwrite=True,
                                       expected_sha256=expected)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(update, range(workers)))
        winners = [item for item in results if item["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual([item["error"] for item in results if not item["ok"]], ["sha_conflict"] * 7)
        self.assertTrue(self.store.load("python-files", expected_sha256=winners[0]["sha256"])["ok"])
        self.assert_no_temporaries()

    def _process_wave(self, expected=None):
        # Each interpreter has an independent module/RLock table. Delaying fsync
        # widens the check/write race; only the OS file lock can serialize it.
        script = """
import importlib.util, json, sys, time
spec = importlib.util.spec_from_file_location('_isolated_skill_store', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_fsync = module.os.fsync
def fsync(fd):
    time.sleep(0.1)
    real_fsync(fd)
module.os.fsync = fsync
store = module.SkillStore(sys.argv[2])
value = json.loads(sys.argv[3])
expected = sys.argv[4] or None
result = store.save_verified(value, overwrite=expected is not None, expected_sha256=expected)
print(json.dumps(result, ensure_ascii=True))
"""

        def invoke(index):
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", "-c", script, str(MODULE_PATH),
                 str(self.root), json.dumps(definition(body=f"process {index}")), expected or ""],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(invoke, range(4)))

    def test_separate_processes_create_and_compare_swap_have_single_winner(self):
        results = self._process_wave()
        winners = [item for item in results if item["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual([item["error"] for item in results if not item["ok"]], ["already_exists"] * 3)
        results = self._process_wave(winners[0]["sha256"])
        winners = [item for item in results if item["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual([item["error"] for item in results if not item["ok"]], ["sha_conflict"] * 3)
        self.assertTrue(self.store.load("python-files", expected_sha256=winners[0]["sha256"])["ok"])
        self.assert_no_temporaries()


if __name__ == "__main__":
    unittest.main()