"""机机语料磁盘库 (2026-08-25 升级计划 Wave ②/③/⑤)。

加载 scripts/mine_fadianji_corpus.py 的产物 (data/fadianji_corpus/):
- scene_pairs.jsonl   → 大规模真实场景对, 供 fdj_scene_retrieval 磁盘检索
- user_profiles.jsonl → 语料人物画像, 供 harness 证据注入
- style_stats.json    → 风格统计, 供统计 style critic / 长度门控

设计:
- 全部惰性加载 + mtime 签名热重载 (数据文件变了下次访问自动生效, 无需重启)
- 任何文件缺失/损坏 → enabled=False 全部 no-op, 不影响主流程
- 轻量语义通道: 哈希 bigram 余弦 (纯 Python, 无 torch/模型依赖)
"""
from __future__ import annotations

import hashlib
import json
import logging
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "data" / "fadianji_corpus"
_SCENES_FILE = "scene_pairs.jsonl"
_PROFILES_FILE = "user_profiles.jsonl"
_STATS_FILE = "style_stats.json"
_MANIFEST_FILE = "manifest.json"

_HASH_DIMS = 4096
_VECTOR_CACHE_LIMIT = 8192

_lock_state = {"imported": False}
_SIGNATURE: tuple[Any, ...] | None = None
_SCENES: tuple[dict[str, Any], ...] = ()
_PROFILES: tuple[dict[str, Any], ...] = ()
_STATS: dict[str, Any] = {}
_MANIFEST: dict[str, Any] = {}
_META_BY_PAIR: dict[tuple[str, str], dict[str, Any]] = {}
_PROFILE_INDEX: dict[str, dict[str, Any]] = {}
_VECTOR_CACHE: OrderedDict[str, dict[int, float]] = OrderedDict()


def corpus_root() -> Path:
    return _CORPUS_ROOT


def _file_signature(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (path.name, stat.st_mtime_ns, stat.st_size)


def _current_signature() -> tuple[Any, ...]:
    return tuple(
        _file_signature(_CORPUS_ROOT / name)
        for name in (_MANIFEST_FILE, _SCENES_FILE, _PROFILES_FILE, _STATS_FILE)
    )


def _load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    entries.append(value)
    except OSError:
        return ()
    return tuple(entries)


def _refresh(force: bool = False) -> None:
    global _SIGNATURE, _SCENES, _PROFILES, _STATS, _MANIFEST, _META_BY_PAIR, _PROFILE_INDEX
    signature = _current_signature()
    if not force and signature == _SIGNATURE:
        return
    scenes_path = _CORPUS_ROOT / _SCENES_FILE
    if not scenes_path.exists():
        _SIGNATURE = signature
        _SCENES = ()
        _PROFILES = ()
        _STATS = {}
        _MANIFEST = {}
        _META_BY_PAIR = {}
        _PROFILE_INDEX = {}
        return
    scenes = _load_jsonl(scenes_path)
    profiles = _load_jsonl(_CORPUS_ROOT / _PROFILES_FILE)
    try:
        stats = json.loads((_CORPUS_ROOT / _STATS_FILE).read_text(encoding="utf-8"))
        if not isinstance(stats, dict):
            stats = {}
    except (OSError, ValueError, TypeError):
        stats = {}
    try:
        manifest = json.loads((_CORPUS_ROOT / _MANIFEST_FILE).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            manifest = {}
    except (OSError, ValueError, TypeError):
        manifest = {}
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in scenes:
        trigger = str(entry.get("trigger") or "").strip()
        reply = str(entry.get("reply") or "").strip()
        if trigger and reply:
            meta[(trigger, reply)] = entry
    index: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        name = _normalize_name(profile.get("name"))
        if name:
            index[name] = profile
    _SCENES = scenes
    _PROFILES = profiles
    _STATS = stats
    _MANIFEST = manifest
    _META_BY_PAIR = meta
    _PROFILE_INDEX = index
    _SIGNATURE = signature
    _VECTOR_CACHE.clear()
    if scenes:
        logger.info(f"fdj corpus store refreshed: {len(scenes)} pairs, {len(profiles)} profiles")


def corpus_signature() -> tuple[Any, ...]:
    _refresh()
    return _SIGNATURE or (("corpus-missing",),)


def corpus_enabled() -> bool:
    _refresh()
    return bool(_SCENES)


def manifest() -> dict[str, Any]:
    _refresh()
    return dict(_MANIFEST)


# ── 场景对 ─────────────────────────────────────────────────────────────

def scene_entries(limit: int = 0) -> tuple[dict[str, Any], ...]:
    """按 quality/count 排序后的场景对原始记录; limit<=0 = 全量。"""
    _refresh()
    if not _SCENES:
        return ()
    ordered = sorted(
        _SCENES,
        key=lambda item: (
            -float(item.get("quality") or 0.0),
            -int(item.get("count") or 0),
            str(item.get("trigger") or ""),
            str(item.get("reply") or ""),
        ),
    )
    if limit and limit > 0:
        ordered = ordered[: int(limit)]
    return tuple(ordered)


def pair_meta(trigger: str, reply: str) -> dict[str, Any] | None:
    _refresh()
    return _META_BY_PAIR.get((str(trigger or "").strip(), str(reply or "").strip()))


# ── 人物画像 ───────────────────────────────────────────────────────────

def _normalize_name(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def lookup_user_profile(display_name: str) -> dict[str, Any] | None:
    """按 live 昵称/群名片找语料画像: 精确 → 双向长前缀 (≥4字, 取互动最多的)。"""
    name = _normalize_name(display_name)
    if not name:
        return None
    _refresh()
    exact = _PROFILE_INDEX.get(name)
    if exact is not None:
        return exact
    if len(name) < 4:
        return None
    best: dict[str, Any] | None = None
    for key, profile in _PROFILE_INDEX.items():
        if len(key) < 4:
            continue
        if key.startswith(name) or name.startswith(key):
            if best is None or int(profile.get("triggers") or 0) > int(best.get("triggers") or 0):
                best = profile
    return best


def all_user_profiles() -> tuple[dict[str, Any], ...]:
    _refresh()
    return _PROFILES


def private_profile(scope: str) -> dict[str, Any] | None:
    """私聊 scope 的语料画像 (挖掘时按 scope 落了一条)。"""
    wanted = str(scope or "").strip().lower()
    if not wanted.startswith("private:"):
        return None
    _refresh()
    for profile in _PROFILES:
        if str(profile.get("scope") or "").strip().lower() == wanted:
            return profile
    return None


# ── 风格统计 ───────────────────────────────────────────────────────────

def style_stats(scope: str = "group") -> dict[str, Any]:
    _refresh()
    value = _STATS.get("group" if not str(scope or "").startswith("private") else "private")
    return dict(value) if isinstance(value, Mapping) else {}


def top_reply_counts(scope: str = "group", min_len: int = 1, max_len: int = 20) -> dict[str, int]:
    """高频短回词表 {回复文本: 语料出现次数}。"""
    stats = style_stats(scope)
    result: dict[str, int] = {}
    for item in stats.get("top_replies") or ():
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("t") or "").strip()
        count = int(item.get("c") or 0)
        if text and min_len <= len(text) <= max_len and count > 0:
            result[text] = count
    return result


# ── 轻量语义通道: 哈希 bigram 余弦 ────────────────────────────────────

def _text_bigrams(text: str) -> list[str]:
    cleaned = "".join(str(text or "").split()).casefold()
    if len(cleaned) <= 1:
        return [cleaned] if cleaned else []
    return [cleaned[i : i + 2] for i in range(len(cleaned) - 1)]


def _hashed_vector(text: str) -> dict[int, float]:
    key = str(text or "")
    cached = _VECTOR_CACHE.get(key)
    if cached is not None:
        _VECTOR_CACHE.move_to_end(key)
        return cached
    vector: dict[int, float] = {}
    for gram in _text_bigrams(key):
        dim = zlib.crc32(gram.encode("utf-8")) % _HASH_DIMS
        vector[dim] = vector.get(dim, 0.0) + 1.0
    norm = sum(weight * weight for weight in vector.values()) ** 0.5
    if norm > 0:
        vector = {dim: weight / norm for dim, weight in vector.items()}
    _VECTOR_CACHE[key] = vector
    _VECTOR_CACHE.move_to_end(key)
    while len(_VECTOR_CACHE) > _VECTOR_CACHE_LIMIT:
        _VECTOR_CACHE.popitem(last=False)
    return vector


def hashed_cosine(text_a: str, text_b: str) -> float:
    vec_a = _hashed_vector(text_a)
    vec_b = _hashed_vector(text_b)
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    return sum(weight * vec_b.get(dim, 0.0) for dim, weight in vec_a.items())


def corpus_semantic_reranker(query: str, candidates: Sequence[Any]) -> list[float]:
    """fdj_scene_retrieval.SemanticReranker 兼容实现: 对候选 trigger 做哈希余弦。"""
    return [max(0.0, min(1.0, hashed_cosine(query, getattr(item, "trigger", "") or ""))) for item in candidates]


__all__ = [
    "all_user_profiles",
    "corpus_enabled",
    "corpus_root",
    "corpus_semantic_reranker",
    "corpus_signature",
    "hashed_cosine",
    "lookup_user_profile",
    "manifest",
    "pair_meta",
    "private_profile",
    "scene_entries",
    "style_stats",
    "top_reply_counts",
]
