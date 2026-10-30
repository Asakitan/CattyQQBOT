from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = "_catty_task_host_tests"
package = types.ModuleType(_PACKAGE)
package.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules[_PACKAGE] = package


def _load(name):
    qualified = f"{_PACKAGE}.{name}"
    spec = importlib.util.spec_from_file_location(qualified, _ROOT / "src" / "catty_qq_ai" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


host_module = _load("task_host")
trace = sys.modules[f"{_PACKAGE}.task_trace"]
contracts = sys.modules[f"{_PACKAGE}.task_contracts"]


class Segment:
    def __init__(self, kind, data):
        self.type, self.data = kind, data

    @classmethod
    def text(cls, text):
        return cls("text", {"text": text})


class Event:
    def __init__(self, text="", *, user_id="42", group_id="7", bot_id="99", files=(), message_id=1, directed=False):
        self.user_id, self.group_id, self.self_id = int(user_id), int(group_id) if group_id is not None else None, int(bot_id)
        self.message_id = message_id
        self.message = [Segment.text(text)] if text else []
        if directed:
            self.message.append(Segment("at", {"qq": bot_id}))
        self.message.extend(Segment("file", item) for item in files)
        self.raw_message = text

    def get_message(self):
        return self.message


class Context(types.SimpleNamespace):
    @property
    def user_id(self):
        return str(self.event.user_id)


class Account:
    owner = "1"

    def is_owner(self, user_id):
        return str(user_id) == self.owner


class FakeBot:
    self_id = "99"

    def __init__(self):
        self.calls = []
        self.members = {"42", "99", "1"}
        self.fail_send = False
        self.missing_receipt = False

    async def call_api(self, method, **arguments):
        self.calls.append((method, arguments))
        if method == "get_group_member_info":
            user_id = str(arguments["user_id"])
            return {"user_id": int(user_id)} if user_id in self.members else {}
        if method == "get_friend_list":
            return [{"user_id": 42}]
        if method in {"get_group_file_url", "get_private_file_url"}:
            return {"url": "https://cdn.example/document.txt"}
        if method.startswith("send_"):
            if self.fail_send:
                raise TimeoutError("ambiguous transport")
            return {} if self.missing_receipt else {"message_id": 123}
        if method.startswith("upload_"):
            return None
        raise AssertionError(method)


class FakeStore:
    def __init__(self):
        self.tasks = []
        self.schedules = []

    def admin_list_tasks(self, *, status, limit):
        return [item for item in self.tasks if item["state"] == status][:limit]

    def admin_list_schedules(self, *, limit):
        return self.schedules[:limit]


class FakeService:
    def __init__(self, config, account, **callbacks):
        self.config, self.account, self.callbacks = config, account, callbacks
        self.policy = types.SimpleNamespace(root=Path(config.catty_agent_tasks["data_dir"]), enabled=config.catty_agent_tasks.get("enabled", True), summary_max_chars=1500)
        self.store = FakeStore()
        self.runtime = types.SimpleNamespace(_jobs={})
        self.calls = []
        self.started, self.closed = False, False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def handle_tool(self, name, arguments, identity, context):
        self.calls.append((name, arguments, identity, context))
        if name == "catty_knowledge":
            if arguments["attachment"].endswith(".exe"):
                return {"ok": False, "error": "unsupported_format"}
            return {"ok": True, "doc_id": "doc-1"}
        return {"ok": True, "task_id": arguments.get("task_id", ""), "state": "queued", "items": []} if arguments["action"] == "list" else {"ok": True, "task_id": arguments.get("task_id", ""), "state": "queued"}


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.bot = FakeBot()
        self.account = Account()
        self.mounts = []
        self.downloads = []
        self.persona = types.SimpleNamespace(name="fadianji")
        self.namespace = {
            "config": types.SimpleNamespace(catty_agent_tasks={"data_dir": self.directory.name}),
            "affection_store": self.account, "get_bots": lambda: {"99": self.bot},
            "_persona_for_scope": lambda scope: self.persona,
            "_task_event_allowed": lambda event, config: str(event.user_id) != "666",
            "_task_mount_dashboard": lambda provider: self.mounts.append(provider),
            "_task_event_factory": lambda identity, bot_id: Event(user_id=identity.user_id, group_id=identity.scope.split(":", 1)[1] if identity.scope.startswith("group:") else None, bot_id=bot_id),
            "MessageSegment": Segment,
        }
        async def download(url, limit):
            self.downloads.append((url, limit))
            return b"document body"
        self.host = host_module.TaskHost(self.namespace, service_factory=FakeService, context_type=Context, downloader=download)
        await self.host.start()

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    def record(self, identity=None):
        identity = identity or contracts.TaskIdentity("42", "group:7", "fadianji")
        return {"task_id": "task-1", "user_id": identity.user_id, "scope": identity.scope, "persona": identity.persona, "payload": {"bot_id": "99", "parent_turn_id": "parent-turn"}}

    async def test_lifecycle_idempotent_and_current_account(self):
        service = self.host.service
        await self.host.start()
        self.assertIs(self.host.service, service)
        self.assertTrue(service.started)
        self.assertEqual(len(self.mounts), 1)
        self.assertIs(self.mounts[0](), service)
        replacement = Account()
        replacement.owner = "42"
        self.namespace["affection_store"] = replacement
        self.assertTrue(service.account.is_owner("42"))
        await self.host.close()
        self.assertTrue(service.closed)
        self.assertIsNone(self.host.service)

    async def test_installer_registers_real_lifecycle_and_isolated_matchers(self):
        driver = types.SimpleNamespace(startups=[], shutdowns=[])
        driver.on_startup = lambda callback: driver.startups.append(callback)
        driver.on_shutdown = lambda callback: driver.shutdowns.append(callback)
        registrations = []
        def on_event(**options):
            record = {**options}
            registrations.append(record)
            return types.SimpleNamespace(handle=lambda: lambda handler: record.update(handler=handler))
        namespace = {**self.namespace, "get_driver": lambda: driver, "on_message": on_event, "on_notice": on_event,
                     "MessageEvent": Event, "NoticeEvent": Event, "Bot": FakeBot, "T_State": dict}
        installed = host_module.install_task_center(namespace)
        self.assertIs(installed, host_module.install_task_center(namespace))
        installed.service_factory, installed.context_type = FakeService, Context
        installed.downloader = self.host.downloader
        await driver.startups[0]()
        try:
            self.assertEqual(len(registrations), 2)
            self.assertTrue(all(item["priority"] < 10 and item["block"] for item in registrations))
            self.assertTrue(await registrations[0]["rule"](Event("/任务"), {}))
            self.assertFalse(await registrations[0]["rule"](Event("hello"), {}))
            upload = Event(files=[{"id": "previous-file", "name": "notes.txt"}], message_id=10)
            self.assertFalse(await registrations[0]["rule"](upload, {}))
            self.assertEqual(self.downloads, [])
            request = Event("看一下", directed=True, message_id=11)
            self.assertFalse(await registrations[0]["rule"](request, {}))
            context = installed._new_context(request, self.persona)
            await installed.attach(context, self.bot, request, "turn")
            self.assertEqual([item["id"] for item in context.incoming_files], ["previous-file"])
            self.assertEqual(len(self.downloads), 1)
        finally:
            await driver.shutdowns[0]()

    async def test_commands_use_real_text_and_bypass_chat(self):
        event = Event("/任务确认 task-1")
        result = await self.host.handle(self.bot, event, {"user_text": "forged", "balance": 0})
        self.assertTrue(result["ok"])
        call = self.host.service.calls[-1]
        self.assertEqual(call[1], {"action": "confirm", "task_id": "task-1"})
        self.assertEqual(call[2], contracts.TaskIdentity("42", "group:7", "fadianji"))
        self.assertIsNone(trace.current_trace())
        forged = Event("/任务")
        forged.raw_message = "/任务确认 task-2"
        await self.host.handle(self.bot, forged, {})
        self.assertEqual(self.host.service.calls[-1][1]["action"], "list")
        await self.host.handle(self.bot, Event("/任务确认 task-3 extra"), {})
        self.assertEqual(self.host.service.calls[-1][1]["action"], "list")

    async def test_membership_and_identity_are_fail_closed(self):
        self.bot.members.remove("42")
        result = await self.host.handle(self.bot, Event("/任务确认 task-1"), {})
        self.assertFalse(result["ok"])
        self.assertEqual(self.host.service.calls, [])
        identity = contracts.TaskIdentity("42", "private:123", "fadianji")
        self.assertIsNone(await self.host.context_factory(identity, self.record(identity)))
        self.assertIsNone(await self.host.context_factory(contracts.TaskIdentity("42", "group:7", "fadianji", True), self.record()))

    async def test_restarted_context_has_original_bot_and_scope(self):
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        context = await self.host.context_factory(identity, self.record())
        self.assertEqual((context.event.self_id, context.event.group_id, context.event.user_id), (99, 7, 42))
        self.assertEqual(context.scope_key, "group:7")
        self.assertEqual(context.user_text, "")
        self.assertIsNot(context, await self.host.context_factory(identity, self.record()))
        record = self.record()
        record["payload"]["bot_id"] = "100"
        self.assertIsNone(await self.host.context_factory(identity, record))

    async def test_file_path_is_never_read_and_pure_file_is_handled(self):
        event = Event(directed=True, files=[{"name": "secret.txt", "file": "C:\\private\\secret.txt"}])
        self.assertTrue(self.host.handles(event))
        result = await self.host.handle(self.bot, event, {})
        self.assertFalse(result["ok"])
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.host.service.calls, [])
        event = Event(directed=True, files=[{"id": "qq-file", "name": "notes.txt"}])
        result = await self.host.handle(self.bot, event, {})
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.host.service.calls[-1][0], "catty_knowledge")

    async def test_file_uri_and_oversize_rejected_before_download(self):
        for file in ({"id": "x", "name": "x.txt", "url": "file:///C:/secret"},
                     {"id": "x", "name": "x.txt", "url": "http://public.example/x", "size": host_module.MAX_FILE_BYTES + 1}):
            result = await self.host.handle(self.bot, Event(directed=True, files=[file]), {})
            self.assertFalse(result["ok"])
        self.assertEqual(self.downloads, [])

    async def test_unknown_format_is_not_reported_as_ingested(self):
        result = await self.host.handle(self.bot, Event(directed=True, files=[{"id": "unknown.exe", "name": "unknown.exe"}]), {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["files"][0]["error"], "unsupported_format")

    async def test_file_name_aliases_are_preserved(self):
        for field in ("file_name", "filename"):
            with self.subTest(field=field):
                event = Event(directed=True, files=[{"file_id": "document-id", field: "notes.txt"}])
                result = await self.host.handle(self.bot, event, {})
                self.assertTrue(result["ok"])
                self.assertEqual(result["files"][0]["name"], "notes.txt")

    async def test_empty_notice_file_does_not_create_phantom_attachment(self):
        event = Event(files=[{"id": "document-id", "name": "notes.txt"}])
        event.notice_type = "group_upload"
        event.file = {}
        files = host_module.extract_incoming_files(event)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "notes.txt")

    async def test_message_and_upload_notice_are_processed_once(self):
        event = Event(directed=True, files=[{"id": "document-id", "name": "Cache_2edbb1880e2ed474"}])
        await self.host.handle(self.bot, event, {})
        notice = Event()
        notice.notice_type = "group_upload"
        notice.file = {"id": "document-id", "name": "notes.txt"}
        await self.host.handle(self.bot, notice, {})
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.host.service.calls), 1)
        self.assertEqual(sum(method.startswith("send_") for method, _ in self.bot.calls), 1)

    async def test_same_file_id_in_other_scope_is_not_suppressed(self):
        for group_id in ("7", "8"):
            await self.host.handle(self.bot, Event(group_id=group_id, directed=True, files=[{"id": "document-id", "name": "notes.txt"}]), {})
        self.assertEqual(len(self.downloads), 2)
        self.assertEqual(len(self.host.service.calls), 2)

    async def test_file_errors_are_readable_instead_of_internal_codes(self):
        receipt = self.host._format_receipt({"ok": False, "files": [{"name": "attachment.bin", "ok": False, "error": "unsupported_format"}]})
        self.assertNotIn("unsupported_format", receipt)
        self.assertIn("PDF", receipt)

    async def test_upload_notice_is_ignored_before_context_lookup(self):
        class UploadNotice(Event):
            notice_type = "group_upload"
            file = {"id": "notice-file", "name": "notice.txt"}

            def get_message(self):
                raise ValueError("notice has no message")

        notice = UploadNotice()
        result = await self.host.handle(self.bot, notice, {})
        self.assertTrue(result["ok"])
        self.assertTrue(result["ignored"])
        self.assertFalse(self.host.handles(notice))
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.host.service.calls, [])
        self.assertEqual(self.host.trace_store.query(), [])

    async def test_unaddressed_group_upload_never_downloads_or_imports(self):
        for text in ("", "sharing a file"):
            with self.subTest(text=text):
                event = Event(text, files=[{"id": "unrelated", "name": "notes.txt"}])
                self.assertFalse(self.host.handles(event))
                result = await self.host.handle(self.bot, event, {})
                self.assertTrue(result["ignored"])
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.host.service.calls, [])
        self.assertEqual(self.host.trace_store.query(), [])

    async def test_main_reply_continuation_cannot_download_unaddressed_file(self):
        event = Event("sharing a file", files=[{"id": "unrelated", "name": "notes.txt"}])
        context = self.host._new_context(event, self.persona)
        context.is_directly_requested = False
        await self.host.attach(context, self.bot, event, "turn")
        self.assertEqual(context.incoming_files, [])
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.downloads, [])

    async def test_explicit_main_reply_can_download_requested_file(self):
        event = Event("please read this file", files=[{"id": "document-id", "name": "notes.txt"}])
        context = self.host._new_context(event, self.persona)
        context.is_directly_requested = True
        await self.host.attach(context, self.bot, event, "turn")
        self.assertEqual(len(context.incoming_files), 1)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.host.service.calls, [])

    async def test_previous_file_is_downloaded_only_when_next_message_requests_it(self):
        upload = Event(files=[{"id": "previous-file", "name": "notes.txt"}], message_id=10)
        self.host.observe_files(upload)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.host.service.calls, [])
        request = Event("看一下", directed=True, message_id=11)
        self.host.observe_files(request)
        context = self.host._new_context(request, self.persona)
        await self.host.attach(context, self.bot, request, "turn")
        self.assertEqual([item["id"] for item in context.incoming_files], ["previous-file"])
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.host.service.calls, [])
        hint = self.host.attachment_prompt(context)
        self.assertIn("previous-file", hint)
        self.assertIn("notes.txt", hint)
        self.assertIn("ingest_file", hint)
        self.assertNotIn("document body", hint)
        self.assertNotIn("https://", hint)

    async def test_read_request_before_upload_is_resumed_once(self):
        request = Event("看一下我接下来发的文件", directed=True, message_id=10)
        self.host.observe_files(request)
        upload = Event(files=[{"id": "next-file", "name": "notes.txt"}], message_id=11)
        self.host.observe_files(upload)
        self.assertEqual(self.downloads, [])
        self.assertTrue(self.host.prepare_file_message(upload))
        self.assertIn("看一下我接下来发的文件", host_module.event_text(upload))
        self.assertTrue(self.host._attachments_requested(upload))
        self.assertFalse(self.host.handles(upload))
        context = self.host._new_context(upload, self.persona)
        await self.host.attach(context, self.bot, upload, "turn")
        self.assertEqual([item["id"] for item in context.incoming_files], ["next-file"])
        self.assertEqual(len(self.downloads), 1)
        self.assertFalse(self.host.prepare_file_message(Event(files=[{"id": "another", "name": "other.txt"}], message_id=12)))

    async def test_pending_request_supports_existing_nickname_extraction(self):
        self.namespace["_extract_trigger_incoming"] = lambda bot_id, event: types.SimpleNamespace(text="看一下", directly_requested=True)
        self.host.observe_files(Event("机机看一下", message_id=10))
        upload = Event(files=[{"id": "next-file", "name": "notes.txt"}], message_id=11)
        self.host.observe_files(upload)
        self.assertTrue(self.host.prepare_file_message(upload))
        self.assertIn("看一下", host_module.event_text(upload))

    async def test_pending_upload_notice_resumes_original_message_flow_once(self):
        resumed_events = []
        async def dispatch(bot, event):
            resumed_events.append(event)
            self.assertFalse(getattr(event, "notice_type", ""))
            self.assertIn("分析接下来发的文件", host_module.event_text(event))
            context = self.host._new_context(event, self.persona)
            await self.host.attach(context, bot, event, "resumed")
            self.assertEqual([item["id"] for item in context.incoming_files], ["notice-file"])
        self.namespace["_task_dispatch_message"] = dispatch
        self.host.observe_files(Event("分析接下来发的文件", directed=True, message_id=10))
        notice = Event(message_id=0)
        notice.notice_type = "group_upload"
        notice.file = {"id": "notice-file", "name": "notes.txt", "busid": 102}
        self.host.observe_files(notice)
        self.assertTrue(await self.host.resume_file_notice(self.bot, notice))
        self.assertFalse(await self.host.resume_file_notice(self.bot, notice))
        self.assertEqual(len(resumed_events), 1)
        self.assertEqual(len(self.downloads), 1)

    async def test_broadcast_cannot_arm_pending_file_read(self):
        request = Event("看一下", directed=True, message_id=10)
        request.message.append(Segment("at", {"qq": "all"}))
        self.host.observe_files(request)
        upload = Event(files=[{"id": "broadcast-file", "name": "notes.txt"}], message_id=11)
        self.host.observe_files(upload)
        self.assertFalse(self.host.prepare_file_message(upload))
        self.assertEqual(self.downloads, [])

    async def test_pending_read_request_is_scoped_and_expires(self):
        real_time = host_module.time
        request = Event("看一下", directed=True, message_id=10)
        with patch.object(host_module, "time", types.SimpleNamespace(monotonic=lambda: 0.0)):
            self.host.observe_files(request)
        for upload in (Event(user_id="43", files=[{"id": "other-user", "name": "notes.txt"}], message_id=11),
                       Event(group_id="8", files=[{"id": "other-group", "name": "notes.txt"}], message_id=11)):
            self.assertFalse(self.host.prepare_file_message(upload))
        with patch.object(host_module, "time", types.SimpleNamespace(monotonic=lambda: 121.0, time=real_time.time)):
            self.assertFalse(self.host.prepare_file_message(Event(files=[{"id": "late", "name": "notes.txt"}], message_id=12)))
        self.assertEqual(self.downloads, [])

    async def test_pending_read_is_cancelled_by_own_unrelated_message(self):
        self.host.observe_files(Event("看一下", directed=True, message_id=10))
        self.host.observe_files(Event("不用看了", message_id=11))
        upload = Event(files=[{"id": "cancelled", "name": "notes.txt"}], message_id=12)
        self.host.observe_files(upload)
        self.assertFalse(self.host.prepare_file_message(upload))
        self.assertEqual(self.downloads, [])

    async def test_future_file_request_does_not_read_an_older_file(self):
        self.host.observe_files(Event(files=[{"id": "older", "name": "old.txt"}], message_id=9))
        request = Event("看一下我接下来发的文件", directed=True, message_id=10)
        self.host.observe_files(request)
        context = self.host._new_context(request, self.persona)
        await self.host.attach(context, self.bot, request, "turn")
        self.assertEqual(context.incoming_files, [])
        upload = Event(files=[{"id": "next-file", "name": "notes.txt"}], message_id=11)
        self.host.observe_files(upload)
        self.assertTrue(self.host.prepare_file_message(upload))

    async def test_recent_file_metadata_has_bounded_memory_and_no_payload(self):
        for user in range(host_module.MAX_RECENT_FILE_SCOPES + 2):
            event = Event(user_id=str(1000 + user), files=[{
                "id": f"file-{user}", "name": "notes.txt", "url": "https://cdn.example/document.txt",
                "content": b"MUST_NOT_BE_CACHED", "arbitrary": "MUST_NOT_BE_CACHED",
            }])
            self.host.observe_files(event)
        self.assertEqual(len(self.host._recent_files), host_module.MAX_RECENT_FILE_SCOPES)
        for record in self.host._recent_files.values():
            for item in record["files"]:
                self.assertEqual(set(item), {"id", "name", "url", "busid", "size"})
                self.assertNotIn("content", item)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.bot.calls, [])

    async def test_explicit_old_file_reference_is_allowed_after_intervening_text(self):
        self.host.observe_files(Event(files=[{"id": "previous-file", "name": "notes.txt"}], message_id=10))
        self.host.observe_files(Event("换个话题", message_id=11))
        request = Event("帮我读一下刚才的文件", directed=True, message_id=12)
        self.host.observe_files(request)
        context = self.host._new_context(request, self.persona)
        await self.host.attach(context, self.bot, request, "turn")
        self.assertEqual([item["id"] for item in context.incoming_files], ["previous-file"])

    async def test_negative_read_request_does_not_download_previous_file(self):
        self.host.observe_files(Event(files=[{"id": "previous-file", "name": "notes.txt"}], message_id=10))
        request = Event("不要读刚才的文件", directed=True, message_id=11)
        self.host.observe_files(request)
        context = self.host._new_context(request, self.persona)
        await self.host.attach(context, self.bot, request, "turn")
        self.assertEqual(context.incoming_files, [])
        self.assertEqual(self.host.attachment_prompt(context), "")
        self.assertEqual(self.downloads, [])

    async def test_recent_file_is_not_downloaded_for_unaddressed_request_or_greeting(self):
        self.host.observe_files(Event(files=[{"id": "previous-file", "name": "notes.txt"}], message_id=10))
        for request in (Event("看一下", message_id=11), Event("你好", directed=True, message_id=12)):
            self.host.observe_files(request)
            context = self.host._new_context(request, self.persona)
            await self.host.attach(context, self.bot, request, "turn")
            self.assertEqual(context.incoming_files, [])
        self.assertEqual(self.downloads, [])

    async def test_recent_file_lookup_is_scoped_to_user_group_bot_and_persona(self):
        self.host.observe_files(Event(files=[{"id": "previous-file", "name": "notes.txt"}], message_id=10))
        for request in (Event("看看刚才的文件", directed=True, user_id="43", message_id=11),
                        Event("看看刚才的文件", directed=True, group_id="8", message_id=11),
                        Event("看看刚才的文件", directed=True, bot_id="100", message_id=11)):
            context = self.host._new_context(request, self.persona)
            await self.host.attach(context, self.bot, request, "turn")
            self.assertEqual(context.incoming_files, [])
        self.persona = types.SimpleNamespace(name="catty")
        request = Event("看看刚才的文件", directed=True, message_id=11)
        context = self.host._new_context(request, self.persona)
        await self.host.attach(context, self.bot, request, "turn")
        self.assertEqual(context.incoming_files, [])
        self.assertEqual(self.downloads, [])

    async def test_expired_recent_file_is_not_downloaded(self):
        real_time = host_module.time
        with patch.object(host_module, "time", types.SimpleNamespace(monotonic=lambda: 0.0)):
            self.host.observe_files(Event(files=[{"id": "previous-file", "name": "notes.txt"}], message_id=10))
        request = Event("看看刚才的文件", directed=True, message_id=11)
        context = self.host._new_context(request, self.persona)
        with patch.object(host_module, "time", types.SimpleNamespace(monotonic=lambda: 601.0, time=real_time.time)):
            await self.host.attach(context, self.bot, request, "turn")
        self.assertEqual(context.incoming_files, [])
        self.assertEqual(self.downloads, [])

    async def test_generic_look_does_not_reuse_a_file_after_intervening_own_text(self):
        self.host.observe_files(Event(files=[{"id": "previous-file", "name": "notes.txt"}], message_id=10))
        self.host.observe_files(Event("换个话题", message_id=11))
        request = Event("看一下", directed=True, message_id=12)
        self.host.observe_files(request)
        context = self.host._new_context(request, self.persona)
        await self.host.attach(context, self.bot, request, "turn")
        self.assertEqual(context.incoming_files, [])
        self.assertEqual(self.downloads, [])

    async def test_upload_notice_can_be_referenced_without_eager_download(self):
        notice = Event(message_id=0)
        notice.notice_type = "group_upload"
        notice.file = {"id": "notice-file", "name": "notes.txt", "busid": 102}
        self.host.observe_files(notice)
        self.assertEqual(self.bot.calls, [])
        request = Event("读一下刚才的附件", directed=True, message_id=11)
        self.host.observe_files(request)
        context = self.host._new_context(request, self.persona)
        await self.host.attach(context, self.bot, request, "turn")
        self.assertEqual([item["id"] for item in context.incoming_files], ["notice-file"])
        self.assertEqual(len(self.downloads), 1)
        self.assertIn(("get_group_file_url", {"file_id": "notice-file", "group_id": 7, "busid": 102}), self.bot.calls)

    async def test_private_upload_and_reply_to_bot_are_direct(self):
        private_event = Event(group_id=None, files=[{"id": "private-file", "name": "private.txt"}])
        self.assertTrue(self.host.handles(private_event))
        self.assertTrue((await self.host.handle(self.bot, private_event, {}))["ok"])
        reply_event = Event(files=[{"id": "reply-file", "name": "reply.txt"}])
        reply_event.reply = {"sender": {"user_id": 99}}
        self.assertTrue(self.host.handles(reply_event))
        self.assertTrue((await self.host.handle(self.bot, reply_event, {}))["ok"])
        self.assertEqual(len(self.downloads), 2)

    async def test_mentions_of_others_or_everyone_do_not_request_files(self):
        for target in ("all", "123"):
            event = Event(bot_id=target, directed=True, files=[{"id": "unrelated", "name": "notes.txt"}]) if target != "all" else Event(files=[{"id": "unrelated", "name": "notes.txt"}])
            event.self_id = 99
            if target == "all":
                event.message.append(Segment("at", {"qq": "all"}))
            self.assertFalse(self.host.handles(event))
            self.assertTrue((await self.host.handle(self.bot, event, {}))["ignored"])
        self.assertEqual(self.downloads, [])

    async def test_private_recovery_checks_friendship_and_cached_event_expiry(self):
        identity = contracts.TaskIdentity("42", "private:42", "fadianji")
        self.assertIsNotNone(await self.host.context_factory(identity, self.record(identity)))
        stranger = contracts.TaskIdentity("43", "private:43", "fadianji")
        event = Event(user_id="43", group_id=None)
        self.host.remember(event)
        self.host._bindings[stranger.ownership_key] = (0, "99", event)
        self.assertIsNone(await self.host.context_factory(stranger, self.record(stranger)))

    async def test_summary_is_literal_text_in_original_scope(self):
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        result = await self.host.deliver(self.record(), identity, contracts.TaskResult("[CQ:at,qq=all]"))
        self.assertEqual(result["status"], "delivered")
        calls = [(method, args) for method, args in self.bot.calls if method.startswith("send_")]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["group_id"], 7)
        self.assertEqual(calls[0][1]["message"].type, "text")
        self.assertEqual(calls[0][1]["message"].data["text"], "[CQ:at,qq=all]")

    async def test_delivery_unknown_never_retries(self):
        self.bot.fail_send = True
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        result = await self.host.deliver(self.record(), identity, contracts.TaskResult("ready"))
        self.assertEqual(result["status"], "delivery_unknown")
        self.assertEqual(sum(method.startswith("send_") for method, _ in self.bot.calls), 1)

    async def test_missing_message_receipt_is_unknown(self):
        self.bot.missing_receipt = True
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        result = await self.host.deliver(self.record(), identity, contracts.TaskResult("ready"))
        self.assertEqual(result["status"], "delivery_unknown")

    async def test_artifact_path_and_hash_verified_before_any_send(self):
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        path = Path(self.directory.name) / "outside.txt"
        path.write_bytes(b"artifact")
        artifact = {"name": "result.txt", "path": str(path), "sha256": hashlib.sha256(b"artifact").hexdigest()}
        result = await self.host.deliver(self.record(), identity, contracts.TaskResult("ready", (artifact,)))
        self.assertEqual(result["status"], "not_delivered")
        self.assertFalse(any(method.startswith("send_") for method, _ in self.bot.calls))
        inside = Path(self.directory.name) / "task-1" / "result.txt"
        inside.parent.mkdir()
        inside.write_bytes(b"changed")
        artifact["path"] = str(inside)
        result = await self.host.deliver(self.record(), identity, contracts.TaskResult("ready", (artifact,)))
        self.assertEqual(result["reason"], "artifact_hash_mismatch")
        artifact["sha256"] = hashlib.sha256(b"changed").hexdigest()
        result = await self.host.deliver(self.record(), identity, contracts.TaskResult("ready", (artifact,)))
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(sum(method == "upload_group_file" for method, _ in self.bot.calls), 1)

    async def test_hot_reload_waits_for_confirmed_work(self):
        old = self.host.service
        old.store.tasks = [{"state": "running"}]
        self.namespace["config"].catty_agent_tasks = {"data_dir": self.directory.name, "enabled": False}
        await self.host.refresh()
        self.assertIs(self.host.service, old)
        self.assertTrue(self.host.pending_reload)
        self.assertFalse(old.closed)
        old.store.tasks.clear()
        await self.host.refresh()
        self.assertTrue(old.closed)
        self.assertIsNot(self.host.service, old)
        self.assertFalse(self.host.pending_reload)
        self.assertFalse(self.host.service.policy.enabled)

    async def test_turn_and_child_trace_reset_and_parent_link(self):
        event = Event("hello")
        with ExitStack() as stack:
            turn_id = self.host.bind_turn(event, {}, stack)
            context = self.host._new_context(event, self.persona)
            await self.host.attach(context, self.bot, event, turn_id)
            async def execute(name, arguments):
                return {"ok": True, "task_id": "task-1", "content": "not stored"}
            await host_module.traced_tool_call(execute, "catty_delegate", "{}")
            self.assertEqual(trace.current_trace().trace_id, turn_id)
        self.assertIsNone(trace.current_trace())
        self.assertEqual(self.host._turns, 0)
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        with self.host.trace_scope(self.record(), identity):
            self.assertEqual(trace.current_trace().parent_id, "parent-turn")
        self.assertIsNone(trace.current_trace())
        self.assertNotIn("not stored", self.host.trace_store.export())

    async def test_dashboard_hook_records_only_live_trace_and_does_not_break_stats(self):
        dashboard = _load("dashboard_state")
        usage = {"prompt_tokens": 30, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 10}}
        dashboard.push_cache_stats("group:7", usage, model="test")
        self.assertEqual(self.host.trace_store.query(), [])
        with ExitStack() as stack:
            self.host.bind_turn(Event("hello"), {}, stack)
            dashboard.push_cache_stats("group:7", usage, model="test")
            rows = [row for row in self.host.trace_store.query() if row["kind"] == "model_usage"]
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["input_tokens"], rows[0]["output_tokens"]), (30, 5))
            with patch.object(self.host.trace_store, "record", side_effect=RuntimeError("trace failure")):
                dashboard.push_cache_stats("group:7", usage, model="test")
        self.assertIsNone(trace.current_trace())

    async def test_finalizer_uses_current_main_route_and_small_material_packet(self):
        calls = []
        async def completion(config, messages):
            calls.append((config, messages))
            return "整理好了，文件和引用都在。"
        config = self.namespace["config"]
        config.catty_openai_api_key = "TOKEN"
        config.catty_openai_base_url = "https://main.example/v1"
        config.catty_openai_model = "main-model"
        config.catty_max_tokens = 4096
        config.catty_openai_extra_body = {"max_output_tokens": 9999}
        self.namespace["chat_completion"] = completion
        self.namespace["_task_finalizer_context"] = lambda identity: nullcontext()
        result = contracts.TaskResult("summary " * 1000, ({"name": "result.txt", "path": "PRIVATE_PATH", "artifact_id": "a1"},), ({"title": "Reference", "chunk_id": "c1", "text": "PRIVATE_FULL_DOCUMENT"},))
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        summary = await self.host.finalize(identity, result)
        self.assertEqual(summary, "整理好了，文件和引用都在。")
        used_config, messages = calls[0]
        self.assertEqual((used_config.catty_openai_base_url, used_config.catty_openai_model), (config.catty_openai_base_url, config.catty_openai_model))
        self.assertEqual((used_config.catty_max_tokens, used_config.catty_openai_extra_body["max_output_tokens"]), (512, 512))
        self.assertEqual((config.catty_max_tokens, config.catty_openai_extra_body["max_output_tokens"]), (4096, 9999))
        self.assertEqual(len(messages), 2)
        self.assertLess(len(messages[1]["content"]), 2500)
        self.assertNotIn("PRIVATE_PATH", str(messages))
        self.assertNotIn("PRIVATE_FULL_DOCUMENT", str(messages))

    async def test_finalizer_failure_preserves_summary(self):
        async def completion(config, messages):
            raise TimeoutError("provider timeout")
        self.namespace["config"].catty_openai_api_key = "TOKEN"
        self.namespace["chat_completion"] = completion
        self.namespace["_task_finalizer_context"] = lambda identity: nullcontext()
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        self.assertEqual(await self.host.finalize(identity, contracts.TaskResult("original summary")), "original summary")

    async def test_finalizer_absent_route_does_not_call_model(self):
        async def completion(config, messages):
            self.fail("model called without an available main route")
        self.namespace["chat_completion"] = completion
        identity = contracts.TaskIdentity("42", "group:7", "fadianji")
        self.assertEqual(await self.host.finalize(identity, contracts.TaskResult("original")), "original")


if __name__ == "__main__":
    unittest.main()
