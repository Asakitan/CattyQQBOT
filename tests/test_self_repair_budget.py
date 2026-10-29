from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from catty_self_repair.budget import WeeklyBudget

UTC8 = timezone(timedelta(hours=8))
DAY = datetime(2026, 10, 29, 12, tzinfo=UTC8)


def process_reserve(path, event, output):
    try:
        if not event.wait(30):
            raise TimeoutError("fixture start event")
        ledger = WeeklyBudget(Path(path), limit=25)
        output.put((True, ledger.reserve(7, now=DAY)))
    except BaseException as exc:
        output.put((False, type(exc).__name__))


def process_crash(path, output):
    ledger = WeeklyBudget(Path(path), limit=10)
    output.send(ledger.reserve(10, now=DAY))
    output.close()
    os._exit(17)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "budget.sqlite3"

    def test_exact_hard_cap_and_settlement(self):
        ledger = WeeklyBudget(self.path)
        self.assertEqual(ledger.snapshot(now=DAY), {"week": "2026-10-26", "limit": 100_000_000, "used": 0, "remaining": 100_000_000, "pending": 0})
        token = ledger.reserve(100_000_000, now=DAY)
        self.assertIsNotNone(token)
        self.assertIsNone(ledger.reserve(1, now=DAY))
        self.assertTrue(ledger.settle(token, 99_999_999, now=DAY))
        self.assertIsNotNone(ledger.reserve(1, now=DAY))
        self.assertIsNone(ledger.reserve(1, now=DAY))

    def test_invalid_limits_do_not_create_database(self):
        for value in (-1, 100_000_001, True, None, 1.5, "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WeeklyBudget(self.path, value)
            self.assertFalse(self.path.exists())

    def test_invalid_reservations_cannot_spend(self):
        ledger = WeeklyBudget(self.path, 10)
        for value in (-1, 100_000_001, True, None, 1.5, "10"):
            self.assertIsNone(ledger.reserve(value, now=DAY))
        self.assertEqual(ledger.snapshot(now=DAY)["used"], 0)

    def test_invalid_or_missing_usage_keeps_full_debt(self):
        ledger = WeeklyBudget(self.path, 10)
        token = ledger.reserve(10, now=DAY)
        for value in (None, -1, 11, True, False, 1.5, "5", {}, float("nan")):
            self.assertFalse(ledger.settle(token, value, now=DAY))
            self.assertEqual(ledger.snapshot(now=DAY)["used"], 10)
            self.assertIsNone(ledger.reserve(1, now=DAY))
        self.assertTrue(ledger.settle(token, 3, now=DAY))
        self.assertFalse(ledger.settle(token, 0, now=DAY))
        self.assertEqual(ledger.snapshot(now=DAY)["used"], 3)

    def test_pending_is_count_and_not_token_total(self):
        ledger = WeeklyBudget(self.path, 40)
        first = ledger.reserve(12, now=DAY)
        ledger.reserve(7, now=DAY)
        ledger.settle(first, 5, now=DAY)
        self.assertEqual(ledger.snapshot(now=DAY), {"week": "2026-10-26", "limit": 40, "used": 12, "remaining": 28, "pending": 1})

    def test_zero_budget_blocks_nonzero_and_zero_usage_is_explicit(self):
        ledger = WeeklyBudget(self.path, 0)
        self.assertIsNone(ledger.reserve(1, now=DAY))
        token = ledger.reserve(0, now=DAY)
        self.assertTrue(ledger.settle(token, 0, now=DAY))
        self.assertEqual(ledger.snapshot(now=DAY)["pending"], 0)

    def test_monday_is_utc_plus_eight_not_utc(self):
        ledger = WeeklyBudget(self.path, 20)
        before = datetime(2026, 11, 1, 15, 59, 59, 999999, tzinfo=timezone.utc)
        after = before + timedelta(microseconds=1)
        token = ledger.reserve(6, now=before)
        ledger.settle(token, 4, now=before)
        self.assertEqual(ledger.snapshot(now=before)["week"], "2026-10-26")
        self.assertEqual(ledger.snapshot(now=after)["week"], "2026-11-02")
        self.assertEqual(ledger.snapshot(now=after)["used"], 0)

    def test_naive_datetimes_are_utc8(self):
        ledger = WeeklyBudget(self.path, 10)
        at = datetime(2026, 11, 2)
        token = ledger.reserve(8, now=at)
        ledger.settle(token, 5, now=at)
        self.assertEqual(ledger.snapshot(now=datetime(2026, 11, 1, 16, tzinfo=timezone.utc))["used"], 5)

    def test_year_boundary_does_not_reset_partial_week(self):
        ledger = WeeklyBudget(self.path, 10)
        first = datetime(2026, 12, 31, tzinfo=UTC8)
        next_year = datetime(2027, 1, 1, tzinfo=UTC8)
        ledger.settle(ledger.reserve(9, now=first), 8, now=next_year)
        self.assertEqual(ledger.snapshot(now=next_year)["used"], 8)

    def test_pending_survives_restart_and_many_weeks(self):
        ledger = WeeklyBudget(self.path, 10)
        token = ledger.reserve(10, now=DAY)
        reopened = WeeklyBudget(self.path, 10)
        far = DAY + timedelta(weeks=300)
        self.assertEqual(reopened.snapshot(now=far)["used"], 10)
        self.assertIsNone(reopened.reserve(1, now=far))
        self.assertTrue(reopened.settle(token, 3, now=far))
        for offset in (0, 1, 149, 299, 300):
            self.assertEqual(reopened.snapshot(now=DAY + timedelta(weeks=offset))["used"], 3)
        self.assertEqual(reopened.snapshot(now=far + timedelta(weeks=1))["used"], 0)

    def test_clock_rollback_preserves_every_observed_week(self):
        ledger = WeeklyBudget(self.path, 10)
        token = ledger.reserve(10, now=DAY)
        ledger.snapshot(now=DAY + timedelta(weeks=4))
        ledger.settle(token, 7, now=DAY + timedelta(weeks=1))
        for offset in range(5):
            self.assertEqual(ledger.snapshot(now=DAY + timedelta(weeks=offset))["used"], 7)

    def test_lower_limit_is_durable_and_not_raised_on_reopen(self):
        original = WeeklyBudget(self.path, 100)
        original.reserve(20, now=DAY)
        lower = WeeklyBudget(self.path, 10)
        self.assertEqual(lower.snapshot(now=DAY)["remaining"], 0)
        self.assertIsNone(original.reserve(1, now=DAY))
        self.assertEqual(WeeklyBudget(self.path).snapshot(now=DAY)["limit"], 10)

    def test_thread_reservations_are_atomic(self):
        ledger = WeeklyBudget(self.path, 100)
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda _: ledger.reserve(7, now=DAY), range(24)))
        self.assertEqual(sum(item is not None for item in results), 14)
        self.assertEqual(ledger.snapshot(now=DAY)["used"], 98)

    def test_process_initialization_and_reservations_are_atomic(self):
        context = multiprocessing.get_context("spawn")
        event, output = context.Event(), context.Queue()
        processes = [context.Process(target=process_reserve, args=(str(self.path), event, output)) for _ in range(6)]
        try:
            for process in processes:
                process.start()
            event.set()
            results = [output.get(timeout=40) for _ in processes]
            self.assertTrue(all(ok for ok, _ in results), results)
            self.assertEqual(sum(value is not None for _, value in results), 3)
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                process.join(10)
            output.close()
            output.join_thread()
        self.assertEqual(WeeklyBudget(self.path, 25).snapshot(now=DAY)["used"], 21)

    def test_crashed_process_does_not_refund(self):
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=process_crash, args=(str(self.path), sender))
        try:
            process.start()
            sender.close()
            self.assertTrue(receiver.poll(30))
            self.assertIsInstance(receiver.recv(), str)
            process.join(10)
            self.assertEqual(process.exitcode, 17)
        finally:
            if process.is_alive():
                process.kill()
            process.join(10)
            receiver.close()
        ledger = WeeklyBudget(self.path, 10)
        self.assertEqual(ledger.snapshot(now=DAY + timedelta(weeks=2))["pending"], 1)
        self.assertIsNone(ledger.reserve(1, now=DAY + timedelta(weeks=2)))

    def test_corrupt_database_is_not_reinitialized(self):
        self.path.write_bytes(b"not a database")
        before = self.path.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            WeeklyBudget(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_schema_tampering_blocks_every_operation(self):
        ledger = WeeklyBudget(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE unexpected (value TEXT)")
            connection.commit()
        for operation in (lambda: ledger.snapshot(now=DAY), lambda: ledger.reserve(1, now=DAY), lambda: ledger.settle("a" * 32, 0, now=DAY), lambda: WeeklyBudget(self.path)):
            with self.assertRaises(sqlite3.DatabaseError):
                operation()

    def test_deleted_open_ledger_is_not_recreated(self):
        ledger = WeeklyBudget(self.path)
        self.path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            ledger.reserve(1, now=DAY)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()