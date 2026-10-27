from __future__ import annotations

from dataclasses import fields
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "catty_shared_persona_capabilities_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(PACKAGE, package)
personas = importlib.import_module(f"{PACKAGE}.personas")
store_module = importlib.import_module(f"{PACKAGE}.persona_store")


CAPABILITY_DEFAULTS = {
    "semantic_harness_enabled": True,
    "group_admission_enabled": True,
    "evidence_scene_provider": "",
    "evidence_corpus_provider": "",
    "corpus_echo_enabled": False,
    "style_critic_enabled": True,
    "style_critic_profile": "persona",
    "output_filter_profile": "persona",
    "proactive_enabled": True,
    "proactive_config_source": "legacy",
    "fadianji_mood_enabled": False,
    "fadianji_self_study_enabled": False,
    "followup_reply_budget": 2,
}
BOOL_FIELDS = (
    "semantic_harness_enabled", "group_admission_enabled", "corpus_echo_enabled",
    "style_critic_enabled", "proactive_enabled", "fadianji_mood_enabled",
    "fadianji_self_study_enabled",
)
ENUM_FIELDS = {
    "evidence_scene_provider": ("", "fadianji"),
    "evidence_corpus_provider": ("", "fadianji"),
    "style_critic_profile": ("persona", "fadianji"),
    "output_filter_profile": ("persona", "fadianji"),
    "proactive_config_source": ("legacy", "fdj"),
}


def definition(**changes):
    value = {
        "schema_version": 1,
        "name": "atlas",
        "char_name": "向导atlas",
        "core_persona": "角色正文原样保留。",
    }
    value.update(changes)
    return value


class SharedPersonaCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = dict(personas.PERSONAS)
        self.aliases = dict(personas.PERSONA_ALIASES)
        self.addCleanup(self.restore_registry)

    def restore_registry(self):
        personas.PERSONAS.clear()
        personas.PERSONAS.update(self.registry)
        personas.PERSONA_ALIASES.clear()
        personas.PERSONA_ALIASES.update(self.aliases)

    def install(self, value):
        personas.install_custom_personas({value["name"]: value}, root=self.root)
        return personas.get_persona(value["name"])

    def assert_capabilities(self, persona, expected):
        self.assertEqual({key: getattr(persona, key) for key in expected}, expected)

    def test_persona_and_catty_defaults_keep_original_wake_names(self):
        self.assert_capabilities(personas.Persona(name="example", char_name="例子"), CAPABILITY_DEFAULTS)
        catty = personas.get_persona("catty")
        self.assert_capabilities(catty, CAPABILITY_DEFAULTS)
        self.assertEqual(catty.char_name, "笨猫")
        self.assertIsNone(catty.trigger_prefixes)
        self.assertIsNone(catty.directed_keywords)
        self.assertEqual(catty.disabled_features, frozenset())
        self.assertTrue(catty.nsfw_allowed(is_private=True))
        self.assertTrue(catty.nsfw_allowed(is_private=False))
        self.assertIsNone(catty.definition_revision)

    def test_fadianji_uses_explicit_providers_but_retains_legacy_switches(self):
        fadianji = personas.get_persona("fadianji")
        expected = {
            **CAPABILITY_DEFAULTS,
            "evidence_scene_provider": "fadianji",
            "evidence_corpus_provider": "fadianji",
            "corpus_echo_enabled": True,
            "style_critic_profile": "fadianji",
            "output_filter_profile": "fadianji",
            "proactive_config_source": "fdj",
            "fadianji_mood_enabled": True,
            "fadianji_self_study_enabled": True,
        }
        self.assert_capabilities(fadianji, expected)
        self.assertEqual(fadianji.trigger_prefixes, ("机机", "小机", "发电机", "不稳定发电机"))
        self.assertIn("proactive_bubble", fadianji.disabled_features)
        self.assertTrue(fadianji.nsfw_allowed(is_private=True))
        self.assertFalse(fadianji.nsfw_allowed(is_private=False))
        self.assertTrue(fadianji.nsfw_gate_private_only)
        self.assertTrue(fadianji.pregnancy_private_only)
        self.assertIsNone(fadianji.definition_revision)

    def test_missing_custom_fields_use_shared_defaults_without_fadianji_evidence(self):
        persona = self.install(definition())
        self.assert_capabilities(persona, {**CAPABILITY_DEFAULTS, "proactive_enabled": False})
        self.assertIn("proactive_bubble", persona.disabled_features)
        self.assertEqual(persona.character_book, ())
        self.assertEqual(persona.core_persona, "角色正文原样保留。")
        self.assertEqual(persona.trigger_prefixes, ("atlas", "向导atlas"))
        self.assertEqual(persona.directed_keywords, ("atlas", "向导atlas"))
        self.assertTrue(persona.nsfw_allowed(is_private=True))
        self.assertTrue(persona.nsfw_allowed(is_private=False))

    def test_legacy_custom_proactive_bubble_disable_and_explicit_override(self):
        cases = (
            ({}, False, True),
            ({"disabled_features": ["proactive_bubble"]}, False, True),
            ({"disabled_features": ["poke"]}, True, False),
            ({"disabled_features": []}, True, False),
            ({"disabled_features": ["proactive_bubble"], "proactive_enabled": True}, True, True),
            ({"disabled_features": [], "proactive_enabled": False}, False, False),
        )
        for changes, enabled, old_disabled in cases:
            with self.subTest(changes=changes):
                persona = self.install(definition(**changes))
                self.assertIs(persona.proactive_enabled, enabled)
                self.assertEqual("proactive_bubble" in persona.disabled_features, old_disabled)

    def test_explicit_budget_including_null_is_not_replaced(self):
        for budget in (0, 1, 7, None):
            with self.subTest(budget=budget):
                persona = self.install(definition(followup_reply_budget=budget))
                self.assertEqual(persona.followup_reply_budget, budget)

    def test_schema_matches_runtime_fields_and_has_exact_enums(self):
        properties = store_module.PERSONA_SCHEMA["properties"]
        serializable = {field.name for field in fields(personas.Persona)} - {"definition_revision"}
        self.assertEqual(set(properties) - {"schema_version", "aliases"}, serializable)
        self.assertNotIn("definition_revision", properties)
        for key, allowed in ENUM_FIELDS.items():
            with self.subTest(field=key):
                self.assertEqual(properties[key]["type"], "string")
                self.assertEqual(set(properties[key]["enum"]), set(allowed))
                for value in allowed:
                    self.assertEqual(store_module.validate_persona_definition(definition(**{key: value}))[key], value)

    def test_schema_rejects_unlisted_profiles_providers_and_sources(self):
        for key, allowed in ENUM_FIELDS.items():
            for value in ("unknown", " fadianji", "FADIANJI"):
                if value in allowed:
                    continue
                with self.subTest(field=key, value=value):
                    with self.assertRaises(store_module.PersonaValidationError) as caught:
                        store_module.validate_persona_definition(definition(**{key: value}))
                    self.assertEqual(caught.exception.code, "enum")
                    self.assertEqual(caught.exception.field, f"$.{key}")
            for value in (None, 1, False):
                with self.subTest(field=key, value=value):
                    with self.assertRaises(store_module.PersonaValidationError) as caught:
                        store_module.validate_persona_definition(definition(**{key: value}))
                    self.assertEqual(caught.exception.code, "type")

    def test_schema_rejects_non_boolean_capability_values(self):
        for key in BOOL_FIELDS:
            for value in (None, 0, 1, "false"):
                with self.subTest(field=key, value=value):
                    with self.assertRaises(store_module.PersonaValidationError) as caught:
                        store_module.validate_persona_definition(definition(**{key: value}))
                    self.assertEqual(caught.exception.code, "type")
            for value in (True, False):
                self.assertIs(store_module.validate_persona_definition(definition(**{key: value}))[key], value)
        with self.assertRaises(store_module.PersonaValidationError):
            store_module.validate_persona_definition(definition(definition_revision="forged"))

    def test_old_saved_definition_and_explicit_values_survive_reload_and_install(self):
        original = definition(
            aliases=["引路人"],
            semantic_harness_enabled=False,
            group_admission_enabled=False,
            disabled_features=["proactive_bubble", "poke"],
            nsfw_scopes=["private"],
            nsfw_freeform=False,
            nsfw_gate_private_only=True,
            pregnancy_private_only=True,
            nsfw_gate_skeleton="旧策略正文",
            nsfw_style_override="旧口吻正文",
            nsfw_prefill="旧前缀",
            nsfw_lexicon=[["旧称", "新称"]],
            trigger_prefixes=["旧唤醒"],
            directed_keywords=["旧称呼"],
            followup_reply_budget=4,
        )
        store = store_module.PersonaDefinitionStore(self.root / "personas")
        receipt = store.save(original)
        self.assertTrue(receipt["ok"], receipt)
        path = Path(receipt["path"])
        disk_before = path.read_bytes()
        self.assertTrue(store.reload()["ok"])
        normalized = store_module.validate_persona_definition(original)
        self.assertEqual(store.definitions()["atlas"], normalized)
        persona = self.install(store.definitions()["atlas"])
        self.assertEqual(path.read_bytes(), disk_before)
        self.assertEqual(store.definitions()["atlas"], normalized)
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        revision = hashlib.sha256(encoded).hexdigest()
        self.assertEqual(persona.definition_revision, revision)
        self.assertEqual(personas.persona_revision_identity(persona), f"atlas@{revision}")
        self.assertEqual(self.install(normalized).definition_revision, revision)
        self.assertIs(persona.semantic_harness_enabled, False)
        self.assertIs(persona.group_admission_enabled, False)
        self.assertIs(persona.proactive_enabled, False)
        self.assertEqual(persona.disabled_features, frozenset({"proactive_bubble", "poke"}))
        self.assertEqual(persona.followup_reply_budget, 4)
        self.assertEqual(persona.trigger_prefixes, ("旧唤醒",))
        self.assertEqual(persona.directed_keywords, ("旧称呼",))
        self.assertEqual(persona.nsfw_scopes, frozenset({"private"}))
        self.assertFalse(persona.nsfw_freeform)
        self.assertTrue(persona.nsfw_gate_private_only)
        self.assertTrue(persona.pregnancy_private_only)
        self.assertEqual(persona.nsfw_gate_skeleton, "旧策略正文")
        self.assertEqual(persona.nsfw_style_override, "旧口吻正文")
        self.assertEqual(persona.nsfw_prefill, "旧前缀")
        self.assertEqual(persona.nsfw_lexicon, (("旧称", "新称"),))
        self.assertTrue(persona.nsfw_allowed(is_private=True))
        self.assertFalse(persona.nsfw_allowed(is_private=False))

    def test_all_new_explicit_values_survive_save_reload_and_materialization(self):
        overrides = {
            "semantic_harness_enabled": False,
            "group_admission_enabled": False,
            "evidence_scene_provider": "fadianji",
            "evidence_corpus_provider": "fadianji",
            "corpus_echo_enabled": True,
            "style_critic_enabled": False,
            "style_critic_profile": "fadianji",
            "output_filter_profile": "fadianji",
            "proactive_enabled": True,
            "proactive_config_source": "fdj",
            "fadianji_mood_enabled": True,
            "fadianji_self_study_enabled": True,
            "followup_reply_budget": 9,
        }
        original = definition(disabled_features=["proactive_bubble"], **overrides)
        store = store_module.PersonaDefinitionStore(self.root / "personas")
        receipt = store.save(original)
        self.assertTrue(receipt["ok"], receipt)
        restarted = store_module.PersonaDefinitionStore(self.root / "personas")
        saved = restarted.definitions()["atlas"]
        persona = self.install(saved)
        self.assert_capabilities(persona, overrides)
        self.assertIn("proactive_bubble", persona.disabled_features)
        self.assertEqual(saved, store_module.validate_persona_definition(original))


if __name__ == "__main__":
    unittest.main()
