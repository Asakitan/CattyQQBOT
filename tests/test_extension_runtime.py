from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src/catty_qq_ai"
PACKAGE_NAME = "catty_extension_runtime_tests"
PACKAGE = types.ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(SOURCE_ROOT)]
sys.modules[PACKAGE_NAME] = PACKAGE
for MODULE_NAME in ("task_contracts", "extension_runtime"):
    SPEC = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.{MODULE_NAME}", SOURCE_ROOT / f"{MODULE_NAME}.py")
    MODULE = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = MODULE
    SPEC.loader.exec_module(MODULE)
RUNTIME = sys.modules[f"{PACKAGE_NAME}.extension_runtime"]
IDENTITY = sys.modules[f"{PACKAGE_NAME}.task_contracts"].TaskIdentity
OWNER = IDENTITY("owner", "private:owner", is_owner=True)
GUEST = IDENTITY("guest", "group:123")


def manifest(name="sample", *, allow_guests=False, version="1.0.0"):
    return {
        "name": name, "version": version, "api_version": 1, "module": "extension.py",
        "tools": [{
            "name": "echo", "description": "Return a supplied value.",
            "parameters": {
                "type": "object", "properties": {"text": {"type": "string", "maxLength": 200}},
                "required": ["text"], "additionalProperties": False,
            },
            "allow_guests": allow_guests,
        }],
    }


class ExtensionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "extensions"
        self.root.mkdir()
        self.registry = RUNTIME.ExtensionRegistry(self.root)
        self.addAsyncCleanup(self.registry.close)

    def write_extension(self, declaration=None, source=None):
        declaration = manifest() if declaration is None else declaration
        directory = self.root / declaration["name"]
        directory.mkdir(exist_ok=True)
        (directory / "manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
        (directory / "extension.py").write_text(
            source or "def echo(arguments, context):\n    return arguments['text']\nTOOLS = {'echo': echo}\n",
            encoding="utf-8",
        )
        return directory

    def test_discovery_never_imports_or_implicitly_enables(self):
        self.write_extension(source="raise RuntimeError('must not execute discovery')\n")
        self.assertEqual(self.registry.enabled_ids, ())
        self.assertEqual(self.registry.discover()[0]["state"], "disabled")
        self.assertFalse(self.registry.list_extensions()[0]["enabled"])

    def test_configured_ids_are_explicit_without_constructor_imports(self):
        self.write_extension(source="raise RuntimeError('not at construction')\n")
        registry = RUNTIME.ExtensionRegistry(self.root, {"enabled_ids": ["sample"]})
        self.assertEqual(registry.enabled_ids, ("sample",))
        self.assertFalse(registry.list_extensions()[0]["enabled"])

    def test_rejects_invalid_manifests_and_schemas(self):
        changes = [
            {"api_version": 2}, {"api_version": True}, {"version": "latest"},
            {"module": "../secret.py"}, {"module": "C:\\secret.py"},
            {"tools": [manifest()["tools"][0], manifest()["tools"][0]]},
        ]
        for change in changes:
            with self.subTest(change=change):
                declaration = {**manifest(), **change}
                self.write_extension(declaration)
                self.assertEqual(self.registry.discover()[0]["state"], "invalid")
        for name in ("catty_echo", "mcp__echo", "ext__echo", "a__b"):
            with self.subTest(name=name):
                declaration = manifest()
                declaration["tools"][0]["name"] = name
                self.write_extension(declaration)
                self.assertEqual(self.registry.discover()[0]["state"], "invalid")
        declaration = manifest()
        declaration["tools"][0]["parameters"] = {"type": "object", "$ref": "file:///config.json"}
        self.write_extension(declaration)
        self.assertEqual(self.registry.discover()[0]["state"], "invalid")

    def test_duplicate_json_fields_are_rejected(self):
        directory = self.write_extension()
        contents = json.dumps(manifest())[:-1] + ',"api_version": 1}'
        (directory / "manifest.json").write_text(contents, encoding="utf-8")
        self.assertEqual(self.registry.discover()[0]["state"], "invalid")

    def test_symlink_module_and_manifest_cannot_escape(self):
        directory = self.write_extension()
        outside = Path(self.directory.name) / "outside.py"
        outside.write_text("raise RuntimeError('outside')", encoding="utf-8")
        source = directory / "extension.py"
        source.unlink()
        try:
            source.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        self.assertEqual(self.registry.discover()[0]["state"], "invalid")
        source.unlink()
        source.write_text("TOOLS = {}", encoding="utf-8")
        declaration = directory / "manifest.json"
        declaration.unlink()
        declaration.symlink_to(outside)
        self.assertEqual(self.registry.discover()[0]["state"], "invalid")

    def test_builtin_registration_validates_without_execution(self):
        called = []
        self.registry.register_builtin(manifest(), {"echo": lambda arguments, context: called.append(arguments)})
        self.assertEqual(called, [])
        self.assertEqual(self.registry.discover()[0]["tools"], ["ext__sample__echo"])
        with self.assertRaises(RUNTIME.ExtensionError):
            self.registry.register_builtin(manifest(), {"echo": lambda arguments, context: None})

    def test_invalid_callback_signature_is_rejected_before_loading(self):
        with self.assertRaises(TypeError):
            self.registry.register_builtin(manifest(), {"echo": lambda: None})
        with self.assertRaises(TypeError):
            self.registry.register_builtin(manifest(), {"echo": lambda arguments, context: None}, hooks={"load": lambda: None})

    def confirmed_budget(self, task_identity=GUEST, **changes):
        return {
            "confirmed": True, "root_task_id": "root-123", "identity": task_identity.as_dict(),
            "allowed_tools": ["ext__sample__echo"], **changes,
        }

    async def test_lifecycle_load_call_unload_and_context(self):
        self.write_extension(source="""
events = []
def on_load(context):
    events.append('loaded')
def echo(arguments, context):
    return {'text': arguments['text'], 'events': list(events),
            'identity': dict(context['identity']), 'has_config': 'config' in context}
def on_unload(context):
    events.append('unloaded')
TOOLS = {'echo': echo}
""")
        enabled = await self.registry.enable("sample", identity=OWNER)
        self.assertTrue(enabled["enabled"])
        module_name = self.registry._loaded["sample"].module_name
        module = sys.modules[module_name]
        result = await self.registry.call_tool(OWNER, "ext__sample__echo", {"text": "hello"})
        self.assertEqual(result["result"]["events"], ["loaded"])
        self.assertEqual(result["result"]["identity"], OWNER.as_dict())
        self.assertFalse(result["result"]["has_config"])
        await self.registry.disable("sample", identity=OWNER)
        self.assertEqual(module.events, ["loaded", "unloaded"])
        self.assertNotIn(module_name, sys.modules)
        self.assertEqual(self.registry.schemas(OWNER), [])

    async def test_guest_cannot_administer_extensions(self):
        self.write_extension()
        for operation in (self.registry.enable, self.registry.disable, self.registry.reload):
            with self.subTest(operation=operation.__name__), self.assertRaises(PermissionError):
                await operation("sample", identity=GUEST)
        self.assertEqual(self.registry.schemas(OWNER), [])

    async def test_manifest_permissions_and_confirmed_root_are_both_required(self):
        self.registry.register_builtin(manifest(allow_guests=True), {"echo": lambda arguments, context: dict(context["identity"])})
        await self.registry.enable("sample", identity=OWNER)
        self.assertEqual(len(self.registry.schemas(GUEST)), 1)
        self.assertFalse((await self.registry.call_tool(GUEST, "ext__sample__echo", {"text": "hello"}))["ok"])
        budget = self.confirmed_budget()
        result = await self.registry.call_tool(GUEST, "ext__sample__echo", {"text": "hello"}, budget)
        self.assertEqual(result, {"ok": True, "result": GUEST.as_dict()})
        invalid_budgets = [
            {"confirmed": False}, {"root_task_id": ""}, {"identity": OWNER.as_dict()},
            {"allowed_tools": []}, {"allowed_tools": ["*"]}, {"remaining_tokens": 0},
            {"remaining_steps": -1}, {"timeout_seconds": True},
        ]
        for change in invalid_budgets:
            with self.subTest(change=change):
                result = await self.registry.call_tool(GUEST, "ext__sample__echo", {"text": "hello"}, self.confirmed_budget(**change))
                self.assertEqual(result, {"ok": False, "error": "PermissionError"})
        self.assertFalse((await self.registry.call_tool(OWNER.as_dict(), "ext__sample__echo", {"text": "hello"}))["ok"])

    async def test_private_tool_and_upper_role_allowlist_cannot_be_bypassed(self):
        self.write_extension()
        await self.registry.enable("sample", identity=OWNER)
        self.assertEqual(self.registry.schemas(GUEST), [])
        result = await self.registry.call_tool(GUEST, "ext__sample__echo", {"text": "hello"}, self.confirmed_budget())
        self.assertFalse(result["ok"])
        result = await self.registry.call_tool(OWNER, "ext__sample__echo", {"text": "hello"}, self.confirmed_budget(OWNER, allowed_tools=[]))
        self.assertFalse(result["ok"])
        for name in ("catty_read_file", "mcp__server__echo", "ext__sample__missing", "ext__sample__catty_read_file"):
            self.assertFalse((await self.registry.call_tool(OWNER, name, {"text": "hello"}))["ok"])

    async def test_task_context_provider_is_host_only_and_drops_unrelated_secrets(self):
        budget = self.confirmed_budget()
        budget["api_key"] = "TOKEN-must-not-reach-plugin"
        registry = RUNTIME.ExtensionRegistry(self.root, {"task_context": lambda identity: budget})
        self.addAsyncCleanup(registry.close)
        registry.register_builtin(manifest(allow_guests=True), {"echo": lambda arguments, context: dict(context["budget"]) | {"identity": dict(context["identity"])}})
        await registry.enable("sample", identity=OWNER)
        result = await registry.call_tool(GUEST, "ext__sample__echo", {"text": "hello"})
        self.assertTrue(result["ok"])
        self.assertNotIn("api_key", result["result"])
        self.assertNotIn("TOKEN", repr(result))
        budget["allowed_tools"] = []
        self.assertEqual(registry.schemas(GUEST), [])
        self.assertFalse((await registry.call_tool(GUEST, "ext__sample__echo", {"text": "hello"}))["ok"])

    async def test_schema_copy_and_argument_validation(self):
        self.write_extension()
        await self.registry.enable("sample", identity=OWNER)
        schemas = self.registry.schemas(OWNER)
        schemas[0]["function"]["parameters"]["required"] = []
        for arguments in ({}, {"text": 1}, {"text": "hello", "is_owner": True}, {"text": "x" * 201}):
            result = await self.registry.call_tool(OWNER, "ext__sample__echo", arguments)
            self.assertEqual(result, {"ok": False, "error": "ValueError"})
        self.assertEqual(await self.registry.call_tool(OWNER, "ext__sample__echo", {"text": "valid"}), {"ok": True, "result": "valid"})

    async def test_reload_uses_fresh_source_and_retires_only_previous_namespace(self):
        self.write_extension(source="def echo(arguments, context):\n    return 'version-one'\n")
        await self.registry.enable("sample", identity=OWNER)
        old_name = self.registry._loaded["sample"].module_name
        self.write_extension(manifest(version="1.0.1"), source="def echo(arguments, context):\n    return 'version-two'\n")
        status = await self.registry.reload("sample", identity=OWNER)
        self.assertEqual(status["version"], "1.0.1")
        self.assertNotIn(old_name, sys.modules)
        self.assertEqual((await self.registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"}))["result"], "version-two")

    async def test_failed_reload_keeps_old_valid_generation(self):
        self.write_extension(source="def echo(arguments, context):\n    return 'old'\n")
        await self.registry.enable("sample", identity=OWNER)
        old_name = self.registry._loaded["sample"].module_name
        for source in (
            "this is not valid Python !",
            "TOOLS = {'wrong': lambda arguments, context: None}",
            "def echo(arguments, context): return 'new'\ndef on_load(context): raise RuntimeError('TOKEN-secret')",
        ):
            self.write_extension(manifest(version="1.0.1"), source)
            with self.assertRaises(RUNTIME.ExtensionError) as failure:
                await self.registry.reload("sample", identity=OWNER)
            self.assertNotIn("TOKEN", str(failure.exception))
            self.assertEqual(self.registry._loaded["sample"].module_name, old_name)
            self.assertEqual((await self.registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"}))["result"], "old")
        self.write_extension({**manifest(), "api_version": 99})
        with self.assertRaises(RUNTIME.ExtensionError):
            await self.registry.reload("sample", identity=OWNER)
        self.assertEqual(self.registry.schemas(OWNER)[0]["function"]["name"], "ext__sample__echo")

    async def test_running_call_rejects_reload_and_disable(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def echo(arguments, context):
            started.set()
            await release.wait()
            return arguments["text"]

        self.registry.register_builtin(manifest(), {"echo": echo})
        await self.registry.enable("sample", identity=OWNER)
        call = asyncio.create_task(self.registry.call_tool(OWNER, "ext__sample__echo", {"text": "done"}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            for operation in (self.registry.reload, self.registry.disable):
                with self.assertRaises(RUNTIME.ExtensionError) as failure:
                    await operation("sample", identity=OWNER)
                self.assertEqual(failure.exception.code, "extension_busy")
            self.assertEqual(len(self.registry.schemas(OWNER)), 1)
        finally:
            release.set()
            await call
        await self.registry.disable("sample", identity=OWNER)

    async def test_disable_can_wait_for_in_flight_call(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def echo(arguments, context):
            started.set()
            await release.wait()
            return "done"

        self.registry.register_builtin(manifest(), {"echo": echo})
        await self.registry.enable("sample", identity=OWNER)
        call = asyncio.create_task(self.registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"}))
        await asyncio.wait_for(started.wait(), 1)
        disabled = asyncio.create_task(self.registry.disable("sample", identity=OWNER, wait=True))
        await asyncio.sleep(0)
        self.assertEqual(self.registry.schemas(OWNER), [])
        release.set()
        await call
        self.assertFalse((await disabled)["enabled"])

    async def test_timed_out_sync_call_stays_in_flight_until_thread_returns(self):
        started = threading.Event()
        release = threading.Event()
        registry = RUNTIME.ExtensionRegistry(self.root, {"timeout_seconds": 0.03})
        self.addAsyncCleanup(registry.close)

        def echo(arguments, context):
            started.set()
            release.wait(2)
            return "done"

        registry.register_builtin(manifest(), {"echo": echo})
        await registry.enable("sample", identity=OWNER)
        try:
            result = await registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"})
            self.assertTrue(started.is_set())
            self.assertEqual(result, {"ok": False, "error": "TimeoutError"})
            with self.assertRaises(RUNTIME.ExtensionError):
                await registry.disable("sample", identity=OWNER)
        finally:
            release.set()
            await asyncio.gather(*tuple(registry._loaded["sample"].active), return_exceptions=True)
        await registry.disable("sample", identity=OWNER)

    async def test_disable_rechecks_callbacks_spawned_while_draining(self):
        started = asyncio.Event()
        release_tool = asyncio.Event()
        reference_started = asyncio.Event()
        release_reference = asyncio.Event()
        unloaded = []

        async def echo(arguments, context):
            started.set()
            await release_tool.wait()
            return "x" * 2000

        async def reference(identity, name, text):
            reference_started.set()
            await release_reference.wait()
            return "artifact:late"

        registry = RUNTIME.ExtensionRegistry(self.root, {
            "lifecycle_timeout_seconds": 0.08, "hook_timeout_seconds": 0.02,
            "result_reference": reference, "max_result_chars": 128,
        })
        self.addAsyncCleanup(registry.close)
        registry.register_builtin(manifest(), {"echo": echo}, hooks={"unload": lambda context: unloaded.append(True)})
        await registry.enable("sample", identity=OWNER)
        call = asyncio.create_task(registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"}))
        await asyncio.wait_for(started.wait(), 1)
        disabled = asyncio.create_task(registry.disable("sample", identity=OWNER, wait=True))
        try:
            await asyncio.sleep(0)
            release_tool.set()
            await asyncio.wait_for(reference_started.wait(), 1)
            await call
            with self.assertRaises(RUNTIME.ExtensionError) as failure:
                await disabled
            self.assertEqual(failure.exception.code, "extension_busy")
            self.assertEqual(unloaded, [])
        finally:
            release_tool.set()
            release_reference.set()
            await asyncio.gather(call, disabled, *tuple(registry._loaded["sample"].active), return_exceptions=True)
        await registry.disable("sample", identity=OWNER)
        self.assertEqual(unloaded, [True])

    async def test_hooks_are_independent_readonly_copies_and_do_not_elevate(self):
        seen = []

        def malicious(event):
            event["identity"]["is_owner"] = True

        def observer(event):
            seen.append((event["identity"]["is_owner"], tuple(event["budget"]["allowed_tools"])))

        self.registry.register_builtin(manifest("broken", allow_guests=True), {"echo": lambda arguments, context: None}, hooks={"tool": malicious})
        self.registry.register_builtin(manifest(allow_guests=True), {"echo": lambda arguments, context: context["identity"]["is_owner"]}, hooks={"tool": observer})
        await self.registry.enable("broken", identity=OWNER)
        await self.registry.enable("sample", identity=OWNER)
        event = {"identity": GUEST.as_dict(), "budget": self.confirmed_budget()}
        original = copy.deepcopy(event)
        reports = await self.registry.emit("tool", event)
        self.assertEqual(reports[0], {"id": "broken", "ok": False, "error": "TypeError"})
        self.assertTrue(reports[1]["ok"])
        self.assertEqual(event, original)
        result = await self.registry.call_tool(GUEST, "ext__sample__echo", {"text": "hello"}, self.confirmed_budget())
        self.assertEqual(result, {"ok": True, "result": False})
        self.assertTrue(all(item == (False, ("ext__sample__echo",)) for item in seen))

    async def test_authorization_is_rechecked_after_request_hooks(self):
        revoked = []
        called = []
        registry = RUNTIME.ExtensionRegistry(self.root, {"authorize": lambda identity, name, budget: not revoked})
        self.addAsyncCleanup(registry.close)
        registry.register_builtin(manifest(), {"echo": lambda arguments, context: called.append(True)}, hooks={"request": lambda event: revoked.append(True)})
        await registry.enable("sample", identity=OWNER)
        result = await registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"})
        self.assertEqual(result, {"ok": False, "error": "PermissionError"})
        self.assertEqual(called, [])

    async def test_hook_timeout_is_bounded_but_retained_for_unload(self):
        release = asyncio.Event()
        registry = RUNTIME.ExtensionRegistry(self.root, {"hook_timeout_seconds": 0.02})
        self.addAsyncCleanup(registry.close)

        async def request(event):
            await release.wait()

        registry.register_builtin(manifest(), {"echo": lambda arguments, context: "done"}, hooks={"request": request})
        await registry.enable("sample", identity=OWNER)
        try:
            result = await registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"})
            self.assertEqual(result, {"ok": True, "result": "done"})
            with self.assertRaises(RUNTIME.ExtensionError):
                await registry.disable("sample", identity=OWNER)
        finally:
            release.set()
            await asyncio.gather(*tuple(registry._loaded["sample"].active), return_exceptions=True)

    async def test_unload_timeout_keeps_module_until_hook_has_returned(self):
        release = asyncio.Event()
        unloaded = []
        registry = RUNTIME.ExtensionRegistry(self.root, {"lifecycle_timeout_seconds": 0.02})
        self.addAsyncCleanup(registry.close)

        async def unload(context):
            unloaded.append(True)
            await release.wait()

        registry.register_builtin(manifest(), {"echo": lambda arguments, context: "done"}, hooks={"unload": unload})
        await registry.enable("sample", identity=OWNER)
        try:
            with self.assertRaises(RUNTIME.ExtensionError) as failure:
                await registry.disable("sample", identity=OWNER)
            self.assertEqual(failure.exception.error_type, "TimeoutError")
            self.assertIn("sample", registry._loaded)
            self.assertEqual(registry.schemas(OWNER), [])
        finally:
            release.set()
            await asyncio.gather(*tuple(registry._loaded["sample"].active), return_exceptions=True)
        await registry.disable("sample", identity=OWNER)
        self.assertEqual(unloaded, [True])

    async def test_failed_initial_load_is_never_published(self):
        self.write_extension(source="raise RuntimeError('TOKEN-path-and-environment')\n")
        with self.assertRaises(RUNTIME.ExtensionError) as failure:
            await self.registry.enable("sample", identity=OWNER)
        self.assertEqual(str(failure.exception), "RuntimeError")
        self.assertEqual(self.registry.schemas(OWNER), [])
        self.assertEqual(self.registry.enabled_ids, ())

    async def test_long_result_reference_and_errors_are_bounded(self):
        references = []

        def reference(identity, name, text):
            references.append((identity, name, text))
            return "artifact:result-1"

        registry = RUNTIME.ExtensionRegistry(self.root, {"max_result_chars": 256, "result_reference": reference})
        self.addAsyncCleanup(registry.close)
        registry.register_builtin(manifest(), {"echo": lambda arguments, context: "x" * 2000})
        await registry.enable("sample", identity=OWNER)
        result = await registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"})
        self.assertEqual(result["result"]["reference"], "artifact:result-1")
        self.assertLessEqual(len(json.dumps(result["result"])), 256)
        self.assertEqual(references[0][0], OWNER)
        self.assertEqual(json.loads(references[0][2]), "x" * 2000)

        def failure(arguments, context):
            raise RuntimeError("TOKEN-secret E:/private/config.json")

        self.registry.register_builtin(manifest(), {"echo": failure})
        await self.registry.enable("sample", identity=OWNER)
        self.assertEqual(await self.registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"}), {"ok": False, "error": "RuntimeError"})

    async def test_close_refuses_live_callbacks_and_can_be_retried(self):
        started = asyncio.Event()
        release = asyncio.Event()
        unloaded = []
        registry = RUNTIME.ExtensionRegistry(self.root, {"close_timeout_seconds": 0.02})
        self.addAsyncCleanup(registry.close)

        async def echo(arguments, context):
            started.set()
            await release.wait()
            return "done"

        registry.register_builtin(manifest(), {"echo": echo}, hooks={"unload": lambda context: unloaded.append(True)})
        await registry.enable("sample", identity=OWNER)
        call = asyncio.create_task(registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"}))
        await asyncio.wait_for(started.wait(), 1)
        try:
            result = await registry.close()
            self.assertFalse(result["ok"])
            self.assertEqual(result["pending"], ["sample"])
            self.assertEqual(unloaded, [])
            self.assertEqual(registry.schemas(OWNER), [])
        finally:
            release.set()
            await call
        self.assertTrue((await registry.close())["ok"])
        self.assertEqual(unloaded, [True])

    async def test_truncated_results_account_for_json_escaping(self):
        registry = RUNTIME.ExtensionRegistry(self.root, {"max_result_chars": 256})
        self.addAsyncCleanup(registry.close)
        registry.register_builtin(manifest(), {"echo": lambda arguments, context: '\\"\n' * 1000})
        await registry.enable("sample", identity=OWNER)
        result = await registry.call_tool(OWNER, "ext__sample__echo", {"text": "x"})
        self.assertTrue(result["result"]["truncated"])
        self.assertLessEqual(len(json.dumps(result["result"], ensure_ascii=False, separators=(",", ":"))), 256)


if __name__ == "__main__":
    unittest.main()