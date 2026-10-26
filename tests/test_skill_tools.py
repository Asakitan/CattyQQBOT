from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_skill_tools_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_TOOLS_PATH = _ROOT / "src" / "catty_qq_ai" / "tools.py"
_tools = _load_module(f"{_PACKAGE_NAME}.tools", _TOOLS_PATH)
_loader = _load_module("catty_skill_tools_config_loader_test", _ROOT / "catty_config_loader.py")
_SKILL_NAMES = {"catty_skill_search", "catty_skill_load", "catty_skill_save"}
_BODY = "FETCH_BODY_MUST_NOT_ENTER_TELEMETRY"


def _names(schemas: list[dict]) -> set[str]:
    return {item["function"]["name"] for item in schemas}


def _config(**overrides):
    values = {"catty_tools_disabled_in_private": []}
    values.update(overrides)
    return _tools.Config(**values)


def _ctx(config=None, *, private: bool = False):
    event = (
        _tools.PrivateMessageEvent.model_construct(user_id=123)
        if private
        else _tools.GroupMessageEvent.model_construct(user_id=123, group_id=456)
    )
    return _tools.ToolContext(config=config or _config(), memory_store=None, event=event)


def _dynamic_schema(name: str, key: str, values: list[str], *, array: bool = False):
    value_schema = {"type": "string", "enum": values}
    if array:
        value_schema = {"type": "array", "items": value_schema, "minItems": 1}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "runtime-issued schema",
            "parameters": {
                "type": "object",
                "properties": {key: value_schema},
                "required": [key],
                "additionalProperties": False,
            },
        },
    }


class _StubSkillTurn:
    instances: list = []

    def __init__(self, config, completion_fn=None):
        self.config = config
        self.completion_fn = completion_fn
        self.candidates: set[str] = set()
        self.receipts: set[str] = set()
        self.calls: list[tuple[str, dict]] = []
        self.observed: list[tuple[str, dict]] = []
        self.issued_schemas: list[dict] = []
        type(self).instances.append(self)

    async def search(self, args):
        self.calls.append(("search", args))
        if not isinstance(args.get("query"), str) or not args["query"].strip():
            return {"ok": False, "error": "query_required"}
        self.candidates = {"known-skill"}
        self.issued_schemas = [
            item for item in self.issued_schemas
            if item["function"]["name"] != "catty_skill_load"
        ]
        self.issued_schemas.append(_dynamic_schema("catty_skill_load", "id", ["known-skill"]))
        return {"ok": True, "candidates": [{"id": "known-skill", "description": "metadata only"}]}

    async def load(self, args):
        self.calls.append(("load", args))
        if args.get("id") not in self.candidates:
            return {"ok": False, "error": "candidate_not_issued"}
        return {"ok": True, "body": "Loaded body stays out of schema"}

    async def save(self, args):
        self.calls.append(("save", args))
        ids = args.get("receipt_ids")
        if not isinstance(ids, list) or not ids or any(item not in self.receipts for item in ids):
            return {"ok": False, "error": "receipt_not_issued"}
        return {"ok": True, "id": "learned-skill"}

    def observe_tool_result(self, name, result):
        self.observed.append((name, dict(result)))
        if name != "catty_fetch_url" or result.get("ok") is not True or result.get("error"):
            return dict(result)
        receipt_id = "fetch-receipt"
        self.receipts.add(receipt_id)
        if not any(item["function"]["name"] == "catty_skill_save" for item in self.issued_schemas):
            self.issued_schemas.append(
                _dynamic_schema("catty_skill_save", "receipt_ids", [receipt_id], array=True)
            )
        return {**result, "skill_receipt_id": receipt_id}

    def schemas(self):
        return list(self.issued_schemas)


class SkillToolsTests(unittest.TestCase):
    def setUp(self):
        _StubSkillTurn.instances.clear()
        runtime = types.ModuleType(f"{_PACKAGE_NAME}.skill_runtime")
        runtime.SkillTurn = _StubSkillTurn
        self.runtime_patch = patch.dict(sys.modules, {runtime.__name__: runtime})
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)

    def call(self, name, args, ctx, *, allowed_names=None):
        return asyncio.run(_tools.execute_tool_call(name, json.dumps(args), ctx, allowed_names=allowed_names))

    def test_only_search_on_first_turn_in_full_and_lazy_modes(self):
        for lazy in (False, True):
            for full in (False, True):
                with self.subTest(lazy=lazy, full=full):
                    cfg = _config(catty_tools_lazy_schema_enabled=lazy, catty_tools_full_exposure=full)
                    schemas = _tools.available_tool_schemas(cfg, is_private=False, user_text="早")
                    self.assertEqual(_names(schemas) & _SKILL_NAMES, {"catty_skill_search"})
                    self.assertEqual(_tools.skill_tool_schemas(_ctx(cfg)), [])
        self.assertEqual(_StubSkillTurn.instances, [])

    def test_search_schema_is_bounded_and_contains_no_library_payload(self):
        for registry in (_tools.ALL_TOOL_SCHEMAS, _tools._LAZY_TOOL_SCHEMAS):
            schema = registry["catty_skill_search"]
            params = schema["function"]["parameters"]
            self.assertEqual(params["required"], ["query"])
            self.assertEqual(set(params["properties"]), {"query", "limit"})
            self.assertEqual(params["properties"]["query"]["minLength"], 1)
            self.assertEqual(params["properties"]["query"]["pattern"], r"\S")
            self.assertEqual(params["properties"]["limit"]["minimum"], 1)
            self.assertEqual(params["properties"]["limit"]["maximum"], 5)
            serialized = json.dumps(schema, ensure_ascii=False)
            for forbidden in ("data/skills", "known-skill", "Loaded body", _BODY):
                self.assertNotIn(forbidden, serialized)
            self.assertLessEqual(len(schema["function"]["description"]), 40)

    def test_dynamic_schema_is_runtime_object_not_static_or_synthesized(self):
        ctx = _ctx()
        with patch.object(_tools, "_skill_turn_for_context", side_effect=AssertionError("no eager turn")):
            self.assertEqual(_tools.skill_tool_schemas(ctx), [])
        result = self.call("catty_skill_search", {"query": "指南", "limit": 2}, ctx)
        self.assertTrue(result["ok"])
        self.assertIs(ctx.skill_turn.config, ctx.config)
        self.assertIsNone(ctx.skill_turn.completion_fn)
        schemas = _tools.skill_tool_schemas(ctx)
        self.assertEqual(_names(schemas), {"catty_skill_load"})
        self.assertIs(schemas[0], ctx.skill_turn.issued_schemas[0])
        self.assertEqual(schemas[0]["function"]["parameters"]["properties"]["id"]["enum"], ["known-skill"])
        self.assertTrue(self.call("catty_skill_load", {"id": "known-skill"}, ctx)["ok"])
        self.assertNotIn("Loaded body", json.dumps(_tools.skill_tool_schemas(ctx)))
        self.assertEqual(
            _names(_tools.available_tool_schemas(ctx.config, is_private=False)) & _SKILL_NAMES,
            {"catty_skill_search"},
        )
        ctx.skill_turn.issued_schemas.extend([{"function": {"name": "catty_run_code"}}, {}, None])
        self.assertEqual(_names(_tools.skill_tool_schemas(ctx)), {"catty_skill_load"})

    def test_disabled_flags_filter_schemas_without_constructing_state(self):
        for flag in ("catty_tools_enabled", "catty_skills_enabled"):
            with self.subTest(flag=flag):
                cfg = _config(**{flag: False})
                ctx = _ctx(cfg)
                self.assertFalse(_names(_tools.available_tool_schemas(cfg, is_private=False)) & _SKILL_NAMES)
                self.assertEqual(_tools.skill_tool_schemas(ctx), [])
                for name in _SKILL_NAMES:
                    denied = self.call(name, {}, ctx, allowed_names=_SKILL_NAMES)
                    self.assertEqual(denied["reason"], "config_disabled")
                ctx.skill_turn = Mock()
                self.assertEqual(_tools.skill_tool_schemas(ctx), [])
                ctx.skill_turn.schemas.assert_not_called()
        self.assertEqual(_StubSkillTurn.instances, [])

    def test_auto_learn_and_private_disable_filter_real_dynamic_schemas(self):
        ctx = _ctx(private=True)
        self.call("catty_skill_search", {"query": "指南"}, ctx)
        ctx.skill_turn.observe_tool_result("catty_fetch_url", {"ok": True, "text": _BODY})
        self.assertEqual(_names(_tools.skill_tool_schemas(ctx)), {"catty_skill_load", "catty_skill_save"})
        ctx.config.catty_skills_auto_learn_enabled = False
        self.assertEqual(_names(_tools.skill_tool_schemas(ctx)), {"catty_skill_load"})
        self.assertEqual(self.call("catty_skill_save", {"receipt_ids": ["fetch-receipt"]}, ctx)["reason"], "config_disabled")
        ctx.config.catty_skills_auto_learn_enabled = True
        ctx.config.catty_tools_disabled_in_private = list(_SKILL_NAMES)
        self.assertEqual(_tools.skill_tool_schemas(ctx), [])
        self.assertFalse(_names(_tools.available_tool_schemas(ctx.config, is_private=True)) & _SKILL_NAMES)
        for name in _SKILL_NAMES:
            self.assertEqual(self.call(name, {}, ctx)["reason"], "disabled_in_private")
        group = _ctx(ctx.config)
        group.skill_turn = ctx.skill_turn
        self.assertEqual(_names(_tools.skill_tool_schemas(group)), {"catty_skill_load", "catty_skill_save"})

    def test_dynamic_capability_filter_matches_execution(self):
        ctx = _ctx(private=True)
        self.call("catty_skill_search", {"query": "指南"}, ctx)
        with patch.dict(_tools._TOOL_CAPABILITIES, {"catty_skill_load": _tools.ToolCapability(scope="group")}):
            self.assertEqual(_tools.skill_tool_schemas(ctx), [])
            self.assertEqual(self.call("catty_skill_load", {"id": "known-skill"}, ctx)["reason"], "group_only")

    def test_registered_modes_and_runtime_qualification_cannot_be_bypassed(self):
        for name, mode in (("catty_skill_search", "read"), ("catty_skill_load", "read"), ("catty_skill_save", "write")):
            self.assertIn(name, _tools._EXECUTORS)
            self.assertIn(name, _tools._TOOL_CONFIG_FLAGS)
            self.assertEqual(_tools.tool_execution_mode(name), mode)
        ctx = _ctx()
        for name, args, error in (
            ("catty_skill_load", {"id": "forged", "body": _BODY}, "candidate_not_issued"),
            ("catty_skill_save", {"receipt_ids": ["forged"], "url": "https://example.test", "body": _BODY}, "receipt_not_issued"),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.call(name, args, ctx, allowed_names=_SKILL_NAMES)["error"], error)
                self.assertEqual(asyncio.run(_tools._EXECUTORS[name](args, ctx))["error"], error)
        ctx.config.catty_skills_enabled = False
        self.assertEqual(asyncio.run(_tools._EXECUTORS["catty_skill_load"]({}, ctx))["reason"], "config_disabled")

    def test_real_fetch_executor_issues_receipt_without_altering_body(self):
        ctx = _ctx()
        url = "https://example.test/guide"
        response = _tools.httpx.Response(200, text=f"<html><title>Guide</title><body>{_BODY}</body></html>", headers={"content-type": "text/html"})
        client = AsyncMock()
        client.get.return_value = response
        with patch.object(_tools, "_validate_public_url", return_value=(url, None)), patch.object(
            _tools.httpx, "AsyncClient"
        ) as factory, patch.object(_tools, "_record_tool_call") as telemetry:
            factory.return_value.__aenter__.return_value = client
            result = self.call("catty_fetch_url", {"url": url}, ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["skill_receipt_id"], "fetch-receipt")
        observed_name, original = ctx.skill_turn.observed[0]
        self.assertEqual(observed_name, "catty_fetch_url")
        self.assertNotIn("skill_receipt_id", original)
        self.assertIn(_BODY, result["text"])
        self.assertEqual({key: result[key] for key in original}, original)
        self.assertNotIn(_BODY, repr(telemetry.call_args_list))
        self.assertEqual(_names(_tools.skill_tool_schemas(ctx)), {"catty_skill_save"})
        self.assertTrue(self.call("catty_skill_save", {"receipt_ids": ["fetch-receipt"]}, ctx)["ok"])

    def test_only_explicit_successful_fetch_lazily_constructs_turn(self):
        for name, payload, enabled in (
            ("catty_fetch_url", {"ok": False, "error": "failed"}, True),
            ("catty_fetch_url", {"text": _BODY}, True),
            ("catty_fetch_url", {"ok": True, "error": "failed", "text": _BODY}, True),
            ("catty_now", {"ok": True, "url": "https://example.test", "text": _BODY}, True),
            ("catty_fetch_url", {"ok": True, "text": _BODY}, False),
        ):
            with self.subTest(name=name, payload=payload, enabled=enabled):
                ctx = _ctx(_config(catty_skills_enabled=enabled))
                with patch.dict(_tools._EXECUTORS, {name: AsyncMock(return_value=payload)}):
                    result = self.call(name, {}, ctx)
                self.assertEqual(result, payload)
                self.assertIsNone(ctx.skill_turn)
        self.assertEqual(_StubSkillTurn.instances, [])

    def test_observer_failure_is_nonfatal_and_does_not_log_body(self):
        ctx = _ctx()
        payload = {"ok": True, "text": _BODY, "url": "https://example.test"}
        ctx.skill_turn = Mock()
        ctx.skill_turn.observe_tool_result.side_effect = ValueError(_BODY)
        with patch.dict(_tools._EXECUTORS, {"catty_fetch_url": AsyncMock(return_value=payload)}), patch.object(_tools, "_logger") as logger:
            result = self.call("catty_fetch_url", {}, ctx)
        self.assertEqual(result, payload)
        self.assertNotIn(_BODY, repr(logger.mock_calls))

    def test_skill_args_and_errors_are_not_recorded_as_body(self):
        ctx = _ctx()
        with patch.object(_tools, "_record_tool_call") as telemetry, patch.object(_tools, "_logger") as logger:
            self.call("catty_skill_load", {"id": "forged", "body": _BODY}, ctx)
            self.call("catty_skill_save", {"body": _BODY}, ctx, allowed_names=set())
            asyncio.run(_tools.execute_tool_call("catty_skill_search", _BODY, ctx))
            with patch.dict(_tools._EXECUTORS, {"catty_skill_search": AsyncMock(side_effect=ValueError(_BODY))}):
                result = self.call("catty_skill_search", {"query": _BODY}, ctx)
        self.assertNotIn(_BODY, repr(telemetry.call_args_list))
        self.assertNotIn(_BODY, repr(logger.mock_calls))
        self.assertNotIn(_BODY, repr(result))

    def test_ast_static_registries_never_publish_load_or_save(self):
        tree = ast.parse(_TOOLS_PATH.read_text(encoding="utf-8-sig"))
        registries = {"ALL_TOOL_SCHEMAS", "_LAZY_TOOL_SCHEMAS"}
        found = set()
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in registries:
                found.add(node.target.id)
                keys = {ast.literal_eval(key) for key in node.value.keys}
                self.assertEqual(keys & _SKILL_NAMES, {"catty_skill_search"})
        self.assertEqual(found, registries)
        self.assertIn("catty_skill_search", _tools._INTELLIGENCE_CORE_TOOLS)
        self.assertFalse({"catty_skill_load", "catty_skill_save"} & _tools._INTELLIGENCE_CORE_TOOLS)
        dynamic = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "skill_tool_schemas")
        calls = {node.func.id for node in ast.walk(dynamic) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse({"SkillTurn", "_skill_turn_for_context", "open"} & calls)
        hint = _tools.tools_system_hint()
        self.assertIn("catty_skill_search", hint)
        self.assertIn("权威", hint)
        self.assertNotIn("data/skills", hint)
        self.assertNotIn("known-skill", hint)


class SkillConfigTests(unittest.TestCase):
    def test_defaults_agree_across_model_loader_and_example(self):
        defaults = {
            "enabled": True, "auto_learn_enabled": True, "dir": "data/skills",
            "search_limit": 3, "max_body_chars": 6000,
            "learning_timeout_seconds": 20, "learning_max_tokens": 2400,
        }
        self.assertEqual(_loader.DEFAULT_CONFIG["skills"], defaults)
        example = json.loads((_ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(example["skills"], defaults)
        cfg = _config()
        for key, value in defaults.items():
            self.assertEqual(getattr(cfg, f"catty_skills_{key}"), value)

    def test_loader_exports_all_fields_and_resolves_relative_dir(self):
        values = {
            "enabled": False, "auto_learn_enabled": False, "dir": "fixtures/skills",
            "search_limit": 5, "max_body_chars": 1200,
            "learning_timeout_seconds": 2.5, "learning_max_tokens": 321,
        }
        with patch.dict(os.environ, {}, clear=True):
            _loader._apply_config({"skills": values}, _ROOT)
            exported = {key: value for key, value in os.environ.items() if key.startswith("CATTY_SKILLS_")}
        self.assertEqual(len(exported), len(values))
        self.assertEqual(exported["CATTY_SKILLS_DIR"], str(_ROOT / "fixtures" / "skills"))
        cfg = _tools.Config(**{key.lower(): value for key, value in exported.items()})
        for key, value in values.items():
            self.assertEqual(getattr(cfg, f"catty_skills_{key}"), str(_ROOT / value) if key == "dir" else value)

    def test_config_rejects_out_of_bounds_values(self):
        for key, values in {
            "catty_skills_search_limit": (0, 6),
            "catty_skills_max_body_chars": (0, -1),
            "catty_skills_learning_timeout_seconds": (0, -1, float("inf"), float("nan")),
            "catty_skills_learning_max_tokens": (0, -1),
            "catty_skills_dir": ("", "  ", "bad\x00path"),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    _config(**{key: value})


if __name__ == "__main__":
    unittest.main()