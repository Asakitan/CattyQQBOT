"""Per-request NLU 共享 cache — 同一 user msg 跨 filter 复用 embed/topics/emotion 结果.

主人 2026-05-28: 上一轮跑分发现, 同一条 incoming.text 被 embed 3-4 次
(detect_topics + score_user_message + detect_trend centroid 各跑一次), 单条 reply
浪费 ~150ms (text2vec 单 call 50-85ms).

设计:
- 单一 user msg 处理 pipeline 内, 共用同一个 NLURequestCache 实例
- _build_messages 创建, 通过 contextvar bind 给当前 task — caller 透明享受 cache
- 不通过 contextvar 时 caller 自动 fallback 到当前行为 (向后兼容)
- 内部 dict 加 RLock — 因为 fire-and-forget 会让 score_user_message 跑在另一个 thread
- contextvar 跨 asyncio.to_thread 自动 propagate (Python 3.7+)

不是 LRU, 不是 long-lived. 单 request scope, request 结束 cache 被 GC.

用法:
    cache = NLURequestCache()
    with use_request_cache(cache):
        # 这个 with block 内, detect_topics/score/theory_of_mind 都自动用同一个 cache
        topics = detect_topics(text)  # 第一次 → embed + 缓存
        delta = score_user_message(text)  # 命中 cache, 不重 embed
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


@dataclass
class NLURequestCache:
    """Per-request scope NLU cache. 单 user msg pipeline 内共享."""

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _embed: dict[str, "Any | None"] = field(default_factory=dict, repr=False)
    _topics: dict[str, set[str]] = field(default_factory=dict, repr=False)
    # emotion key: (text, is_nsfw_context) → delta
    _emotion: dict[tuple[str, bool], int] = field(default_factory=dict, repr=False)
    # 主人 2026-05-28 v2: history context. prime_history 后填充, 用于歧义消解 /
    # detect_trend 复用 / 提升 marginal topic 置信度.
    _history_texts: list[str] = field(default_factory=list, repr=False)
    _history_topics: set[str] = field(default_factory=set, repr=False)
    _history_primed: bool = field(default=False, repr=False)
    # 简单 hit counter, 调试观察
    hits: dict[str, int] = field(default_factory=lambda: {"embed": 0, "topics": 0, "emotion": 0}, repr=True)
    misses: dict[str, int] = field(default_factory=lambda: {"embed": 0, "topics": 0, "emotion": 0}, repr=True)

    def get_or_compute_embed(
        self,
        text: str,
        compute_fn: Callable[[], "Any | None"],
    ) -> "Any | None":
        """嵌入向量缓存. compute_fn 仅在 miss 时调."""
        if not text:
            return None
        with self._lock:
            if text in self._embed:
                self.hits["embed"] += 1
                return self._embed[text]
            self.misses["embed"] += 1
            v = compute_fn()
            self._embed[text] = v
            return v

    def get_or_compute_topics(
        self,
        text: str,
        compute_fn: Callable[[], set[str]],
    ) -> set[str]:
        """detect_topics 结果缓存. 同 text 不同 caller 复用."""
        if not text:
            return set()
        with self._lock:
            if text in self._topics:
                self.hits["topics"] += 1
                return self._topics[text]
            self.misses["topics"] += 1
            v = compute_fn()
            self._topics[text] = v
            return v

    def get_or_compute_emotion(
        self,
        text: str,
        is_nsfw_context: bool,
        compute_fn: Callable[[], int],
    ) -> int:
        """情感打分缓存. key 含 NSFW context."""
        if not text or not text.strip():
            return 0
        key = (text, bool(is_nsfw_context))
        with self._lock:
            if key in self._emotion:
                self.hits["emotion"] += 1
                return self._emotion[key]
            self.misses["emotion"] += 1
            v = compute_fn()
            self._emotion[key] = v
            return v

    def prime_history(self, history_texts: list[str], top_n: int = 3) -> None:
        """一次性 batch embed 最近 N 条 history user msg + 累积 topics.

        主人 2026-05-28 v2:
        - 用于 detect_trend 等多消息 NLU 复用 (避免每条单独 embed N 次)
        - 累积 _history_topics 给 NER 消歧用 (e.g. "猫" 上下文有 pets → 动物)
        - text2vec 不可用时 silently skip (向后兼容)
        - 幂等: 重复 call 同 cache 实例只 prime 一次
        """
        if self._history_primed or not history_texts:
            return
        last_n = [t for t in history_texts[-top_n:] if t and isinstance(t, str) and t.strip()]
        if not last_n:
            with self._lock:
                self._history_primed = True
            return

        # batch embed (text2vec 不可用 → silently skip embed, 但 topics 还会跑)
        try:
            from . import text2vec_engine
            embeds = text2vec_engine.embed_sync_batch(last_n)
        except Exception:
            embeds = None

        with self._lock:
            self._history_texts = list(last_n)
            if embeds is not None and len(embeds) == len(last_n):
                for text, emb in zip(last_n, embeds):
                    if text not in self._embed:
                        self._embed[text] = emb

        # detect topics on each history msg (走 contextvar, 自动复用上面已 embed)
        try:
            from ..daily_life import detect_topics
            for text in last_n:
                topics = detect_topics(text)
                if topics:
                    with self._lock:
                        self._history_topics.update(topics)
                        if text not in self._topics:
                            self._topics[text] = topics
        except Exception:
            pass

        with self._lock:
            self._history_primed = True

    def get_history_embeds(self) -> "list[Any]":
        """返回 history msg 的 embed list (跳过 None). 顺序: 最早 → 最近."""
        with self._lock:
            out: list[Any] = []
            for text in self._history_texts:
                v = self._embed.get(text)
                if v is not None:
                    out.append(v)
            return out

    def get_history_topics(self) -> set[str]:
        """返回 history msg 累积 topic union (浅 copy)."""
        with self._lock:
            return set(self._history_topics)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "embed_size": len(self._embed),
                "topics_size": len(self._topics),
                "emotion_size": len(self._emotion),
                "history_msgs": len(self._history_texts),
                "history_topics": list(self._history_topics),
                "hits": dict(self.hits),
                "misses": dict(self.misses),
            }


# ── ContextVar API — caller 透明享受 cache ─────────────────────────────
# 让 daily_life.detect_topics / affection_scorer.score_user_message /
# catty_theory_of_mind.detect_trend 在 cache 参数为 None 时自动查 contextvar.
# 这样 catty_goals.py / catty_topic_recency.py 等不在 _build_messages 直接调
# detect_topics 的 caller 也透明享受 (不需要改 5 个 caller 都加 cache 参数).
#
# contextvar 跨 asyncio.to_thread 自动 propagate (Python 3.7+), 所以 thread
# pool 内的 score_user_message / detect_trend 也能拿到同一个 cache.

_active_cache: ContextVar["NLURequestCache | None"] = ContextVar(
    "catty_nlu_request_cache", default=None,
)


def get_current_cache() -> "NLURequestCache | None":
    """拿当前 task 绑定的 cache. 没 bind 时返回 None."""
    return _active_cache.get()


@contextmanager
def use_request_cache(cache: "NLURequestCache | None") -> Iterator["NLURequestCache | None"]:
    """绑定 cache 到当前 task scope. 退出时自动恢复.

    用法:
        cache = NLURequestCache()
        with use_request_cache(cache):
            # 此 block 内所有 NLU caller 透明用同一个 cache
            ...
    """
    token = _active_cache.set(cache)
    try:
        yield cache
    finally:
        _active_cache.reset(token)


__all__ = [
    "NLURequestCache",
    "get_current_cache",
    "use_request_cache",
]
