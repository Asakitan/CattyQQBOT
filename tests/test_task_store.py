from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PACKAGE = "catty_task_fixture"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "src/catty_qq_ai")]
    sys.modules[PACKAGE] = package
contracts = importlib.import_module(f"{PACKAGE}.task_contracts")
TaskStore = importlib.import_module(f"{PACKAGE}.task_store").TaskStore
AffectionStore = importlib.import_module(f"{PACKAGE}.affection").AffectionStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.now = 1_800_000_000.0
        self.policy = contracts.TaskPolicy.from_mapping({"timezone": "UTC", "tiers": {"light": {"free_per_day": 0, "points": 20, "max_tokens": 1000}}})
        self.account = AffectionStore(SimpleNamespace(catty_memory_path=str(self.path / "memory.json"), catty_owner_qq="owner"))
        self.account.admin_set_points("user", 500)
        self.store = self.open_store()
        self.identity = contracts.TaskIdentity("user", "group:one")

    def open_store(self, policy=None):
        store = TaskStore(self.path / "tasks.sqlite3", policy or self.policy, clock=lambda: self.now)
        self.addCleanup(store.close)
        return store

    def quote(self, request="request", identity=None):
        return self.store.quote(identity or self.identity, request, "title", "research", "description")

    def running(self, request="request"):
        quote = self.quote(request)
        self.store.confirm(quote["task_id"], self.identity, self.account)
        return self.store.claim()

    def test_quote_and_confirmation_are_idempotent(self):
        quote = self.quote()
        self.assertEqual(quote["task_id"], self.quote()["task_id"])
        with self.assertRaises(contracts.TaskError):
            self.store.quote(self.identity, "request", "title", "research", "different")
        first = self.store.confirm(quote["task_id"], self.identity, self.account)
        second = self.store.confirm(quote["task_id"], self.identity, self.account)
        self.assertEqual(first["state"], "queued")
        self.assertEqual(second["state"], "queued")
        self.assertEqual(self.account.get_points("user"), 480)
        self.assertEqual(self.store.daily_usage(self.identity)["attempts"], 1)

    def test_unconfirmed_quotes_do_not_consume_slots(self):
        first = self.quote()
        second = self.quote("second")
        self.assertEqual(self.store.confirm(second["task_id"], self.identity, self.account)["state"], "queued")
        self.assertEqual(self.store.get_task(first["task_id"], self.identity)["state"], "quoted")

    def test_crash_after_point_reservation_resumes_same_confirmation(self):
        quote = self.quote()
        reserve = self.account.reserve_task_points
        def reserve_then_crash(*args):
            reserve(*args)
            raise OSError("simulated process interruption")
        with patch.object(self.account, "reserve_task_points", side_effect=reserve_then_crash):
            with self.assertRaises(contracts.TaskError):
                self.store.confirm(quote["task_id"], self.identity, self.account)
        self.assertEqual(self.store.get_task(quote["task_id"], self.identity)["state"], "confirming")
        self.now += 301
        reopened = self.open_store()
        reopened.resume_confirmations(self.account)
        self.assertEqual(reopened.get_task(quote["task_id"], self.identity)["state"], "queued")
        self.assertEqual(self.account.get_points("user"), 480)
        self.assertEqual(self.store.daily_usage(self.identity)["attempts"], 1)

    def test_cross_group_active_limit_and_strict_identity(self):
        first = self.quote()
        self.store.confirm(first["task_id"], self.identity, self.account)
        other = contracts.TaskIdentity("user", "group:two")
        second = self.quote("second", other)
        with self.assertRaises(contracts.TaskError) as caught:
            self.store.confirm(second["task_id"], other, self.account)
        self.assertEqual(caught.exception.code, "active_limit")
        for stranger in (other, contracts.TaskIdentity("user", "group:one", "catty"), contracts.TaskIdentity("other", "group:one")):
            with self.assertRaises(contracts.TaskError):
                self.store.get_task(first["task_id"], stranger)
            with self.assertRaises(contracts.TaskError):
                self.store.cancel(first["task_id"], stranger, self.account)
            self.assertFalse(any(row["task_id"] == first["task_id"] for row in self.store.list_tasks(stranger)))

    def test_two_connections_cannot_confirm_two_active_tasks(self):
        other_store = self.open_store()
        tasks = [self.quote("first"), self.quote("second")]
        def confirm(index):
            try:
                (self.store if index == 0 else other_store).confirm(tasks[index]["task_id"], self.identity, self.account)
                return True
            except contracts.TaskError:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(confirm, range(2))), 1)
        self.assertEqual(self.account.get_points("user"), 480)

    def test_daily_attempts_cross_groups_and_retries(self):
        policy = contracts.TaskPolicy.from_mapping({"timezone": "UTC", "daily_attempt_limit": 1})
        limited = self.open_store(policy)
        task = self.running()
        self.store.cancel(task["task_id"], self.identity, self.account)
        other = contracts.TaskIdentity("user", "group:two")
        quote = limited.quote(other, "second", "title", "analysis", "description")
        with self.assertRaises(contracts.TaskError) as caught:
            limited.confirm(quote["task_id"], other, self.account)
        self.assertEqual(caught.exception.code, "daily_attempt_limit")

    def test_missing_usage_keeps_reservation_and_late_usage_is_counted(self):
        task = self.running()
        budget = self.store.budget(task["task_id"])
        reservation = budget.reserve_request(300, 100)
        budget.settle_request(reservation, None, None)
        self.assertEqual(budget.snapshot()["reserved_tokens"], 400)
        self.store.cancel(task["task_id"], self.identity, self.account)
        self.assertEqual(self.store.daily_usage(self.identity)["reserved"], 400)
        budget.settle_request(reservation, 120, 30, receipt_id="provider-one")
        budget.settle_request(reservation, 120, 30, receipt_id="provider-one")
        self.assertEqual(budget.snapshot()["used_tokens"], 150)
        self.assertEqual(self.store.daily_usage(self.identity)["reserved"], 0)
        self.assertEqual(self.account.get_points("user"), 500)

    def test_duplicate_provider_receipt_is_not_double_counted(self):
        task = self.running()
        budget = self.store.budget(task["task_id"])
        first = budget.reserve_request(100, 100)
        second = budget.reserve_request(100, 100)
        budget.settle_request(first, 90, 10, receipt_id="same")
        result = budget.settle_request(second, 90, 10, receipt_id="same")
        self.assertTrue(result["duplicate_receipt"])
        self.assertEqual(budget.snapshot()["used_tokens"], 100)
        self.assertEqual(budget.snapshot()["reserved_tokens"], 0)

    def test_cumulative_root_budget_and_daily_limit(self):
        policy = contracts.TaskPolicy.from_mapping({"timezone": "UTC", "daily_token_limit": 1000, "tiers": {"light": {"free_per_day": 0, "points": 20, "max_tokens": 1000}}})
        limited = self.open_store(policy)
        task = self.running()
        budget = limited.budget(task["task_id"])
        first = budget.reserve_request(500, 100)
        budget.settle_request(first, 500, 100)
        with self.assertRaises(contracts.TaskError):
            budget.reserve_request(500, 100)
        self.store.cancel(task["task_id"], self.identity, self.account)
        second = self.quote("second")
        with self.assertRaises(contracts.TaskError) as caught:
            limited.confirm(second["task_id"], self.identity, self.account)
        self.assertEqual(caught.exception.code, "daily_token_limit")

    def test_failed_execution_refunds_once_and_payment_failure_retries(self):
        task = self.running()
        with patch.object(self.account, "_atomic_write", side_effect=OSError("disk full")):
            result = self.store.complete(task["task_id"], None, self.account, error="failed")
        self.assertEqual(result["payment_state"], "refund_pending")
        self.store.reconcile_payments(self.account)
        self.store.reconcile_payments(self.account)
        self.assertEqual(self.account.get_points("user"), 500)

    def test_send_is_claimed_once_and_unknown_is_not_replayed(self):
        task = self.running()
        self.store.complete(task["task_id"], contracts.TaskResult("done"))
        delivery = self.store.claim_delivery()
        self.assertEqual(delivery["task_id"], task["task_id"])
        self.assertIsNone(self.open_store().claim_delivery())
        self.store.delivery_confirm(task["task_id"], {"status": "delivery_unknown"}, self.account)
        self.assertIsNone(self.open_store().claim_delivery())
        result = self.store.delivery_confirm(task["task_id"], {"status": "delivered", "message_id": "message"}, self.account)
        self.assertEqual(result["state"], "completed")
        self.store.delivery_confirm(task["task_id"], {"status": "delivered", "message_id": "message"}, self.account)
        self.assertEqual(self.account.summary("user")["total_consumed"], 20)

    def test_cancellation_intent_survives_payment_commit_failure(self):
        task = self.running()
        original = self.store._settle_points
        def settle_then_crash(connection, task_id, account, charge):
            original(connection, task_id, account, charge)
            raise RuntimeError("simulated ledger commit interruption")
        with patch.object(self.store, "_settle_points", side_effect=settle_then_crash):
            with self.assertRaises(RuntimeError):
                self.store.cancel(task["task_id"], self.identity, self.account)
        self.assertEqual(self.store.get_task(task["task_id"], self.identity)["state"], "cancelled")
        self.assertEqual(self.store.get_task(task["task_id"], self.identity)["payment_state"], "refund_pending")
        self.store.reconcile_payments(self.account)
        self.assertEqual(self.account.get_points("user"), 500)

    def test_restart_requires_explicit_resume(self):
        task = self.running()
        self.now += 31
        reopened = self.open_store()
        self.assertEqual(reopened.recover_interrupted(), 1)
        self.assertIsNone(reopened.claim())
        self.assertEqual(reopened.get_task(task["task_id"], self.identity)["state"], "interrupted")
        reopened.resume(task["task_id"], self.identity)
        self.assertEqual(reopened.claim()["task_id"], task["task_id"])

    def test_expiry_refunds_hold_from_uncommitted_confirmation(self):
        quote = self.quote()
        self.account.reserve_task_points("user", quote["task_id"], 20)
        self.now += 301
        self.store.expire_quotes(self.account)
        self.assertEqual(self.account.get_points("user"), 500)
        self.assertEqual(self.store.get_task(quote["task_id"], self.identity)["state"], "expired")

    def test_due_claim_is_atomic_and_schedule_identity_is_scoped(self):
        schedule = self.store.create_schedule(self.identity, "schedule", "title", "analysis", "remind", daily="12:00", reminder=True)
        self.now = schedule["next_run"]
        claimed = self.store.claim_due()
        self.assertIsNone(self.open_store().claim_due())
        self.now += 31
        reclaimed = self.open_store().claim_due()
        self.assertEqual(claimed["run_id"], reclaimed["run_id"])
        self.assertFalse(self.store.finish_schedule_claim(schedule["schedule_id"], claimed["claim_token"]))
        with self.assertRaises(contracts.TaskError):
            self.store.cancel_schedule(schedule["schedule_id"], contracts.TaskIdentity("user", "group:other"))

    def test_revision_is_persistent_and_cancel_checks_it_atomically(self):
        quote = self.quote()
        revision = self.store.revision()
        self.assertEqual(self.open_store().revision(), revision)
        self.store.get_task(quote["task_id"], self.identity)
        self.assertEqual(self.store.revision(), revision)
        self.store.append_event(quote["task_id"], "trace", {"step": 1})
        with self.assertRaises(contracts.TaskError) as caught:
            self.store.cancel(quote["task_id"], self.identity, self.account, expected_revision=revision)
        self.assertEqual(caught.exception.code, "stale_revision")
        self.assertEqual(self.store.admin_list_tasks(status="quoted", user_id="user")[0]["task_id"], quote["task_id"])
        self.assertEqual(self.store.admin_events(task_id=quote["task_id"], limit=1)[0]["kind"], "trace")

    def test_owner_skips_personal_limits_but_not_root_budget(self):
        policy = contracts.TaskPolicy.from_mapping({"timezone": "UTC", "daily_token_limit": 1000, "daily_attempt_limit": 1, "tiers": {"light": {"max_tokens": 1000}}})
        store = self.open_store(policy)
        owner = contracts.TaskIdentity("owner", "group:one", is_owner=True)
        first = store.quote(owner, "owner-one", "title", "analysis", "description")
        second = store.quote(owner, "owner-two", "title", "analysis", "description")
        store.confirm(first["task_id"], owner, self.account)
        store.confirm(second["task_id"], owner, self.account)
        running = store.claim()
        budget = store.budget(running["task_id"])
        reservation = budget.reserve_request(600, 100)
        budget.settle_request(reservation, 600, 100)
        with self.assertRaises(contracts.TaskError):
            budget.reserve_request(301, 0)
        self.assertEqual(store.daily_usage(owner)["attempts"], 2)
        self.assertEqual(self.account.get_points("owner"), 9_999_999)

    def test_schedule_creation_limit_is_cross_group_and_pause_is_scoped(self):
        for index in range(5):
            self.store.create_schedule(self.identity, f"schedule-{index}", "title", "analysis", "description", cron="* * * * *")
        with self.assertRaises(contracts.TaskError) as caught:
            self.store.create_schedule(contracts.TaskIdentity("user", "group:two"), "sixth", "title", "analysis", "description", daily="12:00")
        self.assertEqual(caught.exception.code, "schedule_limit")
        schedule = self.store.list_schedules(self.identity)[0]
        paused = self.store.pause_schedule(schedule["schedule_id"], self.identity)
        self.assertEqual(paused["state"], "paused")
        with self.assertRaises(contracts.TaskError):
            self.store.pause_schedule(schedule["schedule_id"], contracts.TaskIdentity("other", "group:one"))


if __name__ == "__main__":
    unittest.main()