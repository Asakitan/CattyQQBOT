from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeStream:
    def __init__(self, mode: str, error: Exception) -> None:
        self.mode = mode
        self.error = error
        self.started = asyncio.Event()
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        if self.mode == "enter_error":
            raise self.error
        self.entered = True
        return self

    async def __aexit__(self, *args):
        self.exited = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        if self.mode == "stream_error":
            raise self.error
        if self.mode == "wait_for_cancel":
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def get_final_message(self):
        return types.SimpleNamespace(
            content=[{"type": "text", "text": "Test response"}],
            usage={},
            id="test-message",
        )


class _FakeClient:
    def __init__(self, stream: _FakeStream) -> None:
        self.stream = stream
        self.enter_count = 0
        self.close_count = 0
        self.stream_kwargs = None
        self.beta = types.SimpleNamespace(
            messages=types.SimpleNamespace(stream=self._stream),
        )

    def _stream(self, **kwargs):
        self.stream_kwargs = kwargs
        return self.stream

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        self.close_count += 1


class AnthropicClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Load just the client and its relative helpers, without initializing
        # the NoneBot plugin or opening any connection to a real provider.
        self.package_name = "_catty_native_lifecycle_test"
        root = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai"
        package = types.ModuleType(self.package_name)
        package.__path__ = [str(root)]
        config = types.ModuleType(f"{self.package_name}.config")
        config.config = types.SimpleNamespace(catty_cache_diag_enabled=False)
        package.config = config
        openai = types.ModuleType(f"{self.package_name}.openai_client")
        openai.get_current_scope_key = lambda: ""
        dashboard = types.ModuleType(f"{self.package_name}.dashboard_state")
        self.dashboard_events = []
        dashboard.start_stream = lambda **kw: self.dashboard_events.append("start") or "test"
        dashboard.end_stream = lambda *args, **kw: self.dashboard_events.append("end")
        dashboard.push_event = lambda *args, **kw: None

        self.mode = "success"
        self.error = RuntimeError("offline stream failure")
        self.clients = []
        self.constructor_kwargs = []
        sdk = types.ModuleType("anthropic")
        sdk.AsyncAnthropic = self._make_client
        modules = {
            self.package_name: package,
            config.__name__: config,
            openai.__name__: openai,
            dashboard.__name__: dashboard,
            "anthropic": sdk,
        }
        self.modules_patch = patch.dict(sys.modules, modules)
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        module_name = f"{self.package_name}.anthropic_native_client"
        spec = importlib.util.spec_from_file_location(
            module_name, root / "anthropic_native_client.py",
        )
        assert spec is not None and spec.loader is not None
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.module
        spec.loader.exec_module(self.module)

        # Request diagnostics must not write into the checkout during tests.
        for name in ("mkdir", "write_text"):
            mocked = patch.object(Path, name)
            mocked.start()
            self.addCleanup(mocked.stop)

    def _make_client(self, **kwargs):
        self.constructor_kwargs.append(kwargs)
        client = _FakeClient(_FakeStream(self.mode, self.error))
        self.clients.append(client)
        return client

    async def _request(self):
        return await self.module.post_messages_native(
            base_url="https://example.invalid/v1/",
            api_key="offline-test-key",
            model="test-model",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=17,
            temperature=0.25,
            timeout=23.0,
            extra_headers={"X-Test": "offline"},
            enable_cache_breakpoints=False,
        )

    def _assert_client_closed(self) -> _FakeClient:
        self.assertEqual(len(self.clients), 1)
        client = self.clients[0]
        self.assertEqual(client.enter_count, 1)
        self.assertEqual(client.close_count, 1)
        return client

    async def test_success_closes_client_and_preserves_parameters(self) -> None:
        data = await self._request()
        self.assertEqual(data["choices"][0]["message"]["content"], "Test response")
        client = self._assert_client_closed()
        self.assertTrue(client.stream.exited)
        self.assertEqual(self.dashboard_events, ["start", "end"])
        self.assertEqual(self.constructor_kwargs, [{
            "base_url": "https://example.invalid",
            "api_key": "offline-test-key",
            "default_headers": {
                "anthropic-version": "2023-06-01",
                "anthropic-beta": self.module._build_beta_header(),
                "X-Test": "offline",
            },
            "timeout": 23.0,
        }])
        self.assertEqual(client.stream_kwargs, {
            "model": "test-model",
            "max_tokens": 17,
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.25,
        })

    async def test_stream_error_propagates_and_closes_client(self) -> None:
        self.mode = "stream_error"
        with self.assertRaises(RuntimeError) as raised:
            await self._request()
        self.assertIs(raised.exception, self.error)
        client = self._assert_client_closed()
        self.assertTrue(client.stream.exited)
        self.assertEqual(self.dashboard_events, ["start", "end"])

    async def test_stream_enter_error_propagates_and_closes_client(self) -> None:
        self.mode = "enter_error"
        with self.assertRaises(RuntimeError) as raised:
            await self._request()
        self.assertIs(raised.exception, self.error)
        client = self._assert_client_closed()
        self.assertFalse(client.stream.entered)
        self.assertEqual(self.dashboard_events, [])

    async def test_task_cancellation_propagates_and_closes_client(self) -> None:
        self.mode = "wait_for_cancel"
        task = asyncio.create_task(self._request())
        # Let the task reach the stream iterator, then cancel it as a caller
        # would during request shutdown, without network or timed delays.
        await asyncio.sleep(0)
        await self.clients[0].stream.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        client = self._assert_client_closed()
        self.assertTrue(client.stream.exited)
        self.assertEqual(self.dashboard_events, ["start", "end"])

    async def test_preparation_error_does_not_create_client(self) -> None:
        with patch.object(
            self.module, "_convert_history_for_anthropic", side_effect=self.error,
        ):
            with self.assertRaises(RuntimeError) as raised:
                await self._request()
        self.assertIs(raised.exception, self.error)
        self.assertEqual(self.clients, [])


if __name__ == "__main__":
    unittest.main()
