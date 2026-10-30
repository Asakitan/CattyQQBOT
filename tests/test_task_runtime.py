from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


PACKAGE = "catty_task_fixture"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "src/catty_qq_ai")]
    sys.modules[PACKAGE] = package
contracts = importlib.import_module(f"{PACKAGE}.task_contracts")
TaskStore = importlib.import_module(f"{PACKAGE}.task_store").TaskStore
TaskRuntime = importlib.import_module(f"{PACKAGE}.task_runtime").TaskRuntime
AffectionStore = importlib.import_module(f"{PACKAGE}.affection").AffectionStore


class TaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.now = 1_800_000_000.0
        self.policy = contracts.TaskPolicy.from_mapping({"timezone": "UTC", "tiers": {"light": {"free_per_day": 0, "points": 20, "max_tokens": 1000}}})
        self.store = TaskStore(self.path / "tasks.sqlite3", self.policy, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.account = AffectionStore(SimpleNamespace(catty_memory_path=str(self.path / "memory.json"), catty_owner_qq="owner"))
        self.account.admin_set_points("user", 500)
        self.identity = contracts.TaskIdentity("user", "group:one")
        self.runtimes = []
        self.sent = []

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.stop()

    async def execute(self, record, identity, budget):
        reservation = budget.reserve_request(100, 100)
        budget.settle_request(reservation, 50, 50, receipt_id=record["task_id"])
        return contracts.TaskResult("done")

    async def deliver(self, record, identity, result):
        saved = self.store.get_task(record["task_id"], identity)
        self.assertEqual(saved["result"]["summary"], result.summary)
        self.sent.append(record["task_id"])
        return {"status": "delivered", "message_id": record["task_id"]}

    def runtime(self, executor=None, deliver=None, **kwargs):
        runtime = TaskRuntime(self.store, self.policy, self.account, executor or self.execute, deliver or self.deliver, poll_seconds=0.01, **kwargs)
        self.runtimes.append(runtime)
        return runtime

    def quote(self, request="request"):
        return self.store.quote(self.identity, request, "title", "research", "description")

    async def wait_state(self, task_id, states, *, payment_state=None):
        async def wait():
            while True:
                record = self.store.get_task(task_id, self.identity)
                if record["state"] in states and (payment_state is None or record["payment_state"] == payment_state):
                    return record
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(wait(), timeout=5)

    async def wait_schedule(self, schedule_id, state):
        async def wait():
            while True:
                schedule = next(item for item in self.store.list_schedules(self.identity) if item["schedule_id"] == schedule_id)
                if schedule["state"] == state:
                    return schedule
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(wait(), timeout=5)

    async def test_persist_before_delivery_and_charge_only_once(self):
        quote = self.quote()
        self.store.confirm(quote["task_id"], self.identity, self.account)
        runtime = self.runtime()
        await runtime.start()
        result = await self.wait_state(quote["task_id"], {"completed"}, payment_state="charged")
        self.assertEqual(result["payment_state"], "charged")
        self.assertEqual(self.sent, [quote["task_id"]])
        self.assertEqual(self.account.summary("user")["total_consumed"], 20)
        await runtime.stop()
        await runtime.start()
        await runtime.poll_once()
        self.assertEqual(len(self.sent), 1)

    async def test_parent_chat_timeout_does_not_cancel_confirmed_job(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def execute(record, identity, budget):
            started.set()
            await release.wait()
            return contracts.TaskResult("done")
        runtime = self.runtime(executor=execute)
        quote = self.quote()
        await runtime.start()
        async def chat():
            await runtime.confirm(quote["task_id"], self.identity)
            await started.wait()
            await asyncio.Event().wait()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(chat(), timeout=0.1)
        release.set()
        await self.wait_state(quote["task_id"], {"completed"})

    async def test_user_cancel_reaches_executor_and_refunds(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def execute(record, identity, budget):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        quote = self.quote()
        runtime = self.runtime(executor=execute)
        await runtime.confirm(quote["task_id"], self.identity)
        await runtime.start()
        await asyncio.wait_for(started.wait(), 5)
        await runtime.cancel(quote["task_id"], self.identity)
        await asyncio.wait_for(cancelled.wait(), 5)
        self.assertEqual(self.account.get_points("user"), 500)
        self.assertEqual(self.sent, [])

    async def test_stopped_running_task_is_interrupted_until_explicit_resume(self):
        started = asyncio.Event()
        async def execute(record, identity, budget):
            started.set()
            await asyncio.Event().wait()
        quote = self.quote()
        runtime = self.runtime(executor=execute)
        await runtime.confirm(quote["task_id"], self.identity)
        await runtime.start()
        await asyncio.wait_for(started.wait(), 5)
        await runtime.stop()
        self.assertEqual(self.store.get_task(quote["task_id"], self.identity)["state"], "interrupted")
        replacement = self.runtime()
        await replacement.start()
        await replacement.poll_once()
        self.assertEqual(self.sent, [])
        await replacement.resume(quote["task_id"], self.identity)
        await self.wait_state(quote["task_id"], {"completed"})

    async def test_delivery_unknown_is_not_replayed_after_restart(self):
        attempts = []
        async def deliver(record, identity, result):
            attempts.append(record["task_id"])
            raise TimeoutError("unknown transport result")
        quote = self.quote()
        runtime = self.runtime(deliver=deliver)
        await runtime.confirm(quote["task_id"], self.identity)
        await runtime.start()
        await self.wait_state(quote["task_id"], {"delivery_unknown"})
        await runtime.stop()
        replacement = self.runtime(deliver=deliver)
        await replacement.start()
        await replacement.poll_once()
        self.assertEqual(attempts, [quote["task_id"]])
        self.assertEqual(self.account.get_points("user"), 480)

    async def test_recurring_insufficient_points_pauses_without_model_call(self):
        self.account.admin_set_points("user", 0)
        schedule = self.store.create_schedule(self.identity, "schedule", "title", "analysis", "reminder", daily="12:00", reminder=True)
        self.now = schedule["next_run"]
        runtime = self.runtime(permission_check=lambda identity: identity)
        await runtime.start()
        paused = await self.wait_schedule(schedule["schedule_id"], "paused")
        self.assertEqual(paused["last_error"], "insufficient_points")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.daily_usage(self.identity)["attempts"], 0)

    async def test_recurring_reminders_have_independent_roots_and_daily_budget(self):
        self.policy = contracts.TaskPolicy.from_mapping({"timezone": "UTC", "tiers": {"light": {"free_per_day": 3, "max_tokens": 1000}}})
        self.store.policy = self.policy
        calls = []
        async def execute(record, identity, budget):
            calls.append(record["task_id"])
            return contracts.TaskResult("should not be needed")
        schedule = self.store.create_schedule(self.identity, "schedule", "title", "analysis", "reminder", daily="12:00", reminder=True)
        self.now = schedule["next_run"]
        runtime = self.runtime(executor=execute, permission_check=lambda identity: identity)
        await runtime.start()
        async def sent_count(expected):
            while len(self.sent) < expected:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(sent_count(1), 5)
        await self.wait_state(self.sent[0], {"completed"})
        updated = self.store.list_schedules(self.identity)[0]
        self.now = updated["next_run"]
        runtime.notify()
        await asyncio.wait_for(sent_count(2), 5)
        await self.wait_state(self.sent[1], {"completed"})
        self.assertNotEqual(self.sent[0], self.sent[1])
        self.assertEqual(calls, [])
        self.assertEqual(self.store.daily_usage(self.identity)["attempts"], 1)

    async def test_missing_permission_verifier_pauses_schedule(self):
        schedule = self.store.create_schedule(self.identity, "schedule", "title", "analysis", "reminder", daily="12:00", reminder=True)
        self.now = schedule["next_run"]
        await self.runtime().start()
        paused = await self.wait_schedule(schedule["schedule_id"], "paused")
        self.assertEqual(paused["last_error"], "permission_denied")
        self.assertEqual(self.sent, [])

    async def test_paid_schedule_requires_explicit_confirmation_even_with_funds(self):
        schedule = self.store.create_schedule(self.identity, "schedule", "title", "analysis", "reminder", daily="12:00", reminder=True)
        self.now = schedule["next_run"]
        runtime = self.runtime(permission_check=lambda identity: identity)
        await runtime.start()
        paused = await self.wait_schedule(schedule["schedule_id"], "paused")
        self.assertEqual(paused["last_error"], "confirmation_required")
        self.assertEqual(self.account.get_points("user"), 500)
        quote = self.store.get_task(paused["last_task_id"], self.identity)
        self.assertEqual(quote["state"], "quoted")
        await runtime.confirm(quote["task_id"], self.identity)
        await self.wait_state(quote["task_id"], {"completed"})
        self.assertEqual(self.account.get_points("user"), 480)


if __name__ == "__main__":
    unittest.main()