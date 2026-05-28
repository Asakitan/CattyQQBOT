"""L2 Semantic Router 包装.

每条 route 挂多个 utterances (示例问句) + responses (米雪儿语气回答).
启动时 bge embed_sync_batch 把所有 utterances 算成一个 flat 矩阵 (N_total, dim),
查询时 query @ flat.T 一次拿全部分数, 按 route 聚合 max -> top1 route.

阈值:
- score >= direct_threshold (默认 0.82): 直答, confidence=score
- candidate_threshold <= score < direct_threshold (默认 0.70-0.82): 标低信心进 L4 风格化
- score < candidate_threshold: 透传到 L3

无 Semantic Router 第三方库依赖: 直接 numpy + text2vec_engine.embed_sync_batch,
原因是 semantic-router 0.1.x 自带 encoder 抽象偏服务侧 (OpenAI/Cohere),
和现有 bge ONNX 本地 stack 适配起来反而多一层. 自己 100 行实现更轻.
"""

from __future__ import annotations

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
    """L2: bge embed 找最近 route."""

    def __init__(self, routes: list[SemanticRoute], embed_batch_fn: EmbedBatchFn) -> None:
        self._routes: list[SemanticRoute] = list(routes)
        self._embed_batch_fn = embed_batch_fn
        self._flat_utterances: list[str] = []
        self._flat_route_idx: list[int] = []
        self._flat_embeddings: np.ndarray | None = None
        self._prepared = False

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

        if not self._flat_utterances:
            logger.warning("[cpu_engine.L2] no utterances across routes")
            self._prepared = True
            return False

        embeddings = self._embed_batch_fn(self._flat_utterances)
        if embeddings is None:
            logger.error(
                f"[cpu_engine.L2] embed_batch_fn returned None for "
                f"{len(self._flat_utterances)} utterances, L2 disabled"
            )
            self._prepared = True
            return False

        if embeddings.shape[0] != len(self._flat_utterances):
            logger.error(
                f"[cpu_engine.L2] embed shape mismatch: got {embeddings.shape[0]}, "
                f"expected {len(self._flat_utterances)}"
            )
            self._prepared = True
            return False

        self._flat_embeddings = embeddings.astype(np.float32, copy=False)
        self._prepared = True
        logger.info(
            f"[cpu_engine.L2] prepared {len(self._routes)} routes, "
            f"{len(self._flat_utterances)} utterances, "
            f"embed dim={self._flat_embeddings.shape[1]}"
        )
        return True

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
                data = yaml.safe_load(f)
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
