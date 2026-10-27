from __future__ import annotations

import ast
import copy
import importlib
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import nonebot

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_harness_audit_fixture"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules[PACKAGE] = package
scene = importlib.import_module(f"{PACKAGE}.fdj_scene_retrieval")
harness = importlib.import_module(f"{PACKAGE}.fadianji_harness")


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        package.config = types.SimpleNamespace(catty_fdj_corpus_enabled=False, catty_fdj_corpus_max_pairs=1)
        scene.clear_retrieval_cache()
        self.addCleanup(scene.clear_retrieval_cache)

    def test_corpus_config_and_live_cache_invalidation(self):
        record = scene._record("铜铃报错", "合成答复", category="测试", source="fdj_corpus", source_scope="group")
        with patch.object(scene, "_refresh_delta_cache"), patch.object(scene, "_corpus_signature", return_value=("unchanged",)), patch.object(scene, "_delta_applied_records", return_value=()), patch.object(scene, "_corpus_records", side_effect=lambda pairs: (record,) if scene._corpus_max_pairs() else ()):
            self.assertEqual(scene._corpus_max_pairs(), 0)
            self.assertEqual(scene.match_scene_pairs("铜铃报错", scope_key="group:1"), [])
            package.config.catty_fdj_corpus_enabled = True
            self.assertEqual(scene._corpus_max_pairs(), 1)
            self.assertEqual(scene.match_scene_pairs("铜铃报错", scope_key="group:1")[0].reply, "合成答复")
            package.config.catty_fdj_corpus_enabled = False
            self.assertEqual(scene.match_scene_pairs("铜铃报错", scope_key="group:1"), [])

    def test_scene_member_scope_keeps_group_isolation(self):
        record = scene._record("测试场景", "群级样本", category="测试", source="fixture", source_scope="group:1")
        self.assertTrue(scene._allowed(record, "group:1:user:2", False))
        self.assertFalse(scene._allowed(record, "group:2:user:2", False))
        self.assertFalse(scene._allowed(record, "group:1:user:", False))
        self.assertFalse(scene._allowed(record, "group:1:user:2", True))

    def test_mismatched_memory_scope_is_rejected_without_quote(self):
        memory = Mock()
        memory.enabled = True
        result = harness.build_fadianji_evidence_packet("你好", scope_key="private:u1", is_private=True, user_id="u2", memory_store=memory, book_k=0, scene_k=0, corpus_profile_enabled=False)
        self.assertEqual(result["source_status"]["memory"]["state"], "scope_mismatch")
        memory.lookup_user_profile.assert_not_called()

    def test_member_scope_queries_group_rag_and_group_scenes(self):
        rag = Mock()
        rag.enabled = True
        rag.query.return_value = [(0.9, "群级事实", {"scope": "group:1"}), (0.9, "别群事实", {"scope": "group:2"})]
        match = scene.SceneMatch("测试问题", "群级口吻", 1.0, "测试", "fixture", "group:1")
        with patch.object(harness, "match_scene_pairs", return_value=[match]):
            result = harness.build_fadianji_evidence_packet("测试问题", scope_key="group:1:user:2", group_id="1", user_id="2", rag_store=rag, book_k=0, scene_k=1, corpus_profile_enabled=False)
        self.assertEqual(rag.query.call_args.args[0], "group:1")
        self.assertIn("群级事实", result["text"])
        self.assertNotIn("别群事实", result["text"])
        self.assertIn("群级口吻", result["text"])

    def test_character_book_uses_actual_persona_scope(self):
        entry = types.SimpleNamespace(keys=("天气",), identifier="x", content="测试事实", order=1)
        persona = types.SimpleNamespace(name="atlas", character_book=(entry,))
        self.assertEqual(harness._activate_character_book("天气", persona, 1)[0].scope, "persona:atlas")

    def test_member_scope_uses_group_timeline_and_adaptive_store(self):
        path = ROOT / "src" / "catty_qq_ai" / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_build_cognitive_turn_context_sync")
        timeline = Mock()
        timeline.list.return_value = []
        adaptive = Mock()
        adaptive.list_prompts.return_value = []
        namespace = {"Any": object, "config": types.SimpleNamespace(), "timeline_store": timeline, "adaptive_prompt_store": adaptive, "logger": Mock()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(node)], type_ignores=[])), str(path), "exec"), namespace)
        namespace[node.name](text="测试", persona=types.SimpleNamespace(name="fadianji", semantic_harness_enabled=False), scope_key="group:1:user:2", is_private=False, user_id="2", group_id="1")
        self.assertEqual(timeline.list.call_args.args[0], "group:1")
        self.assertEqual(adaptive.list_prompts.call_args.args[0], "group:1")

    def test_clear_commands_reject_extra_arguments(self):
        path = ROOT / "src" / "catty_qq_ai" / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        cases = (("_execute_meme_admin_sync", "meme_clear", "scope_lorebook_store", "clear_scope"), ("_execute_timeline_admin_sync", "timeline_clear", "timeline_store", "clear_items"), ("_execute_adaptive_admin_sync", "adaptive_clear", "adaptive_prompt_store", "clear_prompts"))
        for function, command, store_name, method in cases:
            with self.subTest(command=command):
                store = Mock()
                namespace = {"re": re, "Any": object, store_name: store}
                code = ast.Module(body=[copy.deepcopy(nodes[function])], type_ignores=[])
                exec(compile(ast.fix_missing_locations(code), str(path), "exec"), namespace)
                self.assertIn("用法", namespace[function](command, "unexpected", scope="group:1", persona="fadianji"))
                getattr(store, method).assert_not_called()
                namespace[function](command, "", scope="group:1", persona="fadianji")
                getattr(store, method).assert_called_once()

    def test_bad_override_file_is_preserved(self):
        module = importlib.import_module(f"{PACKAGE}.persona_override_store")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "persona_overrides.json"
            for content in (b"{bad", b"\xff", b"[]"):
                with self.subTest(content=content):
                    path.write_bytes(content)
                    store = module.PersonaOverrideStore(Path(directory) / "memory.json")
                    with self.assertRaises(OSError):
                        store.set("private:1", "catty")
                    with self.assertRaises(OSError):
                        store.clear("private:1")
                    self.assertEqual(path.read_bytes(), content)
            path.write_text('{"private:1":{"persona":"catty","set_at":"bad"}}', encoding="utf-8")
            store = module.PersonaOverrideStore(Path(directory) / "memory.json")
            self.assertEqual(store.get("private:1"), "catty")
            store.set("private:2", "fadianji")
            self.assertEqual(store.get("private:2"), "fadianji")

    def test_cli_accepts_member_scope(self):
        spec = importlib.util.spec_from_file_location("_audit_cli", ROOT / "scripts" / "fadianji_harness_cli.py")
        fake = types.ModuleType("catty_qq_ai.fadianji_harness")
        fake.build_fadianji_evidence_packet = Mock()
        parent = types.ModuleType("catty_qq_ai")
        parent.__path__ = []
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"catty_qq_ai": parent, "catty_qq_ai.fadianji_harness": fake}):
            spec.loader.exec_module(module)
        self.assertEqual(module._scope_arg("group:1:user:2"), "group:1:user:2")
        with self.assertRaises(Exception):
            module._scope_arg("private:1:user:2")

    def test_constant_book_entries_do_not_consume_dynamic_slots(self):
        import test_custom_personas as fixtures
        case = fixtures.CustomPersonaTests()
        case.setUp()
        try:
            book = [{"identifier": f"c{i}", "keys": [], "content": f"常驻{i}", "constant": True} for i in range(2)]
            book += [{"identifier": f"k{i}", "keys": [f"测试{i}"], "content": f"动态{i}"} for i in range(3)]
            persona = case.install(fixtures.definition(character_book=book))
            manager, _ = fixtures.render(persona, user_text="测试0 测试1 测试2")
            block = next(entry for entry in manager._entries if entry.identifier == "catty_character_book_hits").materialize()
            for i in range(3):
                self.assertIn(f"动态{i}", block["content"])
        finally:
            case.doCleanups()

    def test_neutral_schema_matches_image_runtime(self):
        from test_tool_dispatcher import _tools
        persona = types.SimpleNamespace(name="fadianji")
        for name, schema in (("catty_imagegen", _tools._IMAGEGEN_SCHEMA), ("catty_nai_director", _tools._NAI_DIRECTOR_SCHEMA)):
            description = _tools._schema_for_persona(name, schema, lazy=False, persona=persona)["function"]["description"]
            self.assertNotIn("必须由用户直接", description)
            self.assertNotIn("必须有图片", description)
            self.assertIn("无需先", description)
        hint = _tools.tools_system_hint(persona)
        self.assertNotIn("全部工具默认开放", hint)


if __name__ == "__main__":
    unittest.main()
