from __future__ import annotations
import importlib.util
import json
import subprocess
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parent.parent; SRC = ROOT / "src"
_PACKAGE_NAME = "catty_fadianji_harness_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(SRC / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)

def _load_module(name: str):
    path = SRC / "catty_qq_ai" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

_load_module("fdj_scene_retrieval")
_harness = _load_module("fadianji_harness")
build_fadianji_evidence_packet = _harness.build_fadianji_evidence_packet
build_fadianji_harness_context = _harness.build_fadianji_harness_context
detect_fadianji_query_flags = _harness.detect_fadianji_query_flags

class FakeMemory:
    def __init__(self): self.calls = []
    def _profile_for(self, user_id, group_id): self.calls.append("group_profile"); return {"impression": "群内熟人", "interests": ["鸣潮"]}
    def lookup_user_profile(self, user_id, group_id=""): self.calls.append("private_profile"); return {"impression": "PRIVATE_SECRET"}
    def recall_notes(self, *, user_id="", group_id="", limit=10):
        if group_id: self.calls.append("group_notes"); return {"group_notes": [{"text": "群里约定周一打派"}]}
        self.calls.append("private_notes"); return {"user_notes": [{"text": "PRIVATE_NOTE"}]}
    def recall(self, *, user_id="", group_id="", keywords="", limit=8):
        if group_id: self.calls.append("group_recall"); return {"long_term_summary": "群里常聊游戏", "matches": [{"text": "群里最近聊过鸣潮"}]}
        self.calls.append("private_recall"); return {"long_term_summary": "PRIVATE_SUMMARY", "matches": [{"text": "PRIVATE_RECALL"}]}

class FakeRag:
    def query(self, scope, text, *, top_k=3, persona=None): return [(0.91, "RAG_FACT", {"scope": scope, "role": "memory_summary", "ts": 0}), (0.88, "PRIVATE_RAG", {"scope": "private:3670608232", "role": "memory_summary", "ts": 0})]

class UnscopedRag:
    def query(self, scope, text, *, top_k=3, persona=None): return [(0.91, "UNSCOPED_RAG", {"role": "memory_summary", "ts": 0})]

class FakeFeedStore:
    def __init__(self, feeds=None): self._feeds = list(feeds or []); self.calls = []
    def recent_feeds(self, *, limit=5, max_chars_each=120):
        self.calls.append(("recent_feeds", limit))
        return [dict(item, text=str(item.get("text", ""))[:max_chars_each]) for item in self._feeds[:limit]]

_FEED_ROWS = [
    {"feed_id": "t1", "author_uin": "10001", "author_name": "小A", "text": "今天去看了海，风好大但是好舒服", "like_count": 9, "liked_by": [], "feed_time": 0, "recorded_at": 0, "source": "qzone"},
    {"feed_id": "t2", "author_uin": "10002", "author_name": "阿机", "text": "新学的菜翻车了呜呜", "like_count": 2, "liked_by": [], "feed_time": 0, "recorded_at": 0, "source": "qzone"},
]

class FadianjiHarnessTests(unittest.TestCase):
    def test_query_flags(self):
        flags = detect_fadianji_query_flags("还记得上次那个报错怎么修吗"); self.assertEqual(flags.intent, "help"); self.assertTrue(flags.memory_need); self.assertTrue(flags.anaphora)
    def test_evidence_ordering_and_conflict_guidance(self):
        persona = SimpleNamespace(name="fadianji", evidence_scene_provider="fadianji", character_book=(SimpleNamespace(identifier="book_test", keys=("报错",), content="角色知识", order=100),))
        packet = build_fadianji_evidence_packet("还记得上次报错吗", persona=persona, scope_key="group:922298923", group_id="922298923", user_id="42", memory_store=FakeMemory(), rag_store=FakeRag(), semantic=False)
        sources = [item["source"] for item in packet["evidence"]]
        self.assertLess(sources.index("MEMORY.profile"), sources.index("FACT.character_book:book_test"))
        self.assertLess(sources.index("FACT.character_book:book_test"), next(i for i, source in enumerate(sources) if source.startswith("STYLE_EXAMPLE.")))
        self.assertIn("【RAG/历史事实】", packet["text"])
        self.assertIn("RAG_FACT", packet["text"])
        self.assertIn("【STYLE_EXAMPLE/口吻母本】", packet["text"])
        self.assertIn("STYLE_EXAMPLE 只学口吻和长度，不当事实", packet["text"])
        self.assertLessEqual(len(packet["scene_matches"]), 3)
        self.assertEqual(packet["schema_version"], 2)
        self.assertLess(packet["text"].index("【MEMORY/"), packet["text"].index("【FACT/"))
        self.assertEqual(packet["rendered_chars"], len(packet["text"]))
        self.assertEqual(len(packet["rendered_ids"]) + len(packet["omitted_ids"]), len(packet["evidence"]))
    def test_private_evidence_never_leaks_to_group(self):
        memory = FakeMemory(); context = build_fadianji_harness_context("还记得上次吗", scope_key="group:922298923", group_id="922298923", user_id="42", memory_store=memory, rag_store=FakeRag(), semantic=False)
        self.assertNotIn("PRIVATE_", context); self.assertNotIn("private_profile", memory.calls); self.assertNotIn("private_notes", memory.calls); self.assertNotIn("private_recall", memory.calls)
    def test_rag_requires_exact_scope_metadata(self):
        context = build_fadianji_harness_context("还记得上次吗", scope_key="group:922298923", group_id="922298923", user_id="42", rag_store=UnscopedRag(), semantic=False)
        self.assertNotIn("UNSCOPED_RAG", context)
    def test_profile_is_compared_even_without_memory_keywords(self):
        memory = FakeMemory(); context = build_fadianji_harness_context("今天一起打派吗", scope_key="group:922298923", group_id="922298923", user_id="42", memory_store=memory, semantic=False)
        self.assertIn("群内熟人", context); self.assertIn("group_profile", memory.calls); self.assertNotIn("group_notes", memory.calls); self.assertNotIn("group_recall", memory.calls)
    def test_special_private_scope_can_use_private_memory_and_rag(self):
        packet = build_fadianji_evidence_packet("还记得上次吗", scope_key="private:3670608232", is_private=True, user_id="3670608232", memory_store=FakeMemory(), rag_store=FakeRag(), semantic=False)
        self.assertIn("PRIVATE_SECRET", packet["text"]); self.assertIn("PRIVATE_NOTE", packet["text"]); self.assertIn("PRIVATE_RAG", packet["text"])
    def test_explicit_group_scope_overrides_private_flag(self):
        memory = FakeMemory(); packet = build_fadianji_evidence_packet("还记得上次吗", scope_key="group:922298923", is_private=True, group_id="922298923", user_id="42", memory_store=memory, rag_store=FakeRag(), semantic=False)
        self.assertEqual(packet["scope"], "group:922298923"); self.assertFalse(packet["is_private"]); self.assertNotIn("PRIVATE_", packet["text"]); self.assertNotIn("private_profile", memory.calls)
    def test_explicit_private_scope_overrides_group_flag(self):
        memory = FakeMemory(); packet = build_fadianji_evidence_packet("还记得上次吗", scope_key="private:3670608232", is_private=False, group_id="922298923", user_id="3670608232", memory_store=memory, rag_store=FakeRag(), semantic=False)
        self.assertEqual(packet["scope"], "private:3670608232"); self.assertTrue(packet["is_private"]); self.assertIn("PRIVATE_SECRET", packet["text"])
    def test_missing_scope_is_derived_from_mode_and_ids(self):
        private_packet = build_fadianji_evidence_packet("在吗", scope_key="", is_private=True, user_id="3670608232", memory_store=FakeMemory(), semantic=False)
        group_packet = build_fadianji_evidence_packet("在吗", scope_key="", is_private=False, group_id="922298923", user_id="42", memory_store=FakeMemory(), semantic=False)
        self.assertEqual((private_packet["scope"], private_packet["is_private"]), ("private:3670608232", True)); self.assertEqual((group_packet["scope"], group_packet["is_private"]), ("group:922298923", False))
    def test_short_greeting_skips_scene_examples(self):
        packet = build_fadianji_evidence_packet("在吗", scope_key="group:922298923", group_id="922298923", user_id="42", semantic=False)
        self.assertEqual(packet["scene_matches"], [])
        self.assertNotIn("STYLE_EXAMPLE/口吻母本", packet["text"])
    def test_packet_is_capped(self):
        context = build_fadianji_harness_context("还记得上次那个报错吗", scope_key="group:922298923", group_id="922298923", user_id="42", memory_store=FakeMemory(), rag_store=FakeRag(), max_chars=240, semantic=False); self.assertLessEqual(len(context), 240)
    def test_feed_evidence_rendered_with_group_header(self):
        packet = build_fadianji_evidence_packet("最近有什么瓜", scope_key="group:922298923", group_id="922298923", user_id="42", feed_store=FakeFeedStore(_FEED_ROWS), semantic=False)
        self.assertIn("【FEED/空间动态见闻】", packet["text"])
        self.assertIn("小A 发了动态: 今天去看了海，风好大但是好舒服（9 赞）", packet["text"])
        sources = [item["source"] for item in packet["evidence"]]
        self.assertIn("FEED.qzone", sources)
        self.assertEqual(packet["counts"].get("FEED"), 2)
    def test_feed_absent_when_store_none(self):
        packet = build_fadianji_evidence_packet("最近有什么瓜", scope_key="group:922298923", group_id="922298923", user_id="42", feed_store=None, semantic=False)
        self.assertNotIn("FEED", packet["text"])
        self.assertEqual(packet["source_status"]["feed"]["state"], "unavailable")
    def test_feed_max_items_limits_entries(self):
        store = FakeFeedStore(_FEED_ROWS)
        packet = build_fadianji_evidence_packet("最近有什么瓜", scope_key="group:922298923", group_id="922298923", user_id="42", feed_store=store, feed_max_items=1, semantic=False)
        self.assertEqual(store.calls[-1], ("recent_feeds", 1))
        feed_sources = [item["source"] for item in packet["evidence"] if item["source"].startswith("FEED.")]
        self.assertEqual(len(feed_sources), 1)
    def test_feed_visible_in_private_scope(self):
        packet = build_fadianji_evidence_packet("最近有什么瓜", scope_key="private:3670608232", is_private=True, user_id="3670608232", feed_store=FakeFeedStore(_FEED_ROWS), semantic=False)
        self.assertIn("【FEED/空间动态见闻】", packet["text"])

    def test_quote_retrieves_synthetic_scene_and_rag_without_book_activation(self):
        scene = sys.modules[f"{_PACKAGE_NAME}.fdj_scene_retrieval"]
        quote = "秘密手册机关怎么开"
        persona = SimpleNamespace(name="fadianji", evidence_scene_provider="fadianji", character_book=(SimpleNamespace(identifier="book_only", keys=("秘密手册",), content="不应激活的设定", order=100),))
        fixtures = (("group:g1", "群甲风格"), ("group:g2", "群乙风格"), ("private:u1", "私甲风格"), ("private:u2", "私乙风格"))
        records = tuple(scene._record(quote, reply, category="合成", source="fixture", source_scope=scope) for scope, reply in fixtures)

        class SyntheticRag:
            def query(self, scope, text, *, top_k, persona):
                if text != quote:
                    return []
                return [(0.9, f"当前历史 {scope}", {"scope": scope}), (0.8, "异域历史", {"scope": "private:other"})]

        with patch.object(scene, "_refresh_delta_cache"), patch.object(scene, "_corpus_signature", return_value=None), patch.object(scene, "_cached_records", return_value=records), patch.object(scene, "_CANDIDATE_INDEX_CACHE", {}), patch.object(scene, "_MATCH_CACHE", OrderedDict()):
            for scope, expected in fixtures:
                with self.subTest(scope=scope):
                    private = scope.startswith("private:")
                    user = scope.split(":", 1)[1] if private else "u1"
                    group = "" if private else scope.split(":", 1)[1]
                    packet = build_fadianji_evidence_packet("这是什么？", persona=persona, scope_key=scope, is_private=private, user_id=user, group_id=group, quoted_text=quote, rag_store=SyntheticRag(), corpus_profile_enabled=False)
                    self.assertEqual(packet["flags"], detect_fadianji_query_flags("这是什么？").as_dict())
                    self.assertEqual([match["reply"] for match in packet["scene_matches"]], [expected])
                    self.assertEqual(packet["scene_matches"][0]["source_scope"], scope)
                    self.assertIn(f"当前历史 {scope}", packet["text"])
                    self.assertIn(expected, packet["text"])
                    self.assertNotIn("异域历史", packet["text"])
                    self.assertNotIn("不应激活的设定", packet["text"])
                    self.assertFalse(any(item["source"].startswith("FACT.character_book") for item in packet["evidence"]))
                    self.assertEqual(packet["source_status"]["rag"]["state"], "partial")
                    for other_scope, other_reply in fixtures:
                        if other_scope != scope:
                            self.assertNotIn(other_reply, packet["text"])

    def test_standalone_cli_runs_without_plugin_initialization(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "fadianji_harness_cli.py"), "--text", "护手霜是什么梗", "--scope", "group:922298923", "--max-chars", "400", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["scope"], "group:922298923")
        self.assertIn("fdj_hushoushuang", result.stdout)

if __name__ == "__main__": unittest.main()