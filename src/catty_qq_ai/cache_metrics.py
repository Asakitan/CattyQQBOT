"""Provider/model cache metrics plus isolated cohort diagnostics."""
from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_TARGET_MIN = 0.95
_ROLLING_MAXLEN = 20
_ROLLING: dict[str, deque[tuple[int, int]]] = {}
_LOW_STREAK: dict[str, int] = {}
_MAX_BUCKETS = 64

# Cohort buckets are deliberately separate from the established provider|model
# rolling buckets so legacy KPI output is not influenced by diagnostic slicing.
_COHORT_ROLLING: dict[str, deque[tuple[int, int]]] = {}
_COHORT_METADATA: dict[str, dict[str, str | int]] = {}
_COHORT_LAST: dict[str, dict[str, int | float | None]] = {}
_COHORT_MAX_BUCKETS = 256
_COHORT_FIELDS = (
    "provider",
    "model",
    "route",
    "scope_type",
    "persona",
    "warm",
    "tool_set_hash",
    "anchor_changed",
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
    roll_n: int
    hit_tok: int
    miss_tok: int
    create_tok: int
    unavoidable_current_turn_tokens: int
    eligible_tokens: int


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


def _cohort_text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "-"


def _cohort_flag(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "on"} else 0
    return 1 if bool(value) else 0


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
) -> dict[str, str | int]:
    """Build the fixed cohort dimensions in a stable representation."""
    return {
        "provider": _cohort_text(provider),
        "model": _cohort_text(model),
        "route": _cohort_text(route),
        "scope_type": _cohort_text(scope_type),
        "persona": _cohort_text(persona),
        "warm": _cohort_flag(warm),
        "tool_set_hash": _cohort_text(tool_set_hash),
        "anchor_changed": _cohort_flag(anchor_changed),
    }


def build_cohort_key(metadata: Mapping[str, Any]) -> str:
    """Build a canonical SHA256 key from the required cohort dimensions."""
    normalized = build_cohort_metadata(
        provider=metadata.get("provider"),
        model=metadata.get("model"),
        route=metadata.get("route"),
        scope_type=metadata.get("scope_type"),
        persona=metadata.get("persona"),
        warm=metadata.get("warm"),
        tool_set_hash=metadata.get("tool_set_hash"),
        anchor_changed=metadata.get("anchor_changed"),
    )
    return canonical_sha256({field: normalized[field] for field in _COHORT_FIELDS})


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
) -> CohortRollingStats:
    """Record actual usage in an independent cohort rolling bucket."""
    normalized = build_cohort_metadata(
        provider=metadata.get("provider"),
        model=metadata.get("model"),
        route=metadata.get("route"),
        scope_type=metadata.get("scope_type"),
        persona=metadata.get("persona"),
        warm=metadata.get("warm"),
        tool_set_hash=metadata.get("tool_set_hash"),
        anchor_changed=metadata.get("anchor_changed"),
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
    bucket.append((hit, total))
    roll_hits = sum(bucket_hit for bucket_hit, _ in bucket)
    roll_total = sum(bucket_total for _, bucket_total in bucket)
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
        roll_rate=(roll_hits / roll_total) if roll_total > 0 else 0.0,
        roll_n=len(bucket),
        hit_tok=hit,
        miss_tok=miss,
        create_tok=create,
        unavoidable_current_turn_tokens=min(unavoidable, total),
        eligible_tokens=eligible,
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
    }
    return stats


def cohort_stats_snapshot() -> dict[str, dict[str, Any]]:
    """Return a detached diagnostic snapshot of all cohort rolling buckets."""
    snapshot: dict[str, dict[str, Any]] = {}
    for cohort_key, bucket in _COHORT_ROLLING.items():
        roll_hits = sum(hit for hit, _ in bucket)
        roll_total = sum(total for _, total in bucket)
        snapshot[cohort_key] = {
            "metadata": dict(_COHORT_METADATA.get(cohort_key, {})),
            "roll_n": len(bucket),
            "roll_hit_tokens": roll_hits,
            "roll_total_tokens": roll_total,
            "roll_rate": (roll_hits / roll_total) if roll_total > 0 else 0.0,
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
    normalized = diagnostics.get("normalized_kpi")
    normalized_value = "NA" if normalized is None else _diagnostic_token(
        f"{float(normalized):.1%}",
    )
    actual = diagnostics.get("actual_hit_rate")
    actual_value = "-" if actual is None else _diagnostic_token(f"{float(actual):.1%}")
    fields = (
        ("cohort", _short_hash(diagnostics.get("cohort"))),
        ("route", _diagnostic_token(diagnostics.get("route"))),
        ("scope_type", _diagnostic_token(diagnostics.get("scope_type"))),
        ("persona", _diagnostic_token(diagnostics.get("persona"))),
        ("anchor", _diagnostic_token(anchor_value)),
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
