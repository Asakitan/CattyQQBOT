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
    layer: str  # "L1" / "L2" / "L3" / "L4" / "L4_catnify" / "L0_beg"
    route_name: str
    intent: str = ""
    matched_text: str = ""
    is_low_confidence: bool = False
    latency_ms: float = 0.0


@dataclass(slots=True)
class CatnifyOutput:
    """S5 catnify() 返回值. 调用方按字段决定后续动作:

    - ``styled`` 非 None → 用 styled 发回复
    - ``deepseek_reason`` 非空 → 走 L5 + 扣积分 (透传)
    - ``overload`` True → 队列满, 调用方按 fallback_on_fail 处理
    - 三者全空 → LLM 调用失败/超时, 调用方按 fallback_on_fail 处理
    """

    styled: "CPURouteResult | None"
    deepseek_reason: str
    overload: bool


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
        # 主人 2026-05-29 热重载: 记录 routes/ 目录下所有 yaml 的 mtime,
        # _routes_watch_loop 每 60s 扫描, 任何 mtime 变化触发 reload_routes().
        self._routes_mtime_signature: str = ""

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
            # S6 (主人 2026-05-29): live 蒸馏 corpus (DeepSeek 自动写入)
            live_corpus_path = getattr(
                self._config, "catty_cpu_engine_l3_distill_live_corpus_path", ""
            ) or None
            model_name = getattr(self._config, "catty_text2vec_model_name", "BAAI/bge-small-zh-v1.5")
            if corpus_path and index_path:
                self._txtai_corpus = TxtaiCorpus(
                    corpus_path,
                    index_path,
                    model_name=model_name,
                    live_corpus_path=live_corpus_path,
                )
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
        # 初始化 mtime signature, 后续 reload 用
        self._routes_mtime_signature = self._compute_routes_mtime_signature(routes_dir)
        logger.info(
            f"[cpu_engine] ready: L1={self._keyword_matcher is not None}, "
            f"L2={self._semantic_router is not None}, "
            f"L3={self._txtai_corpus is not None and self._txtai_corpus.ready}, "
            f"beg_pool={self._beg_pool.size if self._beg_pool else 0}"
        )

    @property
    def beg_pool(self) -> Any | None:
        return self._beg_pool

    @staticmethod
    def _compute_routes_mtime_signature(routes_dir: Path) -> str:
        """所有 yaml 的 (相对路径, mtime) 拼成签名串. 任何 yaml 变化 → 签名变."""
        if not routes_dir.exists():
            return ""
        parts: list[str] = []
        for yaml_path in sorted(routes_dir.glob("*.yaml")):
            try:
                mt = yaml_path.stat().st_mtime
                parts.append(f"{yaml_path.name}:{mt:.3f}")
            except OSError:
                continue
        return "|".join(parts)

    def reload_routes_if_changed(self) -> bool:
        """热重载: 检测 routes/ yaml mtime, 变了重建 L1+L2. 返回 True 表示真重载.

        主人 2026-05-29: 之前推 yaml 必须 touch __init__.py 重启 bot 才生效,
        现在改成后台 watch loop 每 60s 调一次, 零重启更新 routes.
        """
        if not self._enabled or not self._ready:
            return False
        routes_dir = Path(getattr(self._config, "catty_cpu_engine_routes_dir", ""))
        if not routes_dir.exists():
            return False
        new_sig = self._compute_routes_mtime_signature(routes_dir)
        if new_sig == self._routes_mtime_signature:
            return False
        old_sig = self._routes_mtime_signature
        logger.info(
            f"[cpu_engine.reload] routes mtime changed, hot-reload triggered. "
            f"old_files={old_sig.count('|')+1 if old_sig else 0} "
            f"new_files={new_sig.count('|')+1}"
        )
        # 重建 L1
        try:
            keyword_routes = load_kw_routes(routes_dir)
            new_matcher = KeywordMatcher(keyword_routes)
            self._keyword_matcher = new_matcher
        except Exception as exc:
            logger.warning(f"[cpu_engine.reload] L1 rebuild failed: {exc}")
        # 重建 L2 (重新 embed all utterances)
        try:
            from ..nlu.text2vec_engine import embed_sync_batch  # type: ignore
            semantic_routes = load_sem_routes(routes_dir)
            new_sr = SemanticRouter(semantic_routes, embed_batch_fn=embed_sync_batch)
            new_sr.prepare()
            self._semantic_router = new_sr
        except Exception as exc:
            logger.warning(f"[cpu_engine.reload] L2 rebuild failed: {exc}")
        self._routes_mtime_signature = new_sig
        logger.info(
            f"[cpu_engine.reload] done: L1={self._keyword_matcher is not None}, "
            f"L2={self._semantic_router is not None}"
        )
        return True

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

    def reload_l3_corpus(self) -> bool:
        """S6 (主人 2026-05-29) 给 corpus_distill 用: 累积 N 条后异步触发 L3 重 build.

        同步返回 (txtai.index 是 CPU bound, distill 在 asyncio.create_task 里调,
        不阻塞主链路). 失败保留旧 _ready 状态.
        """
        if self._txtai_corpus is None:
            return False
        return self._txtai_corpus.reload()

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

    async def catnify(
        self,
        candidate: CPURouteResult,
        user_text: str,
        scope: str,
        *,
        user_addr: str = "你",
        group_name: str = "",
        history: list[tuple[str, str]] | None = None,
        ctx_builder: Callable[[str], ScriptContext] | None = None,
    ) -> "CatnifyOutput":
        """S5 L4: 全量改写 candidate 成笨猫体质 + 自决 DeepSeek 透传.

        返回 CatnifyOutput:
        - deepseek_reason 非空 → 调用方走 L5 + 扣积分
        - styled 非 None → 调用方用 styled 直接发
        - 两者都为 None → fallback (主人 ``fallback_on_fail`` 配置决定: raw / transparent)

        和 stylize_l4 的差异:
        - 这里**不**按 scope 区分 enabled (主人决策: 全量上)
        - 队列满/超时返回 overload=True, 调用方按 fallback 走
        """
        if not self._enabled or candidate is None:
            return CatnifyOutput(styled=None, deepseek_reason="", overload=False)

        try:
            from .catnify_rewrite import catnify_rewrite
            from .catnify_queue import get_queue, CatnifyOverload
        except ImportError as exc:
            logger.warning(f"[cpu_engine.L4_catnify] import failed: {exc}")
            return CatnifyOutput(styled=None, deepseek_reason="", overload=False)

        queue = get_queue(self._config)
        if not queue.can_accept():
            logger.info(f"[cpu_engine.L4_catnify] queue overload, stats={queue.stats}")
            return CatnifyOutput(styled=None, deepseek_reason="", overload=True)

        scope_type = scope.split(":", 1)[0] if ":" in scope else "private"
        t0 = time.monotonic()
        try:
            async with queue.slot():
                result = await catnify_rewrite(
                    config=self._config,
                    candidate=candidate.reply,
                    user_text=user_text,
                    user_addr=user_addr,
                    scope_type=scope_type,
                    group_name=group_name,
                    history=history,
                )
        except CatnifyOverload:
            return CatnifyOutput(styled=None, deepseek_reason="", overload=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cpu_engine.L4_catnify] raised: {exc}")
            return CatnifyOutput(styled=None, deepseek_reason="", overload=False)

        elapsed = (time.monotonic() - t0) * 1000.0
        if result is None:
            return CatnifyOutput(styled=None, deepseek_reason="", overload=False)
        if result.deepseek_reason:
            return CatnifyOutput(
                styled=None,
                deepseek_reason=result.deepseek_reason,
                overload=False,
            )
        rendered = self._render(result.text, candidate.intent, ctx_builder)
        styled_result = CPURouteResult(
            reply=rendered,
            confidence=max(candidate.confidence, 0.85),
            layer="L4_catnify",
            route_name=candidate.route_name,
            intent=candidate.intent,
            matched_text=candidate.matched_text,
            is_low_confidence=False,
            latency_ms=candidate.latency_ms + elapsed,
        )
        return CatnifyOutput(styled=styled_result, deepseek_reason="", overload=False)

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
