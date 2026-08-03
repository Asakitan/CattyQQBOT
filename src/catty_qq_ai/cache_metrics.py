"""Provider/model cache metrics plus isolated cohort diagnostics."""
from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_TARGET_MIN = 0.95
_ROLLING_MAXLEN = 20
_ROLLING: dict[str, deque[tuple[int, int]]] = {}
_LOW_STREAK: dict[str, int] = {}
_MAX_BUCKETS = 64

_HOT99_TARGET = 0.99
_HOT99_MIN_ELIGIBLE_SAMPLES = 10
_HOT99_MIN_PROVIDER_INPUT_TOKENS = 100_000
_HOT99_USER_FACING_REQUEST_KINDS = frozenset({
    "chat",
    "chat_followup",
    "tool_initial",
    "tool_followup",
})
_HOT99_USER_FACING_REQUEST_CLASSES = frozenset({
    "chat",
})

# Cohort buckets are deliberately separate from the established provider|model
# rolling buckets so legacy KPI output is not influenced by diagnostic slicing.
_COHORT_ROLLING: dict[str, deque[tuple[int, int, int, int]]] = {}
_COHORT_METADATA: dict[str, dict[str, str | int]] = {}
_COHORT_LAST: dict[str, dict[str, int | float | str | None]] = {}
_COHORT_MAX_BUCKETS = 256
# 冻结 cohort 维度 (2026-08-03 Review 定案): warm / anchor_changed /
# anchor_observed / request_class 是 eligibility 元数据, 不进 key — 否则同一
# 稳定热 cohort 会被状态标志切碎, 10 样本门槛永远凑不齐.
_COHORT_FIELDS = (
    "provider",
    "model",
    "route",
    "prompt_variant",
    "persona",
    "scope_type",
    "tool_set_hash",
    "trim_epoch",
    "request_kind",
)


@dataclass
class RollingStats:
    this_rate: float
    roll_rate: float
    roll_n: int
    status: str
    should_warn: bool


@dataclass
class CohortRollingStats:
    cohort_key: str
    metadata: dict[str, str | int]
    this_rate: float
    normalized_rate: float | None
    roll_rate: float
    rolling_normalized_rate: float | None
    roll_n: int
    hit_tok: int
    miss_tok: int
    create_tok: int
    unavoidable_current_turn_tokens: int
    eligible_tokens: int
    hot99_eligible: int = 0
    hot99_eligible_count: int = 0
    hot99_raw_rate: float | None = None
    hot99_status: str = "N/A"
    hot99_target: float = _HOT99_TARGET


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonicalize(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    """Return compact, key-order-independent JSON for diagnostic hashing."""
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    """Return the full SHA256 of ``canonical_json(value)``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _nonnegative_tokens(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def actual_hit_rate(hit_tok: Any, miss_tok: Any, create_tok: Any = 0) -> float:
    """Compute a rate from provider-reported usage only."""
    hit = _nonnegative_tokens(hit_tok)
    total = hit + _nonnegative_tokens(miss_tok) + _nonnegative_tokens(create_tok)
    return (hit / total) if total > 0 else 0.0


def normalized_cache_kpi(
    hit_tok: Any,
    miss_tok: Any,
    create_tok: Any = 0,
    *,
    unavoidable_current_turn_tokens: Any = None,
) -> float | None:
    """Compute the cacheable-token KPI without manufacturing a cache hit.

    Only a value explicitly supplied by the caller is excluded. If no token is
    eligible for caching, ``None`` is returned instead of a synthetic 100%.
    """
    hit = _nonnegative_tokens(hit_tok)
    total = hit + _nonnegative_tokens(miss_tok) + _nonnegative_tokens(create_tok)
    unavoidable = _nonnegative_tokens(unavoidable_current_turn_tokens)
    eligible = max(total - min(unavoidable, total), 0)
    if eligible <= 0:
        return None
    return min(hit / eligible, 1.0)


def normalized_hit_rate(
    hit_tok: Any,
    miss_tok: Any,
    create_tok: Any = 0,
    *,
    unavoidable_current_turn_tokens: Any = None,
) -> float | None:
    """Semantic alias for ``normalized_cache_kpi``."""
    return normalized_cache_kpi(
        hit_tok,
        miss_tok,
        create_tok,
        unavoidable_current_turn_tokens=unavoidable_current_turn_tokens,
    )


def rolling_cache_efficiency(
    samples: Iterable[tuple[Any, Any, Any]],
) -> tuple[float, float | None, int]:
    """Return token-weighted actual and cacheable rates for rolling samples.

    Every sample is ``(hit_tokens, input_total_tokens, eligible_tokens)``.
    ``eligible_tokens`` is already adjusted for explicitly-known, unavoidable
    current-turn input so the normalized value never invents a cache hit.
    """
    hit_total = 0
    input_total = 0
    eligible_total = 0
    count = 0
    for hit_tok, total_tok, eligible_tok in samples:
        hit = _nonnegative_tokens(hit_tok)
        total = _nonnegative_tokens(total_tok)
        eligible = min(_nonnegative_tokens(eligible_tok), total)
        hit_total += hit
        input_total += total
        eligible_total += eligible
        count += 1
    actual = (hit_total / input_total) if input_total > 0 else 0.0
    normalized = (
        min(hit_total / eligible_total, 1.0)
        if eligible_total > 0
        else None
    )
    return actual, normalized, count


def _cohort_text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "-"


def _cohort_flag(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "on"} else 0
    return 1 if bool(value) else 0


def _cohort_epoch(value: Any) -> int:
    return _nonnegative_tokens(value)


def _cohort_request_label(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip().lower()
    if text in {"", "-", "none", "null"}:
        return default
    return text.replace("-", "_").replace(" ", "_")


def _cohort_request_kind(value: Any) -> str:
    return _cohort_request_label(value, default="chat")


def build_cohort_metadata(
    *,
    provider: Any,
    model: Any,
    route: Any,
    scope_type: Any,
    persona: Any,
    warm: Any,
    tool_set_hash: Any,
    anchor_changed: Any,
    anchor_observed: Any = False,
    prompt_variant: Any = None,
    trim_epoch: Any = 0,
    request_kind: Any = "chat",
    request_class: Any = None,
) -> dict[str, str | int]:
    """Build the fixed cohort dimensions in a stable representation.

    request_kind 保持原样 (request_class 不再改写它) — 冻结维度只由真实请求
    语义决定; 辅助请求靠 eligibility 的 request_class/anchor/warm 元数据排除。
    """
    return {
        "provider": _cohort_text(provider),
        "model": _cohort_text(model),
        "route": _cohort_text(route),
        "prompt_variant": _cohort_text(prompt_variant),
        "persona": _cohort_text(persona),
        "scope_type": _cohort_text(scope_type),
        "tool_set_hash": _cohort_text(tool_set_hash),
        "trim_epoch": _cohort_epoch(trim_epoch),
        "request_kind": _cohort_request_kind(request_kind),
        "request_class": _cohort_request_label(request_class, default="-"),
        "anchor_changed": _cohort_flag(anchor_changed),
        "anchor_observed": _cohort_flag(anchor_observed),
        "warm": _cohort_flag(warm),
    }


def build_cohort_key(metadata: Mapping[str, Any]) -> str:
    """Build a canonical SHA256 key from the required cohort dimensions."""
    normalized = build_cohort_metadata(
        provider=metadata.get("provider"),
        model=metadata.get("model"),
        route=metadata.get("route"),
        prompt_variant=metadata.get("prompt_variant"),
        persona=metadata.get("persona"),
        scope_type=metadata.get("scope_type"),
        tool_set_hash=metadata.get("tool_set_hash"),
        trim_epoch=metadata.get("trim_epoch"),
        request_kind=metadata.get("request_kind"),
        request_class=metadata.get("request_class"),
        anchor_changed=metadata.get("anchor_changed"),
        anchor_observed=metadata.get("anchor_observed", False),
        warm=metadata.get("warm"),
    )
    return canonical_sha256({field: normalized[field] for field in _COHORT_FIELDS})


def _hot99_request_is_user_facing(
    metadata: Mapping[str, Any],
    request_kind: str,
) -> bool:
    if request_kind not in _HOT99_USER_FACING_REQUEST_KINDS:
        return False
    request_class = _cohort_request_label(metadata.get("request_class"))
    return request_class in _HOT99_USER_FACING_REQUEST_CLASSES


def _hot99_sample_is_eligible(
    metadata: Mapping[str, Any],
    normalized: Mapping[str, Any],
    *,
    total: int,
    successful: Any,
) -> int:
    if not _cohort_flag(successful):
        return 0
    if (
        normalized.get("warm") != 1
        or normalized.get("anchor_observed") != 1
        or normalized.get("anchor_changed") != 0
    ):
        return 0
    if total < _HOT99_MIN_PROVIDER_INPUT_TOKENS:
        return 0
    if not _hot99_request_is_user_facing(
        metadata,
        str(normalized.get("request_kind") or ""),
    ):
        return 0
    return 1


def _hot99_sample_flag(sample: tuple[int, ...]) -> int:
    return int(sample[3]) if len(sample) > 3 else 0


def _hot99_rolling_stats(
    samples: Iterable[tuple[int, ...]],
) -> tuple[int, float | None, str]:
    hit_total = 0
    input_total = 0
    eligible_count = 0
    for sample in samples:
        if not _hot99_sample_flag(sample):
            continue
        hit_total += sample[0]
        input_total += sample[1]
        eligible_count += 1
    raw_rate = (hit_total / input_total) if input_total > 0 else None
    if eligible_count < _HOT99_MIN_ELIGIBLE_SAMPLES:
        status = "N/A"
    elif raw_rate is not None and raw_rate >= _HOT99_TARGET:
        status = "PASS"
    else:
        status = "FAIL"
    return eligible_count, raw_rate, status


def record_hit(
    provider: str,
    model: str,
    hit_tok: int,
    miss_tok: int,
    create_tok: int = 0,
) -> RollingStats:
    """Record the established provider|model rolling statistic unchanged."""
    key = f"{provider}|{model}"
    bucket = _ROLLING.get(key)
    if bucket is None:
        if len(_ROLLING) >= _MAX_BUCKETS:
            _ROLLING.clear()
            _LOW_STREAK.clear()
        bucket = deque(maxlen=_ROLLING_MAXLEN)
        _ROLLING[key] = bucket
    hit_tok = max(int(hit_tok), 0)
    total = hit_tok + max(int(miss_tok), 0) + max(int(create_tok), 0)
    this_rate = (hit_tok / total) if total > 0 else 0.0
    bucket.append((hit_tok, total))
    roll_hits = sum(h for h, _ in bucket)
    roll_total = sum(t for _, t in bucket)
    roll_rate = (roll_hits / roll_total) if roll_total > 0 else 0.0
    if roll_rate >= _TARGET_MIN:
        status = "OK"
    else:
        status = f"LOW(-{int((_TARGET_MIN - roll_rate) * 100)}pp)"
    should_warn = False
    if len(bucket) >= 5 and roll_rate < 0.9:
        streak = _LOW_STREAK.get(key, 0) + 1
        if streak >= 3:
            should_warn = True
            streak = 0
        _LOW_STREAK[key] = streak
    else:
        _LOW_STREAK[key] = 0
    return RollingStats(
        this_rate=this_rate,
        roll_rate=roll_rate,
        roll_n=len(bucket),
        status=status,
        should_warn=should_warn,
    )


def record_cohort_hit(
    metadata: Mapping[str, Any],
    hit_tok: Any,
    miss_tok: Any,
    create_tok: Any = 0,
    *,
    unavoidable_current_turn_tokens: Any = None,
    successful: Any = None,
    success: Any = None,
) -> CohortRollingStats:
    """Record actual usage in an independent cohort rolling bucket."""
    normalized = build_cohort_metadata(
        provider=metadata.get("provider"),
        model=metadata.get("model"),
        route=metadata.get("route"),
        prompt_variant=metadata.get("prompt_variant"),
        persona=metadata.get("persona"),
        scope_type=metadata.get("scope_type"),
        tool_set_hash=metadata.get("tool_set_hash"),
        trim_epoch=metadata.get("trim_epoch"),
        request_kind=metadata.get("request_kind"),
        request_class=metadata.get("request_class"),
        anchor_changed=metadata.get("anchor_changed"),
        anchor_observed=metadata.get("anchor_observed", False),
        warm=metadata.get("warm"),
    )
    cohort_key = build_cohort_key(normalized)
    bucket = _COHORT_ROLLING.get(cohort_key)
    if bucket is None:
        if len(_COHORT_ROLLING) >= _COHORT_MAX_BUCKETS:
            _COHORT_ROLLING.clear()
            _COHORT_METADATA.clear()
            _COHORT_LAST.clear()
        bucket = deque(maxlen=_ROLLING_MAXLEN)
        _COHORT_ROLLING[cohort_key] = bucket

    hit = _nonnegative_tokens(hit_tok)
    miss = _nonnegative_tokens(miss_tok)
    create = _nonnegative_tokens(create_tok)
    total = hit + miss + create
    unavoidable = _nonnegative_tokens(unavoidable_current_turn_tokens)
    eligible = max(total - min(unavoidable, total), 0)
    if success is not None:
        successful = success
    if successful is None:
        successful = metadata.get("successful", metadata.get("success", True))
    hot99_eligible = _hot99_sample_is_eligible(
        metadata,
        normalized,
        total=total,
        successful=successful,
    )
    bucket.append((hit, total, eligible, hot99_eligible))
    roll_rate, rolling_normalized_rate, roll_n = rolling_cache_efficiency(
        (sample[:3] for sample in bucket),
    )
    hot99_eligible_count, hot99_raw_rate, hot99_status = _hot99_rolling_stats(bucket)
    stats = CohortRollingStats(
        cohort_key=cohort_key,
        metadata=dict(normalized),
        this_rate=actual_hit_rate(hit, miss, create),
        normalized_rate=normalized_cache_kpi(
            hit,
            miss,
            create,
            unavoidable_current_turn_tokens=unavoidable_current_turn_tokens,
        ),
        roll_rate=roll_rate,
        rolling_normalized_rate=rolling_normalized_rate,
        roll_n=roll_n,
        hit_tok=hit,
        miss_tok=miss,
        create_tok=create,
        unavoidable_current_turn_tokens=min(unavoidable, total),
        eligible_tokens=eligible,
        hot99_eligible=hot99_eligible,
        hot99_eligible_count=hot99_eligible_count,
        hot99_raw_rate=hot99_raw_rate,
        hot99_status=hot99_status,
        hot99_target=_HOT99_TARGET,
    )
    _COHORT_METADATA[cohort_key] = dict(normalized)
    _COHORT_LAST[cohort_key] = {
        "this_rate": stats.this_rate,
        "normalized_rate": stats.normalized_rate,
        "hit_tok": hit,
        "miss_tok": miss,
        "create_tok": create,
        "unavoidable_current_turn_tokens": stats.unavoidable_current_turn_tokens,
        "eligible_tokens": eligible,
        "hot99_eligible": stats.hot99_eligible,
        "hot99_eligible_count": stats.hot99_eligible_count,
        "hot99_raw_rate": stats.hot99_raw_rate,
        "hot99_status": stats.hot99_status,
        "hot99_target": stats.hot99_target,
    }
    return stats


def cohort_stats_snapshot() -> dict[str, dict[str, Any]]:
    """Return a detached diagnostic snapshot of all cohort rolling buckets."""
    snapshot: dict[str, dict[str, Any]] = {}
    for cohort_key, bucket in _COHORT_ROLLING.items():
        roll_hits = sum(sample[0] for sample in bucket)
        roll_total = sum(sample[1] for sample in bucket)
        roll_eligible = sum(sample[2] for sample in bucket)
        roll_rate, rolling_normalized_rate, roll_n = rolling_cache_efficiency(
            (sample[:3] for sample in bucket),
        )
        hot99_eligible_count, hot99_raw_rate, hot99_status = _hot99_rolling_stats(bucket)
        snapshot[cohort_key] = {
            "metadata": dict(_COHORT_METADATA.get(cohort_key, {})),
            "roll_n": roll_n,
            "roll_hit_tokens": roll_hits,
            "roll_total_tokens": roll_total,
            "roll_eligible_tokens": roll_eligible,
            "roll_rate": roll_rate,
            "rolling_normalized_rate": rolling_normalized_rate,
            "hot99_eligible": _hot99_sample_flag(bucket[-1]) if bucket else 0,
            "hot99_eligible_count": hot99_eligible_count,
            "hot99_raw_rate": hot99_raw_rate,
            "hot99_status": hot99_status,
            "hot99_target": _HOT99_TARGET,
            "last": dict(_COHORT_LAST.get(cohort_key, {})),
        }
    return snapshot


def get_cohort_stats_snapshot() -> dict[str, dict[str, Any]]:
    """Explicit getter alias for ``cohort_stats_snapshot``."""
    return cohort_stats_snapshot()


def compute_warm_fields(messages: list | None) -> tuple[int, int, int]:
    """Return ``(msgs, hist, warm)`` using the established warm threshold."""
    if not messages:
        return 0, 0, 0
    try:
        msgs = len(messages)
        hist = sum(
            1
            for message in messages
            if isinstance(message, dict) and message.get("role") == "assistant"
        )
    except Exception:  # noqa: BLE001
        return 0, 0, 0
    return msgs, hist, 1 if hist >= 2 else 0


def _diagnostic_token(value: Any, *, limit: int = 80) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    if not text:
        return "-"
    return text.replace(" ", "_").replace("\t", "_").replace("\n", "_")[:limit]


def _short_hash(value: Any) -> str:
    text = _diagnostic_token(value, limit=64)
    return text[:12] if text != "-" else text


def _diagnostic_tail(diagnostics: Mapping[str, Any] | None) -> str:
    if not diagnostics:
        return ""
    anchor = diagnostics.get("anchor")
    if isinstance(anchor, Mapping):
        before = anchor.get("anchor_before")
        after = anchor.get("anchor_after")
        anchor_value = f"{before}>{after}" if before is not None and after is not None else "-"
        anchor_changed = anchor.get("anchor_changed")
        anchor_reason = anchor.get("reset_reason")
    else:
        anchor_value = diagnostics.get("anchor")
        anchor_changed = diagnostics.get("anchor_changed")
        anchor_reason = diagnostics.get("anchor_reason")
    anchor_observed = diagnostics.get("anchor_observed")
    normalized = diagnostics.get("normalized_kpi")
    normalized_value = "NA" if normalized is None else _diagnostic_token(
        f"{float(normalized):.1%}",
    )
    actual = diagnostics.get("actual_hit_rate")
    actual_value = "-" if actual is None else _diagnostic_token(f"{float(actual):.1%}")
    cohort_key = str(diagnostics.get("cohort") or "")
    cohort_metadata = diagnostics.get("cohort_metadata")
    if not isinstance(cohort_metadata, Mapping):
        cohort_metadata = {}
    recorded_metadata = _COHORT_METADATA.get(cohort_key, {})
    cohort_last = _COHORT_LAST.get(cohort_key, {})

    def cohort_value(name: str, default: Any = None) -> Any:
        value = diagnostics.get(name)
        if value is None:
            value = cohort_metadata.get(name)
        if value is None:
            value = recorded_metadata.get(name)
        return default if value is None else value

    hot99_status = cohort_value("hot99_status", cohort_last.get("hot99_status", "N/A"))
    hot99_raw_rate = cohort_value("hot99_raw_rate", cohort_last.get("hot99_raw_rate"))
    hot99_count = cohort_value(
        "hot99_eligible_count",
        cohort_last.get("hot99_eligible_count", 0),
    )
    hot99_rate_value = "NA" if hot99_raw_rate is None else _diagnostic_token(
        f"{float(hot99_raw_rate):.1%}",
    )
    fields = (
        ("cohort", _short_hash(diagnostics.get("cohort"))),
        ("route", _diagnostic_token(diagnostics.get("route"))),
        ("scope_type", _diagnostic_token(diagnostics.get("scope_type"))),
        ("persona", _diagnostic_token(diagnostics.get("persona"))),
        ("anchor", _diagnostic_token(anchor_value)),
        ("anchor_observed", _diagnostic_token(anchor_observed)),
        ("anchor_changed", _diagnostic_token(anchor_changed)),
        ("anchor_reason", _diagnostic_token(anchor_reason)),
        ("tool_hash", _short_hash(diagnostics.get("wire_tool_hash"))),
        ("tools", _diagnostic_token(diagnostics.get("tool_count"))),
        (
            "prefix",
            f"{_short_hash(diagnostics.get('prefix_sys_hash'))}/"
            f"{_short_hash(diagnostics.get('prefix_first_hash'))}",
        ),
        ("end_role", _diagnostic_token(diagnostics.get("message_end_role"))),
        ("stream", _diagnostic_token(diagnostics.get("stream"))),
        ("actual", actual_value),
        ("normalized", normalized_value),
        ("prompt_variant", _diagnostic_token(cohort_value("prompt_variant"))),
        ("trim_epoch", _diagnostic_token(cohort_value("trim_epoch"))),
        ("request_kind", _diagnostic_token(cohort_value("request_kind"))),
        ("request_class", _diagnostic_token(diagnostics.get("request_class"))),
        ("hot99_status", _diagnostic_token(hot99_status)),
        ("hot99_rate", hot99_rate_value),
        ("hot99_count", _diagnostic_token(hot99_count)),
    )
    return " " + " ".join(f"{name}={value}" for name, value in fields)


def format_hit_target_line(
    *,
    provider: str,
    model: str,
    stats: RollingStats,
    hit_tok: int,
    miss_tok: int,
    create_tok: int,
    msgs: int,
    hist: int,
    warm: int,
    scope: str,
    diagnostics: Mapping[str, Any] | None = None,
) -> str:
    """Format legacy HIT_TARGET fields in place and append optional diagnostics."""
    prompt_tok = int(hit_tok) + int(miss_tok) + int(create_tok)
    line = (
        f"HIT_TARGET model={(model or '')[:20]} this={stats.this_rate:.1%} "
        f"rolling{stats.roll_n}={stats.roll_rate:.1%} "
        f"target=95-98% status={stats.status} "
        f"hit_tok={hit_tok} miss_tok={miss_tok} create_tok={create_tok} "
        f"prompt_tok={prompt_tok} provider={provider} "
        f"msgs={msgs} hist={hist} warm={warm} scope={scope}"
    )
    return line + _diagnostic_tail(diagnostics)
