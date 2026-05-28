"""通用快速回复模板池 (S5).

把散落在 __init__.py / legs_picker.py / tools.py 里的硬编码 fallback 文案
统一到 data/cpu_engine/replies/*.yaml. 每个 yaml 是按"上下文桶"分组的模板列表.

yaml 格式 (同 BegTemplatePool 风格):
    - name: signin_lv0_2_first
      level_min: 0          # 触发桶: 等级 [min, max]
      level_max: 2
      already_signed: false # 触发桶: 是否已签 (None = 不过滤)
      is_owner: null         # 触发桶: 是否主人 (None = 不过滤)
      weight: 1.0
      templates:
        - "嗷呜～{user_nickname}签到啦~ +{gained}积分 余额{balance}{cat_suffix}"

接口:
    pool = load_pool_from_dir("data/cpu_engine/replies", category="signin")
    text = pool.pick(level=5, already_signed=False, is_owner=False, vars={"gained": 200, ...})
    # text 已经经过 script_ctx.render 处理变量, 失败返回 None 让调用方走原硬编码 fallback

调用 fallback 保留: pool.pick() 返回 None 时调用方继续用原硬编码 (双层安全网).
"""

from __future__ import annotations

import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Optional

from loguru import logger


# 主人 2026-05-29 v2 算法: 防机械感.
# A) Recently-used 抑制: 同 user 同 category 最近 N 次用过的 (bucket, template_idx)
#    队列, pick 时降权. 同 bucket 再选 template 时直接排除最近 idx.
# B) Time-of-day 加权: 桶可声明 time_of_day, 命中当前时段 weight *= 1.5.
# C) Emotion 适配: 桶可声明 emotion_min/max, 用户消息情绪强度需落区间.
_RECENT_PICKS: dict[tuple[str, str], Deque[tuple[str, int]]] = defaultdict(
    lambda: deque(maxlen=6)
)
_RECENT_BUCKET_PENALTY = 0.15  # 最近用过的 bucket weight 乘这个
_TIME_OF_DAY_BOOST = 1.6  # 时段匹配 weight 乘这个

try:
    import yaml  # type: ignore

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    yaml = None

from .script_ctx import ScriptContext, render as render_template


@dataclass(slots=True)
class QuickReplyBucket:
    name: str
    templates: list[str]
    level_min: int = -1
    level_max: int = 999
    already_signed: Optional[bool] = None
    is_owner: Optional[bool] = None
    scope_type: Optional[str] = None  # private / group / None
    time_of_day: Optional[str] = None  # morning/noon/afternoon/evening/night/late_night
    emotion_min: float = 0.0  # 情绪强度下限 0.0-1.0
    emotion_max: float = 1.0  # 上限
    weight: float = 1.0
    extra_filters: dict[str, Any] = field(default_factory=dict)  # 任意精确匹配字段


def _current_time_of_day() -> str:
    """与 script_ctx._time_of_day_for 一致, 但加 late_night 细分."""
    hour = datetime.now().hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 14:
        return "noon"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    if hour >= 23 or hour < 2:
        return "late_night"
    return "night"


@dataclass(slots=True)
class QuickReplyPool:
    category: str
    buckets: list[QuickReplyBucket]

    @property
    def size(self) -> int:
        return len(self.buckets)

    @property
    def total_templates(self) -> int:
        return sum(len(b.templates) for b in self.buckets)

    def pick(
        self,
        *,
        level: int | None = None,
        already_signed: Optional[bool] = None,
        is_owner: Optional[bool] = None,
        scope_type: Optional[str] = None,
        extra: dict[str, Any] | None = None,
        ctx: ScriptContext | None = None,
        cat_suffixes: list[str] | None = None,
        render_vars: dict[str, Any] | None = None,
        # v2 算法参数
        user_id: str = "",
        emotion_intensity: float = 0.0,
        time_of_day: str | None = None,
    ) -> str | None:
        """加 3 个算法: A) 最近用过的桶/模板抑制 B) 时段加权 C) 情绪适配.

        无匹配/无模板返回 None. user_id 为空时仅启用 B+C, 不启用 A.
        """
        now_tod = time_of_day or _current_time_of_day()

        candidates: list[QuickReplyBucket] = []
        for bucket in self.buckets:
            if not bucket.templates:
                continue
            if level is not None and not (bucket.level_min <= level <= bucket.level_max):
                continue
            if bucket.already_signed is not None and already_signed is not None:
                if bucket.already_signed != already_signed:
                    continue
            if bucket.is_owner is not None and is_owner is not None:
                if bucket.is_owner != is_owner:
                    continue
            if bucket.scope_type is not None and scope_type is not None:
                if bucket.scope_type != scope_type:
                    continue
            if not (bucket.emotion_min <= emotion_intensity <= bucket.emotion_max):
                continue
            if bucket.extra_filters and extra:
                if not all(extra.get(k) == v for k, v in bucket.extra_filters.items()):
                    continue
            candidates.append(bucket)

        if not candidates:
            return None

        # A: 最近用过的 bucket 降权
        recent_key = (user_id, self.category) if user_id else None
        recent_picks = _RECENT_PICKS[recent_key] if recent_key else deque()
        recent_buckets = {b for b, _ in recent_picks}
        recent_template_per_bucket: dict[str, set[int]] = defaultdict(set)
        for b, idx in recent_picks:
            recent_template_per_bucket[b].add(idx)

        weights: list[float] = []
        for b in candidates:
            w = max(b.weight, 0.0001)
            # B: 时段匹配加权
            if b.time_of_day is not None and b.time_of_day == now_tod:
                w *= _TIME_OF_DAY_BOOST
            # A: 最近 bucket 降权
            if b.name in recent_buckets:
                w *= _RECENT_BUCKET_PENALTY
            weights.append(w)

        winner = random.choices(candidates, weights=weights, k=1)[0]

        # A 第二层: 同 bucket 内排除最近用过的 template idx
        used_idx = recent_template_per_bucket.get(winner.name, set())
        available_idx = [i for i in range(len(winner.templates)) if i not in used_idx]
        if not available_idx:  # 全用过了, 整桶都允许 (避免无模板可选)
            available_idx = list(range(len(winner.templates)))
        chosen_idx = random.choice(available_idx)
        template = winner.templates[chosen_idx]

        # 记录这次 pick
        if recent_key is not None:
            _RECENT_PICKS[recent_key].append((winner.name, chosen_idx))

        return self._render(template, ctx, cat_suffixes, render_vars)

    @staticmethod
    def _render(
        template: str,
        ctx: ScriptContext | None,
        cat_suffixes: list[str] | None,
        extra_vars: dict[str, Any] | None,
    ) -> str:
        # 主人 2026-05-29 fix: extra_vars 优先级要高于 ctx 默认值, 否则
        # render_vars={'user_nickname':'小明'} 会被 ctx.user_nickname='主人' 先吃掉.
        # 用 ctx 的 nickname 仅作为最终兜底 (extra_vars 没给时).
        if ctx is not None and extra_vars is not None:
            extra_vars = {**ctx.to_render_dict(""), **extra_vars}  # extra_vars 后覆盖
        if ctx is None:
            ctx = ScriptContext(
                user_id="",
                user_nickname=str((extra_vars or {}).get("user_nickname", "主人")),
                scope_type="private",
                intent="quick_reply",
            )
        elif extra_vars and "user_nickname" in extra_vars:
            # 主人 fix: 让 render_template 也看到 extra_vars 的 nickname
            ctx = ScriptContext(
                user_id=ctx.user_id,
                user_nickname=str(extra_vars["user_nickname"]),
                scope_type=ctx.scope_type,
                intent=ctx.intent,
                affection_level=ctx.affection_level,
                affection_title=ctx.affection_title,
                time_of_day=ctx.time_of_day,
                user_vibe=ctx.user_vibe,
                group_lore=ctx.group_lore,
            )
        rendered = render_template(template, ctx, cat_suffixes)
        if extra_vars:
            try:
                rendered = rendered.format_map(_SafeMap(extra_vars))
            except Exception:  # noqa: BLE001
                pass
        return rendered


class _SafeMap(dict):
    """str.format_map fallback - 缺 key 保留 {xxx} 字面不抛."""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def load_pool_from_dir(replies_dir: str | Path, *, category: str) -> QuickReplyPool:
    """加载 data/cpu_engine/replies/{category}.yaml. 不存在或解析失败返回空 pool."""
    replies_dir = Path(replies_dir)
    yaml_path = replies_dir / f"{category}.yaml"
    if not yaml_path.exists():
        logger.warning(f"[quick_reply] {yaml_path} not found, pool empty")
        return QuickReplyPool(category=category, buckets=[])
    if not _HAS_YAML:
        logger.warning("[quick_reply] PyYAML not installed")
        return QuickReplyPool(category=category, buckets=[])

    try:
        with yaml_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[quick_reply] failed to load {yaml_path}: {exc}")
        return QuickReplyPool(category=category, buckets=[])

    if not isinstance(data, list):
        logger.warning(f"[quick_reply] {yaml_path} root must be a list")
        return QuickReplyPool(category=category, buckets=[])

    buckets: list[QuickReplyBucket] = []
    for entry in data:
        bucket = _parse_entry(entry, source=yaml_path.name)
        if bucket is not None:
            buckets.append(bucket)

    logger.info(
        f"[quick_reply.{category}] loaded {len(buckets)} buckets, "
        f"total_templates={sum(len(b.templates) for b in buckets)}"
    )
    return QuickReplyPool(category=category, buckets=buckets)


def _parse_entry(entry: dict[str, Any], source: str) -> QuickReplyBucket | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    templates = entry.get("templates") or []
    if not name or not isinstance(name, str) or not templates:
        return None

    is_owner_raw = entry.get("is_owner")
    already_raw = entry.get("already_signed")
    scope_raw = entry.get("scope_type")

    known = {"name", "templates", "level_min", "level_max", "already_signed",
             "is_owner", "scope_type", "weight", "time_of_day", "emotion_min",
             "emotion_max", "_note"}
    extra_filters = {k: v for k, v in entry.items() if k not in known}

    tod_raw = entry.get("time_of_day")

    try:
        return QuickReplyBucket(
            name=str(name),
            templates=[str(t) for t in templates if str(t).strip()],
            level_min=int(entry.get("level_min", -1)),
            level_max=int(entry.get("level_max", 999)),
            already_signed=bool(already_raw) if isinstance(already_raw, bool) else None,
            is_owner=bool(is_owner_raw) if isinstance(is_owner_raw, bool) else None,
            scope_type=str(scope_raw) if isinstance(scope_raw, str) else None,
            time_of_day=str(tod_raw) if isinstance(tod_raw, str) else None,
            emotion_min=float(entry.get("emotion_min", 0.0)),
            emotion_max=float(entry.get("emotion_max", 1.0)),
            weight=float(entry.get("weight", 1.0)),
            extra_filters=extra_filters,
        )
    except (TypeError, ValueError) as exc:
        logger.warning(f"[quick_reply] {source}/{name}: parse error {exc}")
        return None


_GLOBAL_POOLS: dict[str, QuickReplyPool] = {}
# 主人 2026-05-29 热重载: 记录 yaml mtime, 变化时 reset 单例.
_POOL_MTIME: dict[str, float] = {}


def get_pool(replies_dir: str | Path, category: str) -> QuickReplyPool:
    """惰性单例. 主人 2026-05-29 热重载: lazy 检查 yaml mtime, 变了重新 load."""
    key = f"{replies_dir}::{category}"
    yaml_path = Path(replies_dir) / f"{category}.yaml"
    try:
        cur_mtime = yaml_path.stat().st_mtime
    except OSError:
        cur_mtime = 0.0
    cached_mtime = _POOL_MTIME.get(key, -1.0)
    if key not in _GLOBAL_POOLS or cur_mtime != cached_mtime:
        pool = load_pool_from_dir(replies_dir, category=category)
        _GLOBAL_POOLS[key] = pool
        _POOL_MTIME[key] = cur_mtime
    return _GLOBAL_POOLS[key]


def reset_pools_for_test() -> None:
    _GLOBAL_POOLS.clear()
    _POOL_MTIME.clear()
