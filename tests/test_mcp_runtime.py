from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import sys
import tempfile
import textwrap
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import anyio


PACKAGE = "catty_mcp_runtime_tests"
ROOT = Path(__file__).resolve().parents[1] / "src/catty_qq_ai"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
for module_name in ("task_contracts", "mcp_runtime"):
    name = f"{PACKAGE}.{module_name}"
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
MODULE = sys.modules[f"{PACKAGE}.mcp_runtime"]
CONTRACTS = sys.modules[f"{PACKAGE}.task_contracts"]
OWNER = CONTRACTS.TaskIdentity("owner", "private:owner", is_owner=True)
GUEST = CONTRACTS.TaskIdentity("guest", "group:test")


class FakeSDK:
    def __init__(self):
        self.tools = [{"name": "lookup", "description": "Find a record", "inputSchema": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        }, "annotations": {"readOnlyHint": True}}]
        self.result = {"content": [{"type": "text", "text": "found"}], "isError": False}
        self.calls = []
        self.transport_entries = []
        self.transport_exits = []
        self.session_entries = []
        self.session_exits = []
        self.initializations = 0
        self.failed_initializations = 0
        self.started = asyncio.Event()
        self.hang = False
        self.failure = None
        self.parameters = []
        self.http_options = []
        self.http_urls = []
        self.client_closed = 0
        self.StdioServerParameters = lambda **values: types.SimpleNamespace(**values)
        self.PaginatedRequestParams = lambda **values: types.SimpleNamespace(**values)

    @asynccontextmanager
    async def stdio_client(self, parameters, *, errlog):
        self.parameters.append(parameters)
        self.transport_entries.append(asyncio.current_task())
        try:
            with anyio.CancelScope():
                yield object(), object()
        finally:
            self.transport_exits.append(asyncio.current_task())

    @asynccontextmanager
    async def AsyncClient(self, **options):
        self.http_options.append(options)
        try:
            yield object()
        finally:
            self.client_closed += 1

    @asynccontextmanager
    async def streamable_http_client(self, url, *, http_client, terminate_on_close):
        self.http_urls.append(url)
        if not terminate_on_close:
            raise AssertionError("session must be terminated")
        self.transport_entries.append(asyncio.current_task())
        try:
            yield object(), object(), lambda: "session-id"
        finally:
            self.transport_exits.append(asyncio.current_task())

    def ClientSession(self, read, write, *, read_timeout_seconds):
        sdk = self

        class Session:
            async def __aenter__(self):
                sdk.session_entries.append(asyncio.current_task())
                self.scope = anyio.CancelScope()
                self.scope.__enter__()
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                sdk.session_exits.append(asyncio.current_task())
                return self.scope.__exit__(exc_type, exc, traceback)

            async def initialize(self):
                sdk.initializations += 1
                if sdk.initializations <= sdk.failed_initializations:
                    raise RuntimeError("secret-connect-error")

            async def list_tools(self, **kwargs):
                return {"tools": sdk.tools}

            async def call_tool(self, name, arguments):
                sdk.calls.append((name, arguments))
                sdk.started.set()
                if sdk.hang:
                    await asyncio.Event().wait()
                if sdk.failure:
                    raise RuntimeError(sdk.failure)
                return sdk.result

        return Session()


class MCPRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sdk = FakeSDK()
        self.patch = patch.object(MODULE, "_load_sdk", return_value=self.sdk)
        self.patch.start()
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.close()
        self.patch.stop()

    def runtime(self, **changes):
        server = {"id": "docs", "transport": "stdio", "command": "ADMIN_EXECUTABLE", "args": ["--stdio"],
                  "allowed_tools": ["lookup"], "allow_guests": True, "timeout_seconds": 1}
        server.update(changes)
        runtime = MODULE.MCPRuntime({"enabled": True, "servers": [server]}, CONTRACTS.TaskPolicy())
        self.runtimes.append(runtime)
        return runtime

    def context(self, identity=GUEST, **changes):
        return MODULE.MCPCallContext(identity, "root-task", True, **changes)

    async def test_default_is_offline_without_sdk(self):
        runtime = MODULE.MCPRuntime({}, CONTRACTS.TaskPolicy())
        self.runtimes.append(runtime)
        with patch.object(MODULE, "_load_sdk", side_effect=AssertionError("must remain offline")):
            await runtime.start()
            self.assertEqual(await runtime.list_tools(OWNER), [])

    async def test_start_list_call_and_close_keep_scope_owner(self):
        runtime = self.runtime()
        await asyncio.create_task(runtime.start())
        tools = await asyncio.create_task(runtime.list_tools(GUEST))
        self.assertEqual(tools[0]["name"], "mcp__docs__lookup")
        self.assertEqual(tools[0]["inputSchema"]["required"], ["query"])
        result = await asyncio.create_task(runtime.call_tool(GUEST, "docs", tools[0]["name"], {"query": "hello"}, self.context()))
        self.assertEqual(result["content"][0]["text"], "found")
        await asyncio.create_task(runtime.close())
        self.assertEqual(self.sdk.transport_entries, self.sdk.transport_exits)
        self.assertEqual(self.sdk.session_entries, self.sdk.session_exits)
        self.assertIsNot(self.sdk.transport_entries[0], asyncio.current_task())
        self.assertEqual(self.sdk.parameters[0].args, ["--stdio"])
        self.assertFalse(hasattr(self.sdk.parameters[0], "shell"))

    async def test_http_is_admin_configured_without_proxy_or_redirects(self):
        runtime = self.runtime(transport="streamable_http", url="http://127.0.0.1:8200/mcp", command="", args=[])
        await runtime.start()
        await runtime.call_tool(OWNER, "docs", "lookup", {"query": "local"})
        await runtime.close()
        self.assertEqual(self.sdk.http_urls, ["http://127.0.0.1:8200/mcp"])
        self.assertFalse(self.sdk.http_options[0]["trust_env"])
        self.assertFalse(self.sdk.http_options[0]["follow_redirects"])
        self.assertEqual(self.sdk.client_closed, 1)

    async def test_guest_context_and_identity_are_required(self):
        runtime = self.runtime()
        await runtime.start()
        other = CONTRACTS.TaskIdentity("other", "group:test")
        contexts = [None, self.context(other), MODULE.MCPCallContext(GUEST, "task", False)]
        for context in contexts:
            with self.subTest(context=context), self.assertRaises(CONTRACTS.TaskError) as caught:
                await runtime.call_tool(GUEST, "docs", "lookup", {"query": "test"}, context)
            self.assertEqual(caught.exception.code, "mcp_unconfirmed_task")
        with self.assertRaises(CONTRACTS.TaskError):
            await runtime.call_tool({"is_owner": True}, "docs", "lookup", {})
        self.assertEqual(self.sdk.calls, [])

    async def test_guest_flag_and_allowlist_apply_outside_schema(self):
        runtime = self.runtime(allow_guests=False)
        await runtime.start()
        self.assertEqual(await runtime.list_tools(GUEST), [])
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await runtime.call_tool(GUEST, "docs", "lookup", {"query": "test"}, self.context())
        self.assertEqual(caught.exception.code, "mcp_forbidden")
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await runtime.call_tool(OWNER, "docs", "delete", {})
        self.assertEqual(caught.exception.code, "mcp_forbidden")
        self.assertEqual(self.sdk.calls, [])

    async def test_unknown_server_and_tool_are_rejected(self):
        runtime = self.runtime(allowed_tools=["*"])
        await runtime.start()
        for server, name, code in [("missing", "lookup", "mcp_unknown_server"), ("docs", "missing", "mcp_unknown_tool")]:
            with self.subTest(server=server, name=name), self.assertRaises(CONTRACTS.TaskError) as caught:
                await runtime.call_tool(OWNER, server, name, {})
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.sdk.calls, [])

    async def test_schema_invalid_arguments_never_reach_server(self):
        runtime = self.runtime()
        await runtime.start()
        for arguments in ({}, {"query": 1}, {"query": "x", "command": "not-allowed"}):
            with self.subTest(arguments=arguments), self.assertRaises(CONTRACTS.TaskError) as caught:
                await runtime.call_tool(OWNER, "docs", "lookup", arguments)
            self.assertEqual(caught.exception.code, "mcp_invalid_arguments")
        self.assertEqual(self.sdk.calls, [])

    async def test_invalid_schema_fails_and_closes(self):
        for schema in ({"type": "unknown"}, {"type": "object", "required": "query"}, {"type": "object", "$ref": "https://example.invalid/schema"}):
            self.sdk.tools[0]["inputSchema"] = schema
            runtime = self.runtime()
            with self.subTest(schema=schema), self.assertRaises(CONTRACTS.TaskError) as caught:
                await runtime.start()
            self.assertEqual(caught.exception.code, "mcp_invalid_schema")
        self.assertEqual(self.sdk.transport_entries, self.sdk.transport_exits)

    async def test_external_output_schema_is_rejected_before_sdk_fetch(self):
        self.sdk.tools[0]["outputSchema"] = {"type": "object", "$ref": "file:///secret.json"}
        runtime = self.runtime()
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await runtime.start()
        self.assertEqual(caught.exception.code, "mcp_invalid_schema")
        self.assertEqual(self.sdk.calls, [])

    async def test_namespace_cannot_replace_builtin_tools(self):
        self.sdk.tools[0]["name"] = "catty_read_file"
        runtime = self.runtime(allowed_tools=["catty_read_file"])
        await runtime.start()
        self.assertEqual((await runtime.list_tools(OWNER))[0]["name"], "mcp__docs__catty_read_file")
        with self.assertRaises(CONTRACTS.TaskError):
            await runtime.call_tool(OWNER, "docs", "mcp__other__catty_read_file", {})

    async def test_timeout_does_not_replay_and_closes_transport(self):
        self.sdk.hang = True
        runtime = self.runtime(timeout_seconds=0.05)
        await runtime.start()
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await runtime.call_tool(OWNER, "docs", "lookup", {"query": "x"})
        self.assertEqual(caught.exception.code, "mcp_timeout")
        self.assertEqual(len(self.sdk.calls), 1)
        self.assertEqual(self.sdk.transport_entries, self.sdk.transport_exits)

    async def test_cancelled_call_cleans_transport(self):
        self.sdk.hang = True
        runtime = self.runtime()
        await runtime.start()
        task = asyncio.create_task(runtime.call_tool(OWNER, "docs", "lookup", {"query": "x"}))
        await self.sdk.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.sdk.calls), 1)
        self.assertEqual(self.sdk.transport_entries, self.sdk.transport_exits)

    async def test_close_unblocks_an_inflight_call(self):
        self.sdk.hang = True
        runtime = self.runtime()
        await runtime.start()
        task = asyncio.create_task(runtime.call_tool(OWNER, "docs", "lookup", {"query": "x"}))
        await self.sdk.started.wait()
        await runtime.close()
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await task
        self.assertEqual(caught.exception.code, "mcp_disconnected")
        self.assertEqual(self.sdk.transport_entries, self.sdk.transport_exits)

    async def test_retired_connection_rejects_without_queueing(self):
        runtime = self.runtime()
        await runtime.start()
        connection = runtime._connections["docs"]
        await runtime.close()
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await connection.request("list", OWNER)
        self.assertEqual(caught.exception.code, "mcp_disconnected")
        self.assertTrue(connection.queue.empty())

    async def test_start_retry_is_bounded(self):
        self.sdk.failed_initializations = 20
        runtime = self.runtime()
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await runtime.start()
        self.assertEqual(caught.exception.code, "mcp_connection_failed")
        self.assertEqual(self.sdk.initializations, 2)
        self.assertEqual(self.sdk.transport_entries, self.sdk.transport_exits)

    async def test_explicit_later_call_can_reconnect_but_failed_call_is_not_replayed(self):
        runtime = self.runtime()
        await runtime.start()
        self.sdk.failure = "bad token=hidden"
        with self.assertRaises(CONTRACTS.TaskError):
            await runtime.call_tool(OWNER, "docs", "lookup", {"query": "x"})
        self.assertEqual(len(self.sdk.calls), 1)
        self.sdk.failure = None
        await runtime.call_tool(OWNER, "docs", "lookup", {"query": "next"})
        self.assertEqual(len(self.sdk.calls), 2)
        self.assertEqual(self.sdk.initializations, 2)

    async def test_secrets_are_redacted_from_inventory_results_logs_and_errors(self):
        secret = "test-private-credential"
        self.sdk.tools[0]["description"] = f"Description {secret}"
        self.sdk.result = {"content": [{"type": "text", "text": f"{secret} token=other-private"}],
                           "structuredContent": {"env": {"PASSWORD": secret}, "api_key": secret}}
        runtime = self.runtime(env={"TOKEN": secret})
        await runtime.start()
        inventory = await runtime.list_tools(OWNER)
        result = await runtime.call_tool(OWNER, "docs", "lookup", {"query": "x"})
        serialized = json.dumps([inventory, result])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("other-private", serialized)
        self.assertNotIn("PASSWORD", serialized)
        record = logging.LogRecord("httpx", logging.ERROR, "", 0, "request %s", (secret,), None)
        runtime._log_filter.filter(record)
        self.assertNotIn(secret, record.getMessage())
        record = logging.LogRecord("mcp.client.streamable_http", logging.INFO, "", 0, "Received session ID: session-private", (), None)
        runtime._log_filter.filter(record)
        self.assertNotIn("session-private", record.getMessage())
        self.sdk.failure = secret
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await runtime.call_tool(OWNER, "docs", "lookup", {"query": "x"})
        self.assertNotIn(secret, str(caught.exception))

    async def test_images_and_embedded_resources_use_artifacts_without_fetching(self):
        image = b"image-bytes"
        resource = b"resource-bytes"
        self.sdk.result = {"content": [
            {"type": "text", "text": "caption"},
            {"type": "image", "data": base64.b64encode(image).decode(), "mimeType": "image/png"},
            {"type": "resource", "resource": {"uri": "file:///not-readable", "blob": base64.b64encode(resource).decode(), "mimeType": "application/octet-stream"}},
            {"type": "resource_link", "uri": "file:///never-fetch", "name": "reference"},
        ]}
        written = []

        async def writer(name, data, mime_type):
            written.append((name, data, mime_type))
            return {"id": name, "uri": f"artifact:{name}"}

        runtime = self.runtime()
        await runtime.start()
        result = await runtime.call_tool(GUEST, "docs", "lookup", {"query": "x"}, self.context(artifact_writer=writer))
        self.assertEqual([item[1] for item in written], [image, resource])
        self.assertEqual([item["type"] for item in result["content"]], ["text", "image", "resource", "resource_link"])
        self.assertNotIn("data", result["content"][1])
        self.assertNotIn("blob", result["content"][2]["resource"])
        self.assertEqual(result["content"][3]["uri"], "file:///never-fetch")

    async def test_large_results_are_stored_not_inlined(self):
        self.sdk.result = {"content": [{"type": "text", "text": "x" * 4000}], "isError": True}
        stored = []

        async def writer(name, data, mime_type):
            stored.append(json.loads(data))
            return {"id": "artifact-result"}

        runtime = self.runtime(max_result_bytes=512)
        await runtime.start()
        result = await runtime.call_tool(GUEST, "docs", "lookup", {"query": "x"}, self.context(artifact_writer=writer))
        self.assertLess(len(json.dumps(result)), 512)
        self.assertTrue(result["isError"])
        self.assertEqual(result["artifact"]["id"], "artifact-result")
        self.assertEqual(stored[0]["content"][0]["text"], "x" * 4000)

    async def test_binary_requires_task_artifact_context(self):
        self.sdk.result = {"content": [{"type": "image", "data": "YWJj", "mimeType": "image/png"}]}
        runtime = self.runtime()
        await runtime.start()
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await runtime.call_tool(OWNER, "docs", "lookup", {"query": "x"})
        self.assertEqual(caught.exception.code, "mcp_artifact_required")

    async def test_cancellation_during_artifact_write_closes_transport(self):
        self.sdk.result = {"content": [{"type": "image", "data": "YWJj", "mimeType": "image/png"}]}
        writing = asyncio.Event()

        async def writer(name, data, mime_type):
            writing.set()
            await asyncio.Event().wait()

        runtime = self.runtime()
        await runtime.start()
        task = asyncio.create_task(runtime.call_tool(GUEST, "docs", "lookup", {"query": "x"}, self.context(artifact_writer=writer)))
        await writing.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.sdk.transport_entries, self.sdk.transport_exits)

    async def test_malformed_results_are_rejected(self):
        runtime = self.runtime()
        await runtime.start()
        for content in ([{"type": "text", "text": 1}], [{"type": "image"}], [{"type": "image", "data": "not base64"}],
                        [{"type": "resource", "resource": {"uri": "file:///x"}}], [{"type": "unknown"}]):
            self.sdk.result = {"content": content}
            with self.subTest(content=content), self.assertRaises(CONTRACTS.TaskError) as caught:
                await runtime.call_tool(OWNER, "docs", "lookup", {"query": "x"})
            self.assertEqual(caught.exception.code, "mcp_invalid_response")

    async def test_result_reference_also_obeys_result_limit(self):
        runtime = self.runtime(max_result_bytes=512)
        self.sdk.result = {"content": [{"type": "text", "text": "x" * 4000}]}

        async def writer(name, data, mime_type):
            return {"id": "x" * 1000}

        await runtime.start()
        with self.assertRaises(CONTRACTS.TaskError) as caught:
            await runtime.call_tool(GUEST, "docs", "lookup", {"query": "x"}, self.context(artifact_writer=writer))
        self.assertEqual(caught.exception.code, "mcp_result_too_large")

    async def test_config_rejects_runtime_addresses_shell_and_ambiguous_names(self):
        for changes in ({"id": "catty__builtin"}, {"url": "file:///secret", "transport": "http", "command": "", "args": []},
                        {"url": "https://user:secret@host/mcp", "transport": "http", "command": "", "args": []},
                        {"shell": True}, {"allowed_tools": ["bad.tool"]}, {"allow_guests": "true"}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, CONTRACTS.TaskError)):
                self.runtime(**changes)


class MCPRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_stdio_roundtrip_and_process_exit(self):
        from mcp.client import stdio as sdk_stdio

        spawned = []
        real_spawn = sdk_stdio._create_platform_compatible_process

        async def capture_spawn(**kwargs):
            process = await real_spawn(**kwargs)
            spawned.append(process)
            return process

        with tempfile.TemporaryDirectory(prefix="catty-mcp-fixture-") as directory:
            fixture = Path(directory) / "fixture_server.py"
            fixture.write_text(textwrap.dedent("""
                from mcp.server.fastmcp import FastMCP

                server = FastMCP("catty-stdio-fixture")

                @server.tool()
                def echo(text: str) -> str:
                    return text

                @server.tool()
                def add(left: int, right: int) -> int:
                    return left + right

                if __name__ == "__main__":
                    server.run(transport="stdio")
            """), encoding="utf-8")
            runtime = MODULE.MCPRuntime({"enabled": True, "servers": [{
                "id": "fixture", "transport": "stdio", "command": sys.executable,
                "args": ["-u", str(fixture)], "allowed_tools": ["echo", "add"],
                "timeout_seconds": 20,
            }]}, CONTRACTS.TaskPolicy())
            with patch.object(sdk_stdio, "_create_platform_compatible_process", side_effect=capture_spawn):
                try:
                    await asyncio.create_task(runtime.start())
                    self.assertEqual(len(spawned), 1)
                    self.assertIsNone(spawned[0].returncode)
                    inventory = await runtime.list_tools(OWNER)
                    self.assertEqual({tool["name"] for tool in inventory}, {"mcp__fixture__echo", "mcp__fixture__add"})
                    echoed = await runtime.call_tool(OWNER, "fixture", "echo", {"text": "stdio-roundtrip"})
                    self.assertIn("stdio-roundtrip", " ".join(item["text"] for item in echoed["content"] if item["type"] == "text"))
                    added = await runtime.call_tool(OWNER, "fixture", "add", {"left": 19, "right": 23})
                    self.assertEqual(json.loads(added["content"][0]["text"]), 42)
                finally:
                    await asyncio.create_task(runtime.close())
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].returncode)


if __name__ == "__main__":
    unittest.main()