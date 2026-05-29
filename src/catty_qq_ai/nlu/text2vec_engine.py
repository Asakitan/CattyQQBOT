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
- 文本 >256 字符截断
- async embed() 走 asyncio.to_thread, 不阻塞 event loop

Phase 6 (Commit 4) — ONNX fast path:
- config catty_text2vec_use_onnx=True 时走 optimum.onnxruntime.ORTModelForFeatureExtraction
- 实测单 embed 从 torch 50-85ms → ONNX 2-3ms (~20-30x)
- 首次加载 export ONNX ~15s, 之后 from_pretrained 命中 HF cache 内的 .onnx
- ONNX 加载/推理失败 → 自动 fallback 到 torch path (graceful)
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

import numpy as np

logger = logging.getLogger("catty.nlu.text2vec")


# torch backend
_MODEL: Any = None
_LOAD_FAILED: bool = False
_LOAD_LOCK = threading.Lock()
_EMBED_DIM: int = 768

# ONNX backend (optimum + onnxruntime)
_ONNX_MODEL: Any = None
_ONNX_TOKENIZER: Any = None
_ONNX_LOAD_FAILED: bool = False
_ONNX_LOAD_LOCK = threading.Lock()

_MAX_TEXT_LEN: int = 256


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


def _is_onnx_enabled() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "catty_text2vec_use_onnx", False)) if cfg else False


def _get_model_name() -> str:
    cfg = _config()
    name = getattr(cfg, "catty_text2vec_model_name", "") if cfg else ""
    return name or "BAAI/bge-small-zh-v1.5"


def _setup_hf_endpoint() -> None:
    """在加载 transformers 前注入 HF_ENDPOINT (大陆走 hf-mirror.com)."""
    cfg = _config()
    endpoint = (
        (getattr(cfg, "catty_nlu_hf_endpoint", "") if cfg else "") or ""
    ).strip()
    if endpoint and "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = endpoint
        logger.info("HF_ENDPOINT injected = %s", endpoint)


# ── ONNX backend ────────────────────────────────────────────────────


def _load_onnx_blocking() -> bool:
    """加载 ONNX model + tokenizer. 首次 export ~15s, 后续 ~2s.

    返回是否成功. 失败时永久标记 _ONNX_LOAD_FAILED, 调 embed 时 fallback torch.
    """
    global _ONNX_MODEL, _ONNX_TOKENIZER, _ONNX_LOAD_FAILED, _EMBED_DIM
    if _ONNX_LOAD_FAILED:
        return False
    if _ONNX_MODEL is not None and _ONNX_TOKENIZER is not None:
        return True
    with _ONNX_LOAD_LOCK:
        if _ONNX_LOAD_FAILED:
            return False
        if _ONNX_MODEL is not None and _ONNX_TOKENIZER is not None:
            return True
        try:
            _setup_hf_endpoint()
            from optimum.onnxruntime import ORTModelForFeatureExtraction  # type: ignore
            from transformers import AutoTokenizer  # type: ignore
            model_name = _get_model_name()
            logger.info("loading text2vec ONNX model: %s (first export ~15s)", model_name)
            # 主人 2026-05-30: 关 ONNX BFC arena, 让推理后 buffer 立即释放,
            # 不然 BiasGelu 等层会累积 chunk 撑爆 17GB 内存 (远端 OOM 实测)
            try:
                from onnxruntime import SessionOptions  # type: ignore
                _opts = SessionOptions()
                _opts.enable_mem_pattern = False
                _opts.enable_cpu_mem_arena = False
            except Exception:
                _opts = None
            _kwargs = {"model_name_or_path": model_name, "export": True}
            if _opts is not None:
                _kwargs["session_options"] = _opts
                _kwargs["provider_options"] = [{"arena_extend_strategy": "kSameAsRequested"}]
            _ONNX_MODEL = ORTModelForFeatureExtraction.from_pretrained(**_kwargs)
            _ONNX_TOKENIZER = AutoTokenizer.from_pretrained(model_name)
            # 探测 dim
            try:
                dummy = _encode_onnx("test")
                if dummy is not None and dummy.shape[-1] > 0:
                    _EMBED_DIM = int(dummy.shape[-1])
            except Exception:
                pass
            logger.info("text2vec ONNX ready, dim=%d", _EMBED_DIM)
            return True
        except Exception as exc:
            logger.warning("text2vec ONNX load failed (fallback torch): %s", exc)
            _ONNX_LOAD_FAILED = True
            return False


def _encode_onnx(text: str) -> np.ndarray | None:
    """ONNX 编码单条文本 → (dim,) L2-normalized ndarray."""
    if _ONNX_MODEL is None or _ONNX_TOKENIZER is None:
        return None
    try:
        inputs = _ONNX_TOKENIZER(
            text, padding=True, truncation=True, return_tensors="np",
            max_length=_MAX_TEXT_LEN,
        )
        outputs = _ONNX_MODEL(**inputs)
        last_hidden = outputs.last_hidden_state  # (1, seq, dim)
        attention_mask = inputs["attention_mask"][..., None]  # (1, seq, 1)
        summed = (last_hidden * attention_mask).sum(axis=1)
        counts = attention_mask.sum(axis=1)
        mean = summed / counts
        norm = np.linalg.norm(mean, axis=1, keepdims=True) + 1e-9
        return (mean / norm).astype(np.float32)[0]
    except Exception as exc:
        logger.debug("onnx encode failed: %s", exc)
        return None


def _encode_onnx_batch(texts: list[str]) -> np.ndarray | None:
    """ONNX 批量编码 → (N, dim) L2-normalized."""
    if _ONNX_MODEL is None or _ONNX_TOKENIZER is None:
        return None
    if not texts:
        return None
    try:
        inputs = _ONNX_TOKENIZER(
            texts, padding=True, truncation=True, return_tensors="np",
            max_length=_MAX_TEXT_LEN,
        )
        outputs = _ONNX_MODEL(**inputs)
        last_hidden = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"][..., None]
        summed = (last_hidden * attention_mask).sum(axis=1)
        counts = attention_mask.sum(axis=1)
        mean = summed / counts
        norm = np.linalg.norm(mean, axis=1, keepdims=True) + 1e-9
        return (mean / norm).astype(np.float32)
    except Exception as exc:
        logger.debug("onnx encode_batch failed: %s", exc)
        return None


# ── torch backend (fallback) ──────────────────────────────────────


def _load_blocking() -> Any | None:
    """同步加载 text2vec SentenceModel (torch). 返回模型或 None."""
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
            logger.info("loading text2vec torch model: %s (first call ~ 2-5s)", model_name)
            _MODEL = SentenceModel(model_name)
            try:
                dummy = _MODEL.encode("test", normalize_embeddings=True)
                if hasattr(dummy, "shape") and len(dummy.shape) >= 1:
                    _EMBED_DIM = int(dummy.shape[-1])
            except Exception:
                pass
            logger.info("text2vec torch ready, dim=%d", _EMBED_DIM)
            return _MODEL
        except Exception as exc:
            logger.warning("text2vec torch load failed, falling back to None: %s", exc)
            _LOAD_FAILED = True
            return None


def get_embed_dim() -> int:
    return _EMBED_DIM


def is_text2vec_available() -> bool:
    return (_MODEL is not None and not _LOAD_FAILED) or (
        _ONNX_MODEL is not None and not _ONNX_LOAD_FAILED
    )


def embed_sync(text: str | None) -> np.ndarray | None:
    """同步 embed. ONNX 优先, fallback torch.

    返回 (dim,) ndarray, 已 L2-normalize.
    失败 / 关闭 / 空 → None.
    """
    if not _is_enabled():
        return None
    if not text or not isinstance(text, str):
        return None
    text = text[:_MAX_TEXT_LEN]

    # ONNX path
    if _is_onnx_enabled():
        if _load_onnx_blocking():
            v = _encode_onnx(text)
            if v is not None:
                return v
        # ONNX 失败 → fallback torch (不再尝试 ONNX 直到下次重启)

    # torch fallback
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
    """批量 embed. ONNX 优先, fallback torch."""
    if not _is_enabled():
        return None
    if not texts:
        return None
    cleaned = [t[:_MAX_TEXT_LEN] for t in texts if t and isinstance(t, str)]
    if not cleaned:
        return None

    if _is_onnx_enabled():
        if _load_onnx_blocking():
            v = _encode_onnx_batch(cleaned)
            if v is not None:
                return v

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
    if not _is_enabled() or not text:
        return None
    return await asyncio.to_thread(embed_sync, text)


async def warmup() -> bool:
    """启动时调一次, 后台预加载. ONNX 模式预先 export."""
    if not _is_enabled():
        return False
    try:
        if _is_onnx_enabled():
            ok = await asyncio.to_thread(_load_onnx_blocking)
            if ok:
                return True
            # ONNX 失败 → 尝试 torch 兜底
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
