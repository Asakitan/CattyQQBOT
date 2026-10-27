from __future__ import annotations

import importlib
import json
import re
import sys
import types
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_catty_harness_v2_tests"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules[PACKAGE_NAME] = package
harness = importlib.import_module(f"{PACKAGE_NAME}.fadianji_harness")
corpus = importlib.import_module(f"{PACKAGE_NAME}.fdj_corpus_store")
Evidence = harness.Evidence
FLAGS = harness.QueryFlags("question", True, False)


class AllSubstringMemory:
    def __init__(self, rows=None, failures=()):
        self.rows = rows or {}
        self.failures = set(failures)
        self.calls = []
        self.note_calls = []

    def _profile_for(self, user_id, group_id):
        return {}

    def lookup_user_profile(self, user_id, group_id=""):
        return {}

    def recall_notes(self, *, user_id="", group_id="", limit=4):
        self.note_calls.append((user_id, group_id))
        return {}

    def recall(self, *, user_id="", group_id="", keywords="", limit=4):
        self.calls.append((user_id, group_id, keywords))
        if keywords in self.failures:
            raise ValueError("synthetic sensitive failure")
        scope = f"group:{group_id}" if group_id else f"private:{user_id}"
        terms = [part.casefold() for part in re.split(r"[\s,，;；]+", keywords) if part]
        matches = [row for row in self.rows.get(scope, ()) if all(term in row["text"].casefold() for term in terms)]
        return {"matches": matches[:limit]}


class HarnessV2Tests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.book = self.stack.enter_context(patch.object(harness, "_activate_character_book", return_value=[]))
        self.scenes = self.stack.enter_context(patch.object(harness, "match_scene_pairs", return_value=[]))
        self.stack.enter_context(patch.object(corpus, "corpus_enabled", return_value=True))

    def packet(self, text="还记得上次吗", **kwargs):
        options = dict(scope_key="group:test", group_id="test", user_id="u", corpus_profile_enabled=False)
        options.update(kwargs)
        return harness.build_fadianji_evidence_packet(text, **options)

    def assert_accounting(self, packet):
        evidence_ids = {item["id"] for item in packet["evidence"]}
        rendered, omitted = set(packet["rendered_ids"]), set(packet["omitted_ids"])
        self.assertFalse(rendered & omitted)
        self.assertEqual(rendered | omitted, evidence_ids)
        self.assertEqual(len(evidence_ids), len(packet["evidence"]))
        self.assertEqual(len(rendered), sum(packet["rendered_counts"].values()))
        self.assertEqual(len(omitted), packet["omitted_count"])
        self.assertEqual(len(omitted), sum(packet["omitted_counts"].values()))
        self.assertEqual(sum(packet["counts"].values()), len(evidence_ids))
        for bucket, total in packet["counts"].items():
            self.assertEqual(total, packet["rendered_counts"].get(bucket, 0) + packet["omitted_counts"].get(bucket, 0))
        self.assertEqual(sum(status["count"] for status in packet["source_status"].values()), len(evidence_ids))
        self.assertEqual(packet["rendered_chars"], len(packet["text"]))
        self.assertLessEqual(len(packet["text"]), packet["max_chars"])
        for item in packet["evidence"]:
            self.assertEqual(item["id"] in rendered, f"id={item['id']} " in packet["text"])

    def test_legacy_evidence_positionals_and_stable_id(self):
        item = Evidence("MEMORY.fixture", "group:test", "你好 世界", .9, .2, .3, .4, False, 10)
        self.assertEqual(item.rank, 10)
        self.assertIsNone(item.observed_at)
        self.assertEqual(item.flags, ())
        self.assertEqual(item.confidence, "")
        self.assertEqual(item.event_date, "")
        self.assertEqual(item.as_dict()["rank"], 10)
        self.assertEqual(item.as_dict()["id"], item.id)
        self.assertEqual(item.id, replace(item, rank=999, recency=0, observed_at=1234).id)
        self.assertNotEqual(item.id, replace(item, scope="group:other").id)
        self.assertNotEqual(item.id, replace(item, source="STYLE_EXAMPLE.fixture").id)

    def test_existing_packet_keys_and_all_positional_arguments(self):
        packet = harness.build_fadianji_evidence_packet("你好", "fadianji", "group:test", False, "u", "test", None, None, 1200, corpus_profile_enabled=False)
        old_keys = {"text", "truncated", "rendered_counts", "omitted_count", "rendered_chars", "scope", "is_private", "flags", "evidence", "scene_matches", "counts", "max_chars"}
        self.assertTrue(old_keys <= packet.keys())
        self.assertEqual(packet["schema_version"], 2)
        self.assertEqual(harness.build_fadianji_harness_context("你好", "fadianji", "group:test", False, "u", "test", None, None, 1200, corpus_profile_enabled=False), packet["text"])
        self.assert_accounting(packet)

    def test_rank_ten_memory_survives_before_rank_thirty_fact(self):
        memory = Evidence("MEMORY.fixture", "group:test", "甲" * 40, rank=10)
        fact = Evidence("FACT.fixture", "group:test", "乙" * 40, rank=30)
        self.book.return_value = [fact, memory]
        complete = self.packet()
        self.assertLess(complete["text"].index(memory.id), complete["text"].index(fact.id))
        budget = len(complete["text"]) - len(harness._render_row(fact))
        limited = self.packet(max_chars=budget)
        self.assertEqual(limited["rendered_ids"], [memory.id])
        self.assertEqual(limited["omitted_ids"], [fact.id])
        self.assertNotIn("【FACT/", limited["text"])
        self.assert_accounting(limited)

    def test_cjk_budgets_are_unicode_characters_and_whole_rows(self):
        self.book.return_value = [Evidence("MEMORY.test", "group:test", "猫咪🙂" * 21, rank=10), Evidence("FACT.test", "group:test", "漢字🌙" * 19, rank=30)]
        full = self.packet()
        complete_rows = {harness._render_row(item).rstrip("\n") for item in self.book.return_value}
        self.assertGreater(len(full["text"].encode("utf-8")), len(full["text"]))
        for budget in range(0, len(full["text"]) + 40, 17):
            with self.subTest(budget=budget):
                packet = self.packet(max_chars=budget)
                self.assert_accounting(packet)
                for line in packet["text"].splitlines():
                    if line.startswith("- ["):
                        self.assertIn(line, complete_rows)

    def test_unknown_prefix_is_other_and_not_lost(self):
        other = Evidence("CUSTOM.lookup", "group:test", "未知来源仍须显示", rank=5)
        self.book.return_value = [other]
        packet = self.packet()
        self.assertEqual(packet["counts"], {"OTHER": 1})
        self.assertEqual(packet["rendered_ids"], [other.id])
        self.assertIn("【OTHER/其他证据】", packet["text"])
        self.assertIn('src="CUSTOM.lookup"', packet["text"])
        self.assert_accounting(packet)

    def test_long_metadata_is_never_truncated_to_fit(self):
        item = Evidence("CUSTOM." + "来源" * 400, "group:test", "short text", rank=1, event_date="2030-01-02", confidence="provider_label", flags=("uncertain",))
        self.book.return_value = [item, Evidence("MEMORY.short", "group:test", "short next", rank=2)]
        packet = self.packet(max_chars=850)
        self.assertIn(item.id, packet["omitted_ids"])
        self.assertNotIn("CUSTOM.", packet["text"])
        self.assertIn("short next", packet["text"])
        self.assert_accounting(packet)

    def test_tiny_budget_does_not_render_partial_header_or_usage(self):
        self.book.return_value = [Evidence("MEMORY.test", "group:test", "note")]
        for budget in (0, 1, 20, 100):
            packet = self.packet(max_chars=budget)
            self.assertEqual(packet["text"], "")
            self.assertTrue(packet["truncated"])
            self.assert_accounting(packet)

    def test_dedupe_is_scoped_and_kind_aware(self):
        memory = Evidence("MEMORY.profile", "group:test", "SAME   text", rank=10)
        self.book.return_value = [memory, replace(memory, source="MEMORY.recall", text="same text", rank=20), replace(memory, source="STYLE_EXAMPLE.test"), replace(memory, scope="group:other")]
        packet = self.packet()
        self.assertEqual(packet["duplicate_count"], 1)
        self.assertEqual(packet["counts"], {"MEMORY": 2, "STYLE_EXAMPLE": 1})
        self.assertEqual(packet["source_status"]["book"]["collected_count"], 4)
        self.assertEqual(packet["source_status"]["book"]["count"], 3)
        self.assert_accounting(packet)

    def test_ids_and_counts_are_deterministic_across_input_order(self):
        rows = [Evidence("MEMORY.profile", "group:test", "同文", rank=10), Evidence("MEMORY.recall", "group:test", "同文", rank=20), Evidence("FACT.test", "group:test", "事实", rank=30)]
        self.book.return_value = rows
        first = self.packet(max_chars=700)
        self.book.return_value = list(reversed(rows))
        second = self.packet(max_chars=700)
        for key in ("evidence", "rendered_ids", "omitted_ids", "counts", "omitted_counts", "duplicate_count", "text"):
            self.assertEqual(first[key], second[key])

    def test_source_unavailable_empty_disabled_and_skipped(self):
        packet = self.packet(text="在吗", feed_store=SimpleNamespace(recent_feeds=lambda **_: []))
        states = {key: value["state"] for key, value in packet["source_status"].items()}
        self.assertEqual(states, {"memory": "unavailable", "corpus": "disabled", "feed": "empty", "book": "empty", "rag": "unavailable", "scene": "skipped"})
        self.assertEqual(packet["diagnostics"], [])
        self.assertIn("sources=memory:unavailable/0", packet["text"])
        self.assertIn("omitted=0", packet["text"])

    def test_disabled_sources_are_not_called_even_for_negative_k(self):
        store = SimpleNamespace(enabled=False, query=Mock(), recent_feeds=Mock(), _profile_for=Mock())
        with patch.object(harness, "_corpus_profile_evidence") as profile:
            packet = self.packet(book_k=-1, scene_k=0, memory_store=store, rag_store=store, feed_store=store, feed_max_items=0)
        self.book.assert_not_called()
        self.scenes.assert_not_called()
        profile.assert_not_called()
        store.query.assert_not_called()
        store.recent_feeds.assert_not_called()
        store._profile_for.assert_not_called()
        self.assertTrue(all(status["state"] == "disabled" for status in packet["source_status"].values()))

    def test_all_provider_exceptions_are_isolated_and_redacted(self):
        secret = "TOKEN secret-path should never enter diagnostics"
        fail = Mock(side_effect=RuntimeError(secret))
        self.book.side_effect = ValueError(secret)
        self.scenes.side_effect = OSError(secret)
        store = SimpleNamespace(_profile_for=fail, recall_notes=fail, recall=fail, query=fail, recent_feeds=fail)
        with patch.object(harness, "_corpus_profile_evidence", side_effect=LookupError(secret)):
            packet = self.packet(memory_store=store, rag_store=store, feed_store=store, corpus_profile_enabled=True, display_name="fixture")
        self.assertEqual({entry["source"] for entry in packet["diagnostics"]}, {"memory", "corpus", "feed", "book", "rag", "scene"})
        self.assertTrue(all(set(entry) == {"source", "error_type"} for entry in packet["diagnostics"]))
        self.assertTrue(all(status["state"] == "error" for status in packet["source_status"].values()))
        self.assertNotIn(secret, json.dumps(packet))
        self.assert_accounting(packet)

    def test_partial_memory_failure_preserves_profile_and_recall(self):
        notes = Mock(side_effect=TypeError("sensitive provider failure"))
        recall = Mock(return_value={"matches": [{"text": "有效召回"}]})
        memory = SimpleNamespace(_profile_for=lambda *args: {"impression": "有效画像"}, recall_notes=notes, recall=recall)
        packet = self.packet(memory_store=memory)
        self.assertEqual(packet["source_status"]["memory"]["state"], "partial")
        self.assertEqual(packet["source_status"]["memory"]["count"], 2)
        notes.assert_called_once()
        recall.assert_called_once()
        self.assertIn("有效画像", packet["text"])
        self.assertIn("有效召回", packet["text"])
        self.assertEqual(packet["diagnostics"], [{"source": "memory", "error_type": "TypeError"}])

    def test_failed_profile_does_not_prevent_notes_or_recall(self):
        memory = SimpleNamespace(_profile_for=Mock(side_effect=ValueError("private")), recall_notes=lambda **_: {"group_notes": [{"text": "剩余笔记"}]}, recall=lambda **_: {"matches": [{"text": "剩余召回"}]})
        packet = self.packet(memory_store=memory)
        self.assertEqual(packet["source_status"]["memory"]["state"], "partial")
        self.assertEqual(packet["counts"], {"MEMORY": 2})

    def test_provider_typeerror_is_not_retried(self):
        query = Mock(side_effect=TypeError("do not retry a mutated query"))
        packet = self.packet(rag_store=SimpleNamespace(query=query))
        query.assert_called_once()
        self.assertEqual(packet["source_status"]["rag"]["state"], "error")
        self.assertEqual(packet["diagnostics"], [{"source": "rag", "error_type": "TypeError"}])

    def test_signature_adapter_supports_old_keywords_without_retry(self):
        calls = []

        def old_query(scope, text, *, top_k):
            calls.append((scope, text, top_k))
            return [(0.7, "old API result", {"scope": scope})]

        packet = self.packet(rag_store=SimpleNamespace(query=old_query))
        self.assertEqual(len(calls), 1)
        self.assertEqual(packet["source_status"]["rag"]["state"], "ok")
        self.assertIn("old API result", packet["text"])

    def test_direct_call_store_propagates_typeerror(self):
        method = Mock(side_effect=TypeError("body failed"))
        with self.assertRaises(TypeError):
            harness._call_store(SimpleNamespace(method=method), "method", answer=42)
        method.assert_called_once_with(answer=42)

    def test_signature_adapter_never_drops_scope_selectors(self):
        calls = []

        def unscoped_notes(*, limit=4):
            calls.append(limit)
            return {"group_notes": [{"text": "must not be queried"}]}

        packet = self.packet(memory_store=SimpleNamespace(recall_notes=unscoped_notes))
        self.assertEqual(calls, [])
        self.assertEqual(packet["source_status"]["memory"]["state"], "error")
        self.assertIn({"source": "memory", "error_type": "TypeError"}, packet["diagnostics"])
        with self.assertRaises(TypeError):
            harness._call_store(SimpleNamespace(lookup_user_profile=lambda name: {}), "lookup_user_profile", "name", scope="private:u")

    def test_private_profile_legacy_empty_group_keyword_is_safe_to_omit(self):
        calls = []

        def profile(user_id):
            calls.append(user_id)
            return {"impression": "private fixture"}

        packet = self.packet(text="你好", scope_key="private:u", is_private=True, memory_store=SimpleNamespace(lookup_user_profile=profile))
        self.assertEqual(calls, ["u"])
        self.assertEqual(packet["source_status"]["memory"]["state"], "ok")

    def test_corpus_unqueried_and_missing_are_not_empty(self):
        with patch.object(corpus, "lookup_user_profile") as lookup, patch.object(corpus, "corpus_enabled", return_value=False) as available:
            skipped = self.packet(corpus_profile_enabled=True)
            available.assert_not_called()
            missing = self.packet(corpus_profile_enabled=True, display_name="fixture")
        lookup.assert_not_called()
        self.assertEqual(skipped["source_status"]["corpus"]["state"], "skipped")
        self.assertEqual(missing["source_status"]["corpus"]["state"], "unavailable")

    def test_shortened_notes_are_explicit_excerpts(self):
        items = harness._notes_evidence({"group_notes": [{"text": "字" * 400}, {"text": "完整"}]}, "group:test", False)
        self.assertEqual(len(items[0].text), 300)
        self.assertIn("excerpt", items[0].flags)
        self.assertNotIn("excerpt", items[1].flags)
        self.assertIn('flags=["excerpt"]', harness._render_row(items[0]))

    def test_renderer_precomputes_each_row_once(self):
        self.book.return_value = [Evidence("MEMORY.fixture", "group:test", f"row {number}", rank=number) for number in range(25)]
        with patch.object(harness, "_render_row", wraps=harness._render_row) as render:
            packet = self.packet(max_chars=10000)
        self.assertEqual(render.call_count, 25)
        self.assert_accounting(packet)

    def test_missing_store_method_is_unavailable_not_empty(self):
        packet = self.packet(rag_store=SimpleNamespace(), feed_store=SimpleNamespace())
        self.assertEqual(packet["source_status"]["rag"]["state"], "unavailable")
        self.assertEqual(packet["source_status"]["feed"]["state"], "unavailable")
        self.assertEqual(packet["diagnostics"], [])

    def test_scope_mismatch_and_partial_rag_are_observable(self):
        rows = [(0.9, "other scope", {"scope": "group:other"}), (0.8, "unscoped", {})]
        store = SimpleNamespace(query=lambda *args, **kwargs: rows)
        packet = self.packet(rag_store=store)
        self.assertEqual(packet["source_status"]["rag"]["state"], "scope_mismatch")
        self.assertEqual(packet["source_status"]["rag"]["rejected_count"], 2)
        rows.append((0.7, "current scope", {"scope": "group:test"}))
        packet = self.packet(rag_store=store)
        self.assertEqual(packet["source_status"]["rag"]["state"], "partial")
        self.assertEqual(packet["counts"], {"RAG": 1})
        self.assertNotIn("other scope", packet["text"])

    def test_note_recall_rag_feed_and_snapshot_timestamps(self):
        note_time = "2026-02-03T04:05:06Z"
        stamp = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc).timestamp()
        memory = SimpleNamespace(
            _profile_for=lambda *args: {"impression": "快照", "snapshot_at": stamp - 10},
            recall_notes=lambda **_: {"group_notes": [{"text": "计划", "time": note_time, "event_date": "2027-03-04", "confidence": "user_reported", "flags": ["uncertain"]}]},
            recall=lambda **_: {"matches": [{"text": "回忆", "time": note_time}]},
        )
        rag = SimpleNamespace(query=lambda *args, **kwargs: [(0.8, "历史", {"scope": "group:test", "ts": stamp - 20})])
        feed = SimpleNamespace(recent_feeds=lambda **_: [{"text": "动态", "feed_time": stamp - 30, "recorded_at": stamp + 99}])
        packet = self.packet(memory_store=memory, rag_store=rag, feed_store=feed)
        items = {item["source"]: item for item in packet["evidence"]}
        self.assertEqual(items["MEMORY.profile"]["observed_at"], stamp - 10)
        self.assertEqual(items["MEMORY.notes"]["observed_at"], stamp)
        self.assertEqual(items["MEMORY.recall"]["observed_at"], stamp)
        self.assertEqual(items["RAG.memory"]["observed_at"], stamp - 20)
        self.assertEqual(items["FEED.qzone"]["observed_at"], stamp - 30)
        self.assertEqual(items["MEMORY.notes"]["event_date"], "2027-03-04")
        self.assertEqual(items["MEMORY.notes"]["confidence"], "user_reported")
        self.assertEqual(items["MEMORY.notes"]["flags"], ("uncertain",))
        self.assertIn('event_date="2027-03-04"', packet["text"])
        self.assertIn(f"time={note_time}", packet["text"])

    def test_missing_or_invalid_time_is_unknown_not_measured_freshness(self):
        for value in (None, "", 0, -1, "not-a-date", float("nan"), float("inf")):
            with self.subTest(value=value):
                item = harness._notes_evidence({"user_notes": [{"text": "无日期", "time": value}]}, "private:u", True)[0]
                self.assertIsNone(item.observed_at)
                self.assertEqual(item.recency, 0)
                self.assertEqual(item.confidence, "")
                self.assertIn("time=?", harness._render_row(item))

    def test_group_recall_preserves_speaker_identity(self):
        matches = [
            {"text": "相同内容", "display_name": "同名", "user_id": "111"},
            {"text": "相同内容", "display_name": "同名", "user_id": "222"},
            {"text": "相同内容", "display_name": "同名", "user_id": "111"},
            {"text": "身份缺失"},
        ]
        memory = SimpleNamespace(_profile_for=lambda *args: {}, recall_notes=lambda **kwargs: {},
                                 recall=lambda **kwargs: {"matches": matches})
        packet = self.packet(memory_store=memory)
        rows = [item for item in packet["evidence"] if item["source"] == "MEMORY.recall"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(packet["duplicate_count"], 1)
        texts = [item["text"] for item in rows]
        self.assertTrue(any('发言者昵称="同名" UID="111"' in text for text in texts))
        self.assertTrue(any('发言者昵称="同名" UID="222"' in text for text in texts))
        self.assertTrue(any('发言者昵称="未知" UID="未知"' in text for text in texts))
        self.assert_accounting(packet)

    def test_recall_identity_is_bounded_and_private_unchanged(self):
        matches = [{"text": "字" * 400, "display_name": "长名" * 60, "user_id": "x" * 110 + suffix}
                   for suffix in ("1", "2")]
        rows = harness._recall_evidence({"matches": matches}, "group:test", False)
        self.assertNotEqual(rows[0].id, rows[1].id)
        for row in rows:
            self.assertLessEqual(len(row.text), 320)
            self.assertIn("excerpt", row.flags)
        private = harness._recall_evidence({"matches": matches[:1]}, "private:u", True)[0]
        self.assertEqual(private.text, "字" * 320)

    def test_group_profile_is_style_not_confirmed_identity(self):
        profile = {"name": "同名", "scope": "group", "triggers": 99,
                   "samples": [{"trigger": "你好", "reply": "风格样例"}]}
        with patch.object(corpus, "lookup_user_profile", return_value=profile):
            packet = self.packet(display_name="同名", corpus_profile_enabled=True)
        self.assertEqual(packet["counts"], {"STYLE_EXAMPLE": 1})
        self.assertIn("仅供口吻参考，不确认与当前发言者为同一人", packet["text"])
        self.assertNotIn("共互动", packet["text"])
        self.assertIn("style_only", packet["evidence"][0]["flags"])
        self.assert_accounting(packet)

    def test_corpus_private_requires_exact_scope_even_after_name_lookup(self):
        for actual_scope in ("", "group", "group:test", "private:other"):
            with self.subTest(scope=actual_scope):
                with patch.object(corpus, "private_profile", return_value=None), patch.object(corpus, "lookup_user_profile", return_value={"name": "同名", "scope": actual_scope, "triggers": 1}) as lookup:
                    packet = self.packet(scope_key="private:u", is_private=True, display_name="同名", corpus_profile_enabled=True)
                lookup.assert_called_once_with("同名", scope="private:u")
                self.assertEqual(packet["source_status"]["corpus"]["state"], "scope_mismatch")
                self.assertEqual(packet["evidence"], [])

    def test_corpus_group_scope_acceptance(self):
        for actual_scope, accepted in (("", True), ("group", True), ("group:test", True), ("group:other", False), ("private:u", False)):
            with self.subTest(scope=actual_scope):
                with patch.object(corpus, "lookup_user_profile", return_value={"name": "昵称", "scope": actual_scope, "triggers": 5}) as lookup:
                    packet = self.packet(display_name="昵称", corpus_profile_enabled=True)
                lookup.assert_called_once_with("昵称", scope="group:test")
                self.assertEqual(bool(packet["evidence"]), accepted)
                self.assertEqual(packet["source_status"]["corpus"]["state"], "ok" if accepted else "scope_mismatch")

    def test_corpus_stats_and_style_are_separate_and_time_is_milliseconds(self):
        profile = {"name": "旧昵称", "scope": "private:u", "triggers": 12, "last_ts": 1700000000123, "avg_reply_len": 6, "register": "casual", "samples": [{"trigger": "旧提问", "reply": "仅作风格示例"}]}
        with patch.object(corpus, "private_profile", return_value=profile):
            packet = self.packet(scope_key="private:u", is_private=True, display_name="旧昵称", corpus_profile_enabled=True, corpus_profile_max_chars=180)
        items = {item["source"]: item for item in packet["evidence"]}
        stats, style = items["MEMORY.corpus_profile"], items["STYLE_EXAMPLE.corpus_profile"]
        self.assertIn("共互动 12 次", stats["text"])
        self.assertNotIn("旧对话例", stats["text"])
        self.assertNotIn("语气档", stats["text"])
        self.assertIn("仅作风格示例", style["text"])
        self.assertEqual(stats["observed_at"], 1700000000.123)
        self.assertEqual(style["observed_at"], 1700000000.123)
        self.assertIn("historical", stats["flags"])
        self.assertIn("style_only", style["flags"])
        self.assertEqual(stats["confidence"], "")
        self.assertLessEqual(len(stats["text"]), 180)
        self.assertLessEqual(len(style["text"]), 180)
        self.assertLessEqual(sum(len(item["text"]) for item in packet["evidence"]), 180)

    def test_corpus_shared_text_cap_and_excerpt_flags(self):
        profile = {"name": "fixture", "scope": "group", "triggers": 3, "samples": [{"trigger": "触发" * 50, "reply": "回复" * 50}]}
        for budget in (1, 40, 80, 320):
            with self.subTest(budget=budget), patch.object(corpus, "lookup_user_profile", return_value=profile):
                rows = harness._corpus_profile_evidence("fixture", "group:test", False, corpus_profile_max_chars=budget)
                self.assertLessEqual(sum(len(item.text) for item in rows), budget)
                self.assertTrue(any("excerpt" in item.flags for item in rows))

    def test_corpus_helper_explicit_disabled_does_not_touch_store(self):
        with patch.object(corpus, "lookup_user_profile") as lookup, patch.object(corpus, "private_profile") as private:
            self.assertEqual(harness._corpus_profile_evidence("name", "private:u", True, corpus_profile_enabled=False), [])
            self.assertEqual(harness._corpus_profile_evidence("name", "private:u", True, corpus_profile_max_chars=0), [])
        lookup.assert_not_called()
        private.assert_not_called()

    def test_absent_or_blank_quote_keeps_packet_and_calls_unchanged(self):
        memory = AllSubstringMemory({"group:test": [{"text": "还记得上次吗", "user_id": "u", "display_name": "同学"}]})
        rag = SimpleNamespace(query=Mock(return_value=[]))
        original = self.packet(memory_store=memory, rag_store=rag)
        original_calls = list(memory.calls)
        original_rag_calls = list(rag.query.call_args_list)
        memory.calls.clear()
        rag.query.reset_mock()
        explicit = self.packet(memory_store=memory, rag_store=rag, quoted_text="")
        self.assertEqual(original, explicit)
        self.assertEqual(original_calls, memory.calls)
        self.assertEqual(original_rag_calls, rag.query.call_args_list)
        self.assertEqual(original, self.packet(memory_store=memory, rag_store=rag, quoted_text=" \n "))
        self.assert_accounting(original)

    def test_quote_and_current_recall_are_independent_and_deduped(self):
        quote = "紫薯飞船 明天出发"
        rows = [
            {"text": "紫薯飞船 明天出发", "user_id": "u1", "display_name": "同名"},
            {"text": "紫薯飞船 明天出发", "user_id": "u1", "display_name": "同名"},
            {"text": "紫薯飞船 明天出发", "user_id": "u2", "display_name": "同名"},
        ]
        memory = AllSubstringMemory({"group:test": rows})
        packet = self.packet("还记得这个吗", quoted_text=quote, memory_store=memory)
        self.assertEqual(memory.calls, [("", "test", "还记得这个吗"), ("", "test", quote)])
        self.assertEqual(memory.note_calls, [("", "test")])
        items = [item for item in packet["evidence"] if item["source"] == "MEMORY.recall"]
        self.assertEqual(len(items), 2)
        self.assertEqual(packet["duplicate_count"], 1)
        self.assertTrue(any('UID="u1"' in item["text"] for item in items))
        self.assertTrue(any('UID="u2"' in item["text"] for item in items))
        self.assertEqual(packet["flags"], harness.detect_fadianji_query_flags("还记得这个吗").as_dict())
        self.assert_accounting(packet)

    def test_short_and_image_queries_use_quote_without_inventing_flags(self):
        quote = "紫薯飞船 明天出发"
        for text in ("这是什么？", "好的", "", "[图片]"):
            with self.subTest(text=text):
                memory = AllSubstringMemory({"group:test": [{"text": quote, "user_id": "u1", "display_name": "甲"}]})
                rag = SimpleNamespace(query=Mock(side_effect=lambda scope, query, **_: [(0.8, "相关历史", {"scope": scope})] if query == quote else []))
                self.scenes.reset_mock()
                packet = self.packet(text, quoted_text=quote, memory_store=memory, rag_store=rag)
                self.assertEqual(memory.calls, [("", "test", quote)])
                self.assertEqual(memory.note_calls, [])
                rag.query.assert_called_once_with("group:test", quote, top_k=4, persona="fadianji")
                self.scenes.assert_called_once_with(quote, k=3, scope_key="group:test", is_private=False, semantic=False)
                self.assertIn("紫薯飞船", packet["text"])
                self.assertIn("相关历史", packet["text"])
                self.assertEqual(packet["flags"], harness.detect_fadianji_query_flags(text).as_dict())
                self.assert_accounting(packet)

    def test_empty_current_with_explicit_memory_flags_does_not_recall_everything(self):
        memory = AllSubstringMemory({"group:test": [{"text": "飞船的颜色是蓝色", "user_id": "u", "display_name": "甲"}]})
        packet = self.packet("", quoted_text="蓝色", memory_store=memory, query_flags=FLAGS)
        self.assertEqual(memory.calls, [("", "test", "蓝色")])
        self.assertEqual(packet["flags"], FLAGS.as_dict())
        self.assertIn("飞船的颜色是蓝色", packet["text"])

    def test_delimiter_only_reference_never_retrieves_unfiltered_recent_rows(self):
        memory = AllSubstringMemory({"group:test": [{"text": "无关的最近消息", "user_id": "u"}]})
        no_quote = self.packet("什么？", memory_store=memory)
        no_terms = self.packet("什么？", quoted_text=" ，；, ; ", memory_store=memory)
        self.assertEqual(no_quote, no_terms)
        self.assertEqual(memory.calls, [])
        packet = self.packet("，；", quoted_text="蓝色", memory_store=memory, query_flags=FLAGS)
        self.assertEqual(memory.calls, [("", "test", "蓝色")])
        self.assertNotIn("无关的最近消息", packet["text"])

    def test_identical_queries_are_not_repeated_and_results_across_queries_dedupe(self):
        text = "还记得紫薯飞船吗"
        memory = AllSubstringMemory({"group:test": [{"text": text, "user_id": "u", "display_name": "甲"}]})
        identical = self.packet(text, quoted_text=text, memory_store=memory)
        self.assertEqual(memory.calls, [("", "test", text)])
        self.assertEqual(identical["duplicate_count"], 0)
        memory.calls.clear()
        repeated = self.packet(text, quoted_text="紫薯飞船", memory_store=memory, max_chars=320)
        self.assertEqual(memory.calls, [("", "test", text), ("", "test", "紫薯飞船")])
        self.assertEqual(repeated["duplicate_count"], 1)
        self.assertEqual(len(repeated["evidence"]), 1)
        self.assert_accounting(repeated)

    def test_quote_queries_are_bounded_and_never_become_evidence(self):
        text = "还记得" + "甲" * 500
        quote = "只当检索参考" + "乙" * 500
        memory = AllSubstringMemory()
        rag = SimpleNamespace(query=Mock(return_value=[]))
        packet = self.packet(text, quoted_text=quote, memory_store=memory, rag_store=rag)
        current, reference = text[:160], quote[:240]
        self.assertEqual(memory.calls, [("", "test", current), ("", "test", reference)])
        rag.query.assert_called_once_with("group:test", current + " " + reference, top_k=4, persona="fadianji")
        self.scenes.assert_called_once_with(current + " " + reference, k=3, scope_key="group:test", is_private=False, semantic=False)
        self.book.assert_called_once_with(text, "fadianji", 3)
        self.assertEqual(packet["flags"], harness.detect_fadianji_query_flags(text).as_dict())
        self.assertEqual(packet["evidence"], [])
        self.assertNotIn(reference, json.dumps(packet, ensure_ascii=False))
        self.assert_accounting(packet)

    def test_quote_recall_errors_are_isolated_and_redacted(self):
        current, quote = "还记得旧事吗", "紫薯飞船"
        for failed, surviving in ((current, quote), (quote, current)):
            with self.subTest(failed=failed):
                memory = AllSubstringMemory({"group:test": [{"text": surviving, "user_id": "u", "display_name": "甲"}]}, failures=(failed,))
                rag = SimpleNamespace(query=Mock(return_value=[(0.8, "独立历史", {"scope": "group:test"})]))
                packet = self.packet(current, quoted_text=quote, memory_store=memory, rag_store=rag)
                self.assertEqual(packet["source_status"]["memory"]["state"], "partial")
                self.assertEqual(packet["source_status"]["memory"]["error_count"], 1)
                self.assertEqual(packet["source_status"]["rag"]["state"], "ok")
                self.assertIn(surviving, packet["text"])
                self.assertIn("独立历史", packet["text"])
                self.assertNotIn("synthetic sensitive failure", json.dumps(packet))
                self.assert_accounting(packet)
        memory = AllSubstringMemory(failures=(quote,))
        packet = self.packet("是什么？", quoted_text=quote, memory_store=memory, rag_store=rag)
        self.assertEqual(packet["source_status"]["memory"]["state"], "error")
        self.assertEqual(packet["source_status"]["rag"]["state"], "ok")
        self.assertIn("独立历史", packet["text"])

    def test_quote_rag_and_scene_failures_leave_other_sources_intact(self):
        quote = "紫薯飞船"
        memory = AllSubstringMemory({"group:test": [{"text": quote, "user_id": "u", "display_name": "甲"}]})
        self.scenes.return_value = [harness.SceneMatch(quote, "合成口吻", 2.0, "合成", "fixture", "group:test")]
        rag = SimpleNamespace(query=Mock(side_effect=RuntimeError("sensitive rag failure")))
        first = self.packet("什么？", quoted_text=quote, memory_store=memory, rag_store=rag)
        self.assertEqual(first["source_status"]["rag"]["state"], "error")
        self.assertEqual(first["source_status"]["scene"]["state"], "ok")
        self.assertEqual(first["source_status"]["memory"]["state"], "ok")
        self.assertIn("合成口吻", first["text"])
        self.assertNotIn("sensitive rag failure", json.dumps(first))
        self.scenes.side_effect = OSError("sensitive scene failure")
        rag.query.side_effect = None
        rag.query.return_value = [(0.8, "合成历史", {"scope": "group:test"})]
        second = self.packet("什么？", quoted_text=quote, memory_store=memory, rag_store=rag)
        self.assertEqual(second["source_status"]["scene"]["state"], "error")
        self.assertEqual(second["source_status"]["rag"]["state"], "ok")
        self.assertEqual(second["source_status"]["memory"]["state"], "ok")
        self.assertIn("合成历史", second["text"])
        self.assertNotIn("sensitive scene failure", json.dumps(second))
        self.assert_accounting(first)
        self.assert_accounting(second)

    def test_quote_memory_respects_two_private_users_and_group_scope(self):
        quote = "紫薯飞船"
        memory = AllSubstringMemory({
            "private:u1": [{"text": "紫薯飞船 私聊甲"}],
            "private:u2": [{"text": "紫薯飞船 私聊乙"}],
            "group:test": [{"text": "紫薯飞船 群甲", "user_id": "u1", "display_name": "同名"}, {"text": "紫薯飞船 群乙", "user_id": "u2", "display_name": "同名"}],
            "group:other": [{"text": "紫薯飞船 别群", "user_id": "u2"}],
        })
        first = self.packet("什么？", scope_key="private:u1", is_private=True, user_id="u1", quoted_text=quote, memory_store=memory)
        second = self.packet("什么？", scope_key="private:u2", is_private=True, user_id="u2", quoted_text=quote, memory_store=memory)
        group = self.packet("什么？", quoted_text=quote, memory_store=memory)
        self.assertIn("私聊甲", first["text"])
        self.assertNotIn("私聊乙", first["text"])
        self.assertIn("私聊乙", second["text"])
        self.assertNotIn("私聊甲", second["text"])
        self.assertIn('UID="u1"', group["text"])
        self.assertIn('UID="u2"', group["text"])
        self.assertNotIn("私聊", group["text"])
        self.assertNotIn("别群", group["text"])
        for packet in (first, second, group):
            self.assert_accounting(packet)

    def test_quote_scope_selector_mismatch_never_queries_memory(self):
        for scope, private, user, group in (("private:u1", True, "u2", ""), ("group:test", False, "u1", "other"), ("group:test:user:u2", False, "u1", "test")):
            with self.subTest(scope=scope):
                memory = AllSubstringMemory()
                packet = self.packet("什么？", scope_key=scope, is_private=private, user_id=user, group_id=group, quoted_text="紫薯飞船", memory_store=memory)
                self.assertEqual(packet["source_status"]["memory"]["state"], "scope_mismatch")
                self.assertEqual(memory.calls, [])
                self.assertEqual(memory.note_calls, [])
                self.assertNotIn("MEMORY", packet["counts"])

    def test_quote_in_matching_per_user_group_scope_uses_group_memory(self):
        memory = AllSubstringMemory({"group:test": [{"text": "紫薯飞船", "user_id": "u1"}]})
        packet = self.packet("什么？", scope_key="group:test:user:u1", is_private=False,
                             user_id="u1", group_id="test", quoted_text="紫薯飞船", memory_store=memory)
        self.assertEqual(packet["source_status"]["memory"]["state"], "ok")
        self.assertEqual(memory.calls, [("", "test", "紫薯飞船")])


if __name__ == "__main__":
    unittest.main()