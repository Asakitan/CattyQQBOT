from __future__ import annotations

import ast
import base64
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from fastapi import FastAPI, WebSocket
import httpx
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_dashboard_auth_fixture"
ROUTES = (
    ("GET", "/dashboard"),
    ("GET", "/dashboard/_js_test"),
    ("GET", "/dashboard/state"),
    ("GET", "/dashboard/sse"),
    ("POST", "/dev/sim_chat"),
    ("POST", "/dev/ambient_inject"),
)


class DashboardAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env_patch = patch.dict(os.environ, {"CATTY_ADMIN_TOKEN": ""})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.app = FastAPI()
        self.sim_calls = []
        self.ambient_calls = []
        self.sse_calls = 0
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
        dashboard_name = PACKAGE + ".dashboard"
        spec = importlib.util.spec_from_file_location(
            dashboard_name, ROOT / "src" / "catty_qq_ai" / "dashboard.py"
        )
        assert spec is not None and spec.loader is not None
        dashboard = importlib.util.module_from_spec(spec)
        sim_module = types.ModuleType(PACKAGE + ".catty_sim_chat")

        async def sim_chat(**kwargs):
            self.sim_calls.append(kwargs)
            return {
                "system_blocks": ["fixture"],
                "history_count": 0,
                "stats": {},
                "reply": "fixture reply",
                "messages": [],
            }

        sim_module.sim_chat = sim_chat
        modules = {
            PACKAGE: package,
            dashboard_name: dashboard,
            sim_module.__name__: sim_module,
        }
        modules_patch = patch.dict(sys.modules, modules)
        modules_patch.start()
        self.addCleanup(modules_patch.stop)
        spec.loader.exec_module(dashboard)
        package.dashboard = dashboard

        async def state():
            return {"fixture": "snapshot"}

        async def stream():
            self.sse_calls += 1
            yield 'data: {"type":"snapshot"}\n\n'

        dashboard.dashboard_state = state
        dashboard.dashboard_sse_stream = stream
        ambient = types.SimpleNamespace(
            record=lambda **kwargs: self.ambient_calls.append(kwargs),
            snapshot=lambda: {"group:fixture": len(self.ambient_calls)},
        )
        source = (ROOT / "src" / "catty_qq_ai" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        matches = [
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_mount_dev_sim_chat_endpoint"
        ]
        self.assertEqual(len(matches), 1)
        matches[0].decorator_list = []
        namespace = {
            "__name__": PACKAGE,
            "__package__": PACKAGE,
            "ambient_store": ambient,
            "logger": types.SimpleNamespace(
                debug=lambda *args, **kwargs: None,
                info=lambda *args, **kwargs: None,
                exception=lambda *args, **kwargs: None,
            ),
        }
        exec(compile(ast.Module(body=matches, type_ignores=[]), "fixture_init.py", "exec",
                 dont_inherit=True), namespace)
        with patch("nonebot.get_app", return_value=self.app):
            self.assertTrue(dashboard.mount_dashboard_routes())
            await namespace["_mount_dev_sim_chat_endpoint"]()

        @self.app.post("/onebot/v11/")
        async def other_http():
            return {"ok": True}

        @self.app.websocket("/onebot/v11/")
        async def other_websocket(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("fixture")
            await websocket.close()

    async def request(self, method, path, *, client=("127.0.0.1", 50123), headers=None,
                      body=None, proxy=False, follow_redirects=False,
                      base_url="http://127.0.0.1:8080"):
        app = ProxyHeadersMiddleware(self.app, trusted_hosts="127.0.0.1") if proxy else self.app
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
            follow_redirects=follow_redirects,
        ) as http:
            return await http.request(method, path, headers=headers, json=body)

    @staticmethod
    def body_for(path):
        if path.startswith("/dev/sim_chat"):
            return {"text": "fixture", "user_id": 123, "live": False}
        if path.startswith("/dev/ambient_inject"):
            return {"scope": "group:fixture", "user_id": "123", "text": "fixture"}
        return None

    async def test_local_html_sse_state_and_dev(self):
        for method, path in ROUTES:
            with self.subTest(path=path):
                response = await self.request(method, path, body=self.body_for(path))
                self.assertEqual(response.status_code, 200, response.text[:200])
                if path == "/dashboard":
                    self.assertIn("EventSource('/dashboard/sse')", response.text)
                elif path == "/dashboard/state":
                    self.assertEqual(response.json(), {"fixture": "snapshot"})
                elif path == "/dashboard/sse":
                    self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
                    self.assertIn('"type":"snapshot"', response.text)
                elif path.startswith("/dev/"):
                    self.assertTrue(response.json()["ok"])
        self.assertEqual(len(self.sim_calls), 1)
        self.assertEqual(len(self.ambient_calls), 1)
        self.assertEqual(self.sse_calls, 1)

    async def test_loopback_addresses_and_missing_or_remote_peers(self):
        for client, expected in (
            (("127.0.0.1", 1), 200),
            (("127.0.0.2", 1), 200),
            (("::1", 1), 200),
            (("::ffff:127.0.0.1", 1), 200),
            (("198.51.100.7", 1), 403),
            (("192.168.1.3", 1), 403),
            (("::ffff:198.51.100.7", 1), 403),
            (("2001:db8::7", 1), 403),
            (("localhost", 1), 403),
            (("not-an-address", 1), 403),
            (None, 403),
        ):
            with self.subTest(client=client):
                response = await self.request("GET", "/dashboard/state", client=client)
                self.assertEqual(response.status_code, expected)

    async def test_remote_cannot_access_any_sensitive_route(self):
        for method, path in ROUTES:
            with self.subTest(path=path):
                response = await self.request(
                    method, path,
                    client=("198.51.100.7", 50123),
                    headers={"Host": "127.0.0.1:8080", "Authorization": "Bearer fixture"},
                    body=self.body_for(path),
                )
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("fixture reply", response.text)
        self.assertEqual(self.sim_calls, [])
        self.assertEqual(self.ambient_calls, [])
        self.assertEqual(self.sse_calls, 0)

    async def test_forwarding_headers_and_uvicorn_rewrite_are_denied(self):
        headers_to_reject = (
            {"X-Forwarded-For": "127.0.0.1"},
            {"Forwarded": "for=127.0.0.1"},
            {"X-Forwarded-Host": "127.0.0.1:8080"},
            {"X-Forwarded-Proto": "http"},
            {"X-Real-IP": "127.0.0.1"},
            {"CF-Connecting-IP": "127.0.0.1"},
            {"Via": "1.1 local-proxy"},
        )
        for headers in headers_to_reject:
            for method, path in ROUTES:
                with self.subTest(headers=headers, path=path):
                    response = await self.request(method, path, headers=headers, body=self.body_for(path))
                    self.assertEqual(response.status_code, 403)
        for method, path in ROUTES:
            with self.subTest(uvicorn_proxy=path):
                response = await self.request(
                    method, path, proxy=True,
                    headers={"X-Forwarded-For": "127.0.0.1"},
                    body=self.body_for(path),
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.sim_calls, [])
        self.assertEqual(self.ambient_calls, [])
        self.assertEqual(self.sse_calls, 0)

    async def test_trusted_uvicorn_proxy_cannot_forge_loopback_peer(self):
        observed = []

        async def capture(scope, receive, send):
            observed.append(scope.get("client"))
            await self.app(scope, receive, send)

        proxy = ProxyHeadersMiddleware(capture, trusted_hosts="*")
        transport = httpx.ASGITransport(app=proxy, client=("198.51.100.7", 50123))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as http:
            for method, path in ROUTES:
                with self.subTest(path=path):
                    response = await http.request(
                        method, path, headers={"X-Forwarded-For": "127.0.0.1"},
                        json=self.body_for(path),
                    )
                    self.assertEqual(observed[-1][0], "127.0.0.1")
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(self.sim_calls, [])
        self.assertEqual(self.ambient_calls, [])
        self.assertEqual(self.sse_calls, 0)

    async def test_slash_redirects_cannot_bypass_the_guard(self):
        for method, path in ROUTES:
            with self.subTest(path=path):
                response = await self.request(
                    method, path + "/", client=("198.51.100.7", 1),
                    body=self.body_for(path), follow_redirects=True,
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.sim_calls, [])
        self.assertEqual(self.ambient_calls, [])
        self.assertEqual(self.sse_calls, 0)

    async def test_host_and_origin_cannot_bypass_loopback_policy(self):
        for headers in (
            {"Host": "remote.example:8080"},
            {"Origin": "https://remote.example"},
            {"Origin": "null"},
            {"Origin": "http://localhost:8080"},
        ):
            with self.subTest(headers=headers):
                response = await self.request("GET", "/dashboard/state", headers=headers)
                self.assertEqual(response.status_code, 403)
        response = await self.request(
            "GET", "/dashboard/state", headers={"Origin": "http://127.0.0.1:8080"}
        )
        self.assertEqual(response.status_code, 200)
        response = await self.request(
            "GET", "/dashboard/state",
            headers={"Host": "localhost:8080", "Origin": "http://localhost:8080"},
        )
        self.assertEqual(response.status_code, 200)

    async def test_other_http_and_onebot_websocket_are_unchanged(self):
        response = await self.request(
            "POST", "/onebot/v11/", client=("198.51.100.7", 1),
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 200)
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.1"},
            "scheme": "ws", "path": "/onebot/v11/", "raw_path": b"/onebot/v11/",
            "root_path": "", "query_string": b"", "headers": [],
            "client": ("198.51.100.7", 1), "server": ("127.0.0.1", 8080),
            "subprotocols": [],
        }
        await self.app(scope, receive, send)
        self.assertEqual([message["type"] for message in sent],
                         ["websocket.accept", "websocket.send", "websocket.close"])
        self.assertEqual(sent[1]["text"], "fixture")

    async def test_token_authentication_and_remote_https(self):
        token = "fixture-password-不是真密钥"
        valid = base64.b64encode(("catty:" + token).encode("utf-8")).decode("ascii")
        invalid = base64.b64encode(b"catty:wrong").decode("ascii")
        with patch.dict(os.environ, {"CATTY_ADMIN_TOKEN": token}):
            for method, path in ROUTES:
                with self.subTest(path=path):
                    options = dict(client=("198.51.100.7", 1), body=self.body_for(path), base_url="https://admin.example:8443")
                    for auth in (None, "Bearer fixture", "Basic !!!", "Basic " + invalid):
                        response = await self.request(method, path, headers={"Authorization": auth} if auth else None, **options)
                        self.assertEqual(response.status_code, 401)
                        self.assertIn("Basic", response.headers["www-authenticate"])
                    response = await self.request(method, path, headers={"Authorization": "Basic " + valid}, **options)
                    self.assertEqual(response.status_code, 200, response.text[:200])
            response = await self.request("GET", "/dashboard/state", client=("198.51.100.7", 1), base_url="http://admin.example:8080", headers={"Authorization": "Basic " + valid})
            self.assertEqual(response.status_code, 403)
            response = await self.request("GET", "/dashboard/state", client=("198.51.100.7", 1), base_url="https://admin.example:8443", headers={"Authorization": "Basic " + valid, "Origin": "https://attacker.example"})
            self.assertEqual(response.status_code, 403)
            response = await self.request("GET", "/dashboard/state", headers={"Authorization": "Basic " + valid})
            self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
