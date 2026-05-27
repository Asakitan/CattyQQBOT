"""jieba 中文分词包装 — lazy load + 笨猫自定义词典.

主人 2026-05-28: 引入作为 NLU 增强基础, 给 HanLP/text2vec 之外的轻量分词需要打底.

特性:
- lazy load (第一次 tokenize 时才 import jieba, 启动时不卡)
- 失败 graceful fallback: jieba 缺装 / 加载异常 → tokenize() 返回 None,
  caller **必须**当作 None 走 legacy 路径 (一般是 list(text) 单字切)
- 笨猫黑话自定义词典: 杂鱼/破防/yyds/磨叽/嗷呜/喵呜/蹭蹭/贴贴/ฅฅ 等不切碎

config gating:
- catty_use_jieba=False 时, tokenize() 直接返回 None (绕过 jieba)
- 默认 False, 主人手动 flip 后启用
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger("catty.nlu.jieba")


# 笨猫专属词典 — 这些词 jieba 默认分错, 需要锁词
# (词, 词频, 词性) — 词频高于默认时优先成词
_CATTY_CUSTOM_DICT: tuple[tuple[str, int, str | None], ...] = (
    # 笨猫黑话 + 网络词
    ("杂鱼", 1000, "n"),
    ("破防", 1000, "v"),
    ("yyds", 1000, "n"),
    ("磨叽", 800, "v"),
    ("嗷呜", 1000, "y"),    # 语气词
    ("喵呜", 1000, "y"),
    ("喵喵", 1000, "y"),
    ("蹭蹭", 1000, "v"),
    ("贴贴", 1000, "v"),
    ("抱抱", 1000, "v"),
    ("摸摸", 1000, "v"),
    ("亲亲", 1000, "v"),
    ("摸头", 1000, "v"),
    ("撒娇", 1000, "v"),
    ("ฅฅ", 1000, "y"),
    # 主人/猫娘相关
    ("笨猫", 1000, "n"),
    ("猫猫", 1000, "n"),
    ("猫娘", 1000, "n"),
    ("主人", 1000, "n"),
    ("笨蛋主人", 1000, "n"),
    # 游戏/技术圈
    ("strinova", 1000, "n"),
    ("原神", 1000, "n"),
    ("崩铁", 1000, "n"),
    ("绝区零", 1000, "n"),
    ("赛博朋克", 800, "n"),
    ("地铁跑酷", 800, "n"),
    # NSFW 软词 (确保分词不切碎给 NLU 用)
    ("援交", 800, "n"),
    ("好感度", 1000, "n"),
)


_JIEBA_MOD: Any = None       # jieba 模块对象
_LOAD_FAILED: bool = False
_LOAD_LOCK = threading.Lock()


def _is_enabled() -> bool:
    """读 config 看 catty_use_jieba 是否开. 避免 import cycle 用 lazy import."""
    try:
        from nonebot import get_driver
        config = get_driver().config
        return bool(getattr(config, "catty_use_jieba", False))
    except Exception:
        return False


def _load_blocking() -> Any | None:
    """同步加载 jieba + 注入自定义词典. 返回模块或 None.

    线程安全: 取锁后再 check. 失败永久 mark _LOAD_FAILED, 不再重试.
    """
    global _JIEBA_MOD, _LOAD_FAILED
    if _LOAD_FAILED:
        return None
    if _JIEBA_MOD is not None:
        return _JIEBA_MOD
    with _LOAD_LOCK:
        if _LOAD_FAILED:
            return None
        if _JIEBA_MOD is not None:
            return _JIEBA_MOD
        try:
            import jieba  # type: ignore
            jieba.setLogLevel(logging.WARNING)
            # 注入自定义词典 (一次性, 模块级)
            for word, freq, pos in _CATTY_CUSTOM_DICT:
                try:
                    if pos:
                        jieba.add_word(word, freq=freq, tag=pos)
                    else:
                        jieba.add_word(word, freq=freq)
                except Exception:
                    pass  # 单个词加失败不致命
            _JIEBA_MOD = jieba
            logger.info("jieba loaded with %d catty custom words", len(_CATTY_CUSTOM_DICT))
            return _JIEBA_MOD
        except Exception as exc:
            logger.warning("jieba load failed, falling back to None: %s", exc)
            _LOAD_FAILED = True
            return None


def get_jieba() -> Any | None:
    """同步拿 jieba 模块. 不开 config 或加载失败 → None."""
    if not _is_enabled():
        return None
    return _load_blocking()


def is_jieba_available() -> bool:
    """快速 check: 是否能用 jieba (config 开 + 加载未失败).

    不触发加载. 仅看 mod 是否已加载. 第一次 call 前永远 False.
    """
    return _JIEBA_MOD is not None and not _LOAD_FAILED


def tokenize(text: str, cut_all: bool = False) -> list[str] | None:
    """jieba 分词. 失败 / 关闭 / 空文本 → None.

    Args:
        text: 输入字符串
        cut_all: True = 全模式 (返回所有可能词), False = 精确模式 (推荐)
    Returns:
        词列表, 或 None 表示 fallback
    """
    if not text or not isinstance(text, str):
        return None
    j = get_jieba()
    if j is None:
        return None
    try:
        return list(j.cut(text, cut_all=cut_all))
    except Exception as exc:
        logger.debug("jieba.cut failed on text len=%d: %s", len(text), exc)
        return None


def posseg(text: str) -> list[tuple[str, str]] | None:
    """词性标注分词. 返回 [(word, pos), ...] 或 None.

    用于 user_details_store NER 时配合 HanLP 做 POS 锚定 (commit 3).
    """
    if not text or not isinstance(text, str):
        return None
    j = get_jieba()
    if j is None:
        return None
    try:
        from jieba import posseg  # type: ignore
        return [(p.word, p.flag) for p in posseg.cut(text)]
    except Exception as exc:
        logger.debug("jieba.posseg failed: %s", exc)
        return None


__all__ = [
    "get_jieba",
    "is_jieba_available",
    "tokenize",
    "posseg",
]
