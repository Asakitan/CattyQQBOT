"""catty_write_file 暂存 / TTL 清理单元测试 (2026-10-26)。"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_file_delivery_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)

_SPEC = importlib.util.spec_from_file_location(
    f"{_PACKAGE_NAME}.tools", _ROOT / "src" / "catty_qq_ai" / "tools.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_tools = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _tools
_SPEC.loader.exec_module(_tools)


def _config(root: Path, **overrides):
    values = {
        "catty_file_delivery_enabled": True,
        "catty_file_delivery_dir": str(root),
        "catty_file_delivery_ttl_hours": 24.0,
        "catty_file_delivery_max_chars": 10_000,
        "catty_file_delivery_max_per_turn": 2,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class FileDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ctx = _tools.ToolContext(
            config=_config(self.root), memory_store=None, event=None
        )

    async def test_stage_file_writes_and_registers(self) -> None:
        result = await _tools._exec_write_file(
            {"filename": "报告.md", "content": "# 标题\n正文"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "报告.md")
        self.assertEqual(result["status"], "staged")
        self.assertEqual(len(self.ctx.pending_file_deliveries), 1)
        staged = self.ctx.pending_file_deliveries[0]
        self.assertEqual(staged["name"], "报告.md")
        path = Path(staged["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), "# 标题\n正文")
        self.assertEqual(path.parent, self.root)
        self.assertIn("_staged_file", result)

    async def test_traversal_and_any_suffix_are_sanitized(self) -> None:
        result = await _tools._exec_write_file(
            {"filename": "../../evil.exe", "content": "x"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "evil.exe")
        staged = self.ctx.pending_file_deliveries[0]
        self.assertEqual(Path(staged["path"]).parent, self.root)
        self.assertFalse((self.root.parent.parent / "evil.exe").exists())

    async def test_any_format_and_format_fallback(self) -> None:
        result = await _tools._exec_write_file(
            {"filename": "报告.docx", "content": "纯文本内容"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "报告.docx")
        self.assertEqual(
            Path(self.ctx.pending_file_deliveries[0]["path"]).suffix, ".docx"
        )

        second = await _tools._exec_write_file(
            {"content": "a,b", "format": "csv"}, self.ctx
        )
        self.assertTrue(second["ok"])
        self.assertEqual(second["filename"], "catty-file.csv")
        self.assertEqual(
            Path(self.ctx.pending_file_deliveries[1]["path"]).suffix, ".csv"
        )

    async def test_content_limit_and_per_turn_limit(self) -> None:
        too_long = await _tools._exec_write_file(
            {"filename": "big.txt", "content": "x" * 10001}, self.ctx
        )
        self.assertFalse(too_long["ok"])
        self.assertIn("上限", too_long["error"])
        self.assertEqual(self.ctx.pending_file_deliveries, [])

        for index in range(2):
            result = await _tools._exec_write_file(
                {"filename": f"f{index}.txt", "content": "ok"}, self.ctx
            )
            self.assertTrue(result["ok"])
        third = await _tools._exec_write_file(
            {"filename": "f2.txt", "content": "ok"}, self.ctx
        )
        self.assertFalse(third["ok"])
        self.assertIn("最多生成", third["error"])

    async def test_cleanup_removes_only_expired_files(self) -> None:
        config = _config(self.root)
        old = self.root / "old.txt"
        fresh = self.root / "fresh.txt"
        old.write_text("old", encoding="utf-8")
        fresh.write_text("fresh", encoding="utf-8")
        now = time.time()
        old_time = now - 25 * 3600
        old.touch()
        import os

        os.utime(old, (old_time, old_time))
        removed = _tools.cleanup_file_outbox(config, now=now)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    async def test_disabled_feature_rejects_write(self) -> None:
        ctx = _tools.ToolContext(
            config=_config(self.root, catty_file_delivery_enabled=False),
            memory_store=None,
            event=None,
        )
        result = await _tools._exec_write_file(
            {"filename": "x.txt", "content": "x"}, ctx
        )
        self.assertFalse(result["ok"])
        self.assertIn("关闭", result["error"])


class DeferredFileDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def load_delivery(self, executor, send_file):
        source = _ROOT / "src/catty_qq_ai/__init__.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {"_file_items_from_task", "_file_tool_with_deferred_delivery", "_spawn_deferred_file_delivery"}
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name in names]
        self.assertEqual({node.name for node in functions}, names)
        namespace = {
            "asyncio": asyncio, "Any": object, "Bot": object, "MessageEvent": object,
            "ToolContext": object, "Path": Path, "logger": Mock(),
            "_DEFERRED_DELIVERY_MAX_WAIT_SECONDS": 2,
            "_deferred_delivery_tasks": set(), "_file_delivery_lock": asyncio.Lock(),
            "_execute_schema_gated_tool_call": executor, "_send_staged_file": send_file,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        return namespace

    async def test_cancelled_thread_write_is_delivered_once_to_original_event(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _tools.ToolContext(config=_config(Path(directory)), memory_store=None, event=None)
            context.pending_file_deliveries.append({"path": "other-tool.txt", "name": "other.txt"})
            entered, after = asyncio.Event(), asyncio.Event()
            release = threading.Event()
            loop = asyncio.get_running_loop()
            real_write = _tools.write_document_atomic
            workers = []

            def controlled_write(*args):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise TimeoutError("fixture not released")
                return real_write(*args)

            async def executor(name, arguments, ctx, *, allowed_names):
                self.assertIs(ctx, context)
                workers.append(asyncio.current_task())
                return await _tools._exec_write_file(json.loads(arguments), ctx)

            delivered = []
            original_event = object()

            async def send_file(bot, event, item, *, label):
                self.assertIs(event, original_event)
                delivered.append((item["name"], Path(item["path"]).read_text(encoding="utf-8")))
                return True

            namespace = self.load_delivery(executor, send_file)
            scheduled_paths = set()
            with patch.object(_tools, "write_document_atomic", controlled_write):
                task = asyncio.create_task(namespace["_file_tool_with_deferred_delivery"](
                    "catty_write_file", '{"filename":"report.txt","content":"original content"}', context,
                    allowed_names={"catty_write_file"}, bot=object(), event=original_event,
                    after=after, scheduled_paths=scheduled_paths,
                ))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(context.deferred_delivery_started)
                    self.assertEqual(context._pending_file_writes, 1)
                    blocked = await _tools._exec_write_file({"content": "must respect quota"}, context)
                    self.assertFalse(blocked["ok"])
                    self.assertEqual(len(namespace["_deferred_delivery_tasks"]), 1)
                finally:
                    release.set()
                    after.set()
                    await asyncio.gather(*workers, *list(namespace["_deferred_delivery_tasks"]), return_exceptions=True)
            self.assertEqual(delivered, [("report.txt", "original content")])
            self.assertEqual(context._pending_file_writes, 0)
            self.assertEqual(len(scheduled_paths), 1)

    async def test_file_result_collection_ignores_failures_and_already_scheduled_paths(self):
        namespace = self.load_delivery(None, None)
        scheduled_paths = {"already.txt"}

        async def result_task(result):
            return result

        failed = asyncio.create_task(result_task({"ok": False, "files": [{"path": "partial.txt"}]}))
        self.assertEqual(await namespace["_file_items_from_task"](failed, scheduled_paths), [])
        success = asyncio.create_task(result_task({"ok": True, "files": [
            {"path": "already.txt"}, {"path": "new.txt", "name": "report.txt"},
            {"path": "new.txt", "name": "duplicate.txt"}, {}, None,
        ]}))
        self.assertEqual(await namespace["_file_items_from_task"](success, scheduled_paths),
                         [{"path": "new.txt", "name": "report.txt"}])
        self.assertEqual(scheduled_paths, {"already.txt", "new.txt"})

    async def test_fast_file_tool_keeps_regular_dispatcher_ownership(self):
        async def executor(name, arguments, context, *, allowed_names):
            return {"ok": True, "status": "staged", "_staged_file": {"path": "file.txt"}}

        namespace = self.load_delivery(executor, AsyncMock())
        context = types.SimpleNamespace(deferred_delivery_started=False)
        result = await namespace["_file_tool_with_deferred_delivery"](
            "catty_write_file", "{}", context, allowed_names={"catty_write_file"},
            bot=object(), event=object(), after=asyncio.Event(), scheduled_paths=set(),
        )
        self.assertEqual(result["status"], "staged")
        self.assertEqual(namespace["_deferred_delivery_tasks"], set())
        self.assertFalse(context.deferred_delivery_started)


if __name__ == "__main__":
    unittest.main()
