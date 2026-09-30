"""机机场景检索：真实聊天母本、scope 隔离、纯规则 delta 与可选显式语义重排。"""
from __future__ import annotations

import hashlib
import json
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
_MIN_LEXICAL_SCORE = 0.55
_DELTA_SCHEMA_VERSION = 1
_DELTA_ROOT = Path(__file__).resolve().parent / "data" / "fdj_scene_delta"
_DELTA_MANIFEST = _DELTA_ROOT / "manifest.json"


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
_DELTA_SIGNATURE: tuple[Any, ...] | None = None
_DELTA_ENTRIES: tuple[dict[str, Any], ...] = ()


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
    trigger = str(trigger or "").strip()
    reply = str(reply or "").strip()
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


def _stable_uid(record: SceneRecord) -> str:
    raw = "\x1f".join((record.source, record.source_scope, record.category, record.trigger, record.reply))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _valid_delta_scope(value: Any) -> str | None:
    scope = str(value or "").strip().lower()
    if scope == "group":
        return scope
    if re.fullmatch(r"(?:group|private):[0-9A-Za-z_-]+", scope):
        return scope
    return None


def _delta_signature() -> tuple[Any, ...]:
    try:
        manifest_stat = _DELTA_MANIFEST.stat()
    except OSError:
        return (("manifest-missing",),)
    files: list[tuple[str, int, int]] = []
    try:
        data = json.loads(_DELTA_MANIFEST.read_text(encoding="utf-8-sig"))
        active_files = data.get("active_files", []) if isinstance(data, Mapping) else []
        if isinstance(active_files, list):
            for item in active_files:
                relative = item.get("path") if isinstance(item, Mapping) else item
                if not isinstance(relative, str):
                    continue
                path = (_DELTA_ROOT / relative).resolve()
                try:
                    path.relative_to(_DELTA_ROOT.resolve())
                    stat = path.stat()
                except (OSError, ValueError):
                    files.append((relative, -1, -1))
                else:
                    files.append((relative, stat.st_mtime_ns, stat.st_size))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        files.append(("manifest-invalid", -1, -1))
    return ((manifest_stat.st_mtime_ns, manifest_stat.st_size), tuple(files))


def _read_delta_entries() -> tuple[dict[str, Any], ...] | None:
    try:
        manifest = json.loads(_DELTA_MANIFEST.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != _DELTA_SCHEMA_VERSION:
        return None
    active_files = manifest.get("active_files")
    if not isinstance(active_files, list):
        return None
    entries: list[dict[str, Any]] = []
    root = _DELTA_ROOT.resolve()
    for item in active_files:
        relative = item.get("path") if isinstance(item, Mapping) else item
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            return None
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(value, Mapping) or value.get("status") != "approved":
                        continue
                    uid = str(value.get("record_id") or value.get("uid") or "").strip()
                    op = str(value.get("op") or "").strip().lower()
                    if not uid or op not in {"upsert", "delete"}:
                        continue
                    if op == "delete":
                        entries.append({"uid": uid, "op": op})
                        continue
                    trigger = str(
                        value.get("query_text")
                        or value.get("trigger")
                        or value.get("primary_trigger")
                        or ""
                    ).strip()
                    reply = str(value.get("reply") or "").strip()
                    category = str(value.get("category") or "未分类").strip()[:80]
                    source_scope = _valid_delta_scope(value.get("source_scope", value.get("scope")))
                    if not trigger or not reply or not source_scope:
                        continue
                    entries.append({
                        "uid": uid,
                        "op": op,
                        "trigger": trigger[:320],
                        "reply": reply[:320],
                        "category": category or "未分类",
                        "source_scope": source_scope,
                        "source": f"fdj_delta:{relative}",
                    })
        except OSError:
            return None
    return tuple(entries)


def _refresh_delta_cache(*, force: bool = False) -> None:
    global _DELTA_SIGNATURE, _DELTA_ENTRIES
    signature = _delta_signature()
    if not force and signature == _DELTA_SIGNATURE:
        return
    entries = _read_delta_entries()
    if entries is None:
        if _DELTA_SIGNATURE is not None:
            return
        entries = ()
    _DELTA_SIGNATURE = signature
    _DELTA_ENTRIES = entries
    _RECORDS_CACHE.clear()
    _CANDIDATE_INDEX_CACHE.clear()
    _cached_pairs.cache_clear()
    _MATCH_CACHE.clear()


def refresh_scene_delta() -> None:
    _refresh_delta_cache(force=True)


def _base_records() -> list[tuple[str, SceneRecord]]:
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
    return [(_stable_uid(record), record) for record in records]


def _delta_applied_records() -> tuple[SceneRecord, ...]:
    by_uid = dict(_base_records())
    order = list(by_uid)
    for entry in _DELTA_ENTRIES:
        uid = entry["uid"]
        if entry["op"] == "delete":
            by_uid.pop(uid, None)
            if uid in order:
                order.remove(uid)
            continue
        record = _record(entry["trigger"], entry["reply"], category=entry["category"], source=entry["source"], source_scope=entry["source_scope"])
        if uid not in by_uid:
            order.append(uid)
        by_uid[uid] = record
    return tuple(by_uid[uid] for uid in order if uid in by_uid)


def _corpus_max_pairs() -> int:
    try:
        from . import config as _module_config
        if not bool(getattr(_module_config.config, "catty_fdj_corpus_enabled", True)):
            return 0
        return max(0, int(getattr(_module_config.config, "catty_fdj_corpus_max_pairs", 12000) or 0))
    except Exception:  # noqa: BLE001
        return 12000


def _corpus_signature() -> tuple[Any, ...]:
    try:
        from . import fdj_corpus_store
        return fdj_corpus_store.corpus_signature()
    except Exception:  # noqa: BLE001
        return (("corpus-unavailable",),)


def _corpus_records(existing_pairs: set[tuple[str, str]]) -> tuple[SceneRecord, ...]:
    limit = _corpus_max_pairs()
    if limit <= 0:
        return ()
    try:
        from . import fdj_corpus_store
        entries = fdj_corpus_store.scene_entries(limit=limit)
    except Exception:  # noqa: BLE001
        return ()
    records: list[SceneRecord] = []
    for entry in entries:
        trigger = str(entry.get("trigger") or "").strip()
        reply = str(entry.get("reply") or "").strip()
        scope = str(entry.get("scope") or "group").strip() or "group"
        category = str(entry.get("category") or "未分类").strip()[:80] or "未分类"
        if not trigger or not reply or (trigger, reply) in existing_pairs:
            continue
        existing_pairs.add((trigger, reply))
        records.append(_record(trigger, reply, category=category, source="fdj_corpus", source_scope=scope))
    return tuple(records)


_RECORDS_CACHE: dict[tuple[Any, ...], tuple[SceneRecord, ...]] = {}


def _cached_records() -> tuple[SceneRecord, ...]:
    """精选库 + delta + 语料磁盘库; 按 (delta签名, corpus签名) 手动缓存。"""
    _refresh_delta_cache()
    corpus_sig = _corpus_signature()
    key = (_DELTA_SIGNATURE, corpus_sig)
    cached = _RECORDS_CACHE.get(key)
    if cached is None:
        base = _delta_applied_records()
        existing_pairs = {(item.trigger, item.reply) for item in base}
        cached = base + _corpus_records(existing_pairs)
        _RECORDS_CACHE.clear()
        _RECORDS_CACHE[key] = cached
        _cached_pairs.cache_clear()
        _CANDIDATE_INDEX_CACHE.clear()
    return cached


_CANDIDATE_INDEX_CACHE: dict[tuple[Any, ...], tuple[dict[str, list[int]], dict[str, list[int]]]] = {}


def _candidate_index() -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """词/分类倒排索引, 给大语料库做候选剪枝 (候选太少时调用方回退全扫)。"""
    records = _cached_records()
    key = (_DELTA_SIGNATURE, _corpus_signature(), len(records))
    cached = _CANDIDATE_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    word_index: dict[str, list[int]] = {}
    category_index: dict[str, list[int]] = {}
    for position, record in enumerate(records):
        for word in record.trigger_words:
            word_index.setdefault(word, []).append(position)
        category_index.setdefault(record.category, []).append(position)
    built = (word_index, category_index)
    _CANDIDATE_INDEX_CACHE.clear()
    _CANDIDATE_INDEX_CACHE[key] = built
    return built


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
    source_scope = record.source_scope.lower()
    if source_scope == "group":
        return True
    if source_scope.startswith("private:"):
        return (is_private or scope_key.startswith("private:")) and scope_key == source_scope
    if source_scope.startswith("group:"):
        return not is_private and scope_key == source_scope
    return False


def set_scene_semantic_reranker(reranker: SemanticReranker | None) -> None:
    global _SEMANTIC_RERANKER
    _SEMANTIC_RERANKER = reranker
    clear_retrieval_cache()


def _ensure_default_semantic_reranker() -> None:
    """语料磁盘库可用时自动挂轻量语义通道 (哈希 bigram 余弦, 无模型依赖)。"""
    if _SEMANTIC_RERANKER is not None:
        return
    try:
        try:
            from . import config as _module_config
            enabled = bool(getattr(_module_config.config, "catty_fdj_corpus_semantic_enabled", True))
        except Exception:  # noqa: BLE001
            enabled = True
        if not enabled:
            return
        from . import fdj_corpus_store
        if fdj_corpus_store.corpus_enabled():
            set_scene_semantic_reranker(fdj_corpus_store.corpus_semantic_reranker)
    except Exception:  # noqa: BLE001
        return


def clear_retrieval_cache() -> None:
    _MATCH_CACHE.clear()
    _RECORDS_CACHE.clear()
    _CANDIDATE_INDEX_CACHE.clear()
    _cached_pairs.cache_clear()


def _semantic_scores(query: str, candidates: Sequence[SceneRecord]) -> dict[int, float]:
    if _SEMANTIC_RERANKER is None:
        return {}
    try:
        result = _SEMANTIC_RERANKER(query, candidates)
        if result is None:
            return {}
        if isinstance(result, Mapping):
            return {int(index): float(score) for index, score in result.items()}
        return {index: float(score) for index, score in enumerate(result)}
    except Exception:
        return {}


def _source_priority(source: str) -> int:
    if source.startswith("fadianji_seed"):
        return 2
    if source.startswith("fdj_delta:") or source.startswith("fadianji_"):
        return 1
    return 0


def _select_diverse(matches: Sequence[SceneMatch], k: int) -> list[SceneMatch]:
    selected: list[SceneMatch] = []
    seen_categories: set[str] = set()
    for diverse_only in (True, False):
        for item in matches:
            if item in selected:
                continue
            if diverse_only and item.category in seen_categories:
                continue
            selected.append(item)
            seen_categories.add(item.category)
            if len(selected) >= k:
                return selected
    return selected


def match_scene_pairs(text: str, k: int = 5, *, scope_key: str = "", is_private: bool = False, category: str = "", semantic: bool = False) -> list[SceneMatch]:
    _refresh_delta_cache()
    query = _strip_names(str(text or "").strip())
    if not query or k <= 0:
        return []
    normalized_scope = _scope_key(scope_key)
    key = (query, int(k), normalized_scope, bool(is_private), str(category or ""), bool(semantic), _DELTA_SIGNATURE, _corpus_signature())
    cached = _MATCH_CACHE.get(key)
    if cached is not None:
        _MATCH_CACHE.move_to_end(key)
        return list(cached)
    query_grams = _bigrams(query)
    query_words = frozenset(_WORD_RE.findall(query))
    query_categories = _query_categories(query)
    records = _cached_records()
    word_index, category_index = _candidate_index()
    candidate_positions: set[int] = set()
    for word in query_words:
        positions = word_index.get(word)
        if positions:
            candidate_positions.update(positions)
    if category:
        candidate_positions.update(category_index.get(category, ()))
    for query_category in query_categories:
        candidate_positions.update(category_index.get(query_category, ()))
    if 4 <= len(candidate_positions) < len(records):
        record_iter = (records[position] for position in sorted(candidate_positions))
    else:
        record_iter = iter(records)
    lexical: list[tuple[float, float, SceneRecord, float]] = []
    for record in record_iter:
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
        total = lexical_score + scope_bonus
        if total < _MIN_LEXICAL_SCORE:
            continue
        lexical.append((total, lexical_score, record, scope_bonus))
    lexical.sort(key=lambda item: (-item[0], -item[1], _source_priority(item[2].source), item[2].source, item[2].category, item[2].trigger, item[2].reply))
    candidates = [item[2] for item in lexical[: max(k * 8, 32)]]
    if semantic and candidates:
        _ensure_default_semantic_reranker()
    semantic_scores = _semantic_scores(query, candidates) if semantic and candidates else {}
    best: dict[str, SceneMatch] = {}
    lexical_by_identity = {id(item[2]): item for item in lexical}
    for index, record in enumerate(candidates):
        total, lexical_score, _, scope_bonus = lexical_by_identity[id(record)]
        semantic_score = max(0.0, min(1.0, semantic_scores.get(index, 0.0)))
        match = SceneMatch(record.trigger, record.reply, total + semantic_score * 1.5, record.category, record.source, record.source_scope, lexical_score, semantic_score, scope_bonus)
        old = best.get(record.reply)
        if old is None or (match.score, match.lexical_score, -_source_priority(match.source), match.trigger) > (old.score, old.lexical_score, -_source_priority(old.source), old.trigger):
            best[record.reply] = match
    ranked = sorted(best.values(), key=lambda item: (-item.score, -item.lexical_score, -item.semantic_score, _source_priority(item.source), item.source, item.category, item.trigger, item.reply))
    result = _select_diverse(ranked, int(k))
    packed = tuple(result)
    _MATCH_CACHE[key] = packed
    _MATCH_CACHE.move_to_end(key)
    while len(_MATCH_CACHE) > _CACHE_LIMIT:
        _MATCH_CACHE.popitem(last=False)
    return list(packed)


retrieve_scene_matches = match_scene_pairs


def top_scene_pairs(text: str, k: int = 5, *, scope_key: str = "", is_private: bool = False, category: str = "", semantic: bool = False) -> list[tuple[str, str, float]]:
    return [(item.trigger, item.reply, item.score) for item in match_scene_pairs(text, k=k, scope_key=scope_key, is_private=is_private, category=category, semantic=semantic)]


def build_scene_reference_block(text: str, k: int = 5, is_private: bool = False, *, scope_key: str = "", category: str = "", semantic: bool = False) -> str:
    matches = match_scene_pairs(text, k=k, scope_key=scope_key, is_private=is_private, category=category, semantic=semantic)
    if not matches:
        return ""
    lines = ["【STYLE_EXAMPLE·机机真实聊天母本】只参考口吻、长度和反应方式，不照抄原文"]
    lines.extend(f"历史: {item.trigger} → 机机: {item.reply}（分类={item.category}）" for item in matches)
    return "\n".join(lines)


__all__ = [
    "SceneMatch",
    "SceneRecord",
    "build_scene_reference_block",
    "clear_retrieval_cache",
    "match_scene_pairs",
    "parse_scene_block",
    "refresh_scene_delta",
    "retrieve_scene_matches",
    "set_scene_semantic_reranker",
    "top_scene_pairs",
]