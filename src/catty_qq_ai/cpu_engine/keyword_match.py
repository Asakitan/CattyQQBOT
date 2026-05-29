"""L1 精确匹配 (升级版 keyword_reply).

特性:
- pyahocorasick 多关键词同时扫描 (C 速度), 失败 fallback 到 Python substring
- 支持子串和正则两种 pattern
- 每 route 独立冷却 (按 scope), 复用现 keyword_reply 思路
- 命中即 confidence=1.0 (精确匹配最高信心)

数据源: data/cpu_engine/routes/*.yaml (S1.10 生成)
每个 yaml 文件是一组 routes:

  - name: greet_morning_001
    keywords: [早安, 早上好, 早]
    regex: null
    disambiguate_context: [起床, 醒, 上班, 早餐]  # 可选: 冲突时用
    responses:
      - "早安主人～(蹭蹭) {cat_suffix}"
      - "嗷呜～主人早～{cat_suffix}"
    cooldown_seconds: 30
    weight: 1.0

冲突仲裁 (2026-05-30 R802 主人加):
- 当一条 user_text 同时命中 N≥2 个 routes 时, 不再单纯按 weight 取 winner
- 优先用每个 route 的 disambiguate_context 在原文中查命中数 (context_score)
- 排序 key = context_score * 2.0 + weight
- 800+ yaml 下"想哭/想躺平/失眠/心碎"等 keyword 撞 10+ routes, 走 context 消歧
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

try:
    import ahocorasick  # type: ignore

    _HAS_AHOCORASICK = True
except ImportError:
    _HAS_AHOCORASICK = False
    ahocorasick = None

try:
    import yaml  # type: ignore

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    yaml = None


@dataclass(slots=True)
class KeywordRoute:
    name: str
    keywords: list[str]
    responses: list[str]
    intent: str = "default"
    regex: re.Pattern[str] | None = None
    cooldown_seconds: float = 0.0
    weight: float = 1.0
    disambiguate_context: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KeywordMatchResult:
    route_name: str
    response: str
    confidence: float = 1.0
    matched_keyword: str = ""
    intent: str = "default"
    conflict_count: int = 1  # 同 user_text 命中候选数, 1 = 无冲突
    context_score: int = 0  # disambiguate_context 词命中数


class KeywordMatcher:
    """L1 精确匹配引擎.

    线程不安全 - 调用方 (cpu_engine 在 nonebot 事件循环里单线程) 自管.
    """

    def __init__(self, routes: list[KeywordRoute]) -> None:
        self._routes: dict[str, KeywordRoute] = {r.name: r for r in routes}
        self._automaton = self._build_automaton(routes) if _HAS_AHOCORASICK else None
        self._last_hit_at: dict[tuple[str, str], float] = {}  # (route_name, scope) -> ts
        logger.info(
            f"[cpu_engine.L1] loaded {len(routes)} routes, "
            f"aho-corasick={_HAS_AHOCORASICK}, total_keywords="
            f"{sum(len(r.keywords) for r in routes)}"
        )

    @staticmethod
    def _build_automaton(routes: list[KeywordRoute]) -> Any:
        a = ahocorasick.Automaton()
        for route in routes:
            for kw in route.keywords:
                kw_low = kw.lower()
                if not kw_low:
                    continue
                a.add_word(kw_low, (route.name, kw))
        a.make_automaton()
        return a

    def match(self, text: str, scope: str) -> KeywordMatchResult | None:
        if not text or not self._routes:
            return None
        text_low = text.lower()
        now = time.monotonic()

        candidates: list[tuple[KeywordRoute, str]] = []

        if self._automaton is not None:
            seen: set[str] = set()
            for _end, (route_name, matched_kw) in self._automaton.iter(text_low):
                if route_name in seen:
                    continue
                seen.add(route_name)
                route = self._routes.get(route_name)
                if route is None:
                    continue
                if self._on_cooldown(route, scope, now):
                    continue
                candidates.append((route, matched_kw))
        else:
            for route in self._routes.values():
                if self._on_cooldown(route, scope, now):
                    continue
                hit = self._fallback_substring(text_low, route)
                if hit is not None:
                    candidates.append((route, hit))

        for route in self._routes.values():
            if route.regex is None or self._on_cooldown(route, scope, now):
                continue
            if route.regex.search(text):
                candidates.append((route, route.regex.pattern))

        if not candidates:
            return None

        conflict_count = len(candidates)
        winner_route, winner_kw, winner_ctx_score = self._resolve_winner(
            candidates, text_low
        )

        self._last_hit_at[(winner_route.name, scope)] = now
        response = random.choice(winner_route.responses) if winner_route.responses else ""
        return KeywordMatchResult(
            route_name=winner_route.name,
            response=response,
            confidence=1.0,
            matched_keyword=winner_kw,
            intent=winner_route.intent,
            conflict_count=conflict_count,
            context_score=winner_ctx_score,
        )

    @staticmethod
    def _resolve_winner(
        candidates: list[tuple[KeywordRoute, str]],
        text_low: str,
    ) -> tuple[KeywordRoute, str, int]:
        # 无冲突直接返回, 省 context 扫描开销
        if len(candidates) == 1:
            return candidates[0][0], candidates[0][1], 0

        scored: list[tuple[float, int, KeywordRoute, str]] = []
        for route, kw in candidates:
            ctx_score = 0
            for ctx_kw in route.disambiguate_context:
                ctx_low = ctx_kw.lower()
                if ctx_low and ctx_low in text_low:
                    ctx_score += 1
            # context_score *2 优先, weight 二级排序
            total = ctx_score * 2.0 + route.weight
            scored.append((total, ctx_score, route, kw))

        scored.sort(key=lambda x: x[0], reverse=True)
        _, ctx_score, route, kw = scored[0]
        return route, kw, ctx_score

    def _on_cooldown(self, route: KeywordRoute, scope: str, now: float) -> bool:
        if route.cooldown_seconds <= 0:
            return False
        last = self._last_hit_at.get((route.name, scope))
        return last is not None and (now - last) < route.cooldown_seconds

    @staticmethod
    def _fallback_substring(text_low: str, route: KeywordRoute) -> str | None:
        for kw in route.keywords:
            if kw.lower() in text_low:
                return kw
        return None


def load_routes_from_dir(
    routes_dir: str | Path,
    disambiguate_overrides_path: str | Path | None = None,
) -> list[KeywordRoute]:
    """加载 routes. 可选 disambiguate_overrides 文件 merge 补丁.

    overrides 文件格式 (yaml dict, route_name -> context list):
        ia_cry_001: [面试, hr, 简历, offer]
        ps_cry_001: [宠物, 猫, 狗, 它]
    """
    routes_dir = Path(routes_dir)
    if not routes_dir.exists():
        logger.warning(f"[cpu_engine.L1] routes_dir not found: {routes_dir}")
        return []
    if not _HAS_YAML:
        logger.warning("[cpu_engine.L1] PyYAML not installed, cannot load routes")
        return []

    routes: list[KeywordRoute] = []
    for yaml_path in sorted(routes_dir.glob("*.yaml")):
        try:
            with yaml_path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            logger.warning(f"[cpu_engine.L1] failed to load {yaml_path}: {exc}")
            continue
        if not isinstance(data, list):
            logger.warning(f"[cpu_engine.L1] {yaml_path} root must be a list, got {type(data).__name__}")
            continue
        for entry in data:
            route = _parse_entry(entry, source=yaml_path.name)
            if route is not None:
                routes.append(route)

    if disambiguate_overrides_path is not None:
        overrides = _load_disambiguate_overrides(disambiguate_overrides_path)
        if overrides:
            applied = 0
            for r in routes:
                ctx = overrides.get(r.name)
                if ctx:
                    # merge: yaml 内显式定义 + overrides 不重复
                    seen = {c.lower() for c in r.disambiguate_context}
                    for c in ctx:
                        c_str = str(c).strip()
                        if c_str and c_str.lower() not in seen:
                            r.disambiguate_context.append(c_str)
                            seen.add(c_str.lower())
                    applied += 1
            logger.info(
                f"[cpu_engine.L1] disambiguate_overrides applied to {applied}/{len(routes)} routes "
                f"({len(overrides)} entries in overrides file)"
            )

    return routes


def _load_disambiguate_overrides(path: str | Path) -> dict[str, list[str]]:
    path = Path(path)
    if not path.exists():
        logger.debug(f"[cpu_engine.L1] disambiguate_overrides not found: {path}")
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        logger.warning(f"[cpu_engine.L1] failed to load disambiguate_overrides {path}: {exc}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"[cpu_engine.L1] disambiguate_overrides root must be dict, got {type(data).__name__}")
        return {}
    out: dict[str, list[str]] = {}
    for name, ctx in data.items():
        if not isinstance(name, str) or not isinstance(ctx, list):
            continue
        out[name] = [str(c) for c in ctx if str(c).strip()]
    return out


def _parse_entry(entry: dict[str, Any], source: str) -> KeywordRoute | None:
    if not isinstance(entry, dict):
        logger.warning(f"[cpu_engine.L1] {source}: entry must be dict, got {type(entry).__name__}")
        return None
    name = entry.get("name")
    keywords = entry.get("keywords") or []
    responses = entry.get("responses") or []
    regex_pattern = entry.get("regex")
    if not name or not isinstance(name, str):
        logger.warning(f"[cpu_engine.L1] {source}: entry missing 'name'")
        return None
    if not responses:
        logger.warning(f"[cpu_engine.L1] {source}/{name}: no responses, skipped")
        return None
    if not keywords and not regex_pattern:
        logger.warning(f"[cpu_engine.L1] {source}/{name}: need keywords or regex, skipped")
        return None

    regex_obj: re.Pattern[str] | None = None
    if regex_pattern:
        try:
            regex_obj = re.compile(regex_pattern)
        except re.error as exc:
            logger.warning(f"[cpu_engine.L1] {source}/{name}: invalid regex '{regex_pattern}': {exc}")
            return None

    disambiguate_context = entry.get("disambiguate_context") or []
    if not isinstance(disambiguate_context, list):
        disambiguate_context = []

    return KeywordRoute(
        name=name,
        keywords=[str(k) for k in keywords if str(k).strip()],
        responses=[str(r) for r in responses if str(r).strip()],
        intent=str(entry.get("intent", "default")),
        regex=regex_obj,
        cooldown_seconds=float(entry.get("cooldown_seconds", 0.0)),
        weight=float(entry.get("weight", 1.0)),
        disambiguate_context=[str(c) for c in disambiguate_context if str(c).strip()],
    )
