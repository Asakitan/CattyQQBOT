"""机机场景检索：真实聊天对母本、类别元数据、scope 隔离与可选本地语义重排。"""
from __future__ import annotations

import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_TRIGGER_RE = re.compile(r"^(?:群友|对方|用户):\s*(.*)$")
_REPLY_RE = re.compile(r"^机机:\s*(.*)$")
_HEADING_RE = re.compile(r"^###\s+(.+?)\s*(?:\(.+\))?$")
_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")
_NAME_PAT = re.compile(r"机机|小机|发电机|不稳定发电机|阿机|牢机|机老师|机宝|机神|机皇|机爷|本体")
_PRIVATE_SCOPE = "private:3670608232"
_CACHE_LIMIT = 128


@dataclass(frozen=True, slots=True)
class SceneRecord:
    trigger: str
    reply: str
    category: str
    source: str
    source_scope: str
    trigger_bigrams: frozenset[str]
    trigger_words: frozenset[str]

    @property
    def private(self) -> bool:
        return self.source_scope.startswith("private:")


@dataclass(frozen=True, slots=True)
class SceneMatch:
    trigger: str
    reply: str
    score: float
    category: str
    source: str
    source_scope: str
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    scope_bonus: float = 0.0

    @property
    def private(self) -> bool:
        return self.source_scope.startswith("private:")

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "reply": self.reply,
            "score": round(self.score, 6),
            "lexical_score": round(self.lexical_score, 6),
            "semantic_score": round(self.semantic_score, 6),
            "scope_bonus": round(self.scope_bonus, 6),
            "category": self.category,
            "source": self.source,
            "source_scope": self.source_scope,
            "private": self.private,
        }


SemanticReranker = Callable[[str, Sequence[SceneRecord]], Mapping[int, float] | Sequence[float] | None]
_SEMANTIC_RERANKER: SemanticReranker | None = None
_MATCH_CACHE: OrderedDict[tuple[Any, ...], tuple[SceneMatch, ...]] = OrderedDict()


def _bigrams(text: str) -> frozenset[str]:
    if len(text) == 1:
        return frozenset({text})
    return frozenset(text[i : i + 2] for i in range(max(0, len(text) - 1)))


def _strip_names(text: str) -> str:
    return _NAME_PAT.sub("", text).strip()


def _normalise_heading(value: str) -> str:
    heading = re.sub(r"\s+", " ", str(value or "").strip())
    heading = heading.split("（", 1)[0].split("(", 1)[0].strip()
    return heading or "未分类"


def _record(trigger: str, reply: str, *, category: str, source: str, source_scope: str) -> SceneRecord:
    return SceneRecord(trigger, reply, category or "未分类", source, source_scope, _bigrams(trigger), frozenset(_WORD_RE.findall(trigger)))


def parse_scene_block(block: str, *, source: str = "group", source_scope: str = "group") -> list[SceneRecord]:
    records: list[SceneRecord] = []
    lines = [line.strip() for line in str(block or "").splitlines()]
    category = "未分类"
    index = 0
    while index < len(lines):
        heading = _HEADING_RE.match(lines[index])
        if heading:
            category = _normalise_heading(heading.group(1))
        if index + 1 < len(lines):
            trigger_match = _TRIGGER_RE.match(lines[index])
            reply_match = _REPLY_RE.match(lines[index + 1]) if trigger_match else None
            if trigger_match and reply_match:
                trigger = trigger_match.group(1).strip()
                reply = reply_match.group(1).strip()
                if trigger and reply:
                    records.append(_record(trigger, reply, category=category, source=source, source_scope=source_scope))
                index += 2
                continue
        index += 1
    return records


def _extract_pairs_from_block(block: str) -> list[tuple[str, str]]:
    return [(item.trigger, item.reply) for item in parse_scene_block(block)]


@lru_cache(maxsize=1)
def _cached_records() -> tuple[SceneRecord, ...]:
    import importlib.util

    scenes_path = Path(__file__).resolve().parent / "personas" / "fadianji_scenes.py"
    spec = importlib.util.spec_from_file_location("fadianji_scenes", scenes_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    records: list[SceneRecord] = []
    records.extend(parse_scene_block(module.FADIANJI_GROUP_SCENE_EXAMPLES, source="fadianji_group_scene", source_scope="group"))
    records.extend(parse_scene_block(module.FADIANJI_GROUP_SCENE_EXAMPLES_EXT, source="fadianji_group_scene_ext", source_scope="group"))
    records.extend(parse_scene_block(module.FADIANJI_PRIVATE_SCENE_EXAMPLES, source="fadianji_private_scene", source_scope=_PRIVATE_SCOPE))
    records.extend(_record(trigger, reply, category="种子兜底", source="fadianji_seed", source_scope="group") for trigger, reply in (
        ("你是真的机机吗", "分机, 本体忙去了"), ("在吗", "在"), ("机机好可爱", "嘿嘿"),
        ("机机陪我干坏事", "滚啊"), ("这图你看懂了吗", "没看懂"), ("机机今天播吗", "不知道啊问本体"),
        ("今天好难过", "怎么了"), ("对不起刚才说重了", "没事"),
        ("config.json 报错说路径找不到", "启动目录不对, 先看配置路径, 不行把报错丢来"),
    ))
    return tuple(records)


@lru_cache(maxsize=1)
def _cached_pairs() -> tuple[tuple[str, str, frozenset[str], frozenset[str]], ...]:
    return tuple((item.trigger, item.reply, item.trigger_bigrams, item.trigger_words) for item in _cached_records())


_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("技术/求助", ("报错", "配置", "代码", "怎么", "帮我", "问题", "启动")),
    ("安慰/接脆弱", ("难过", "哭", "累", "委屈", "疼", "不舒服", "崩溃")),
    ("感谢/收礼", ("谢谢", "感谢", "送", "礼物", "上舰")),
    ("直播相关", ("直播", "开播", "播吗", "下播", "电台")),
    ("不懂/不知道", ("什么", "啥", "意思", "看懂", "知道", "清楚")),
    ("夸赞/惊讶", ("厉害", "牛", "可爱", "好看", "nb", "牛逼")),
    ("深夜 emo", ("失眠", "睡不着", "emo", "凌晨")),
    ("游戏日常", ("鸣潮", "游戏", "排位", "抽卡", "打派")),
)


def _query_categories(query: str) -> frozenset[str]:
    return frozenset(category for category, hints in _CATEGORY_HINTS if any(hint in query for hint in hints))


def _scope_key(scope_key: str) -> str:
    value = str(scope_key or "").strip().lower()
    if value.startswith("qq_private_"):
        return "private:" + value.removeprefix("qq_private_")
    if value.startswith("qq_group_"):
        return "group:" + value.removeprefix("qq_group_")
    return value


def _allowed(record: SceneRecord, scope_key: str, is_private: bool) -> bool:
    if not record.private:
        return True
    return (is_private or scope_key.startswith("private:")) and scope_key == _PRIVATE_SCOPE


def set_scene_semantic_reranker(reranker: SemanticReranker | None) -> None:
    global _SEMANTIC_RERANKER
    _SEMANTIC_RERANKER = reranker
    clear_retrieval_cache()


def clear_retrieval_cache() -> None:
    _MATCH_CACHE.clear()
    _local_embedding_vector.cache_clear()


@lru_cache(maxsize=256)
def _local_embedding_vector(text: str) -> tuple[float, ...] | None:
    try:
        try:
            from .nlu.text2vec_engine import embed_sync_batch
        except ImportError:
            from catty_qq_ai.nlu.text2vec_engine import embed_sync_batch
        values = embed_sync_batch([text])
        if values is None or len(values) == 0:
            return None
        return tuple(float(value) for value in values[0])
    except Exception:
        return None


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return max(0.0, min(1.0, dot / (left_norm * right_norm))) if left_norm and right_norm else 0.0


def _semantic_scores(query: str, candidates: Sequence[SceneRecord]) -> dict[int, float]:
    try:
        if _SEMANTIC_RERANKER is not None:
            result = _SEMANTIC_RERANKER(query, candidates)
            if result is None:
                return {}
            return ({int(index): float(score) for index, score in result.items()} if isinstance(result, Mapping) else {index: float(score) for index, score in enumerate(result)})
        try:
            from .nlu.text2vec_engine import embed_sync_batch
        except ImportError:
            from catty_qq_ai.nlu.text2vec_engine import embed_sync_batch
        values = embed_sync_batch([query, *(record.trigger for record in candidates)])
        if values is None or len(values) != len(candidates) + 1:
            return {}
        query_vector = tuple(float(value) for value in values[0])
        return {
            index: _cosine(query_vector, tuple(float(value) for value in values[index + 1]))
            for index in range(len(candidates))
        }
    except Exception:
        return {}


def match_scene_pairs(text: str, k: int = 5, *, scope_key: str = "", is_private: bool = False, category: str = "", semantic: bool = True) -> list[SceneMatch]:
    query = _strip_names(str(text or "").strip())
    if not query or k <= 0:
        return []
    normalized_scope = _scope_key(scope_key)
    key = (query, int(k), normalized_scope, bool(is_private), str(category or ""), bool(semantic))
    cached = _MATCH_CACHE.get(key)
    if cached is not None:
        _MATCH_CACHE.move_to_end(key)
        return list(cached)
    query_grams = _bigrams(query)
    query_words = frozenset(_WORD_RE.findall(query))
    query_categories = _query_categories(query)
    lexical: list[tuple[float, float, SceneRecord, float]] = []
    for record in _cached_records():
        if not _allowed(record, normalized_scope, bool(is_private)) or not record.trigger_bigrams:
            continue
        word_hit = len(query_words & record.trigger_words)
        union = len(query_grams | record.trigger_bigrams)
        jaccard = len(query_grams & record.trigger_bigrams) / max(1, union)
        category_hit = 0.8 if category and category == record.category else (0.55 if record.category in query_categories else 0.0)
        lexical_score = word_hit * 2.0 + jaccard * 1.5 + category_hit + (0.05 if len(record.trigger) <= 12 else 0.0)
        if word_hit == 0 and jaccard < 0.25 and category_hit <= 0:
            continue
        scope_bonus = 0.18 if record.private and normalized_scope == _PRIVATE_SCOPE else 0.0
        lexical.append((lexical_score + scope_bonus, lexical_score, record, scope_bonus))
    lexical.sort(key=lambda item: (-item[0], -item[1], item[2].source, item[2].category, item[2].trigger, item[2].reply))
    candidates = [item[2] for item in lexical[: max(k * 8, 32)]]
    semantic_scores = _semantic_scores(query, candidates) if semantic and candidates else {}
    best: dict[str, SceneMatch] = {}
    for index, record in enumerate(candidates):
        lexical_item = next(item for item in lexical if item[2] is record)
        total, lexical_score, _, scope_bonus = lexical_item
        semantic_score = max(0.0, min(1.0, semantic_scores.get(index, 0.0)))
        match = SceneMatch(record.trigger, record.reply, total + semantic_score * 1.5, record.category, record.source, record.source_scope, lexical_score, semantic_score, scope_bonus)
        old = best.get(record.reply)
        if old is None or (match.score, match.lexical_score, match.trigger) > (old.score, old.lexical_score, old.trigger):
            best[record.reply] = match
    result = sorted(best.values(), key=lambda item: (-item.score, -item.lexical_score, -item.semantic_score, item.source, item.category, item.trigger, item.reply))[:k]
    packed = tuple(result)
    _MATCH_CACHE[key] = packed
    _MATCH_CACHE.move_to_end(key)
    while len(_MATCH_CACHE) > _CACHE_LIMIT:
        _MATCH_CACHE.popitem(last=False)
    return list(packed)


retrieve_scene_matches = match_scene_pairs


def top_scene_pairs(text: str, k: int = 5, *, scope_key: str = "", is_private: bool = False, category: str = "") -> list[tuple[str, str, float]]:
    return [(item.trigger, item.reply, item.score) for item in match_scene_pairs(text, k=k, scope_key=scope_key, is_private=is_private, category=category)]


def build_scene_reference_block(text: str, k: int = 5, is_private: bool = False, *, scope_key: str = "", category: str = "") -> str:
    matches = match_scene_pairs(text, k=k, scope_key=scope_key, is_private=is_private, category=category)
    if not matches:
        return ""
    lines = ["【当前消息最像的机机真实聊天记录】(只参考她的口吻和反应方式, 不照抄原文)"]
    lines.extend(f"历史: {item.trigger} → 机机: {item.reply}" for item in matches)
    return "\n".join(lines)


__all__ = ["SceneMatch", "SceneRecord", "build_scene_reference_block", "clear_retrieval_cache", "match_scene_pairs", "parse_scene_block", "retrieve_scene_matches", "set_scene_semantic_reranker", "top_scene_pairs"]