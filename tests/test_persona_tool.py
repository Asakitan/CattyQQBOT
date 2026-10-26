"""Persona-tool contract tests using a package shell and injected offline callbacks."""
from __future__ import annotations

import ast
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import textwrap
import types
import unittest
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "catty_persona_tool_test"
PACKAGE = types.ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(PACKAGE_NAME, PACKAGE)
SPEC = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.tools", ROOT / "src" / "catty_qq_ai" / "tools.py")
assert SPEC is not None and SPEC.loader is not None
tools = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tools
SPEC.loader.exec_module(tools)

NAME = "catty_persona"
ACTIONS = {"schema", "list", "show", "save", "switch", "reset", "bind", "unbind", "reload"}
FIELDS = {"action", "name", "definition", "command", "overwrite", "expected_sha256"}
DEFINITION = {"schema_version": 1, "name": "luna", "char_name": "露娜", "core_persona": "一位天文爱好者。"}


def _context(*, owner: bool = True, callback=None, text: str = "就换刚才那个"):
    return tools.ToolContext(
        config=types.SimpleNamespace(catty_owner_qq="10001"),
        memory_store=None,
        event=types.SimpleNamespace(user_id=10001 if owner else 10002),
        user_text=text,
        is_directly_requested=False,
        persona_manage_fn=callback,
    )


class PersonaToolSchemaTests(unittest.TestCase):
    def test_full_lazy_schema_parameter_and_store_contracts_match(self) -> None:
        full = tools.ALL_TOOL_SCHEMAS[NAME]["function"]
        lazy = tools._LAZY_TOOL_SCHEMAS[NAME]["function"]
        self.assertEqual(full["name"], NAME)
        self.assertEqual(lazy["name"], NAME)
        self.assertEqual(full["parameters"], lazy["parameters"])
        parameters = full["parameters"]
        self.assertEqual(set(parameters["properties"]), FIELDS)
        self.assertEqual(parameters["required"], ["action"])
        self.assertIs(parameters["additionalProperties"], False)
        self.assertEqual(set(parameters["properties"]["action"]["enum"]), ACTIONS)
        store = sys.modules[f"{PACKAGE_NAME}.persona_store"]
        self.assertIs(parameters["properties"]["definition"], store.PERSONA_SCHEMA)
        json.dumps(parameters, ensure_ascii=False)

    def test_executor_capabilities_config_and_intent_registries_include_persona(self) -> None:
        registries = (
            tools.ALL_TOOL_SCHEMAS, tools._LAZY_TOOL_SCHEMAS, tools._EXECUTORS,
            tools._TOOL_CAPABILITIES, tools._TOOL_CONFIG_FLAGS, tools._INTENT_KEYWORDS,
        )
        for registry in registries:
            self.assertIn(NAME, registry)
        self.assertEqual(set(tools.ALL_TOOL_SCHEMAS), set(tools._LAZY_TOOL_SCHEMAS))
        dynamic_names = {"catty_skill_load", "catty_skill_save"}
        self.assertFalse(set(tools.ALL_TOOL_SCHEMAS) & dynamic_names)
        self.assertEqual(set(tools.ALL_TOOL_SCHEMAS) | dynamic_names, set(tools._EXECUTORS))
        self.assertEqual(set(tools.ALL_TOOL_SCHEMAS) | dynamic_names, set(tools._TOOL_CAPABILITIES))
        self.assertIs(tools._EXECUTORS[NAME], tools._exec_persona)
        self.assertEqual(tools._TOOL_CONFIG_FLAGS[NAME], ())
        self.assertIn(NAME, tools._INTELLIGENCE_CORE_TOOLS)
        self.assertIn(NAME, tools._detect_tool_intent("切换人格", False))

    def test_executor_fields_equal_full_and_lazy_schema_fields(self) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(tools._exec_persona)))
        fields = {
            node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "args"
            and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant)
        }
        self.assertEqual(fields, FIELDS)

    def test_no_direct_message_image_or_persona_feature_gate(self) -> None:
        capability = tools.tool_capability(NAME)
        self.assertEqual(capability.scope, "both")
        self.assertEqual(capability.execution_mode, "write")
        self.assertFalse(capability.requires_direct_request)
        self.assertFalse(capability.requires_image)
        self.assertIsNone(capability.persona_feature)

    def test_schema_available_for_history_only_request_even_in_legacy_intent_mode(self) -> None:
        for full in (False, True):
            for lazy in (False, True):
                for private in (False, True):
                    with self.subTest(full=full, lazy=lazy, private=private):
                        config = types.SimpleNamespace(catty_tools_full_exposure=full, catty_tools_lazy_schema_enabled=lazy)
                        schemas = tools.available_tool_schemas(
                            config, is_private=private, user_text="就换刚才那个",
                            is_directly_requested=False, has_image=False,
                            persona=types.SimpleNamespace(name="custom"),
                        )
                        self.assertEqual(sum(schema["function"]["name"] == NAME for schema in schemas), 1)

    def test_existing_tools_disable_switch_remains_effective(self) -> None:
        schemas = tools.available_tool_schemas(types.SimpleNamespace(catty_tools_enabled=False), is_private=True)
        self.assertEqual(schemas, [])
        schemas = tools.available_tool_schemas(
            types.SimpleNamespace(catty_tools_disabled_in_private=[NAME]), is_private=True,
        )
        self.assertNotIn(NAME, [schema["function"]["name"] for schema in schemas])

    def test_executor_has_one_callback_await_and_no_planner_or_model_import(self) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(tools._exec_persona)))
        awaits = [node.value for node in ast.walk(tree) if isinstance(node, ast.Await)]
        self.assertEqual(len(awaits), 1)
        self.assertIsInstance(awaits[0].func, ast.Name)
        self.assertEqual(awaits[0].func.id, "callback")
        self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)))
        for text in ("chat_completion", "generate_persona_definition", "user_text", "get_plaintext"):
            self.assertNotIn(text, ast.dump(tree))


class PersonaToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonowner_denied_before_callback_and_argument_access(self) -> None:
        callback = AsyncMock(return_value={"ok": True})
        result = await tools._exec_persona(None, _context(owner=False, callback=callback))
        self.assertEqual(result["error"], "owner_only")
        callback.assert_not_called()

        class UnreadableCallback:
            config = types.SimpleNamespace(catty_owner_qq="10001")
            user_id = "10002"

            @property
            def persona_manage_fn(self):
                raise AssertionError("unauthorized callback inspection")

        result = await tools._exec_persona({}, UnreadableCallback())
        self.assertEqual(result["error"], "owner_only")

    async def test_missing_real_event_or_unconfigured_owner_is_denied(self) -> None:
        callback = AsyncMock()
        ctx = _context(callback=callback)
        ctx.event = None
        self.assertEqual((await tools._exec_persona({"action": "reset"}, ctx))["error"], "owner_only")
        ctx = _context(callback=callback)
        ctx.config.catty_owner_qq = ""
        self.assertEqual((await tools._exec_persona({"action": "reset"}, ctx))["error"], "owner_only")
        callback.assert_not_called()

    async def test_callback_absent_reports_unavailable_for_every_action(self) -> None:
        for action in ACTIONS:
            with self.subTest(action=action):
                result = await tools._exec_persona({"action": action}, _context())
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "unavailable")
                self.assertNotIn("saved", result)
                self.assertNotIn("switched", result)

    async def test_each_action_forwards_exact_provided_arguments_and_receipt(self) -> None:
        requests = [
            {"action": "schema"}, {"action": "list"}, {"action": "show", "name": "luna"},
            {"action": "save", "definition": dict(DEFINITION)},
            {"action": "switch", "name": "luna"}, {"action": "reset"},
            {"action": "bind", "name": "luna", "command": "/露娜 上线", "overwrite": False},
            {"action": "unbind", "command": "/露娜 上线"}, {"action": "reload"},
        ]
        self.assertEqual({request["action"] for request in requests}, ACTIONS)
        for request in requests:
            with self.subTest(action=request["action"]):
                receipt = {"ok": True, "action": request["action"], "receipt": {"source": "callback"}}
                callback = AsyncMock(return_value=receipt)
                result = await tools._exec_persona(request, _context(callback=callback))
                self.assertIs(result, receipt)
                callback.assert_awaited_once_with(request)

    async def test_unknown_identity_scope_fields_are_not_forwarded(self) -> None:
        allowed = {
            "action": "save", "name": "luna", "definition": dict(DEFINITION),
            "overwrite": True, "expected_sha256": "a" * 64,
        }
        args = {**allowed, "is_owner": True, "user_id": "10002", "scope": "group:other", "path": "other.json"}
        before = dict(args)
        receipt = {"ok": True, "status": "saved", "sha256": "b" * 64}
        callback = AsyncMock(return_value=receipt)
        self.assertIs(await tools._exec_persona(args, _context(callback=callback)), receipt)
        callback.assert_awaited_once_with(allowed)
        self.assertEqual(args, before)
        self.assertNotIn("switched", receipt)

    async def test_invalid_or_missing_action_never_defaults_to_mutation(self) -> None:
        callback = AsyncMock()
        for args in ({}, {"action": ""}, {"action": "delete"}, {"action": []}, {"action": None}):
            with self.subTest(args=args):
                result = await tools._exec_persona(args, _context(callback=callback))
                self.assertEqual(result["error"], "invalid_action")
        callback.assert_not_called()

    async def test_missing_target_is_an_error_not_reset_or_broad_mutation(self) -> None:
        callback = AsyncMock()
        for args, field in (
            ({"action": "show"}, "name"), ({"action": "switch"}, "name"),
            ({"action": "switch", "name": "  "}, "name"),
            ({"action": "bind", "command": "/上线"}, "name"),
            ({"action": "bind", "name": "luna"}, "command"),
            ({"action": "unbind"}, "command"), ({"action": "save"}, "definition"),
            ({"action": "save", "definition": {}}, "definition.name"),
        ):
            with self.subTest(args=args):
                result = await tools._exec_persona(args, _context(callback=callback))
                self.assertEqual(result["error"], "missing_argument")
                self.assertEqual(result["field"], field)
        callback.assert_not_called()

    async def test_invalid_target_types_and_mismatch_do_not_reach_callback(self) -> None:
        callback = AsyncMock()
        for args in (
            {"action": "switch", "name": 123},
            {"action": "unbind", "command": []},
            {"action": "save", "definition": []},
            {"action": "save", "definition": dict(DEFINITION), "overwrite": "false"},
            {"action": "save", "definition": dict(DEFINITION), "expected_sha256": 123},
            {"action": "save", "definition": dict(DEFINITION), "name": "other"},
        ):
            with self.subTest(args=args):
                result = await tools._exec_persona(args, _context(callback=callback))
                self.assertFalse(result["ok"])
                self.assertIn(result["error"], {"invalid_argument", "target_mismatch"})
        callback.assert_not_called()

    async def test_callback_failure_receipt_is_forwarded_without_success_fields(self) -> None:
        receipt = {"ok": False, "error": "revision_conflict", "sha256": "c" * 64}
        callback = AsyncMock(return_value=receipt)
        result = await tools._exec_persona({"action": "save", "definition": dict(DEFINITION)}, _context(callback=callback))
        self.assertIs(result, receipt)
        self.assertNotIn("saved", result)
        self.assertNotIn("switched", result)

    async def test_invalid_callback_receipt_never_becomes_success(self) -> None:
        for receipt in (None, True, "saved"):
            callback = AsyncMock(return_value=receipt)
            result = await tools._exec_persona({"action": "reset"}, _context(callback=callback))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "invalid_receipt")
            callback.assert_awaited_once_with({"action": "reset"})

    async def test_dispatcher_accepts_history_target_without_direct_request(self) -> None:
        receipt = {"ok": True, "status": "switched", "name": "luna", "scope": "private:10001"}
        callback = AsyncMock(return_value=receipt)
        result = await tools.execute_tool_call(
            NAME, '{"action":"switch","name":"luna"}', _context(callback=callback), allowed_names={NAME},
        )
        self.assertIs(result, receipt)
        callback.assert_awaited_once_with({"action": "switch", "name": "luna"})

    async def test_dispatcher_owner_and_allowlist_denials_prevent_callback(self) -> None:
        callback = AsyncMock()
        result = await tools.execute_tool_call(NAME, '{"action":"reset"}', _context(owner=False, callback=callback))
        self.assertEqual(result["error"], "owner_only")
        result = await tools.execute_tool_call(NAME, '{"action":"reset"}', _context(callback=callback), allowed_names=set())
        self.assertEqual(result["reason"], "not_in_allowlist")
        callback.assert_not_called()

    async def test_callback_exception_is_dispatcher_error_not_fabricated_success(self) -> None:
        callback = AsyncMock(side_effect=RuntimeError("offline callback failed"))
        with self.assertLogs("catty_qq_ai.tools", level="WARNING"):
            result = await tools.execute_tool_call(NAME, '{"action":"reset"}', _context(callback=callback))
        self.assertIn("RuntimeError", result["error"])
        self.assertNotIn("saved", result)
        self.assertNotIn("switched", result)
        callback.assert_awaited_once_with({"action": "reset"})


if __name__ == "__main__":
    unittest.main()