from __future__ import annotations

import base64
import datetime
import http.server
import importlib.util
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "catty_image_security_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
sys.modules[PACKAGE_NAME] = package


def load_module(name):
    qualified = f"{PACKAGE_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(qualified, ROOT / "src" / "catty_qq_ai" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


client = load_module("openai_client")
tools = load_module("tools")
REAL_CLIENT = httpx.AsyncClient
IMAGE = b"test image bytes"


def config(**overrides):
    values = dict(
        catty_vision_request_timeout=5, catty_request_timeout=5, catty_http_proxy="",
        catty_vision_base_url="https://api.example/v1", catty_openai_base_url="",
        catty_vision_api_key="", catty_openai_api_key="", catty_vision_model="vision",
        catty_openai_model="", catty_vision_prompt="", catty_vision_max_tokens=64,
        catty_vision_temperature=0, catty_vision_extra_headers={},
        catty_openai_extra_headers={}, catty_vision_extra_body={},
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


def records(addresses, port):
    answer = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        answer.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return answer


def resolver_for(mapping):
    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        value = mapping[host]
        if isinstance(value, Exception):
            raise value
        return records(value, port)
    return resolve


def http_patch(handler):
    transport = httpx.MockTransport(handler)
    return patch.object(client.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs))


def tool_context(url, callback):
    return tools.ToolContext(
        config=config(), memory_store=None, event=None, scope_key="group:42",
        recent_messages=[{"seq": 17, "images": [{"url": url}]}],
        input_image_urls=[url], recent_image_urls=[], describe_images_fn=callback,
    )


class ImageURLSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_literals_and_invalid_schemes_never_reach_look_callback(self):
        blocked = [
            "http://127.0.0.1/a", "http://10.2.3.4/a", "http://192.168.1.2/a",
            "http://169.254.169.254/a", "http://100.64.0.1/a", "http://localhost/a",
            "http://[::1]/a", "http://[fc00::1]/a", "http://[fe80::1]/a",
            "http://[::ffff:127.0.0.1]/a", "http://[ff02::1]/a",
            "https://user@cdn.example/a", "file:///etc/passwd",
        ]
        for url in blocked:
            for reference in (url, "m17#0", "0"):
                with self.subTest(url=url, reference=reference):
                    callback = AsyncMock(return_value="should not run")
                    result = await tools._exec_look_image({"image_ref": reference}, tool_context(url, callback))
                    self.assertIn("error", result)
                    callback.assert_not_awaited()

    async def test_tool_url_and_message_refs_reject_mixed_dns_before_any_request(self):
        for reference in ("https://cdn.example/a.png", "m17#0", "0"):
            calls = []
            async def describe(urls, question):
                return await client.describe_images(config(), urls, question)
            ctx = tool_context("https://cdn.example/a.png", describe)
            handler = lambda request: calls.append(request) or httpx.Response(200, content=IMAGE)
            with self.subTest(reference=reference), \
                    patch.object(client.socket, "getaddrinfo", side_effect=resolver_for({"cdn.example": ["93.184.216.34", "10.0.0.2"]})), \
                    http_patch(handler), patch.object(client, "_post_chat_completion", new_callable=AsyncMock) as post:
                result = await tools._exec_look_image({"image_ref": reference}, ctx)
                self.assertIn("error", result)
                self.assertEqual(calls, [])
                post.assert_not_awaited()

    async def test_dns_rebinding_is_pinned_and_vision_receives_data_not_url(self):
        calls = []
        answers = [records(["93.184.216.34"], 443), records(["127.0.0.1"], 443)]
        handler = lambda request: calls.append(request) or httpx.Response(200, content=IMAGE, headers={"content-type": "image/png"})
        with patch.object(client.socket, "getaddrinfo", side_effect=answers) as dns, \
                http_patch(handler), patch.object(client, "_route_native", return_value=False), \
                patch.object(client, "_post_chat_completion", new=AsyncMock(return_value="cat")) as post:
            result = await client.describe_images(config(), ["https://cdn.example/a.png?key=1"], "look")
        self.assertEqual(result, "cat")
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request.url.host, "93.184.216.34")
        self.assertEqual(request.headers["host"], "cdn.example")
        self.assertEqual(request.extensions["sni_hostname"], "cdn.example")
        self.assertEqual(request.url.query, b"key=1")
        payload = post.await_args.kwargs["messages"][0]["content"][1]["image_url"]["url"]
        self.assertEqual(payload, "data:image/png;base64," + base64.b64encode(IMAGE).decode())

    async def test_relative_and_cross_host_redirects_revalidate_and_pin_each_hop(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(302, headers={"location": "/next?token=1"})
            if len(calls) == 2:
                return httpx.Response(302, headers={"location": "https://other.example:8443/p.png"})
            return httpx.Response(200, content=IMAGE, headers={"content-type": "image/jpeg"})
        mapping = {"cdn.example": ["93.184.216.34"], "other.example": ["1.1.1.1"]}
        with patch.object(client.socket, "getaddrinfo", side_effect=resolver_for(mapping)) as dns, http_patch(handler):
            data, mime = await client.download_binary(config(), "https://cdn.example/start")
        self.assertEqual((data, mime), (IMAGE, "image/jpeg"))
        self.assertEqual(dns.call_count, 3)
        self.assertEqual([r.url.host for r in calls], ["93.184.216.34", "93.184.216.34", "1.1.1.1"])
        self.assertEqual([r.headers["host"] for r in calls], ["cdn.example", "cdn.example", "other.example:8443"])
        self.assertEqual([r.extensions["sni_hostname"] for r in calls], ["cdn.example", "cdn.example", "other.example"])
        self.assertEqual(calls[1].url.path, "/next")
        self.assertEqual(calls[1].url.query, b"token=1")

    async def test_redirects_to_private_and_dns_mixed_are_rejected(self):
        for destination in ("http://127.0.0.1/private", "https://evil.example/private", "https://[::1]/x"):
            calls = []
            handler = lambda request: calls.append(request) or httpx.Response(302, headers={"location": destination})
            mapping = {"cdn.example": ["93.184.216.34"], "evil.example": ["1.1.1.1", "192.168.1.1"]}
            with self.subTest(destination=destination), \
                    patch.object(client.socket, "getaddrinfo", side_effect=resolver_for(mapping)), http_patch(handler):
                with self.assertRaises(ValueError):
                    await client.download_binary(config(), "https://cdn.example/start")
            self.assertEqual(len(calls), 1)

    async def test_https_downgrade_and_redirect_exhaustion_are_rejected(self):
        for destination, expected_calls in (("http://cdn.example/next", 1), ("/loop", 6)):
            calls = []
            handler = lambda request: calls.append(request) or httpx.Response(302, headers={"location": destination})
            with self.subTest(destination=destination), \
                    patch.object(client.socket, "getaddrinfo", side_effect=resolver_for({"cdn.example": ["93.184.216.34"]})), http_patch(handler):
                with self.assertRaises(ValueError):
                    await client.download_binary(config(), "https://cdn.example/start")
            self.assertEqual(len(calls), expected_calls)

    async def test_public_ipv6_and_qq_cdn_preserve_host_sni_and_port(self):
        calls = []
        handler = lambda request: calls.append(request) or httpx.Response(200, content=IMAGE, headers={"content-type": "image/png"})
        with patch.object(client.socket, "getaddrinfo", side_effect=resolver_for({"multimedia.nt.qq.com": ["2606:4700:4700::1111"]})), http_patch(handler):
            parts = await client.prepare_passthrough_image_parts(config(), ["https://multimedia.nt.qq.com:8443/path?x=1"])
        self.assertEqual(calls[0].url.host, "2606:4700:4700::1111")
        self.assertEqual(calls[0].headers["host"], "multimedia.nt.qq.com:8443")
        self.assertEqual(calls[0].extensions["sni_hostname"], "multimedia.nt.qq.com")
        self.assertTrue(parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    async def test_resolution_failures_and_proxy_fail_closed(self):
        calls = []
        handler = lambda request: calls.append(request) or httpx.Response(200, content=IMAGE)
        with patch.object(client.socket, "getaddrinfo", side_effect=resolver_for({"cdn.example": socket.gaierror()})), http_patch(handler):
            with self.assertRaises(ValueError):
                await client.download_binary(config(), "https://cdn.example/a")
        with patch.object(client.socket, "getaddrinfo") as dns, http_patch(handler):
            with self.assertRaises(ValueError):
                await client.download_binary(config(catty_http_proxy="http://proxy.example:8080"), "https://cdn.example/a")
            dns.assert_not_called()
        self.assertEqual(calls, [])

    async def test_real_local_transport_uses_pinned_address_and_host(self):
        observed = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((self.path, self.headers.get("Host")))
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(IMAGE)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_getaddrinfo = socket.getaddrinfo
        lookups = []
        port = server.server_address[1]

        def resolve(host, service, *args, **kwargs):
            if host == "fixture.invalid":
                lookups.append(host)
                return records(["127.0.0.1"], service)
            return original_getaddrinfo(host, service, *args, **kwargs)

        original_public_ip = client._public_image_ip
        try:
            with patch.object(client.socket, "getaddrinfo", side_effect=resolve), \
                    patch.object(client, "_public_image_ip", side_effect=lambda ip: ip.is_loopback or original_public_ip(ip)):
                data, mime = await client.download_binary(config(), f"http://fixture.invalid:{port}/image?x=1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual((data, mime), (IMAGE, "image/png"))
        self.assertEqual(lookups, ["fixture.invalid"])
        self.assertEqual(observed, [("/image?x=1", f"fixture.invalid:{port}")])

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "cryptography unavailable")
    async def test_real_tls_uses_original_sni_and_validates_hostname(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        observed = []
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.invalid")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - datetime.timedelta(minutes=1))
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName("fixture.invalid")]), critical=False)
                       .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                       .sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory() as directory:
            cert_path = Path(directory) / "cert.pem"
            key_path = Path(directory) / "key.pem"
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert_path, key_path)
            server_context.set_servername_callback(lambda _socket, name, _context: observed.append(name))
            verify_context = ssl.create_default_context(cafile=str(cert_path))

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(IMAGE)

                def log_message(self, *args):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.socket = server_context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            original_getaddrinfo = socket.getaddrinfo
            original_public_ip = client._public_image_ip

            def resolve(host, service, *args, **kwargs):
                if host in ("fixture.invalid", "other.invalid"):
                    return records(["127.0.0.1"], service)
                return original_getaddrinfo(host, service, *args, **kwargs)

            try:
                with patch.object(client.socket, "getaddrinfo", side_effect=resolve), \
                        patch.object(client, "_public_image_ip", side_effect=lambda ip: ip.is_loopback or original_public_ip(ip)), \
                        patch.object(client.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(verify=verify_context, **kwargs)):
                    data, mime = await client.download_binary(config(), f"https://fixture.invalid:{port}/ok")
                    with self.assertRaises(httpx.ConnectError):
                        await client.download_binary(config(), f"https://other.invalid:{port}/wrong-host")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        self.assertEqual((data, mime), (IMAGE, "image/png"))
        self.assertEqual(observed, ["fixture.invalid", "other.invalid"])

    async def test_download_size_limit_checks_header_and_stream(self):
        for headers in ({"content-length": "11"}, {}):
            handler = lambda request: httpx.Response(200, content=b"x" * 11, headers=headers)
            with self.subTest(headers=headers), patch.object(client, "_IMAGE_DOWNLOAD_MAX_BYTES", 10), \
                    patch.object(client.socket, "getaddrinfo", side_effect=resolver_for({"cdn.example": ["93.184.216.34"]})), http_patch(handler):
                with self.assertRaises(ValueError):
                    await client.download_binary(config(), "https://cdn.example/a")

if __name__ == "__main__":
    unittest.main()
