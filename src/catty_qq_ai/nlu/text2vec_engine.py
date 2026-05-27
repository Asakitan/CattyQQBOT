"""text2vec 中文 sentence embedding 包装 — lazy load + 同步/异步 embed.

主人 2026-05-28 plan: 给 daily_life topic / affection_scorer emotion /
theory_of_mind trend 提供 encoder-only 语义向量, 算 cosine 相似度.

默认模型: BAAI/bge-small-zh-v1.5 (~95MB, 512 dim). 主人 2026-05-28 phase 5
切换. 之前用 shibing624/text2vec-base-chinese (~100MB, 768 dim).
特性:
- HF 走 hf-mirror.com 镜像 (大陆环境), 加载前注入 HF_ENDPOINT
- lazy load (第一次 embed 时加载, 阻塞 2-5s)
- normalize_embeddings=True (返回单位向量, cosine 退化成内积)
- 失败 graceful fallback: embed() 返回 None, caller 走 legacy

核心契约:
- embed() 失败 → None, 不抛异常
- 文本 >256 字符截断 (text2vec-base-chinese 上限)
- async embed() 走 asyncio.to_thread, 不阻塞 event loop
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

import numpy as np

logger = logging.getLogger("catty.nlu.text2vec")


_MODEL: Any = None
_LOAD_FAILED: bool = False
_LOAD_LOCK = threading.Lock()
_EMBED_DIM: int = 768  # text2vec-base-chinese 固定 768
_MAX_TEXT_LEN: int = 256  # 模型 max position embeddings


def _config() -> Any:
    try:
        from nonebot import get_plugin_config
        from ..config import Config
        return get_plugin_config(Config)
    except Exception:
        return None


def _is_enabled() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "catty_use_text2vec", False)) if cfg else False


def _get_model_name() -> str:
    cfg = _config()
    name = getattr(cfg, "catty_text2vec_model_name", "") if cfg else ""
    return name or "BAAI/bge-small-zh-v1.5"


def _setup_hf_endpoint() -> None:
    """在加载 transformers 前注入 HF_ENDPOINT (大陆走 hf-mirror.com).

    只在 HF_ENDPOINT 未设时注入. 主人本地有其它代理时不动.
    """
    cfg = _config()
    endpoint = (
        (getattr(cfg, "catty_nlu_hf_endpoint", "") if cfg else "") or ""
    ).strip()
    if endpoint and "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = endpoint
        logger.info("HF_ENDPOINT injected = %s", endpoint)


def _load_blocking() -> Any | None:
    """同步加载 text2vec SentenceModel. 返回模型或 None."""
    global _MODEL, _LOAD_FAILED, _EMBED_DIM
    if _LOAD_FAILED:
        return None
    if _MODEL is not None:
        return _MODEL
    with _LOAD_LOCK:
        if _LOAD_FAILED:
            return None
        if _MODEL is not None:
            return _MODEL
        try:
            _setup_hf_endpoint()
            from text2vec import SentenceModel  # type: ignore
            model_name = _get_model_name()
            logger.info("loading text2vec model: %s (first call ~ 2-5s)", model_name)
            _MODEL = SentenceModel(model_name)
            # 探测 embedding dim (单条 dummy)
            try:
                dummy = _MODEL.encode("test", normalize_embeddings=True)
                if hasattr(dummy, "shape") and len(dummy.shape) >= 1:
                    _EMBED_DIM = int(dummy.shape[-1])
            except Exception:
                pass
            logger.info("text2vec ready, dim=%d", _EMBED_DIM)
            return _MODEL
        except Exception as exc:
            logger.warning("text2vec load failed, falling back to None: %s", exc)
            _LOAD_FAILED = True
            return None


def get_embed_dim() -> int:
    """返回当前 embedding 维度. 加载前/失败时返回默认 768."""
    return _EMBED_DIM


def is_text2vec_available() -> bool:
    """快速 check: 模型已加载且未失败. 不触发加载."""
    return _MODEL is not None and not _LOAD_FAILED


def embed_sync(text: str | None) -> np.ndarray | None:
    """同步 embed (主要用于已经在 to_thread 内部的 caller).

    返回 (dim,) ndarray, 已 L2-normalize (cosine = 内积).
    失败 / 关闭 / 空 → None.
    """
    if not _is_enabled():
        return None
    if not text or not isinstance(text, str):
        return None
    text = text[:_MAX_TEXT_LEN]
    model = _load_blocking()
    if model is None:
        return None
    try:
        vec = model.encode(text, normalize_embeddings=True)
        if not isinstance(vec, np.ndarray):
            vec = np.asarray(vec, dtype=np.float32)
        return vec.astype(np.float32)
    except Exception as exc:
        logger.debug("text2vec.encode failed on text len=%d: %s", len(text), exc)
        return None


def embed_sync_batch(texts: list[str]) -> np.ndarray | None:
    """批量 embed (用于 prototypes.py 一次性算所有原型).

    返回 (N, dim) ndarray, 每行 L2-normalize. 失败 → None.
    """
    if not _is_enabled():
        return None
    if not texts:
        return None
    cleaned = [t[:_MAX_TEXT_LEN] for t in texts if t and isinstance(t, str)]
    if not cleaned:
        return None
    model = _load_blocking()
    if model is None:
        return None
    try:
        vecs = model.encode(cleaned, normalize_embeddings=True)
        return np.asarray(vecs, dtype=np.float32)
    except Exception as exc:
        logger.debug("text2vec.encode_batch failed n=%d: %s", len(cleaned), exc)
        return None


async def embed(text: str | None) -> np.ndarray | None:
    """异步 embed (异步 caller 用). 内部走 asyncio.to_thread 不阻塞 event loop."""
    if not _is_enabled() or not text:
        return None
    return await asyncio.to_thread(embed_sync, text)


async def warmup() -> bool:
    """启动时调一次, 后台预加载. 返回是否成功."""
    if not _is_enabled():
        return False
    try:
        model = await asyncio.to_thread(_load_blocking)
        return model is not None
    except Exception as exc:
        logger.warning("text2vec warmup failed: %s", exc)
        return False


__all__ = [
    "embed",
    "embed_sync",
    "embed_sync_batch",
    "get_embed_dim",
    "is_text2vec_available",
    "warmup",
]
