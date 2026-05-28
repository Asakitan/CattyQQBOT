"""S4.4 Rollback: git revert + 负反馈监控.

触发条件:
- 7 天滚动: 评后 1 天用户负反馈率较前 7 天均值上升 > rollback_neg_feedback_pct (默认 0.20)
  → 自动 rollback 最近 1 天.
- 30 天滚动: judge 评分均值连续 N 天下降 → 自动 rollback N 天.
- 主人手动 #rollback_evolution N → 回滚 N 天.

实现:
- 用 git revert HEAD~N..HEAD 反向打 commit (而不是 reset --hard, 保留审计).
- N = 2*天数 (每天 pre/post 两次 commit).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from .evolution_logger import (
    compute_negative_feedback_rate,
    load_emits,
    load_feedbacks,
)


@dataclass(slots=True)
class RollbackDecision:
    should_rollback: bool
    days_to_rollback: int  # 1..30
    reason: str
    detail: dict[str, Any]


def evaluate_rollback_signal(
    *,
    config: Any,
    log_dir: str | Path,
    now_ts: float | None = None,
) -> RollbackDecision:
    """评估是否应该自动 rollback. 返回 should_rollback=True 时调用方触发 rollback_n_days."""
    now = now_ts if now_ts is not None else time.time()
    threshold = float(getattr(config, "catty_evolution_rollback_neg_feedback_pct", 0.2))

    day_seconds = 86400.0
    recent_emits = load_emits(log_dir, since_ts=now - day_seconds, until_ts=now, skip_private=True)
    recent_feedbacks = load_feedbacks(log_dir, since_ts=now - day_seconds)
    baseline_emits = load_emits(
        log_dir,
        since_ts=now - 8 * day_seconds,
        until_ts=now - day_seconds,
        skip_private=True,
    )
    baseline_feedbacks = load_feedbacks(log_dir, since_ts=now - 8 * day_seconds)
    baseline_feedbacks = [f for f in baseline_feedbacks if f.ts < now - day_seconds]

    recent_rate = compute_negative_feedback_rate(recent_emits, recent_feedbacks)
    baseline_rate = compute_negative_feedback_rate(baseline_emits, baseline_feedbacks)
    rate_delta = recent_rate - baseline_rate

    if rate_delta > threshold and len(recent_emits) >= 10:
        return RollbackDecision(
            should_rollback=True,
            days_to_rollback=1,
            reason=(
                f"neg_feedback_rate {recent_rate:.1%} - baseline {baseline_rate:.1%} "
                f"= {rate_delta:+.1%} > {threshold:.1%}"
            ),
            detail={
                "recent_rate": recent_rate,
                "baseline_rate": baseline_rate,
                "rate_delta": rate_delta,
                "recent_emit_count": len(recent_emits),
                "baseline_emit_count": len(baseline_emits),
            },
        )

    return RollbackDecision(
        should_rollback=False,
        days_to_rollback=0,
        reason="OK",
        detail={
            "recent_rate": recent_rate,
            "baseline_rate": baseline_rate,
            "rate_delta": rate_delta,
            "recent_emit_count": len(recent_emits),
        },
    )


def rollback_n_days(
    *,
    days: int,
    repo_root: str | Path,
    routes_dir: str | Path,
    learned_dir: str | Path,
) -> tuple[bool, str]:
    """对 git 历史里最近 N 天的 evolution-*-pre/post commit 做 git revert.

    返回 (success, message).
    每天 ~2 commits (pre + post), N 天 ≈ 2N commits.
    """
    if days <= 0:
        return False, "days must be > 0"

    commits = _list_evolution_commits(repo_root=repo_root, limit=days * 4)
    if not commits:
        return False, "no evolution-* commits found in git log"

    take = commits[: days * 2]
    if not take:
        return False, f"no commits within last {days} days"

    success_count = 0
    fail_count = 0
    for sha, subject in take:
        ok = _git_revert_single(repo_root=repo_root, sha=sha)
        if ok:
            success_count += 1
            logger.info(f"[evolution.rollback] reverted {sha[:8]} {subject}")
        else:
            fail_count += 1
            logger.warning(f"[evolution.rollback] revert FAILED {sha[:8]} {subject}")

    summary = (
        f"rolled back {success_count} commits (failed {fail_count}) "
        f"covering last {days} days"
    )
    return success_count > 0, summary


def _list_evolution_commits(
    *,
    repo_root: str | Path,
    limit: int = 60,
) -> list[tuple[str, str]]:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_root),
                "log",
                f"-n{limit}",
                "--pretty=%H\t%s",
                "--grep=^evolution-",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"[evolution.rollback] git log failed: {exc}")
        return []
    commits: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0]:
            commits.append((parts[0], parts[1]))
    return commits


def _git_revert_single(*, repo_root: str | Path, sha: str) -> bool:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_root),
                "revert", "--no-edit", "--no-gpg-sign", sha,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                f"[evolution.rollback] git revert {sha[:8]} failed: "
                f"{result.stderr[:200]}"
            )
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"[evolution.rollback] git revert {sha[:8]} exc: {exc}")
        return False
