"""A/B 缓存命中报告 — 解析 bot_live.log 的 HIT_TARGET 行 (openai-claude-95 §五).

用法 (远端 D:/CattyQQAI 下):
  python scripts/ab_hit_report.py --scope group:731001 [--since "2026-07-06 19:00"] \
      [--log logs/bot_live.log] [--warm-hist 2]

输出: 按 provider|model 分桶的 warm token 加权命中率 + cold 明细 + 达标判定。
口径: warm = HIT_TARGET 行的 hist >= --warm-hist (默认 2, 与 cache_metrics.warm 一致);
token 加权命中 = Σhit_tok / Σ(hit_tok+miss_tok+create_tok)。
openai 桶里 prompt_tok < 1024 的行单独剔除报告 (OpenAI 隐式缓存最小前缀, 物理不可缓存)。
达标线: 私聊 warm ≥95% / 群聊 warm ≥86% (per 主人 2026-07-06 拍板, warm 会话口径)。
兼容旧格式 HIT_TARGET 行 (无 provider/warm 字段时按 provider=deepseek, warm 用 msgs 缺省 1)。

--mode hot99 为独立的长上下文 cohort 审计口径；默认 legacy 保持以上原有输出和阈值。
"""

from __future__ import annotations

import argparse
import glob
import re
from collections import defaultdict

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\S*.*?HIT_TARGET "
    r"model=(?P<model>\S+) this=(?P<this>[\d.]+)% .*?"
    r"hit_tok=(?P<hit>\d+) miss_tok=(?P<miss>\d+)"
    r"(?: create_tok=(?P<create>\d+))?"
    r"(?: prompt_tok=(?P<prompt>\d+))?"
    r"(?: provider=(?P<provider>\S+))?"
    r"(?: msgs=(?P<msgs>\d+))?"
    r"(?: hist=(?P<hist>\d+))?"
    r"(?: warm=(?P<warm>\d))?"
    r" scope=(?P<scope>\S*)",
)

_DIAGNOSTIC_FIELDS = (
    "route",
    "scope_type",
    "persona",
    "tool_hash",
    "prompt_variant",
    "trim_epoch",
    "request_kind",
    "request_class",
    "anchor_observed",
    "anchor_changed",
    "actual",
    "normalized",
)
_DIAGNOSTIC_FIELD_RE = re.compile(
    r"(?<!\S)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)",
)
_HOT99_COHORT_FIELDS = (
    "provider",
    "model",
    "route",
    "prompt_variant",
    "persona",
    "scope_type",
    "tool_hash",
    "trim_epoch",
    "request_kind",
)
_HOT99_REQUEST_KINDS = {
    "chat",
    "chat_followup",
    "tool_initial",
    "tool_followup",
}
_HOT99_REQUEST_CLASSES = {"chat"}
_HOT99_MIN_PROMPT_TOKENS = 100_000
_HOT99_MIN_ROWS = 10


def parse_diagnostic_tail(line: str) -> dict[str, str]:
    """Return supported key=value diagnostics appended to a HIT_TARGET line."""
    _prefix, marker, tail = line.partition("HIT_TARGET ")
    if not marker:
        return {}
    return {
        match.group("key"): match.group("value")
        for match in _DIAGNOSTIC_FIELD_RE.finditer(tail)
        if match.group("key") in _DIAGNOSTIC_FIELDS
    }


def parse_hit_target_line(line: str, *, warm_hist: int) -> dict | None:
    """Parse one legacy HIT_TARGET line and preserve optional tail diagnostics."""
    match = _LINE_RE.search(line)
    if not match:
        return None

    data = match.groupdict()
    diagnostics = parse_diagnostic_tail(line)
    return {
        "ts": data["ts"],
        "model": data["model"],
        "provider": data["provider"] or "deepseek",
        "hit": int(data["hit"]),
        "miss": int(data["miss"]),
        "create": int(data["create"] or 0),
        "prompt": int(data["prompt"] or 0)
        or (int(data["hit"]) + int(data["miss"]) + int(data["create"] or 0)),
        "hist": int(data["hist"]) if data["hist"] is not None else None,
        "warm": (
            int(data["hist"]) >= warm_hist
            if data["hist"] is not None
            else (data["warm"] == "1" if data["warm"] is not None else True)
        ),
        "warm_field": int(data["warm"]) if data["warm"] is not None else None,
        "scope": data["scope"],
        **{field: diagnostics.get(field) for field in _DIAGNOSTIC_FIELDS},
    }


def collect_hit_target_rows(
    log_pattern: str,
    *,
    scope: str,
    since: str,
    warm_hist: int,
) -> tuple[list[dict], list[str]]:
    """Load matching HIT_TARGET rows and defer file errors to the CLI caller."""
    rows: list[dict] = []
    errors: list[str] = []
    for log_file in sorted(glob.glob(log_pattern)) or [log_pattern]:
        try:
            fh = open(log_file, encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"!! 打不开 {log_file}: {exc}")
            continue
        with fh:
            for line in fh:
                row = parse_hit_target_line(line, warm_hist=warm_hist)
                if row is None:
                    continue
                if row["scope"] != scope:
                    continue
                if since and row["ts"] < since:
                    continue
                rows.append(row)
    return rows, errors


def render_legacy_report(
    rows: list[dict],
    *,
    scope: str,
    since: str,
    warm_hist: int,
    min_prompt_openai: int,
) -> str:
    """Render the original report verbatim for the default legacy mode."""
    if not rows:
        return f"scope={scope} 没有匹配的 HIT_TARGET 行 (since={since!r})"

    is_private = scope.startswith("private:")
    target = 0.95 if is_private else 0.86
    lines = [
        f"scope={scope} rows={len(rows)} 口径: warm hist>={warm_hist} "
        f"达标线={'私聊95%' if is_private else '群聊86%'}",
    ]

    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[f"{row['provider']}|{row['model']}"].append(row)

    overall_pass = True
    for key, bucket_rows in sorted(buckets.items()):
        provider = key.split("|", 1)[0]
        warm = [row for row in bucket_rows if row["warm"]]
        cold = [row for row in bucket_rows if not row["warm"]]
        na = []
        if provider == "openai":
            na = [row for row in warm if row["prompt"] < min_prompt_openai]
            warm = [row for row in warm if row["prompt"] >= min_prompt_openai]
        th = sum(row["hit"] for row in warm)
        tt = sum(row["hit"] + row["miss"] + row["create"] for row in warm)
        rate = th / tt if tt else 0.0
        ok = rate >= target if warm else False
        overall_pass = overall_pass and (ok or not warm)
        lines.append(f"\n== {key} ==")
        lines.append(
            f"  warm n={len(warm)} token加权命中={rate:.1%} "
            f"{'PASS' if ok else 'FAIL' if warm else 'N/A'} (target {target:.0%})",
        )
        for row in warm:
            total = row["hit"] + row["miss"] + row["create"]
            this = row["hit"] / total if total else 0
            lines.append(
                f"    {row['ts']} this={this:.1%} hit={row['hit']} miss={row['miss']} "
                f"create={row['create']} prompt={row['prompt']} hist={row['hist']}",
            )
        if cold:
            lines.append(f"  cold n={len(cold)} (冷启/前缀检测滞后, 不计口径):")
            for row in cold:
                total = row["hit"] + row["miss"] + row["create"]
                this = row["hit"] / total if total else 0
                lines.append(f"    {row['ts']} this={this:.1%} hist={row['hist']}")
        if na:
            lines.append(
                f"  N/A n={len(na)} (openai prompt_tok<{min_prompt_openai}, 物理不可缓存)",
            )

    lines.append(f"\n>>> 总判定: {'PASS' if overall_pass else 'FAIL'}")
    return "\n".join(lines)


def hot99_cohort_key(row: dict) -> tuple[str, ...]:
    """Return the stable dimension tuple used for hot99 aggregation."""
    return tuple(str(row.get(field) or "") for field in _HOT99_COHORT_FIELDS)


def hot99_row_is_eligible(row: dict) -> bool:
    """Apply the strict hot99 population gate to a parsed HIT_TARGET row."""
    return (
        str(row.get("warm_field") or "") == "1"
        and str(row.get("anchor_observed") or "").strip().lower()
        in {"true", "1"}
        and str(row.get("anchor_changed") or "").strip().lower() in {"false", "0"}
        and row["prompt"] >= _HOT99_MIN_PROMPT_TOKENS
        and str(row.get("request_kind") or "").strip().lower() in _HOT99_REQUEST_KINDS
        and str(row.get("request_class") or "").strip().lower() in _HOT99_REQUEST_CLASSES
    )


def _parse_diagnostic_rate(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "-"}:
        return None
    try:
        rate = float(text.rstrip("%"))
    except ValueError:
        return None
    return rate / 100 if text.endswith("%") or rate > 1 else rate


def _diagnostic_rate_average(rows: list[dict], field: str) -> float | None:
    rates = []
    for row in rows:
        rate = _parse_diagnostic_rate(row.get(field))
        if rate is not None:
            rates.append(rate)
    return sum(rates) / len(rates) if rates else None


def _format_hot99_cohort(key: tuple[str, ...]) -> str:
    return " ".join(
        f"{field}={value or '-'}"
        for field, value in zip(_HOT99_COHORT_FIELDS, key)
    )


def render_hot99_report(rows: list[dict], *, scope: str, since: str) -> str:
    """Render strict long-context cohorts; normalized diagnostics never decide PASS."""
    if not rows:
        return (
            f"scope={scope} 没有匹配的 HIT_TARGET 行 (since={since!r})"
            "\n\n>>> 总判定: N/A"
        )

    eligible = [row for row in rows if hot99_row_is_eligible(row)]
    lines = [
        f"scope={scope} rows={len(rows)} eligible={len(eligible)} "
        f"excluded={len(rows) - len(eligible)} 口径: warm=1 anchor_observed=1 "
        f"anchor_changed=false request_class=chat "
        f"prompt>=100000 user-facing request_kind cohort n>={_HOT99_MIN_ROWS} raw target=99%",
    ]
    buckets: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for row in eligible:
        buckets[hot99_cohort_key(row)].append(row)

    decisions: list[bool] = []
    has_under_sampled_cohort = False
    for key, cohort_rows in sorted(buckets.items()):
        hit = sum(row["hit"] for row in cohort_rows)
        total = sum(row["hit"] + row["miss"] + row["create"] for row in cohort_rows)
        raw_rate = hit / total if total else 0.0
        normalized_rate = _diagnostic_rate_average(cohort_rows, "normalized")
        normalized_display = (
            f"{normalized_rate:.1%}" if normalized_rate is not None else "N/A"
        )
        if len(cohort_rows) < _HOT99_MIN_ROWS:
            has_under_sampled_cohort = True
            decision = "N/A"
        else:
            passed = raw_rate >= 0.99
            decisions.append(passed)
            decision = "PASS" if passed else "FAIL"

        lines.append(f"\n== {_format_hot99_cohort(key)} ==")
        lines.append(
            f"  eligible n={len(cohort_rows)} raw token加权命中={raw_rate:.1%} "
            f"normalized(avg)={normalized_display} {decision} "
            f"(target 99%, n>={_HOT99_MIN_ROWS})",
        )

    if any(not decision for decision in decisions):
        overall = "FAIL"
    elif has_under_sampled_cohort or not decisions:
        overall = "N/A"
    else:
        overall = "PASS"
    lines.append(f"\n>>> 总判定: {overall}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", required=True, help="如 group:731001 / private:707701")
    ap.add_argument("--since", default="", help='如 "2026-07-06 19:00" (含)')
    ap.add_argument("--log", default="logs/bot_live.log", help="log 路径, 支持 glob")
    ap.add_argument("--warm-hist", type=int, default=2, help="warm 口径: hist >= N")
    ap.add_argument("--min-prompt-openai", type=int, default=1024)
    ap.add_argument("--mode", choices=("legacy", "hot99"), default="legacy")
    args = ap.parse_args()

    rows, errors = collect_hit_target_rows(
        args.log,
        scope=args.scope,
        since=args.since,
        warm_hist=args.warm_hist,
    )
    for error in errors:
        print(error)
    if args.mode == "hot99":
        print(render_hot99_report(rows, scope=args.scope, since=args.since))
        return
    print(
        render_legacy_report(
            rows,
            scope=args.scope,
            since=args.since,
            warm_hist=args.warm_hist,
            min_prompt_openai=args.min_prompt_openai,
        ),
    )


if __name__ == "__main__":
    main()