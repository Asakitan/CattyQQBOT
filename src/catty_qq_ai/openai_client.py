from __future__ import annotations

import asyncio
import base64
from collections import deque
import contextvars
import hashlib
from io import BytesIO
import json
import logging
import time
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageSequence

from .config import Config
from .mc_status import mc_has_players
from .parsers import lenient_json_object, unwrap_content_block_repr
from .reply_markers import INLINE_IMAGE_PREFIX, INLINE_IMAGE_SUFFIX


_logger = logging.getLogger("catty_qq_ai")

# 全局云端健康状态：> time.monotonic() 时主回复直接走 fallback 不再尝试云。
_cloud_fail_until: float = 0.0


def _cloud_is_unhealthy() -> bool:
    return time.monotonic() < _cloud_fail_until


def _cloud_health_remaining_seconds() -> float:
    return max(_cloud_fail_until - time.monotonic(), 0.0)


def _mark_cloud_unhealthy(cooldown_seconds: float) -> None:
    global _cloud_fail_until
    _cloud_fail_until = time.monotonic() + max(cooldown_seconds, 0.0)


def _mark_cloud_healthy() -> None:
    global _cloud_fail_until
    _cloud_fail_until = 0.0


def _reset_cloud_health_for_tests() -> None:
    """Test-only helper: reset the global health timer."""
    _mark_cloud_healthy()


# Fallback warmup: 第一次 fallback 调用走冷启动会同时付出"模型 load 到显存"+"大 prompt 处理"
# 两份成本(~60-120s)。先发一个 1-token "hi" 把模型 load 上来,再发真正的请求,
# 让真正回复只付"prompt 处理"那一份(KV cache 已经热了)。
_fallback_warmed_at: float = 0.0
_FALLBACK_WARMUP_WINDOW_SECONDS = 120.0
_FALLBACK_WARMUP_TIMEOUT_SECONDS = 180.0


def _mark_fallback_warmed() -> None:
    global _fallback_warmed_at
    _fallback_warmed_at = time.monotonic()


def _fallback_is_warm() -> bool:
    return time.monotonic() - _fallback_warmed_at < _FALLBACK_WARMUP_WINDOW_SECONDS


ChatMessage = dict[str, Any]
ToolChoice = str | dict[str, Any]

# Phase 2B: 请求层只接收 PersonaReplyContext duck type，避免反向 import personas 形成循环。
# 未设置时严格沿用 Catty 老路径，保证已有调用方和 cache/tool 合约不变。
_current_persona_reply_context_var: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "catty_current_persona_reply_context", default=None,
)


def set_current_persona_reply_context(context: Any | None) -> contextvars.Token:
    """设置当前 async context 的 PersonaReplyContext duck type。"""
    return _current_persona_reply_context_var.set(context)


def get_current_persona_reply_context() -> Any | None:
    """读取当前请求的人格回复上下文；None 保持 Catty 兼容路径。"""
    return _current_persona_reply_context_var.get()


def reset_current_persona_reply_context(token: contextvars.Token) -> None:
    """用 set_current_persona_reply_context 返回的 token 恢复上一个上下文。"""
    _current_persona_reply_context_var.reset(token)


# 主人 2026-06-06: 部分端点 (DeepSeek 思考模型 deepseek-v4-flash 等) 不支持非 "auto" 的
# tool_choice — 强制 {"type":"function",...} 或 "required" 会 HTTP 400
# "Thinking mode does not support this tool_choice". 一旦某 (base_url, model) 被探测到拒绝
# 强制, 在 TTL 内记住它, 后续画图请求直接用 auto (实测 auto 下 AI 仍会主动调 catty_imagegen),
# 省掉每次必失败的 400 往返。
_FORCED_TOOL_CHOICE_BLOCKED: dict[str, float] = {}
_FORCED_TOOL_CHOICE_BLOCK_TTL = 3600.0


def _forced_tool_choice_blocked(endpoint_key: str) -> bool:
    if not endpoint_key:
        return False
    until = _FORCED_TOOL_CHOICE_BLOCKED.get(endpoint_key)
    return bool(until and time.monotonic() < until)


def _mark_forced_tool_choice_blocked(endpoint_key: str) -> None:
    if endpoint_key:
        _FORCED_TOOL_CHOICE_BLOCKED[endpoint_key] = time.monotonic() + _FORCED_TOOL_CHOICE_BLOCK_TTL


class OpenAICompatibleError(Exception):
    def __init__(self, public_message: str, detail: str | None = None) -> None:
        super().__init__(detail or public_message)
        self.public_message = public_message


class MCBusyError(OpenAICompatibleError):
    """MC server has players online and local fallback is gated off."""


# 同一错误码多个 catty 变体——避免连续 503 时用户看到同一句死板提示;
# random.choice 让群友觉得猫猫真的"每次都在挣扎",而不是机器复读。
_HTTP_503_VARIANTS = (
    "喵呜～{svc}那边人太多挤爆了（503），主人 30 秒后再戳人家一下嘛 ฅฅ",
    "嗷呜～{svc}炸毛了（503），猫猫帮你按 F5 几次都没用，等半分钟再来叭(尾巴垂垂)",
    "哼…{svc}今天闹脾气（503），人家也没办法喵，30s 后重戳大概率好啦~",
    "{svc}排队人太多了喵（503）！主人稍等半分钟，猫猫会接着回的~",
)
_HTTP_429_VARIANTS = (
    "喵呜~{svc}嫌猫猫戳太快啦（429），让人家缓 1 分钟再试嘛(爪爪)",
    "哼…{svc}限流了（429），主人冷静一下喵，等 60 秒猫猫就能接着回~",
    "嗷呜~{svc}说人家戳得太频繁（429），等会儿就好啦 ฅฅ",
)
_HTTP_5XX_VARIANTS = (
    "喵呜～{svc}临时炸毛了（{code}），主人稍后再戳一下喵 ฅฅ",
    "嗷呜～{svc}那边好像在抢修（{code}），等会儿再来叭(尾巴轻晃)",
    "{svc}打了个喷嚏（{code}），猫猫也没辙喵，主人等 1-2 分钟再戳~",
)


def _is_catty_persona_reply_context(context: Any | None) -> bool:
    if context is None:
        return True
    persona = getattr(context, "persona", None)
    return str(getattr(persona, "name", "") or "").strip().lower() == "catty"


def _render_persona_api_transport_reply(
    context: Any,
    service_name: str,
    status_code: int,
) -> str:
    catalog = getattr(context, "catalog", None)
    template = str(getattr(catalog, "api_transport_reply", "") or "").strip()
    if not template:
        template = "{service_name}暂时不可用，请稍后再试。"

    render = getattr(context, "render", None)
    if callable(render):
        message = str(render(template, service_name=service_name, status_code=status_code)).strip()
    else:
        message = template.format(service_name=service_name, status_code=status_code).strip()

    if str(status_code) not in message:
        message = f"{message}（{status_code}）"
    if status_code in {401, 403}:
        message = f"{message} 请检查 API Key 或权限配置。"
    return message


def _catty_http_status_message(service_name: str, status_code: int) -> str:
    context = get_current_persona_reply_context()
    if not _is_catty_persona_reply_context(context):
        return _render_persona_api_transport_reply(context, service_name, status_code)

    import random as _random
    if status_code == 503:
        return _random.choice(_HTTP_503_VARIANTS).format(svc=service_name)
    if status_code == 429:
        return _random.choice(_HTTP_429_VARIANTS).format(svc=service_name)
    if status_code in {401, 403}:
        return f"喵呜，{service_name}不让猫猫进去（{status_code}），主人检查一下 API Key 或权限。"
    if 500 <= status_code < 600:
        return _random.choice(_HTTP_5XX_VARIANTS).format(svc=service_name, code=status_code)
    return f"喵呜，{service_name}请求没过（{status_code}），主人检查一下配置或稍后再试。"


_CATTY_TOOL_RESULT_FOLLOW_UP_HINT = (
    "请基于以上 tool 结果**用笨猫口吻**回答, **绝不**复读 JSON 字段名 "
    "(如 'long_term_summary' / 'matches' / 'extract' 等), "
    "**绝不**贴原始字典/数组. 把结果挑出 1-2 个有用点用猫娘语气短句串起来即可."
)
_TOOL_RESULT_ANTI_JSON_HINT = (
    "**绝不**复读 JSON 字段名 (如 'long_term_summary' / 'matches' / 'extract' 等), "
    "**绝不**贴原始字典/数组。"
)


def _tool_result_follow_up_hint() -> str:
    context = get_current_persona_reply_context()
    if _is_catty_persona_reply_context(context):
        return _CATTY_TOOL_RESULT_FOLLOW_UP_HINT

    catalog = getattr(context, "catalog", None)
    prefix = str(getattr(catalog, "tool_result_follow_up_instruction", "") or "").strip()
    if not prefix:
        prefix = "请根据工具结果直接回复用户。"
    return f"{prefix} {_TOOL_RESULT_ANTI_JSON_HINT}"


def _mc_busy_public_message() -> str:
    if _is_catty_persona_reply_context(get_current_persona_reply_context()):
        return "喵呜，MC 群友正在玩游戏中，猫猫这会儿不能用本地脑子顶上来——主人稍等一下再戳。"
    return "本地服务正被游戏占用，请稍后再试。"


def _chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _normalize_openai_tool_parameters(params: Any) -> dict[str, Any]:
    """规范化 OpenAI tool parameters — 对齐 VSCode tool 序列化:
    - 必须是 dict
    - type 强制 "object"
    - 必须含 properties dict
    - 剥离 $schema 字段 (供应商对 JSON Schema meta 字段处理不一致, 部分中转直接拒)
    """
    if not isinstance(params, dict):
        return {"type": "object", "properties": {}}
    normalized = {k: v for k, v in params.items() if k != "$schema"}
    normalized["type"] = "object"
    if not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def normalize_openai_tool_schemas(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把任意形态的 tool 定义统一规整成标准 OpenAI function-calling 形态:
        {"type": "function", "function": {"name", "description", "parameters": {...}}}
    """
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # 已是标准 OpenAI 形态
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            out.append({
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", "") or "",
                    "parameters": _normalize_openai_tool_parameters(fn.get("parameters")),
                },
            })
            continue
        # Anthropic 形态 (name + input_schema) → 转 OpenAI
        if "input_schema" in t and "name" in t:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", "") or "",
                    "parameters": _normalize_openai_tool_parameters(t.get("input_schema")),
                },
            })
            continue
        # 简化形态 (name + parameters)
        if "name" in t and "parameters" in t:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", "") or "",
                    "parameters": _normalize_openai_tool_parameters(t.get("parameters")),
                },
            })
    return out


_INVALID_TOOL_CALL_NAME = "__invalid_tool_call__"
_TOOL_HISTORY_MAX_JSON_CHARS = 12_000
_TOOL_HISTORY_MAX_ARGUMENT_JSON_CHARS = 4_000
_TOOL_HISTORY_MAX_STRING_CHARS = 2_000
_TOOL_HISTORY_MAX_LIST_ITEMS = 24
_TOOL_HISTORY_MAX_MAPPING_ITEMS = 40
_TOOL_HISTORY_MAX_DEPTH = 8
_TOOL_HISTORY_BLOB_MIN_CHARS = 1_024
_TOOL_HISTORY_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
    "cookie",
    "cookies",
}


def _tool_payload_short_hash(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def _allowed_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for schema in normalize_openai_tool_schemas(tools):
        function = schema.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def _is_valid_tool_function_name(name: str) -> bool:
    return bool(name) and len(name) <= 64 and all(
        char.isascii() and (char.isalnum() or char in {"_", "-"})
        for char in name
    )


def _coerce_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _normalize_assistant_tool_calls(
    raw_tool_calls: Any,
    *,
    round_idx: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    raw_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else [raw_tool_calls]
    normalized_calls: list[dict[str, Any]] = []
    call_specs: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for call_idx, raw_call in enumerate(raw_calls):
        call = raw_call if isinstance(raw_call, dict) else {}
        raw_id = call.get("id")
        call_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not call_id or call_id in seen_ids:
            base_id = f"catty_tool_call_{round_idx}_{call_idx}"
            call_id = base_id
            suffix = 1
            while call_id in seen_ids:
                call_id = f"{base_id}_{suffix}"
                suffix += 1
        seen_ids.add(call_id)

        function = call.get("function") if isinstance(raw_call, dict) else None
        requested_name = ""
        arguments_json = "{}"
        error_reason = ""
        if not isinstance(raw_call, dict):
            error_reason = "tool_call_not_object"
        elif call.get("type") not in (None, "function"):
            error_reason = "unsupported_tool_call_type"
        elif not isinstance(function, dict):
            error_reason = "invalid_function"
        else:
            raw_name = function.get("name")
            requested_name = raw_name.strip() if isinstance(raw_name, str) else ""
            arguments_json = _coerce_tool_arguments(function.get("arguments"))
            if not _is_valid_tool_function_name(requested_name):
                error_reason = "invalid_function_name"

        history_name = requested_name if not error_reason else _INVALID_TOOL_CALL_NAME
        normalized_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": history_name,
                    "arguments": _serialize_tool_arguments_for_history(arguments_json),
                },
            }
        )
        call_specs.append(
            {
                "id": call_id,
                "name": history_name,
                "requested_name": requested_name,
                "arguments_json": arguments_json,
                "error_reason": error_reason,
            }
        )

    return normalized_calls, call_specs


def _tool_argument_log_summary(arguments_json: str) -> tuple[int, str, str]:
    args_len = len(arguments_json)
    args_hash = _tool_payload_short_hash(arguments_json)
    try:
        parsed = json.loads(arguments_json)
    except (TypeError, ValueError):
        return args_len, "<invalid_json>", args_hash
    if not isinstance(parsed, dict):
        return args_len, f"<{type(parsed).__name__}>", args_hash
    fields = sorted(str(field)[:64] for field in parsed)
    if not fields:
        return args_len, "<none>", args_hash
    if len(fields) > 8:
        return args_len, ",".join(fields[:8]) + f",...(+{len(fields) - 8})", args_hash
    return args_len, ",".join(fields), args_hash


def _tool_call_error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **details}}


def _is_sensitive_tool_history_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return (
        normalized in _TOOL_HISTORY_SENSITIVE_KEYS
        or normalized.endswith(("_api_key", "_token", "_secret", "_password", "_cookie"))
    )


def _tool_history_blob_summary(value: str | bytes, *, kind: str) -> str:
    length = len(value)
    return f"<{kind} omitted chars={length} sha256={_tool_payload_short_hash(value)}>"


def _looks_like_oversize_blob(value: str) -> bool:
    compact = "".join(value.split())
    if len(compact) < _TOOL_HISTORY_BLOB_MIN_CHARS:
        return False
    blob_chars = sum(char.isalnum() or char in "+/=_-" for char in compact)
    return blob_chars / len(compact) >= 0.95


def _sanitize_tool_result_for_history(
    value: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    if seen is None:
        seen = set()
    if depth >= _TOOL_HISTORY_MAX_DEPTH:
        return {"truncated": "max_depth", "type": type(value).__name__}
    if isinstance(value, str):
        lowered = value.lstrip().lower()
        if lowered.startswith("base64://"):
            return _tool_history_blob_summary(value, kind="base64")
        if lowered.startswith("data:"):
            return _tool_history_blob_summary(value, kind="data_uri")
        if _looks_like_oversize_blob(value):
            return _tool_history_blob_summary(value, kind="blob")
        if len(value) > _TOOL_HISTORY_MAX_STRING_CHARS:
            return (
                value[:_TOOL_HISTORY_MAX_STRING_CHARS]
                + f"...<truncated chars={len(value)} sha256={_tool_payload_short_hash(value)}>"
            )
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _tool_history_blob_summary(bytes(value), kind="binary")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            return "<circular_reference>"
        seen.add(identity)
        try:
            sanitized: dict[str, Any] = {}
            items = list(value.items())
            for key, item in items[:_TOOL_HISTORY_MAX_MAPPING_ITEMS]:
                key_text = key if isinstance(key, str) else f"<{type(key).__name__}>"
                if key_text.startswith("_short_circuit") or key_text == "_deepseek_plan":
                    continue
                safe_key = key_text[:128]
                if _is_sensitive_tool_history_key(key_text):
                    sanitized[safe_key] = "<redacted>"
                else:
                    sanitized[safe_key] = _sanitize_tool_result_for_history(
                        item,
                        depth=depth + 1,
                        seen=seen,
                    )
            if len(items) > _TOOL_HISTORY_MAX_MAPPING_ITEMS:
                sanitized["_catty_history_truncated_fields"] = len(items) - _TOOL_HISTORY_MAX_MAPPING_ITEMS
            return sanitized
        finally:
            seen.discard(identity)
    if isinstance(value, (list, tuple, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return "<circular_reference>"
        seen.add(identity)
        try:
            items = list(value)
            sanitized = [
                _sanitize_tool_result_for_history(item, depth=depth + 1, seen=seen)
                for item in items[:_TOOL_HISTORY_MAX_LIST_ITEMS]
            ]
            if len(items) > _TOOL_HISTORY_MAX_LIST_ITEMS:
                sanitized.append(
                    {
                        "truncated_items": len(items) - _TOOL_HISTORY_MAX_LIST_ITEMS,
                    }
                )
            return sanitized
        finally:
            seen.discard(identity)
    return f"<non_json_value type={type(value).__name__}>"


def _serialize_tool_result_for_history(payload: Any) -> str:
    sanitized = _sanitize_tool_result_for_history(payload)
    try:
        content = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        content = json.dumps(
            _tool_call_error(
                "tool_result_not_serializable",
                "Tool result could not be serialized for history.",
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if len(content) <= _TOOL_HISTORY_MAX_JSON_CHARS:
        return content
    return json.dumps(
        {
            "truncated": True,
            "reason": "tool_result_exceeded_history_budget",
            "json_chars": len(content),
            "sha256": _tool_payload_short_hash(content),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _serialize_tool_arguments_for_history(arguments_json: str) -> str:
    try:
        parsed = json.loads(arguments_json)
    except (TypeError, ValueError):
        return json.dumps(
            {
                "invalid": True,
                "chars": len(arguments_json),
                "sha256": _tool_payload_short_hash(arguments_json),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    if not isinstance(parsed, dict):
        return json.dumps(
            {
                "invalid": True,
                "chars": len(arguments_json),
                "sha256": _tool_payload_short_hash(arguments_json),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    sanitized = _sanitize_tool_result_for_history(parsed)
    try:
        content = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return json.dumps(
            {
                "invalid": True,
                "chars": len(arguments_json),
                "sha256": _tool_payload_short_hash(arguments_json),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if len(content) <= _TOOL_HISTORY_MAX_ARGUMENT_JSON_CHARS:
        return content
    return json.dumps(
        {
            "truncated": True,
            "reason": "tool_arguments_exceeded_history_budget",
            "json_chars": len(content),
            "sha256": _tool_payload_short_hash(content),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def _execute_tool_calls(
    tool_executor: Any,
    calls: list[dict[str, str]],
) -> list[Any]:
    async def _gather_compatibly() -> list[Any]:
        return await asyncio.gather(
            *[
                tool_executor(call["name"], call["arguments_json"])
                for call in calls
            ],
            return_exceptions=True,
        )

    async def _execute_serially() -> list[Any]:
        results: list[Any] = []
        for call in calls:
            try:
                results.append(
                    await tool_executor(call["name"], call["arguments_json"])
                )
            except Exception as exc:  # noqa: BLE001
                results.append(exc)
        return results

    try:
        from . import tools as tools_module
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "tool scheduling compatibility fallback: .tools import failed (%s); "
            "executing %d calls serially",
            exc.__class__.__name__,
            len(calls),
        )
        return await _execute_serially()
    tool_execution_mode = getattr(tools_module, "tool_execution_mode", None)
    if not callable(tool_execution_mode):
        _logger.warning(
            "tool scheduling hook missing after .tools import; treating %d calls as serial side effects",
            len(calls),
        )

    results: list[Any] = [None] * len(calls)
    pending_reads: list[int] = []

    async def _flush_reads() -> None:
        if not pending_reads:
            return
        indexes = tuple(pending_reads)
        pending_reads.clear()
        read_results = await asyncio.gather(
            *[
                tool_executor(calls[index]["name"], calls[index]["arguments_json"])
                for index in indexes
            ],
            return_exceptions=True,
        )
        for index, result in zip(indexes, read_results):
            results[index] = result

    for index, call in enumerate(calls):
        mode: Any = None
        if callable(tool_execution_mode):
            try:
                mode = tool_execution_mode(call["name"])
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "tool scheduling hook failed for %s (%s); treating call as a serial side effect",
                    call["name"],
                    exc.__class__.__name__,
                )
        if isinstance(mode, str) and mode.strip().lower() == "read":
            pending_reads.append(index)
            continue
        await _flush_reads()
        try:
            results[index] = await tool_executor(
                call["name"],
                call["arguments_json"],
            )
        except Exception as exc:  # noqa: BLE001
            results[index] = exc
    await _flush_reads()
    return results


def _ollama_chat_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1", "/api/chat", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/api/chat"


def _looks_like_ollama_route(base_url: str, api_key: str, extra_body: dict[str, Any]) -> bool:
    native_flag = extra_body.get("native_ollama")
    if isinstance(native_flag, bool):
        return native_flag
    parsed = urlparse(base_url)
    return parsed.port == 11434 or api_key.strip().lower() == "ollama"


def _looks_like_deepseek_thinking_route(base_url: str, model: str) -> bool:
    """DeepSeek v4 supports explicit thinking controls on OpenAI-compatible calls."""
    model_l = (model or "").strip().lower()
    if model_l.startswith("deepseek-v4"):
        return True
    parsed = urlparse((base_url or "").strip())
    host = (parsed.hostname or "").lower()
    return host.endswith("deepseek.com") and model_l.startswith("deepseek-v4")


def _with_deepseek_thinking_defaults(
    base_url: str,
    model: str,
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    """Enable DeepSeek v4 thinking mode at max effort unless config overrides it."""
    body = dict(extra_body or {})
    if not _looks_like_deepseek_thinking_route(base_url, model):
        return body
    if "thinking" not in body:
        body["thinking"] = {"type": "enabled"}
    if "reasoning_effort" not in body:
        body["reasoning_effort"] = "max"
    return body


# 主 AI 多模态输出里的图片(image_url / base64)在文本里用 INLINE_IMAGE 占位符表达,
# 让发送链路看到后转成 MessageSegment.image。占位符常量在 reply_markers 里统一定义;
# history 写入前要 strip 掉(否则 base64 会污染 prompt token)。


def _coerce_multimodal_part(item: dict[str, Any]) -> str:
    """把多模态 content list 里的一项渲染成纯文本片段(含 INLINE_IMAGE 占位符)。"""
    text = item.get("text") or item.get("content")
    if isinstance(text, str) and text:
        return text

    item_type = str(item.get("type") or "").lower()
    if item_type in {"image_url", "image", "input_image", "output_image"}:
        url = ""
        image_url = item.get("image_url")
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "")
        elif isinstance(image_url, str):
            url = image_url
        if not url:
            url = str(item.get("url") or "")
        # Gemini 风格: {"type": "image", "data": "<base64>", "mime_type": "image/png"}
        if not url and isinstance(item.get("data"), str):
            mime = str(item.get("mime_type") or "image/png")
            url = f"data:{mime};base64,{item['data']}"
        if url:
            return f"{INLINE_IMAGE_PREFIX}{url}{INLINE_IMAGE_SUFFIX}"
    return ""


def _render_message_images(message: dict[str, Any]) -> str:
    """gpt-5.5 / codex 网关把原生生成的图放在 message.images 字段, 不走 content 也不走 tool_calls。

    每项格式 {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    或 {"type": "image", "image_url": "..."} 等变体。
    拼成 INLINE_IMAGE marker 序列让发送链路自动渲染成 MessageSegment.image。
    """
    if not isinstance(message, dict):
        return ""
    images = message.get("images")
    if not isinstance(images, list) or not images:
        return ""
    parts: list[str] = []
    for item in images:
        if isinstance(item, str):
            if item:
                parts.append(f"{INLINE_IMAGE_PREFIX}{item}{INLINE_IMAGE_SUFFIX}")
            continue
        if not isinstance(item, dict):
            continue
        rendered = _coerce_multimodal_part(item)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def _extract_content(data: dict[str, Any]) -> str:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenAICompatibleError("AI 返回格式不符合 Chat Completions。", repr(data)[:500]) from exc

    message = choice.get("message") or {}
    content = message.get("content")
    finish_reason = choice.get("finish_reason")

    if isinstance(content, str):
        # 兜底:模型偶发把回复包成 content-block 字面量 [{'type':'text','text':...}] 当纯文本吐出,
        # 解回真正的文本再往下走(纯响应解析侧,不影响 prompt/cache)。
        content = unwrap_content_block_repr(content)
        # content 有文本,但有些模型(gpt-5.5)同时把图放在 message.images 字段——
        # 文本+图都拼上,让发送链路一起处理。
        rendered = (content.strip() + "\n" + _render_message_images(message)).strip()
        return rendered
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
            elif isinstance(item, dict):
                rendered = _coerce_multimodal_part(item)
                if rendered:
                    parts.append(rendered)
        merged = "\n".join(part for part in parts if part).strip()
        images_inline = _render_message_images(message)
        if images_inline:
            merged = (merged + "\n" + images_inline).strip() if merged else images_inline
        if merged:
            return merged
        # content 是 list 但里面没有任何可用的文本或图片,继续往下走兜底

    # content 为空/None:gpt-5.5 / codex 网关常把原生生成的图直接放 message.images 字段
    # (不走 tool_calls 也不走 content),老逻辑直接当 error 处理,导致用户看到"AI 没有返回可读文本"
    # 实际上模型已经把图生好了。这里识别这种格式,把图转成 INLINE_IMAGE marker 让发送链路渲染。
    images_only = _render_message_images(message)
    if images_only:
        _logger.info(
            "AI returned native images-only response (no text), rendered as INLINE_IMAGE. "
            "finish_reason=%s image_count=%d",
            finish_reason,
            len(message.get("images") or []) if isinstance(message, dict) else 0,
        )
        return images_only

    # content 为空/None：先看 reasoning_content / reasoning(R1 / QwQ / DeepSeek-Reasoner 风格)
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        _logger.warning(
            "AI message.content empty but reasoning present; using reasoning as reply. "
            "finish_reason=%s content=%r reasoning_preview=%r",
            finish_reason,
            content,
            reasoning.strip()[:120],
        )
        return reasoning.strip()

    # 顶层 text 字段(非 chat / legacy completions)
    raw_text = choice.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()

    # 真的什么都没有 — 把诊断信息打到日志(整段 response 切片 800 字够定位)
    _logger.error(
        "AI returned no readable content. finish_reason=%s content=%r "
        "message_keys=%s tool_calls=%s response_preview=%r",
        finish_reason,
        content,
        list(message.keys()) if isinstance(message, dict) else type(message).__name__,
        bool(message.get("tool_calls")) if isinstance(message, dict) else False,
        repr(data)[:800],
    )
    detail = (
        f"finish_reason={finish_reason} content={content!r} "
        f"message_keys={list(message.keys()) if isinstance(message, dict) else '?'} "
        f"raw={repr(data)[:400]}"
    )
    raise OpenAICompatibleError("AI 没有返回可读文本。", detail)


def _extract_ollama_chat_content(data: dict[str, Any]) -> str:
    message = data.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
    response = data.get("response")
    if isinstance(response, str):
        return response.strip()
    raise OpenAICompatibleError("Ollama 没有返回可读文本。", repr(data)[:500])


def _client_kwargs(timeout: float, proxy: str) -> dict[str, Any]:
    # 分开 connect / read：网络不通 10s 内快速判死,但慢慢吐 token 的云端要给完整 read 窗口。
    # 整个 timeout 当成 read timeout(LLM 慢回复的瓶颈)。pool/write 一并用同样的 read 时长,
    # connect 固定 10s 避开"网络挂了还要等 read 超时"的等待。
    connect_timeout = min(float(timeout), 10.0) if timeout and timeout > 0 else 10.0
    read_timeout = float(timeout) if timeout and timeout > 0 else None
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(read_timeout, connect=connect_timeout),
        "follow_redirects": True,
    }
    if proxy.strip():
        kwargs["proxy"] = proxy.strip()
    return kwargs


def _ollama_options(
    *,
    temperature: float | None,
    max_tokens: int | None,
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    raw_options = extra_body.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    if temperature is not None and "temperature" not in options:
        options["temperature"] = temperature
    if max_tokens is not None and "num_predict" not in options:
        options["num_predict"] = max_tokens
    return options


async def _post_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout: float,
    proxy: str,
    temperature: float | None,
    max_tokens: int | None,
    extra_headers: dict[str, str],
    extra_body: dict[str, Any],
    enable_cache: bool = False,
    cache_depth: int = 2,
    request_route: str = "main",
) -> str:
    # 主人 2026-05-28 Step 1: 非 Claude 端点 (DeepSeek 等 OpenAI compat) 自动开真流式.
    # Claude 中间人路径保持非流式 (cache_control 注入 + Anthropic SDK 流式走 native 路径).
    try:
        from .prompt_cache import is_claude_endpoint
        _stream = not is_claude_endpoint(base_url, model)
    except Exception:  # noqa: BLE001
        _stream = False
    data = await _post_chat_completion_raw(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=timeout,
        proxy=proxy,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_headers=extra_headers,
        extra_body=extra_body,
        tools=None,
        enable_cache=enable_cache,
        cache_depth=cache_depth,
        stream=_stream,
        request_route=request_route,
    )
    return _extract_content(data)


# 主人 2026-05-28: contextvar 让 scope_key 跨 async/await 透传, 不破坏 chat_completion 签名.
# bot handler (handle_chat / handle_group_msg 等) 入口 set, 内部 _post_anthropic_native_chat
# 读取生成 metadata.user_id 让 Anthropic 路由到同一 cache backend.
_current_scope_key_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "catty_current_scope_key", default=None,
)

_current_logical_turn_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "catty_current_logical_turn",
    default=None,
)
_current_session_context_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "catty_current_session_context",
    default=None,
)

_SESSION_TOKEN_RATIO_SAMPLES: dict[str, deque[float]] = {}


def set_current_scope_key(scope_key: str | None) -> contextvars.Token:
    """bot handler 入口调用, 设置当前对话 scope (private:<uid> / group:<gid>).

    返回的 token 可用于 reset (一般不需要, async task 自动隔离 contextvar).
    """
    return _current_scope_key_var.set(scope_key)


def get_current_scope_key() -> str | None:
    """读当前 async context 的 scope_key. None 时不发 metadata (兼容老代码)."""
    return _current_scope_key_var.get()


def reset_current_scope_key(token: contextvars.Token) -> None:
    """恢复 `set_current_scope_key()` 之前的 scope，供旁路/sim 调用显式收口。"""
    _current_scope_key_var.reset(token)


def set_current_logical_turn(logical_turn_id: str | None = None) -> contextvars.Token:
    """Bind one local conversation turn without adding provider payload fields."""
    scope_key = get_current_scope_key() or ""
    turn_id = str(logical_turn_id or "").strip()
    if not turn_id:
        seed = f"{scope_key or 'noscope'}:{time.time_ns()}"
        turn_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return _current_logical_turn_var.set({
        "logical_turn_id": turn_id,
        "request_seq": 0,
        "scope_key": scope_key,
    })


def reset_current_logical_turn(token: contextvars.Token) -> None:
    _current_logical_turn_var.reset(token)


def get_current_logical_turn() -> dict[str, Any] | None:
    state = _current_logical_turn_var.get()
    return dict(state) if isinstance(state, dict) else None


def set_current_session_context(context: dict[str, Any] | None) -> contextvars.Token:
    return _current_session_context_var.set(dict(context) if isinstance(context, dict) else None)


def reset_current_session_context(token: contextvars.Token) -> None:
    _current_session_context_var.reset(token)


def get_current_session_context() -> dict[str, Any] | None:
    context = _current_session_context_var.get()
    return dict(context) if isinstance(context, dict) else None


def _next_request_identity(
    messages: list[ChatMessage],
    tools: list[dict[str, Any]] | None,
    *,
    request_route: str = "main",
    request_class: str = "chat",
) -> dict[str, Any]:
    state = _current_logical_turn_var.get()
    scope_key = get_current_scope_key() or ""
    if not isinstance(state, dict) or str(state.get("scope_key") or "") != scope_key:
        token = set_current_logical_turn()
        del token
        state = _current_logical_turn_var.get() or {}
    next_state = dict(state)
    request_seq = int(next_state.get("request_seq") or 0) + 1
    next_state["request_seq"] = request_seq
    has_tool_result = any(
        isinstance(message, dict) and message.get("role") == "tool"
        for message in messages
    )
    if request_class == "auxiliary":
        class_seq = int(next_state.get("auxiliary_request_seq") or 0) + 1
        next_state["auxiliary_request_seq"] = class_seq
        request_kind = f"auxiliary:{request_route or 'other'}"
    else:
        class_seq = int(next_state.get("chat_request_seq") or 0) + 1
        next_state["chat_request_seq"] = class_seq
        if tools:
            request_kind = "tool_followup" if has_tool_result else "tool_initial"
        else:
            request_kind = "chat" if class_seq == 1 else "chat_followup"
    _current_logical_turn_var.set(next_state)
    return {
        "logical_turn_id": str(next_state.get("logical_turn_id") or ""),
        "request_seq": request_seq,
        "request_class_seq": class_seq,
        "request_kind": request_kind,
        "request_route": request_route,
        "request_class": request_class,
    }


def _record_session_token_ratio_sample(model: str, actual_tokens: int, local_tokens: int) -> None:
    if actual_tokens <= 0 or local_tokens <= 0:
        return
    ratio = actual_tokens / local_tokens
    if ratio <= 0 or ratio > 8:
        return
    samples = _SESSION_TOKEN_RATIO_SAMPLES.setdefault(model, deque(maxlen=64))
    samples.append(ratio)


def get_session_token_estimator_multiplier(model: str | None = None) -> float:
    """Return max(1.25, recent per-model actual/local p95)."""
    model_key = str(model or "").strip()
    samples = list(_SESSION_TOKEN_RATIO_SAMPLES.get(model_key, ()))
    if len(samples) < 4:
        return 1.25
    samples.sort()
    index = min(max(int(len(samples) * 0.95 + 0.999999) - 1, 0), len(samples) - 1)
    return max(1.25, float(samples[index]))


# Request-local cache diagnostics are populated only after the final wire payload
# exists, then reset after the HTTP request path completes.
_current_cache_request_diagnostics_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "catty_current_cache_request_diagnostics",
    default=None,
)


def _get_current_cache_request_diagnostics() -> dict[str, Any] | None:
    diagnostics = _current_cache_request_diagnostics_var.get()
    return dict(diagnostics) if isinstance(diagnostics, dict) else None


# 主人 2026-08-02: persona 级主模型覆写保留, 机机已统一走 deepseek-v4-flash。
# 同 scope_key 模式: handle_chat 入口按 persona set, 主回复路径 (OpenAI-compat) 读取.
# 只覆盖 catty_openai_model 的主回复调用点; native /v1/messages、filter/vision/audit/
# fallback 等独立 model 配置不受影响. 换模型只换 model 字段, prompt 字节不变;
# DeepSeek server cache 按模型隔离, 机机 scope 首轮 miss 一次属预期.
_current_model_override_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "catty_current_model_override", default=None,
)


def set_current_model_override(model: str | None) -> contextvars.Token:
    """bot handler 入口按 persona.model_override 调用; None/空串 = 用 config 主模型."""
    return _current_model_override_var.set((model or "").strip() or None)


def get_current_model_override() -> str | None:
    return _current_model_override_var.get()


def reset_current_model_override(token: contextvars.Token) -> None:
    """恢复 `set_current_model_override()` 之前的模型覆盖。"""
    _current_model_override_var.reset(token)


def _effective_main_model(config: Config) -> str:
    return get_current_model_override() or config.catty_openai_model


# 主人 2026-07-06 openai-claude-95 §4.1: persona override 防呆告警去重 (per line|endpoint|model)
_NATIVE_MISMATCH_WARNED: set[str] = set()


def _route_native(config: Config, line: str, base_url: str, model: str) -> bool:
    """native /v1/messages 路由单点决策 (主人 2026-07-06 openai-claude-95 §4.1).

    catty_anthropic_native_enabled 语义升级: 「主线专属开关」→「native 路由总闸
    (kill switch)」。False = 全网 OpenAI-compat 与旧行为逐字节一致 (回滚只改一个布尔);
    True = 各线路按 catty_native_route_overrides[line] (native/compat 显式) 或
    detect_provider(base_url, model)=='claude' (auto) 判别。全 deepseek 配置下判别
    恒 False, 行为仍与总闸关时一致。

    纯函数无 IO, 从**传入的 config 实例**读 → sim A/B 的 config 副本天然生效。
    """
    try:
        if not bool(getattr(config, "catty_anthropic_native_enabled", False)):
            return False
        ov = ""
        try:
            overrides = getattr(config, "catty_native_route_overrides", {}) or {}
            ov = str(overrides.get(line or "", "") or "").strip().lower()
        except Exception:  # noqa: BLE001
            ov = ""
        if ov == "native":
            return True
        if ov == "compat":
            return False
        from .prompt_cache import detect_provider, is_claude_endpoint

        routed = detect_provider(base_url, model) == "claude"
        # persona model_override 防呆: 模型名像 claude 但端点是已知非 anthropic 域名 —
        # detect_provider 的 URL 优先规则已兜住不走错协议, 这里只提示配置嫌疑 (去重防刷屏)。
        if not routed and is_claude_endpoint("", model):
            _key = f"{line}|{(base_url or '')[:40]}|{(model or '')[:20]}"
            if _key not in _NATIVE_MISMATCH_WARNED:
                _NATIVE_MISMATCH_WARNED.add(_key)
                _logger.warning(
                    "native route mismatch: model %r looks like claude but endpoint %r "
                    "is a known non-anthropic provider (persona model_override?), "
                    "staying on OpenAI-compat (line=%s)",
                    (model or "")[:20], (base_url or "")[:40], line,
                )
        return routed
    except Exception:  # noqa: BLE001
        return False


# === S6 (主人 2026-05-29): DeepSeek 回复统一蒸馏 hook ===
# 主人决策: 所有"主 AI / catnify 透传 / NSFW spark / 占位/签到"等【生成自然语言回复】
# 的链路都要蒸馏到 L3 corpus, 但 filter/分类/判断 (输出 bool/JSON) 绝不采.
# 落点选在 openai_client 的【面向用户回复入口】(chat_completion / _with_tools 自返回点 /
# _instant / _codex_instant), 一处覆盖所有调用者, 不管 handle_chat 内部怎么分段都只采一次
# 完整回复. _filter_completion / _post_with_fallback / summary / local_critic 不埋 (分类/总结).
#
# 解耦: openai_client 不 import cpu_engine. __init__.py 在 router ready 后 set 一个 hook,
# 这里只负责"在拿到 reply 时, 若当前轮是真实聊天 (有 distill ctx + scope 是 private/group)
# 就把 (user_text, reply) 交给 hook". hook 内部自己 fire-and-forget 调 distiller.
#
# 签名: hook(user_text: str, assistant_text: str, scope: str, terms: list[str], source: str)
_reply_distill_hook: Any = None

# 当前轮蒸馏上下文 (handle_chat 入口 set): 干净的 user 原文 + 脱敏 terms.
# 后台 summary / sim_chat / 分类判断路径不 set → 留 None → 不蒸 (安全阀).
_current_distill_ctx_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "catty_current_distill_ctx", default=None,
)


def set_reply_distill_hook(fn: Any) -> None:
    """__init__.py 在 cpu_engine router ready 后注册. fn 为 None 时关闭蒸馏."""
    global _reply_distill_hook
    _reply_distill_hook = fn


def set_current_distill_context(
    user_text: str | None,
    terms: list[str] | None,
) -> Callable[[], None]:
    """bot handler 入口 (handle_chat) 调: 记下当前轮干净 user 原文 + 脱敏 terms.

    只有 set 过的 async context 才会触发蒸馏 — 后台总结 / sim_chat / 分类判断路径不 set,
    自然被 _maybe_distill_reply 的安全阀挡掉, 不会把非聊天内容污染进 L3.

    返回一个恢复前序 context 的回调，方便请求生命周期挂到 ExitStack。
    """
    token = _current_distill_ctx_var.set(
        {"user_text": user_text or "", "terms": list(terms or [])}
    )

    def _clear() -> None:
        _current_distill_ctx_var.reset(token)

    return _clear


def clear_current_distill_context() -> None:
    _current_distill_ctx_var.set(None)


# 主人 2026-05-29: 占位话 / 超时垫场话 ("猫猫现在很忙~稍等喵") 等"非真正回答"路径用此
# 临时抑制蒸馏 — 它们不是用户问题的答案, 且和同轮正式回复共享 user_text, 不排除会被
# dedup 挤掉真正的回复. 占位在独立 task 跑 (contextvar 已隔离), 仍 try/finally reset 兜底.
_distill_suppressed_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "catty_distill_suppressed", default=False,
)


def set_distill_suppressed(flag: bool) -> contextvars.Token:
    return _distill_suppressed_var.set(bool(flag))


def reset_distill_suppressed(token: Any) -> None:
    try:
        _distill_suppressed_var.reset(token)
    except Exception:  # noqa: BLE001
        pass


def _maybe_distill_reply(reply: str, *, source: str) -> None:
    """面向用户回复入口拿到 reply 后调. 同步、永不抛 — 失败静默.

    安全阀 (任一不满足直接跳过, 保证只采真实聊天的猫娘回复):
      1. hook 已注册 (router ready)
      2. reply 非空
      3. 当前 async context 有 distill ctx 且 user_text 非空 (= 经过 handle_chat 入口)
      4. scope 是 private:* / group:* (排除 summary: / sim: / 空 scope)
    """
    if _distill_suppressed_var.get():
        return
    hook = _reply_distill_hook
    if hook is None or not reply or not reply.strip():
        return
    ctx = _current_distill_ctx_var.get()
    if not ctx:
        return
    user_text = str(ctx.get("user_text") or "")
    if not user_text.strip():
        return
    scope = get_current_scope_key() or ""
    if not (scope.startswith("private:") or scope.startswith("group:")):
        return
    try:
        hook(user_text, reply, scope, list(ctx.get("terms") or []), source)
    except Exception:  # noqa: BLE001
        pass


def _scope_to_metadata_user_id(scope_key: str | None) -> str | None:
    """scope key (private:123 / group:456) → Anthropic metadata.user_id 格式.

    Anthropic 文档要求 user_id 不能含 PII (name / email / phone). QQ 号是不可逆映射不算
    name, 但稳妥起见加前缀 qq_ 区分类型.
    """
    if not scope_key:
        return None
    sk = scope_key.strip()
    if sk.startswith("private:"):
        return f"qq_private_{sk[len('private:'):]}"
    if sk.startswith("group:"):
        # 处理 group:gid 和 group:gid:user:uid 两种 (后者主人虽用 group_history_scope=group
        # 不会出现, 但兼容 group_history_scope=user 时也工作).
        rest = sk[len("group:"):]
        return f"qq_group_{rest.replace(':', '_')}"
    return f"qq_scope_{sk.replace(':', '_').replace('/', '_')}"


async def _post_anthropic_native_chat(
    config: Config, messages: list[ChatMessage]
) -> str:
    """走 anthropic SDK 的 /v1/messages 路径 — Anthropic 原生协议.

    主人 2026-05-28: NewAPI SG relay pass_through_body_enabled=true 字节级透传 body,
    catty 可以直接发原生请求享受 3 个 beta:
      prompt-caching-2024-07-31, compact-2026-01-12, context-management-2025-06-27
    server-side compaction 在 input>150K 时自动触发并在 response 插 compaction block.

    主人 2026-05-28 C18 (vscode 公式): cache_control 单一 owner —
    全部在 post_messages_native 内部 sweep+split_system 之后由
    _apply_anthropic_cache_breakpoints 统一标. caller 不再插 marker.
    """
    from .anthropic_native_client import post_messages_native

    # 主人 2026-07-06 openai-claude-95 §2.5: prefill 收口 — 外部 adapt 已删,
    # 由 post_messages_native 内部 _apply_prefill_mode 单一 owner 按 prefill_mode 处理.
    # sweep / hoist / cache_control 标位同样都在 post_messages_native 内部统一处理.
    prepared_messages = messages

    # 主人 2026-07-06 openai-claude-95: model 用 _effective_main_model (修 native 下
    # persona model_override 失效的老问题) + per-line TTL/betas/prefill_mode 贯通。
    from .prompt_cache import resolve_cache_ttl

    _native_model = _effective_main_model(config)
    _native_payload: dict[str, Any] = {
        "messages": prepared_messages,
        "max_tokens": config.catty_max_tokens or 4096,
    }
    _native_diagnostics_token, _ = _bind_native_request_diagnostics(
        base_url=config.catty_openai_base_url,
        model=_native_model,
        payload=_native_payload,
        request_route="main",
    )
    prepared_messages = _native_payload["messages"]
    try:
        data = await post_messages_native(
            base_url=config.catty_openai_base_url,
            api_key=config.catty_openai_api_key,
            model=_native_model,
            messages=prepared_messages,
            max_tokens=config.catty_max_tokens or 4096,
            temperature=config.catty_temperature,
            timeout=float(config.catty_request_timeout),
            enable_compaction=bool(getattr(config, "catty_compaction_enabled", False)),
            compaction_trigger_tokens=int(getattr(config, "catty_compaction_trigger_tokens", 150_000)),
            metadata_user_id=_scope_to_metadata_user_id(get_current_scope_key()),
            cache_ttl=resolve_cache_ttl(config, "main"),
            line="main",
            prefill_mode=str(getattr(config, "catty_native_prefill_mode", "hint") or "hint"),
            extra_betas=list(getattr(config, "catty_native_extra_betas", []) or []) or None,
        )
        return _extract_content(data)
    finally:
        _reset_native_request_diagnostics(_native_diagnostics_token)


# OpenAI prompt_cache_key 被端点拒 (400/422) 的能力缓存 — key = f"{base_url}|{model}".
# 复刻 _forced_tool_choice_blocked 模式: 探测到拒绝后本进程内不再对该端点发该参数.
# (主人 2026-07-06 openai-claude-95 §三: 严格中转可能不认 unknown 参数)
_PROMPT_CACHE_KEY_BLOCKED: set[str] = set()


def _get_runtime_config() -> Any:
    """拿全局运行时 Config 实例 — 深层无 config 参数的函数用.

    复刻 _post_chat_completion_raw 群聊 hoist 段的 sys.modules 模式
    (插件包顶层挂着 hot-reload 后的最新 config)。拿不到返回 None。
    """
    try:
        import sys as _sys

        _plugin_mod = _sys.modules.get("catty_qq_ai") or _sys.modules.get("src.catty_qq_ai")
        _cfg = getattr(_plugin_mod, "config", None) if _plugin_mod is not None else None
        if _cfg is None:
            from . import config as _config_module

            _cfg = getattr(_config_module, "config", None)
        return _cfg
    except Exception:  # noqa: BLE001
        return None


_SESSION_HISTORY_MARKER = "_catty_session_history"


def _prepare_session_context_payload(
    payload: dict[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    """Trim marked history only for this final request and remove internal markers."""
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return {}

    messages: list[ChatMessage] = []
    marked_history: list[ChatMessage] = []
    first_marked_index: int | None = None
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            messages.append(raw_message)
            continue
        message = dict(raw_message)
        is_history = bool(message.pop(_SESSION_HISTORY_MARKER, False))
        if is_history:
            if first_marked_index is None:
                first_marked_index = len(messages)
            marked_history.append(message)
        else:
            messages.append(message)

    runtime_config = _get_runtime_config()
    enabled = bool(
        runtime_config is not None
        and getattr(runtime_config, "catty_session_context_enabled", False)
    )
    multiplier = get_session_token_estimator_multiplier(model)
    try:
        from .nlu.prompt_compressor import (
            count_history_tokens,
            count_message_tokens,
            count_tokens,
            trim_history_to_token_budget,
        )
    except Exception:  # noqa: BLE001
        payload["messages"] = raw_messages
        for raw_message in raw_messages:
            if isinstance(raw_message, dict):
                raw_message.pop(_SESSION_HISTORY_MARKER, None)
        return {}

    tools = payload.get("tools")
    tools_text = ""
    if tools:
        try:
            tools_text = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            tools_text = str(tools)

    non_history_local = count_history_tokens(messages) + count_tokens(tools_text)
    target_tokens = int(
        getattr(runtime_config, "catty_session_context_target_tokens", 256_000)
        if runtime_config is not None else 256_000
    )
    trim_to_tokens = int(
        getattr(runtime_config, "catty_session_context_trim_to_tokens", 192_000)
        if runtime_config is not None else 192_000
    )
    headroom_tokens = int(
        getattr(runtime_config, "catty_session_context_headroom_tokens", 32_000)
        if runtime_config is not None else 32_000
    )
    model_context_tokens = int(
        getattr(runtime_config, "catty_model_context_tokens", 1_000_000)
        if runtime_config is not None else 1_000_000
    )
    max_output_tokens = max(int(payload.get("max_tokens") or 0), 0)
    effective_target = min(target_tokens, max(model_context_tokens - max_output_tokens, 0))
    non_history_estimate = int(non_history_local * multiplier + 0.999999)
    allowed_history_estimate = max(effective_target - non_history_estimate, 0)
    allowed_history_local = int(allowed_history_estimate / multiplier) if multiplier > 0 else 0

    retained_history = marked_history
    emergency_trimmed = False
    if enabled and marked_history:
        retained_history = trim_history_to_token_budget(marked_history, allowed_history_local)
        emergency_trimmed = len(retained_history) != len(marked_history)

    if first_marked_index is not None:
        messages[first_marked_index:first_marked_index] = retained_history
    payload["messages"] = messages

    history_local = count_history_tokens(retained_history)
    history_estimate = int(history_local * multiplier + 0.999999)
    retained_input_estimate = non_history_estimate + history_estimate
    current_turn_local = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            current_turn_local = count_history_tokens(messages[index:])
            break

    context = get_current_session_context() or {}
    return {
        **context,
        "session_context_enabled": enabled,
        "token_estimator_multiplier": multiplier,
        "local_input_tokens": non_history_local + history_local,
        "retained_input_tokens": retained_input_estimate,
        "history_tokens": history_estimate,
        "history_messages": len(retained_history),
        "history_turns": sum(
            1 for message in retained_history
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        "non_history_input_tokens": non_history_estimate,
        "unavoidable_current_turn_tokens": int(current_turn_local * multiplier + 0.999999),
        "target_context_tokens": target_tokens,
        "trim_to_tokens": trim_to_tokens,
        "headroom_tokens": headroom_tokens,
        "history_high_watermark_tokens": max(target_tokens - headroom_tokens, 0),
        "model_context_tokens": model_context_tokens,
        "max_output_tokens": max_output_tokens,
        "allowed_history_tokens": allowed_history_estimate,
        "request_emergency_trimmed": emergency_trimmed,
        "request_trimmed_messages": len(marked_history) - len(retained_history),
    }


def _cache_request_dump_enabled() -> bool:
    """Return the single default-off cache diagnostic switch."""
    config = _get_runtime_config()
    return bool(getattr(config, "catty_cache_diag_enabled", False)) if config is not None else False


def _cache_scope_type(scope: str) -> str:
    if scope.startswith("private:"):
        return "private"
    if scope.startswith("group:"):
        return "group"
    if scope.startswith("summary:"):
        return "summary"
    return "unknown"


# 256K session 模式跳过 compressor monotonic trim → 没有 compressor anchor
# observation。用每 scope 上次见到的 trim_epoch + request-local emergency trim
# 推断"本轮前缀相对上一轮是否变化", 让默认长会话路径也能产出显式 anchor。
_SESSION_TRIM_EPOCH_OBSERVED: dict[str, int] = {}


def _session_mode_anchor_observation(
    scope: str,
    session_context: dict[str, Any],
) -> dict[str, Any]:
    """Synthesize an anchor observation for the persistent-session path.

    前缀只在两种情况下相对上一轮变化: 持久层 trim (trim_epoch 递增) 或本轮
    request-local emergency trim。首次观察按 legacy monotonic 语义视为稳定。
    """
    trim_epoch = int(session_context.get("trim_epoch") or 0)
    key = str(scope or session_context.get("conversation_id") or "")
    previous = _SESSION_TRIM_EPOCH_OBSERVED.get(key)
    if key:
        if len(_SESSION_TRIM_EPOCH_OBSERVED) > 512:
            _SESSION_TRIM_EPOCH_OBSERVED.clear()
        _SESSION_TRIM_EPOCH_OBSERVED[key] = trim_epoch
    emergency = bool(session_context.get("request_emergency_trimmed"))
    epoch_changed = previous is not None and previous != trim_epoch
    changed = bool(emergency or epoch_changed)
    reason = (
        "request_emergency_trim" if emergency
        else "trim_epoch_changed" if epoch_changed
        else ""
    )
    return {
        "scope_id": key,
        "anchor_before": previous if previous is not None else trim_epoch,
        "anchor_after": trim_epoch,
        "anchor_changed": changed,
        "reset_reason": reason,
        "source": "session_context",
        "request_emergency_trimmed": emergency,
    }


def _request_class_for_route(route: str, scope: str) -> str:
    normalized = str(route or "main").strip().lower()
    if scope.startswith("summary:") or normalized in {
        "audit",
        "filter",
        "local_critic",
        "summary",
        "summary_fallback",
        "vision",
        "imagegen_plan",
        "imagegen_caption",
        "spark",
    }:
        return "auxiliary"
    return "chat"


def _cache_persona_name() -> str:
    context = get_current_persona_reply_context()
    persona = getattr(context, "persona", None) if context is not None else None
    name = str(getattr(persona, "name", "") or "").strip()
    return name or "catty"


def _canonical_cache_diagnostic_sha256(value: Any) -> str:
    try:
        from .cache_metrics import canonical_sha256

        return canonical_sha256(value)
    except Exception:  # noqa: BLE001
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        except (TypeError, ValueError):
            encoded = repr(value).encode("utf-8", errors="replace")
        return hashlib.sha256(encoded).hexdigest()


def _wire_tool_hash(tools: Any) -> str:
    """Hash normalized OpenAI wire schemas, not the caller's source shape."""
    schemas = normalize_openai_tool_schemas(tools if isinstance(tools, list) else None)
    return _canonical_cache_diagnostic_sha256(schemas)


def _payload_prefix_hashes(messages: Any) -> tuple[str, str, str]:
    if not isinstance(messages, list) or not messages:
        return "", "", ""
    leading_system: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        leading_system.append(message)
    first = messages[0] if isinstance(messages[0], dict) else {"value": messages[0]}
    end = messages[-1] if isinstance(messages[-1], dict) else {}
    end_role = str(end.get("role") or "")
    return (
        _canonical_cache_diagnostic_sha256(leading_system) if leading_system else "",
        _canonical_cache_diagnostic_sha256(first),
        end_role,
    )


def _build_cache_request_diagnostics(
    *,
    base_url: str,
    model: str,
    payload: dict[str, Any],
    request_route: str = "main",
    request_class: str = "chat",
    request_identity: dict[str, Any] | None = None,
    session_context: dict[str, Any] | None = None,
    transport: str = "openai_compat",
) -> dict[str, Any]:
    messages = payload.get("messages")
    final_messages = messages if isinstance(messages, list) else []
    try:
        from .prompt_cache import detect_provider

        provider = detect_provider(base_url, model)
    except Exception:  # noqa: BLE001
        provider = "other"
    scope = get_current_scope_key() or ""
    try:
        from .cache_metrics import build_cohort_metadata, compute_warm_fields

        _msgs, _hist, warm = compute_warm_fields(final_messages)
    except Exception:  # noqa: BLE001
        warm = 0
    try:
        from .nlu.prompt_compressor import get_anchor_observation

        anchor = get_anchor_observation()
    except Exception:  # noqa: BLE001
        anchor = None
    anchor_observed = isinstance(anchor, dict) and "anchor_changed" in anchor
    anchor_changed = bool(anchor.get("anchor_changed")) if anchor_observed else False
    wire_tool_hash = _wire_tool_hash(payload.get("tools"))
    prefix_sys_hash, prefix_first_hash, message_end_role = _payload_prefix_hashes(final_messages)
    prompt_variant = _canonical_cache_diagnostic_sha256({
        "prefix_sys_hash": prefix_sys_hash,
        "prefix_first_hash": prefix_first_hash,
        "message_end_role": message_end_role,
    })
    request_identity = dict(request_identity or {})
    session_context = dict(session_context or {})
    if anchor is None and session_context.get("session_context_enabled"):
        # 默认 256K 路径没有 compressor anchor — 合成显式观察, 否则 Hot99
        # eligibility 的 anchor_observed 门槛在 session 模式下永远不满足。
        anchor = _session_mode_anchor_observation(scope, session_context)
        anchor_observed = True
        anchor_changed = bool(anchor.get("anchor_changed"))
    diagnostics: dict[str, Any] = {
        "provider": provider,
        "route": request_route,
        "request_route": request_route,
        "request_class": request_class,
        "transport": transport,
        "scope_type": _cache_scope_type(scope),
        "persona": _cache_persona_name(),
        "wire_tool_hash": wire_tool_hash,
        "tool_count": len(payload.get("tools") or []),
        "stream": bool(payload.get("stream")),
        "message_end_role": message_end_role,
        "prefix_sys_hash": prefix_sys_hash,
        "prefix_first_hash": prefix_first_hash,
        "prompt_variant": prompt_variant,
        "anchor": anchor,
        "anchor_observed": anchor_observed,
        "conversation_id": str(session_context.get("conversation_id") or scope),
        "trim_epoch": int(session_context.get("trim_epoch") or 0),
        **session_context,
        **request_identity,
    }
    runtime_config = _get_runtime_config()
    if provider == "deepseek" and runtime_config is not None:
        diagnostics["cache_hit_billing_multiplier"] = float(
            getattr(runtime_config, "catty_cache_hit_input_price_ratio", 0.02)
        )
    try:
        cohort_kwargs = dict(
            provider=provider,
            model=model,
            route=diagnostics["route"],
            scope_type=diagnostics["scope_type"],
            persona=diagnostics["persona"],
            warm=warm,
            tool_set_hash=wire_tool_hash,
            anchor_changed=anchor_changed,
            anchor_observed=anchor_observed,
            prompt_variant=prompt_variant,
            trim_epoch=diagnostics["trim_epoch"],
            request_kind=diagnostics.get("request_kind", ""),
            request_class=diagnostics.get("request_class", ""),
        )
        try:
            diagnostics["cohort_metadata"] = build_cohort_metadata(**cohort_kwargs)
        except TypeError:
            for key in (
                "anchor_observed",
                "prompt_variant",
                "trim_epoch",
                "request_kind",
                "request_class",
            ):
                cohort_kwargs.pop(key, None)
            diagnostics["cohort_metadata"] = build_cohort_metadata(**cohort_kwargs)
    except Exception:  # noqa: BLE001
        diagnostics["cohort_metadata"] = {}
    return diagnostics


def _bind_native_request_diagnostics(
    *,
    base_url: str,
    model: str,
    payload: dict[str, Any],
    request_route: str,
) -> tuple[contextvars.Token, dict[str, Any]]:
    session_context = _prepare_session_context_payload(payload, model=model)
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    scope = get_current_scope_key() or ""
    request_class = _request_class_for_route(request_route, scope)
    request_identity = _next_request_identity(
        messages,
        payload.get("tools"),
        request_route=request_route,
        request_class=request_class,
    )
    diagnostics = _build_cache_request_diagnostics(
        base_url=base_url,
        model=model,
        payload=payload,
        request_route=request_route,
        request_class=request_class,
        request_identity=request_identity,
        session_context=session_context,
        transport="anthropic_native",
    )
    return _current_cache_request_diagnostics_var.set(diagnostics), diagnostics


def _reset_native_request_diagnostics(token: contextvars.Token) -> None:
    try:
        _current_cache_request_diagnostics_var.reset(token)
    finally:
        try:
            from .nlu.prompt_compressor import clear_anchor_observation

            clear_anchor_observation()
        except Exception:  # noqa: BLE001
            pass


def _record_cache_cohort_diagnostics(
    request_diagnostics: dict[str, Any] | None,
    *,
    hit_tok: int,
    miss_tok: int,
    create_tok: int,
) -> dict[str, Any] | None:
    if not request_diagnostics:
        return None
    diagnostics = dict(request_diagnostics)
    metadata = diagnostics.get("cohort_metadata")
    if not isinstance(metadata, dict):
        return diagnostics
    try:
        from .cache_metrics import record_cohort_hit

        stats = record_cohort_hit(
            metadata,
            hit_tok,
            miss_tok,
            create_tok,
            unavoidable_current_turn_tokens=diagnostics.get("unavoidable_current_turn_tokens"),
        )
    except Exception:  # noqa: BLE001
        return diagnostics
    diagnostics.update({
        "cohort": stats.cohort_key,
        "actual_hit_rate": stats.this_rate,
        "normalized_kpi": stats.normalized_rate,
        "hot99_eligible": stats.hot99_eligible,
        "hot99_eligible_count": stats.hot99_eligible_count,
        "hot99_rate": stats.hot99_raw_rate,
        "hot99_raw_rate": stats.hot99_raw_rate,
        "hot99_status": stats.hot99_status,
        "hot99_target": stats.hot99_target,
    })
    return diagnostics


def _log_cache_stats(
    data: dict[str, Any],
    model: str,
    *,
    messages: list[ChatMessage] | None = None,
    base_url: str = "",
) -> None:
    """从 chat completion response 提取 cache hit 统计 + log.

    响应侧按 usage 字段嗅探分支 (中转改写字段时监控不瞎, 单次请求只走一条):
    1. DeepSeek 风格: usage.prompt_cache_hit_tokens / usage.prompt_cache_miss_tokens
       (DeepSeek 独有字段, 分开 hit/miss 比 OpenAI cached_tokens 更精确)
    2. Anthropic 风格: usage.cache_read_input_tokens / usage.cache_creation_input_tokens
    3. OpenAI 风格 (兜底): usage.prompt_tokens_details.cached_tokens / usage.prompt_tokens

    主人 2026-07-06 openai-claude-95 §二: 三分支统一打 HIT_TARGET 行 (cache_metrics
    按 provider|model 分桶 rolling, 修掉旧全局单 deque 混模型污染), provider 标签取
    请求侧 detect_provider(base_url, model) — 与字段嗅探不一致时日志天然暴露诊断线索。
    (2)/(3) 分支补齐 dashboard push (之前 openai usage 落 anthropic 分支全 0,
    dashboard 瞎 + token 计费漏记)。
    """
    usage = data.get("usage") or {}
    try:
        from .prompt_cache import detect_provider

        _provider = detect_provider(base_url, model)
    except Exception:  # noqa: BLE001
        _provider = "other"
    try:
        from .cache_metrics import (
            compute_warm_fields,
            format_hit_target_line,
            record_hit,
        )
    except Exception:  # noqa: BLE001
        return
    _msgs, _hist, _warm = compute_warm_fields(messages)
    try:
        _hit_scope = get_current_scope_key() or ""
    except Exception:  # noqa: BLE001
        _hit_scope = ""
    _request_diagnostics = _get_current_cache_request_diagnostics()
    _is_auxiliary = (
        str((_request_diagnostics or {}).get("request_class") or "").strip().lower()
        == "auxiliary"
    )

    # === (1) DeepSeek 风格 ===
    ds_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    ds_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    if ds_hit or ds_miss:
        if _is_auxiliary:
            _cohort_diagnostics = _record_cache_cohort_diagnostics(
                _request_diagnostics,
                hit_tok=ds_hit,
                miss_tok=ds_miss,
                create_tok=0,
            )
            try:
                from . import dashboard_state as _dash

                _dash.push_cache_stats(
                    _hit_scope or model,
                    usage,
                    model=model,
                    diagnostics=_cohort_diagnostics,
                )
            except Exception:  # noqa: BLE001
                pass
            return
        _record_session_token_ratio_sample(
            model,
            ds_hit + ds_miss,
            int((_request_diagnostics or {}).get("local_input_tokens") or 0),
        )
        stats = record_hit(_provider, model, ds_hit, ds_miss, 0)
        _cohort_diagnostics = _record_cache_cohort_diagnostics(
            _request_diagnostics,
            hit_tok=ds_hit,
            miss_tok=ds_miss,
            create_tok=0,
        )
        _logger.info(
            f"cache stats(deepseek) model={model[:20]} hit={ds_hit} "
            f"miss={ds_miss} hit_rate={stats.this_rate:.0%} "
            f"rolling{stats.roll_n}={stats.roll_rate:.0%}",
        )
        # 主人 2026-05-29 Round 1: 显眼 HIT_TARGET 状态 (grep 用), 目标 95-98%
        # 主人 2026-05-31 测量基建: scope 标签让 sim A/B 按 scope 干净过滤
        _logger.info(
            format_hit_target_line(
                provider=_provider,
                model=model,
                stats=stats,
                hit_tok=ds_hit,
                miss_tok=ds_miss,
                create_tok=0,
                msgs=_msgs,
                hist=_hist,
                warm=_warm,
                scope=_hit_scope,
                diagnostics=_cohort_diagnostics,
            ),
        )
        # rolling 命中率连续 3 次 < 90% → warn (主人目标 95-98%)
        if stats.should_warn:
            _logger.warning(
                f"deepseek cache rolling hit_rate dropped to {stats.roll_rate:.0%} "
                f"(target 95-98%), check prefix stability",
            )
        # 主人 2026-05-28: 推 dashboard (DeepSeek 路径 cache 实时显示)
        try:
            from . import dashboard_state as _dash

            _dash.push_cache_stats(
                _hit_scope or model,
                usage,
                model=model,
                diagnostics=_cohort_diagnostics,
            )
        except Exception:  # noqa: BLE001
            pass
        return

    # === (2) Anthropic 风格 ===
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    # === (3) OpenAI 风格 (兜底) ===
    if not (cache_read or cache_create):
        prompt_details = usage.get("prompt_tokens_details") or {}
        cache_read = int(prompt_details.get("cached_tokens") or 0)
        input_tokens = int(usage.get("prompt_tokens") or 0) - cache_read
    total_input = cache_read + cache_create + input_tokens
    if total_input <= 0:
        return
    if _is_auxiliary:
        _cohort_diagnostics = _record_cache_cohort_diagnostics(
            _request_diagnostics,
            hit_tok=cache_read,
            miss_tok=input_tokens,
            create_tok=cache_create,
        )
        try:
            from . import dashboard_state as _dash

            _dash.push_cache_stats(
                _hit_scope or model,
                usage,
                model=model,
                diagnostics=_cohort_diagnostics,
            )
        except Exception:  # noqa: BLE001
            pass
        return
    _record_session_token_ratio_sample(
        model,
        total_input,
        int((_request_diagnostics or {}).get("local_input_tokens") or 0),
    )
    stats = record_hit(_provider, model, cache_read, input_tokens, cache_create)
    _cohort_diagnostics = _record_cache_cohort_diagnostics(
        _request_diagnostics,
        hit_tok=cache_read,
        miss_tok=input_tokens,
        create_tok=cache_create,
    )
    _logger.info(
        f"cache stats model={model[:20]} read={cache_read} create={cache_create} "
        f"new={input_tokens} hit={stats.this_rate:.0%}"
    )
    _logger.info(
        format_hit_target_line(
            provider=_provider,
            model=model,
            stats=stats,
            hit_tok=cache_read,
            miss_tok=input_tokens,
            create_tok=cache_create,
            msgs=_msgs,
            hist=_hist,
            warm=_warm,
            scope=_hit_scope,
            diagnostics=_cohort_diagnostics,
        ),
    )
    if stats.should_warn:
        _logger.warning(
            f"cache rolling hit_rate ({_provider}) dropped to {stats.roll_rate:.0%} "
            f"(target 95-98%), check prefix/breakpoints/TTL",
        )
    # 主人 2026-07-06: openai / 中转 claude 走 compat 时之前不推 dashboard —
    # cache 卡片瞎 + token_billing 漏记 (openai usage 落 anthropic 分支全 0), 补推。
    try:
        from . import dashboard_state as _dash

        _dash.push_cache_stats(
            _hit_scope or model,
            usage,
            model=model,
            diagnostics=_cohort_diagnostics,
        )
    except Exception:  # noqa: BLE001
        pass


async def _stream_chat_completion_attempt(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    proxy: str,
    dash_stream_id: str | None,
    dash_mod: Any,
) -> dict[str, Any]:
    """单次流式请求 attempt — 拼接 SSE chunks 成跟非流式相同的 response dict.

    主人 2026-05-28 plan-quizzical-crane Step 1: tool-based summary retrieval 链路
    依赖流式 (AI 边输出 tool_call delta 边推 dashboard). DeepSeek OpenAI compat 协议:
    `payload["stream"] = True` + `payload["stream_options"] = {"include_usage": True}`
    让最后一个 chunk 含 usage.

    成功 → 返回 {"choices": [{"message": {...}, "finish_reason": ...}], "usage": {...}}
    HTTP 错误 → raise OpenAICompatibleError (exc.status_code 标 5xx, caller 决定重试).
    """
    text_accum = ""
    reasoning_accum = ""
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    role = "assistant"

    async with httpx.AsyncClient(**_client_kwargs(timeout, proxy)) as client:
        async with client.stream(
            "POST", url, headers=headers, json=payload,
        ) as response:
            if response.status_code >= 400:
                try:
                    error_text = (await response.aread()).decode("utf-8", "ignore")[:500]
                except Exception:  # noqa: BLE001
                    error_text = f"<read failed status={response.status_code}>"
                err = OpenAICompatibleError(
                    _catty_http_status_message("AI 接口", response.status_code),
                    error_text,
                )
                err.status_code = response.status_code  # type: ignore[attr-defined]
                raise err

            async for line in response.aiter_lines():
                if not line:
                    continue
                # SSE 标准行: "data: {...}\n\n", 也兼容无 space "data:{...}"
                if line.startswith("data:"):
                    data_str = line[5:].lstrip()
                elif line.startswith(":"):
                    # SSE 注释 (keep-alive heartbeat), 跳过
                    continue
                else:
                    continue
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except (ValueError, json.JSONDecodeError):
                    continue

                # usage chunk (最后一个; DeepSeek/OpenAI 在 stream_options.include_usage 时返回)
                if chunk.get("usage"):
                    usage = chunk["usage"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}

                # role (一般出现在第一个 chunk)
                if delta.get("role"):
                    role = delta["role"]

                # 累积 text content
                content_delta = delta.get("content")
                if content_delta:
                    text_accum += content_delta
                    if dash_stream_id is not None and dash_mod is not None:
                        try:
                            dash_mod.push_event(dash_stream_id, {
                                "delta_text": content_delta,
                                "event_type": "deepseek_chunk",
                            })
                        except Exception:  # noqa: BLE001
                            pass

                reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning_delta:
                    reasoning_accum += reasoning_delta

                # 累积 tool_calls (按 index 拼名字 + 参数)
                tc_deltas = delta.get("tool_calls") or []
                for tc_delta in tc_deltas:
                    if not isinstance(tc_delta, dict):
                        continue
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_by_index:
                        tool_calls_by_index[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    tc = tool_calls_by_index[idx]
                    if tc_delta.get("id"):
                        tc["id"] = tc_delta["id"]
                    if tc_delta.get("type"):
                        tc["type"] = tc_delta["type"]
                    fn_delta = tc_delta.get("function") or {}
                    if fn_delta.get("name"):
                        tc["function"]["name"] += fn_delta["name"]
                    if fn_delta.get("arguments"):
                        tc["function"]["arguments"] += fn_delta["arguments"]

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

    # 拼成跟非流式相同的 dict
    message: dict[str, Any] = {"role": role, "content": text_accum}
    if reasoning_accum:
        message["reasoning_content"] = reasoning_accum
    if tool_calls_by_index:
        message["tool_calls"] = [
            tool_calls_by_index[i] for i in sorted(tool_calls_by_index.keys())
        ]
    return {
        "choices": [
            {"message": message, "finish_reason": finish_reason or "stop", "index": 0},
        ],
        "usage": usage or {},
    }


async def _post_chat_completion_raw(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout: float,
    proxy: str,
    temperature: float | None,
    max_tokens: int | None,
    extra_headers: dict[str, str],
    extra_body: dict[str, Any],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: ToolChoice = "auto",
    enable_cache: bool = False,
    cache_depth: int = 2,
    stream: bool = False,
    request_route: str = "main",
) -> dict[str, Any]:
    """返回完整 response JSON,供 function calling 链路读 tool_calls。

    enable_cache: ST PR #3085 风 Anthropic Prompt Caching 注入 — 给 messages 末尾倒数
        depth/depth+2 处 role 切换 + system 末尾打 cache_control: ephemeral breakpoint,
        header 加 anthropic-beta: prompt-caching-2024-07-31. 仅 Claude/Anthropic 协议生效;
        OpenAI native 是 implicit caching 不需要此参数.
    cache_depth: 倒数第 N 处 role 切换打 breakpoint, ST 推荐 2.
    """
    if not base_url.strip():
        raise OpenAICompatibleError("AI 接口地址为空。")
    if not model.strip():
        raise OpenAICompatibleError("AI 模型名为空。")

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        **extra_headers,
    }
    # === ST PR #3085 移植: cache_control 注入 (Anthropic Prompt Caching) ===
    # 主人原话『prompt 也更聪明一点不要一直变不能 hit cache』.
    # 调用方传 enable_cache=True 时, 给 messages 注入最多 3 个 message-level breakpoint
    # (depth 和 depth+2 处 role 切换 + system 末尾) + 加 anthropic-beta header.
    # OpenAI native 收到 cache_control 字段会忽略 (unknown field); Claude / 中间人走
    # Claude 协议时才真正命中 cache.
    if enable_cache:
        from .prompt_cache import (
            cachingAtDepthForClaude,
            inject_system_tail_cache,
            is_claude_endpoint,
        )
        try:
            # 深拷贝避免修改调用方 messages (会被多次注入污染)
            import copy
            messages = copy.deepcopy(messages)
            # 主人 2026-05-28 Phase 1.2: 标位前先剥所有现存 cache_control (defensive single-owner).
            # history messages 持久化回来可能含上一轮 cache_control, 叠 2 份会让 relay 第二轮 500.
            _stripped = 0
            for _m in messages:
                if not isinstance(_m, dict):
                    continue
                _c = _m.get("content")
                if isinstance(_c, list):
                    for _blk in _c:
                        if isinstance(_blk, dict) and _blk.pop("cache_control", None) is not None:
                            _stripped += 1
            if _stripped > 0:
                _logger.debug(
                    "openai-compat cache single-owner: stripped %d residual marker(s)", _stripped,
                )
            cachingAtDepthForClaude(messages, cachingAtDepth=cache_depth)
            inject_system_tail_cache(messages)
            # 仅 Claude endpoint 加 anthropic-beta header (避免 OpenAI 报 unknown header)
            if is_claude_endpoint(base_url, model):
                headers["anthropic-beta"] = "prompt-caching-2024-07-31"
                # 主人 2026-05-28: 不给 tools[-1] 加 cache_control (复刻 CC). CC
                # toolToAPISchema (claude.ts) 完全不带 cacheControl 参数, 仅在
                # MCP needsToolBasedCacheMarker=true 场景才 fallback 到 tool-based
                # cache strategy. catty 无 MCP, 加 tools cache 反而让 tools 序列化
                # 字节差异破 cache 命中 (77% breaks per CC promptCacheBreakDetection).
        except Exception as exc:  # noqa: BLE001
            _logger.warning(f"prompt cache 注入失败 (降级到无 cache): {exc}")

    # === DeepSeek KV cache 前缀稳定优化 (OpenAI compat path 专属) ===
    # 主人 2026-05-28: DeepSeek 硬盘缓存是服务端自动 token 级精确前缀匹配,
    # 不需要 cache_control 字段, 但前缀必须字节稳定. 仅在非 Claude 端点跑.
    # 参考: dist/deepseek硬盘缓存规则.txt + QwenLM/qwen-code#4065.
    try:
        from .prompt_cache import (
            collapse_trailing_systems_into_last_user,
            compute_prefix_hash,
            hoist_stable_group_common_trailing,
            hoist_stable_group_owner_trailing,
            hoist_stable_private_trailing,
            inline_assistant_prefill_without_reordering,
            is_claude_endpoint,
            merge_consecutive_system_messages,
            stabilize_tools_order,
            strip_all_cache_control,
            strip_inline_dynamic_segments_from_history,
        )
        if not is_claude_endpoint(base_url, model):
            import copy as _copy
            messages = _copy.deepcopy(messages)
            # (a) strip 残留 cache_control (防 DeepSeek 未来严格校验未知字段)
            stripped = strip_all_cache_control(messages, tools)
            if stripped > 0:
                _logger.debug(
                    "deepseek prefix opt: stripped %d cache_control", stripped,
                )
            # (a2) 剥离历史 user 里的 inline 动态段 (主人 22:08 case: 历史 first_user_md5
            # 每次都变 → 6656 hit / 4827 miss / 58%. 这步让 history user 字节稳定.
            # current turn 最后一条 user 保留完整版给 LLM 读.)
            strip_hist = strip_inline_dynamic_segments_from_history(messages)
            # 主人 22:16: 命中率没起来, 加 info 级诊断看 strip 实际剥了多少 + first user 实际样子
            _diag_first_user_preview = ""
            _diag_first_user_len = 0
            for _m in messages[:5]:
                if isinstance(_m, dict) and _m.get("role") == "user":
                    _c = _m.get("content", "")
                    if isinstance(_c, list):
                        for _b in _c:
                            if isinstance(_b, dict) and _b.get("type") == "text":
                                _c = _b.get("text", "")
                                break
                    if isinstance(_c, str):
                        _diag_first_user_len = len(_c)
                        _diag_first_user_preview = _c[:200].replace("\n", "\\n")
                    break
            _logger.info(
                "deepseek prefix opt: strip_inline_history=%d first_user_len=%d "
                "first_user_head=%r",
                strip_hist, _diag_first_user_len, _diag_first_user_preview,
            )
            # (a3) 主人 2026-05-31 Stage3: 私聊把 stable trailing system 段(发言者/称呼/今日
            # 小心思/Lv/cb_diet, 5轮字节验证跨轮恒定)hoist 到 history 之前独立 sentinel block,
            # 离开 current-user 每轮 miss 区进 cache. 仅私聊(单 user 才跨轮稳定; 群聊每轮换人
            # 会破 block 后 history 前缀). volatile 段(时刻/续聊)留给下面 collapse.
            try:
                _scope_early = get_current_scope_key() or ""
            except Exception:  # noqa: BLE001
                _scope_early = ""
            if _scope_early.startswith("private:"):
                _hoisted = hoist_stable_private_trailing(messages)
                if _hoisted:
                    _logger.info(
                        "deepseek prefix opt: hoisted %d stable private trailing → history前 "
                        "(独立 sentinel block, 跨轮稳定进 cache)", _hoisted,
                    )
            elif _scope_early.startswith("group:"):
                _owner_qq = ""
                try:
                    import sys as _sys
                    _plugin_mod = _sys.modules.get("catty_qq_ai") or _sys.modules.get("src.catty_qq_ai")
                    _runtime_config = getattr(_plugin_mod, "config", None) if _plugin_mod is not None else None
                    if _runtime_config is None:
                        from . import config as _config_module
                        _runtime_config = getattr(_config_module, "config", None)
                    _owner_qq = str(getattr(_runtime_config, "catty_owner_qq", "") or "").strip()
                    if not _owner_qq or _owner_qq == "0":
                        import os as _os
                        _owner_qq = str(_os.environ.get("CATTY_OWNER_QQ", "") or "").strip()
                except Exception:  # noqa: BLE001
                    _owner_qq = ""
                _current_user_text = ""
                for _m in reversed(messages):
                    if isinstance(_m, dict) and _m.get("role") == "user":
                        _c = _m.get("content", "")
                        if isinstance(_c, list):
                            _current_user_text = "\n".join(
                                str(_b.get("text", "") or "")
                                for _b in _c if isinstance(_b, dict)
                            )
                        else:
                            _current_user_text = str(_c or "")
                        break
                if _owner_qq and _current_user_text.startswith(f"[QQ:{_owner_qq}]"):
                    _hoisted = hoist_stable_group_owner_trailing(messages)
                    if _hoisted:
                        _logger.info(
                            "deepseek prefix opt: hoisted %d stable group-owner trailing → history前 "
                            "(独立 sentinel block, owner-in-group cache)", _hoisted,
                        )
                else:
                    _hoisted = hoist_stable_group_common_trailing(messages)
                    if _hoisted:
                        _logger.info(
                            "deepseek prefix opt: hoisted %d stable group-common trailing → history前 "
                            "(独立 sentinel block, group common cache)", _hoisted,
                        )
            # (b) 合并开头连续 system → 单条 (前缀更紧凑)
            merged = merge_consecutive_system_messages(messages)
            if merged > 0:
                _logger.debug(
                    "deepseek prefix opt: merged %d system blocks", merged,
                )
            # (b2-spark) 主人 2026-05-31 cache: DeepSeek spark 也不能长期保持
            # [user, system..., assistant(prefill)] 结尾。先把 assistant prefill 改写到最近 user,
            # 再让下面 collapse 把尾部 system 并入 user, 使 spark 也以 user 边界结尾。
            # 旧版只给 Claude 中转做此适配, 导致 DeepSeek spark history 复用卡在 50-80%。
            if (
                messages
                and isinstance(messages[-1], dict)
                and messages[-1].get("role") == "assistant"
            ):
                before_len = len(messages)
                messages = inline_assistant_prefill_without_reordering(messages)
                if len(messages) != before_len:
                    _logger.info(
                        "deepseek prefix opt: adapted assistant prefill into last user "
                        "without reordering (spark strict user-end cache boundary)",
                    )
            # (b2) 主人 2026-05-30 决定性修复: 末尾连续 system → inline 进 current user,
            # 让 messages 结尾 = user. 真实 dump 重放实测: 末尾=system 时 history 死锁不进
            # cache (hit 7808), 改成末尾=user 后 history 进 cache (hit 7808→12224, +30pp).
            # 根因 = DeepSeek 只在 user-end/output-end 落盘 turn 边界单元, 末尾 system 两头不沾.
            # spark 已在上面把 assistant prefill 改写为 user hint, 因此也能触发这里的折叠。
            collapsed = collapse_trailing_systems_into_last_user(messages)
            if collapsed > 0:
                _logger.info(
                    "deepseek prefix opt: collapsed %d trailing system(s) into last user "
                    "(末尾→user → history 可进 cache)", collapsed,
                )
            # (c) tools 字典序排锁死 (QwenLM 翻车: 顺序变→97.5%→81.5%)
            if tools:
                tools = _copy.deepcopy(tools)
                reordered = stabilize_tools_order(tools)
                if reordered:
                    _logger.debug(
                        "deepseek prefix opt: tools reordered to lexicographic",
                    )
            # (d) 前缀 hash 诊断 (对比上一轮看是否漂移)
            h = compute_prefix_hash(messages, tools, n=5)
            try:
                _ph_scope = get_current_scope_key() or ""
            except Exception:  # noqa: BLE001
                _ph_scope = ""
            _logger.info(
                f"prefix_hash model={model[:20]} scope={_ph_scope} sys_count={h['sys_count']} "
                f"msg_count={h['msg_count']} sys_md5={h['sys_md5']} "
                f"first_user_md5={h['first_user_md5']} "
                f"tools_count={h['tools_count']} tools_md5={h['tools_md5']}",
            )
            # 主人 2026-05-29 Round 1: per-msg hash 定位 history 中段漂移
            _logger.info(f"per_msg_hash: {h.get('per_msg', '')}")
    except Exception as exc:  # noqa: BLE001
        _logger.warning(f"deepseek prefix opt 失败 (降级到原 messages): {exc}")

    # 主人 2026-05-28: claude/sonnet 经中转拒末尾 assistant prefill → 把 prefill 内容
    # 追加到最近的 user message 作为强语气 hint (NSFW spark messages 末尾结构是
    # [..., user, system, system, assistant(prefill)], user 不在倒数第二) + drop 末尾
    # assistant + 把 user 移到末尾, 保留 IC 起手机制. 真 Anthropic native API 走
    # /v1/messages (不是这里的 chat/completions), 所以这里所有 claude 都是中转.
    try:
        from .prompt_cache import adapt_assistant_prefill_for_strict_user_end, is_claude_endpoint
        if (
            messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "assistant"
            and is_claude_endpoint(base_url, model)
        ):
            messages = adapt_assistant_prefill_for_strict_user_end(messages)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(f"claude assistant prefill 适配失败 (降级到原 messages): {exc}")

    extra_body = _with_deepseek_thinking_defaults(base_url, model, extra_body)

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    payload.update(extra_body)
    if tools:
        payload["tools"] = normalize_openai_tool_schemas(tools)
        payload["tool_choice"] = tool_choice

    # === Streaming params (主人 2026-05-28 Step 1: 真流式) ===
    if stream:
        payload["stream"] = True
        # include_usage: 让最后一个 chunk 返回 usage (DeepSeek / OpenAI 都支持)
        payload["stream_options"] = {"include_usage": True}

    # === OpenAI 隐式缓存路由亲和 (主人 2026-07-06 openai-claude-95 §三) ===
    # prompt_cache_key = "catty:{scope}" 让同 scope 请求落同一缓存分片 (官方建议按会话粒度).
    # 红线: 只对 detect_provider=='openai' 的端点注入 — DeepSeek 绝不发 user/prompt_cache_key
    # (下方 Round 10 注释是铁证: 传 user 会把 DeepSeek 公共前缀 cache 分裂成独立 namespace).
    # 双 gate: provider 判别 + config 开关 (默认关); 端点拒收 400/422 时由 retry 层剥参拉黑.
    try:
        from .prompt_cache import detect_provider as _detect_provider

        if _detect_provider(base_url, model) == "openai":
            _rc = _get_runtime_config()
            if _rc is not None and bool(
                getattr(_rc, "catty_openai_prompt_cache_key_enabled", False),
            ):
                _pck_scope = ""
                try:
                    _pck_scope = get_current_scope_key() or ""
                except Exception:  # noqa: BLE001
                    _pck_scope = ""
                if _pck_scope and f"{base_url}|{model}" not in _PROMPT_CACHE_KEY_BLOCKED:
                    payload["prompt_cache_key"] = f"catty:{_pck_scope}"
    except Exception:  # noqa: BLE001
        pass

    # 主人 2026-05-29 Round 10 回滚: 完全不传 user 字段, 让 DeepSeek 后端公共前缀检测
    # 跨所有 catty 请求自动落盘共享 cache 池. 之前 Round 6 user=scope_key, Round 7 升级
    # scope+sys_md5 — 反而让每种 prefix 独立 namespace 都 cold start (主人 dashboard
    # 看到 67% / 27% 是各自独立 cache cold 的结果).
    # DeepSeek 文档明确: "多请求公共前缀检测落盘" — 不传 user 时自动跨请求共享 prefix.

    try:
        from .prompt_cache import detect_provider as _detect_payload_provider

        if _detect_payload_provider(base_url, model) == "deepseek":
            payload.pop("user", None)
            payload.pop("user_id", None)
            payload.pop("prompt_cache_key", None)
            payload.pop("conversation_id", None)
    except Exception:  # noqa: BLE001
        pass

    _session_context_diagnostics = _prepare_session_context_payload(
        payload,
        model=model,
    )
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else messages
    _scope_for_request = get_current_scope_key() or ""
    _request_class = _request_class_for_route(request_route, _scope_for_request)
    _request_identity = _next_request_identity(
        messages,
        payload.get("tools"),
        request_route=request_route,
        request_class=_request_class,
    )

    _request_cache_diagnostics = _build_cache_request_diagnostics(
        base_url=base_url,
        model=model,
        payload=payload,
        request_route=request_route,
        request_class=_request_class,
        request_identity=_request_identity,
        session_context=_session_context_diagnostics,
    )
    _cache_diagnostics_token = _current_cache_request_diagnostics_var.set(
        _request_cache_diagnostics,
    )

    # Full request dumps remain available, but only when the single diagnostic
    # switch is explicitly enabled. The old messages/tools body is preserved.
    if _cache_request_dump_enabled():
        try:
            import time as _t
            from pathlib import Path as _Path

            _dump_root = _Path("D:/CattyQQAI/logs/req_dumps")
            _dump_root.mkdir(parents=True, exist_ok=True)
            _scope_safe = (get_current_scope_key() or "noscope").replace(
                ":", "_",
            ).replace("/", "_").replace("\\", "_")
            _ts = int(_t.time() * 1000)
            _dump = _dump_root / f"{_scope_safe}_{_ts}.json"
            _dump_obj = {
                "model": model,
                "base_url_head": base_url[:50],
                "payload_keys": sorted(payload.keys()),
                "payload_user": payload.get("user"),
                "payload_stream": payload.get("stream", False),
                "stream_param": stream,
                "enable_cache_param": enable_cache,
                "scope_key": get_current_scope_key(),
                "messages": messages,
                "tools": tools,
                "cohort": _request_cache_diagnostics.get("cohort_metadata"),
                "wire_tool_hash": _request_cache_diagnostics.get("wire_tool_hash"),
                "prefix_metadata": {
                    "prefix_sys_hash": _request_cache_diagnostics.get("prefix_sys_hash"),
                    "prefix_first_hash": _request_cache_diagnostics.get("prefix_first_hash"),
                    "message_end_role": _request_cache_diagnostics.get("message_end_role"),
                },
                "cache_diagnostics": _request_cache_diagnostics,
            }
            _dump_str = json.dumps(_dump_obj, ensure_ascii=False, indent=2, default=str)
            _dump.write_text(_dump_str, encoding="utf-8")
            _olds = sorted(_dump_root.glob(f"{_scope_safe}_*.json"))
            for _o in _olds[:-8]:
                try:
                    _o.unlink()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as _dump_exc:  # noqa: BLE001
            import traceback as _tb

            _logger.warning(
                f"req dump failed: {type(_dump_exc).__name__}: {_dump_exc}\n"
                + _tb.format_exc()[:600],
            )

    # === Dashboard stream lifecycle hook (DeepSeek / OpenAI compat 路径) ===
    # 主人 2026-05-28: 让非 Anthropic 路径也能在 dashboard 上看到对话.
    # stream=True: chunk-by-chunk push (helper 内部已做)
    # stream=False: 收到完整 response 后一次性 push 完整 text 当一个 delta
    # finally 块兜底清理 active stream (失败 raise 路径).
    _dash_stream_id: str | None = None
    try:
        from . import dashboard_state as _dash_mod
        _dash_stream_id = _dash_mod.start_stream(model=model)
    except Exception:  # noqa: BLE001
        _dash_mod = None  # type: ignore[assignment]

    try:
        # 主人:任何 5xx 自动 retry 3 次(共 4 次尝试),3 次都失败才上抛
        # 4xx 是 client error,重试也是一样的错,不重试
        last_error: OpenAICompatibleError | None = None
        for attempt in range(4):
            if stream:
                # === Streaming branch (主人 2026-05-28 Step 1) ===
                try:
                    data = await _stream_chat_completion_attempt(
                        url=_chat_completions_url(base_url),
                        headers=headers,
                        payload=payload,
                        timeout=timeout,
                        proxy=proxy,
                        dash_stream_id=_dash_stream_id,
                        dash_mod=_dash_mod,
                    )
                except OpenAICompatibleError as exc:
                    _status = getattr(exc, "status_code", None)
                    if _status and 500 <= _status < 600:
                        # 5xx → retry
                        last_error = exc
                        if attempt < 3:
                            backoff = 0.5 * (2 ** attempt)
                            _logger.info(
                                f"AI 接口 (stream) {_status} retry {attempt + 1}/3 after {backoff}s",
                            )
                            await asyncio.sleep(backoff)
                            continue
                        raise
                    # 主人 2026-07-06: 端点拒 prompt_cache_key (严格中转 400/422) →
                    # 剥参 + 拉黑该端点 + 重试本轮; 其余 4xx 语义不变 (直接抛)。
                    if _status in (400, 422) and "prompt_cache_key" in payload:
                        payload.pop("prompt_cache_key", None)
                        _PROMPT_CACHE_KEY_BLOCKED.add(f"{base_url}|{model}")
                        last_error = exc
                        _logger.warning(
                            f"endpoint rejected prompt_cache_key (stream HTTP {_status}), "
                            f"stripped + blocked {base_url[:40]}|{model[:20]}, retrying",
                        )
                        continue
                    # 4xx 直接抛
                    raise
                _log_cache_stats(data, model, messages=messages, base_url=base_url)
                # 流式分支: chunk 已经 chunk-by-chunk 推过, 这里只 end_stream
                if _dash_stream_id and _dash_mod is not None:
                    try:
                        _dash_mod.end_stream(_dash_stream_id, final_usage=data.get("usage"))
                        _dash_stream_id = None
                    except Exception:  # noqa: BLE001
                        pass
                return data

            # === Non-streaming branch (现有逻辑保留) ===
            async with httpx.AsyncClient(**_client_kwargs(timeout, proxy)) as client:
                response = await client.post(
                    _chat_completions_url(base_url), headers=headers, json=payload,
                )

            if response.status_code < 400:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise OpenAICompatibleError(
                        "AI 返回的不是 JSON。", response.text[:500],
                    ) from exc
                # === cache hit 监测 (DeepSeek / Anthropic / OpenAI 都识别) ===
                # 主人 2026-05-28: 改为始终调用, 函数内部自己判断有无 cache 字段
                # (DeepSeek 路径 enable_cache=False 但仍想看 prompt_cache_hit_tokens).
                _log_cache_stats(data, model, messages=messages, base_url=base_url)
                # === Dashboard: 推完整 text 当一次 delta + end_stream ===
                if _dash_stream_id and _dash_mod is not None:
                    try:
                        _text = ""
                        _choices = data.get("choices") or []
                        if _choices:
                            _msg = _choices[0].get("message") or {}
                            _content = _msg.get("content")
                            if isinstance(_content, str):
                                _text = _content
                            elif _content is not None:
                                _text = str(_content)
                        if _text:
                            _dash_mod.push_event(_dash_stream_id, {
                                "delta_text": _text,
                                "event_type": "openai_completion",
                            })
                        _dash_mod.end_stream(_dash_stream_id, final_usage=data.get("usage"))
                        _dash_stream_id = None  # 标记已清理, finally 不再重复 end
                    except Exception:  # noqa: BLE001
                        pass
                return data

            detail = response.text[:500]
            if not (500 <= response.status_code < 600):
                # 主人 2026-07-06: 端点拒 prompt_cache_key (严格中转 400/422) →
                # 剥参 + 拉黑该端点 + 重试本轮; 其余 4xx 语义不变 (直接抛)。
                if response.status_code in (400, 422) and "prompt_cache_key" in payload:
                    payload.pop("prompt_cache_key", None)
                    _PROMPT_CACHE_KEY_BLOCKED.add(f"{base_url}|{model}")
                    last_error = OpenAICompatibleError(
                        _catty_http_status_message("AI 接口", response.status_code), detail,
                    )
                    _logger.warning(
                        f"endpoint rejected prompt_cache_key (HTTP {response.status_code}), "
                        f"stripped + blocked {base_url[:40]}|{model[:20]}, retrying",
                    )
                    continue
                # 4xx 直接抛,无重试意义
                raise OpenAICompatibleError(
                    _catty_http_status_message("AI 接口", response.status_code), detail,
                )

            last_error = OpenAICompatibleError(
                _catty_http_status_message("AI 接口", response.status_code), detail,
            )
            if attempt < 3:
                backoff = 0.5 * (2 ** attempt)  # 0.5 / 1.0 / 2.0 s
                _logger.info(
                    f"AI 接口 {response.status_code} retry {attempt + 1}/3 after {backoff}s"
                )
                await asyncio.sleep(backoff)

        assert last_error is not None
        raise last_error
    finally:
        # 异常路径兜底清理 (200 OK 后 _dash_stream_id 已设 None, 这里只处理 raise 出去的情况)
        if _dash_stream_id:
            try:
                from . import dashboard_state as _dash_mod2
                _dash_mod2.end_stream(_dash_stream_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            _current_cache_request_diagnostics_var.reset(_cache_diagnostics_token)
        finally:
            try:
                from .nlu.prompt_compressor import clear_anchor_observation

                clear_anchor_observation()
            except Exception:  # noqa: BLE001
                pass


async def _post_ollama_chat(
    *,
    base_url: str,
    model: str,
    messages: list[ChatMessage],
    timeout: float,
    proxy: str,
    temperature: float | None,
    max_tokens: int | None,
    extra_headers: dict[str, str],
    extra_body: dict[str, Any],
) -> str:
    if not base_url.strip():
        raise OpenAICompatibleError("Ollama 接口地址为空。")
    if not model.strip():
        raise OpenAICompatibleError("Ollama 模型名为空。")

    options = _ollama_options(temperature=temperature, max_tokens=max_tokens, extra_body=extra_body)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if options:
        payload["options"] = options
    if "keep_alive" in extra_body:
        payload["keep_alive"] = extra_body["keep_alive"]
    if "think" in extra_body:
        payload["think"] = extra_body["think"]

    _prepare_session_context_payload(payload, model=model)

    headers = {
        "Content-Type": "application/json",
        **extra_headers,
    }
    async with httpx.AsyncClient(**_client_kwargs(timeout, proxy)) as client:
        response = await client.post(_ollama_chat_url(base_url), headers=headers, json=payload)

    if response.status_code >= 400:
        detail = response.text[:500]
        raise OpenAICompatibleError(_catty_http_status_message("Ollama 接口", response.status_code), detail)

    try:
        data = response.json()
    except ValueError as exc:
        raise OpenAICompatibleError("Ollama 返回的不是 JSON。", response.text[:500]) from exc

    return _extract_ollama_chat_content(data)


def _needs_first_frame(url: str, content_type: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".gif", ".webp")) or "gif" in content_type or "webp" in content_type


def _first_frame_data_url(data: bytes) -> str | None:
    try:
        with Image.open(BytesIO(data)) as image:
            frame_count = getattr(image, "n_frames", 1)
            image_format = (image.format or "").upper()
            if image_format not in {"GIF", "WEBP"} and frame_count <= 1:
                return None
            frame = next(ImageSequence.Iterator(image)).convert("RGBA")
            output = BytesIO()
            frame.save(output, format="PNG")
    except Exception:
        return None
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _data_url_content_type_and_bytes(url: str) -> tuple[str, bytes] | None:
    if not url.startswith("data:"):
        return None
    header, separator, encoded = url.partition(",")
    if not separator or ";base64" not in header.lower():
        return None
    content_type = header[5:].split(";", 1)[0].lower()
    try:
        data = base64.b64decode(encoded, validate=False)
    except Exception:
        return None
    return content_type, data


async def _vision_image_url(config: Config, url: str) -> str:
    if url.startswith("data:"):
        parsed = _data_url_content_type_and_bytes(url)
        if parsed is not None:
            content_type, data = parsed
            if _needs_first_frame(url, content_type):
                return _first_frame_data_url(data) or url
        return url
    timeout = config.catty_vision_request_timeout or config.catty_request_timeout
    async with httpx.AsyncClient(**_client_kwargs(timeout, config.catty_http_proxy)) as client:
        response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if not _needs_first_frame(url, content_type):
        data_url = _first_frame_data_url(response.content)
        return data_url or url
    return _first_frame_data_url(response.content) or url


def _image_analysis_from_reply(reply: str) -> dict[str, Any]:
    if not reply.strip():
        return {}
    parsed = _json_object(reply) or {}
    try:
        interest = int(parsed.get("interest", 0))
    except (TypeError, ValueError):
        interest = 0
    emotion_tags = parsed.get("emotion_tags")
    if isinstance(emotion_tags, str):
        tags = [item.strip() for item in emotion_tags.replace("，", ",").split(",") if item.strip()]
    elif isinstance(emotion_tags, list):
        tags = [str(item).strip() for item in emotion_tags if str(item).strip()]
    else:
        tags = []
    return {
        "summary": str(parsed.get("summary") or "").strip() or reply.strip(),
        "interest": max(min(interest, 100), 0),
        "emotion_tags": tags,
        "expression": str(parsed.get("expression") or "").strip(),
        "emoji_query": str(parsed.get("emoji_query") or "").strip(),
        "save_as_emoji": bool(parsed.get("save_as_emoji")),
        "raw": reply,
    }


async def download_binary(config: Config, url: str, *, timeout: float | None = None) -> tuple[bytes, str]:
    request_timeout = timeout or config.catty_vision_request_timeout or config.catty_request_timeout
    async with httpx.AsyncClient(**_client_kwargs(request_timeout, config.catty_http_proxy)) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.content, response.headers.get("content-type", "")


def _fallback_is_configured(config: Config) -> bool:
    # 主人 2026-05-28: 恢复真实判断 — 主人意图『sonnet 主, deepseek 备, 所有部分自动 fallback』.
    # 之前 (5 月初) 硬关闭是因为本地 ollama qwen 推理慢; 现在 ai_fallback 指向 deepseek API,
    # 性能跟主 cloud 接近, 让所有路径都能在 sonnet 失败时自动降到 deepseek.
    if not bool(getattr(config, "catty_ai_fallback_enabled", False)):
        return False
    if not str(getattr(config, "catty_ai_fallback_base_url", "") or "").strip():
        return False
    if not str(getattr(config, "catty_ai_fallback_api_key", "") or "").strip():
        return False
    if not str(getattr(config, "catty_ai_fallback_model", "") or "").strip():
        return False
    return True


def _fallback_should_strip_system(config: Config) -> bool:
    """catty-* 派生模型把人格烧进了 SYSTEM 层,再发 system role 会破坏 KV cache。"""
    if bool(getattr(config, "catty_ai_fallback_strip_system_messages", False)):
        return True
    model = str(getattr(config, "catty_ai_fallback_model", "") or "").strip().lower()
    return model.startswith("catty-")


def _strip_system_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if m.get("role") != "system"]


async def _check_mc_gate_or_raise(config: Config) -> None:
    """If MC has players online, refuse to run the local fallback model."""
    if not bool(getattr(config, "catty_ai_fallback_mc_gate_enabled", False)):
        return
    host = str(getattr(config, "catty_ai_fallback_mc_server_host", "") or "").strip()
    if not host:
        return
    port = int(getattr(config, "catty_ai_fallback_mc_server_port", 0) or 0)
    if port <= 0:
        return
    timeout = float(getattr(config, "catty_ai_fallback_mc_ping_timeout_seconds", 3.0) or 3.0)
    try:
        has_players = await mc_has_players(host, port, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("MC gate ping failed (%s); allowing fallback to proceed", exc)
        return
    if has_players:
        _logger.info("MC gate: players online at %s:%d, refusing local fallback", host, port)
        raise MCBusyError(
            _mc_busy_public_message(),
            "MC has players online; local 7B fallback is gated off to protect game performance.",
        )


async def _warmup_fallback_if_cold(config: Config) -> bool:
    """如果本地 fallback 模型最近没有被打到,先发一个 1-token "hi" 把它 load 上来。

    Ollama 第一次推理同时付"GGUF 从磁盘 load 到显存"+"prompt 处理"两份成本——
    在 5070Ti Laptop 上对 7B Q4_K_M 大概 60-120s。先用最小 prompt(1 token)单独
    付掉 load 那一份,然后真正用户请求只付 prompt 处理那一份(模型已经在显存里,
    KV cache 在 OLLAMA_KEEP_ALIVE=-1 下持久)。

    返回 True 代表本次确实做了 warmup;False 代表跳过(已经 warm 或者不支持)。
    失败不抛异常——真正的 fallback 调用会自己再试一次,该报错就让它报。
    """
    if _fallback_is_warm():
        return False
    base_url = config.catty_ai_fallback_base_url
    api_key = config.catty_ai_fallback_api_key
    model = config.catty_ai_fallback_model
    if not str(base_url or "").strip() or not str(model or "").strip():
        return False
    # 只对 Ollama 后端做 warmup;OpenAI-compatible HTTP 后端用法不一样,跳过。
    if not _looks_like_ollama_route(base_url, api_key, {}):
        return False
    _logger.info("Fallback cold-start: warming %s with 1-token hi", model)
    try:
        await _post_ollama_chat(
            base_url=base_url,
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            timeout=_FALLBACK_WARMUP_TIMEOUT_SECONDS,
            proxy=config.catty_http_proxy,
            temperature=None,
            max_tokens=1,
            extra_headers={},
            extra_body={"keep_alive": -1},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "Fallback warmup failed (%s: %s); real call will retry directly",
            exc.__class__.__name__,
            exc,
        )
        return False
    _mark_fallback_warmed()
    _logger.info("Fallback %s warmed up; real request can stream immediately", model)
    return True


async def _post_fallback_chat(
    config: Config,
    messages: list[ChatMessage],
    *,
    request_route: str = "fallback",
) -> str:
    await _check_mc_gate_or_raise(config)

    # 冷启动时先单独 warmup,避免真正请求同时承担 load + 大 prompt 处理被卡 60s+。
    await _warmup_fallback_if_cold(config)

    base_url = config.catty_ai_fallback_base_url
    api_key = config.catty_ai_fallback_api_key
    model = config.catty_ai_fallback_model
    extra_body = dict(config.catty_ai_fallback_extra_body or {})
    extra_headers = config.catty_ai_fallback_extra_headers or {}
    timeout = config.catty_ai_fallback_request_timeout or config.catty_request_timeout
    temperature = config.catty_ai_fallback_temperature
    max_tokens = config.catty_ai_fallback_max_tokens

    if _fallback_should_strip_system(config):
        stripped = _strip_system_messages(messages)
        dropped = len(messages) - len(stripped)
        if dropped > 0:
            _logger.info("fallback: dropped %d system message(s) for %s (persona baked in)", dropped, model)
        messages = stripped

    # 内存预算够时让 7B 留在内存复用更快。想要立刻卸载,在 config 的
    # ai_fallback.extra_body 里加 "keep_alive": 0 即可。
    if _looks_like_ollama_route(base_url, api_key, extra_body):
        result = await _post_ollama_chat(
            base_url=base_url,
            model=model,
            messages=messages,
            timeout=timeout,
            proxy=config.catty_http_proxy,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_headers=extra_headers,
            extra_body=extra_body,
        )
        _mark_fallback_warmed()
        return result
    # 主人 2026-07-06 openai-claude-95: summary/主线兜底共用线 — ai_fallback 三件套
    # 判定为 claude 时走 native /v1/messages (line="summary_fallback"), 失败落回 compat。
    if _route_native(config, request_route, base_url, model):
        try:
            from .anthropic_native_client import post_messages_native
            from .prompt_cache import resolve_cache_ttl
            # prefill/sweep/hoist/断点全在 post_messages_native 内部收口 (2026-07-06)
            _native_payload: dict[str, Any] = {
                "messages": messages,
                "max_tokens": max_tokens or 4096,
            }
            _native_diagnostics_token, _ = _bind_native_request_diagnostics(
                base_url=base_url,
                model=model,
                payload=_native_payload,
                request_route=request_route,
            )
            try:
                data = await post_messages_native(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=_native_payload["messages"],
                    max_tokens=max_tokens or 4096,
                    temperature=temperature,
                    timeout=float(timeout),
                    metadata_user_id=_scope_to_metadata_user_id(get_current_scope_key()),
                    cache_ttl=resolve_cache_ttl(config, request_route),
                    line=request_route,
                    prefill_mode=str(getattr(config, "catty_native_prefill_mode", "hint") or "hint"),
                    extra_betas=list(getattr(config, "catty_native_extra_betas", []) or []) or None,
                )
                _mark_fallback_warmed()
                return _extract_content(data)
            finally:
                _reset_native_request_diagnostics(_native_diagnostics_token)
        except Exception as native_exc:  # noqa: BLE001
            _logger.warning(
                "summary_fallback native /v1/messages failed (%s); trying OpenAI-compat",
                native_exc.__class__.__name__,
            )
    result = await _post_chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=timeout,
        proxy=config.catty_http_proxy,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_headers=extra_headers,
        extra_body=extra_body,
        request_route=request_route,
    )
    _mark_fallback_warmed()
    return result


async def chat_completion_with_tools(
    config: Config,
    messages: list[ChatMessage],
    *,
    tools: list[dict[str, Any]] | None,
    tool_executor: Any | None,
    max_rounds: int = 3,
    max_calls_per_round: int = 3,
    router_base_url: str = "",
    router_api_key: str = "",
    router_model: str = "",
    router_label: str = "",
    tool_choice: ToolChoice = "auto",
) -> str:
    """OpenAI function calling 主回复循环。

    tools/tool_executor 任一为空 → 退化到 plain chat_completion(完整 fallback 链)。
    tool 调度过程中云端抛错也直接降级到 chat_completion(保留本地 7B 兜底)。
    tool_executor 签名:async (name: str, arguments_json: str) -> dict。

    router_* 参数: 三件套都填时, 把这次 tool 调用整段切到 router endpoint
    (例如 catty_imagegen 意图命中 → 切 DeepSeek codex_instant 替主 AI 出 tool_call,
    省 Opus token 并避开 OOC 触发). 不带 router 时走 catty_openai_* 主通道.
    Native /v1/messages (catty_anthropic_native_enabled=True) 与 router 互斥, 一旦
    router 三件套齐就强制 OpenAI-compat 路径.
    """
    if not tools or tool_executor is None:
        _logger.info("tool_chat: tools/executor empty → fallback to plain chat_completion")
        return await chat_completion(config, messages)
    if not getattr(config, "catty_tools_enabled", True):
        _logger.info("tool_chat: catty_tools_enabled=False → fallback to plain chat_completion")
        return await chat_completion(config, messages)
    _router_active = bool(router_base_url.strip() and router_api_key.strip() and router_model.strip())
    if _cloud_is_unhealthy() and not _router_active:
        # 云端冷却期不带 tools 试,直接走 fallback 链 (router 走独立 endpoint, 不受主云冷却影响)。
        _logger.info("tool_chat: cloud unhealthy → fallback to plain chat_completion (no tools)")
        return await chat_completion(config, messages)
    # 主人 2026-05-28: native_enabled 时 with_tools 走 native /v1/messages 完整 tool
    # calling loop (post_messages_native_data 自动转换 OpenAI tools → Anthropic tools
    # 格式 + history 里 OpenAI 风格 tool 消息 → Anthropic native tool_use/tool_result
    # blocks), 享受 cache hit 100% 同时保留 tool 调用能力.
    # router 模式强制走 OpenAI-compat (DeepSeek 等不走 Anthropic /v1/messages).
    # 主人 2026-07-06 openai-claude-95: gate 改 _route_native (per-line 判别, 见 §4.1)。
    _native_route = (
        _route_native(config, "main", config.catty_openai_base_url, _effective_main_model(config))
        and not _router_active
    )
    if _router_active:
        _logger.info(
            "tool_chat: router=%s model=%s starting with %d tools (OpenAI-compat, bypass main AI)",
            router_label or "custom", router_model, len(tools),
        )
    elif _native_route:
        _logger.info("tool_chat: starting with %d tools available (native /v1/messages)", len(tools))
        if tool_choice != "auto":
            _logger.info("tool_chat: forced tool_choice requested, native route will use provider auto")
    else:
        _logger.info("tool_chat: starting with %d tools available (OpenAI-compat)", len(tools))
    if tool_choice != "auto" and not _native_route:
        _logger.info("tool_chat: first round tool_choice forced: %s", tool_choice)

    history: list[ChatMessage] = list(messages)
    allowed_tool_names = _allowed_tool_names(tools)

    # 主人 2026-06-06: 发一轮请求 (按当前路由选端点)。抽成内部 helper, 以便强制 tool_choice 被
    # 端点拒绝时能带着同一批 tools 用 "auto" 无缝重试本轮 (而不是丢掉工具降级成纯聊天)。
    # history 是同一个 list 对象, loop 体 append 后闭包下次调用会读到最新内容。
    async def _dispatch_round(rtc: ToolChoice) -> dict[str, Any]:
        if _native_route:
            from .anthropic_native_client import post_messages_native_data
            from .prompt_cache import resolve_cache_ttl
            # native /v1/messages 当前不透传 tool_choice — 一律走 provider auto。
            _native_payload: dict[str, Any] = {
                "messages": history,
                "tools": tools,
                "max_tokens": config.catty_max_tokens or 4096,
            }
            _native_model = _effective_main_model(config)
            _native_diagnostics_token, _ = _bind_native_request_diagnostics(
                base_url=config.catty_openai_base_url,
                model=_effective_main_model(config),
                payload=_native_payload,
                request_route="main",
            )
            try:
                return await post_messages_native_data(
                    config, _native_payload["messages"], tools=tools,
                    metadata_user_id=_scope_to_metadata_user_id(get_current_scope_key()),
                    model=_native_model,
                    cache_ttl=resolve_cache_ttl(config, "main"),
                    line="main",
                )
            finally:
                _reset_native_request_diagnostics(_native_diagnostics_token)
        if _router_active:
            # 主人 2026-05-28 Step 1: 非 Claude 端点开真流式 (chunk push dashboard)
            try:
                from .prompt_cache import is_claude_endpoint
                _router_stream = not is_claude_endpoint(router_base_url, router_model)
            except Exception:  # noqa: BLE001
                _router_stream = False
            return await _post_chat_completion_raw(
                base_url=router_base_url,
                api_key=router_api_key,
                model=router_model,
                messages=history,
                timeout=config.catty_request_timeout,
                proxy=config.catty_http_proxy,
                temperature=config.catty_temperature,
                max_tokens=config.catty_max_tokens,
                extra_headers={},
                extra_body={},
                tools=tools,
                tool_choice=rtc,
                enable_cache=False,
                cache_depth=2,
                stream=_router_stream,
                request_route="router",
            )
        try:
            from .prompt_cache import is_claude_endpoint
            _openai_stream = not is_claude_endpoint(
                config.catty_openai_base_url, _effective_main_model(config),
            )
        except Exception:  # noqa: BLE001
            _openai_stream = False
        return await _post_chat_completion_raw(
            base_url=config.catty_openai_base_url,
            api_key=config.catty_openai_api_key,
            model=_effective_main_model(config),
            messages=history,
            timeout=config.catty_request_timeout,
            proxy=config.catty_http_proxy,
            temperature=config.catty_temperature,
            max_tokens=config.catty_max_tokens,
            extra_headers=config.catty_openai_extra_headers,
            extra_body=config.catty_openai_extra_body,
            tools=tools,
            tool_choice=rtc,
            enable_cache=bool(getattr(config, "catty_prompt_cache_enabled", False)),
            cache_depth=int(getattr(config, "catty_prompt_cache_depth", 2) or 2),
            stream=_openai_stream,
            request_route="main",
        )

    # 强制 tool_choice 的目标端点 (用于"该端点不支持强制"的能力缓存)。native 不透传 tool_choice,
    # 故只对 router / openai-compat 路径有意义。
    _force_endpoint_key = (
        f"{router_base_url}|{router_model}" if _router_active
        else f"{config.catty_openai_base_url}|{_effective_main_model(config)}"
    )

    for round_idx in range(max(1, max_rounds)):
        round_tool_choice: ToolChoice = tool_choice if round_idx == 0 else "auto"
        # 已知该端点拒绝非 auto tool_choice (如 DeepSeek 思考模型) → 直接用 auto, 省一次必败 400。
        if round_tool_choice != "auto" and _forced_tool_choice_blocked(_force_endpoint_key):
            _logger.info(
                "tool_chat: endpoint known to reject forced tool_choice → using auto (tools kept)",
            )
            round_tool_choice = "auto"
        try:
            data = await _dispatch_round(round_tool_choice)
        except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as exc:
            # 主人 2026-06-06: 强制 tool_choice 第 0 轮失败 (端点不支持 object/required, 例如 DeepSeek
            # 思考模型返回 "Thinking mode does not support this tool_choice") → 不要丢掉工具降级成纯聊天
            # (那样明确画图请求永远不画)。带 tools 用 auto 重试本轮 — 实测 auto 下 AI 仍会主动调
            # catty_imagegen。native 路径根本不透传 tool_choice (其失败与 tool_choice 无关), 故 guard
            # 掉 — 避免把 native 的瞬时错误误判成"端点拒绝强制"而污染能力缓存 / 做无意义重试。
            if round_idx == 0 and round_tool_choice != "auto" and not _native_route:
                _logger.warning(
                    "tool_chat: forced tool_choice rejected (%s); retrying round 0 with tool_choice=auto (tools kept)",
                    exc.__class__.__name__,
                )
                try:
                    data = await _dispatch_round("auto")
                except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as exc2:
                    _logger.warning(
                        "chat_completion_with_tools: auto retry after forced reject also failed (%s); "
                        "degrading to plain chat_completion",
                        exc2.__class__.__name__,
                    )
                    return await chat_completion(config, history)
                # 仅"auto 重试成功 (而强制失败)"才确认是 tool_choice 维度问题 → 此时才标记端点拒绝强制;
                # 若 auto 也失败 (端点整体临时挂) 已在上面 return, 不会污染缓存。
                _mark_forced_tool_choice_blocked(_force_endpoint_key)
            else:
                _logger.warning(
                    "chat_completion_with_tools: round %d cloud call failed (%s); degrading to plain chat_completion",
                    round_idx,
                    exc.__class__.__name__,
                )
                # 降级到 plain 调用,让原有 fallback/cooldown 逻辑接管(它会自己 mark unhealthy)。
                return await chat_completion(config, history)

        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAICompatibleError("AI 返回格式不符合 Chat Completions。", repr(data)[:500]) from exc
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None

        if not tool_calls:
            # 模型直接给了最终回复
            if _cloud_is_unhealthy():
                _mark_cloud_healthy()
            _reply = _extract_content(data)
            # S6: 有 tools 但模型直接出文本回复 (不经 chat_completion) → 在此蒸馏
            _maybe_distill_reply(_reply, source="deepseek")
            return _reply

        normalized_tool_calls, call_specs = _normalize_assistant_tool_calls(
            tool_calls,
            round_idx=round_idx,
        )

        # Normalize missing ids and malformed function envelopes before history writes so
        # every assistant tool call has an explicit role=tool response in this round.
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "tool_calls": normalized_tool_calls,
        }
        # Preserve content (None and empty strings are both valid OpenAI protocol values).
        if isinstance(message.get("content"), (str, list)):
            assistant_msg["content"] = message["content"]
        else:
            assistant_msg["content"] = None
        if isinstance(message.get("reasoning_content"), str):
            assistant_msg["reasoning_content"] = message["reasoning_content"]
        history.append(assistant_msg)

        call_limit = max(1, max_calls_per_round)
        tool_payloads: dict[str, Any] = {}
        calls_to_execute: list[dict[str, str]] = []
        execution_call_ids: set[str] = set()
        for call_idx, call in enumerate(call_specs):
            args_len, fields, args_hash = _tool_argument_log_summary(call["arguments_json"])
            _logger.info(
                "tool_call: name=%s args_len=%d args_fields=%s args_sha256=%s",
                call["name"],
                args_len,
                fields,
                args_hash,
            )
            if call_idx >= call_limit:
                tool_payloads[call["id"]] = _tool_call_error(
                    "tool_call_truncated",
                    "Tool call was not executed because the per-round call limit was reached.",
                    max_calls_per_round=call_limit,
                )
            elif call["error_reason"]:
                tool_payloads[call["id"]] = _tool_call_error(
                    "invalid_tool_call",
                    "Tool call must include a valid function object and function name.",
                    reason=call["error_reason"],
                )
            elif call["name"] not in allowed_tool_names:
                tool_payloads[call["id"]] = _tool_call_error(
                    "tool_not_allowed",
                    "The requested tool was not exposed for this request.",
                    requested_tool=call["name"],
                )
            else:
                calls_to_execute.append(call)
                execution_call_ids.add(call["id"])

        if calls_to_execute:
            tool_results = await _execute_tool_calls(tool_executor, calls_to_execute)
            for call, result in zip(calls_to_execute, tool_results):
                tool_payloads[call["id"]] = result

        # Phase B1: collect full successful results for cascade checks. History receives
        # only the safe, bounded projection below; short-circuit checks keep full payloads.
        cascade_inputs: list[tuple[str, Any]] = []
        short_circuit_reply: str | None = None
        for call in call_specs:
            call_id = call["id"]
            name = call["name"]
            result = tool_payloads[call_id]
            if isinstance(result, BaseException):
                args_len, fields, args_hash = _tool_argument_log_summary(call["arguments_json"])
                payload = _tool_call_error(
                    "tool_execution_error",
                    "Tool executor raised an exception.",
                    exception_type=result.__class__.__name__,
                )
                _logger.warning(
                    "tool_call failed: name=%s args_len=%d args_fields=%s args_sha256=%s error_type=%s",
                    name,
                    args_len,
                    fields,
                    args_hash,
                    result.__class__.__name__,
                )
            elif not isinstance(result, dict):
                payload = {"value": result}
            else:
                payload = result
                if call_id in execution_call_ids:
                    cascade_inputs.append((name, payload))
            if (
                len(calls_to_execute) == 1
                and call_id in execution_call_ids
                and isinstance(payload, dict)
                and isinstance(payload.get("_short_circuit_reply"), str)
                and payload["_short_circuit_reply"].strip()
            ):
                short_circuit_reply = payload["_short_circuit_reply"].strip()
                _logger.info(
                    "tool_chat: %s short-circuit return (reply_len=%d, skip follow-up)",
                    name, len(short_circuit_reply),
                )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": _serialize_tool_result_for_history(payload),
                }
            )

        if short_circuit_reply:
            if _cloud_is_unhealthy():
                _mark_cloud_healthy()
            # S6: tool 短路回复 (catty_imagegen agent 模式等, 不经 chat_completion) → 蒸馏
            _maybe_distill_reply(short_circuit_reply, source="deepseek_tool")
            return short_circuit_reply

        # Phase B1: tool cascade — 看上一轮 tool 结果, 给 AI 一个『下一步推荐调 X』hint
        if cascade_inputs:
            try:
                from .tool_cascade import build_post_tool_hint
                _cascade_hint = build_post_tool_hint(
                    cascade_inputs,
                    allowed_tools=allowed_tool_names,
                )
                if _cascade_hint:
                    history.append({"role": "system", "content": _cascade_hint})
            except Exception as _cascade_exc:  # noqa: BLE001
                _logger.debug("tool_cascade check failed (non-fatal): %s", _cascade_exc)

        # Phase B3: tool result post-process hint — 防 AI 拿到 result 后复读 JSON / 列字段名
        if call_specs:
            history.append(
                {
                    "role": "system",
                    "content": _tool_result_follow_up_hint(),
                }
            )

        # 处理被截断的 tool_calls:给模型一条提示,下一轮可以继续
        truncated = max(len(call_specs) - call_limit, 0)
        if truncated > 0:
            history.append(
                {
                    "role": "system",
                    "content": f"上一轮还有 {truncated} 个 tool 调用被截断,本轮请基于已收到的工具结果继续。",
                }
            )

    # 循环达到上限还在调 tool — 强制再发一次纯回复,禁掉 tools。
    history.append(
        {
            "role": "system",
            "content": "已达到本次主回复的工具调用上限,请直接基于已有信息和上下文给最终回复,不再调用工具。",
        }
    )
    return await chat_completion(config, history)


async def chat_completion(config: Config, messages: list[ChatMessage]) -> str:
    """主回复入口 (plain chat). S6: 成功返回后统一蒸馏到 L3.

    with_tools 的多数降级路径也委托到这里 → 一处覆盖主 AI / fallback DeepSeek /
    catnify 透传回 L5 等所有走主回复的链路. _maybe_distill_reply 内部有安全阀,
    后台 summary (走 chat_completion_summary 兜底进来) 因 scope=summary:* 被自动挡掉.
    """
    reply = await _chat_completion_impl(config, messages)
    _maybe_distill_reply(reply, source="deepseek")
    return reply


async def _chat_completion_impl(config: Config, messages: list[ChatMessage]) -> str:
    fallback_ready = _fallback_is_configured(config)
    cooldown = float(getattr(config, "catty_ai_fallback_cooldown_seconds", 300.0))

    # 云端在冷却期：直接走 fallback，不再戳云白白等超时。
    # MCBusyError（MC 有玩家）会自然向上传播给用户看到。
    if fallback_ready and _cloud_is_unhealthy():
        _logger.info(
            "chat_completion: cloud in cooldown (%.0fs remaining), routing to local fallback %s",
            _cloud_health_remaining_seconds(),
            config.catty_ai_fallback_model,
        )
        return await _post_fallback_chat(config, messages)

    # Anthropic native /v1/messages 分支 (主人 2026-05-28: NewAPI SG relay 透传 body 已开,
    # context_management server-side compaction 已在 Anthropic 端激活).
    # 失败降级走原 OpenAI-compat /chat/completions 路径 (兜底保证主链路不挂).
    # 主人 2026-07-06 openai-claude-95: gate 改 _route_native — 总闸开着但主线是
    # deepseek 时不再白试 native (旧行为: 每轮 native 失败再降级, 浪费一次 RTT)。
    if _route_native(config, "main", config.catty_openai_base_url, _effective_main_model(config)):
        try:
            result = await _post_anthropic_native_chat(config, messages)
            if _cloud_is_unhealthy():
                _logger.info("chat_completion: native path recovered, clearing cooldown")
                _mark_cloud_healthy()
            return result
        except Exception as native_exc:  # noqa: BLE001
            _logger.warning(
                "chat_completion: native /v1/messages failed (%s), fallback to /chat/completions",
                native_exc.__class__.__name__,
            )
            # 不熔断 (cloud_unhealthy 不标记), 直接降级到 OpenAI-compat 同一中转端点

    # 正常走云。
    # Phase A: 主回复路径补 enable_cache (之前 bug: 只 with_tools / codex_instant 路径有 cache).
    # 动态 cache_depth: 热 session (history>=12 条 user/assistant) → 4, 冷 → 2.
    # depth=4 多覆盖 history (热 session prefix 稳定到更深), depth=2 避免冷 session 频繁 invalidate.
    _cache_enable = bool(getattr(config, "catty_prompt_cache_enabled", True))
    _non_system_count = sum(
        1 for m in messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    )
    _cache_depth_base = int(getattr(config, "catty_prompt_cache_depth", 2))
    _cache_depth_dynamic = 4 if _non_system_count >= 12 else _cache_depth_base
    try:
        result = await _post_chat_completion(
            base_url=config.catty_openai_base_url,
            api_key=config.catty_openai_api_key,
            model=_effective_main_model(config),
            messages=messages,
            timeout=config.catty_request_timeout,
            proxy=config.catty_http_proxy,
            temperature=config.catty_temperature,
            max_tokens=config.catty_max_tokens,
            extra_headers=config.catty_openai_extra_headers,
            extra_body=config.catty_openai_extra_body,
            enable_cache=_cache_enable,
            cache_depth=_cache_depth_dynamic,
        )
        # 云端成功:清除任何残留 unhealthy 标记
        if _cloud_is_unhealthy():
            _logger.info("chat_completion: cloud recovered, clearing cooldown")
            _mark_cloud_healthy()
        return result
    except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as cloud_exc:
        if not fallback_ready:
            raise
        _mark_cloud_unhealthy(cooldown)
        _logger.warning(
            "chat_completion: cloud call failed (%s), routing to local fallback %s for next %.0fs",
            cloud_exc.__class__.__name__,
            config.catty_ai_fallback_model,
            cooldown,
        )
        try:
            return await _post_fallback_chat(config, messages)
        except MCBusyError:
            # MC 有人 → 直接抛"游戏中不可用"给用户，不被云错误覆盖
            raise
        except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as fb_exc:
            # 云端和 fallback 都挂了:抛云的原始错误,把 fallback 异常作为 __cause__
            raise cloud_exc from fb_exc


async def local_critic_completion(
    config: Config,
    messages: list[ChatMessage],
    *,
    timeout: float | None = None,
    max_tokens: int | None = None,
    extra_body: dict[str, Any] | None = None,
) -> str:
    body = extra_body if extra_body is not None else config.catty_local_critic_extra_body
    request_timeout = timeout or config.catty_local_critic_request_timeout or config.catty_request_timeout
    request_max_tokens = max_tokens if max_tokens is not None else config.catty_local_critic_max_tokens
    if _looks_like_ollama_route(config.catty_local_critic_base_url, config.catty_local_critic_api_key, body):
        return await _post_ollama_chat(
            base_url=config.catty_local_critic_base_url,
            model=config.catty_local_critic_model,
            messages=messages,
            timeout=request_timeout,
            proxy=config.catty_http_proxy,
            temperature=config.catty_local_critic_temperature,
            max_tokens=request_max_tokens,
            extra_headers=config.catty_local_critic_extra_headers,
            extra_body=body,
        )

    return await _post_chat_completion(
        base_url=config.catty_local_critic_base_url,
        api_key=config.catty_local_critic_api_key,
        model=config.catty_local_critic_model,
        messages=messages,
        timeout=request_timeout,
        proxy=config.catty_http_proxy,
        temperature=config.catty_local_critic_temperature,
        max_tokens=request_max_tokens,
        extra_headers=config.catty_local_critic_extra_headers,
        extra_body=body,
        request_route="local_critic",
    )


def _json_decision(text: str, key: str) -> bool:
    raw = text.strip()
    if not raw:
        return False
    parsed = _json_object(raw)
    if isinstance(parsed, dict):
        value = parsed.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "y", "1", "reply", "split"}

    normalized = raw.strip().lower()
    if key == "reply":
        return normalized in {"reply", "yes", "true", "1", "回复"}
    if key == "split":
        return normalized in {"split", "yes", "true", "1", "拆分"}
    return False


def _json_object(text: str) -> dict[str, Any] | None:
    """LLM 输出宽容 JSON 解析:fence / 智能引号 / 尾逗号 / 单引号 / mixed text 都能恢复。

    实际逻辑在 parsers.lenient_json_object;保留本函数名是为了不动现有 caller。
    """
    return lenient_json_object(text)


async def chat_completion_summary(config: Config, messages: list[ChatMessage]) -> str:
    """专用后台总结路径：优先走 ai_fallback (deepseek)，避免烧主 AI (opus/sonnet) token。

    主人 2026-05-28: 群/私聊/成员/游戏摘要属于后台批处理, deepseek-v4-flash 完全够用,
    没必要每天用 opus 烧 ¥. fallback 不可用或调用失败时回退到 chat_completion (主 AI)。
    """
    if _fallback_is_configured(config):
        try:
            return await _post_fallback_chat(
                config,
                messages,
                request_route="summary_fallback",
            )
        except MCBusyError:
            _logger.info("chat_completion_summary: MC busy, falling back to main AI for summary")
        except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as exc:  # noqa: BLE001
            _logger.warning(
                "chat_completion_summary: ai_fallback failed (%s), falling back to main AI",
                exc.__class__.__name__,
            )
    return await chat_completion(config, messages)


async def chat_completion_instant(config: Config, messages: list[ChatMessage], *, fallback_max_tokens: int = 80) -> str:
    """走 catty_filter_* 配置(spark 这种小快模型)的瞬时完成。

    用途:placeholder 等候语、签到/积分卡 caption 这种 1-2 句猫娘短话——
    主回复模型 (gpt-5.5 等) 响应慢,这里需要『立刻』出文案不能等。
    复用 _filter_completion 的路由逻辑;无 spark 配置时自动回退到 audit/openai。
    """
    reply = await _filter_completion(config, messages, fallback_max_tokens=fallback_max_tokens)
    # S6: instant 是面向用户的猫娘短回复 (占位话/签到 caption) → 蒸馏.
    # 注意: 这里在 chat_completion_instant 埋而非 _filter_completion, 因为 _filter_completion
    # 还被 classify_mood / assess_anger / should_reply 等分类判断复用 (输出 bool/JSON, 不能采).
    _maybe_distill_reply(reply, source="deepseek_instant")
    return reply


async def _post_with_fallback(
    config: Config,
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout: float,
    temperature: float | None,
    max_tokens: int | None,
    extra_headers: dict[str, str],
    extra_body: dict[str, Any],
    enable_cache: bool = False,
    cache_depth: int = 2,
    label: str = "",
    line: str = "",
) -> str:
    """主 endpoint 调用失败 → 自动 fallback 到 ai_fallback config (sonnet → deepseek).

    主人 2026-05-28: 覆盖 spark / filter / mood / anger / summarize 等所有路径.
    vision 除外 (主人原话 'vision 不用 fallback', 因 deepseek-v4-flash 不支持图).
    主回复 chat_completion 已有 fallback 链, 本函数给小模型短路径用.

    主人 2026-05-28: native_enabled + Claude endpoint 时, 也走 native /v1/messages
    (绕开 NewAPI 中转层 OpenAI→Anthropic 转换 bug: codex_instant 等路径 OpenAI-compat
    带 role=system 被中转层转 Anthropic 时不提到顶层 system, Anthropic 直接 400 拒收).

    主人 2026-07-06 openai-claude-95: gate 改 _route_native(line) per-line 判别;
    降级链重排 native 失败 → 同 triple OpenAI-compat → line fallback (旧行为 native
    异常直接跳 line fallback, 跳过了 compat 一级)。
    """
    _line = line or "other"
    try:
        # native /v1/messages 路径优先 (per-line 路由: 总闸 + override + detect_provider)
        if _route_native(config, _line, base_url, model):
            try:
                from .anthropic_native_client import post_messages_native
                from .prompt_cache import resolve_cache_ttl
                # 主人 2026-05-28 C18 → 2026-07-06 收口: cache_control 标位、sweep、hoist、
                # prefill 全由 post_messages_native 内部单一 owner 处理, caller 零预处理.
                _native_payload: dict[str, Any] = {
                    "messages": messages,
                    "max_tokens": max_tokens or 4096,
                }
                _native_diagnostics_token, _ = _bind_native_request_diagnostics(
                    base_url=base_url,
                    model=model,
                    payload=_native_payload,
                    request_route=_line,
                )
                try:
                    data = await post_messages_native(
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        messages=_native_payload["messages"],
                        max_tokens=max_tokens or 4096,
                        temperature=temperature,
                        timeout=float(timeout),
                        enable_compaction=bool(getattr(config, "catty_compaction_enabled", False)),
                        compaction_trigger_tokens=int(getattr(config, "catty_compaction_trigger_tokens", 150_000)),
                        metadata_user_id=_scope_to_metadata_user_id(get_current_scope_key()),
                        cache_ttl=resolve_cache_ttl(config, _line),
                        line=_line,
                        prefill_mode=str(getattr(config, "catty_native_prefill_mode", "hint") or "hint"),
                        extra_betas=list(getattr(config, "catty_native_extra_betas", []) or []) or None,
                    )
                    return _extract_content(data)
                finally:
                    _reset_native_request_diagnostics(_native_diagnostics_token)
            except Exception as native_exc:  # noqa: BLE001
                _logger.warning(
                    "%s native /v1/messages failed (%s); trying OpenAI-compat same endpoint",
                    label or "primary",
                    native_exc.__class__.__name__,
                )
                # fall through → 同 triple 的 OpenAI-compat (请求本身可用, 只是 cache 死)
        return await _post_chat_completion(
            base_url=base_url, api_key=api_key, model=model,
            messages=messages, timeout=timeout, proxy=config.catty_http_proxy,
            temperature=temperature, max_tokens=max_tokens,
            extra_headers=extra_headers, extra_body=extra_body,
            enable_cache=enable_cache, cache_depth=cache_depth,
            request_route=_line,
        )
    except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        if not _fallback_is_configured(config):
            raise
        _logger.warning(
            "%s cloud call failed (%s); falling back to %s",
            label or "primary",
            exc.__class__.__name__,
            config.catty_ai_fallback_model,
        )
        return await _post_fallback_chat(config, messages, request_route=_line)
    except Exception as exc:  # noqa: BLE001
        # native SDK 抛的 anthropic.* 异常不属于上面捕获范围; 让它也走 fallback
        if not _fallback_is_configured(config):
            raise
        _logger.warning(
            "%s cloud call failed (%s); falling back to %s",
            label or "primary",
            exc.__class__.__name__,
            config.catty_ai_fallback_model,
        )
        return await _post_fallback_chat(config, messages, request_route=_line)


async def chat_completion_codex_instant(
    config: Config,
    messages: list[ChatMessage],
    *,
    max_tokens: int = 800,
    model_override: str = "",
    request_route: str = "spark",
) -> str:
    """通用 5.3-codex 短回复路径 — placeholder / 签到 caption / NSFW deep 共用。
    model_override 优先, 否则 catty_nsfw_spark_model, 兜底 catty_filter_model.
    走 catty_filter_base_url + catty_filter_api_key (跟 spark 同 host) 但 model 单独配,
    跟 _filter_completion 的 catty_filter_model (mood classifier / spark 等) 解耦.
    主人原话:placeholder / 签到 caption / NSFW deep 三处都不要走 spark, 统一走 codex.
    """
    nsfw_model = (
        (model_override or "").strip()
        or (config.catty_codex_instant_model or "").strip()
        or (config.catty_nsfw_spark_model or "").strip()
        or config.catty_filter_model
    )
    # nsfw_spark 独立 endpoint (主人 2026-05-27 选项 2): 配上就走独立, 空时走 filter 同站
    base_url = (
        (config.catty_nsfw_spark_base_url or "").strip()
        or config.catty_filter_base_url
        or config.catty_audit_ai_base_url
        or config.catty_openai_base_url
    )
    api_key = (
        (config.catty_nsfw_spark_api_key or "").strip()
        or config.catty_filter_api_key
        or config.catty_audit_ai_api_key
        or config.catty_openai_api_key
    )
    reply = await _post_with_fallback(
        config,
        base_url=base_url,
        api_key=api_key,
        model=nsfw_model,
        messages=messages,
        timeout=config.catty_request_timeout,
        temperature=config.catty_temperature,
        max_tokens=max_tokens,
        extra_headers=config.catty_filter_extra_headers or config.catty_openai_extra_headers,
        extra_body=config.catty_filter_extra_body or config.catty_openai_extra_body,
        enable_cache=bool(getattr(config, "catty_prompt_cache_enabled", False)),
        cache_depth=int(getattr(config, "catty_prompt_cache_depth", 2) or 2),
        label=f"codex_instant({nsfw_model})",
        line=request_route,
    )
    # S6: codex_instant 覆盖 NSFW spark / 占位话 / 签到 caption — 都是面向用户的猫娘回复 → 蒸馏.
    # (在此入口埋而非 _post_with_fallback, 后者还被 _filter_completion 分类路径复用.)
    _maybe_distill_reply(reply, source="deepseek_codex")
    return reply


async def _filter_completion(config: Config, messages: list[ChatMessage], *, fallback_max_tokens: int = 64) -> str:
    use_filter_route = bool(config.catty_filter_model.strip())
    base_url = (
        (config.catty_filter_base_url if use_filter_route else "")
        or config.catty_audit_ai_base_url
        or config.catty_openai_base_url
    )
    api_key = (
        (config.catty_filter_api_key if use_filter_route else "")
        or config.catty_audit_ai_api_key
        or config.catty_openai_api_key
    )
    model = config.catty_filter_model if use_filter_route else (config.catty_audit_ai_model or config.catty_openai_model)
    temperature = config.catty_filter_temperature if use_filter_route else config.catty_audit_ai_temperature
    max_tokens = config.catty_filter_max_tokens if use_filter_route else config.catty_audit_ai_max_tokens
    return await _post_with_fallback(
        config,
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=(
            (config.catty_filter_request_timeout if use_filter_route else None)
            or config.catty_audit_ai_request_timeout
            or config.catty_request_timeout
        ),
        temperature=temperature,
        max_tokens=max_tokens or fallback_max_tokens,
        extra_headers=(
            (config.catty_filter_extra_headers if use_filter_route else {})
            or config.catty_audit_ai_extra_headers
            or config.catty_openai_extra_headers
        ),
        extra_body=(
            (config.catty_filter_extra_body if use_filter_route else {})
            or config.catty_audit_ai_extra_body
            or config.catty_openai_extra_body
        ),
        label=f"filter({model})",
        line="filter",
    )


async def should_reply_to_group_message(config: Config, message_text: str, *, has_image: bool = False) -> bool:
    if not config.catty_filter_enabled:
        return False
    if not (config.catty_filter_api_key or config.catty_audit_ai_api_key or config.catty_openai_api_key):
        return False

    content = message_text.strip() or ("[图片]" if has_image else "")
    if not content:
        return False
    prompt = (
        "你是QQ群AI回复过滤器，只判断普通群消息是否明显指向机器人/AI并需要机器人回复。"
        "规则：明确在问机器人、AI、猫猫、助手，或明显要求机器人帮忙/回答/看图，才回复；"
        "普通闲聊、群友互相对话、吐槽、表情包、没有指向对象的问题，一律不回复。"
        "只输出JSON：{\"reply\":true|false}，不要解释。"
    )
    user_content = f"消息：{content}\n是否有图片：{'是' if has_image else '否'}"
    reply = await _filter_completion(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}],
    )
    return _json_decision(reply, "reply")


_CATTY_MOOD_DIMS: tuple[str, ...] = (
    "happy", "excited", "annoyed", "shy", "sad", "sleepy", "sulky", "bored",
)
_CATTY_MOOD_BASE_DELTA = 18.0  # weight=1.0 时的 delta 上限


async def classify_catty_mood(config: Config, text: str) -> list[tuple[str, float]]:
    """让 spark 小模型判断一条用户消息会触发笨猫哪些情绪维度。

    返回 [(dim, delta)] 列表, delta = weight * _CATTY_MOOD_BASE_DELTA;
    走 catty_filter_* 路由(spark), 无 filter 配置则回退 audit/openai。
    LLM 失败、JSON 解析失败、无命中 → 返回 []。
    """
    if not text or not text.strip():
        return []
    if not (config.catty_filter_api_key or config.catty_audit_ai_api_key or config.catty_openai_api_key):
        return []
    prompt = (
        "你是QQ猫娘机器人『笨猫』的情绪分类器。读用户这一条消息，判断它会让笨猫产生哪些情绪反应。"
        "8 个情绪维度:\n"
        "- happy   开心(好消息/被夸/有趣/逗笑)\n"
        "- excited 兴奋(惊喜/激动/牛 b 事/突发好运)\n"
        "- annoyed 烦躁(冒犯/吐槽/无理/重复打扰)\n"
        "- shy     害羞(暧昧/亲密/告白/性暗示)\n"
        "- sad     难过(分享负面/伤心事/委屈)\n"
        "- sleepy  困倦(深夜/熬夜/晚安/疲惫)\n"
        "- sulky   生闷气(被指责/被嫌弃/赶人/羞辱)\n"
        "- bored   无聊(无意义闲话/划水/没话找话)\n\n"
        "只输出 JSON: {\"hits\":[{\"dim\":\"happy\",\"weight\":0.8}, ...]}\n"
        "- weight 在 [0.0, 1.0]，1.0 = 极强，0.5 = 中等，0.0 = 完全没\n"
        "- 只输出 weight >= 0.3 的维度，最多 3 个\n"
        "- 没有命中就 hits: []\n"
        "- 不要解释，不要多余字段"
    )
    try:
        reply = await _filter_completion(
            config,
            [{"role": "system", "content": prompt}, {"role": "user", "content": text.strip()}],
            fallback_max_tokens=120,
        )
    except Exception:  # noqa: BLE001
        return []
    parsed = _json_object(reply)
    if not isinstance(parsed, dict):
        return []
    hits = parsed.get("hits")
    if not isinstance(hits, list):
        return []
    out: list[tuple[str, float]] = []
    for item in hits:
        if not isinstance(item, dict):
            continue
        dim = item.get("dim")
        if not isinstance(dim, str) or dim not in _CATTY_MOOD_DIMS:
            continue
        try:
            w = float(item.get("weight", 0.0))
        except (TypeError, ValueError):
            continue
        if w < 0.3:
            continue
        w = min(max(w, 0.0), 1.0)
        out.append((dim, round(w * _CATTY_MOOD_BASE_DELTA, 2)))
    return out


async def summarize_scope_lore(
    config: Config,
    history_excerpt: str,
    scope_label: str = "",
) -> list[dict]:
    """从 scope 最近对话总结出值得长期记下来的 lorebook entry。

    主人 2026-05-28: 改走 chat_completion_summary (deepseek 优先), 和其它后台
    总结 (_summary_loop 4 段) 统一,不烧 opus token. deepseek-v4-flash 做 JSON
    提取够用; ai_fallback 不可用时自动回退到主 AI。
    返回 list of {"keys": [...], "content": "..."} (0-3 条), 失败 / 无输出返回 []。

    每条 entry:
    - keys: 1-3 个关键词, 以后命中 user_text substring 时触发 prompt 注入
    - content: 一句口语化的『这个群值得记的事』, 给笨猫长期记忆用
    """
    history_excerpt = (history_excerpt or "").strip()
    if not history_excerpt:
        return []
    prompt = (
        "你在帮一只 QQ 群猫娘机器人(『笨猫』)整理她对当前群聊场景的『长期记忆』。\n"
        "下面是这个 scope 最近的一段对话历史。你的任务:**从中挑出 0-3 条**值得笨猫长期记住的"
        "『这个群专属的小事』,做成 lorebook 条目。\n\n"
        "**该挑的**(请选这类):\n"
        "- 这个群反复出现的梗 / 黑话 / 内部段子(以后再听到能接住, 能顺势 callback)\n"
        "- 重要群友的稳定特征 / 偏好 / 称呼 / 边界 (比如『张三喜欢被叫小张, 不喜欢被叫老师』)\n"
        "- 群规 / 风气 / 重大事件 / 长期关系网(谁经常和谁互怼、谁是技术向、谁常开某个梗)\n"
        "- 最近形成的新梗, 但必须有复现价值, 不是单句笑话\n\n"
        "**不要挑的**:\n"
        "- 一次性闲聊 / 笑话(没复现价值)\n"
        "- 个人隐私 / 敏感信息(隐私优先于记忆)\n"
        "- 笨猫自己的人设(已经写在 character_card 里了, 不需要再记)\n"
        "- 完全 trivial 的事(早安/吃饭这种)\n\n"
        f"当前 scope 标签: {scope_label or '<unknown>'}\n\n"
        "对话历史:\n"
        "------\n"
        f"{history_excerpt[-6000:]}\n"
        "------\n\n"
        "**严格输出 JSON**(不要解释、不要 markdown、不要多余字段):\n"
        "{\"entries\":[{\"keys\":[\"关键词1\",\"关键词2\"],\"content\":\"该记的事(口语化,30-100 字)\"}]}\n\n"
        "- keys 数组 1-3 个, 优先选群友原话里的精确短词、别名、梗名或人物昵称；避免『游戏/聊天/今天』这种泛词\n"
        "- 每个 key 2-10 个字左右, 可包含英文缩写；同一条里放 1 个精确梗名 + 1 个常见别称最好\n"
        "- content 30-100 字, 口语化, 描述清楚『这个群有这么个事』以及笨猫以后该怎么接住\n"
        "- 如果是人物记忆, content 必须带 QQ/昵称和证据性质, 不要脑补隐私\n"
        "- 没值得挑的就 entries: []\n"
        "- 最多 3 条, 宁缺毋滥"
    )
    try:
        reply = await chat_completion_summary(
            config,
            [
                {"role": "system", "content": "你是一个对话总结器, 严格按 JSON 输出。"},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception:  # noqa: BLE001
        return []
    parsed = _json_object(reply)
    if not isinstance(parsed, dict):
        return []
    raw_entries = parsed.get("entries")
    if not isinstance(raw_entries, list):
        return []
    out: list[dict] = []
    for item in raw_entries[:3]:  # 最多 3 条
        if not isinstance(item, dict):
            continue
        raw_keys = item.get("keys")
        content = item.get("content")
        if not isinstance(raw_keys, list) or not isinstance(content, str):
            continue
        keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        content = content.strip()
        if not keys or not content:
            continue
        out.append({"keys": keys[:3], "content": content[:300]})  # content 硬上限 300 字
    return out


async def should_request_reply_split(config: Config, user_content: str, *, min_chars: int) -> bool:
    if not config.catty_filter_enabled:
        return False
    if not (config.catty_filter_api_key or config.catty_audit_ai_api_key or config.catty_openai_api_key):
        return False

    prompt = (
        "你是QQ回复分段判断器，只判断本轮回复是否值得允许主AI按语义拆成多条消息。"
        f"只有当用户问题大概率需要不少于约{min_chars}个中文字符、且拆分会更像自然聊天时，返回true；"
        "短答、斗嘴、普通闲聊、简单问候、只需一句话回答时返回false。"
        "只输出JSON：{\"split\":true|false}，不要解释。"
    )
    reply = await _filter_completion(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": user_content.strip()}],
    )
    return _json_decision(reply, "split")


async def assess_user_anger(config: Config, message_text: str, *, current_anger: int, has_image: bool = False) -> dict[str, Any]:
    if not config.catty_filter_enabled or not config.catty_filter_anger_enabled:
        return {"useless": False, "anger_delta": -5, "reason": ""}
    if not (config.catty_filter_api_key or config.catty_audit_ai_api_key or config.catty_openai_api_key):
        return {"useless": False, "anger_delta": 0, "reason": ""}

    content = message_text.strip() or ("[图片]" if has_image else "")
    prompt = (
        "你是QQ群机器人耐心条评估器，判断用户这条发给机器人的消息是否无用、复读、刷屏、纠缠或故意消耗机器人。"
        "结合当前怒气值给出本条对怒气的增减：有实质问题/正常交流应降低怒气；复读、无意义短句、反复挑衅、只发无关表情应增加怒气。"
        "anger_delta 范围 -20 到 40；useless 为 true 表示这条确实无用或复读。"
        "只输出JSON：{\"useless\":true|false,\"anger_delta\":整数,\"reason\":\"<=30字\"}，不要解释。"
    )
    user_content = f"当前怒气值：{max(min(current_anger, 100), 0)}/100\n消息：{content}\n是否有图片：{'是' if has_image else '否'}"
    reply = await _filter_completion(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}],
        fallback_max_tokens=96,
    )
    parsed = _json_object(reply) or {}
    useless = parsed.get("useless")
    if isinstance(useless, str):
        useless_value = useless.strip().lower() in {"true", "yes", "1", "无用", "复读"}
    else:
        useless_value = bool(useless)
    try:
        anger_delta = int(parsed.get("anger_delta", 0))
    except (TypeError, ValueError):
        anger_delta = 0
    reason = str(parsed.get("reason") or "").strip()
    return {
        "useless": useless_value,
        "anger_delta": max(min(anger_delta, 40), -20),
        "reason": reason,
    }


async def describe_images(config: Config, image_urls: list[str], context: str) -> str:
    if not image_urls:
        return ""

    base_url = config.catty_vision_base_url or config.catty_openai_base_url
    api_key = config.catty_vision_api_key or config.catty_openai_api_key
    model = config.catty_vision_model or config.catty_openai_model
    prompt = config.catty_vision_prompt.strip() or "请识别图片内容，提取和聊天回复相关的信息。"
    text = f"{prompt}\n\n聊天上下文：\n{context}".strip()
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]

    for url in image_urls:
        prepared_url = await _vision_image_url(config, url)
        content.append({"type": "image_url", "image_url": {"url": prepared_url}})

    # 主人 2026-07-06 openai-claude-95: vision 线判定为 claude 时走 native (一次性调用
    # 不标 cache breakpoints), 失败落回 compat。图片块由 _normalize_message_content 转换。
    if _route_native(config, "vision", base_url, model):
        try:
            from .anthropic_native_client import post_messages_native
            _native_payload: dict[str, Any] = {
                "messages": [{"role": "user", "content": content}],
                "max_tokens": config.catty_vision_max_tokens or 4096,
            }
            _native_diagnostics_token, _ = _bind_native_request_diagnostics(
                base_url=base_url,
                model=model,
                payload=_native_payload,
                request_route="vision",
            )
            try:
                data = await post_messages_native(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=_native_payload["messages"],
                    max_tokens=config.catty_vision_max_tokens or 4096,
                    temperature=config.catty_vision_temperature,
                    timeout=float(config.catty_vision_request_timeout or config.catty_request_timeout),
                    metadata_user_id=None,
                    tools=None,
                    line="vision",
                    enable_cache_breakpoints=False,
                    extra_betas=list(getattr(config, "catty_native_extra_betas", []) or []) or None,
                )
                return _extract_content(data)
            finally:
                _reset_native_request_diagnostics(_native_diagnostics_token)
        except Exception as native_exc:  # noqa: BLE001
            _logger.warning(
                "vision native /v1/messages failed (%s); trying OpenAI-compat",
                native_exc.__class__.__name__,
            )
    return await _post_chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": content}],
        timeout=config.catty_vision_request_timeout or config.catty_request_timeout,
        proxy=config.catty_http_proxy,
        temperature=config.catty_vision_temperature,
        max_tokens=config.catty_vision_max_tokens,
        extra_headers=config.catty_vision_extra_headers or config.catty_openai_extra_headers,
        extra_body=config.catty_vision_extra_body,
        request_route="vision",
    )


async def analyze_images_for_reply(config: Config, image_urls: list[str], context: str) -> dict[str, Any]:
    if not image_urls:
        return {}

    prompt = (
        "请识别图片内容并只输出 JSON。字段："
        "summary 字符串，描述图片/表情内容；"
        "interest 0-100 整数，表示这张图作为聊天表情或回复素材的有趣程度；"
        "emotion_tags 字符串数组，提取情绪/语气标签，如 开心、震惊、无语、害羞、嘲笑、疑惑；"
        "expression 字符串，说明它适合表达什么；"
        "emoji_query 字符串，给主 AI 匹配本地表情库用；"
        "save_as_emoji 布尔值，只有非常适合作为表情复用时为 true。"
        "不要输出 Markdown，不要解释。"
    )
    text = f"{prompt}\n\n聊天上下文：\n{context}".strip()
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for url in image_urls:
        prepared_url = await _vision_image_url(config, url)
        content.append({"type": "image_url", "image_url": {"url": prepared_url}})

    _v_base_url = config.catty_vision_base_url or config.catty_openai_base_url
    _v_api_key = config.catty_vision_api_key or config.catty_openai_api_key
    _v_model = config.catty_vision_model or config.catty_openai_model
    # 主人 2026-07-06 openai-claude-95: vision 线 native 旁路 (同 describe_images)。
    if _route_native(config, "vision", _v_base_url, _v_model):
        try:
            from .anthropic_native_client import post_messages_native
            _native_payload: dict[str, Any] = {
                "messages": [{"role": "user", "content": content}],
                "max_tokens": config.catty_vision_max_tokens or 4096,
            }
            _native_diagnostics_token, _ = _bind_native_request_diagnostics(
                base_url=_v_base_url,
                model=_v_model,
                payload=_native_payload,
                request_route="vision",
            )
            try:
                data = await post_messages_native(
                    base_url=_v_base_url,
                    api_key=_v_api_key,
                    model=_v_model,
                    messages=_native_payload["messages"],
                    max_tokens=config.catty_vision_max_tokens or 4096,
                    temperature=config.catty_vision_temperature,
                    timeout=float(config.catty_vision_request_timeout or config.catty_request_timeout),
                    metadata_user_id=None,
                    tools=None,
                    line="vision",
                    enable_cache_breakpoints=False,
                    extra_betas=list(getattr(config, "catty_native_extra_betas", []) or []) or None,
                )
                return _image_analysis_from_reply(_extract_content(data))
            finally:
                _reset_native_request_diagnostics(_native_diagnostics_token)
        except Exception as native_exc:  # noqa: BLE001
            _logger.warning(
                "vision native /v1/messages failed (%s); trying OpenAI-compat",
                native_exc.__class__.__name__,
            )
    reply = await _post_chat_completion(
        base_url=_v_base_url,
        api_key=_v_api_key,
        model=_v_model,
        messages=[{"role": "user", "content": content}],
        timeout=config.catty_vision_request_timeout or config.catty_request_timeout,
        proxy=config.catty_http_proxy,
        temperature=config.catty_vision_temperature,
        max_tokens=config.catty_vision_max_tokens,
        extra_headers=config.catty_vision_extra_headers or config.catty_openai_extra_headers,
        extra_body=config.catty_vision_extra_body,
        request_route="vision",
    )
    return _image_analysis_from_reply(reply)
