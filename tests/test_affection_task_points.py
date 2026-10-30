from __future__ import annotations

import importlib
import multiprocessing
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
AffectionStore = importlib.import_module(f"{PACKAGE}.affection").AffectionStore


def make_account(path: Path, owner: str = "owner"):
    return AffectionStore(SimpleNamespace(catty_memory_path=str(path / "memory.json"), catty_owner_qq=owner))


def reserve_in_process(path, task_id, start, output):
    account = make_account(Path(path))
    start.wait(10)
    output.put(account.reserve_task_points("user", task_id, 80)["ok"])


class TaskPointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.account = make_account(self.path)
        self.account.admin_set_points("user", 100)

    def test_reservation_blocks_legacy_spending_and_settles_once(self):
        self.assertTrue(self.account.reserve_task_points("user", "task", 80)["ok"])
        self.assertEqual(self.account.get_points("user"), 20)
        self.assertFalse(self.account.consume_points("user", 21)["ok"])
        self.assertTrue(self.account.settle_task_points("user", "task", True)["ok"])
        self.assertTrue(self.account.settle_task_points("user", "task", True)["ok"])
        self.assertFalse(self.account.settle_task_points("user", "task", False)["ok"])
        self.assertEqual(self.account.summary("user")["total_consumed"], 80)

    def test_refund_is_idempotent_across_instances(self):
        self.assertTrue(self.account.reserve_task_points("user", "task", 80)["ok"])
        other = make_account(self.path)
        self.assertTrue(other.settle_task_points("user", "task", False)["ok"])
        self.assertTrue(self.account.settle_task_points("user", "task", False)["ok"])
        self.assertEqual(self.account.get_points("user"), 100)

    def test_two_instances_cannot_reserve_same_balance(self):
        accounts = [make_account(self.path), make_account(self.path)]
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda index: accounts[index].reserve_task_points("user", f"task-{index}", 80), range(2)))
        self.assertEqual(sum(bool(result["ok"]) for result in results), 1)
        self.assertEqual(self.account.get_points("user"), 20)

    def test_write_failure_rolls_back_and_retry_survives_reload(self):
        with patch.object(self.account, "_atomic_write", side_effect=OSError("disk full")):
            self.assertFalse(self.account.reserve_task_points("user", "task", 80)["ok"])
        self.assertEqual(self.account.get_points("user"), 100)
        self.assertTrue(self.account.reserve_task_points("user", "task", 80)["ok"])
        other = make_account(self.path)
        with patch.object(other, "_atomic_write", side_effect=OSError("disk full")):
            self.assertFalse(other.settle_task_points("user", "task", True)["ok"])
        self.assertEqual(other.get_points("user"), 20)
        self.assertTrue(other.settle_task_points("user", "task", True)["ok"])
        self.assertEqual(make_account(self.path).get_points("user"), 20)

    def test_two_processes_cannot_reserve_same_balance(self):
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        output = context.Queue()
        processes = [context.Process(target=reserve_in_process, args=(str(self.path), f"process-{index}", start, output)) for index in range(2)]
        try:
            for process in processes:
                process.start()
            start.set()
            results = [output.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sum(results), 1)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
            output.close()

    def test_owner_remains_exempt(self):
        self.assertTrue(self.account.reserve_task_points("owner", "task", 200)["ok"])
        self.assertTrue(self.account.settle_task_points("owner", "task", True)["ok"])
        self.assertEqual(self.account.get_points("owner"), 9_999_999)


if __name__ == "__main__":
    unittest.main()