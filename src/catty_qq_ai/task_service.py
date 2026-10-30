from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import inspect
import json
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping

from .extension_runtime import ExtensionRegistry
from .dispatch_context import ContextArchive
from .knowledge_base import KnowledgeBase
from .mcp_runtime import MCPCallContext, MCPRuntime
from .task_contracts import TaskError, TaskIdentity, TaskPolicy, TaskResult
from .task_runtime import TaskRuntime
from .task_store import TaskStore


async def _call(function, *args, **kwargs):
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    value = await asyncio.to_thread(function, *args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def public_task(record: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: record.get(key) for key in (
        "task_id", "title", "role", "tier", "state", "points", "payment_state",
        "max_tokens", "created_at", "updated_at", "expires_at", "error", "requires_confirmation",
    ) if key in record}
    output = record.get("result")
    if isinstance(output, Mapping):
        result["summary"] = output.get("summary", "")
        result["artifacts"] = [
            {key: value for key, value in artifact.items() if key not in {"path", "local_path"}}
            for artifact in output.get("artifacts", ()) if isinstance(artifact, Mapping)
        ]
        result["sources"] = output.get("sources", [])
    if result.get("state") == "quoted" and result.get("points", 0):
        result["confirmation_command"] = f"/任务确认 {result['task_id']}"
    result["can_resume"] = result.get("state") == "interrupted"
    result["progress"] = result.get("state", "unknown")
    return {"ok": True, **result}


class TaskService:
    def __init__(
        self, config, account, *, context_factory=None, deliver=None,
        finalize=None, embedding_fn=None, config_path=None, delegator=None,
    ):
        self.config = config
        self.account = account
        self.settings = dict(getattr(config, "catty_agent_tasks", {}) or {})
        self.policy = TaskPolicy.from_mapping(self.settings)
        self.policy.root.mkdir(parents=True, exist_ok=True)
        self.store = TaskStore(self.policy.root / "tasks.sqlite3", self.policy)
        self.context_archive = ContextArchive(self.policy.root / "contexts")
        self.knowledge = KnowledgeBase(self.policy.root / "knowledge", self.settings.get("knowledge"), embedding_fn)
        self.mcp = MCPRuntime(self.settings.get("mcp", {}), self.policy)
        extension_settings = dict(self.settings.get("extensions", {}) or {})
        extension_root = extension_settings.pop("root", str(self.policy.root / "extensions"))
        extension_settings["result_reference"] = self._extension_reference
        self.extensions = ExtensionRegistry(extension_root, extension_settings)
        self.context_factory = context_factory
        self.delivery_callback = deliver
        self.finalize_callback = finalize
        self._contexts: dict[str, Any] = {}
        self._active: contextvars.ContextVar[Any] = contextvars.ContextVar("catty_task_service_context", default=None)
        self._mcp_tools: contextvars.ContextVar[Any] = contextvars.ContextVar("catty_task_mcp_tools", default={})
        self._artifact_refs: contextvars.ContextVar[Any] = contextvars.ContextVar("catty_task_artifact_refs", default=None)
        self._admin_lock = asyncio.Lock()
        self._revision = 1
        self._closed = False
        self.host_trace_store = None
        owner_id = str(getattr(config, "catty_owner_qq", "") or "0")
        self.admin_identity = TaskIdentity(owner_id, f"private:{owner_id}", "fadianji", True)
        if delegator is None:
            from .agent_delegate import AgentDelegator
            delegator = AgentDelegator(config, self.policy, self._worker_tools, self._worker_execute, knowledge=self._reference_materials)
        self.delegator = delegator
        self.runtime = TaskRuntime(self.store, self.policy, account, self._execute, self._deliver, permission_check=self._verify_identity)
        self.config_editor = None
        if config_path is not None:
            from .task_dashboard import ConfigEditor
            self.config_editor = ConfigEditor(config_path)

    async def start(self):
        if not self.policy.enabled:
            return
        await self.mcp.start()
        for extension in self.settings.get("extensions", {}).get("enabled_ids", []):
            try:
                await self.extensions.enable(extension, identity=self.admin_identity)
            except Exception:
                continue
        await self.runtime.start()

    async def close(self):
        if self._closed:
            return
        await self.runtime.stop()
        await self.mcp.close()
        await self.extensions.close()
        self.knowledge.close()
        self.store.close()
        self._contexts.clear()
        self._closed = True

    async def _verify_identity(self, identity):
        fresh = TaskIdentity(identity.user_id, identity.scope, identity.persona, bool(self.account.is_owner(identity.user_id)))
        if fresh.is_owner != identity.is_owner:
            return None
        if self.context_factory is not None:
            context = await _call(self.context_factory, fresh, None)
            if context is None:
                return None
        return fresh

    async def _context(self, record, identity, budget):
        context = await _call(self.context_factory, identity, record) if self.context_factory is not None else self._contexts.get(record["task_id"])
        if context is None:
            raise TaskError("context_unavailable", "the original conversation is not available")
        context = copy.copy(context)
        context.task_service = self
        context.task_root_id = record["task_id"]
        context.task_budget = budget
        context.dispatch_context_id = str((record.get("payload") or {}).get("context_id", ""))
        for name in ("pending_image_segments", "pending_voice_deliveries", "pending_file_deliveries"):
            setattr(context, name, [])
        return context

    async def _execute_bound(self, record, identity, budget):
        if bool(self.account.is_owner(identity.user_id)) != identity.is_owner:
            raise TaskError("identity_changed", "task privileges changed")
        context = await self._context(record, identity, budget)
        token = self._active.set(context)
        mcp_token = self._mcp_tools.set({})
        artifact_token = self._artifact_refs.set([])
        try:
            descriptors = await self.mcp.list_tools(identity)
            self._mcp_tools.set({item["name"]: item for item in descriptors})
            payload = record.get("payload") or {}
            kind = payload.get("kind", "delegate")
            if kind == "mcp":
                result = await self._mcp_call(identity, payload, context)
                return await self._external_result(record["task_id"], result)
            if kind == "extension":
                permissions = self._extension_budget(context)
                permissions["allowed_tools"] = [payload["tool"]]
                result = await self.extensions.call_tool(identity, payload["tool"], payload.get("arguments", {}), permissions)
                return await self._external_result(record["task_id"], result)
            worker_record = {**record, "payload": {key: value for key, value in payload.items() if key in {"materials", "references", "subtasks"}}}
            if context.dispatch_context_id:
                worker_record["payload"]["materials"] = [
                    *worker_record["payload"].get("materials", []),
                    f"Original context is available through catty_context with context_id={context.dispatch_context_id}. Read only needed sources; omitted text has not been reviewed.",
                ]
            result = await self.delegator.execute(worker_record, identity, budget)
            return TaskResult(result.summary, (*result.artifacts, *self._artifact_refs.get()), result.sources, result.metadata)
        finally:
            self._artifact_refs.reset(artifact_token)
            self._mcp_tools.reset(mcp_token)
            self._active.reset(token)

    async def _execute(self, record, identity, budget):
        trace_scope = getattr(self, "trace_scope", None)
        manager = trace_scope(record, identity) if callable(trace_scope) else nullcontext()
        with manager:
            return await self._execute_bound(record, identity, budget)

    async def _external_result(self, task_id, result):
        if not isinstance(result, Mapping) or result.get("ok") is False or result.get("error"):
            raise TaskError("external_tool_failed", "the external tool did not complete successfully")
        encoded = json.dumps(result, ensure_ascii=False)
        await self._write_artifact(task_id, "external-result.json", encoded.encode("utf-8"), "application/json")
        return TaskResult(encoded[:self.policy.summary_max_chars], tuple(self._artifact_refs.get() or ()))

    async def _extension_reference(self, identity, name, text):
        context = self._active.get()
        if context is None or context.user_id != identity.user_id or context.scope_key != identity.scope:
            raise TaskError("context_unavailable", "extension result has no authenticated task")
        artifact = await self._write_artifact(context.task_root_id, "extension-result.json", text.encode("utf-8"), "application/json")
        return artifact["artifact_id"]

    async def _deliver(self, record, identity, result):
        if self.delivery_callback is None:
            return {"status": "not_delivered", "reason": "delivery_unavailable"}
        if self.finalize_callback is not None and not record.get("reminder"):
            try:
                summary = await asyncio.wait_for(_call(self.finalize_callback, identity, result), timeout=20)
                if isinstance(summary, str) and summary.strip():
                    result = TaskResult(summary[:self.policy.summary_max_chars], result.artifacts, result.sources, result.metadata)
            except Exception:
                pass
        receipt = await _call(self.delivery_callback, record, identity, result)
        self._contexts.pop(record["task_id"], None)
        return receipt

    def _worker_tools(self, identity):
        from .tools import available_tool_schemas
        context = self._active.get()
        if context is None:
            return []
        schemas = available_tool_schemas(self.config, is_private=identity.scope.startswith("private:"), persona=context.persona, is_directly_requested=True)
        forbidden = {"catty_delegate", "catty_schedule", "catty_mcp", "catty_extension", "catty_persona", "catty_run_code", "catty_imagegen", "catty_nai_director", "catty_voice_say"}
        if not identity.is_owner:
            forbidden.add("catty_read_file")
        schemas = [item for item in schemas if item["function"]["name"] not in forbidden]
        for index, item in enumerate(schemas):
            name = item["function"]["name"]
            if name in {"catty_knowledge", "catty_task_artifact"}:
                item = copy.deepcopy(item)
                item["function"]["parameters"]["properties"]["action"]["enum"] = ["search", "read", "list"] if name == "catty_knowledge" else ["read", "list"]
                schemas[index] = item
        schemas.extend({"type": "function", "function": {"name": item["name"], "description": item.get("description", ""), "parameters": item.get("inputSchema", {"type": "object"})}} for item in self._mcp_tools.get().values())
        schemas.extend(self.extensions.schemas(identity))
        return schemas

    @staticmethod
    def _extension_budget(context):
        return {"confirmed": True, "root_task_id": context.task_root_id, "identity": {
            "user_id": context.user_id, "scope": context.scope_key,
            "persona": str(getattr(context.persona, "name", "catty")),
            "is_owner": bool(context.affection_store.is_owner(context.user_id)),
        }, "allowed_tools": getattr(context, "task_allowed_tools", [])}

    async def _mcp_call(self, identity, arguments, context):
        async def write_artifact(name, content, mime):
            return await self._write_artifact(context.task_root_id, name, content, mime)
        call_context = MCPCallContext(identity, context.task_root_id, True, artifact_writer=write_artifact)
        return await self.mcp.call_tool(identity, arguments["server"], arguments["tool"], arguments.get("arguments", {}), call_context)

    async def _worker_execute(self, identity, name, arguments_json):
        context = self._active.get()
        if context is None:
            raise TaskError("context_unavailable", "worker context is not bound")
        allowed = {item["function"]["name"] for item in self._worker_tools(identity)}
        if name not in allowed:
            raise TaskError("tool_not_allowed", "tool is not available to this task")
        arguments = json.loads(arguments_json or "{}")
        if name in {"catty_knowledge", "catty_task_artifact"} and arguments.get("action") not in {"search", "read", "list"}:
            raise TaskError("tool_not_allowed", "worker knowledge and artifact access is read-only")
        if name in self._mcp_tools.get():
            descriptor = self._mcp_tools.get()[name]
            return await self._mcp_call(identity, {"server": descriptor["server"], "tool": descriptor["tool"], "arguments": arguments}, context)
        if name.startswith("ext__"):
            budget = self._extension_budget(context)
            budget["allowed_tools"] = sorted(allowed)
            return await self.extensions.call_tool(identity, name, arguments, budget)
        from .tools import execute_tool_call
        return await execute_tool_call(name, arguments_json, context, allowed_names=allowed)

    async def _reference_materials(self, identity, references):
        material = []
        for reference in references[:20]:
            context = self._active.get()
            context_id = getattr(context, "dispatch_context_id", "") if context is not None else ""
            if isinstance(reference, str) and reference.startswith("m") and reference[1:].isdigit() and context_id:
                source = await _call(self.context_archive.read, identity, context_id, reference, max_chars=6000)
                text = source["text"]
                if source.get("truncated"):
                    text += f"\n[More unreviewed source: catty_context context_id={context_id}, source_id={reference}, offset={source['next_offset']}]"
                material.append({"source_id": reference, "title": reference, "text": text})
                continue
            chunk = await _call(self.knowledge.get_chunk, identity, reference)
            if chunk:
                material.append({"source_id": chunk["chunk_id"], "title": chunk.get("title", ""), "text": chunk["text"], "url": chunk.get("source", "")})
            else:
                data = await _call(self.knowledge.read_artifact, identity, reference)
                if data is not None:
                    material.append({"source_id": reference, "text": data.decode("utf-8", errors="replace"), "title": reference})
                else:
                    raise TaskError("source_not_found", "a referenced source is not accessible")
        return material

    async def _write_artifact(self, task_id, name, content, mime="application/octet-stream"):
        if len(content) > 20 * 1024 * 1024:
            raise TaskError("artifact_too_large", "artifact exceeds its size limit")
        directory = (self.policy.root / task_id / "artifacts").resolve()
        if not directory.is_relative_to(self.policy.root):
            raise TaskError("invalid_artifact", "invalid artifact directory")
        directory.mkdir(parents=True, exist_ok=True)
        artifact_id = uuid.uuid4().hex
        suffix = Path(name).suffix[:16]
        target = directory / f"{artifact_id}{suffix}"
        await _call(target.write_bytes, content)
        result = {"artifact_id": artifact_id, "name": Path(name).name, "path": str(target), "size": len(content), "mime_type": mime, "sha256": hashlib.sha256(content).hexdigest()}
        references = self._artifact_refs.get()
        if references is not None:
            references.append(result)
        return result

    async def handle_tool(self, name, arguments, identity, context):
        if not self.policy.enabled:
            raise TaskError("disabled", "task service is disabled")
        if name == "catty_delegate":
            return await self._task_tool(arguments, identity, context)
        if name == "catty_schedule":
            return await self._schedule_tool(arguments, identity, context)
        if name == "catty_knowledge":
            return await self._knowledge_tool(arguments, identity, context)
        if name == "catty_context":
            context_id = str(arguments.get("context_id") or getattr(context, "dispatch_context_id", ""))
            action = arguments.get("action")
            if action in {"list", "search"}:
                items = await _call(self.context_archive.list_sources, identity, context_id, query=str(arguments.get("query", "")), limit=int(arguments.get("limit", 10)))
                return {"ok": True, "context_id": context_id, "sources": items}
            if action == "read":
                return {"ok": True, **await _call(self.context_archive.read, identity, context_id, str(arguments.get("source_id", "")), offset=int(arguments.get("offset", 0)), max_chars=int(arguments.get("max_chars", 6000)))}
            raise TaskError("invalid_action", "unsupported context action")
        if name in {"catty_mcp", "catty_extension"}:
            if arguments.get("action") == "list":
                items = await self.mcp.list_tools(identity) if name == "catty_mcp" else self.extensions.schemas(identity)
                return {"ok": True, "items": items}
            if arguments.get("action") != "call":
                raise TaskError("invalid_action", "unsupported action")
            if context.task_root_id:
                raise TaskError("nested_task", "use the task's scoped external tool")
            payload = {"kind": "mcp" if name == "catty_mcp" else "extension", "tool": str(arguments.get("tool", "")), "arguments": arguments.get("arguments", {})}
            if name == "catty_mcp":
                payload["server"] = str(arguments.get("server", ""))
            return await self._quote(arguments, identity, context, payload, description=f"Run approved tool {payload['tool']}")
        if name == "catty_task_artifact":
            return await self._artifact_tool(arguments, identity)
        raise TaskError("unknown_tool", "unknown task tool")

    async def _quote(self, arguments, identity, context, payload=None, description=None):
        description = str(description or arguments.get("description", "")).strip()
        if not description:
            raise TaskError("description_required", "describe the requested task")
        if payload is None:
            payload = {key: arguments[key] for key in ("materials", "references", "subtasks") if key in arguments}
        event = getattr(context, "event", None)
        payload["bot_id"] = str(getattr(event, "self_id", "") or "")
        payload["parent_turn_id"] = context.task_turn_id
        from .dispatch_context import get_dispatch_stats
        dispatch_stats = get_dispatch_stats() or {}
        context_id = str(arguments.get("context_id") or dispatch_stats.get("context_id") or getattr(context, "dispatch_context_id", ""))
        if context_id:
            await _call(self.context_archive.list_sources, identity, context_id, limit=1)
            payload["context_id"] = context_id
        fingerprint = hashlib.sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        request_id = f"{context.task_turn_id}:{fingerprint}"
        record = await _call(self.store.quote, identity, request_id, str(arguments.get("title") or description[:100]), str(arguments.get("role") or "research"), description, str(arguments.get("tier") or "light"), payload)
        self._contexts[record["task_id"]] = context
        while len(self._contexts) > 256:
            self._contexts.pop(next(iter(self._contexts)))
        if record["state"] == "quoted" and record["points"] == 0:
            record = await self.runtime.confirm(record["task_id"], identity)
        return public_task(record)

    async def _task_tool(self, arguments, identity, context):
        action = arguments.get("action")
        if action == "create":
            return await self._quote(arguments, identity, context)
        if action == "list":
            rows = await _call(self.store.list_tasks, identity)
            return {"ok": True, "items": [public_task(row) for row in rows], "usage": await _call(self.store.daily_usage, identity.user_id)}
        task_id = str(arguments.get("task_id") or "")
        if action == "status":
            record = await _call(self.store.get_task, task_id, identity)
        elif action == "confirm":
            self._contexts[task_id] = context
            record = await self.runtime.confirm(task_id, identity)
        elif action == "cancel":
            record = await self.runtime.cancel(task_id, identity)
        elif action == "resume":
            self._contexts[task_id] = context
            record = await self.runtime.resume(task_id, identity)
        else:
            raise TaskError("invalid_action", "unsupported task action")
        return public_task(record)

    async def _schedule_tool(self, arguments, identity, context):
        action = arguments.get("action")
        if action == "list":
            return {"ok": True, "items": await _call(self.store.list_schedules, identity)}
        if action == "cancel":
            return {"ok": True, "schedule": await _call(self.store.cancel_schedule, str(arguments.get("schedule_id", "")), identity, self.account)}
        if action != "create":
            raise TaskError("invalid_action", "unsupported schedule action")
        when = str(arguments.get("when", "")).strip()
        trigger = {"daily": when[6:]} if when.startswith("daily ") else {"cron": when} if len(when.split()) == 5 else {"when": when}
        description = str(arguments.get("description", "")).strip()
        payload = {"bot_id": str(getattr(getattr(context, "event", None), "self_id", "") or ""), "parent_turn_id": context.task_turn_id}
        request_id = f"{context.task_turn_id}:{hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()}"
        record = await _call(self.store.create_schedule, identity, request_id, str(arguments.get("title") or description[:100]), str(arguments.get("role") or "research"), description, tier=str(arguments.get("tier") or "light"), payload=payload, reminder=arguments.get("kind", "reminder") == "reminder", **trigger)
        self.runtime.notify()
        return {"ok": True, "schedule": record}

    async def _knowledge_tool(self, arguments, identity, context):
        action = arguments.get("action")
        if action == "list":
            return {"ok": True, "items": await _call(self.knowledge.list_documents, identity)}
        if action == "search":
            return {"ok": True, "items": await _call(self.knowledge.search, identity, str(arguments.get("query", "")), int(arguments.get("limit", 5)))}
        if action == "ingest":
            return await _call(self.knowledge.ingest_text, identity, str(arguments.get("name") or "document.txt"), str(arguments.get("text", "")))
        if action == "ingest_file":
            attachment = str(arguments.get("attachment", ""))
            item = next((item for item in getattr(context, "incoming_files", ()) if item.get("id") == attachment or item.get("name") == attachment), None)
            if item is None or not isinstance(item.get("content"), bytes):
                raise TaskError("attachment_unavailable", "the requested attachment is not available")
            return await _call(self.knowledge.ingest_bytes, identity, item["name"], item["content"], item.get("source", ""))
        if action == "ingest_url":
            from .auto_web_fetch import _fetch_page_text
            async def fetch(url):
                result = await _fetch_page_text(url, timeout=15, max_chars=100000)
                if result is not None and "text" not in result:
                    result = {**result, "text": result.get("content", "")}
                return result
            return {"ok": True, "items": await self.knowledge.ingest_urls(identity, [str(arguments.get("url", ""))], fetch)}
        if action == "read":
            return {"ok": True, "chunk": await _call(self.knowledge.get_chunk, identity, str(arguments.get("chunk_id", "")))}
        if action == "delete":
            return await _call(self.knowledge.delete_document, identity, str(arguments.get("doc_id", "")))
        if action == "rebuild":
            return await _call(self.knowledge.rebuild, identity, str(arguments.get("doc_id", "")))
        raise TaskError("invalid_action", "unsupported knowledge action")

    def _artifact(self, record, artifact_id):
        for item in (record.get("result") or {}).get("artifacts", ()):
            if item.get("artifact_id", item.get("id", item.get("name"))) != artifact_id:
                continue
            path = Path(str(item.get("path", ""))).resolve()
            if not path.is_relative_to(self.policy.root / record["task_id"]) or not path.is_file():
                raise TaskError("artifact_unavailable", "artifact is not available")
            return item, path
        raise TaskError("artifact_not_found", "artifact is not part of this task")

    async def _artifact_tool(self, arguments, identity):
        record = await _call(self.store.get_task, str(arguments.get("task_id", "")), identity)
        if arguments.get("action") == "list":
            return public_task(record)
        item, path = self._artifact(record, str(arguments.get("artifact_id", "")))
        if arguments.get("action") == "read":
            data = await _call(path.read_bytes)
            offset = max(0, int(arguments.get("offset", 0)))
            limit = min(12000, max(1, int(arguments.get("max_chars", 4000))))
            text = data.decode("utf-8", errors="replace")
            return {"ok": True, "text": text[offset:offset + limit], "next_offset": min(len(text), offset + limit), "total_chars": len(text)}
        if arguments.get("action") == "deliver" and self.delivery_callback is not None:
            return dict(await _call(self.delivery_callback, record, identity, TaskResult("", (item,))))
        raise TaskError("invalid_action", "unsupported artifact action")

    def _bump(self):
        self._revision += 1

    def _current_revision(self):
        getter = getattr(self.store, "revision", None)
        return self._revision + (int(getter()) * 100000 if callable(getter) else 0)

    def _collection(self, items):
        return {"items": items, "revision": self._current_revision(), "total": len(items)}

    def _check_revision(self, expected):
        if type(expected) is not int or expected != self._current_revision():
            raise TaskError("stale_revision", "refresh before modifying this state")

    async def list_tasks(self, *, status=None, owner_id=None, limit=50, offset=0):
        rows = await _call(self.store.admin_list_tasks, status=status, user_id=owner_id, limit=limit, offset=offset)
        result = self._collection([{**public_task(row), "user_id": row["user_id"], "scope": row["scope"]} for row in rows])
        result["total"] = await _call(self.store.admin_task_count, status=status, user_id=owner_id)
        result["has_more"] = offset + len(rows) < result["total"]
        return result

    async def get_task(self, *, task_id):
        return public_task(await _call(self.store.admin_get_task, task_id))

    async def mutate_task(self, *, task_id, action, expected_revision, delivered=None):
        async with self._admin_lock:
            self._check_revision(expected_revision)
            record = await _call(self.store.admin_get_task, task_id)
            identity = self.store.identity_of(record)
            if action == "cancel":
                result = await self.runtime.cancel(task_id, identity)
            elif action == "resume":
                result = await self.runtime.resume(task_id, identity)
            elif action == "confirm_delivery":
                result = await _call(self.store.delivery_confirm, task_id, {"status": "delivered" if delivered else "not_delivered", "receipt_id": f"admin:{task_id}:{uuid.uuid4().hex}"}, self.account)
            else:
                raise TaskError("invalid_action", "unsupported management action")
            self._bump()
            return public_task(result)

    async def list_schedules(self):
        rows = await _call(self.store.admin_list_schedules)
        return self._collection([{**row, "name": (row.get("template") or {}).get("title", ""), "status": row["state"], "enabled": row["state"] == "active", "next_run_at": row.get("next_run")} for row in rows])

    async def mutate_schedule(self, *, schedule_id, action, expected_revision):
        async with self._admin_lock:
            self._check_revision(expected_revision)
            rows = await _call(self.store.admin_list_schedules)
            row = next((row for row in rows if row["schedule_id"] == schedule_id), None)
            if row is None:
                raise TaskError("not_found", "schedule not found")
            identity = self.store.identity_of(row)
            method = self.store.resume_schedule if action == "resume" else getattr(self.store, "pause_schedule", None) if action == "pause" else self.store.cancel_schedule
            if method is None:
                raise TaskError("unsupported_action", "this schedule cannot be paused")
            result = await _call(method, schedule_id, identity)
            self._bump()
            self.runtime.notify()
            return {"ok": True, "schedule": result}

    async def list_knowledge(self):
        rows = await _call(self.knowledge.admin_list_documents)
        return self._collection([{**row, "document_id": row["doc_id"]} for row in rows])

    async def search_knowledge(self, *, query, limit=20):
        rows = await _call(self.knowledge.search, self.admin_identity, query, limit)
        return self._collection([{**row, "document_id": row["doc_id"], "snippet": row.get("text", "")[:1000]} for row in rows])

    async def upload_knowledge(self, *, filename, content, expected_revision):
        async with self._admin_lock:
            self._check_revision(expected_revision)
            result = await _call(self.knowledge.ingest_bytes, self.admin_identity, filename, content)
            self._bump()
            return result

    async def delete_knowledge(self, *, document_id, expected_revision):
        async with self._admin_lock:
            self._check_revision(expected_revision)
            rows = await _call(self.knowledge.admin_list_documents)
            row = next((row for row in rows if row["doc_id"] == document_id), None)
            if row is None:
                raise TaskError("not_found", "document not found")
            identity = TaskIdentity(row["user_id"], row["scope"], row["persona"], bool(self.account.is_owner(row["user_id"])))
            result = await _call(self.knowledge.delete_document, identity, document_id)
            self._bump()
            return result

    async def rebuild_knowledge(self, *, expected_revision):
        async with self._admin_lock:
            self._check_revision(expected_revision)
            rows = await _call(self.knowledge.admin_list_documents)
            results = []
            for row in rows:
                identity = TaskIdentity(row["user_id"], row["scope"], row["persona"], bool(self.account.is_owner(row["user_id"])))
                results.append(await _call(self.knowledge.rebuild, identity, row["doc_id"]))
            self._bump()
            return {"ok": True, "items": results}

    async def list_extensions(self):
        return self._collection(self.extensions.list_extensions())

    async def set_extension_enabled(self, *, extension_id, enabled, expected_revision):
        async with self._admin_lock:
            self._check_revision(expected_revision)
            result = await (self.extensions.enable(extension_id, identity=self.admin_identity) if enabled else self.extensions.disable(extension_id, identity=self.admin_identity))
            try:
                if self.config_editor is not None:
                    current = self.config_editor.read()
                    ids = list(self.settings.get("extensions", {}).get("enabled_ids", []))
                    ids = sorted(set([*ids, extension_id])) if enabled else [item for item in ids if item != extension_id]
                    self.config_editor.update({"agent_tasks": {"extensions": {"enabled_ids": ids}}}, current["sha256"])
                    self.settings.setdefault("extensions", {})["enabled_ids"] = ids
            except Exception:
                if enabled:
                    await self.extensions.disable(extension_id, identity=self.admin_identity)
                else:
                    await self.extensions.enable(extension_id, identity=self.admin_identity)
                raise
            self._bump()
            return {"ok": True, "extension": result}

    async def list_mcp(self):
        return self._collection([{key: item.get(key) for key in ("id", "transport", "enabled", "allow_guests")} for item in self.settings.get("mcp", {}).get("servers", [])])

    async def list_mcp_tools(self, *, server_id):
        return self._collection([item for item in await self.mcp.list_tools(self.admin_identity) if item["server"] == server_id])

    async def trace(self, *, task_id=None, event=None, limit=100):
        rows = await _call(self.store.admin_events, task_id=task_id, limit=limit)
        if self.host_trace_store is not None:
            rows.extend(await _call(self.host_trace_store.query, task_id=task_id or "", limit=limit))
        rows = [row for row in rows if not event or row.get("kind", row.get("event")) == event]
        rows.sort(key=lambda row: row.get("created_at", row.get("timestamp", 0)), reverse=True)
        return self._collection(rows[:limit])

    async def diagnostics(self):
        rows = await _call(self.store.admin_list_tasks, limit=500)
        counts = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        modules = [
            {"name": "Task ledger", "status": "ready"},
            {"name": self.policy.worker_model, "status": "failed" if self.runtime.last_error else "configured", "error_code": self.runtime.last_error},
            {"name": "Knowledge base", "status": "ready" if self.settings.get("knowledge", {}).get("enabled", True) else "disabled"},
            {"name": "MCP", "status": "configured" if self.settings.get("mcp", {}).get("servers") else "unconfigured"},
            {"name": "Extensions", "status": "configured" if self.settings.get("extensions", {}).get("enabled_ids") else "unconfigured"},
        ]
        return {"ok": True, "task_counts": counts, "modules": modules, "worker_model": self.policy.worker_model, "runtime_error": self.runtime.last_error, "revision": self._current_revision(), "timestamp": time.time()}