"""S4.5 每日进化协调器 + cron 调度 loop.

入口:
- run_evolution_once(config) - 跑一次完整流程, 给手动 #evolve 用.
- daily_evolution_loop(config) - 后台 asyncio loop, 计算到下个 cron_hour 的秒数, sleep 等待.

完整流程:
1. evaluate_rollback_signal: 若触发回滚, 先 rollback_n_days 再退出.
2. load_emits 过去 24h.
3. sample_emits_by_layer 取 samples_per_layer × layers 条.
4. call_judge_async: DeepSeek 评审.
5. apply_judge_report: 应用到 routes/*.yaml + git commit.
6. write_report_md: 写 data/cpu_engine/evolution_logs/report_YYYY-MM-DD.md.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from .evolution_applier import ApplyResult, apply_judge_report
from .evolution_judge import JudgeReport, call_judge_async
from .evolution_logger import (
    load_emits,
    sample_emits_by_layer,
)
from .evolution_rollback import evaluate_rollback_signal, rollback_n_days


async def run_evolution_once(
    *,
    config: Any,
    repo_root: str | Path = ".",
    routes_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
    learned_dir: str | Path | None = None,
    sample_window_seconds: float = 86400.0,
    force_skip_rollback_check: bool = False,
) -> dict[str, Any]:
    """跑一次完整 evolution. 返回 summary dict.

    force_skip_rollback_check: 主人 #evolve 手动跑时跳过 rollback 检查, 直接进化.
    """
    routes_dir = Path(routes_dir or getattr(config, "catty_cpu_engine_routes_dir", ""))
    log_dir = Path(log_dir or getattr(config, "catty_evolution_logs_dir", ""))
    learned_dir = Path(learned_dir or (routes_dir.parent / "learned"))

    summary: dict[str, Any] = {
        "ts": time.time(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "status": "starting",
    }

    if not force_skip_rollback_check:
        rb = evaluate_rollback_signal(config=config, log_dir=log_dir)
        summary["rollback_check"] = asdict(rb)
        if rb.should_rollback:
            ok, msg = rollback_n_days(
                days=rb.days_to_rollback,
                repo_root=repo_root,
                routes_dir=routes_dir,
                learned_dir=learned_dir,
            )
            summary["status"] = "rollback_triggered"
            summary["rollback_result"] = {"ok": ok, "msg": msg}
            logger.warning(
                f"[evolution.pipeline] AUTO ROLLBACK triggered: {rb.reason} → {msg}"
            )
            _write_report_md(log_dir, summary)
            return summary

    skip_private = bool(getattr(config, "catty_evolution_sample_only_group", True))
    emits = load_emits(
        log_dir,
        since_ts=time.time() - sample_window_seconds,
        skip_private=skip_private,
    )
    samples = sample_emits_by_layer(
        emits,
        samples_per_layer=int(getattr(config, "catty_evolution_samples_per_layer", 30)),
    )
    summary["emits_total"] = len(emits)
    summary["samples_count"] = len(samples)
    summary["samples_by_layer"] = _count_by_layer(samples)

    if not samples:
        summary["status"] = "no_samples"
        logger.info("[evolution.pipeline] no samples in window, skip judge")
        _write_report_md(log_dir, summary)
        return summary

    timeout_s = float(getattr(config, "catty_audit_ai_request_timeout", 180.0))
    report = await call_judge_async(config=config, emits=samples, timeout_s=timeout_s)
    if report is None:
        summary["status"] = "judge_failed"
        logger.error("[evolution.pipeline] judge call failed, abort apply")
        _write_report_md(log_dir, summary)
        return summary

    summary["judge_mean_score"] = round(report.mean_score, 3)
    summary["judge_score_distribution"] = report.score_distribution
    summary["judge_evaluations_count"] = len(report.evaluations)
    summary["judge_new_routes_count"] = len(report.new_routes)

    cat_suffixes = list(getattr(config, "catty_cpu_engine_cat_suffixes", []) or [])
    git_enabled = bool(getattr(config, "catty_evolution_git_commit_enabled", True))

    apply_result = apply_judge_report(
        report=report,
        routes_dir=routes_dir,
        learned_dir=learned_dir,
        cat_suffixes=cat_suffixes,
        git_commit_enabled=git_enabled,
        repo_root=repo_root,
    )

    summary["apply"] = {
        "rewrites": apply_result.rewrites_applied,
        "retires": apply_result.retires_applied,
        "keeps_weighted": apply_result.keeps_weighted,
        "new_routes_added": apply_result.new_routes_added,
        "new_routes_skipped": apply_result.new_routes_skipped,
        "rewrites_rejected_persona": apply_result.rewrites_rejected_persona,
        "errors": apply_result.errors,
        "git_pre_ok": apply_result.git_pre_ok,
        "git_post_ok": apply_result.git_post_ok,
        "backup_dir": apply_result.backup_dir,
    }
    summary["status"] = "applied"
    _write_report_md(log_dir, summary)
    return summary


async def daily_evolution_loop(config: Any, *, repo_root: str | Path = ".") -> None:
    """后台 loop: 等到下一个 cron 时间触发, 完了再 sleep 24h."""
    while True:
        if not bool(getattr(config, "catty_evolution_enabled", False)):
            await asyncio.sleep(3600)
            continue

        cron_str = str(getattr(config, "catty_evolution_cron", "0 3 * * *") or "0 3 * * *")
        try:
            wait_s = _seconds_until_next_cron(cron_str)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution.pipeline] cron parse failed '{cron_str}': {exc}, retry in 1h")
            await asyncio.sleep(3600)
            continue

        logger.info(
            f"[evolution.pipeline] next run in {wait_s/3600:.1f}h "
            f"(cron={cron_str})"
        )
        await asyncio.sleep(max(wait_s, 60))

        if not bool(getattr(config, "catty_evolution_enabled", False)):
            continue

        try:
            await run_evolution_once(config=config, repo_root=repo_root)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[evolution.pipeline] run_evolution_once raised: {exc}")
        await asyncio.sleep(60)  # 避免重复 trigger


def _seconds_until_next_cron(cron_str: str) -> float:
    """简易 cron 解析: 仅支持 'M H * * *' (分钟+小时, 其余必须 *).

    更复杂的 cron 表达式不支持, 但日常 03:00 / 04:30 这种够用.
    """
    parts = cron_str.split()
    if len(parts) != 5:
        raise ValueError("expected 5 fields")
    minute_s, hour_s, dom_s, mon_s, dow_s = parts
    if dom_s != "*" or mon_s != "*" or dow_s != "*":
        raise ValueError("only '* * *' for day/month/weekday supported")
    try:
        minute = int(minute_s)
        hour = int(hour_s)
    except ValueError as e:
        raise ValueError(f"non-integer minute/hour: {e}") from None
    if not (0 <= minute < 60 and 0 <= hour < 24):
        raise ValueError("minute/hour out of range")

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _count_by_layer(samples: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in samples:
        out[s.layer] = out.get(s.layer, 0) + 1
    return out


def _write_report_md(log_dir: str | Path, summary: dict[str, Any]) -> None:
    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        date = summary.get("date") or datetime.now().strftime("%Y-%m-%d")
        path = log_dir / f"report_{date}.md"
        with path.open("w", encoding="utf-8") as f:
            f.write(f"# Evolution Report {date}\n\n")
            f.write(f"- status: {summary.get('status')}\n")
            if "rollback_check" in summary:
                f.write(f"- rollback_check.reason: {summary['rollback_check'].get('reason')}\n")
            if "rollback_result" in summary:
                f.write(f"- rollback_result: {summary['rollback_result']}\n")
            f.write(f"- emits_total: {summary.get('emits_total', 0)}\n")
            f.write(f"- samples_count: {summary.get('samples_count', 0)}\n")
            f.write(f"- samples_by_layer: {summary.get('samples_by_layer', {})}\n")
            if "judge_mean_score" in summary:
                f.write(f"- judge_mean_score: {summary['judge_mean_score']}\n")
                f.write(f"- judge_score_distribution: {summary['judge_score_distribution']}\n")
                f.write(f"- judge_evaluations: {summary['judge_evaluations_count']}\n")
                f.write(f"- judge_new_routes: {summary['judge_new_routes_count']}\n")
            if "apply" in summary:
                f.write("\n## Apply\n")
                for k, v in summary["apply"].items():
                    f.write(f"- {k}: {v}\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution.pipeline] write report failed: {exc}")
