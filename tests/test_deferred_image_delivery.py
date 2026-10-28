from __future__ import annotations

import ast
import asyncio
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock

_ENTRY = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "__init__.py"
_NAMES = (
    "_spawn_deferred_image_delivery",
    "_segments_from_image_task",
    "_deferred_imagegen_items",
    "_imagegen_tool_with_deferred_delivery",
)


class _ActionFailed(Exception):
    pass


class _NetworkError(Exception):
    pass


class _Message(tuple):
    def __new__(cls, item):
        return super().__new__(cls, ("msg", item))


@dataclass
class _Ctx:
    pending_image_segments: list = field(default_factory=list)
    deferred_delivery_started: bool = False


def _load(namespace_extra: dict) -> dict:
    tree = ast.parse(_ENTRY.read_text(encoding="utf-8"))
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _NAMES
    ]
    assert len(selected) == len(_NAMES)
    namespace: dict = {
        "asyncio": asyncio,
        "Any": object,
        "Bot": object,
        "MessageEvent": object,
        "ToolContext": _Ctx,
        "Message": _Message,
        "OnebotActionFailed": _ActionFailed,
        "OnebotNetworkError": _NetworkError,
        "logger": Mock(),
        "_IMAGEGEN_INLINE_WAIT_SECONDS": 0.05,
        "_DEFERRED_DELIVERY_MAX_WAIT_SECONDS": 2.0,
        "_deferred_delivery_tasks": set(),
    }
    namespace.update(namespace_extra)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(_ENTRY), "exec"), namespace)
    return namespace


class _Bot:
    def __init__(self, log: list) -> None:
        self.log = log

    async def send(self, event, message) -> None:
        self.log.append(("send", message))


class DeferredImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_inline_wait_retains_delivery_owner(self) -> None:
        entered = asyncio.Event()
        complete = asyncio.Event()
        log = []

        async def tool(name, args_json, ctx, *, allowed_names):
            entered.set()
            await complete.wait()
            ctx.pending_image_segments.append("IMAGE")
            return {"ok": True}

        ns = _load({"_execute_schema_gated_tool_call": tool})
        after = asyncio.Event()
        ctx = _Ctx()
        task = asyncio.create_task(ns["_imagegen_tool_with_deferred_delivery"](
            "catty_imagegen", "{}", ctx, allowed_names={"catty_imagegen"},
            bot=_Bot(log), event=object(), after=after,
        ))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(ctx.deferred_delivery_started)
        self.assertEqual(len(ns["_deferred_delivery_tasks"]), 1)
        complete.set()
        self.assertEqual(log, [])
        after.set()
        await asyncio.gather(*ns["_deferred_delivery_tasks"])
        self.assertEqual(log, [("send", ("msg", "IMAGE"))])

    async def test_slow_generation_returns_immediately_and_sends_after_reply(self) -> None:
        log: list = []

        async def slow_tool(name, args_json, ctx, *, allowed_names):
            await asyncio.sleep(0.2)
            ctx.pending_image_segments.append("IMAGE")
            return {"ok": True}

        ns = _load({"_execute_schema_gated_tool_call": slow_tool})
        ctx = _Ctx()
        after = asyncio.Event()
        result = await ns["_imagegen_tool_with_deferred_delivery"](
            "catty_imagegen", "{}", ctx, allowed_names={"catty_imagegen"},
            bot=_Bot(log), event=object(), after=after,
        )
        self.assertEqual(result["status"], "generating_in_background")
        self.assertEqual(ctx.pending_image_segments, [])
        self.assertTrue(ctx.deferred_delivery_started)
        await asyncio.sleep(0.4)  # 图已经画好, 但回复还没发完 → 不能先发
        self.assertEqual(log, [])
        log.append(("reply", "text"))
        after.set()
        await asyncio.gather(*ns["_deferred_delivery_tasks"])
        self.assertEqual(log, [("reply", "text"), ("send", ("msg", "IMAGE"))])

    async def test_fast_result_stays_inline(self) -> None:
        async def fast_tool(name, args_json, ctx, *, allowed_names):
            ctx.pending_image_segments.append("IMAGE")
            return {"ok": True, "image_sent": True}

        ns = _load({"_execute_schema_gated_tool_call": fast_tool})
        ctx = _Ctx()
        result = await ns["_imagegen_tool_with_deferred_delivery"](
            "catty_imagegen", "{}", ctx, allowed_names=set(),
            bot=_Bot([]), event=object(), after=asyncio.Event(),
        )
        self.assertEqual(result, {"ok": True, "image_sent": True})
        self.assertEqual(ctx.pending_image_segments, ["IMAGE"])
        self.assertFalse(ctx.deferred_delivery_started)
        self.assertEqual(ns["_deferred_delivery_tasks"], set())

    async def test_background_failure_sends_only_an_apology(self) -> None:
        log: list = []

        async def failing_tool(name, args_json, ctx, *, allowed_names):
            await asyncio.sleep(0.1)
            return {"error": "pixelbunny HTTP 502 upstream"}

        ns = _load({"_execute_schema_gated_tool_call": failing_tool})
        fake_tools = types.ModuleType("fake_tools")
        fake_tools.imagegen_public_failure = lambda ctx, detail: {"user_facing_hint": "抱歉，这次没画出来"}
        import sys
        sys.modules["fake_pkg"] = types.ModuleType("fake_pkg")
        sys.modules["fake_pkg"].__path__ = []
        sys.modules["fake_pkg.tools"] = fake_tools
        ns["__package__"] = "fake_pkg"
        after = asyncio.Event()
        after.set()
        await ns["_imagegen_tool_with_deferred_delivery"](
            "catty_imagegen", "{}", _Ctx(), allowed_names=set(),
            bot=_Bot(log), event=object(), after=after,
        )
        await asyncio.gather(*ns["_deferred_delivery_tasks"])
        self.assertEqual(log, [("send", ("msg", "抱歉，这次没画出来"))])

    async def test_image_task_segments_are_wrapped(self) -> None:
        ns = _load({"_execute_schema_gated_tool_call": None})

        async def gen():
            return "SEG"

        self.assertEqual(await ns["_segments_from_image_task"](asyncio.ensure_future(gen())), ["SEG"])


if __name__ == "__main__":
    unittest.main()
