"""工具工程缺口回归：只使用 mock 和临时目录，不请求模型/QQ/网络。"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_tool_audit_test"
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


class WriteFileAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = _tools.ToolContext(
            config=types.SimpleNamespace(catty_file_delivery_dir=str(self.root),
                                         catty_file_delivery_max_per_turn=10),
            event=None, memory_store=None, is_directly_requested=False,
        )

    async def test_real_formats_stage_actual_bytes_without_claiming_sent(self) -> None:
        for extension in ("docx", "xlsx", "pdf"):
            with self.subTest(format=extension):
                result = await _tools._exec_write_file(
                    {"filename": "报告." + extension, "content": "标题,内容\n测试,中文"}, self.ctx
                )
                self.assertTrue(result["ok"], result)
                path = Path(result["_staged_file"]["path"])
                self.assertEqual(result["format"], extension)
                self.assertEqual(result["bytes"], path.stat().st_size)
                self.assertTrue(path.read_bytes().startswith(b"%PDF-" if extension == "pdf" else b"PK"))
                self.assertEqual(result["status"], "staged")
                self.assertNotIn("file_sent", result)
                self.assertIn("不要说已经发送", result["note"])
        self.assertEqual(len(self.ctx.pending_file_deliveries), 3)
        self.assertFalse(list(self.root.glob("*.tmp")))

    async def test_explicit_format_renames_suffix_to_actual_format(self) -> None:
        result = await _tools._exec_write_file(
            {"filename": "报告.txt", "format": "docx", "content": "真实文档"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "报告.docx")
        self.assertEqual(result["format"], "docx")

    async def test_arbitrary_extension_retains_raw_contract(self) -> None:
        result = await _tools._exec_write_file(
            {"filename": "../../原文.exe", "content": "不冒充程序"}, self.ctx
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "原文.exe")
        self.assertEqual(result["format"], "text")
        self.assertEqual(result["content_mode"], "text/raw")
        self.assertIn("catty_python_run", result["format_note"])
        path = Path(result["_staged_file"]["path"])
        self.assertEqual(path.parent, self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), "不冒充程序")

    async def test_unsupported_explicit_binary_encoder_does_not_stage(self) -> None:
        result = await _tools._exec_write_file(
            {"format": "pptx", "content": "演示文稿"}, self.ctx
        )
        self.assertFalse(result["ok"])
        self.assertIn("catty_python_run", result["error"])
        self.assertEqual(self.ctx.pending_file_deliveries, [])
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_blocking_encoder_runs_off_event_loop(self) -> None:
        loop_thread = threading.get_ident()
        worker_threads = []
        write = _tools.write_document_atomic

        def observe(*args):
            worker_threads.append(threading.get_ident())
            return write(*args)

        with patch.object(_tools, "write_document_atomic", side_effect=observe):
            result = await _tools._exec_write_file({"content": "原文"}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread)

    async def test_encoder_or_disk_failure_never_stages(self) -> None:
        for error in (ImportError("missing"), ValueError("invalid"), PermissionError("denied")):
            with self.subTest(error=type(error).__name__):
                with patch.object(_tools, "write_document_atomic", side_effect=error):
                    result = await _tools._exec_write_file({"content": "原文"}, self.ctx)
                self.assertFalse(result["ok"])
                self.assertNotIn("_staged_file", result)
                self.assertEqual(self.ctx.pending_file_deliveries, [])
                self.assertEqual(self.ctx._pending_file_writes, 0)

    async def test_inflight_encoding_reserves_per_turn_quota(self) -> None:
        self.ctx.config.catty_file_delivery_max_per_turn = 1
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_write(*_args):
            entered.set()
            await release.wait()
            return {"format": "text", "bytes": 1}

        with patch.object(_tools.asyncio, "to_thread", new=AsyncMock(side_effect=delayed_write)) as write:
            first = asyncio.create_task(_tools._exec_write_file({"content": "a"}, self.ctx))
            try:
                await entered.wait()
                second = await _tools._exec_write_file({"content": "b"}, self.ctx)
                self.assertFalse(second["ok"])
                self.assertIn("最多生成", second["error"])
            finally:
                release.set()
                result = await first
        self.assertTrue(result["ok"])
        self.assertEqual(write.await_count, 1)
        self.assertEqual(self.ctx._pending_file_writes, 0)
        self.assertEqual(len(self.ctx.pending_file_deliveries), 1)


class PythonRunAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = _tools.ToolContext(
            config=types.SimpleNamespace(catty_owner_qq="owner"),
            event=types.SimpleNamespace(user_id="owner"), memory_store=None, scope_key="group:42",
        )
        self.module_name = f"{_PACKAGE_NAME}.code_sandbox"

    async def _run_with_result(self, result):
        module = types.ModuleType(self.module_name)
        module.run_code = AsyncMock(return_value=result)
        with patch.dict(sys.modules, {self.module_name: module}):
            actual = await _tools._exec_python_run({"code": "print('offline')"}, self.ctx)
        module.run_code.assert_awaited_once_with(
            self.ctx.config, "print('offline')", scope="group:42:user:owner"
        )
        return actual

    async def test_success_only_stages_files_and_leaves_partial_files_as_feedback(self) -> None:
        result = {"ok": True, "files": [{"path": "exports/run-1/report.txt", "name": "report.txt"}, {}, None],
                  "partial_files": [{"path": "work/partial.txt", "name": "partial.txt"}]}
        actual = await self._run_with_result(result)
        self.assertIs(actual, result)
        self.assertEqual(self.ctx.pending_file_deliveries,
                         [{"path": "exports/run-1/report.txt", "name": "report.txt"}])
        self.assertEqual(actual["partial_files"], result["partial_files"])

    async def test_failure_or_timeout_does_not_queue_even_if_files_are_present(self) -> None:
        for timed_out in (False, True):
            result = {"ok": False, "timed_out": timed_out,
                      "files": [{"path": "old/unsafe.txt"}],
                      "partial_files": [{"path": "work/partial.txt"}]}
            actual = await self._run_with_result(result)
            self.assertIs(actual, result)
            self.assertEqual(self.ctx.pending_file_deliveries, [])

    async def test_contract_typeerror_is_not_swallowed_or_retried(self) -> None:
        module = types.ModuleType(self.module_name)
        module.run_code = AsyncMock(side_effect=TypeError("scope contract mismatch"))
        with patch.dict(sys.modules, {self.module_name: module}):
            with self.assertRaisesRegex(TypeError, "scope contract mismatch"):
                await _tools._exec_python_run({"code": "pass"}, self.ctx)
        self.assertEqual(module.run_code.await_count, 1)
        self.assertEqual(self.ctx.pending_file_deliveries, [])

    async def test_existing_owner_gate_is_unchanged(self) -> None:
        self.ctx.event.user_id = "guest"
        result = await _tools._exec_python_run({"code": "pass"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(self.ctx.pending_file_deliveries, [])


class RecallUserMessagesAuditTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, *, mode="group", messages=None, history=None):
        cache = Mock()
        cache.get.return_value = history or []
        return types.SimpleNamespace(
            group_id="42", scope_key="group:42", session_cache=cache,
            config=types.SimpleNamespace(catty_group_history_scope=mode),
            recent_messages=messages or [],
        )

    async def test_group_mode_uses_exact_sender_not_other_people_or_bot(self) -> None:
        ctx = self._ctx(messages=[
            {"seq": 1, "user_id": "100", "text": "我的第一句"},
            {"seq": 2, "user_id": "1000", "text": "另一个人的话"},
            {"seq": 3, "user_id": "100", "text": "机器人回复", "is_bot": True},
            {"seq": 4, "user_id": "200", "text": "引用100的话"},
            {"seq": 5, "user_id": 100, "text": "我的第二句"},
        ])
        result = await _tools._exec_recall_user_messages({"user_id": "100", "count": 2}, ctx)
        self.assertEqual([row["content"] for row in result["messages"]], ["我的第一句", "我的第二句"])
        self.assertEqual(result["scope"], "group:42")
        self.assertEqual(result["source"], "recent_messages")
        ctx.session_cache.get.assert_not_called()

    async def test_group_no_match_does_not_read_nonexistent_per_user_history(self) -> None:
        ctx = self._ctx(history=[{"role": "user", "content": "过时的独立缓存"}])
        result = await _tools._exec_recall_user_messages({"user_id": "100"}, ctx)
        self.assertEqual(result["count"], 0)
        self.assertIn("空结果不表示", result["note"])
        ctx.session_cache.get.assert_not_called()

    async def test_user_mode_prioritizes_verified_recent_sender(self) -> None:
        ctx = self._ctx(mode="user", messages=[{"seq": 1, "user_id": "100", "text": "最新"}])
        result = await _tools._exec_recall_user_messages({"user_id": "100"}, ctx)
        self.assertEqual(result["source"], "recent_messages")
        ctx.session_cache.get.assert_not_called()

    async def test_user_mode_preserves_history_and_marks_assistant_as_not_user(self) -> None:
        ctx = self._ctx(mode="user", history=[
            {"role": "user", "content": [{"type": "text", "text": "以前的问题"}]},
            {"role": "assistant", "content": "模型的回复"},
            {"role": "user", "user_id": "other", "content": "其他人的旧记录"},
        ])
        result = await _tools._exec_recall_user_messages({"user_id": "100"}, ctx)
        ctx.session_cache.get.assert_called_once_with("group:42:user:100")
        self.assertEqual(result["scope"], "group:42:user:100")
        self.assertEqual(result["source"], "session_cache")
        self.assertEqual(result["count"], 2)
        self.assertIsNone(result["messages"][0]["user_id"])
        self.assertIsNone(result["messages"][1]["user_id"])
        self.assertEqual(result["messages"][1]["role"], "assistant")

    async def test_user_mode_checks_explicit_sender_tags_and_conflicts(self) -> None:
        ctx = self._ctx(mode="user", history=[
            {"role": "user", "content": "[QQ:100] 本人发言"},
            {"role": "user", "content": "[QQ:200] 其他人"},
            {"role": "user", "user_id": "100", "content": "[QQ:200] 冲突"},
            {"role": "user", "content": "[QQ:未知] 老记录"},
        ])
        result = await _tools._exec_recall_user_messages({"user_id": "100"}, ctx)
        self.assertEqual([row["user_id"] for row in result["messages"]], ["100", None])
        self.assertIn("身份未知", result["note"])

    async def test_count_is_capped_and_private_scope_is_still_rejected(self) -> None:
        ctx = self._ctx(messages=[{"seq": n, "user_id": "100", "text": str(n)} for n in range(30)])
        result = await _tools._exec_recall_user_messages({"user_id": "100", "count": 99}, ctx)
        self.assertEqual(result["count"], 20)
        self.assertEqual(result["messages"][0]["content"], "10")
        ctx.group_id = ""
        self.assertIn("error", await _tools._exec_recall_user_messages({"user_id": "100"}, ctx))


if __name__ == "__main__":
    unittest.main()