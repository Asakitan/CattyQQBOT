import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_responses_ws_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules[_PACKAGE_NAME] = _PACKAGE


def _load_module(name):
    path = _ROOT / "src" / "catty_qq_ai" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


responses_ws = _load_module("responses_ws")
openai_client = _load_module("openai_client")


class ResponseItemHashTests(unittest.TestCase):
    def test_only_supported_content_cache_metadata_is_ignored(self):
        for item_type, field in (("message", "content"), ("function_call_output", "output")):
            for part in (
                {"type": "input_text", "text": "TARGET"},
                {"type": "input_image", "image_url": "https://example.test/image.png", "detail": "low"},
                {"type": "input_file", "file_id": "file_TARGET"},
            ):
                with self.subTest(item_type=item_type, part_type=part["type"]):
                    plain = {"type": item_type, "role": "user", field: [part]}
                    marked = copy.deepcopy(plain)
                    marked[field][0]["prompt_cache_breakpoint"] = {"mode": "explicit"}
                    original = copy.deepcopy(marked)
                    self.assertEqual(responses_ws.item_hash(plain), responses_ws.item_hash(marked))
                    self.assertEqual(marked, original)
                    changed = copy.deepcopy(marked)
                    changed[field][0][next(key for key in part if key != "type")] = "changed"
                    self.assertNotEqual(responses_ws.item_hash(plain), responses_ws.item_hash(changed))
                    changed = {**marked, "role": "developer"}
                    self.assertNotEqual(responses_ws.item_hash(plain), responses_ws.item_hash(changed))

    def test_tool_arguments_and_literal_cache_metadata_are_not_ignored(self):
        for plain in (
            {"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "{}"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "{}"}]},
        ):
            with self.subTest(item_type=plain["type"]):
                changed = copy.deepcopy(plain)
                literal = '{"prompt_cache_breakpoint":"PAYLOAD"}'
                if "arguments" in changed:
                    changed["arguments"] = literal
                elif "output" in changed:
                    changed["output"] = literal
                else:
                    changed["content"][0]["text"] = literal
                self.assertNotEqual(responses_ws.item_hash(plain), responses_ws.item_hash(changed))
                self.assertNotEqual(responses_ws.item_hash(plain), responses_ws.item_hash({
                    **plain, "prompt_cache_breakpoint": "not-a-content-block",
                }))


class ResponsesWSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frames: list[dict] = []
        self.received = asyncio.Event()

    async def _serve(self, handler):
        server = await serve(handler, "127.0.0.1", 0)
        self.addAsyncCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        return server, port

    async def test_rolling_history_markers_keep_append_only_continuation(self):
        frames = []

        class FakeTransport:
            generation = 1

            async def ensure_ready(self):
                return self.generation

            async def iter_events(self, body, *, stream_id, previous_response_id):
                frames.append((copy.deepcopy(body), previous_response_id))
                yield {
                    "type": "response.completed",
                    "response": {
                        "id": f"resp_{len(frames)}",
                        "output": [{"type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": "ok"},
                        ]}],
                    },
                }

        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        messages = [{"role": "system", "content": "rules <<<CACHE_BOUNDARY:catty_stable_prefix>>>"}]
        for number in range(4):
            messages.extend([
                {"role": "user", "content": f"history {number}"},
                {"role": "assistant", "content": "ok"},
            ])
        with tempfile.TemporaryDirectory() as directory:
            cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
            with patch.object(openai_client, "_get_ws_transport", return_value=FakeTransport()):
                with patch.object(openai_client, "_get_ws_chain_cache", return_value=cache):
                    for turn in range(4):
                        messages.append({"role": "user", "content": f"current {turn}"})
                        await openai_client._stream_responses_ws_attempt(
                            config=config, base_url="https://api.openai.com/v1/responses",
                            api_key="TOKEN", extra_headers={},
                            payload={"model": "gpt-6.1-terra", "messages": messages},
                            timeout=5, proxy="", dash_stream_id=None, dash_mod=None, scope="private:1",
                        )
                        messages.append({"role": "assistant", "content": "ok"})
        self.assertIsNone(frames[0][1])
        for turn in range(1, 4):
            self.assertEqual(frames[turn][1], f"resp_{turn}")
            self.assertEqual(frames[turn][0]["input"], [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": f"current {turn}"}],
            }])

    async def test_explicit_cache_wire_and_previous_response_continuation(self):
        frames = []

        class FakeTransport:
            generation = 1

            async def ensure_ready(self):
                return self.generation

            async def iter_events(self, body, *, stream_id, previous_response_id):
                frames.append((json.loads(json.dumps(body)), previous_response_id))
                yield {
                    "type": "response.completed",
                    "response": {
                        "id": f"resp_{len(frames)}",
                        "output": [{
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }],
                    },
                }

        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        arguments = dict(
            config=config,
            base_url="https://api.openai.com/v1/responses",
            api_key="TOKEN",
            extra_headers={},
            timeout=5,
            proxy="",
            dash_stream_id=None,
            dash_mod=None,
            scope="private:1",
        )
        first_messages = [
            {"role": "system", "content": "stable <<<CACHE_BOUNDARY:catty_stable_prefix>>>"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "context one"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
            with patch.object(openai_client, "_get_ws_transport", return_value=FakeTransport()):
                with patch.object(openai_client, "_get_ws_chain_cache", return_value=cache):
                    await openai_client._stream_responses_ws_attempt(
                        payload={"model": "gpt-6.1-terra", "messages": first_messages},
                        **arguments,
                    )
                    await openai_client._stream_responses_ws_attempt(
                        payload={"model": "gpt-6.1-terra", "messages": first_messages + [
                            {"role": "assistant", "content": "ok"},
                            {"role": "user", "content": "next"},
                        ]},
                        **arguments,
                    )
                    await openai_client._stream_responses_ws_attempt(
                        payload={"model": "gpt-6.1-terra", "messages": [
                            first_messages[0], first_messages[1],
                            {"role": "assistant", "content": "ok"},
                            {"role": "user", "content": "next"},
                            {"role": "system", "content": "context two"},
                        ]},
                        **arguments,
                    )
        self.assertEqual(
            frames[0][0]["input"][0]["content"][0]["prompt_cache_breakpoint"],
            {"mode": "explicit"},
        )
        self.assertIsNone(frames[0][1])
        self.assertEqual(frames[1][1], "resp_1")
        self.assertEqual([item["role"] for item in frames[1][0]["input"]], ["user"])
        self.assertIsNone(frames[2][1])
        self.assertEqual(frames[0][0]["input"][0], frames[2][0]["input"][0])

    async def test_stream_events_and_frame_shape(self):
        async def handler(ws):
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                lane = frame.get("stream_id")
                extra = {"stream_id": lane} if lane else {}
                await ws.send(json.dumps({
                    **extra,
                    "type": "response.created",
                    "response": {"id": "resp_1"},
                }))
                await ws.send(json.dumps({
                    **extra,
                    "type": "response.output_text.delta",
                    "delta": "hello",
                }))
                await ws.send(json.dumps({
                    **extra,
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "output": [{
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "hello"}],
                        }],
                    },
                }))
                self.received.set()

        _, port = await self._serve(handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)
        events = [
            event async for event in transport.iter_events(
                {
                    "model": "gpt-6.1-terra",
                    "store": False,
                    "input": [{
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                    }],
                },
                stream_id="catty-test",
            )
        ]
        await asyncio.wait_for(self.received.wait(), timeout=5)
        self.assertEqual(self.frames[0]["type"], "response.create")
        self.assertEqual(self.frames[0]["model"], "gpt-6.1-terra")
        self.assertFalse(self.frames[0]["store"])
        self.assertEqual(self.frames[0]["stream_id"], "catty-test")
        self.assertEqual([event["type"] for event in events], [
            "response.created",
            "response.output_text.delta",
            "response.completed",
        ])

    async def test_previous_response_id_is_forwarded_and_default_lane_routes(self):
        async def handler(ws):
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                await ws.send(json.dumps({
                    "type": "response.completed",
                    "response": {"id": "resp_2", "output": []},
                }))
                self.received.set()

        _, port = await self._serve(handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)
        events = [
            event async for event in transport.iter_events(
                {"model": "gpt-6.1-terra", "store": False, "input": []},
                previous_response_id="resp_1",
            )
        ]
        await asyncio.wait_for(self.received.wait(), timeout=5)
        self.assertEqual(self.frames[0]["previous_response_id"], "resp_1")
        self.assertNotIn("stream_id", self.frames[0])
        self.assertEqual(events[-1]["response"]["id"], "resp_2")

    async def test_cancelled_request_cannot_deliver_a_late_response_to_next_turn(self):
        first_received = asyncio.Event()
        connections = 0

        async def handler(ws):
            nonlocal connections
            connections += 1
            connection = connections
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                lane = frame["stream_id"]
                if len(self.frames) == 1:
                    await ws.send(json.dumps({
                        "stream_id": lane,
                        "type": "response.created",
                        "response": {"id": "old"},
                    }))
                    continue
                if connection == 1:
                    await ws.send(json.dumps({
                        "stream_id": lane,
                        "type": "response.completed",
                        "response": {"id": "old", "output": []},
                    }))
                await ws.send(json.dumps({
                    "stream_id": lane,
                    "type": "response.completed",
                    "response": {"id": "new", "output": []},
                }))

        _, port = await self._serve(handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)

        async def first_turn():
            async for _event in transport.iter_events(
                {"model": "test", "input": []}, stream_id="same-lane"
            ):
                first_received.set()

        task = asyncio.create_task(first_turn())
        await asyncio.wait_for(first_received.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        events = [
            event async for event in transport.iter_events(
                {"model": "test", "input": []}, stream_id="same-lane"
            )
        ]
        self.assertEqual(events[-1]["response"]["id"], "new")
        self.assertEqual(connections, 2)

    async def test_distinct_transports_do_not_reuse_connection_generation(self):
        async def handler(ws):
            async for _raw in ws:
                pass

        _, port = await self._serve(handler)
        first = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses", api_key="TOKEN"
        )
        second = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses", api_key="TOKEN"
        )
        self.addAsyncCleanup(first.close)
        self.addAsyncCleanup(second.close)
        first_generation = await first.ensure_ready()
        second_generation = await second.ensure_ready()
        self.assertNotEqual(first_generation, second_generation)

    async def test_response_is_not_cached_under_a_replacement_connection(self):
        class SwitchingTransport:
            generation = 1

            async def ensure_ready(self):
                return self.generation

            async def iter_events(self, body, *, stream_id, previous_response_id):
                self.generation = 2
                yield {
                    "type": "response.completed",
                    "response": {"id": "old-connection", "output": []},
                }

        transport = SwitchingTransport()
        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
            with patch.object(openai_client, "_get_ws_transport", return_value=transport):
                with patch.object(openai_client, "_get_ws_chain_cache", return_value=cache):
                    await openai_client._stream_responses_ws_attempt(
                        config=config,
                        base_url="https://example.test/v1/responses",
                        api_key="TOKEN",
                        extra_headers={},
                        payload={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
                        timeout=5,
                        proxy="",
                        dash_stream_id=None,
                        dash_mod=None,
                        scope="private:1",
                    )
            self.assertIsNone(cache.get("private:1"))

    async def test_expired_previous_response_replays_without_lane_deadlock(self):
        async def handler(ws):
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                lane = frame["stream_id"]
                if len(self.frames) == 2:
                    await ws.send(json.dumps({
                        "stream_id": lane,
                        "type": "error",
                        "status": 400,
                        "error": {"code": "previous_response_not_found"},
                    }))
                    continue
                await ws.send(json.dumps({
                    "stream_id": lane,
                    "type": "response.completed",
                    "response": {
                        "id": f"resp_{len(self.frames)}",
                        "output": [{
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }],
                    },
                }))

        _, port = await self._serve(handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            idle_timeout=5,
        )
        self.addAsyncCleanup(transport.close)
        config = types.SimpleNamespace(
            catty_openai_websocket_store=False,
            catty_openai_websocket_include=[],
            catty_openai_websocket_previous_response=True,
            catty_openai_websocket_stream_id="catty",
        )
        arguments = dict(
            config=config,
            base_url="https://example.test/v1/responses",
            api_key="TOKEN",
            extra_headers={},
            timeout=5,
            proxy="",
            dash_stream_id=None,
            dash_mod=None,
            scope="private:1",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = responses_ws.ResponseChainCache(Path(directory) / "cache.json", 60)
            with patch.object(openai_client, "_get_ws_transport", return_value=transport):
                with patch.object(openai_client, "_get_ws_chain_cache", return_value=cache):
                    first = {"model": "test", "messages": [{"role": "user", "content": "hi"}]}
                    await openai_client._stream_responses_ws_attempt(payload=first, **arguments)
                    second = {
                        "model": "test",
                        "messages": first["messages"] + [
                            {"role": "assistant", "content": "ok"},
                            {"role": "user", "content": "next"},
                        ],
                    }
                    result = await asyncio.wait_for(
                        openai_client._stream_responses_ws_attempt(payload=second, **arguments),
                        timeout=2,
                    )
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(self.frames[1]["previous_response_id"], "resp_1")
        self.assertNotIn("previous_response_id", self.frames[2])
        self.assertEqual(len(self.frames[2]["input"]), 3)

    async def test_disconnect_raises_and_error_frame_is_yielded(self):
        async def closing_handler(ws):
            async for _raw in ws:
                await ws.close()
                return

        _, port = await self._serve(closing_handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)
        with self.assertRaises(responses_ws.ResponsesWSDisconnected):
            async for _event in transport.iter_events(
                {"model": "gpt-6.1-terra", "input": []},
                stream_id="drop",
            ):
                pass

        async def error_handler(ws):
            async for raw in ws:
                frame = json.loads(raw)
                lane = frame.get("stream_id")
                extra = {"stream_id": lane} if lane else {}
                await ws.send(json.dumps({
                    **extra,
                    "type": "error",
                    "status": 400,
                    "error": {
                        "code": "previous_response_not_found",
                        "message": "Previous response not found",
                    },
                }))
                self.received.set()

        _, port = await self._serve(error_handler)
        transport = responses_ws.ResponsesWebSocketTransport(
            url=f"ws://127.0.0.1:{port}/v1/responses",
            api_key="TOKEN",
            connect_timeout=5,
            idle_timeout=5,
            max_age_seconds=60,
        )
        self.addAsyncCleanup(transport.close)
        events = [
            event async for event in transport.iter_events(
                {"model": "gpt-6.1-terra", "input": []},
                stream_id="err",
            )
        ]
        await asyncio.wait_for(self.received.wait(), timeout=5)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["error"]["code"], "previous_response_not_found")

    async def test_chain_cache_round_trip_and_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = responses_ws.ResponseChainCache(path, 60)
            cache.put(
                "private:1",
                response_id="resp_1",
                item_hashes=["a", "b"],
                generation=3,
                store=False,
            )
            entry = cache.get("private:1")
            self.assertEqual(entry["response_id"], "resp_1")
            self.assertEqual(entry["item_hashes"], ["a", "b"])
            self.assertFalse(entry["store"])

            loaded = responses_ws.ResponseChainCache(path, 60)
            self.assertEqual(loaded.get("private:1")["response_id"], "resp_1")
            loaded.clear("private:1")
            self.assertIsNone(loaded.get("private:1"))

    def test_ws_url_and_prefix(self):
        self.assertEqual(
            responses_ws.responses_ws_url("https://api.openai.com/v1/responses"),
            "wss://api.openai.com/v1/responses",
        )
        self.assertEqual(
            responses_ws.responses_ws_url("https://api.openai.com/v1/chat/completions"),
            "wss://api.openai.com/v1/responses",
        )
        self.assertEqual(
            responses_ws.responses_ws_url("http://127.0.0.1:11434/v1"),
            "ws://127.0.0.1:11434/v1/responses",
        )
        self.assertEqual(
            responses_ws.common_prefix_len(["a", "b", "c"], ["a", "b"]),
            2,
        )
        self.assertEqual(
            responses_ws.common_prefix_len(["a"], ["b"]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
