"""S4.3 应用 DeepSeek 评审结果到 routes/*.yaml + git 备份.

操作:
- score>=4 + action=keep: weight *= 1.1 (上限 2.0)
- score<=2 + action=retire: route 从原文件移到 learned/retired_YYYY-MM-DD.yaml
- score<=2 + action=rewrite + validate_persona(new_text) OK: 替换 responses 字段 (备份原文)
- new_routes: 灰度入 learned/approved_YYYY-MM-DD.yaml weight=0.1

git 备份:
- 进化前: git add data/cpu_engine/ + commit `evolution-{date}-pre`
- 进化后: git add data/cpu_engine/ + commit `evolution-{date}-post`
失败 (subprocess 返回 != 0) 仅 warn, 不中断流程 (回滚仍可通过文件级备份).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore
from loguru import logger

from .evolution_judge import Evaluation, JudgeReport, NewRoute, validate_persona


@dataclass(slots=True)
class ApplyResult:
    rewrites_applied: int = 0
    retires_applied: int = 0
    keeps_weighted: int = 0
    new_routes_added: int = 0
    new_routes_skipped: int = 0
    rewrites_rejected_persona: int = 0
    errors: list[str] = field(default_factory=list)
    git_pre_ok: bool = False
    git_post_ok: bool = False
    backup_dir: str = ""


def apply_judge_report(
    *,
    report: JudgeReport,
    routes_dir: str | Path,
    learned_dir: str | Path,
    cat_suffixes: list[str],
    git_commit_enabled: bool = True,
    repo_root: str | Path = ".",
    today_str: str | None = None,
) -> ApplyResult:
    """主入口. 顺序: git pre commit → 备份 → apply 改动 → git post commit."""
    routes_dir = Path(routes_dir)
    learned_dir = Path(learned_dir)
    today = today_str or datetime.now().strftime("%Y-%m-%d")
    result = ApplyResult()

    if git_commit_enabled:
        result.git_pre_ok = _git_commit_all(
            repo_root=repo_root,
            paths=[str(routes_dir), str(learned_dir)],
            message=f"evolution-{today}-pre",
        )

    backup_root = learned_dir / "backups" / today
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        for yaml_path in sorted(routes_dir.glob("*.yaml")):
            shutil.copy2(yaml_path, backup_root / yaml_path.name)
        result.backup_dir = str(backup_root)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"backup: {exc}")
        logger.warning(f"[evolution.applier] backup failed: {exc}")

    routes_by_file = _load_routes_with_paths(routes_dir)
    route_index = _index_routes(routes_by_file)

    for evaluation in report.evaluations:
        try:
            _apply_evaluation(
                evaluation=evaluation,
                route_index=route_index,
                routes_by_file=routes_by_file,
                learned_dir=learned_dir,
                cat_suffixes=cat_suffixes,
                today=today,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"evaluation {evaluation.rule_id}: {exc}")
            logger.warning(f"[evolution.applier] eval {evaluation.rule_id} failed: {exc}")

    _write_routes_back(routes_by_file)

    if report.new_routes:
        try:
            added, skipped = _append_new_routes(
                report.new_routes, learned_dir, today, cat_suffixes
            )
            result.new_routes_added = added
            result.new_routes_skipped = skipped
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"new_routes: {exc}")
            logger.warning(f"[evolution.applier] new_routes failed: {exc}")

    if git_commit_enabled:
        result.git_post_ok = _git_commit_all(
            repo_root=repo_root,
            paths=[str(routes_dir), str(learned_dir)],
            message=(
                f"evolution-{today}-post "
                f"rewrites={result.rewrites_applied} "
                f"retires={result.retires_applied} "
                f"new_routes={result.new_routes_added}"
            ),
        )

    logger.info(
        f"[evolution.applier] done: rewrites={result.rewrites_applied} "
        f"retires={result.retires_applied} keeps_weighted={result.keeps_weighted} "
        f"new_routes={result.new_routes_added} errors={len(result.errors)}"
    )
    return result


def _apply_evaluation(
    *,
    evaluation: Evaluation,
    route_index: dict[str, tuple[Path, int]],
    routes_by_file: dict[Path, list[dict[str, Any]]],
    learned_dir: Path,
    cat_suffixes: list[str],
    today: str,
    result: ApplyResult,
) -> None:
    info = route_index.get(evaluation.rule_id)
    if info is None:
        logger.info(f"[evolution.applier] rule_id={evaluation.rule_id} not found (corpus or stale), skip")
        return
    file_path, idx = info
    entry = routes_by_file[file_path][idx]

    if evaluation.action == "keep" and evaluation.score >= 4:
        cur = float(entry.get("weight", 1.0))
        new_weight = min(cur * 1.1, 2.0)
        entry["weight"] = round(new_weight, 3)
        result.keeps_weighted += 1
        return

    if evaluation.action == "retire" and evaluation.score <= 2:
        _move_route_to_retired(
            entry=entry,
            file_path=file_path,
            idx=idx,
            routes_by_file=routes_by_file,
            learned_dir=learned_dir,
            today=today,
        )
        result.retires_applied += 1
        return

    if evaluation.action == "rewrite" and evaluation.score <= 2:
        if not validate_persona(evaluation.new_text, cat_suffixes=cat_suffixes):
            result.rewrites_rejected_persona += 1
            logger.warning(
                f"[evolution.applier] rewrite rejected for {evaluation.rule_id}: "
                f"persona validation failed ({evaluation.new_text[:40]!r})"
            )
            return
        responses = entry.setdefault("responses", [])
        if responses:
            responses[0] = evaluation.new_text
        else:
            entry["responses"] = [evaluation.new_text]
        result.rewrites_applied += 1
        return


def _move_route_to_retired(
    *,
    entry: dict[str, Any],
    file_path: Path,
    idx: int,
    routes_by_file: dict[Path, list[dict[str, Any]]],
    learned_dir: Path,
    today: str,
) -> None:
    retired_path = learned_dir / f"retired_{today}.yaml"
    retired_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if retired_path.exists():
        try:
            with retired_path.open(encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, list):
                existing = loaded
        except Exception:  # noqa: BLE001
            existing = []
    entry_copy = dict(entry)
    entry_copy["retired_at"] = today
    existing.append(entry_copy)
    with retired_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)
    routes_by_file[file_path][idx] = None  # type: ignore[assignment]


def _append_new_routes(
    new_routes: list[NewRoute],
    learned_dir: Path,
    today: str,
    cat_suffixes: list[str],
) -> tuple[int, int]:
    approved_path = learned_dir / f"approved_{today}.yaml"
    approved_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if approved_path.exists():
        try:
            with approved_path.open(encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, list):
                existing = loaded
        except Exception:  # noqa: BLE001
            existing = []

    added = 0
    skipped = 0
    for i, route in enumerate(new_routes):
        valid_responses = [
            r for r in route.responses if validate_persona(r, cat_suffixes=cat_suffixes)
        ]
        if not valid_responses:
            skipped += 1
            continue
        existing.append({
            "name": f"learned_{today.replace('-','')}_{i:03d}",
            "intent": route.intent,
            "utterances": route.utterances,
            "responses": valid_responses,
            "weight": 0.1,  # 灰度
            "_learned_at": today,
            "_reason": route.reason,
        })
        added += 1

    if existing:
        with approved_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, allow_unicode=True, sort_keys=False)

    return added, skipped


def _load_routes_with_paths(routes_dir: Path) -> dict[Path, list[dict[str, Any]]]:
    out: dict[Path, list[dict[str, Any]]] = {}
    for yaml_path in sorted(routes_dir.glob("*.yaml")):
        try:
            with yaml_path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, list):
                out[yaml_path] = data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution.applier] load {yaml_path} failed: {exc}")
    return out


def _index_routes(
    routes_by_file: dict[Path, list[dict[str, Any]]],
) -> dict[str, tuple[Path, int]]:
    index: dict[str, tuple[Path, int]] = {}
    for file_path, entries in routes_by_file.items():
        for idx, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("name"):
                index[str(entry["name"])] = (file_path, idx)
    return index


def _write_routes_back(routes_by_file: dict[Path, list[dict[str, Any]]]) -> None:
    for file_path, entries in routes_by_file.items():
        compacted = [e for e in entries if e is not None]
        try:
            with file_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(compacted, f, allow_unicode=True, sort_keys=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[evolution.applier] write {file_path} failed: {exc}")


def _git_commit_all(
    *,
    repo_root: str | Path,
    paths: list[str],
    message: str,
) -> bool:
    repo_root = str(repo_root)
    try:
        add_result = subprocess.run(
            ["git", "-C", repo_root, "add", *paths],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if add_result.returncode != 0:
            logger.warning(f"[evolution.applier] git add failed: {add_result.stderr[:200]}")
            return False
        status = subprocess.run(
            ["git", "-C", repo_root, "diff", "--cached", "--quiet"],
            capture_output=True,
            timeout=10,
        )
        if status.returncode == 0:
            logger.info(f"[evolution.applier] git commit '{message}' skipped (no changes)")
            return True
        commit_result = subprocess.run(
            ["git", "-C", repo_root, "commit", "-m", message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if commit_result.returncode != 0:
            logger.warning(f"[evolution.applier] git commit failed: {commit_result.stderr[:200]}")
            return False
        logger.info(f"[evolution.applier] git commit ok: {message}")
        return True
    except FileNotFoundError:
        logger.warning("[evolution.applier] git not found in PATH")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("[evolution.applier] git commit timeout")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution.applier] git commit unexpected: {exc}")
        return False
