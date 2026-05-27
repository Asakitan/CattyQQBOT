"""笨猫·本地 NLU 增强层 (jieba + text2vec + HanLP).

主人 2026-05-28 确认架构:
- 不引入"对话引擎" (ChatScript/RiveScript/AIML), 那会替换云 LLM 让能力倒退
- 引入 NLU 增强层: 抽 intent/entity/sentiment/语义相似度, 喂进 prompt
- 现有 28 层 hard filter 当下走 regex/keyword 字典, 升级到正则+embedding+NER

Stack 边界 (主人确认):
- 允许 encoder-only embedding (BERT/MiniLM, 不生成只算向量, 不算 LLM)
- jieba (5MB, 纯 Python) + text2vec (~100MB, 中文 sentence-transformer)
  + HanLP (~80MB, NER FINE_ELECTRA_SMALL_ZH)
- HuggingFace 走 hf-mirror.com 镜像 (大陆环境)

核心契约:
- 每个 engine `embed()` / `tokenize()` / `extract_entities()` 失败时返回 None,
  caller **必须**当作"走 legacy regex 路径", 不抛异常
- lazy load: 第一次调用时阻塞加载, 之后内存常驻; 加载失败永久 fallback
- async safety: 所有重型操作走 asyncio.to_thread, 不阻塞 event loop

模块布局 (Commit 1 仅 jieba_helper 实装):
- jieba_helper.py    — Commit 1
- text2vec_engine.py — Commit 2
- hanlp_engine.py    — Commit 3
- prototypes.py      — Commit 2 (随 text2vec 上)
- cache_paths.py     — Commit 1
"""
from __future__ import annotations

from .cache_paths import (
    get_nlu_cache_dir,
    get_topic_prototypes_path,
    get_emotion_prototypes_path,
    get_trend_prototypes_path,
    get_prototypes_meta_path,
)
from .jieba_helper import tokenize, get_jieba, is_jieba_available

__all__ = [
    "tokenize",
    "get_jieba",
    "is_jieba_available",
    "get_nlu_cache_dir",
    "get_topic_prototypes_path",
    "get_emotion_prototypes_path",
    "get_trend_prototypes_path",
    "get_prototypes_meta_path",
]
