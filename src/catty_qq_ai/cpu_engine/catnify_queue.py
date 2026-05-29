"""S5 catnify 并发队列 (2026-05-29).

6 核服务器跑 Qwen3-4B Q4_K_M 推理大概 1-3s, 同时跑多个会把 CPU 打爆.
用 ``asyncio.Semaphore`` 限制并发, 队列满时直接降级 raw_candidate (调用方处理).

主人决策:
- ``concurrency=2`` (留 4 核给 bot + Ollama + OS)
- ``queue_max=8`` (8 个排队 + 2 个执行 = 10 个用户消息缓冲)
- 超出 queue_max 直接 ``acquire_fast()`` 返回 False, 调用方按 fallback 路径走
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger


class CatnifyQueue:
    """进程级 catnify 并发控制. 单例, 配置变化也只生效首次加载."""

    def __init__(self, *, concurrency: int = 2, queue_max: int = 8) -> None:
        self._concurrency = max(1, int(concurrency))
        self._queue_max = max(1, int(queue_max))
        self._sem = asyncio.Semaphore(self._concurrency)
        self._waiting = 0  # 当前在 acquire 阻塞的协程数
        self._running = 0  # 当前持有 sem 的协程数
        self._overload_count = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "concurrency": self._concurrency,
            "queue_max": self._queue_max,
            "waiting": self._waiting,
            "running": self._running,
            "overload_count": self._overload_count,
        }

    def can_accept(self) -> bool:
        """快速判断当前是否还有队位. 不抢占 sem."""
        return self._waiting < self._queue_max

    @asynccontextmanager
    async def slot(self):
        """async with queue.slot(): ... 阻塞 acquire + 自动 release.

        队列满时不阻塞 — 调用方应先用 ``can_accept()`` 检查或捕获 ``CatnifyOverload``.
        """
        if not self.can_accept():
            self._overload_count += 1
            raise CatnifyOverload(
                f"queue full: waiting={self._waiting} max={self._queue_max}"
            )
        self._waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self._waiting -= 1
        self._running += 1
        try:
            yield
        finally:
            self._running -= 1
            self._sem.release()


class CatnifyOverload(RuntimeError):
    """队列满 → 调用方应走 fallback (主人决策: 用 raw_candidate)."""


_GLOBAL_QUEUE: CatnifyQueue | None = None


def get_queue(config: Any | None = None) -> CatnifyQueue:
    """惰性单例. config 可选, 首次调用时读取 catnify_concurrency / catnify_queue_max."""
    global _GLOBAL_QUEUE
    if _GLOBAL_QUEUE is None:
        if config is None:
            concurrency = 2
            queue_max = 8
        else:
            concurrency = int(getattr(config, "catty_cpu_engine_l4_catnify_concurrency", 2))
            queue_max = int(getattr(config, "catty_cpu_engine_l4_catnify_queue_max", 8))
        _GLOBAL_QUEUE = CatnifyQueue(concurrency=concurrency, queue_max=queue_max)
        logger.info(
            f"[cpu_engine.catnify_queue] init concurrency={concurrency} queue_max={queue_max}"
        )
    return _GLOBAL_QUEUE


def reset_queue_for_test() -> None:
    """测试用: 清单例."""
    global _GLOBAL_QUEUE
    _GLOBAL_QUEUE = None
