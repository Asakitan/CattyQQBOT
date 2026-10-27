from __future__ import annotations

import importlib.util
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "persona_evolution_budget.py"
_SPEC = importlib.util.spec_from_file_location("persona_evolution_budget_under_test", _MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"cannot load {_MODULE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
BudgetLedger = _MODULE.BudgetLedger
HARD_DAILY_LIMIT = _MODULE.HARD_DAILY_LIMIT

DAY = datetime(2026, 10, 28, 23, 55)
NEXT_DAY = DAY + timedelta(days=1)


def _process_reserve(path, start, output):
    try:
        ledger = BudgetLedger(path, daily_limit=25)
        if not start.wait(30):
            output.put(("error", "start event timed out"))
            return
        output.put(("ok", ledger.reserve(7, now=DAY)))
    except BaseException as exc:
        output.put(("error", repr(exc)))


def _process_crash_after_reserve(path, output):
    ledger = BudgetLedger(path, daily_limit=10)
    output.send(ledger.reserve(10, now=DAY))
    output.close()
    os._exit(17)


class BudgetLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "budget.sqlite3"

    def test_default_limit_boundaries_and_zero(self):
        ledger = BudgetLedger(self.path)
        empty = ledger.reserve(0, now=DAY)
        self.assertIsInstance(empty, str)
        self.assertTrue(ledger.settle(empty, 0, now=DAY))
        reservation = ledger.reserve(HARD_DAILY_LIMIT, now=DAY)
        self.assertIsInstance(reservation, str)
        self.assertEqual(ledger.used_today(now=DAY), HARD_DAILY_LIMIT)
        self.assertIsNone(ledger.reserve(1, now=DAY))
        self.assertTrue(ledger.settle(reservation, HARD_DAILY_LIMIT - 1, now=DAY))
        self.assertEqual(ledger.used_today(now=DAY), HARD_DAILY_LIMIT - 1)
        self.assertIsNotNone(ledger.reserve(1, now=DAY))
        self.assertIsNone(ledger.reserve(1, now=DAY))

    def test_parameter_validation_never_releases_a_reservation(self):
        ledger = BudgetLedger(self.path, daily_limit=10)
        for value in (-1, 1.5, True, None, "1", HARD_DAILY_LIMIT + 1):
            with self.subTest(reserve=value):
                self.assertIsNone(ledger.reserve(value, now=DAY))
        self.assertIsNone(ledger.reserve(1, now="2026-10-28"))
        reservation = ledger.reserve(10, now=DAY)
        for value in (None, -1, 11, True, 1.5, "5", 2**100):
            with self.subTest(usage=value):
                self.assertFalse(ledger.settle(reservation, value, now=DAY))
                self.assertEqual(ledger.used_today(now=DAY), 10)
        self.assertFalse(ledger.settle(reservation, 5, now="2026-10-28"))
        self.assertFalse(ledger.settle("missing", 5, now=DAY))
        with self.assertRaises(ValueError):
            ledger.used_today(now="2026-10-28")
        self.assertTrue(ledger.settle(reservation, 5, now=DAY))
        self.assertFalse(ledger.settle(reservation, 4, now=DAY))
        self.assertEqual(ledger.used_today(now=DAY), 5)

    def test_limit_configuration_is_bounded_and_global(self):
        for limit in (-1, HARD_DAILY_LIMIT + 1, 2**100, True, 1.5, None):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    BudgetLedger(self.path, daily_limit=limit)
        with self.assertRaises(ValueError):
            BudgetLedger(":memory:")
        with self.assertRaises(ValueError):
            BudgetLedger("")
        cat = BudgetLedger(self.path, daily_limit=100)
        other_persona = BudgetLedger(self.path, daily_limit=20)
        self.assertIsNone(cat.reserve(21, now=DAY))
        self.assertIsNotNone(cat.reserve(12, now=DAY))
        self.assertIsNone(other_persona.reserve(9, now=DAY))
        self.assertIsNotNone(other_persona.reserve(8, now=DAY))
        self.assertEqual(cat.used_today(now=DAY), 20)
        reopened_with_higher_limit = BudgetLedger(self.path, daily_limit=HARD_DAILY_LIMIT)
        self.assertIsNone(reopened_with_higher_limit.reserve(1, now=DAY))
        self.assertIsNone(reopened_with_higher_limit.reserve(21, now=NEXT_DAY))

    def test_default_local_date_and_aware_datetime(self):
        ledger = BudgetLedger(self.path, daily_limit=10)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 28, 12, 0)

        with patch.object(_MODULE, "datetime", FrozenDateTime):
            reservation = ledger.reserve(3)
            self.assertEqual(ledger.used_today(), 3)
            self.assertTrue(ledger.settle(reservation, 2))
        self.assertEqual(ledger.used_today(now=DAY), 2)
        aware = datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)
        local = aware.astimezone().replace(tzinfo=None)
        self.assertIsNotNone(ledger.reserve(4, now=aware))
        self.assertEqual(ledger.used_today(now=local), 4)

    def test_pending_survives_reopen_cancellation_and_many_days(self):
        first = BudgetLedger(self.path, daily_limit=10)
        reservation = first.reserve(10, now=DAY)
        self.assertFalse(first.settle(reservation, None, now=DAY))
        del first
        reopened = BudgetLedger(self.path, daily_limit=10)
        far_future = DAY + timedelta(days=1000)
        for when in (DAY, NEXT_DAY, far_future):
            with self.subTest(day=when):
                self.assertEqual(reopened.used_today(now=when), 10)
                self.assertIsNone(reopened.reserve(1, now=when))
        self.assertTrue(reopened.settle(reservation, 3, now=far_future))
        self.assertEqual(reopened.used_today(now=DAY), 3)
        self.assertEqual(reopened.used_today(now=NEXT_DAY), 3)
        self.assertEqual(reopened.used_today(now=far_future), 3)
        self.assertIsNotNone(reopened.reserve(7, now=far_future))

    def test_crashed_process_leaves_its_reservation_on_disk(self):
        BudgetLedger(self.path, daily_limit=10)
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_process_crash_after_reserve, args=(str(self.path), sender))
        try:
            process.start()
            sender.close()
            self.assertTrue(receiver.poll(30), "crashing worker did not reserve")
            reservation = receiver.recv()
            process.join(30)
            self.assertEqual(process.exitcode, 17)
            reopened = BudgetLedger(self.path, daily_limit=10)
            self.assertEqual(reopened.used_today(now=NEXT_DAY), 10)
            self.assertIsNone(reopened.reserve(1, now=NEXT_DAY))
            self.assertTrue(reopened.settle(reservation, 4, now=NEXT_DAY))
            self.assertEqual(reopened.used_today(now=DAY), 4)
            self.assertEqual(reopened.used_today(now=NEXT_DAY), 4)
        finally:
            receiver.close()
            sender.close()
            if process.is_alive():
                process.terminate()
                process.join()

    def test_cross_day_settlement_counts_both_days_once(self):
        ledger = BudgetLedger(self.path, daily_limit=20)
        cross_day = ledger.reserve(10, now=DAY)
        self.assertTrue(ledger.settle(cross_day, 8, now=NEXT_DAY))
        self.assertEqual(ledger.used_today(now=DAY), 8)
        self.assertEqual(ledger.used_today(now=NEXT_DAY), 8)
        same_day = ledger.reserve(7, now=NEXT_DAY)
        self.assertTrue(ledger.settle(same_day, 4, now=NEXT_DAY))
        self.assertEqual(ledger.used_today(now=NEXT_DAY), 12)
        self.assertEqual(ledger.used_today(now=NEXT_DAY + timedelta(days=1)), 0)
        self.assertFalse(ledger.settle(cross_day, 1, now=NEXT_DAY))
        self.assertEqual(ledger.used_today(now=DAY), 8)

    def test_multiday_request_remains_charged_each_day(self):
        ledger = BudgetLedger(self.path, daily_limit=20)
        reservation = ledger.reserve(10, now=DAY)
        self.assertTrue(ledger.settle(reservation, 8, now=DAY + timedelta(days=3)))
        for offset in range(4):
            self.assertEqual(ledger.used_today(now=DAY + timedelta(days=offset)), 8)
        self.assertEqual(ledger.used_today(now=DAY + timedelta(days=4)), 0)

    def test_missing_parent_directory_is_created(self):
        ledger = BudgetLedger(self.path.parent / "missing" / "budget.sqlite3", daily_limit=10)
        self.assertIsNotNone(ledger.reserve(5, now=DAY))

    def test_pending_is_conservative_when_clock_moves_back(self):
        ledger = BudgetLedger(self.path, daily_limit=10)
        reservation = ledger.reserve(10, now=NEXT_DAY)
        self.assertEqual(ledger.used_today(now=DAY), 10)
        self.assertIsNone(ledger.reserve(1, now=DAY))
        self.assertTrue(ledger.settle(reservation, 3, now=DAY))
        self.assertEqual(ledger.used_today(now=DAY), 3)
        self.assertEqual(ledger.used_today(now=NEXT_DAY), 3)

    def test_threads_with_independent_instances_cannot_overspend(self):
        ledger = BudgetLedger(self.path, daily_limit=100)
        count = 24
        barrier = threading.Barrier(count)

        def reserve(_):
            instance = BudgetLedger(self.path, daily_limit=100)
            barrier.wait(timeout=30)
            return instance.reserve(7, now=DAY)

        with ThreadPoolExecutor(max_workers=count) as executor:
            results = list(executor.map(reserve, range(count)))
        accepted = [value for value in results if value is not None]
        self.assertEqual(len(accepted), 14)
        self.assertEqual(len(set(accepted)), 14)
        self.assertEqual(ledger.used_today(now=DAY), 98)

    def test_concurrent_first_open_initializes_only_once(self):
        count = 12
        barrier = threading.Barrier(count)

        def reserve(_):
            barrier.wait(timeout=30)
            return BudgetLedger(self.path, daily_limit=count).reserve(1, now=DAY)

        with ThreadPoolExecutor(max_workers=count) as executor:
            results = list(executor.map(reserve, range(count)))
        self.assertEqual(len({value for value in results if value is not None}), count)
        self.assertEqual(BudgetLedger(self.path, daily_limit=count).used_today(now=DAY), count)

    def test_independent_processes_share_the_same_budget(self):
        ledger = BudgetLedger(self.path, daily_limit=25)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        output = context.Queue()
        processes = [
            context.Process(target=_process_reserve, args=(str(self.path), start, output))
            for _ in range(6)
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(45)
                self.assertFalse(process.is_alive(), "reservation worker did not exit")
                self.assertEqual(process.exitcode, 0)
            messages = [output.get(timeout=10) for _ in processes]
            self.assertEqual([status for status, _ in messages], ["ok"] * len(processes))
            accepted = [value for _, value in messages if value is not None]
            self.assertEqual(len(accepted), 3)
            self.assertEqual(len(set(accepted)), 3)
            self.assertEqual(ledger.used_today(now=DAY), 21)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
            output.close()
            output.join_thread()

    def test_invalid_and_empty_existing_database_are_not_reinitialized(self):
        for contents in (b"not a sqlite database", b""):
            with self.subTest(contents=contents):
                path = Path(self.tmp.name) / ("invalid.sqlite3" if contents else "empty.sqlite3")
                path.write_bytes(contents)
                with self.assertRaises(sqlite3.DatabaseError):
                    BudgetLedger(path)
                self.assertEqual(path.read_bytes(), contents)

    def test_missing_schema_fails_closed_without_recreating_tables(self):
        ledger = BudgetLedger(self.path, daily_limit=10)
        reservation = ledger.reserve(10, now=DAY)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE reservations")
        with self.assertRaises(sqlite3.DatabaseError):
            BudgetLedger(self.path, daily_limit=10)
        for operation in (
            lambda: ledger.reserve(1, now=DAY),
            lambda: ledger.settle(reservation, 1, now=DAY),
            lambda: ledger.used_today(now=DAY),
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                operation()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'reservations'"
            ).fetchone())

    def test_logically_corrupt_record_cannot_be_settled_or_ignored(self):
        ledger = BudgetLedger(self.path, daily_limit=10)
        reservation = ledger.reserve(10, now=DAY)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE reservations SET start_day = 'corrupt'")
        for operation in (
            lambda: ledger.reserve(1, now=DAY),
            lambda: ledger.settle(reservation, 1, now=DAY),
            lambda: ledger.used_today(now=DAY),
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                operation()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT used_tokens, end_day FROM reservations WHERE reservation_id = ?",
                (reservation,),
            ).fetchone(), (None, None))


if __name__ == "__main__":
    unittest.main()
