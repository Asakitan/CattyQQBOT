"""Setup.py - 仅用于编译 Cython 扩展.

项目元数据 / 依赖 / 入口走 pyproject.toml. 这里仅声明 ext_modules,
因为 PEP 621 pyproject.toml 不直接支持 ext_modules.

编译方式:
    python setup.py build_ext --inplace
或:
    scripts/build_native.ps1
或:
    pip install -e .[cpu-engine]  (自动触发)

热点模块:
    cpu_engine/native/normalize_native.pyx   - 文本归一化批处理
    cpu_engine/native/cosine_topk.pyx        - cosine 相似度 + Top-K 批算
    cpu_engine/native/keyword_scan.pyx       - 多关键词同时扫描 (Aho-Corasick 风格)
"""

from __future__ import annotations

import os
from pathlib import Path

from setuptools import setup

try:
    from Cython.Build import cythonize
    import numpy as np

    _HAS_CYTHON = True
except ImportError:
    _HAS_CYTHON = False
    cythonize = None
    np = None


_NATIVE_DIR = Path("src/catty_qq_ai/cpu_engine/native")


def _find_pyx_modules() -> list[str]:
    if not _NATIVE_DIR.exists():
        return []
    return [str(p) for p in _NATIVE_DIR.glob("*.pyx")]


def _build_ext_modules():
    pyx_files = _find_pyx_modules()
    if not pyx_files or not _HAS_CYTHON:
        return []
    return cythonize(
        pyx_files,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
            "initializedcheck": False,
        },
        annotate=bool(os.environ.get("CYTHON_ANNOTATE")),
    )


setup(
    ext_modules=_build_ext_modules(),
    include_dirs=[np.get_include()] if _HAS_CYTHON else [],
)
