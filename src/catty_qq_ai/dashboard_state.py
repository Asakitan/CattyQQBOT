"""Catty Dashboard 共享状态 — 流式接收事件 + cache stats + SSE 推送源.

主人 2026-05-28 C5/C6: 让 anthropic_native_client streaming + FastAPI dashboard
通过 module-level state 共享数据. 不要循环 import (dashboard.py 用本模块, 本模块
不依赖 dashboard.py).

设计:
- _ACTIVE_STREAMS: 当前正在接收的 stream {stream_id: StreamState}
- _COMPLETED_STREAMS: 最近完成的 stream 历史 (LRU, 最多 50 条)
- _SSE_SUBSCRIBERS: dashboard 前端注册的 SSE 订阅者 (asyncio.Queue list)
- push_event() 同步 push 到所有订阅者
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable

_MAX_COMPLETED = 50
_MAX_SCOPE_STATES = 100
_MAX_SCOPE_EVENTS = 12
_SCOPE_ROLLING_MAXLEN = 20

_CACHE_BILLING_PROFILES: dict[str, dict[str, float]] = {
    "deepseek": {
        "cache_read_multiplier": 0.02,
        "cache_create_multiplier": 0.0,
        "input_multiplier": 1.0,
    },
    "openai": {
        "cache_read_multiplier": 0.1,
        "cache_create_multiplier": 0.0,
        "input_multiplier": 1.0,
    },
    "anthropic": {
        "cache_read_multiplier": 0.1,
        "cache_create_multiplier": 2.0,
        "input_multiplier": 1.0,
    },
}


@dataclass
class StreamState:
    """单次 LLM 调用的 streaming 状态."""

    stream_id: str
    started_at: float
    model: str = ""
    text_buffer: str = ""  # 流式累积的 text content (display 用)
    block_count: int = 0
    last_event_type: str = ""
    completed: bool = False
    final_usage: dict[str, Any] | None = None
    ended_at: float | None = None


@dataclass
class ScopeState:
    """Bounded cache and session-window state for one conversation scope."""

    scope: str
    model: str = ""
    latest_cache_stats: dict[str, Any] = field(default_factory=dict)
    latest_auxiliary_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_context: dict[str, Any] = field(default_factory=dict)
    cache_events: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_MAX_SCOPE_EVENTS),
    )
    rolling: deque[tuple[int, int, int]] = field(
        default_factory=lambda: deque(maxlen=_SCOPE_ROLLING_MAXLEN),
    )
    request_ledger: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    updated_at: float = 0.0


_ACTIVE_STREAMS: dict[str, StreamState] = {}
_COMPLETED_STREAMS: deque[StreamState] = deque(maxlen=_MAX_COMPLETED)
_SSE_SUBSCRIBERS: list[asyncio.Queue] = []

# 主人 2026-05-28 plan-cpu-alicebot-nlu-ai S3.7:
# per-user 积分 ledger (内存最近 N 条 + SSE 推送). 持久化的余额在 affection.json,
# 这里只记 charge/settle/passive_recover/signin 事件用于 dashboard 可视化.
_CREDIT_LEDGER: deque[dict[str, Any]] = deque(maxlen=200)
_USER_LATEST_CREDIT: dict[str, dict[str, Any]] = {}

# S3.8: 按 scope 暂存最新 usage, 供 handle_chat 调 settle_after_response 用.
# push_cache_stats 时写, handle_chat 拿到 reply 后读.
_LATEST_USAGE_BY_SCOPE: dict[str, dict[str, Any]] = {}

# Latest state per conversation. OrderedDict gives the scope cache an LRU-style
# bound while preserving a stable snapshot order for the dashboard.
_SCOPE_STATES: OrderedDict[str, ScopeState] = OrderedDict()


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _nonnegative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _ensure_scope_state(scope: str, *, model: str = "") -> ScopeState:
    scope_key = str(scope or "").strip() or "unknown"
    state = _SCOPE_STATES.get(scope_key)
    if state is None:
        if len(_SCOPE_STATES) >= _MAX_SCOPE_STATES:
            _SCOPE_STATES.popitem(last=False)
        state = ScopeState(scope=scope_key)
        _SCOPE_STATES[scope_key] = state
    else:
        _SCOPE_STATES.move_to_end(scope_key)
    if model:
        state.model = str(model)
    return state


def _coerce_session_context(
    state: ScopeState,
    stats: Mapping[str, Any] | None,
    *,
    model: str = "",
    request_kind: str = "",
    logical_turn_id: str = "",
    persist: bool = True,
) -> dict[str, Any]:
    source = dict(stats) if isinstance(stats, Mapping) else {}
    session = dict(state.session_context) if persist else {}
    aliases = {
        "watermark_tokens": "history_high_watermark_tokens",
        "target_tokens": "target_context_tokens",
        "model_limit_tokens": "model_context_tokens",
        "session_trim_epoch": "trim_epoch",
        "session_trim_count": "trim_count",
    }
    for alias, canonical in aliases.items():
        if canonical not in source and alias in source:
            source[canonical] = source[alias]

    for key in (
        "retained_input_tokens",
        "history_tokens",
        "history_turns",
        "history_messages",
        "headroom_tokens",
        "history_high_watermark_tokens",
        "trim_epoch",
        "trim_count",
        "request_trimmed_messages",
        "target_context_tokens",
        "model_context_tokens",
        "max_output_tokens",
        "allowed_history_tokens",
        "non_history_input_tokens",
        "local_input_tokens",
        "unavoidable_current_turn_tokens",
        "request_seq",
    ):
        if source.get(key) is not None:
            session[key] = _nonnegative_int(source[key])

    for key in ("session_context_enabled", "request_emergency_trimmed"):
        if source.get(key) is not None:
            session[key] = bool(source[key])

    next_request_kind = request_kind or str(source.get("request_kind") or "")
    if next_request_kind:
        session["request_kind"] = next_request_kind
    next_logical_turn_id = logical_turn_id or str(source.get("logical_turn_id") or "")
    if next_logical_turn_id:
        session["logical_turn_id"] = next_logical_turn_id
    if model:
        session["model"] = str(model)
    elif state.model:
        session["model"] = state.model
    if persist:
        state.session_context = session
    return dict(session)


def _request_class(
    scope: str,
    diagnostics: Mapping[str, Any] | None,
) -> str:
    if isinstance(diagnostics, Mapping):
        explicit = str(diagnostics.get("request_class") or "").strip().lower()
        if explicit in {"chat", "auxiliary"}:
            return explicit
        route = str(
            diagnostics.get("request_route")
            or diagnostics.get("route")
            or ""
        ).strip().lower()
        if route in {
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
    return "auxiliary" if str(scope or "").startswith("summary:") else "chat"


def _resolve_billing_profile(
    *,
    provider: str,
    model: str,
    diagnostics: Mapping[str, Any] | None,
    billing_profile: str | Mapping[str, Any] | None,
    cache_hit_billing_multiplier: float | None,
) -> tuple[str, dict[str, float]]:
    supplied: str | Mapping[str, Any] | None = billing_profile
    if supplied is None and isinstance(diagnostics, Mapping):
        supplied = diagnostics.get("billing_profile")

    default_name = "deepseek" if "deepseek" in (model or "").lower() else provider
    profile_name = default_name
    overrides: Mapping[str, Any] = {}
    if isinstance(supplied, Mapping):
        overrides = supplied
        profile_name = str(supplied.get("name") or default_name)
    elif isinstance(supplied, str) and supplied.strip():
        profile_name = supplied.strip().lower()

    default_profile = _CACHE_BILLING_PROFILES.get(
        default_name,
        _CACHE_BILLING_PROFILES["openai"],
    )
    profile = dict(_CACHE_BILLING_PROFILES.get(profile_name, default_profile))
    for key, aliases in {
        "cache_read_multiplier": ("cache_read_multiplier", "cache_hit_multiplier", "cache_hit_billing_multiplier"),
        "cache_create_multiplier": ("cache_create_multiplier",),
        "input_multiplier": ("input_multiplier",),
    }.items():
        for alias in aliases:
            if alias in overrides:
                profile[key] = _nonnegative_float(overrides[alias], profile[key])
                break
            if isinstance(diagnostics, Mapping) and alias in diagnostics:
                profile[key] = _nonnegative_float(diagnostics[alias], profile[key])
                break
    if cache_hit_billing_multiplier is not None:
        profile["cache_read_multiplier"] = _nonnegative_float(
            cache_hit_billing_multiplier,
            profile["cache_read_multiplier"],
        )
    return profile_name, profile


def _normalize_cache_usage(
    usage: Mapping[str, Any],
    *,
    model: str,
    diagnostics: Mapping[str, Any] | None,
    billing_profile: str | Mapping[str, Any] | None,
    cache_hit_billing_multiplier: float | None,
) -> dict[str, Any]:
    has_deepseek_usage = (
        "prompt_cache_hit_tokens" in usage
        or "prompt_cache_miss_tokens" in usage
    )
    has_anthropic_usage = any(
        key in usage
        for key in (
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "input_tokens",
            "output_tokens",
        )
    )
    if has_deepseek_usage:
        provider = "deepseek"
        cache_read = _nonnegative_int(usage.get("prompt_cache_hit_tokens"))
        cache_create = 0
        input_tokens = _nonnegative_int(usage.get("prompt_cache_miss_tokens"))
        output_tokens = _nonnegative_int(usage.get("completion_tokens"))
    elif has_anthropic_usage:
        provider = "anthropic"
        cache_read = _nonnegative_int(usage.get("cache_read_input_tokens"))
        cache_create = _nonnegative_int(usage.get("cache_creation_input_tokens"))
        input_tokens = _nonnegative_int(usage.get("input_tokens"))
        output_tokens = _nonnegative_int(usage.get("output_tokens"))
    else:
        provider = "openai"
        prompt_details = usage.get("prompt_tokens_details")
        cache_read = _nonnegative_int(
            prompt_details.get("cached_tokens") if isinstance(prompt_details, Mapping) else 0,
        )
        cache_create = 0
        input_tokens = max(_nonnegative_int(usage.get("prompt_tokens")) - cache_read, 0)
        output_tokens = _nonnegative_int(usage.get("completion_tokens"))

    input_total = cache_read + cache_create + input_tokens
    unavoidable_current_turn_tokens = _nonnegative_int(
        diagnostics.get("unavoidable_current_turn_tokens")
        if isinstance(diagnostics, Mapping)
        else 0,
    )
    eligible_tokens = max(
        input_total - min(unavoidable_current_turn_tokens, input_total),
        0,
    )
    try:
        from .cache_metrics import actual_hit_rate, normalized_cache_kpi

        actual = actual_hit_rate(cache_read, input_tokens, cache_create)
        normalized = normalized_cache_kpi(
            cache_read,
            input_tokens,
            cache_create,
            unavoidable_current_turn_tokens=unavoidable_current_turn_tokens,
        )
    except Exception:  # noqa: BLE001
        actual = (cache_read / input_total) if input_total > 0 else 0.0
        normalized = min(cache_read / eligible_tokens, 1.0) if eligible_tokens > 0 else None

    profile_name, profile = _resolve_billing_profile(
        provider=provider,
        model=model,
        diagnostics=diagnostics,
        billing_profile=billing_profile,
        cache_hit_billing_multiplier=cache_hit_billing_multiplier,
    )
    billed_input_equiv = (
        cache_read * profile["cache_read_multiplier"]
        + cache_create * profile["cache_create_multiplier"]
        + input_tokens * profile["input_multiplier"]
    )
    saved_pct = (
        (1 - billed_input_equiv / input_total) * 100
        if input_total > 0
        else 0.0
    )
    return {
        "provider": provider,
        "cache_read": cache_read,
        "cache_create": cache_create,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_total": input_total,
        "total_context": input_total,
        "unavoidable_current_turn_tokens": min(unavoidable_current_turn_tokens, input_total),
        "eligible_tokens": eligible_tokens,
        "actual_hit_rate": actual,
        "normalized_cache_kpi": normalized,
        "hit_ratio": actual,
        "billing_profile": profile_name,
        "cache_read_billing_multiplier": profile["cache_read_multiplier"],
        "cache_create_billing_multiplier": profile["cache_create_multiplier"],
        "input_billing_multiplier": profile["input_multiplier"],
        "billed_input_equiv": billed_input_equiv,
        "saved_pct": saved_pct,
    }


def _rolling_snapshot(state: ScopeState) -> dict[str, Any]:
    try:
        from .cache_metrics import rolling_cache_efficiency

        actual, normalized, roll_n = rolling_cache_efficiency(state.rolling)
    except Exception:  # noqa: BLE001
        hit_total = sum(hit for hit, _, _ in state.rolling)
        input_total = sum(total for _, total, _ in state.rolling)
        eligible_total = sum(eligible for _, _, eligible in state.rolling)
        actual = (hit_total / input_total) if input_total > 0 else 0.0
        normalized = min(hit_total / eligible_total, 1.0) if eligible_total > 0 else None
        roll_n = len(state.rolling)
    return {
        "rolling_n": roll_n,
        "rolling_actual_hit_rate": actual,
        "rolling_normalized_cache_kpi": normalized,
    }


def _scope_state_snapshot(state: ScopeState) -> dict[str, Any]:
    return {
        "scope": state.scope,
        "model": state.model,
        "latest_cache_stats": deepcopy(state.latest_cache_stats),
        "latest_auxiliary_stats": deepcopy(state.latest_auxiliary_stats),
        "session_context": deepcopy(state.session_context),
        "cache_rolling": _rolling_snapshot(state),
        "recent_cache_events": deepcopy(list(state.cache_events)),
        "request_ledger": deepcopy(dict(state.request_ledger)),
        "updated_at": state.updated_at,
    }


def start_stream(model: str = "") -> str:
    """创建一个新 stream 记录, 返回 stream_id. anthropic_native_client.post_messages_native
    在 streaming 入口调用.
    """
    sid = uuid.uuid4().hex[:12]
    state = StreamState(stream_id=sid, started_at=time.time(), model=model)
    _ACTIVE_STREAMS[sid] = state
    _broadcast({"type": "stream_start", "stream_id": sid, "model": model, "ts": state.started_at})
    return sid


def push_event(stream_id: str, event: Any) -> None:
    """anthropic_native_client 每个 stream event 调用一次. event 是 anthropic SDK 的
    RawMessageStartEvent / RawContentBlockDeltaEvent 等. 提取 text delta + 类型推到 SSE.

    主人 2026-05-28: 加 dict 形态支持 — OpenAI compat path (DeepSeek) 模拟流式时直接
    传 {"delta_text": "...", "event_type": "..."} 不需要 SDK event 对象.
    """
    state = _ACTIVE_STREAMS.get(stream_id)
    if state is None:
        return
    # === dict 形态 (OpenAI compat / DeepSeek 模拟流式) ===
    if isinstance(event, dict):
        event_type = event.get("event_type", "dict_delta")
        delta_text = event.get("delta_text", "") or ""
    else:
        # === Anthropic SDK event 对象 ===
        event_type = type(event).__name__
        delta_text = ""
        delta = getattr(event, "delta", None)
        if delta is not None:
            delta_text = getattr(delta, "text", "") or ""
    state.last_event_type = event_type
    if delta_text:
        state.text_buffer += delta_text
    # broadcast 简化: 只推 text delta + event_type, 避免序列化复杂 SDK 对象
    payload = {
        "type": "stream_delta",
        "stream_id": stream_id,
        "event_type": event_type,
        "delta_text": delta_text,
        "text_len": len(state.text_buffer),
        "ts": time.time(),
    }
    _broadcast(payload)


def end_stream(stream_id: str, final_usage: dict[str, Any] | None = None) -> None:
    """stream 结束时调. final_usage 来自 stream.get_final_message().usage (可选).
    把 stream state 从 active 移到 completed, broadcast end 事件.
    """
    state = _ACTIVE_STREAMS.pop(stream_id, None)
    if state is None:
        return
    state.completed = True
    state.ended_at = time.time()
    state.final_usage = final_usage
    _COMPLETED_STREAMS.append(state)
    _broadcast({
        "type": "stream_end",
        "stream_id": stream_id,
        "ts": state.ended_at,
        "duration_s": (state.ended_at - state.started_at) if state.started_at else 0,
        "text_len": len(state.text_buffer),
        "usage": final_usage,
    })


def push_cache_stats(
    scope: str,
    usage: dict[str, Any],
    *,
    model: str = "",
    diagnostics: Any = None,
    billing_profile: str | Mapping[str, Any] | None = None,
    cache_hit_billing_multiplier: float | None = None,
) -> None:
    """Normalize one provider usage event and retain it under its conversation scope.

    ``billing_profile`` may be a known profile name or a mapping with per-event
    ``cache_read_multiplier`` / ``cache_create_multiplier`` / ``input_multiplier``
    overrides. The legacy SSE fields remain present for existing listeners.
    """
    raw_usage: Mapping[str, Any] = usage if isinstance(usage, Mapping) else {}
    diagnostic_mapping = diagnostics if isinstance(diagnostics, Mapping) else None
    normalized = _normalize_cache_usage(
        raw_usage,
        model=model,
        diagnostics=diagnostic_mapping,
        billing_profile=billing_profile,
        cache_hit_billing_multiplier=cache_hit_billing_multiplier,
    )
    scope_state = _ensure_scope_state(scope)
    request_class = _request_class(scope_state.scope, diagnostic_mapping)
    if request_class == "chat" and model:
        scope_state.model = str(model)
    session_context = _coerce_session_context(
        scope_state,
        diagnostic_mapping,
        model=model,
        persist=request_class == "chat",
    )
    input_total = int(normalized["input_total"])
    if input_total > 0 and request_class == "chat":
        scope_state.rolling.append((
            int(normalized["cache_read"]),
            input_total,
            int(normalized["eligible_tokens"]),
        ))
    rolling = _rolling_snapshot(scope_state)
    timestamp = time.time()
    normalized.update(rolling)
    route = str(
        diagnostic_mapping.get("request_route")
        or diagnostic_mapping.get("route")
        or ""
    ) if diagnostic_mapping is not None else ""
    logical_turn_id = str(
        diagnostic_mapping.get("logical_turn_id") or ""
    ) if diagnostic_mapping is not None else ""
    request_kind = str(
        diagnostic_mapping.get("request_kind") or ""
    ) if diagnostic_mapping is not None else ""
    request_seq = _nonnegative_int(
        diagnostic_mapping.get("request_seq")
        if diagnostic_mapping is not None else 0,
    )
    conversation_id = str(
        diagnostic_mapping.get("conversation_id") or scope_state.scope
    ) if diagnostic_mapping is not None else scope_state.scope
    request_key = "|".join((
        conversation_id,
        logical_turn_id or f"ts-{int(timestamp * 1000)}",
        request_kind or request_class,
        str(request_seq),
    ))
    normalized.update({
        "scope": scope_state.scope,
        "model": model or scope_state.model,
        "conversation_id": conversation_id,
        "logical_turn_id": logical_turn_id,
        "request_kind": request_kind,
        "request_seq": request_seq,
        "request_route": route,
        "request_class": request_class,
        "request_key": request_key,
        "diagnostics": deepcopy(diagnostic_mapping) if diagnostic_mapping is not None else None,
        "session_context": session_context,
        "ts": timestamp,
    })
    for field in (
        "hot99_eligible",
        "hot99_eligible_count",
        "hot99_rate",
        "hot99_status",
        "hot99_target",
    ):
        if diagnostic_mapping is not None and field in diagnostic_mapping:
            normalized[field] = diagnostic_mapping[field]
    if request_class == "chat":
        scope_state.latest_cache_stats = dict(normalized)
    else:
        scope_state.latest_auxiliary_stats[route or request_kind or "auxiliary"] = dict(normalized)
    scope_state.cache_events.append(dict(normalized))
    scope_state.request_ledger[request_key] = dict(normalized)
    scope_state.request_ledger.move_to_end(request_key)
    while len(scope_state.request_ledger) > _MAX_SCOPE_EVENTS:
        scope_state.request_ledger.popitem(last=False)
    if request_class == "chat":
        scope_state.updated_at = timestamp

    # S3.8 settle_after_response handoff. Input totals explicitly exclude
    # completion/output tokens for every provider normalization branch.
    if scope and request_class == "chat":
        previous = _LATEST_USAGE_BY_SCOPE.get(scope)
        if previous is not None and logical_turn_id and previous.get("logical_turn_id") == logical_turn_id:
            previous["prompt_tokens"] = int(previous.get("prompt_tokens", 0)) + input_total
            previous["completion_tokens"] = int(previous.get("completion_tokens", 0)) + int(normalized["output_tokens"])
            previous["ts"] = int(timestamp)
        else:
            _LATEST_USAGE_BY_SCOPE[scope] = {
                "prompt_tokens": input_total,
                "completion_tokens": int(normalized["output_tokens"]),
                "logical_turn_id": logical_turn_id,
                "ts": int(timestamp),
            }
        try:
            from .token_billing import add_turn_usage

            add_turn_usage(
                prompt_tokens=input_total,
                completion_tokens=int(normalized["output_tokens"]),
            )
        except Exception:  # noqa: BLE001
            pass
    _broadcast({"type": "cache_stats", **normalized})


def push_session_context_stats(
    scope: str,
    stats: Mapping[str, Any] | None = None,
    *,
    model: str = "",
    request_kind: str = "",
    logical_turn_id: str = "",
    **updates: Any,
) -> None:
    """Merge current conversation-window stats and publish a lightweight update.

    Core code can pass its existing session-context diagnostics mapping directly,
    or provide the exposed fields as keyword arguments for an incremental update.
    """
    merged_stats = dict(stats) if isinstance(stats, Mapping) else {}
    merged_stats.update(updates)
    state = _ensure_scope_state(scope, model=model)
    session_context = _coerce_session_context(
        state,
        merged_stats,
        model=model,
        request_kind=request_kind,
        logical_turn_id=logical_turn_id,
    )
    state.updated_at = time.time()
    _broadcast({
        "type": "session_context",
        "scope": state.scope,
        "model": model or state.model,
        "session_context": deepcopy(session_context),
        "ts": state.updated_at,
    })

def get_state_snapshot() -> dict[str, Any]:
    """Return stream history plus initialized cache/session state by scope."""
    return {
        "active_streams": [
            {
                "stream_id": s.stream_id,
                "model": s.model,
                "started_at": s.started_at,
                "text_preview": s.text_buffer[:125],
                "text_len": len(s.text_buffer),
                "last_event": s.last_event_type,
            }
            for s in _ACTIVE_STREAMS.values()
        ],
        "completed_recent": [
            {
                "stream_id": s.stream_id,
                "model": s.model,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "duration_s": (s.ended_at - s.started_at) if s.ended_at else 0,
                "text_len": len(s.text_buffer),
                "usage": s.final_usage,
            }
            for s in list(_COMPLETED_STREAMS)[-10:]
        ],
        "scope_state": {
            scope: _scope_state_snapshot(state)
            for scope, state in _SCOPE_STATES.items()
        },
    }

def push_credit_event(
    user_id: str,
    kind: str,
    *,
    delta: int = 0,
    balance_after: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    scope: str = "",
    reason: str = "",
) -> None:
    """记录 + 广播一条积分事件 (S3.7).

    kind: charge_base / settle_extra / settle_refund / passive_recover / signin / grant / punish
    """
    payload = {
        "type": "credit_event",
        "ts": time.time(),
        "user_id": str(user_id),
        "kind": kind,
        "delta": int(delta),
        "balance_after": int(balance_after),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "scope": scope,
        "reason": reason,
    }
    _CREDIT_LEDGER.append(payload)
    _USER_LATEST_CREDIT[str(user_id)] = payload
    _broadcast(payload)


def get_credit_snapshot(*, recent: int = 20) -> dict[str, Any]:
    """dashboard 用: 拿最近 N 条 ledger + 每用户最新余额."""
    return {
        "ledger_recent": list(_CREDIT_LEDGER)[-recent:],
        "user_latest": dict(_USER_LATEST_CREDIT),
    }


def pop_latest_usage(scope: str, *, max_age_seconds: int = 30) -> dict[str, int] | None:
    """取并删除给定 scope 的最近 usage (S3.8 settle 用).

    超过 max_age_seconds 视为过期, 返回 None (避免误用其他对话的 usage).
    """
    entry = _LATEST_USAGE_BY_SCOPE.pop(scope, None)
    if entry is None:
        return None
    if int(time.time()) - int(entry.get("ts", 0)) > max_age_seconds:
        return None
    return {
        "prompt_tokens": int(entry.get("prompt_tokens", 0)),
        "completion_tokens": int(entry.get("completion_tokens", 0)),
    }


def subscribe() -> asyncio.Queue:
    """SSE 订阅者注册. 返回 Queue, dashboard SSE handler 从中读 + yield."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _SSE_SUBSCRIBERS.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    try:
        _SSE_SUBSCRIBERS.remove(q)
    except ValueError:
        pass


def _broadcast(payload: dict[str, Any]) -> None:
    """同步把 payload 投到所有 SSE 订阅者. Queue 满了就丢弃 (避免 backpressure 卡死调用方)."""
    for q in list(_SSE_SUBSCRIBERS):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "start_stream",
    "push_event",
    "end_stream",
    "push_cache_stats",
    "push_session_context_stats",
    "push_credit_event",
    "pop_latest_usage",
    "get_state_snapshot",
    "get_credit_snapshot",
    "subscribe",
    "unsubscribe",
]
