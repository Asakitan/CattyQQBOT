"""fdj_scene_retrieval 单元测试。"""
from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("fdj_scene_retrieval", _ROOT / "src" / "catty_qq_ai" / "fdj_scene_retrieval.py")
assert _spec is not None and _spec.loader is not None
retr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = retr
_spec.loader.exec_module(retr)

class SceneRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_root = retr._DELTA_ROOT
        self._old_manifest = retr._DELTA_MANIFEST
        self._tmp = tempfile.TemporaryDirectory()
        retr._DELTA_ROOT = Path(self._tmp.name)
        retr._DELTA_MANIFEST = retr._DELTA_ROOT / "manifest.json"
        self._write_manifest([])

    def tearDown(self) -> None:
        retr.set_scene_semantic_reranker(None)
        retr._DELTA_ROOT = self._old_root
        retr._DELTA_MANIFEST = self._old_manifest
        retr._DELTA_SIGNATURE = None
        retr._DELTA_ENTRIES = ()
        retr.clear_retrieval_cache()
        self._tmp.cleanup()

    def _write_manifest(self, active_files) -> None:
        retr._DELTA_MANIFEST.write_text(
            json.dumps({"schema_version": 1, "active_files": active_files}, ensure_ascii=False),
            encoding="utf-8",
        )
        retr._DELTA_SIGNATURE = None
        retr._DELTA_ENTRIES = ()
        retr.refresh_scene_delta()

    def _write_delta(self, records, name="delta.jsonl") -> None:
        path = retr._DELTA_ROOT / name
        path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        self._write_manifest([{"path": name}])

    def test_pairs_loaded_from_all_blocks(self) -> None:
        pairs = retr._cached_pairs(); self.assertGreater(len(pairs), 4800)
        trigger, reply, _, _ = pairs[0]; self.assertTrue(trigger); self.assertTrue(reply)

    def test_top_scene_pairs_returns_similar_trigger(self) -> None:
        matches = retr.top_scene_pairs("我去", k=5); self.assertTrue(matches); self.assertLessEqual(len(matches), 5)

    def test_existing_group_scenes_are_retrievable_and_scope_safe(self) -> None:
        queries = (
            "机机好可爱", "今天好难过", "config.json 报错说路径找不到",
            "这图你看懂了吗", "你是真的机机吗",
        )
        records = retr._cached_records()
        exact_hits = 0
        for index, query in enumerate(queries):
            with self.subTest(case=index):
                self.assertTrue(any(item.trigger == query and not item.private for item in records))
                matches = retr.match_scene_pairs(query, k=12, scope_key="group:local", semantic=False)
                self.assertTrue(matches)
                exact = any(item.trigger == query for item in matches)
                self.assertTrue(exact, query)
                exact_hits += int(exact)
                self.assertTrue(all(not item.private for item in matches))
        self.assertEqual(exact_hits, len(queries))

    def test_nickname_exact_survives_candidate_pruning_and_ranks_first(self) -> None:
        records = (
            retr._record("机机晴天", "原样回复", category="测试", source="fixture", source_scope="group"),
            *(retr._record(f"晴天x{index}", f"相似回复{index}", category="测试", source="fixture", source_scope="group") for index in range(4)),
            retr._record("完全不同", "无关回复", category="测试", source="fixture", source_scope="group"),
        )
        with patch.object(retr, "_cached_records", return_value=records), patch.object(retr, "_corpus_signature", return_value=("fixture",)):
            retr.clear_retrieval_cache()
            word_index, _, exact_index = retr._candidate_index()
            self.assertNotIn(0, word_index.get("晴天", ()))
            self.assertEqual(exact_index["晴天"], [0])
            self.assertEqual(len(set(word_index["晴天"]) | set(exact_index["晴天"])), 5)
            matches = retr.match_scene_pairs("小机晴天", k=5, scope_key="group:1")
            self.assertEqual(matches[0].trigger, "机机晴天")
            self.assertEqual(matches[0].reply, "原样回复")
            self.assertAlmostEqual(matches[0].lexical_score, 3.55)
            self.assertEqual([item.score for item in matches], sorted((item.score for item in matches), reverse=True))
            self.assertEqual(matches, retr.match_scene_pairs("晴天", k=5, scope_key="group:1"))

    def test_short_nickname_exact_passes_threshold_and_empty_aliases_do_not_match(self) -> None:
        records = tuple(
            retr._record(trigger, f"回复{index}", category="测试", source="fixture", source_scope="group")
            for index, trigger in enumerate(("机机好", "小机好", "发电机好", "不稳定发电机好", "机机"))
        )
        with patch.object(retr, "_cached_records", return_value=records), patch.object(retr, "_corpus_signature", return_value=("fixture",)):
            retr.clear_retrieval_cache()
            _, _, exact_index = retr._candidate_index()
            self.assertNotIn("", exact_index)
            matches = retr.match_scene_pairs("小机好", k=5, scope_key="group:1")
            self.assertEqual({item.trigger for item in matches}, {"机机好", "小机好", "发电机好", "不稳定发电机好"})
            for item in matches:
                self.assertAlmostEqual(item.lexical_score, 1.55)
                self.assertGreater(item.lexical_score, retr._MIN_LEXICAL_SCORE)
            for nickname in ("机机", "小机", "发电机", "不稳定发电机", "机机机机"):
                with self.subTest(nickname=nickname):
                    self.assertEqual(retr.match_scene_pairs(nickname, k=5, scope_key="group:1"), [])

    def test_exact_same_text_keeps_scope_isolation_and_private_bonus_order(self) -> None:
        records = tuple(
            retr._record("机机约好了", reply, category="测试", source="fixture", source_scope=scope)
            for scope, reply in (
                ("group", "公共回复"),
                ("group:1", "一群回复"),
                ("group:2", "二群回复"),
                ("private:3670608232", "专属私聊回复"),
            )
        )
        with patch.object(retr, "_cached_records", return_value=records), patch.object(retr, "_corpus_signature", return_value=("fixture",)):
            retr.clear_retrieval_cache()
            first = retr.match_scene_pairs("小机约好了", k=8, scope_key="qq_group_1")
            second = retr.match_scene_pairs("小机约好了", k=8, scope_key="group:2")
            private = retr.match_scene_pairs("小机约好了", k=8, scope_key="qq_private_3670608232", is_private=True)
            other_private = retr.match_scene_pairs("小机约好了", k=8, scope_key="private:999", is_private=True)
            self.assertEqual({item.reply for item in first}, {"公共回复", "一群回复"})
            self.assertEqual({item.reply for item in second}, {"公共回复", "二群回复"})
            self.assertEqual({item.reply for item in private}, {"公共回复", "专属私聊回复"})
            self.assertEqual({item.reply for item in other_private}, {"公共回复"})
            self.assertEqual(private[0].reply, "专属私聊回复")
            self.assertAlmostEqual(private[0].scope_bonus, 0.18)
            self.assertGreater(private[0].score, private[1].score)

    def test_implicit_category_requires_overlap_but_explicit_category_is_preserved(self) -> None:
        fish = next(
            record for _, record in retr._base_records()
            if record.trigger == "可以选择变章鱼" and record.category == "深夜 emo" and record.source_scope == "group"
        )
        overlap = retr._record("今晚晚安", "稍有重叠", category="深夜 emo", source="fixture", source_scope="group")
        records = (fish, overlap)
        self.assertFalse(retr._bigrams("失眠") & fish.trigger_bigrams)
        self.assertFalse(frozenset(retr._WORD_RE.findall("失眠")) & fish.trigger_words)
        self.assertGreaterEqual(0.55 + 0.05, retr._MIN_LEXICAL_SCORE)
        with patch.object(retr, "_cached_records", return_value=records), patch.object(retr, "_corpus_signature", return_value=("fixture",)):
            retr.clear_retrieval_cache()
            self.assertEqual(retr.match_scene_pairs("失眠", k=5), [])
            explicit = retr.match_scene_pairs("失眠", k=5, category="深夜 emo")
            self.assertIn(fish.trigger, {item.trigger for item in explicit})
            explicit_without_hint = retr.match_scene_pairs("随机", k=5, category="深夜 emo")
            self.assertIn(fish.trigger, {item.trigger for item in explicit_without_hint})
            partial = retr.match_scene_pairs("今晚失眠", k=5)
            self.assertEqual([item.reply for item in partial], ["稍有重叠"])
            self.assertEqual(retr.match_scene_pairs(fish.trigger, k=5)[0].reply, fish.reply)

    def test_exact_index_and_negative_cache_refresh_when_delta_file_changes(self) -> None:
        target = {
            "op": "upsert", "status": "approved", "record_id": "exact-refresh",
            "source_scope": "group:1", "query_text": "机机晴天", "reply": "旧增量回复", "category": "测试",
        }
        decoys = [
            {"op": "upsert", "status": "approved", "record_id": f"decoy-{index}",
             "source_scope": "group:1", "query_text": f"晴天x{index}", "reply": f"相似回复{index}", "category": "测试"}
            for index in range(4)
        ]
        unrelated = {
            "op": "upsert", "status": "approved", "record_id": "unrelated",
            "source_scope": "group:1", "query_text": "完全不同", "reply": "无关回复", "category": "测试",
        }
        with patch.object(retr, "_base_records", return_value=[]), patch.object(retr, "_corpus_records", return_value=()), patch.object(retr, "_corpus_signature", return_value=("fixture",)):
            self._write_delta([target, *decoys, unrelated])
            before = retr.match_scene_pairs("小机晴天", k=5, scope_key="group:1")
            self.assertEqual(before[0].reply, "旧增量回复")
            self.assertEqual(retr.match_scene_pairs("小机星光", k=5, scope_key="group:1"), [])
            old_signature = retr._DELTA_SIGNATURE
            updated = dict(target, query_text="不稳定发电机星光", reply="新的增量回复已更新")
            (retr._DELTA_ROOT / "delta.jsonl").write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in (updated, *decoys, unrelated)),
                encoding="utf-8",
            )
            after = retr.match_scene_pairs("小机星光", k=5, scope_key="group:1")
            self.assertNotEqual(retr._DELTA_SIGNATURE, old_signature)
            self.assertEqual(after[0].trigger, "不稳定发电机星光")
            self.assertEqual(after[0].reply, "新的增量回复已更新")
            self.assertFalse(any(item.reply == "旧增量回复" for item in retr.match_scene_pairs("小机晴天", k=5, scope_key="group:1")))
            _, _, exact_index = retr._candidate_index()
            self.assertNotIn("晴天", exact_index)
            self.assertIn("星光", exact_index)

    def test_same_reply_deduplicated(self) -> None:
        matches = retr.top_scene_pairs("什么意思", k=10); replies = [reply for _, reply, _ in matches]; self.assertEqual(len(replies), len(set(replies)))

    def test_build_block_shape(self) -> None:
        block = retr.build_scene_reference_block("老板？", k=3); self.assertIn("STYLE_EXAMPLE", block); self.assertIn("历史: ", block); self.assertIn("机机: ", block)

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(retr.top_scene_pairs(""), []); self.assertEqual(retr.build_scene_reference_block(""), "")

    def test_no_banned_words_injected(self) -> None:
        for trigger, reply, _ in retr.top_scene_pairs("今天怎么样", k=20):
            for banned in ("喵", "笨猫", "猫娘", "主人", "无名老师"): self.assertNotIn(banned, trigger + reply)

    def test_metadata_contains_category_and_scope(self) -> None:
        matches = retr.match_scene_pairs("配置文件报错了", k=5, semantic=False); self.assertTrue(matches); self.assertTrue(all(item.category and item.source_scope == "group" for item in matches))

    def test_group_scope_never_returns_private_examples(self) -> None:
        record = next(item for item in retr._cached_records() if item.private)
        matches = retr.match_scene_pairs(record.trigger, k=100, scope_key="group:922298923", semantic=False)
        self.assertTrue(all(not item.private for item in matches))

    def test_special_private_scope_prefers_private_examples(self) -> None:
        record = next(item for item in retr._cached_records() if item.private)
        matches = retr.match_scene_pairs(record.trigger, k=100, scope_key="private:3670608232", is_private=True, semantic=False)
        self.assertTrue(any(item.private for item in matches)); self.assertGreaterEqual(next(item for item in matches if item.private).scope_bonus, 0.18)

    def test_other_private_scope_uses_group_examples_only(self) -> None:
        record = next(item for item in retr._cached_records() if item.private)
        matches = retr.match_scene_pairs(record.trigger, k=100, scope_key="private:9999999999", is_private=True, semantic=False)
        self.assertTrue(all(not item.private for item in matches))

    def test_semantic_without_explicit_reranker_is_lexical_only(self) -> None:
        retr.set_scene_semantic_reranker(None)
        lexical = retr.match_scene_pairs("我去", k=8, semantic=False)
        semantic_flag = retr.match_scene_pairs("我去", k=8, semantic=True)
        self.assertEqual(
            [(x.trigger, x.reply, round(x.score, 6)) for x in lexical],
            [(x.trigger, x.reply, round(x.score, 6)) for x in semantic_flag],
        )

    def test_semantic_failure_falls_back_deterministically(self) -> None:
        retr.clear_retrieval_cache(); lexical = retr.match_scene_pairs("我去", k=8, semantic=False)
        retr.set_scene_semantic_reranker(lambda _query, _items: (_ for _ in ()).throw(RuntimeError("offline")))
        fallback = retr.match_scene_pairs("我去", k=8, semantic=True)
        self.assertEqual([(x.trigger, x.reply, round(x.score, 6)) for x in lexical], [(x.trigger, x.reply, round(x.score, 6)) for x in fallback]); retr.set_scene_semantic_reranker(None)

    def test_explicit_semantic_reranker_is_used(self) -> None:
        retr.set_scene_semantic_reranker(lambda _query, items: {index: 1.0 for index, _ in enumerate(items)})
        matches = retr.match_scene_pairs("我去", k=3, semantic=True)
        self.assertTrue(matches)
        self.assertTrue(all(item.semantic_score == 1.0 for item in matches))

    def test_registered_reranker_is_opt_in_for_all_public_entrypoints(self) -> None:
        calls: list[str] = []

        def reranker(query, items):
            calls.append(query)
            return {index: 1.0 for index, _ in enumerate(items)}

        retr.set_scene_semantic_reranker(reranker)
        self.assertTrue(retr.match_scene_pairs("我去", k=3))
        self.assertTrue(retr.top_scene_pairs("我去", k=3))
        self.assertTrue(retr.build_scene_reference_block("我去", k=3))
        self.assertEqual(calls, [])
        explicit = retr.match_scene_pairs("我去", k=3, semantic=True)
        self.assertTrue(calls)
        self.assertTrue(all(item.semantic_score == 1.0 for item in explicit))

    def test_approved_delta_upsert_pending_ignore_and_private_isolation(self) -> None:
        records = [
            {"op": "upsert", "status": "pending", "record_id": "pending", "source_scope": "group:922298923", "query_text": "待审核专用触发", "reply": "待审核回复", "category": "测试"},
            {"op": "upsert", "status": "approved", "record_id": "group-ok", "source_scope": "group:922298923", "query_text": "钛合金狐狸报错", "reply": "增量群回复", "category": "技术/求助"},
            {"op": "upsert", "status": "approved", "record_id": "private-ok", "source_scope": "private:3670608232", "query_text": "私聊钛合金狐狸", "reply": "增量私聊回复", "category": "私聊"},
        ]
        self._write_delta(records)
        group = retr.match_scene_pairs("钛合金狐狸报错", k=10, scope_key="group:922298923", semantic=False)
        self.assertTrue(any(item.reply == "增量群回复" for item in group))
        self.assertFalse(any(item.reply in {"待审核回复", "增量私聊回复"} for item in group))
        private = retr.match_scene_pairs("私聊钛合金狐狸", k=10, scope_key="private:3670608232", is_private=True, semantic=False)
        self.assertTrue(any(item.reply == "增量私聊回复" for item in private))
        other_private = retr.match_scene_pairs("私聊钛合金狐狸", k=10, scope_key="private:999", is_private=True, semantic=False)
        self.assertFalse(any(item.reply == "增量私聊回复" for item in other_private))

    def test_delta_delete_and_bad_manifest_keep_last_good(self) -> None:
        base = next(item for item in retr._base_records() if item[1].source == "fadianji_seed")
        base_uid, base_record = base
        self._write_delta([
            {"op": "delete", "status": "approved", "record_id": base_uid},
            {"op": "upsert", "status": "approved", "record_id": "last-good", "source_scope": "group:922298923", "query_text": "last good 狐狸", "reply": "last good reply", "category": "测试"},
        ])
        self.assertFalse(any(item.trigger == base_record.trigger and item.reply == base_record.reply and item.source == base_record.source for item in retr._cached_records()))
        self.assertTrue(any(item.reply == "last good reply" for item in retr.match_scene_pairs("last good 狐狸", k=10, scope_key="group:922298923", semantic=False)))
        retr._DELTA_MANIFEST.write_text("{broken", encoding="utf-8")
        retr.refresh_scene_delta()
        self.assertTrue(any(item.reply == "last good reply" for item in retr.match_scene_pairs("last good 狐狸", k=10, scope_key="group:922298923", semantic=False)))

    def test_category_diversity_prefers_distinct_categories(self) -> None:
        items = [
            retr.SceneMatch("a", "r1", 3.0, "A", "s", "group"),
            retr.SceneMatch("b", "r2", 2.9, "A", "s", "group"),
            retr.SceneMatch("c", "r3", 2.8, "B", "s", "group"),
        ]
        selected = retr._select_diverse(items, 2)
        self.assertEqual([item.category for item in selected], ["A", "B"])

    def test_static_group_scene_bank_keeps_private_corpus_isolated(self) -> None:
        source = (_ROOT / "src" / "catty_qq_ai" / "personas" / "fadianji.py").read_text(encoding="utf-8")
        prompt_manager_source = (_ROOT / "src" / "catty_qq_ai" / "prompt_manager.py").read_text(encoding="utf-8")
        scenes_source = (_ROOT / "src" / "catty_qq_ai" / "personas" / "fadianji_scenes.py").read_text(encoding="utf-8")
        self.assertNotIn("+ FADIANJI_GROUP_SCENE_EXAMPLES", source)
        self.assertNotIn("+ FADIANJI_GROUP_SCENE_EXAMPLES_EXT", source)
        self.assertNotIn("+ FADIANJI_PRIVATE_SCENE_EXAMPLES", source)
        self.assertIn("cache_full_character_book=True", source)
        self.assertIn("build_fadianji_group_scene_bank", prompt_manager_source)
        self.assertIn("FADIANJI_GROUP_SCENE_EXAMPLES_EXT.strip()", scenes_source)
        self.assertNotIn("FADIANJI_PRIVATE_SCENE_EXAMPLES.strip()", scenes_source)

    def test_static_group_scene_bank_trims_only_at_paragraph_boundaries(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "_scene_bank_boundary_fixture",
            _ROOT / "src" / "catty_qq_ai" / "personas" / "fadianji_scenes.py",
        )
        assert spec is not None and spec.loader is not None
        scenes = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scenes)
        build_fadianji_group_scene_bank = scenes.build_fadianji_group_scene_bank

        self.assertEqual(build_fadianji_group_scene_bank(32), "")
        clipped = build_fadianji_group_scene_bank(500)
        self.assertTrue(clipped)
        self.assertLessEqual(len(clipped), 500)
        self.assertFalse(clipped.endswith("群友:"))
        self.assertFalse(clipped.endswith("机机:"))

if __name__ == "__main__": unittest.main()