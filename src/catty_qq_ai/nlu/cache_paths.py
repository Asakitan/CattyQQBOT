"""NLU 缓存路径管理.

所有 prototype 向量 (.npy) + 元数据 (meta.json) 缓存在
`src/catty_qq_ai/data/nlu_cache/` 下, 自动创建.

prototype 源文本变化时通过 hash 比对自动 invalidate (在 prototypes.py 里实现).
"""
from __future__ import annotations

import os
from pathlib import Path


# 包根目录 (.../catty_qq_ai/)
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_cache_dir(configured: str | None = None) -> Path:
    """解析 nlu cache dir. 优先用 config 传入的, 否则用包内默认.

    configured 是相对路径时, 相对于当前工作目录 (bot cwd) 解析;
    绝对路径直接用. 空时回退到 `<package>/data/nlu_cache/`.
    """
    if configured:
        p = Path(configured).expanduser()
        if not p.is_absolute():
            p = Path(os.getcwd()) / p
    else:
        p = _PACKAGE_ROOT / "data" / "nlu_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


_cache_dir_override: str | None = None


def set_nlu_cache_dir(configured: str | None) -> None:
    """Bot 启动时, 由 config.catty_nlu_cache_dir 设一次. 后续 getter 用这个."""
    global _cache_dir_override
    _cache_dir_override = configured or None


def get_nlu_cache_dir() -> Path:
    return _resolve_cache_dir(_cache_dir_override)


def get_topic_prototypes_path() -> Path:
    return get_nlu_cache_dir() / "topic_prototypes.npy"


def get_emotion_prototypes_path() -> Path:
    return get_nlu_cache_dir() / "emotion_prototypes.npy"


def get_trend_prototypes_path() -> Path:
    return get_nlu_cache_dir() / "trend_prototypes.npy"


def get_prototypes_meta_path() -> Path:
    return get_nlu_cache_dir() / "prototypes_meta.json"


__all__ = [
    "set_nlu_cache_dir",
    "get_nlu_cache_dir",
    "get_topic_prototypes_path",
    "get_emotion_prototypes_path",
    "get_trend_prototypes_path",
    "get_prototypes_meta_path",
]
