from __future__ import annotations

import inspect
import re
import uuid
from typing import Any

from .task_contracts import TaskError, TaskIdentity
from .dispatch_context import CONTEXT_TOOL_SCHEMA


def _schema(name: str, description: str, actions: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"action": {"type": "string", "enum": actions}, **properties},
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    }


_TEXT = {"type": "string"}
_TASK_ID = {"type": "string", "description": "Task id from a previous receipt."}
TASK_TOOL_SCHEMAS = {
    "catty_delegate": _schema(
        "catty_delegate",
        "Delegate substantial research, document work, analysis or file creation to bounded DeepSeek workers. Create returns a quote; free tasks start automatically. Paid tasks require the user's explicit confirmation. Queued/running is not completed. Do not pass the entire chat history.",
        ["create", "confirm", "status", "list", "cancel", "resume"],
        {
            "task_id": _TASK_ID,
            "title": {"type": "string", "maxLength": 120},
            "description": {"type": "string", "maxLength": 12000},
            "context_id": {"type": "string", "description": "Original context id from the current archive marker."},
            "role": {"type": "string", "enum": ["research", "document", "analysis", "artifact"]},
            "tier": {"type": "string", "enum": ["light", "standard", "deep"]},
            "materials": {"type": "array", "maxItems": 12, "items": _TEXT},
            "references": {"type": "array", "maxItems": 20, "items": _TEXT},
            "subtasks": {
                "type": "array", "maxItems": 3,
                "items": {"type": "object", "properties": {"role": _TEXT, "description": _TEXT}, "required": ["role", "description"], "additionalProperties": False},
            },
        },
    ),
    "catty_schedule": _schema(
        "catty_schedule", "Create and manage this user's persistent reminders or scheduled agent tasks. Use explicit timezone-aware dates or a five-field cron expression. Each agent run has its own quota and approved maximum price.",
        ["create", "list", "cancel"],
        {
            "schedule_id": _TEXT, "title": _TEXT, "description": _TEXT,
            "when": {"type": "string", "description": "ISO datetime with timezone, daily HH:MM, or cron expression."},
            "kind": {"type": "string", "enum": ["reminder", "agent"]},
            "role": {"type": "string", "enum": ["research", "document", "analysis", "artifact"]},
            "tier": {"type": "string", "enum": ["light", "standard", "deep"]},
        },
    ),
    "catty_knowledge": _schema(
        "catty_knowledge", "Import or search documents in this user's current conversation and persona. Preserve returned document/page/chunk citations. attachment must identify an actually received file, never an arbitrary local path.",
        ["ingest", "ingest_file", "ingest_url", "search", "list", "read", "delete", "rebuild"],
        {
            "name": _TEXT, "text": {"type": "string", "maxLength": 200000},
            "attachment": _TEXT, "url": _TEXT, "query": _TEXT, "doc_id": _TEXT, "chunk_id": _TEXT,
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    ),
    "catty_mcp": _schema(
        "catty_mcp", "List approved external MCP tools or request a bounded task to call one. Server configuration and permissions are administrator-controlled; a call may return a quote rather than execute immediately.",
        ["list", "call"],
        {"server": _TEXT, "tool": _TEXT, "arguments": {"type": "object"}, "tier": {"type": "string", "enum": ["light", "standard", "deep"]}},
    ),
    "catty_extension": _schema(
        "catty_extension", "List enabled, approved extensions or request their tools. Installation and enable/disable are available only in the authenticated administration console.",
        ["list", "call"],
        {"tool": _TEXT, "arguments": {"type": "object"}, "tier": {"type": "string", "enum": ["light", "standard", "deep"]}},
    ),
    "catty_task_artifact": _schema(
        "catty_task_artifact", "Read a bounded portion of this user's task artifact, or request delivery to the original conversation. Use only task/artifact ids returned by the service.",
        ["list", "read", "deliver"],
        {"task_id": _TASK_ID, "artifact_id": _TEXT, "offset": {"type": "integer", "minimum": 0}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000}},
    ),
}


TASK_TOOL_SCHEMAS["catty_context"] = CONTEXT_TOOL_SCHEMA


def identity_from_context(context: Any) -> TaskIdentity:
    user_id = str(getattr(context, "user_id", "") or "")
    account = getattr(context, "affection_store", None)
    checker = getattr(account, "is_owner", None)
    return TaskIdentity(
        user_id=user_id,
        scope=str(getattr(context, "scope_key", "") or ""),
        persona=str(getattr(getattr(context, "persona", None), "name", "catty") or "catty"),
        is_owner=bool(checker(user_id)) if callable(checker) else False,
    )


def explicit_task_confirmation(text: str, task_id: str) -> bool:
    if not task_id or len(task_id) > 128:
        return False
    return re.fullmatch(
        r"\s*/?(?:任务确认|确认任务|确认|同意|开始|任务继续|继续任务|继续|恢复任务)\s*[:：]?\s*"
        + re.escape(task_id) + r"\s*[。.!！]?\s*",
        text or "",
    ) is not None


async def execute_task_tool(name: str, arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    if name not in TASK_TOOL_SCHEMAS:
        return {"ok": False, "error": "unknown_task_tool"}
    settings = getattr(getattr(context, "config", None), "catty_agent_tasks", {})
    if not isinstance(settings, dict) or not settings.get("enabled", True):
        return {"ok": False, "error": "task_service_disabled"}
    if {"user_id", "scope", "is_owner", "model", "base_url", "api_key", "price", "token_budget"} & set(arguments):
        return {"ok": False, "error": "reserved_task_arguments"}
    accepted = set(TASK_TOOL_SCHEMAS[name]["function"]["parameters"]["properties"])
    if set(arguments) - accepted:
        return {"ok": False, "error": "unknown_task_arguments"}
    if name in {"catty_delegate", "catty_schedule"} and getattr(context, "task_root_id", ""):
        return {"ok": False, "error": "nested_delegation_forbidden"}
    try:
        identity = identity_from_context(context)
    except ValueError:
        return {"ok": False, "error": "task_identity_unavailable"}
    if name == "catty_delegate" and arguments.get("action") in {"confirm", "resume"}:
        if not explicit_task_confirmation(
            str(getattr(context, "user_text", "") or ""), str(arguments.get("task_id", "") or ""),
        ):
            return {"ok": False, "error": "user_confirmation_required", "message": "Send /任务确认 <task_id> to authorize this task."}
    service = getattr(context, "task_service", None)
    if service is None:
        return {"ok": False, "error": "task_service_unavailable"}
    if not getattr(context, "task_turn_id", ""):
        context.task_turn_id = uuid.uuid4().hex
    try:
        result = service.handle_tool(name, dict(arguments), identity, context)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise TaskError("invalid_task_receipt", "task service returned an invalid receipt")
        return result
    except TaskError as exc:
        return {"ok": False, "error": exc.code, "message": str(exc)}


def task_executor(name: str):
    async def execute(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        return await execute_task_tool(name, arguments, context)
    execute.accepted_arguments = frozenset(TASK_TOOL_SCHEMAS[name]["function"]["parameters"]["properties"])
    return execute