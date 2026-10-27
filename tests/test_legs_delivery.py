import ast
import asyncio
import copy
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest import mock


_ROOT = Path(__file__).resolve().parents[1]


class ActionFailed(Exception):
    pass


class NetworkError(Exception):
    pass


class LegsDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.picture = Path(self.temp.name) / "fixture.png"
        self.picture.write_bytes(b"fixture")
        self.event = types.SimpleNamespace(user_id=7, self_id=99)
        self.chunks = ["first", "last"]
        self.outcomes = []
        self.timeline = []

        async def send(message):
            self.timeline.append(("send", message))
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.matcher = types.SimpleNamespace(
            send=mock.AsyncMock(side_effect=send),
            finish=mock.AsyncMock(),
        )
        self.remember_text = mock.Mock(
            side_effect=lambda event, text: self.timeline.append(("text", text))
        )
        self.remember_image = mock.Mock(
            side_effect=lambda *args, **kwargs: self.timeline.append(("image", kwargs))
        )
        self.cooldowns = {}
        self.namespace = {
            "Path": Path,
            "asyncio": asyncio,
            "time": time,
            "_conversation_queue_key": lambda event: "group:fixture",
            "_generate_legs_caption": mock.AsyncMock(return_value="caption"),
            "event_plain_text": lambda event: "fixture",
            "_reply_chunks": lambda text: list(self.chunks),
            "_locks": {"group:fixture": asyncio.Lock()},
            "_legs_last_sent_at": self.cooldowns,
            "_remember_bot_reply_for_event": self.remember_text,
            "_remember_bot_conversation_message": self.remember_image,
            "GroupMessageEvent": type("GroupMessageEvent", (), {}),
            "_reply_quote_segment": lambda event: None,
            "config": types.SimpleNamespace(catty_reply_human_split_delay_seconds=0),
            "_compose_reply_message": lambda event, text, quote: "caption:" + text,
            "OnebotActionFailed": ActionFailed,
            "OnebotNetworkError": NetworkError,
            "logger": mock.Mock(),
            "MessageSegment": types.SimpleNamespace(image=lambda file: ("image", file)),
            "Message": lambda value: value,
            "_addr_user": lambda event: "fixture",
        }
        path = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = copy.deepcopy(next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_legs_picture"
        ))
        function.decorator_list = []
        function.returns = None
        for argument in function.args.args:
            argument.annotation = None
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        exec(compile(module, str(path), "exec"), self.namespace)
        self.handle = self.namespace["handle_legs_picture"]

    async def run_handler(self):
        await self.handle(self.matcher, self.event, {"catty_legs_picture": self.picture})

    async def test_success_records_confirmed_text_and_image(self):
        self.outcomes = [None, None, None]
        await self.run_handler()
        self.remember_text.assert_called_once_with(self.event, "first\nlast")
        self.remember_image.assert_called_once()
        self.assertTrue(self.remember_image.call_args.kwargs["has_image"])
        self.assertEqual([event[0] for event in self.timeline], ["send", "send", "text", "send", "image"])
        self.matcher.finish.assert_awaited_once()

    async def test_image_failure_never_records_an_image(self):
        self.chunks = ["caption"]
        self.outcomes = [None, ActionFailed("result=120"), None]
        await self.run_handler()
        self.remember_text.assert_called_once_with(self.event, "caption")
        self.remember_image.assert_not_called()
        self.assertEqual(self.matcher.send.await_count, 3)
        self.matcher.finish.assert_awaited_once()

    async def test_caption_failure_does_not_record_caption(self):
        self.chunks = ["caption"]
        self.outcomes = [ActionFailed("result=120"), None]
        await self.run_handler()
        self.remember_text.assert_not_called()
        self.remember_image.assert_called_once()

    async def test_partial_caption_records_only_confirmed_chunks(self):
        self.chunks = ["first", "middle", "last"]
        self.outcomes = [None, ActionFailed("result=120"), None, None]
        await self.run_handler()
        self.remember_text.assert_called_once_with(self.event, "first\nlast")
        self.remember_image.assert_called_once()

    async def test_caption_network_error_preserves_prior_confirmed_text(self):
        self.outcomes = [None, NetworkError("fixture")]
        with self.assertRaises(NetworkError):
            await self.run_handler()
        self.remember_text.assert_called_once_with(self.event, "first")
        self.remember_image.assert_not_called()
        self.matcher.finish.assert_not_awaited()

    async def test_caption_cancellation_preserves_prior_confirmed_text(self):
        self.outcomes = [None, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.run_handler()
        self.remember_text.assert_called_once_with(self.event, "first")
        self.remember_image.assert_not_called()

    async def test_image_network_retry_records_only_after_success(self):
        self.chunks = []
        self.outcomes = [NetworkError("fixture"), None]
        sleep = mock.AsyncMock()
        self.namespace["asyncio"] = types.SimpleNamespace(sleep=sleep)
        await self.run_handler()
        sleep.assert_awaited_once_with(2.0)
        self.remember_image.assert_called_once()
        self.assertEqual([event[0] for event in self.timeline], ["send", "send", "image"])

    async def test_empty_caption_still_records_confirmed_image(self):
        self.chunks = []
        self.outcomes = [None]
        await self.run_handler()
        self.remember_text.assert_not_called()
        self.remember_image.assert_called_once()
        self.matcher.finish.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
