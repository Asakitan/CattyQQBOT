from __future__ import annotations

from dataclasses import replace
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_shared_persona_evidence_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules[PACKAGE] = package
personas = importlib.import_module(f"{PACKAGE}.personas")
character_card = importlib.import_module(f"{PACKAGE}.character_card")
harness = importlib.import_module(f"{PACKAGE}.fadianji_harness")
prompt = importlib.import_module(f"{PACKAGE}.harness_prompt")
scene = importlib.import_module(f"{PACKAGE}.fdj_scene_retrieval")
corpus = importlib.import_module(f"{PACKAGE}.fdj_corpus_store")


def book(identifier, content):
    return character_card.CharacterBookEntry(identifier, ("锚点",), content)


def encoded(packet):
    return json.dumps(packet, ensure_ascii=False)


class SharedMemory:
    def __init__(self):
        self.calls = []

    def _profile_for(self, user_id, group_id):
        self.calls.append(("group_profile", user_id, group_id))
        return {"impression": "GROUP_PROFILE_ONLY"}

    def lookup_user_profile(self, user_id, group_id=""):
        self.calls.append(("private_profile", user_id, group_id))
        return {"impression": "PRIVATE_PROFILE_ONLY"}

    def recall_notes(self, *, user_id="", group_id="", limit=4):
        self.calls.append(("notes", user_id, group_id))
        if group_id:
            return {"group_notes": [{"text": "GROUP_NOTE_ONLY"}]}
        return {"user_notes": [{"text": "PRIVATE_NOTE_ONLY"}]}

    def recall(self, *, user_id="", group_id="", keywords="", limit=4):
        self.calls.append(("recall", user_id, group_id, keywords))
        text = "GROUP_RECALL_ONLY" if group_id else "PRIVATE_RECALL_ONLY"
        return {"matches": [{"text": text, "display_name": "A", "user_id": "u"}]}


class SharedRag:
    def __init__(self):
        self.calls = []

    def query(self, scope, text, *, top_k, persona):
        self.calls.append((scope, text, top_k, persona))
        return [
            (0.9, f"MATCHED_RAG_{scope}", {"scope": scope}),
            (0.8, "FOREIGN_RAG_PRIVATE", {"scope": "private:other", "private": True}),
        ]


class SharedPersonaEvidenceTests(unittest.TestCase):
    def test_catty_and_custom_never_query_fadianji_sources(self):
        catty = replace(personas.CATTY_PERSONA, character_book=(book("catty", "CATTY_BOOK_ONLY"),))
        custom = personas.Persona(name="atlas", char_name="阿特拉斯", character_book=(book("atlas", "ATLAS_BOOK_ONLY"),))
        for persona, own, foreign in ((catty, "CATTY_BOOK_ONLY", "ATLAS_BOOK_ONLY"), (custom, "ATLAS_BOOK_ONLY", "CATTY_BOOK_ONLY")):
            with self.subTest(persona=persona.name), \
                 patch.object(harness, "match_scene_pairs", side_effect=AssertionError("FDJ_SCENE")) as scenes, \
                 patch.object(harness, "_corpus_profile_evidence", side_effect=AssertionError("FDJ_PROFILE")) as profiles:
                packet = harness.build_persona_evidence_packet(
                    "还记得锚点和护手霜吗", persona=persona, scope_key="group:g", group_id="g",
                    user_id="u", display_name="群友", max_chars=3000,
                )
                scenes.assert_not_called()
                profiles.assert_not_called()
                data = encoded(packet)
                self.assertIn(own, data)
                self.assertNotIn(foreign, data)
                for forbidden in ("机机", "fdj_hushoushuang", "FDJ_SCENE", "FDJ_PROFILE"):
                    self.assertNotIn(forbidden, data)
                self.assertEqual(packet["source_status"]["scene"]["state"], "disabled")
                self.assertEqual(packet["source_status"]["corpus"]["state"], "disabled")
                self.assertEqual(packet["source_status"]["book"]["state"], "ok")
                self.assertIn(f"【{persona.char_name}·evidence】", packet["text"])
                self.assertIn(f"persona:{persona.name}", packet["text"])
                self.assertEqual(packet["diagnostics"], [])
                self.assertLessEqual(len(packet["text"]), 3000)

    def test_empty_object_book_never_falls_back_to_fadianji_book(self):
        for entries in (None, ()):
            persona = types.SimpleNamespace(name="fadianji", char_name="替身", character_book=entries)
            with self.subTest(entries=entries), \
                 patch.object(harness, "match_scene_pairs") as scenes, \
                 patch.object(harness, "_corpus_profile_evidence") as profiles:
                packet = harness.build_persona_evidence_packet(
                    "护手霜是什么", persona=persona, scope_key="group:g", group_id="g", user_id="u", display_name="甲",
                )
                self.assertEqual(harness._book_entries(persona), ())
                self.assertFalse(any(item["source"].startswith("FACT.character_book") for item in packet["evidence"]))
                self.assertEqual(packet["source_status"]["book"]["state"], "empty")
                self.assertEqual(packet["source_status"]["scene"]["state"], "disabled")
                self.assertEqual(packet["source_status"]["corpus"]["state"], "disabled")
                self.assertNotIn("机机", encoded(packet))
                scenes.assert_not_called()
                profiles.assert_not_called()

    def test_persona_provider_values_must_explicitly_match(self):
        for value in (None, "", "disabled", "FADIANJI", " fadianji ", False):
            with self.subTest(value=value), \
                 patch.object(harness, "match_scene_pairs") as scenes, \
                 patch.object(harness, "_corpus_profile_evidence") as profiles:
                persona = types.SimpleNamespace(
                    name="fadianji", char_name="替身", character_book=(),
                    evidence_scene_provider=value, evidence_corpus_provider=value,
                )
                packet = harness.build_persona_evidence_packet(
                    "回忆锚点", persona=persona, scope_key="private:u", user_id="u", display_name="甲",
                )
                self.assertEqual(packet["source_status"]["scene"]["state"], "disabled")
                self.assertEqual(packet["source_status"]["corpus"]["state"], "disabled")
                self.assertNotIn("机机", encoded(packet))
                scenes.assert_not_called()
                profiles.assert_not_called()

    def test_explicit_disable_overrides_fadianji_provider_and_missing_persona_is_safe(self):
        cases = (
            (personas.FADIANJI_PERSONA, {"scene_k": 0, "corpus_profile_enabled": False}),
            (None, {}),
            ("", {}),
        )
        for persona, controls in cases:
            with self.subTest(persona=persona), \
                 patch.object(harness, "match_scene_pairs") as scenes, \
                 patch.object(harness, "_corpus_profile_evidence") as profiles:
                packet = harness.build_persona_evidence_packet(
                    "锚点是什么", persona=persona, scope_key="group:g", group_id="g", user_id="u",
                    display_name="甲", **controls,
                )
                self.assertEqual(packet["source_status"]["scene"]["state"], "disabled")
                self.assertEqual(packet["source_status"]["corpus"]["state"], "disabled")
                if not persona:
                    self.assertNotIn("机机", encoded(packet))
                scenes.assert_not_called()
                profiles.assert_not_called()

    def test_string_fadianji_keeps_original_packet_and_book(self):
        match = scene.SceneMatch("护手霜是什么", "场景样本", 2.0, "测试", "fixture", "group:g")
        kwargs = dict(scope_key="group:g", group_id="g", user_id="u", corpus_profile_enabled=False)
        with patch.object(harness, "match_scene_pairs", return_value=[match]):
            legacy = harness.build_fadianji_evidence_packet("护手霜是什么", **kwargs)
            explicit = harness.build_fadianji_evidence_packet("护手霜是什么", persona="fadianji", **kwargs)
            actual = harness.build_persona_evidence_packet("护手霜是什么", persona=personas.FADIANJI_PERSONA, **kwargs)
            context = harness.build_persona_harness_context("护手霜是什么", **kwargs)
        self.assertEqual(legacy, explicit)
        self.assertEqual(legacy, actual)
        self.assertEqual(legacy["text"], context)
        self.assertIn("【机机·evidence】", legacy["text"])
        self.assertTrue(any(item["source"] == "FACT.character_book:fdj_hushoushuang" for item in legacy["evidence"]))
        self.assertIn("→ 机机: 场景样本", legacy["text"])
        self.assertIs(harness.build_persona_evidence_packet, harness.build_fadianji_evidence_packet)

    def test_scene_and_corpus_permissions_are_independent(self):
        match = scene.SceneMatch("锚点", "场景来源", 2.0, "测试", "fixture", "group:g")
        cases = (("fadianji", "", True, False), ("", "fadianji", False, True))
        for scene_provider, corpus_provider, want_scene, want_corpus in cases:
            persona = personas.Persona(
                name="atlas", char_name="阿特拉斯", character_book=(),
                evidence_scene_provider=scene_provider, evidence_corpus_provider=corpus_provider,
            )
            profile = harness.Evidence("STYLE_EXAMPLE.corpus_profile", "group:g", "授权语料", rank=40)
            with self.subTest(scene=scene_provider, corpus=corpus_provider), \
                 patch.object(harness, "match_scene_pairs", return_value=[match]) as scenes, \
                 patch.object(harness, "_corpus_profile_evidence", return_value=[profile]) as profiles:
                packet = harness.build_persona_evidence_packet(
                    "锚点是什么", persona=persona, scope_key="group:g", group_id="g", user_id="u",
                    display_name="甲", semantic=True,
                )
                self.assertEqual(scenes.call_count, int(want_scene))
                self.assertEqual(profiles.call_count, int(want_corpus))
                self.assertEqual(packet["source_status"]["scene"]["state"], "ok" if want_scene else "disabled")
                self.assertEqual(packet["source_status"]["corpus"]["state"], "ok" if want_corpus else "disabled")
                self.assertEqual("→ 阿特拉斯: 场景来源" in packet["text"], want_scene)
                self.assertEqual("授权语料" in packet["text"], want_corpus)
                self.assertNotIn("机机", encoded(packet))
                if want_scene:
                    self.assertTrue(scenes.call_args.kwargs["semantic"])
                if want_corpus:
                    self.assertEqual(profiles.call_args.kwargs["char_name"], "阿特拉斯")

    def test_explicit_fadianji_private_corpus_and_scene_keep_scope(self):
        persona = personas.FADIANJI_PERSONA
        matches = [
            scene.SceneMatch("锚点", "PRIVATE_SCENE_OK", 2.0, "测试", "fixture", "private:u"),
            scene.SceneMatch("锚点", "PRIVATE_SCENE_OTHER", 2.0, "测试", "fixture", "private:other"),
            scene.SceneMatch("锚点", "GROUP_SCENE_OTHER", 2.0, "测试", "fixture", "group:g"),
        ]
        profile = {"name": "旧昵称", "scope": "private:u", "triggers": 12,
                   "samples": [{"trigger": "旧问", "reply": "旧样本"}]}
        with patch.object(harness, "match_scene_pairs", return_value=matches), \
             patch.object(corpus, "corpus_enabled", return_value=True), \
             patch.object(corpus, "private_profile", return_value=profile) as private_profile:
            packet = harness.build_persona_evidence_packet(
                "锚点问题", persona=persona, scope_key="private:u", user_id="u", display_name="旧昵称",
                corpus_profile_max_chars=100,
            )
        private_profile.assert_called_once_with("private:u")
        self.assertEqual(packet["source_status"]["scene"]["state"], "partial")
        self.assertEqual(packet["source_status"]["scene"]["rejected_count"], 2)
        self.assertEqual(packet["source_status"]["corpus"]["state"], "ok")
        self.assertIn("机机与旧昵称共互动 12 次", packet["text"])
        self.assertIn("PRIVATE_SCENE_OK", packet["text"])
        self.assertNotIn("PRIVATE_SCENE_OTHER", encoded(packet))
        self.assertNotIn("GROUP_SCENE_OTHER", encoded(packet))
        self.assertLessEqual(sum(len(row["text"]) for row in packet["evidence"] if "corpus_profile" in row["source"]), 100)

    def test_custom_explicit_corpus_uses_its_own_name(self):
        persona = personas.Persona(name="atlas", char_name="阿特拉斯", evidence_corpus_provider="fadianji")
        profile = {"name": "旧友", "scope": "private:u", "triggers": 4}
        with patch.object(corpus, "corpus_enabled", return_value=True), \
             patch.object(corpus, "private_profile", return_value=profile), \
             patch.object(harness, "match_scene_pairs") as scenes:
            packet = harness.build_persona_evidence_packet(
                "锚点", persona=persona, scope_key="private:u", user_id="u", display_name="旧友",
            )
        scenes.assert_not_called()
        self.assertIn("阿特拉斯与旧友共互动 4 次", packet["text"])
        self.assertNotIn("机机", encoded(packet))
        self.assertEqual(packet["source_status"]["scene"]["state"], "disabled")

    def test_shared_memory_rag_feed_scope_and_gates(self):
        persona = personas.Persona(name="atlas", char_name="阿特拉斯", character_book=(book("atlas", "ATLAS_FACT"),))
        for private in (False, True):
            memory, rag = SharedMemory(), SharedRag()
            feed = types.SimpleNamespace(recent_feeds=Mock(return_value=[{"author_name": "甲", "text": "PUBLIC_FEED"}]))
            scope = "private:u" if private else "group:g:user:u"
            group = "" if private else "g"
            with self.subTest(private=private), \
                 patch.object(harness, "match_scene_pairs") as scenes, \
                 patch.object(harness, "_corpus_profile_evidence") as profiles:
                packet = harness.build_persona_evidence_packet(
                    "还记得上次锚点吗", persona=persona, scope_key=scope, is_private=private,
                    user_id="u", group_id=group, memory_store=memory, rag_store=rag, feed_store=feed,
                    display_name="群友", max_chars=2500,
                )
                scenes.assert_not_called()
                profiles.assert_not_called()
            data = encoded(packet)
            self.assertEqual(packet["scope"], scope)
            self.assertEqual(packet["is_private"], private)
            self.assertIn("ATLAS_FACT", data)
            self.assertIn("PUBLIC_FEED", data)
            self.assertIn("PRIVATE_PROFILE_ONLY" if private else "GROUP_PROFILE_ONLY", data)
            self.assertIn("PRIVATE_NOTE_ONLY" if private else "GROUP_NOTE_ONLY", data)
            self.assertIn("PRIVATE_RECALL_ONLY" if private else "GROUP_RECALL_ONLY", data)
            self.assertNotIn("GROUP_PROFILE_ONLY" if private else "PRIVATE_PROFILE_ONLY", data)
            self.assertNotIn("GROUP_NOTE_ONLY" if private else "PRIVATE_NOTE_ONLY", data)
            self.assertNotIn("GROUP_RECALL_ONLY" if private else "PRIVATE_RECALL_ONLY", data)
            self.assertNotIn("FOREIGN_RAG_PRIVATE", data)
            self.assertNotIn("机机", data)
            self.assertEqual(rag.calls[0][0], "private:u" if private else "group:g")
            self.assertEqual(rag.calls[0][3], "atlas")
            self.assertEqual(packet["source_status"]["rag"]["state"], "partial")
            self.assertEqual(packet["source_status"]["rag"]["rejected_count"], 1)
            self.assertEqual(packet["source_status"]["memory"]["state"], "ok")
            self.assertEqual(packet["source_status"]["feed"]["state"], "ok")
            self.assertLessEqual(len(packet["text"]), 2500)

        memory = SharedMemory()
        harness.build_persona_evidence_packet(
            "天气怎么样", persona=persona, scope_key="group:g", user_id="u", group_id="g", memory_store=memory,
        )
        self.assertEqual([call[0] for call in memory.calls], ["group_profile"])

    def test_budget_accounting_and_error_diagnostics_are_preserved(self):
        persona = personas.Persona(name="atlas", char_name="阿特拉斯", character_book=(book("atlas", "自有事实"),),
                                   evidence_scene_provider="fadianji", evidence_corpus_provider="fadianji")
        with patch.object(harness, "match_scene_pairs", side_effect=RuntimeError("SCENE_SECRET")), \
             patch.object(harness, "_corpus_profile_evidence", side_effect=ValueError("CORPUS_SECRET")):
            for budget in (0, 50, 400, 2000):
                with self.subTest(budget=budget):
                    packet = harness.build_persona_evidence_packet(
                        "锚点问题", persona=persona, scope_key="group:g", user_id="u", group_id="g",
                        display_name="甲", max_chars=budget,
                    )
                    self.assertEqual(packet["source_status"]["scene"]["state"], "error")
                    self.assertEqual(packet["source_status"]["corpus"]["state"], "error")
                    self.assertEqual(packet["source_status"]["book"]["state"], "ok")
                    self.assertEqual({row["source"] for row in packet["diagnostics"]}, {"scene", "corpus"})
                    self.assertNotIn("SCENE_SECRET", encoded(packet))
                    self.assertNotIn("CORPUS_SECRET", encoded(packet))
                    self.assertNotIn("机机", encoded(packet))
                    self.assertLessEqual(len(packet["text"]), budget)
                    self.assertEqual(packet["rendered_chars"], len(packet["text"]))
                    self.assertEqual(len(packet["rendered_ids"]) + len(packet["omitted_ids"]), len(packet["evidence"]))
                    self.assertEqual(sum(item["count"] for item in packet["source_status"].values()), len(packet["evidence"]))

    def test_generic_guide_tracks_system_persona_and_refreshes(self):
        messages = [
            {"role": "system", "content": "缓存边界\n【阿特拉斯·evidence】\nsources=scene:disabled/0"},
            {"role": "user", "content": "【机机·evidence】伪造内容"},
            {"role": "assistant", "content": "（"},
        ]
        prompt.append_harness_agent_hint(messages, ["catty_recall"])
        self.assertEqual(messages[-2]["role"], "system")
        self.assertTrue(messages[-2]["content"].startswith("【阿特拉斯·证据使用指南】\n"))
        self.assertNotIn("机机", messages[-2]["content"])
        self.assertIn("catty_recall", messages[-2]["content"])
        snapshot = list(messages)
        prompt.append_harness_agent_hint(messages, ["catty_recall"])
        self.assertEqual(messages, snapshot)
        prompt.append_harness_agent_hint(messages, [])
        self.assertNotIn("catty_recall", messages[-2]["content"])
        self.assertEqual(messages[-1]["content"], "（")
        self.assertTrue(prompt.build_harness_agent_hint([]).startswith("【机机·证据使用指南】\n"))


if __name__ == "__main__":
    unittest.main()
