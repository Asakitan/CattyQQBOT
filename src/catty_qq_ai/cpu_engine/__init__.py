"""CPU 主回复引擎 (BotLibre 风格).

普通闲聊命中 L1-L4 CPU 层零成本秒回; 强互动/NSFW/低信心透传到 L5 主 AI.
模块入口: router.cpu_route().

模块布局:
    normalize.py        - 文本归一化 (去@/表情占位/繁简/全半角/jieba 切词)
    keyword_match.py    - L1 精确匹配 (子串+正则+冷却+confidence)
    semantic_route.py   - L2 Semantic Router 包装 (bge embed 找 route)
    corpus_txtai.py     - L3 大语料库向量召回 (txtai Top-K)
    script_ctx.py       - Script 变量上下文 (affection/nickname/topic/...)
    router.py           - 顶层六判定点协调器
    native/             - Cython 热点加速 (失败自动 fallback)
"""

from __future__ import annotations

__all__ = [
    "router",
    "normalize",
    "keyword_match",
    "semantic_route",
    "script_ctx",
]
