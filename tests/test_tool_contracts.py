"""Offline contract checks for full/lazy tool schemas and their executors."""
import ast
import importlib.util
import inspect
from pathlib import Path
import sys
import textwrap
import types
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_tool_contracts_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)

_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "tools.py"
_SPEC = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.tools", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_tools = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _tools
_SPEC.loader.exec_module(_tools)

_runtime = importlib.import_module(f"{_PACKAGE_NAME}.skill_runtime")
_DYNAMIC_TOOL_NAMES = {"catty_skill_load", "catty_skill_save"}


def _dynamic_schemas() -> dict:
    turn = _runtime.SkillTurn(types.SimpleNamespace())
    assert turn.schemas() == []
    # Seed only capabilities issued by a search/load in this contract fixture;
    # real transition/receipt validation is covered by test_skill_runtime.
    turn._candidates["python-venv"] = "a" * 64
    turn._loaded["python-venv"] = "a" * 64
    turn._load_open = True
    observed = turn.observe_tool_result("catty_fetch_url", {
        "ok": True, "status_code": 200, "url": "https://docs.python.org/3/library/venv.html",
        "title": "Virtual environments", "text": "Create a virtual environment with python -m venv.",
    })
    assert "skill_receipt_id" in observed
    return {schema["function"]["name"]: schema for schema in turn.schemas()}


# Agent mode deliberately keeps imagegen's public schema empty while its
# executor continues to accept legacy direct-generation arguments internally.
_HIDDEN_EXECUTOR_ARGUMENT_TOOLS = {"catty_imagegen"}


def _parameters(schema: dict) -> dict:
    return schema["function"]["parameters"]


def _literal_executor_argument_fields(executor: object) -> set[str]:
    """Return literal fields read from an executor's `args` mapping."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(executor)))
    fields: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "args"
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            fields.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "args"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            fields.add(node.slice.value)
    return fields


class ToolContractTests(unittest.TestCase):
    def test_image_schemas_do_not_reintroduce_removed_addressing_gates(self) -> None:
        obsolete = (
            "用户**直接指向猫猫", "用户没 @", "必须用户**直接指向",
            "用户消息里**必须有图片**", "等用户明确要求",
        )
        for registry in (_tools.ALL_TOOL_SCHEMAS, _tools._LAZY_TOOL_SCHEMAS):
            for name in ("catty_imagegen", "catty_nai_director"):
                description = registry[name]["function"]["description"]
                for phrase in obsolete:
                    with self.subTest(tool=name, phrase=phrase):
                        self.assertNotIn(phrase, description)
        director = _tools.ALL_TOOL_SCHEMAS["catty_nai_director"]["function"]["description"]
        self.assertIn("5 积分/张", director)
        self.assertIn("~170 积分", director)
        self.assertIn("必须提前报价", director)

    def test_write_file_schemas_distinguish_real_documents_and_raw_text(self) -> None:
        for registry in (_tools.ALL_TOOL_SCHEMAS, _tools._LAZY_TOOL_SCHEMAS):
            schema = registry["catty_write_file"]
            self.assertEqual(set(_parameters(schema)["properties"]), {"filename", "content", "format"})
            self.assertEqual(_parameters(schema)["required"], ["content"])
            text = str(schema).lower()
            for token in ("docx", "xlsx", "pdf", "utf-8", "raw", "bytes"):
                self.assertIn(token, text)

    def test_tool_registries_cover_the_same_names(self) -> None:
        full_names = set(_tools.ALL_TOOL_SCHEMAS)
        self.assertSetEqual(set(_tools._LAZY_TOOL_SCHEMAS), full_names)
        dynamic = _dynamic_schemas()
        self.assertSetEqual(set(dynamic), _DYNAMIC_TOOL_NAMES)
        self.assertFalse(full_names & _DYNAMIC_TOOL_NAMES)
        self.assertSetEqual(set(_tools._EXECUTORS), full_names | _DYNAMIC_TOOL_NAMES)
        self.assertSetEqual(set(_tools._TOOL_CAPABILITIES), full_names | _DYNAMIC_TOOL_NAMES)
        for name in sorted(full_names):
            with self.subTest(tool=name):
                self.assertEqual(
                    _tools.ALL_TOOL_SCHEMAS[name]["function"]["name"],
                    name,
                )
                self.assertEqual(
                    _tools._LAZY_TOOL_SCHEMAS[name]["function"]["name"],
                    name,
                )

    def test_lazy_schemas_match_full_parameter_contracts(self) -> None:
        for name in sorted(_tools.ALL_TOOL_SCHEMAS):
            with self.subTest(tool=name):
                full = _parameters(_tools.ALL_TOOL_SCHEMAS[name])
                lazy = _parameters(_tools._LAZY_TOOL_SCHEMAS[name])
                full_properties = full["properties"]
                lazy_properties = lazy["properties"]

                self.assertSetEqual(set(lazy_properties), set(full_properties))
                self.assertListEqual(lazy["required"], full["required"])
                for field in sorted(full_properties):
                    self.assertEqual(
                        lazy_properties[field].get("enum"),
                        full_properties[field].get("enum"),
                    )

    def test_executor_fields_match_public_contracts_except_agent_mode(self) -> None:
        mismatched_tools: set[str] = set()
        dynamic = _dynamic_schemas()
        full_schemas = {**_tools.ALL_TOOL_SCHEMAS, **dynamic}
        lazy_schemas = {**_tools._LAZY_TOOL_SCHEMAS, **dynamic}
        delegates = {
            "catty_skill_search": _runtime.SkillTurn.search,
            "catty_skill_load": _runtime.SkillTurn.load,
            "catty_skill_save": _runtime.SkillTurn.save,
        }
        for name, executor in _tools._EXECUTORS.items():
            with self.subTest(tool=name):
                executor_fields = _literal_executor_argument_fields(delegates.get(name, executor))
                full_fields = set(_parameters(full_schemas[name])["properties"])
                lazy_fields = set(_parameters(lazy_schemas[name])["properties"])
                self.assertSetEqual(lazy_fields, full_fields)
                if executor_fields != full_fields:
                    mismatched_tools.add(name)

        self.assertSetEqual(mismatched_tools, _HIDDEN_EXECUTOR_ARGUMENT_TOOLS)
        self.assertEqual(
            _parameters(_tools.ALL_TOOL_SCHEMAS["catty_imagegen"])["properties"],
            {},
        )
        self.assertTrue(
            _literal_executor_argument_fields(_tools._EXECUTORS["catty_imagegen"])
        )


    def test_intelligence_core_has_full_schemas(self) -> None:
        core = {
            "catty_recall",
            "catty_user_profile",
            "catty_recall_notes",
            "catty_web_search",
            "catty_meme_explain",
            "catty_now",
            "catty_profile_memory",
            "catty_scope_meme",
            "catty_timeline",
            "catty_adaptive_prompt",
        }
        self.assertTrue(core.issubset(_tools.ALL_TOOL_SCHEMAS))
        for name in core:
            self.assertEqual(
                _tools.ALL_TOOL_SCHEMAS[name]["function"]["description"],
                _tools.ALL_TOOL_SCHEMAS[name]["function"]["description"].strip(),
            )


if __name__ == "__main__":
    unittest.main()