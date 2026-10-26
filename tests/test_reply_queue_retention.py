from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/catty_qq_ai/__init__.py"


class PrivateEvent:
    user_id = 123


class Finished(Exception):
    pass


def load_queue_check():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    handle = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_chat")
    stack = next(n for n in handle.body if isinstance(n, ast.AsyncWith))
    body = []
    collecting = False
    for statement in stack.body:
        names = {n.id for n in ast.walk(statement) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        if "queue_wait_seconds" in names:
            collecting = True
            continue
        if "_turn_persona_token" in names:
            break
        if collecting:
            body.append(copy.deepcopy(statement))
    assert body, "production queue expiry block not found"
    module = ast.parse("async def check():\n    return True\n")
    module.body[0].body = body + module.body[0].body
    ast.fix_missing_locations(module)
    return compile(module, str(SOURCE), "exec")


class QueueRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, *, private=False, waited=43.4, threshold=25.0, busy=True, **signals):
        logs = []

        async def finish():
            raise Finished()

        namespace = {
            "PrivateMessageEvent": PrivateEvent,
            "event": PrivateEvent() if private else SimpleNamespace(user_id=123),
            "incoming": SimpleNamespace(text="补充说明", directly_requested=False,
                                        mentioned=False, replied_to_self=False, opportunistic=True),
            "queue_was_busy": busy, "queue_wait_seconds": waited,
            "config": SimpleNamespace(catty_reply_queue_max_wait_seconds=threshold),
            "matcher": SimpleNamespace(finish=finish),
            "logger": SimpleNamespace(info=logs.append),
            "time": SimpleNamespace(monotonic=lambda: waited),
            "_turn_started_at": 0.0, "queue_key": "group:fixture",
        }
        for name, value in signals.items():
            setattr(namespace["incoming"], name, value)
        exec(load_queue_check(), namespace)
        try:
            await namespace["check"]()
        except Finished:
            return False, logs
        return True, logs

    async def test_real_mention_survives_observed_43_second_wait(self):
        accepted, _ = await self.check(mentioned=True, directly_requested=True)
        self.assertTrue(accepted)

    async def test_private_reply_and_continuation_survive_long_wait(self):
        for signals in ({"private": True}, {"replied_to_self": True}, {"directly_requested": True}):
            with self.subTest(signals=signals):
                accepted, _ = await self.check(waited=180.0, **signals)
                self.assertTrue(accepted)

    async def test_ordinary_stale_reply_can_expire(self):
        accepted, logs = await self.check()
        self.assertFalse(accepted)
        self.assertTrue(any("Abandoning queued reply" in line for line in logs))

    async def test_under_limit_disabled_limit_and_idle_queue_survive(self):
        for options in ({"waited": 24.9}, {"threshold": 0}, {"threshold": -1}, {"busy": False}):
            with self.subTest(options=options):
                accepted, _ = await self.check(**options)
                self.assertTrue(accepted)

    async def test_expiry_boundary_does_not_drop_direct_request(self):
        accepted, _ = await self.check(waited=25.0, mentioned=True)
        self.assertTrue(accepted)
        accepted, _ = await self.check(waited=25.0)
        self.assertFalse(accepted)

    async def test_queued_direct_request_reaches_provider_and_history(self):
        lock = asyncio.Lock()
        first_entered = asyncio.Event()
        second_enqueued = asyncio.Event()
        release_first = asyncio.Event()
        provider_calls = []
        history = []

        async def first():
            async with lock:
                first_entered.set()
                await release_first.wait()
                provider_calls.append("first")
                history.append("first")

        async def second():
            await first_entered.wait()
            busy = lock.locked()
            second_enqueued.set()
            async with lock:
                accepted, _ = await self.check(busy=busy, mentioned=True, directly_requested=True)
                if accepted:
                    provider_calls.append("second")
                    history.append("second")

        tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
        try:
            await asyncio.wait_for(second_enqueued.wait(), timeout=1)
        finally:
            release_first.set()
            await asyncio.gather(*tasks)
        self.assertEqual(provider_calls, ["first", "second"])
        self.assertEqual(history, ["first", "second"])


if __name__ == "__main__":
    unittest.main()