"""L2 Semantic Router 包装.

每条 route 挂多个 utterances (示例问句) + responses (米雪儿语气回答).
启动时 bge embed_sync_batch 把所有 utterances 算成一个 flat 矩阵 (N_total, dim),
查询时 query @ flat.T 一次拿全部分数, 按 route 聚合 max -> top1 route.

阈值:
- score >= direct_threshold (默认 0.82): 直答, confidence=score
- candidate_threshold <= score < direct_threshold (默认 0.70-0.82): 标低信心进 L4 风格化
- score < candidate_threshold: 透传到 L3

主人 2026-05-30: 磁盘缓存 — 130K utterances 嵌入矩阵存 .npy + metadata.json, 
用 routes_dir mtime 签名校验, 命中时 np.load() 直接恢复 (~100ms), 跳过嵌入重算.
首次或无缓存时 chunked embed (5K/chunk) + 进度 log.

无 Semantic Router 第三方库依赖: 直接 numpy + text2vec_engine.embed_sync_batch,
原因是 semantic-router 0.1.x 自带 encoder 抽象偏服务侧 (OpenAI/Cohere),
和现有 bge ONNX 本地 stack 适配起来反而多一层. 自己 100 行实现更轻.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

try:
    import numpy as np  # type: ignore

    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None  # type: ignore[assignment]

try:
    import yaml  # type: ignore

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    yaml = None


EmbedBatchFn = Callable[[list[str]], "np.ndarray | None"]


@dataclass(slots=True)
class SemanticRoute:
    name: str
    intent: str
    utterances: list[str]
    responses: list[str]
    weight: float = 1.0


@dataclass(slots=True)
class SemanticMatchResult:
    route_name: str
    intent: str
    response: str
    confidence: float
    matched_utterance: str = ""
    is_direct: bool = False


class SemanticRouter:
    """L2: bge embed 找最近 route.

    主人 2026-05-30: 磁盘缓存 — 130K utterances 嵌入矩阵存 .npy + metadata.json,
    用 mtime_sig 校验, 命中时 np.load() 直接恢复 (~100ms). 未命中时 chunked embed
    (5K/chunk) + 进度 log, 避免 130K 一次性嵌入卡死 129s 无输出.
    """

    _EMBED_CHUNK_SIZE = 5000  # 每批嵌 5K 条

    def __init__(
        self,
        routes: list[SemanticRoute],
        embed_batch_fn: EmbedBatchFn,
        *,
        cache_dir: str | Path | None = None,
        routes_mtime_sig: str = "",
    ) -> None:
        self._routes: list[SemanticRoute] = list(routes)
        self._embed_batch_fn = embed_batch_fn
        self._flat_utterances: list[str] = []
        self._flat_route_idx: list[int] = []
        self._flat_embeddings: np.ndarray | None = None
        self._prepared = False
        # 磁盘缓存路径
        if cache_dir:
            self._cache_npy = Path(cache_dir) / "semantic_l2_embeddings.npy"
            self._cache_meta = Path(cache_dir) / "semantic_l2_metadata.json"
        else:
            self._cache_npy = None
            self._cache_meta = None
        self._routes_mtime_sig = routes_mtime_sig

    def prepare(self) -> bool:
        if self._prepared:
            return True
        if not _HAS_NUMPY:
            logger.error("[cpu_engine.L2] numpy not installed, L2 disabled")
            self._prepared = True
            return False
        if not self._routes:
            logger.warning("[cpu_engine.L2] no routes loaded, skipping prepare")
            self._prepared = True
            return False

        for i, route in enumerate(self._routes):
            for utt in route.utterances:
                if not utt.strip():
                    continue
                self._flat_utterances.append(utt)
                self._flat_route_idx.append(i)

        n_utts = len(self._flat_utterances)
        if not n_utts:
            logger.warning("[cpu_engine.L2] no utterances across routes")
            self._prepared = True
            return False

        # ── 主人 2026-05-30: 磁盘缓存 ──
        if self._try_load_cache(n_utts):
            self._prepared = True
            return True

        # ── chunked embed + 进度 log ──
        logger.info(
            f"[cpu_engine.L2] embedding {n_utts} utterances "
            f"(chunk_size={self._EMBED_CHUNK_SIZE}..."
        )
        all_embs: list[np.ndarray] = []
        for chunk_start in range(0, n_utts, self._EMBED_CHUNK_SIZE):
            chunk_end = min(chunk_start + self._EMBED_CHUNK_SIZE, n_utts)
            chunk = self._flat_utterances[chunk_start:chunk_end]
            chunk_embs = self._embed_batch_fn(chunk)
            if chunk_embs is None:
                logger.error(
                    f"[cpu_engine.L2] embed chunk [{chunk_start}:{chunk_end}] "
                    f"returned None, L2 disabled"
                )
                self._prepared = True
                return False
            all_embs.append(chunk_embs.astype(np.float32, copy=False))
            logger.info(
                f"[cpu_engine.L2] embedded {chunk_end}/{n_utts} "
                f"(dim={chunk_embs.shape[1]})"
            )
        embeddings = np.vstack(all_embs)

        if embeddings.shape[0] != n_utts:
            logger.error(
                f"[cpu_engine.L2] embed shape mismatch: got {embeddings.shape[0]}, "
                f"expected {n_utts}"
            )
            self._prepared = True
            return False

        self._flat_embeddings = embeddings.astype(np.float32, copy=False)
        # 保存磁盘缓存
        self._save_cache()
        self._prepared = True
        dim = self._flat_embeddings.shape[1]
        logger.info(
            f"[cpu_engine.L2] prepared {len(self._routes)} routes, "
            f"{n_utts} utterances, embed dim={dim}"
        )
        return True

    # ── 磁盘缓存 helpers ──
    def _try_load_cache(self, n_utts: int) -> bool:
        if not self._cache_npy or not self._cache_meta:
            return False
        if not self._cache_npy.exists() or not self._cache_meta.exists():
            return False
        try:
            meta = json.loads(self._cache_meta.read_text(encoding="utf-8"))
            if meta.get("n_utts") != n_utts:
                logger.info(
                    f"[cpu_engine.L2] cache stale: n_utts mismatch "
                    f"({meta.get('n_utts')} != {n_utts}), rebuild"
                )
                return False
            if meta.get("mtime_sig") != self._routes_mtime_sig:
                logger.info(
                    "[cpu_engine.L2] cache stale: mtime_sig changed, rebuild"
                )
                return False
            flat = np.load(str(self._cache_npy))
            if flat.shape[0] != n_utts:
                logger.info(
                    f"[cpu_engine.L2] cache stale: shape mismatch "
                    f"({flat.shape[0]} != {n_utts}), rebuild"
                )
                return False
            self._flat_embeddings = flat.astype(np.float32, copy=False)
            logger.info(
                f"[cpu_engine.L2] disk cache hit: {n_utts} utterances, "
                f"dim={flat.shape[1]}, ~{flat.nbytes / 1024 / 1024:.1f}MB"
            )
            return True
        except Exception as exc:
            logger.info(f"[cpu_engine.L2] cache load failed: {exc}, rebuild")
            return False

    def _save_cache(self) -> None:
        if not self._cache_npy or self._flat_embeddings is None:
            return
        try:
            self._cache_npy.parent.mkdir(parents=True, exist_ok=True)
            np.save(str(self._cache_npy), self._flat_embeddings)
            meta = {
                "n_utts": len(self._flat_utterances),
                "mtime_sig": self._routes_mtime_sig,
                "dim": int(self._flat_embeddings.shape[1]),
            }
            self._cache_meta.write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
            logger.info(
                f"[cpu_engine.L2] disk cache saved: "
                f"{self._flat_embeddings.shape[0]} utterances, "
                f"dim={meta['dim']}"
            )
        except Exception as exc:
            logger.warning(f"[cpu_engine.L2] cache save failed: {exc}")

    def match(
        self,
        text: str,
        *,
        embed_query_fn: Callable[[str], np.ndarray | None],
        direct_threshold: float = 0.82,
        candidate_threshold: float = 0.70,
    ) -> SemanticMatchResult | None:
        if not self._prepared:
            return None
        if self._flat_embeddings is None or not self._flat_route_idx:
            return None

        query_vec = embed_query_fn(text)
        if query_vec is None:
            return None
        query = query_vec.astype(np.float32, copy=False)
        if query.ndim != 1 or query.shape[0] != self._flat_embeddings.shape[1]:
            logger.warning(
                f"[cpu_engine.L2] query vec shape {query.shape} mismatch "
                f"corpus {self._flat_embeddings.shape}"
            )
            return None

        scores = self._flat_embeddings @ query
        best_per_route: dict[int, tuple[float, int]] = {}
        for utt_idx, score in enumerate(scores):
            route_idx = self._flat_route_idx[utt_idx]
            weighted = float(score) * self._routes[route_idx].weight
            current = best_per_route.get(route_idx)
            if current is None or weighted > current[0]:
                best_per_route[route_idx] = (weighted, utt_idx)

        if not best_per_route:
            return None

        winner_route_idx, (winner_score, winner_utt_idx) = max(
            best_per_route.items(), key=lambda kv: kv[1][0]
        )

        if winner_score < candidate_threshold:
            return None

        route = self._routes[winner_route_idx]
        response = random.choice(route.responses) if route.responses else ""
        return SemanticMatchResult(
            route_name=route.name,
            intent=route.intent,
            response=response,
            confidence=min(winner_score, 1.0),
            matched_utterance=self._flat_utterances[winner_utt_idx],
            is_direct=winner_score >= direct_threshold,
        )


# 主人 2026-05-30: CSafeLoader (C-backed LibYAML, 5-10x faster than pure Python safe_load)
def _yaml_load(file_obj):
    try:
        return yaml.load(file_obj, Loader=yaml.CSafeLoader)
    except AttributeError:
        return yaml.safe_load(file_obj)


def load_routes_from_dir(routes_dir: str | Path) -> list[SemanticRoute]:
    routes_dir = Path(routes_dir)
    if not routes_dir.exists():
        logger.warning(f"[cpu_engine.L2] routes_dir not found: {routes_dir}")
        return []
    if not _HAS_YAML:
        logger.warning("[cpu_engine.L2] PyYAML not installed")
        return []

    routes: list[SemanticRoute] = []
    for yaml_path in sorted(routes_dir.glob("*.yaml")):
        try:
            with yaml_path.open(encoding="utf-8") as f:
                data = _yaml_load(f)
        except Exception as exc:
            logger.warning(f"[cpu_engine.L2] failed to load {yaml_path}: {exc}")
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            route = _parse_entry(entry, source=yaml_path.name)
            if route is not None:
                routes.append(route)

    return routes


def _parse_entry(entry: dict[str, Any], source: str) -> SemanticRoute | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    intent = entry.get("intent") or "default"
    utterances = entry.get("utterances") or []
    responses = entry.get("responses") or []
    if not name or not isinstance(name, str):
        return None
    if not utterances or not responses:
        return None

    return SemanticRoute(
        name=str(name),
        intent=str(intent),
        utterances=[str(u) for u in utterances if str(u).strip()],
        responses=[str(r) for r in responses if str(r).strip()],
        weight=float(entry.get("weight", 1.0)),
    )
