"""文本归一化 (CPU 引擎 L1-L2 输入预处理).

输入: QQ 原始消息文本.
输出: NormalizedText {原文, 去 @ 后, 表情占位后, 全半角统一后, jieba 切词后}.

不做繁简转换 (QQ 用户主要简体), 不做敏感词过滤 (那是 filter AI 的事).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    import jieba  # type: ignore

    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False
    jieba = None


_RE_AT = re.compile(r"@[\w一-鿿]+\s*")
_RE_CQ = re.compile(r"\[CQ:([a-z_]+)(?:,[^\]]*)?\]")
_CQ_PLACEHOLDERS = {
    "face": "[FACE]",
    "image": "[IMG]",
    "record": "[VOICE]",
    "video": "[VIDEO]",
    "at": "",
    "reply": "[REPLY]",
    "rps": "[RPS]",
    "dice": "[DICE]",
    "share": "[SHARE]",
    "music": "[MUSIC]",
    "poke": "[POKE]",
}
_RE_WHITESPACE = re.compile(r"\s+")
_RE_PUNCT_DUP = re.compile(r"([!?。！？.])\1{2,}")


@dataclass(slots=True)
class NormalizedText:
    raw: str
    cleaned: str
    tokens: list[str]
    has_image: bool
    has_voice: bool
    has_at: bool
    punct_intensity: float


def _cq_replace(match: re.Match[str]) -> str:
    return _CQ_PLACEHOLDERS.get(match.group(1), "")


def _fullwidth_to_halfwidth(text: str) -> str:
    chars = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    return "".join(chars)


def _punct_intensity(text: str) -> float:
    if not text:
        return 0.0
    bang = text.count("!") + text.count("！")
    question = text.count("?") + text.count("？")
    ellipsis = text.count("...") + text.count("…")
    raw_score = bang * 1.5 + question * 1.0 + ellipsis * 0.8
    return min(raw_score / max(len(text) / 6, 1), 1.0)


def normalize(text: str) -> NormalizedText:
    if not text:
        return NormalizedText(raw="", cleaned="", tokens=[], has_image=False, has_voice=False, has_at=False, punct_intensity=0.0)

    has_image = "[CQ:image" in text
    has_voice = "[CQ:record" in text
    has_at = "[CQ:at" in text or bool(_RE_AT.search(text))

    cleaned = _RE_CQ.sub(_cq_replace, text)
    cleaned = _RE_AT.sub("", cleaned)
    cleaned = _fullwidth_to_halfwidth(cleaned)
    cleaned = _RE_PUNCT_DUP.sub(r"\1\1\1", cleaned)
    cleaned = _RE_WHITESPACE.sub(" ", cleaned).strip()

    tokens = _tokenize(cleaned)
    intensity = _punct_intensity(text)

    return NormalizedText(
        raw=text,
        cleaned=cleaned,
        tokens=tokens,
        has_image=has_image,
        has_voice=has_voice,
        has_at=has_at,
        punct_intensity=intensity,
    )


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    if _HAS_JIEBA:
        return [t for t in jieba.lcut(text, HMM=True) if t.strip()]
    return [t for t in text.split() if t]
