from __future__ import annotations

import importlib.util
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI, HTTPException, Request
import httpx


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_task_dashboard_fixture"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(PACKAGE, package)
spec = importlib.util.spec_from_file_location(PACKAGE + ".task_dashboard", ROOT / "src/catty_qq_ai/task_dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dashboard
spec.loader.exec_module(dashboard)


class ConfigEditorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        self.original = {"ai": {"api_key": "TOKEN-KEEP-PRIVATE"}, "agent_tasks": {"enabled": True}}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.editor = dashboard.ConfigEditor(self.path)

    def test_read_redacted_and_compare_and_swap(self):
        snapshot = self.editor.read()
        self.assertNotIn("TOKEN-KEEP-PRIVATE", json.dumps(snapshot))
        result = self.editor.update({"agent_tasks": {"enabled": False}}, snapshot["sha256"])
        self.assertNotEqual(result["sha256"], snapshot["sha256"])
        with self.assertRaises(dashboard.ConfigError) as error:
            self.editor.update({"agent_tasks": {"enabled": True}}, snapshot["sha256"])
        self.assertEqual(error.exception.status_code, 409)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["ai"], self.original["ai"])
        self.assertFalse(saved["agent_tasks"]["enabled"])

    def test_strict_schema_and_scope(self):
        for patch in (
            {"ai": {"api_key": "replace"}},
            {"agent_tasks": {"enabled": "true"}},
            {"agent_tasks": {"unrecognized": 1}},
            {"agent_tasks": {"global_concurrency": True}},
            {"agent_tasks": {"tiers": {"light": {"points": -1}}}},
            {"agent_tasks": {"worker_model": "other-model"}},
        ):
            with self.subTest(patch=patch), self.assertRaises(dashboard.ConfigError):
                self.editor.update(patch, self.editor.read()["sha256"])
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_backup_limit_and_rollback_preserve_other_config(self):
        first = self.editor.update({"agent_tasks": {"enabled": False}}, self.editor.read()["sha256"])
        backup_id = first["backups"][0]
        updated = json.loads(self.path.read_text())
        updated["ai"]["api_key"] = "NEW-TOKEN-PRIVATE"
        self.path.write_text(json.dumps(updated), encoding="utf-8")
        self.editor.rollback(backup_id, self.editor.read()["sha256"])
        self.assertTrue(json.loads(self.path.read_text())["agent_tasks"]["enabled"])
        self.assertEqual(json.loads(self.path.read_text())["ai"]["api_key"], "NEW-TOKEN-PRIVATE")
        for count in range(12):
            self.editor.update({"agent_tasks": {"daily_attempt_limit": count + 1}}, self.editor.read()["sha256"])
        self.assertEqual(len(self.editor.read()["backups"]), 10)

    def test_redactor_filters_credential_values_anywhere(self):
        result = dashboard.Redactor(["TOKEN-KEEP-PRIVATE"]).redact({
            "apiKey": "other", "nested": [{"label": "copy TOKEN-KEEP-PRIVATE"}],
            "description": "Bearer bearer-secret token=literal-secret", "messages": [{"text": "private chat"}],
        })
        encoded = json.dumps(result)
        for secret in ("TOKEN-KEEP-PRIVATE", "other", "bearer-secret", "literal-secret", "private chat"):
            self.assertNotIn(secret, encoded)

    def test_token_documentation_is_not_interpreted_as_a_credential(self):
        text = "Cumulative token budget | per-task limit"
        secrets = dashboard._secret_values({"snippet": text, "args": "--token REAL-SECRET"})
        self.assertNotIn("budget", secrets)
        self.assertNotIn("|", secrets)
        self.assertEqual(dashboard.Redactor(secrets).text(text), text)
        self.assertNotIn("REAL-SECRET", dashboard.Redactor(secrets).text("--token REAL-SECRET"))

    def test_validator_rejection_and_nonfinite_values_do_not_write(self):
        original = self.path.read_bytes()
        rejected = dashboard.ConfigEditor(self.path, validator=lambda value: False)
        with self.assertRaises(dashboard.ConfigError):
            rejected.update({"agent_tasks": {"enabled": False}}, self.editor.read()["sha256"])
        with self.assertRaises(dashboard.ConfigError):
            self.editor.update({"agent_tasks": {"daily_token_limit": float("nan")}}, self.editor.read()["sha256"])
        self.assertEqual(original, self.path.read_bytes())

    def test_atomic_replace_failure_preserves_config_and_cleans_temp(self):
        original = self.path.read_bytes()
        replace = dashboard.os.replace
        target = self.path.resolve()

        def fail_config_replace(source, destination):
            if Path(destination).resolve() == target:
                raise OSError("fixture write failure")
            return replace(source, destination)

        with patch.object(dashboard.os, "replace", side_effect=fail_config_replace):
            with self.assertRaises(OSError):
                self.editor.update({"agent_tasks": {"enabled": False}}, self.editor.read()["sha256"])
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(list(self.path.parent.glob(".config.json.*")), [])

    def test_rollback_cas_and_backup_path_guard(self):
        previous = self.editor.read()["sha256"]
        current = self.editor.update({"agent_tasks": {"enabled": False}}, previous)
        with self.assertRaises(dashboard.ConfigError) as error:
            self.editor.rollback(current["backups"][0], previous)
        self.assertEqual(error.exception.status_code, 409)
        with self.assertRaises(dashboard.ConfigError):
            self.editor.rollback("../config.json", current["sha256"])

    def test_secret_keys_cannot_be_patched_even_when_empty(self):
        for value in ("changed", "", None, dashboard.REDACTED):
            with self.subTest(value=value), self.assertRaises(dashboard.ConfigError):
                self.editor.update({"agent_tasks": {"api_key": value}}, self.editor.read()["sha256"])

    def test_module_configuration_is_strict_and_validates_without_starting_services(self):
        modules = {
            "mcp": {"enabled": True, "servers": [{"id": "local", "transport": "stdio", "command": "python", "args": [], "enabled": True}]},
            "knowledge": {"enabled": True, "max_file_bytes": 1048576, "chunk_size": 800, "chunk_overlap": 100},
            "extensions": {"enabled_ids": ["sample"], "root": "extensions"},
        }
        result = self.editor.update({"agent_tasks": modules}, self.editor.read()["sha256"])
        self.assertEqual(result["config"]["agent_tasks"]["extensions"]["enabled_ids"], ["sample"])
        for patch_value in (
            {"mcp": {"enabled": "true"}},
            {"mcp": {"servers": [{"id": "bad", "command": "python", "timeout_seconds": True}]}},
            {"knowledge": {"max_file_bytes": True}},
            {"knowledge": {"chunk_size": 10}},
            {"knowledge": {"unknown": 1}},
            {"extensions": {"enabled_ids": ["../bad"]}},
            {"extensions": {"enabled_ids": ["sample", "sample"]}},
            {"extensions": {"unknown": True}},
        ):
            with self.subTest(value=patch_value), self.assertRaises(dashboard.ConfigError):
                self.editor.update({"agent_tasks": patch_value}, self.editor.read()["sha256"])

    def test_redacted_module_environment_roundtrip_retains_all_secrets(self):
        config = {"ai": {"api_key": "TOKEN-KEEP-PRIVATE"}, "agent_tasks": {"mcp": {"servers": [
            {"id": "local", "command": "python", "env": {"CUSTOM": "ENV-SECRET-KEEP"}},
        ]}}}
        self.path.write_text(json.dumps(config), encoding="utf-8")
        snapshot = self.editor.read()
        self.assertNotIn("ENV-SECRET-KEEP", json.dumps(snapshot))
        changed = snapshot["config"]["agent_tasks"]
        changed["mcp"]["enabled"] = True
        self.editor.update({"agent_tasks": changed}, snapshot["sha256"])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["agent_tasks"]["mcp"]["servers"][0]["env"]["CUSTOM"], "ENV-SECRET-KEEP")
        with self.assertRaises(dashboard.ConfigError):
            self.editor.update({"agent_tasks": {"mcp": {"servers": []}}}, self.editor.read()["sha256"])

    def test_url_and_argument_credential_placeholders_roundtrip(self):
        config = {"agent_tasks": {"mcp": {"servers": [
            {"id": "web", "transport": "http", "url": "https://example.test/mcp?token=URL_SECRET"},
            {"id": "local", "command": "python", "args": ["--token", "ARG_SECRET"], "env": {"SECRET": "ARG_SECRET"}},
        ]}}}
        self.path.write_text(json.dumps(config), encoding="utf-8")
        snapshot = self.editor.read()
        for secret in ("URL_SECRET", "ARG_SECRET"):
            self.assertNotIn(secret, json.dumps(snapshot))
        self.editor.update({"agent_tasks": snapshot["config"]["agent_tasks"]}, snapshot["sha256"])
        self.assertEqual(json.loads(self.path.read_text()), config)


class FakeServices:
    def __init__(self, config_path):
        self.config_editor = dashboard.ConfigEditor(config_path)
        self.redaction_secrets = ("TOKEN", "OTHER_SECRET")
        self.calls = []
        self.revision = 1
        self.state = "running"

    def list_tasks(self, **kwargs):
        self.calls.append(("list_tasks", kwargs))
        return {"items": [{"task_id": "task-1", "state": self.state, "user_id": "user-7", "revision": self.revision,
                           "query": "ORIGINAL-CHAT", "content": "PRIVATE-CONTENT", "api_key": "TOKEN",
                           "error_code": "TOKEN", "progress": {"text": "RAW-CHAT", "value": 50}}], "revision": self.revision, "total": 1}

    def get_task(self, *, task_id):
        if task_id != "task-1":
            raise dashboard.TaskError("not_found", "TOKEN raw exception")
        return self.list_tasks()["items"][0]

    def mutate_task(self, *, task_id, action, expected_revision, **kwargs):
        if expected_revision != self.revision:
            raise dashboard.TaskError("revision_conflict", "TOKEN raw exception")
        self.calls.append(("mutate_task", {"task_id": task_id, "action": action, **kwargs}))
        self.revision += 1
        self.state = {"cancel": "cancelled", "resume": "queued", "confirm_delivery": "completed"}[action]
        return {"task_id": task_id, "state": self.state, "revision": self.revision, **kwargs}

    def list_schedules(self):
        return {"items": [{"id": "schedule-1", "enabled": True, "revision": 1}], "revision": 1}

    def mutate_schedule(self, **kwargs):
        self.calls.append(("mutate_schedule", kwargs))
        return {"id": kwargs["schedule_id"], "action": kwargs["action"], "revision": 2}

    def list_knowledge(self):
        return {"items": [{"document_id": "doc-1", "filename": "note.txt", "status": "ready", "revision": 1}], "revision": 1}

    def search_knowledge(self, **kwargs):
        self.calls.append(("search_knowledge", kwargs))
        return {"items": [{"document_id": "doc-1", "score": 0.8, "excerpt": "TOKEN public excerpt"}]}

    def upload_knowledge(self, **kwargs):
        self.calls.append(("upload_knowledge", kwargs))
        return {"document_id": "doc-2", "filename": kwargs["filename"], "revision": 2, "content": "PRIVATE-CONTENT"}

    def delete_knowledge(self, **kwargs):
        self.calls.append(("delete_knowledge", kwargs))
        return {"document_id": kwargs["document_id"], "deleted": True, "revision": 2}

    def rebuild_knowledge(self, **kwargs):
        self.calls.append(("rebuild_knowledge", kwargs))
        return {"rebuilding": True, "revision": 2}

    def list_extensions(self):
        return {"items": [{"id": "ext-1", "enabled": False, "name": "<script>bad()</script>", "revision": 1}], "revision": 1}

    def set_extension_enabled(self, **kwargs):
        self.calls.append(("set_extension_enabled", kwargs))
        return {"id": kwargs["extension_id"], "enabled": kwargs["enabled"], "revision": 2}

    def list_mcp(self):
        return {"items": [{"server_id": "mcp-1", "name": "local", "transport": "stdio", "status": "connected", "api_key": "TOKEN"}]}

    def list_mcp_tools(self, **kwargs):
        self.calls.append(("list_mcp_tools", kwargs))
        return {"items": [{"name": "lookup", "description": "Bearer credential-private", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}}}]}

    async def trace(self, **kwargs):
        self.calls.append(("trace", kwargs))
        return {"items": [{"task_id": "task-1", "event": "step", "error_code": "TOKEN", "messages": "ORIGINAL-CHAT",
                           "text": "KB-ORIGINAL", "token": "OTHER_SECRET", "metadata": "PRIVATE-CONTENT"} for _ in range(300)]}

    def diagnostics(self):
        return {"modules": {"tasks": {"status": "ready", "version": "1", "config": {"api_key": "TOKEN"}}},
                "task_counts": {"running": 1}, "config": {"api_key": "TOKEN"}, "env": {"SECRET": "TOKEN"},
                "chat": "ORIGINAL-CHAT", "knowledge": "KB-ORIGINAL"}


class DashboardRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config_path = Path(self.directory.name) / "config.json"
        config_path.write_text(json.dumps({"ai": {"api_key": "TOKEN"}, "agent_tasks": {"enabled": True}}), encoding="utf-8")
        self.service = FakeServices(config_path)
        self.app = FastAPI()
        self.auth_calls = []

        async def require_admin(request: Request):
            self.auth_calls.append(request.url.path)
            if request.headers.get("x-fixture-admin") != "yes":
                raise HTTPException(401, "Admin required")

        self.assertTrue(dashboard.mount_task_dashboard(self.app, lambda: self.service, require_admin))
        transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 32100))
        self.client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def request(self, method, path, body=None, *, auth=True, headers=None):
        supplied = {"Origin": "http://testserver"}
        if auth:
            supplied["X-Fixture-Admin"] = "yes"
        supplied.update(headers or {})
        return await self.client.request(method, path, json=body, headers=supplied)

    async def test_every_route_including_html_requires_admin(self):
        for route in self.app.routes:
            if not route.path.startswith("/dashboard"):
                continue
            path = route.path
            for name, value in (("task_id", "task-1"), ("schedule_id", "schedule-1"), ("document_id", "doc-1"), ("extension_id", "ext-1"), ("server_id", "mcp-1"), ("action", "cancel")):
                path = path.replace("{" + name + "}", value)
            with self.subTest(path=path):
                response = await self.request(next(iter(route.methods)), path, {"expected_revision": 1}, auth=False)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertNotIn("ORIGINAL-CHAT", response.text)
        self.assertEqual(self.service.calls, [])

    async def test_html_has_csp_local_assets_and_safe_dom(self):
        response = await self.request("GET", "/dashboard/tasks")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("textContent", response.text)
        self.assertNotIn("innerHTML", response.text)
        self.assertNotIn('src="http', response.text)
        self.assertIn('id="panel-tasks"', response.text)

    async def test_task_list_metadata_and_filter_not_identity(self):
        response = await self.request("GET", "/dashboard/api/tasks?owner_id=user-9&status=running&limit=10&offset=2")
        self.assertEqual(response.status_code, 200, response.text)
        for secret in ("TOKEN", "ORIGINAL-CHAT", "PRIVATE-CONTENT", "RAW-CHAT"):
            self.assertNotIn(secret, response.text)
        self.assertEqual(response.json()["data"]["items"][0]["user_id"], "user-7")
        self.assertEqual(self.service.calls[-1], ("list_tasks", {"owner_id": "user-9", "status": "running", "limit": 10, "offset": 2}))
        for query in ("is_owner=true", "user_id=admin", "owner_id=a&owner_id=b", "limit=-1", "limit=10000"):
            response = await self.request("GET", "/dashboard/api/tasks?" + query)
            self.assertEqual(response.status_code, 422, query)

    async def test_task_actions_and_revision_conflict(self):
        for action, expected in (("cancel", "cancelled"), ("resume", "queued")):
            response = await self.request("POST", "/dashboard/api/tasks/task-1/" + action, {"expected_revision": self.service.revision})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["state"], expected)
        response = await self.request("POST", "/dashboard/api/tasks/task-1/cancel", {"expected_revision": 1})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("TOKEN", response.text)
        response = await self.request("POST", "/dashboard/api/tasks/task-1/confirm-delivery", {"expected_revision": self.service.revision, "delivered": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["data"]["delivered"])

    async def test_mutations_need_revision_and_strict_fields(self):
        for body, status in (({}, 428), ({"expected_revision": True}, 422), ({"expected_revision": -1}, 422), ({"expected_revision": 1, "is_owner": True}, 422)):
            response = await self.request("POST", "/dashboard/api/tasks/task-1/cancel", body)
            self.assertEqual(response.status_code, status, response.text)
        response = await self.request("POST", "/dashboard/api/tasks/task-1/confirm-delivery", {"expected_revision": 1})
        self.assertEqual(response.status_code, 422)
        response = await self.request("POST", "/dashboard/api/tasks/task-1/unknown", {"expected_revision": 1})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.service.calls, [])

    async def test_origin_content_type_and_fetch_site_are_enforced(self):
        for headers in ({"Origin": "http://other.example"}, {"Origin": "null"}, {"Origin": "https://testserver"}, {"Origin": "http://testserver/path"}, {"Origin": "http://testserver@evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://testserver:0"}):
            response = await self.request("POST", "/dashboard/api/tasks/task-1/cancel", {"expected_revision": 1}, headers=headers)
            self.assertEqual(response.status_code, 403, str(headers))
        response = await self.client.post("/dashboard/api/tasks/task-1/cancel", json={"expected_revision": 1}, headers={"X-Fixture-Admin": "yes"})
        self.assertEqual(response.status_code, 403)
        response = await self.request("POST", "/dashboard/api/tasks/task-1/cancel", {"expected_revision": 1}, headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.service.calls, [])

    async def test_service_missing_or_false_result_is_explicit_error(self):
        with patch.object(self.service, "list_tasks", None):
            response = await self.request("GET", "/dashboard/api/tasks")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["code"], "service_unavailable")
        with patch.object(self.service, "mutate_task", return_value=True):
            response = await self.request("POST", "/dashboard/api/tasks/task-1/cancel", {"expected_revision": 1})
            self.assertEqual(response.status_code, 502)

    async def test_task_detail_and_not_found(self):
        response = await self.request("GET", "/dashboard/api/tasks/task-1")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("ORIGINAL-CHAT", response.text)
        response = await self.request("GET", "/dashboard/api/tasks/absent")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("TOKEN", response.text)

    async def test_collection_routes(self):
        for path in ("schedules", "knowledge", "extensions", "mcp", "trace", "diagnostics"):
            with self.subTest(path=path):
                response = await self.request("GET", "/dashboard/api/" + path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("TOKEN", response.text)
                self.assertNotIn("ORIGINAL-CHAT", response.text)
        response = await self.request("GET", "/dashboard/api/trace?task_id=task-1&event=step&limit=7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[-1], ("trace", {"task_id": "task-1", "event": "step", "limit": 7}))

    async def test_schedule_and_extension_actions(self):
        for action in ("pause", "resume", "delete"):
            response = await self.request("POST", "/dashboard/api/schedules/schedule-1/" + action, {"expected_revision": 1})
            self.assertEqual(response.status_code, 200, response.text)
        for action in ("enable", "disable"):
            response = await self.request("POST", "/dashboard/api/extensions/ext-1/" + action, {"expected_revision": 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["enabled"], action == "enable")

    async def test_knowledge_upload_search_delete_rebuild(self):
        response = await self.request("POST", "/dashboard/api/knowledge/upload", {"filename": "note.txt", "content_base64": base64.b64encode(b"fixture document").decode(), "expected_revision": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.service.calls[-1][1]["content"], b"fixture document")
        self.assertNotIn("PRIVATE-CONTENT", response.text)
        response = await self.request("GET", "/dashboard/api/knowledge/search?query=fixture")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("TOKEN", response.text)
        response = await self.request("DELETE", "/dashboard/api/knowledge/doc-1", {"expected_revision": 1})
        self.assertEqual(response.status_code, 200)
        response = await self.request("POST", "/dashboard/api/knowledge/rebuild", {"expected_revision": 1})
        self.assertEqual(response.status_code, 200)
        for filename, encoded in (("../bad", "eA=="), ("good.txt", "???"), ("good.txt", "")):
            response = await self.request("POST", "/dashboard/api/knowledge/upload", {"filename": filename, "content_base64": encoded, "expected_revision": 1})
            self.assertIn(response.status_code, (413, 422))

    async def test_mcp_tools_are_redacted(self):
        response = await self.request("GET", "/dashboard/api/mcp/mcp-1/tools")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["items"][0]["name"], "lookup")
        self.assertNotIn("credential-private", response.text)

    async def test_config_http_cas_and_bad_config(self):
        original = await self.request("GET", "/dashboard/api/config")
        self.assertNotIn("TOKEN", original.text)
        sha = original.json()["data"]["sha256"]
        response = await self.request("PATCH", "/dashboard/api/config", {"patch": {"agent_tasks": {"enabled": False}}, "expected_sha256": sha})
        self.assertEqual(response.status_code, 200, response.text)
        current = response.json()["data"]
        response = await self.request("PATCH", "/dashboard/api/config", {"patch": {"agent_tasks": {"enabled": True}}, "expected_sha256": sha})
        self.assertEqual(response.status_code, 409)
        response = await self.request("PATCH", "/dashboard/api/config", {"patch": {"agent_tasks": {"global_concurrency": 0}}, "expected_sha256": current["sha256"]})
        self.assertEqual(response.status_code, 422)
        response = await self.request("PATCH", "/dashboard/api/config", {"patch": {"agent_tasks": {"enabled": True}}})
        self.assertEqual(response.status_code, 428)
        response = await self.request("POST", "/dashboard/api/config/rollback", {"backup_id": current["backups"][0], "expected_sha256": current["sha256"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["data"]["config"]["agent_tasks"]["enabled"])

    async def test_diagnostic_zip_excludes_credentials_chat_and_knowledge(self):
        response = await self.request("GET", "/dashboard/api/diagnostics/download")
        self.assertEqual(response.status_code, 200, response.text[:100])
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(set(archive.namelist()), {"system.json", "status.json", "trace.json"})
            contents = "\n".join(archive.read(name).decode() for name in archive.namelist())
            trace = json.loads(archive.read("trace.json"))
        self.assertLessEqual(len(trace["items"]), 100)
        for secret in ("TOKEN", "OTHER_SECRET", "ORIGINAL-CHAT", "KB-ORIGINAL", "PRIVATE-CONTENT"):
            self.assertNotIn(secret, contents)
        self.assertIn('"packages"', contents)


if __name__ == "__main__":
    unittest.main()