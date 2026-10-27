"""Offline receipt tests: no QQ, files, or model requests."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "src/catty_qq_ai/reply_delivery.py"
_SPEC = importlib.util.spec_from_file_location("catty_reply_delivery_test", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _module
_SPEC.loader.exec_module(_module)


class ReplyDeliveryStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_is_not_history_until_acknowledged(self):
        state = _module.ReplyDeliveryState()

        async def sender(payload):
            self.assertEqual(state.status_counts()["pending"], 1)
            self.assertEqual(state.delivered_texts(), [])
            return {"message_id": 42}

        result = await state.send(sender, "message", history_text="acknowledged")
        self.assertEqual(result, {"message_id": 42})
        self.assertEqual(state.delivered_texts(), ["acknowledged"])

    async def test_partial_failure_preserves_only_acknowledged_bubbles(self):
        state = _module.ReplyDeliveryState()

        async def sender(payload):
            if payload == "second":
                raise RuntimeError("missing ACK")

        await state.send(sender, "first", history_text="first")
        with self.assertRaises(RuntimeError):
            await state.send(sender, "second", history_text="second")
        await state.send(sender, "third", history_text="third")
        self.assertEqual(state.delivered_texts(), ["first", "third"])
        self.assertEqual(state.status_counts(), {"pending": 0, "delivered": 2, "unconfirmed": 1})

    async def test_first_failure_does_not_create_phantom_reply(self):
        state = _module.ReplyDeliveryState()

        async def sender(payload):
            raise OSError("transport failure")

        with self.assertRaises(OSError):
            await state.send(sender, "message", history_text="never confirmed")
        self.assertEqual(state.delivered_texts(), [])
        self.assertEqual(state.receipts[0].error_type, "OSError")

    async def test_cancellation_is_unconfirmed_and_propagates(self):
        state = _module.ReplyDeliveryState()
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def sender(payload):
            entered.set()
            await gate.wait()

        task = asyncio.create_task(state.send(sender, "message", history_text="pending"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(state.delivered_texts(), [])
        self.assertEqual(state.status_counts()["unconfirmed"], 1)


if __name__ == "__main__":
    unittest.main()