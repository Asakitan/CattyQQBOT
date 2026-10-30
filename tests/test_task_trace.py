from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_trace():
    path = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "task_trace.py"
    name = "_catty_task_trace_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


trace = _load_trace()


class TraceStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = trace.TraceStore(Path(self.directory.name) / "trace.sqlite3", max_events=3)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_only_metadata_is_retained_and_rows_are_bounded(self):
        for index in range(6):
            self.store.record("event", trace_id=f"turn-{index}", text="secret text", api_key="secret key", event_count=index)
        rows = self.store.query()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["event_count"] for row in rows], [5, 4, 3])
        exported = self.store.export()
        self.assertNotIn("secret", exported)
        self.assertEqual(len(json.loads(exported)), 3)

    def test_filters_and_closed_store(self):
        self.store.record("event", task_id="task-1", trace_id="turn-1", scope="group:12", user_id="42")
        self.store.record("event", task_id="task-2", trace_id="turn-2", scope="group:13", user_id="43")
        self.assertEqual(len(self.store.query(task_id="task-1", user_id="42")), 1)
        self.assertEqual(self.store.query(scope="group:other"), [])
        self.store.close()
        self.assertEqual(self.store.query(), [])
        self.assertEqual(self.store.record("event"), 0)

    def test_retention_prunes_expired_rows(self):
        with patch.object(trace.time, "time", return_value=1):
            self.store.record("event", trace_id="expired")
        self.store.record("event", trace_id="current")
        self.assertEqual([row["trace_id"] for row in self.store.query()], ["current"])


class TraceContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_task_cannot_write_after_parent_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = trace.TraceStore(Path(directory) / "trace.sqlite3")
            ready = asyncio.Event()
            async def late_usage():
                await ready.wait()
                trace.record_model_usage({"input_tokens": 99})
            try:
                token = trace.bind_trace(store, "parent", user_id="42", scope="group:7", persona="fadianji")
                task = asyncio.create_task(late_usage())
                trace.reset_trace(token)
                ready.set()
                await task
                self.assertEqual(store.query(), [])
            finally:
                store.close()

    async def test_concurrent_turns_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = trace.TraceStore(Path(directory) / "trace.sqlite3")
            try:
                async def run_turn(turn_id):
                    token = trace.bind_trace(store, turn_id, user_id=turn_id, scope=f"private:{turn_id}", persona="fadianji")
                    try:
                        await asyncio.sleep(0)
                        trace.record_model_usage({"input_tokens": 12, "output_tokens": 3, "content": "do not retain"})
                        trace.record_event("tool_end", tool="catty_delegate", task_id="task-1")
                    finally:
                        trace.reset_trace(token)
                    self.assertIsNone(trace.current_trace())

                await asyncio.gather(run_turn("turn-1"), run_turn("turn-2"))
                self.assertEqual(len(store.query(trace_id="turn-1")), 2)
                self.assertEqual(len(store.query(trace_id="turn-2")), 2)
                self.assertEqual(store.query(trace_id="turn-1")[0]["user_id"], "turn-1")
                trace.record_event("ignored")
                self.assertEqual(len(store.query()), 4)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()