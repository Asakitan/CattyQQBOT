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
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

_MAX_COMPLETED = 50


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
_LATEST_USAGE_BY_SCOPE: dict[str, dict[str, int]] = {}


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


def push_cache_stats(scope: str, usage: dict[str, Any], *, model: str = "") -> None:
    """把 cache 命中率 + context window 占用推到 dashboard.

    优先识别 DeepSeek 风格 (prompt_cache_hit_tokens / prompt_cache_miss_tokens),
    否则走 Anthropic 风格 (cache_read_input_tokens / cache_creation_input_tokens).

    主人 2026-05-28: 加 DeepSeek 字段识别 — DeepSeek 无 cache_create 概念
    (服务端自动 KV cache, 不分 read/create), 计费: hit 0.1x, miss 1.0x.
    Anthropic 计费: read 0.1x, create 2.0x (1h TTL), input 1.0x.
    """
    # === DeepSeek 风格 (优先识别) ===
    ds_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    ds_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    if ds_hit or ds_miss:
        cache_read = ds_hit
        cache_create = 0  # DeepSeek 无 cache_create
        input_tokens = ds_miss
        output_tokens = int(usage.get("completion_tokens") or 0)
        # S3.8: 暂存供 settle_after_response 用
        if scope:
            _LATEST_USAGE_BY_SCOPE[scope] = {
                "prompt_tokens": cache_read + input_tokens,
                "completion_tokens": output_tokens,
                "ts": int(time.time()),
            }
        total_context = cache_read + input_tokens
        hit = (cache_read / total_context) if total_context > 0 else 0.0
        # DeepSeek 计费系数: hit 0.1x, miss 1.0x
        billed_input_equiv = int(cache_read * 0.1 + input_tokens * 1.0)
        saved_pct = (1 - billed_input_equiv / total_context) * 100 if total_context > 0 else 0.0
    else:
        # === Anthropic 风格 (原逻辑) ===
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_context = cache_read + cache_create + input_tokens
        # S3.8: 暂存供 settle_after_response 用 (Anthropic native 路径)
        if scope:
            _LATEST_USAGE_BY_SCOPE[scope] = {
                "prompt_tokens": cache_read + cache_create + input_tokens,
                "completion_tokens": output_tokens,
                "ts": int(time.time()),
            }
        hit = (cache_read / total_context) if total_context > 0 else 0.0
        # Anthropic 定价系数 (1h TTL): cache_read 0.1x / cache_create 2.0x / input 1.0x
        billed_input_equiv = int(cache_read * 0.1 + cache_create * 2.0 + input_tokens * 1.0)
        saved_pct = (1 - billed_input_equiv / total_context) * 100 if total_context > 0 else 0.0
    _broadcast({
        "type": "cache_stats",
        "scope": scope,
        "model": model or "",
        "cache_read": cache_read,
        "cache_create": cache_create,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "hit_ratio": hit,
        "total_context": total_context,
        "billed_input_equiv": billed_input_equiv,
        "saved_pct": saved_pct,
        "ts": time.time(),
    })


def get_state_snapshot() -> dict[str, Any]:
    """dashboard GET /state 用 — 返回当前 active stream + 最近 N 完成的 streams."""
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
    "push_credit_event",
    "pop_latest_usage",
    "get_state_snapshot",
    "get_credit_snapshot",
    "subscribe",
    "unsubscribe",
]
