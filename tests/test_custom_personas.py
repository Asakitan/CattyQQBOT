"""Offline registry/rendering tests; never import the live NoneBot plugin."""
from __future__ import annotations

import ast
from dataclasses import fields
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "catty_custom_personas_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(PACKAGE, package)
nonebot = types.ModuleType("nonebot")
nonebot.logger = types.SimpleNamespace(**{
    name: lambda *args, **kwargs: None
    for name in ("debug", "info", "warning", "error", "exception")
})
sys.modules.setdefault("nonebot", nonebot)

personas = importlib.import_module(f"{PACKAGE}.personas")
store = importlib.import_module(f"{PACKAGE}.persona_store")
prompt_manager = importlib.import_module(f"{PACKAGE}.prompt_manager")
character_card = importlib.import_module(f"{PACKAGE}.character_card")


def definition(name="atlas", **overrides):
    value = {
        "schema_version": 1, "name": name, "char_name": f"向导{name}",
        "core_persona": "你是一位喜欢探索的向导，按自己设定自然聊天。",
    }
    value.update(overrides)
    return value


def render(persona, *, group=True, **extra):
    manager = prompt_manager.PromptManager()
    context = {
        "config": types.SimpleNamespace(catty_fadianji_static_scene_bank_enabled=False),
        "persona": persona, "scope": "group:17" if group else "private:8",
        "is_group": group, "is_owner": False, "affection_level": 5,
        "user_text": "尾巴、猫粮、欧泊、弦化与钢板是什么？",
        "no_reply_marker": "[NO_REPLY]", "reply_split_marker": "<<<CATTY_REPLY_SPLIT>>>",
    }
    context.update(extra)
    prompt_manager.register_catty_persona(manager, context)
    # Avoid tiktoken downloads: all production content functions still execute.
    world_info = importlib.import_module(f"{PACKAGE}.world_info")
    original_find = world_info.find_triggered_entries
    def deterministic_find(*args, **kwargs):
        return original_find(*args, **{**kwargs, "now": 1700000000.0, "rng_seed": 0})
    with patch.object(prompt_manager, "estimate_tokens", side_effect=len), \
         patch.dict(world_info._TRIGGER_STATE, {}, clear=True), \
         patch.object(world_info, "find_triggered_entries", side_effect=deterministic_find):
        messages = manager.build_messages()
    return manager, "\n".join(message["content"] for message in messages)


class CustomPersonaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 注册表按 resolve 后的根目录生成资源路径，期望值同样规范化。
        self.root = Path(self.temp.name).resolve()
        self.registry = personas.PERSONAS
        self.aliases = personas.PERSONA_ALIASES
        self.previous_registry = dict(self.registry)
        self.previous_aliases = dict(self.aliases)
        self.addCleanup(self.restore_registry)
        personas.install_custom_personas({}, root=self.root)

    def restore_registry(self):
        self.registry.clear()
        self.registry.update(self.previous_registry)
        self.aliases.clear()
        self.aliases.update(self.previous_aliases)

    def install(self, value=None):
        value = definition() if value is None else value
        personas.install_custom_personas({value["name"]: value}, root=self.root)
        return personas.get_persona(value["name"])

    def test_public_dict_identity_and_builtin_objects_are_stable(self):
        builtins = dict(self.registry)
        files = [ROOT / "src" / "catty_qq_ai" / "personas" / f"{key}.py" for key in builtins]
        source_bytes = [path.read_bytes() for path in files]
        self.install()
        personas.install_custom_personas({}, root=self.root)
        self.assertIs(personas.PERSONAS, self.registry)
        self.assertIs(personas.PERSONA_ALIASES, self.aliases)
        for name, original in builtins.items():
            self.assertIs(self.registry[name], original)
            self.assertIsNone(original.definition_revision)
            self.assertEqual(personas.persona_revision_identity(original), name)
        self.assertEqual([path.read_bytes() for path in files], source_bytes)

    def test_aliases_display_name_and_casefold_resolution(self):
        persona = self.install(definition(aliases=["Explorer", "Straße", "阿特拉斯"]))
        for name in ("ATLAS", "explorer", "EXPLORER", "STRASSE", "阿特拉斯", persona.char_name):
            self.assertEqual(personas.normalize_persona_name(name), "atlas")
            self.assertIs(personas.get_persona(name), persona)
        self.assertIn("Explorer", persona.trigger_prefixes)
        self.assertNotIn("猫猫", persona.directed_keywords)
        self.assertEqual(personas.normalize_persona_name("笨猫"), "catty")

    def test_override_group_mapping_and_default_precedence(self):
        self.install(definition(aliases=["Guide"]))
        cfg = types.SimpleNamespace(catty_group_personas={"17": "Guide"}, catty_default_persona="catty")
        resolve = personas.resolve_persona_name
        self.assertEqual(resolve("group:17:user:8", config=cfg), "atlas")
        self.assertEqual(resolve("group:17:user:8", config=cfg, override_store={"group:17": "fadianji"}), "fadianji")
        self.assertEqual(resolve("group:17:user:8", config=cfg, override_store={"group:17:user:8": "Guide", "group:17": "catty"}), "atlas")
        self.assertEqual(resolve("private:8", config=cfg), "catty")
        cfg.catty_default_persona = "Guide"
        self.assertEqual(resolve("private:8", config=cfg), "atlas")
        self.assertEqual(resolve("group:17", config=cfg, override_store={"group:17": "unknown"}), "atlas")

    def test_replacement_removes_old_personas_and_aliases(self):
        old = self.install(definition(aliases=["old-alias"]))
        self.install(definition("other", aliases=["new-alias"]))
        self.assertNotIn("atlas", self.registry)
        self.assertIsNone(personas.normalize_persona_name("old-alias"))
        self.assertEqual(personas.normalize_persona_name("new-alias"), "other")
        self.assertEqual(old.name, "atlas")  # Captured turns remain immutable.

    def test_revision_is_deterministic_and_content_sensitive(self):
        original = definition(aliases=["Z", "A"], disabled_features=["poke", "mood"])
        with patch("time.time", return_value=1):
            first = self.install(original)
        reordered = dict(reversed(list(original.items())))
        reordered["aliases"] = ["A", "Z"]
        reordered["disabled_features"] = ["mood", "poke"]
        with patch("time.time", return_value=999999999):
            same = self.install(reordered)
        self.assertEqual(first.definition_revision, same.definition_revision)
        self.assertEqual(len(first.definition_revision), 64)
        changed = self.install({**original, "core_persona": original["core_persona"] + "喜欢地图。"})
        self.assertNotEqual(first.definition_revision, changed.definition_revision)
        self.assertEqual(personas.persona_revision_identity(first), f"atlas@{first.definition_revision}")

    def test_bad_candidate_never_changes_either_registry(self):
        self.install()
        before, aliases = dict(self.registry), dict(self.aliases)
        invalid = [
            {"broken": definition("broken", core_persona=" ")},
            {"wrong-key": definition("other")},
            {"catty": definition("catty")},
            {"other": definition("other", aliases=["猫猫"])},
            {"other": definition("other", aliases=["shared"]), "third": definition("third", aliases=["SHARED"])},
            {"other": definition("other", aliases=["third"]), "third": definition("third")},
            {"other": definition("other", imagegen={"girl_tags": [], "ref_path": ""})},
            {"other": definition("other", definition_revision="forged")},
        ]
        for candidate in invalid:
            with self.subTest(candidate=list(candidate)):
                with self.assertRaises(store.PersonaValidationError):
                    personas.install_custom_personas({"valid": definition("valid"), **candidate}, root=self.root)
                self.assertEqual(self.registry, before)
                self.assertEqual(self.aliases, aliases)
                for name, old in before.items():
                    self.assertIs(self.registry[name], old)

    def test_materialization_failure_is_not_published(self):
        self.install()
        before, aliases = dict(self.registry), dict(self.aliases)
        defaults = importlib.import_module(f"{PACKAGE}.persona_defaults")
        with patch.object(defaults, "materialize_custom_persona", side_effect=ValueError("fixture failure")):
            with self.assertRaises(ValueError):
                self.install(definition("other"))
        self.assertEqual(self.registry, before)
        self.assertEqual(self.aliases, aliases)

    def test_all_structured_fields_are_typed_and_detached(self):
        value = definition(
            character_book=[{"identifier": "map", "keys": ["map"], "content": "A blue map.", "order": 7, "constant": True, "case_sensitive": True}],
            imagegen={"girl_tags": "robot, metal", "ref_path": "portrait.png", "ref_path_extra": ["side.png"], "ref_strength": 0.5},
            reply_catalog={"slow_reply_placeholders": ["One moment."], "api_timeout_reply": "Try later."},
            trigger_prefixes=["guide"], directed_keywords=["atlas"],
            disabled_prompt_segments=["sample"], disabled_features=["poke"],
            nsfw_scopes=["private", "group"], nsfw_lexicon=[["OLD", "NEW"]],
            conversation_prompts=[["private:8", "Map discussion."]],
        )
        persona = self.install(value)
        self.assertIsInstance(persona.character_book[0], character_card.CharacterBookEntry)
        self.assertEqual(persona.character_book[0].keys, ("map",))
        self.assertIsInstance(persona.imagegen, personas.PersonaImagegen)
        self.assertEqual(persona.imagegen.ref_path_extra, ("side.png",))
        self.assertIsInstance(persona.reply_catalog, personas.PersonaReplyCatalog)
        self.assertEqual(persona.reply_catalog.slow_reply_placeholders, ("One moment.",))
        self.assertEqual(persona.reply_catalog.api_timeout_reply, "Try later.")
        self.assertEqual(persona.reply_catalog.image_send_failure_reply, personas.NEUTRAL_REPLY_CATALOG.image_send_failure_reply)
        for key in ("disabled_prompt_segments", "disabled_features", "nsfw_scopes"):
            self.assertIsInstance(getattr(persona, key), frozenset)
        for key in ("trigger_prefixes", "directed_keywords", "conversation_prompts", "nsfw_lexicon", "character_book"):
            self.assertIsInstance(getattr(persona, key), tuple)
        self.assertEqual(persona.conversation_prompts, (("private:8", "Map discussion."),))
        self.assertEqual(persona.nsfw_lexicon, (("OLD", "NEW"),))
        value["imagegen"]["ref_path_extra"].append("later.png")
        value["character_book"][0]["keys"].append("later")
        self.assertEqual(persona.imagegen.ref_path_extra, ("side.png",))
        self.assertEqual(persona.character_book[0].keys, ("map",))

    def test_schema_covers_runtime_fields_except_revision(self):
        serializable = {field.name for field in fields(personas.Persona)} - {"definition_revision"}
        properties = set(store.PERSONA_SCHEMA["properties"])
        self.assertEqual(properties - {"schema_version", "aliases"}, serializable)

    def test_default_identity_and_policy(self):
        persona = self.install()
        builtin = personas.get_persona("catty")
        self.assertEqual(persona.character_book, ())
        self.assertEqual(persona.first_mes, "")
        self.assertEqual(persona.mes_example, "")
        for key in ("chat_rhythm", "group_silence", "persona_reminder_text"):
            self.assertTrue(getattr(persona, key))
        for key in ("owner_concept", "nsfw_scopes", "nsfw_gate_private_only", "pregnancy_private_only", "mention_only_trigger", "followup_reply_budget", "nsfw_freeform", "emoji_allow_downloads"):
            self.assertEqual(getattr(persona, key), getattr(builtin, key), key)
        for feature in ("nsfw_spark", "pregnancy", "affection_commands", "tools"):
            self.assertFalse(persona.feature_disabled(feature), feature)
        self.assertTrue(persona.nsfw_allowed(is_private=True))
        self.assertTrue(persona.nsfw_allowed(is_private=False))
        self.assertTrue(persona.semantic_harness_enabled)
        self.assertEqual(persona.evidence_scene_provider, "")
        self.assertEqual(persona.evidence_corpus_provider, "")
        self.assertIs(persona.reply_catalog, personas.NEUTRAL_REPLY_CATALOG)
        self.assertEqual(persona.imagegen.girl_tags, "")
        self.assertEqual(persona.imagegen.ref_path, "")
        self.assertEqual(persona.imagegen.ref_nsfw_path, "")
        self.assertEqual(Path(persona.emoji_root), self.root / "assets" / "atlas" / "emoji")
        self.assertEqual(Path(persona.emoji_manifest_path), Path(persona.emoji_root) / "manifest.json")
        self.assertEqual(Path(persona.emoji_download_dir), Path(persona.emoji_root) / "downloaded")
        self.assertTrue(self.install(definition(emoji_allow_downloads=True)).emoji_allow_downloads)

    def test_null_identity_fields_use_custom_defaults(self):
        persona = self.install(definition(
            character_book=None, imagegen=None, reply_catalog=None,
            first_mes=None, chat_rhythm=None, trigger_prefixes=None, emoji_root=None,
        ))
        self.assertEqual(persona.character_book, ())
        self.assertEqual(persona.first_mes, "")
        self.assertTrue(persona.chat_rhythm)
        self.assertIsNotNone(persona.imagegen)
        self.assertIs(persona.reply_catalog, personas.NEUTRAL_REPLY_CATALOG)
        self.assertIn("atlas", persona.trigger_prefixes)

    def test_real_generic_prompt_has_no_builtin_identity_or_lore(self):
        persona = self.install()
        for group in (False, True):
            with self.subTest(group=group):
                manager, text = render(persona, group=group)
                self.assertIn(persona.core_persona, text)
                for literal in ("笨猫", "猫猫", "爪爪", "猫娘", "喵", "机机", "发电机", "米雪儿", "欧泊", "卡拉彼丘", "猫粮"):
                    self.assertNotIn(literal, text)
                ids = {entry.identifier for entry in manager._entries if entry.materialize()}
                for kept in ("catty_relationship_skeleton", "catty_daily_affection_gate_skeleton", "catty_nsfw_gate_skeleton", "catty_nsfw_gate_params", "catty_semantic_perception", "catty_conversation_flow", "catty_cache_boundary"):
                    self.assertIn(kept, ids)
                self.assertNotIn("fadianji_static_scene_bank", ids)
                self.assertNotIn("catty_world_info", ids)

    def test_explicit_empty_strings_do_not_fall_back(self):
        persona = self.install(definition(
            group_silence="", chat_rhythm="", style_examples="",
            disambiguation_examples="", mes_example="", nsfw_gate_skeleton="",
        ))
        manager, _ = render(persona)
        ids = {entry.identifier for entry in manager._entries if entry.materialize()}
        for empty in ("catty_group_silence", "catty_qq_chat_rhythm", "catty_catgirl_examples", "catty_disambiguation", "catty_mes_example", "catty_nsfw_gate_skeleton"):
            self.assertNotIn(empty, ids)

    def test_custom_examples_and_book_render_with_macros(self):
        persona = self.install(definition(
            style_examples="STYLE {{char}} to {{user}}",
            disambiguation_examples="DISAMBIG {{char}}",
            mes_example="EXAMPLE {{char}}",
            character_book=[{"identifier": "maps", "keys": ["地图"], "content": "CUSTOM MAP LORE", "constant": True}],
        ))
        _, text = render(persona)
        self.assertIn(f"STYLE {persona.char_name} to 用户", text)
        self.assertIn(f"DISAMBIG {persona.char_name}", text)
        self.assertIn(f"EXAMPLE {persona.char_name}", text)
        self.assertIn("CUSTOM MAP LORE", text)

    def test_builtin_rendering_is_unchanged_by_registry_install(self):
        before = {(name, group): render(personas.get_persona(name), group=group)[1]
                  for name in ("catty", "fadianji") for group in (False, True)}
        self.install()
        after = {(name, group): render(personas.get_persona(name), group=group)[1]
                 for name in ("catty", "fadianji") for group in (False, True)}
        self.assertEqual(before, after)

    def test_empty_custom_appearance_does_not_select_builtin_tags_or_references(self):
        source = ROOT / "src" / "catty_qq_ai" / "catty_nsfw_imagegen.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_persona_nsfw_character_tags", "_persona_nsfw_reference_paths"}]
        self.assertEqual(len(functions), 2)
        namespace = {"Any": object, "Path": Path, "logger": nonebot.logger, "CHARACTER_GIRL_TAGS": "BUILTIN_APPEARANCE"}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        persona = self.install()
        self.assertEqual(namespace["_persona_nsfw_character_tags"](persona), "")
        self.assertEqual(namespace["_persona_nsfw_reference_paths"](persona.imagegen), [])
        self.assertEqual(namespace["_persona_nsfw_character_tags"](personas.get_persona("catty")), "BUILTIN_APPEARANCE")
        explicit = self.install(definition(imagegen={"girl_tags": "robot, metal", "ref_path": ""}))
        self.assertEqual(namespace["_persona_nsfw_character_tags"](explicit), "robot, metal")


if __name__ == "__main__":
    unittest.main()