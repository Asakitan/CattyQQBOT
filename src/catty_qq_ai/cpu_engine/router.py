"""CPU 引擎顶层协调器 (六判定点).

挂在 nonebot priority=37 matcher (legs_picture=35 之后, keyword_reply=40 之前),
命中即 finish, miss 则透传给现 keyword_reply / handle_chat 链路.

判定点 (按顺序):
1. enabled / 硬拦不在此处理 (上游已拦)
2. 主人 #ai/#aikey/#refresh 强制透传
3. L1 精确匹配 → 命中 confidence=1.0
4. L2 Semantic Router → direct≥0.82 直答; candidate≥0.70 标低信心
5. (S2 接入) L3 txtai 大语料召回
6. (S3 接入) 强互动判定 + 积分门控 + L0_beg 撒娇求充值

S1 仅实现 1-4, miss 全部透传到主链路.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from .corpus_txtai import CorpusMatchResult, TxtaiCorpus
from .keyword_match import KeywordMatcher, load_routes_from_dir as load_kw_routes
from .normalize import normalize, NormalizedText
from .script_ctx import ScriptContext, render as render_template
from .semantic_route import (
    SemanticMatchResult,
    SemanticRouter,
    load_routes_from_dir as load_sem_routes,
)


@dataclass(slots=True)
class CPURouteResult:
    reply: str
    confidence: float
    layer: str  # "L1" / "L2" / "L3" / "L4"
    route_name: str
    intent: str = ""
    matched_text: str = ""
    is_low_confidence: bool = False
    latency_ms: float = 0.0


class CPUEngineRouter:
    """有状态的协调器: 启动时 prepare(), 每条消息调 route_sync()."""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._enabled: bool = bool(getattr(config, "catty_cpu_engine_enabled", False))
        self._keyword_matcher: KeywordMatcher | None = None
        self._semantic_router: SemanticRouter | None = None
        self._txtai_corpus: TxtaiCorpus | None = None
        self._beg_pool: Any | None = None  # BegTemplatePool, lazy import in prepare
        self._ready: bool = False
        self._cat_suffixes: list[str] = list(
            getattr(config, "catty_cpu_engine_cat_suffixes", []) or []
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def ready(self) -> bool:
        return self._ready

    def prepare(self) -> None:
        """启动时调用. 失败仅 warn 不抛, 让 bot 仍能跑."""
        if not self._enabled:
            logger.info("[cpu_engine] disabled (catty_cpu_engine_enabled=False), skip prepare")
            return

        routes_dir = Path(getattr(self._config, "catty_cpu_engine_routes_dir", "src/catty_qq_ai/data/cpu_engine/routes"))
        if not routes_dir.exists():
            logger.warning(f"[cpu_engine] routes_dir not found: {routes_dir}, engine NOT ready")
            return

        try:
            keyword_routes = load_kw_routes(routes_dir)
            self._keyword_matcher = KeywordMatcher(keyword_routes)
        except Exception as exc:
            logger.exception(f"[cpu_engine] L1 keyword matcher init failed: {exc}")
            self._keyword_matcher = None

        try:
            from ..nlu.text2vec_engine import embed_sync_batch  # type: ignore

            semantic_routes = load_sem_routes(routes_dir)
            self._semantic_router = SemanticRouter(semantic_routes, embed_batch_fn=embed_sync_batch)
            self._semantic_router.prepare()
        except Exception as exc:
            logger.exception(f"[cpu_engine] L2 semantic router init failed: {exc}")
            self._semantic_router = None

        try:
            corpus_path = getattr(self._config, "catty_cpu_engine_corpus_path", "")
            index_path = getattr(self._config, "catty_cpu_engine_txtai_index_path", "")
            model_name = getattr(self._config, "catty_text2vec_model_name", "BAAI/bge-small-zh-v1.5")
            if corpus_path and index_path:
                self._txtai_corpus = TxtaiCorpus(corpus_path, index_path, model_name=model_name)
                self._txtai_corpus.prepare()
                if not self._txtai_corpus.ready:
                    self._txtai_corpus = None
        except Exception as exc:
            logger.exception(f"[cpu_engine] L3 txtai corpus init failed: {exc}")
            self._txtai_corpus = None

        try:
            from .beg_template import load_pool_from_dir as _load_beg_pool

            beg_dir = Path(getattr(self._config, "catty_cpu_engine_routes_dir", "")).parent / "beg"
            if beg_dir.exists():
                self._beg_pool = _load_beg_pool(beg_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cpu_engine] beg pool init failed: {exc}")
            self._beg_pool = None

        if self._keyword_matcher is None and self._semantic_router is None and self._txtai_corpus is None:
            logger.warning("[cpu_engine] L1/L2/L3 all failed, engine NOT ready")
            return

        self._ready = True
        logger.info(
            f"[cpu_engine] ready: L1={self._keyword_matcher is not None}, "
            f"L2={self._semantic_router is not None}, "
            f"L3={self._txtai_corpus is not None and self._txtai_corpus.ready}, "
            f"beg_pool={self._beg_pool.size if self._beg_pool else 0}"
        )

    @property
    def beg_pool(self) -> Any | None:
        return self._beg_pool

    def route_sync(
        self,
        text: str,
        scope: str,
        *,
        is_owner: bool = False,
        ctx_builder: Callable[[str], ScriptContext] | None = None,
    ) -> CPURouteResult | None:
        """同步试跑 L1->L2->L3, 返回 None 表示 CPU 全 miss 应透传现链路.

        命中 L1 或 L2/L3 direct (score>=direct_threshold): 返回 is_low_confidence=False,
        调用方直接 emit.

        L2/L3 mid candidate (candidate<=score<direct): 返回 is_low_confidence=True,
        调用方应 await stylize_l4() 尝试风格化, 失败则透传.

        S3 调用方再做强互动判定 + 积分门控 + L0_beg 分流.
        """
        if not self._enabled or not self._ready:
            return None
        if not text:
            return None

        force_prefixes = getattr(self._config, "catty_cpu_engine_force_ai_prefixes", [])
        text_strip = text.lstrip()
        if any(text_strip.startswith(p) for p in force_prefixes):
            return None

        t0 = time.monotonic()
        norm = normalize(text)
        if norm.has_image or norm.has_voice:
            return None

        scope_type = scope.split(":", 1)[0] if ":" in scope else "private"

        if self._keyword_matcher is not None:
            l1 = self._keyword_matcher.match(norm.cleaned, scope)
            if l1 is not None:
                rendered = self._render(l1.response, l1.intent, ctx_builder)
                return CPURouteResult(
                    reply=rendered,
                    confidence=1.0,
                    layer="L1",
                    route_name=l1.route_name,
                    intent=l1.intent,
                    matched_text=l1.matched_keyword,
                    is_low_confidence=False,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                )

        candidates: list[CPURouteResult] = []

        if self._semantic_router is not None:
            direct_thr, candidate_thr = self._get_l2_thresholds(scope_type)
            l2 = self._match_l2(norm.cleaned, direct_thr, candidate_thr)
            if l2 is not None:
                rendered = self._render(l2.response, l2.intent, ctx_builder)
                if l2.is_direct:
                    return CPURouteResult(
                        reply=rendered,
                        confidence=l2.confidence,
                        layer="L2",
                        route_name=l2.route_name,
                        intent=l2.intent,
                        matched_text=l2.matched_utterance,
                        is_low_confidence=False,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                    )
                candidates.append(
                    CPURouteResult(
                        reply=l2.response,  # 未渲染 raw, 留给 L4 风格化
                        confidence=l2.confidence,
                        layer="L2",
                        route_name=l2.route_name,
                        intent=l2.intent,
                        matched_text=l2.matched_utterance,
                        is_low_confidence=True,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                    )
                )

        if self._txtai_corpus is not None and self._txtai_corpus.ready:
            direct_thr, candidate_thr = self._get_l3_thresholds(scope_type)
            l3 = self._txtai_corpus.match(
                norm.cleaned,
                direct_threshold=direct_thr,
                candidate_threshold=candidate_thr,
            )
            if l3 is not None:
                if l3.is_direct:
                    rendered = self._render(l3.a, l3.intent, ctx_builder)
                    return CPURouteResult(
                        reply=rendered,
                        confidence=l3.score,
                        layer="L3",
                        route_name=f"corpus:{l3.qid}",
                        intent=l3.intent,
                        matched_text=l3.q,
                        is_low_confidence=False,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                    )
                candidates.append(
                    CPURouteResult(
                        reply=l3.a,  # raw
                        confidence=l3.score,
                        layer="L3",
                        route_name=f"corpus:{l3.qid}",
                        intent=l3.intent,
                        matched_text=l3.q,
                        is_low_confidence=True,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                    )
                )

        if not candidates:
            return None
        return max(candidates, key=lambda r: r.confidence)

    async def stylize_l4(
        self,
        candidate: CPURouteResult,
        user_text: str,
        scope: str,
        *,
        ctx_builder: Callable[[str], ScriptContext] | None = None,
    ) -> CPURouteResult | None:
        """L4 风格化: 用 Ollama 把 candidate.reply 改写为米雪儿语气.

        失败/超时/L4 被 scope 禁用时返回 None, 调用方应透传到 L5.
        """
        if not self._enabled or candidate is None:
            return None
        scope_type = scope.split(":", 1)[0] if ":" in scope else "private"
        if scope_type == "private":
            enabled = bool(getattr(self._config, "catty_cpu_engine_l4_enabled_private", True))
        else:
            enabled = bool(getattr(self._config, "catty_cpu_engine_l4_enabled_group", False))
        if not enabled:
            return None

        try:
            from .ollama_stylize import stylize_candidate
        except ImportError as exc:
            logger.warning(f"[cpu_engine.L4] ollama_stylize import failed: {exc}")
            return None

        t0 = time.monotonic()
        styled = await stylize_candidate(
            config=self._config,
            candidate=candidate.reply,
            user_text=user_text,
        )
        if not styled:
            return None
        rendered = self._render(styled, candidate.intent, ctx_builder)
        return CPURouteResult(
            reply=rendered,
            confidence=max(candidate.confidence, 0.8),
            layer="L4",
            route_name=candidate.route_name,
            intent=candidate.intent,
            matched_text=candidate.matched_text,
            is_low_confidence=False,
            latency_ms=candidate.latency_ms + (time.monotonic() - t0) * 1000.0,
        )

    def _render(
        self,
        template: str,
        intent: str,
        ctx_builder: Callable[[str], ScriptContext] | None,
    ) -> str:
        if ctx_builder is None:
            return render_template(template, _empty_ctx(intent), self._cat_suffixes)
        try:
            ctx = ctx_builder(intent)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cpu_engine] ctx_builder raised: {exc}")
            ctx = _empty_ctx(intent)
        return render_template(template, ctx, self._cat_suffixes)

    def _get_l3_thresholds(self, scope_type: str) -> tuple[float, float]:
        direct = float(getattr(self._config, "catty_cpu_engine_l2_threshold_direct", 0.82))
        candidate = float(getattr(self._config, "catty_cpu_engine_l2_threshold_candidate", 0.70))
        if scope_type == "group":
            bonus = float(getattr(self._config, "catty_cpu_engine_group_threshold_bonus", 0.03))
            direct += bonus
            candidate += bonus
        return direct, candidate

    def _get_l2_thresholds(self, scope_type: str) -> tuple[float, float]:
        direct = float(getattr(self._config, "catty_cpu_engine_l2_threshold_direct", 0.82))
        candidate = float(getattr(self._config, "catty_cpu_engine_l2_threshold_candidate", 0.70))
        if scope_type == "group":
            bonus = float(getattr(self._config, "catty_cpu_engine_group_threshold_bonus", 0.03))
            direct += bonus
            candidate += bonus
        return direct, candidate

    def _match_l2(self, text: str, direct_thr: float, candidate_thr: float) -> SemanticMatchResult | None:
        if self._semantic_router is None:
            return None
        try:
            from ..nlu.text2vec_engine import embed_sync  # type: ignore
        except ImportError:
            return None
        return self._semantic_router.match(
            text,
            embed_query_fn=embed_sync,
            direct_threshold=direct_thr,
            candidate_threshold=candidate_thr,
        )


def _empty_ctx(intent: str) -> ScriptContext:
    return ScriptContext(
        user_id="",
        user_nickname="主人",
        scope_type="private",
        intent=intent or "default",
    )


_GLOBAL_ROUTER: CPUEngineRouter | None = None


def get_router(config: Any) -> CPUEngineRouter:
    """惰性单例. 第一次调 prepare(), 后续直接拿."""
    global _GLOBAL_ROUTER
    if _GLOBAL_ROUTER is None:
        _GLOBAL_ROUTER = CPUEngineRouter(config)
        _GLOBAL_ROUTER.prepare()
    return _GLOBAL_ROUTER


def reset_router_for_test() -> None:
    """测试用: 清单例重新初始化."""
    global _GLOBAL_ROUTER
    _GLOBAL_ROUTER = None
