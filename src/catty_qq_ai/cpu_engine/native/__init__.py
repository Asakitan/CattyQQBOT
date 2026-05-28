"""Cython native 热点模块.

每个 .pyx 在 Python 代码里都对应一个 try/except 降级路径:
    try:
        from .normalize_native import normalize_text
    except ImportError:
        from ..normalize_pure import normalize_text

编译: python setup.py build_ext --inplace 或 scripts/build_native.ps1
"""
