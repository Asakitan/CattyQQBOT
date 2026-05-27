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
    """
    state = _ACTIVE_STREAMS.get(stream_id)
    if state is None:
        return
    event_type = type(event).__name__
    state.last_event_type = event_type
    # 提取 text delta (anthropic 流式 text 主要在 ContentBlockDeltaEvent.delta.text)
    delta_text = ""
    delta = getattr(event, "delta", None)
    if delta is not None:
        delta_text = getattr(delta, "text", "") or ""
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
    """anthropic_native_client._log_native_usage 调用, 把 cache 命中率 + context window 占用推到 dashboard.

    主人 2026-05-28: 加 model 参数 + 算计费 token equiv (cache_read*0.1 + cache_create*2.0 + input).
    1h TTL cache_create 倍数=2.0, 5min=1.25. dashboard 据此显示实际计费 token 节省比例.
    """
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_context = cache_read + cache_create + input_tokens  # 实际 input context 占用
    hit = (cache_read / total_context) if total_context > 0 else 0.0
    # 计费 token 等价 (Anthropic 定价系数, 假设 1h TTL):
    # - cache_read: 0.1x (90% off)
    # - cache_create: 2.0x (1h TTL write)
    # - new input: 1.0x base
    billed_input_equiv = int(cache_read * 0.1 + cache_create * 2.0 + input_tokens * 1.0)
    # 不用 cache 的话 = total_context * 1.0
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
                "text_preview": s.text_buffer[:200],
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
    "get_state_snapshot",
    "subscribe",
    "unsubscribe",
]
