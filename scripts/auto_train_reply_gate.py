from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from export_reply_gate_dataset import export_all_datasets
from mc_idle_ping import ping_mc_server


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    return data


def _resolve(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def _format_command(command: str, *, dataset_path: Path, output_dir: Path, config_path: Path) -> str:
    return command.format(
        dataset=str(dataset_path),
        output_dir=str(output_dir),
        config=str(config_path),
        python=sys.executable,
        scripts_dir=str(config_path.parent / "scripts"),
    )


def _auto_wrapper_command(config_path: Path, *, task_name: str, mode: str) -> str:
    script_path = config_path.parent / "scripts" / "local_lora_train.py"
    return subprocess.list2cmdline(
        [
            sys.executable,
            str(script_path),
            "--dataset",
            "{dataset}",
            "--output-dir",
            "{output_dir}",
            "--config",
            "{config}",
            "--task",
            task_name,
            "--mode",
            mode,
        ]
    )


def _resolve_training_command(
    training: dict[str, Any],
    *,
    explicit_key: str,
    config_path: Path,
    task_name: str,
    mode: str,
) -> str:
    explicit = str(training.get(explicit_key) or "").strip()
    if explicit:
        return explicit
    if _as_bool(training.get("auto_fill_training_commands"), default=True):
        return _auto_wrapper_command(config_path, task_name=task_name, mode=mode)
    return ""


def _hour_in_window(hour: int, start_hour: int, end_hour: int) -> bool:
    start = max(min(start_hour, 23), 0)
    end = max(min(end_hour, 23), 0)
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _sample_source_paths(config: dict[str, Any], config_path: Path) -> list[Path]:
    base_dir = config_path.parent
    training = config.get("local_training", {})
    if not isinstance(training, dict):
        training = {}
    local_critic = config.get("local_critic", {})
    if not isinstance(local_critic, dict):
        local_critic = {}
    values = [
        str(training.get("source_samples_path") or local_critic.get("training_samples_path") or "local_critic_samples.jsonl"),
        str(training.get("assistant_samples_path") or "training/assistant_reply_samples.jsonl"),
    ]
    return [_resolve(base_dir, value) for value in values if value.strip()]


def _latest_sample_mtime(paths: list[Path]) -> float | None:
    latest: float | None = None
    for path in paths:
        if not path.exists():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        latest = mtime if latest is None else max(latest, mtime)
    return latest


def _mc_idle_decision(
    training: dict[str, Any],
    state: dict[str, Any],
    now: float,
    *,
    idle_interval: int,
    active_interval: int,
) -> tuple[bool, str, int] | None:
    """如果开启 MC idle 检查，返回最终决定；否则返回 None 让外层走时间窗口逻辑。

    决策：
    - ping 失败 → 谨慎不训（保护游戏运行）
    - 有玩家 → 不训，刷新 mc_last_player_seen
    - 无玩家但持续时间不够 → 不训
    - 无玩家且持续 ≥ mc_idle_min_minutes → 可训
    """
    if not _as_bool(training.get("mc_idle_check_enabled"), default=False):
        return None
    host = str(training.get("mc_server_host") or "localhost").strip() or "localhost"
    port = int(training.get("mc_server_port") or 26843)
    idle_minutes = max(float(training.get("mc_idle_min_minutes") or 30.0), 0.0)
    timeout = max(float(training.get("mc_ping_timeout_seconds") or 5.0), 0.5)

    online, players = ping_mc_server(host, port, timeout=timeout)
    if not online:
        return False, f"MC server unreachable at {host}:{port}, skip training to be safe", active_interval

    if players > 0:
        state["mc_last_player_seen"] = now
        return False, f"MC has {players} players online", active_interval

    last_seen = float(state.get("mc_last_player_seen") or 0.0)
    if last_seen <= 0.0:
        # 第一次看到 0 玩家，从这一刻起算
        state["mc_last_player_seen"] = now
        return False, f"MC empty starting now, need {int(idle_minutes)}m streak", active_interval

    empty_for_seconds = max(now - last_seen, 0.0)
    needed_seconds = idle_minutes * 60
    if empty_for_seconds < needed_seconds:
        remaining_min = max(int((needed_seconds - empty_for_seconds) / 60), 1)
        return (
            False,
            f"MC empty for {int(empty_for_seconds / 60)}m, need {int(idle_minutes)}m (~{remaining_min}m to go)",
            active_interval,
        )
    return (
        True,
        f"MC empty for {int(empty_for_seconds / 60)}m (>= {int(idle_minutes)}m), safe to train",
        idle_interval,
    )


def _idle_decision(
    config: dict[str, Any],
    config_path: Path,
    state: dict[str, Any] | None = None,
) -> tuple[bool, str, int]:
    training = config.get("local_training", {})
    if not isinstance(training, dict):
        training = {}
    active_interval = max(int(training.get("active_check_interval_seconds") or 900), 60)
    idle_interval = max(int(training.get("idle_check_interval_seconds") or training.get("watch_interval_seconds") or 3600), 60)
    if not _as_bool(training.get("idle_training_enabled"), default=True):
        return True, "idle gate disabled", idle_interval

    now = time.time()
    # MC idle gating 是主导条件——开启后无视时间窗口，按"MC 真的没人"判断
    if state is not None:
        mc_decision = _mc_idle_decision(
            training,
            state,
            now,
            idle_interval=idle_interval,
            active_interval=active_interval,
        )
        if mc_decision is not None:
            return mc_decision

    local_time = time.localtime(now)
    start_hour = int(training.get("idle_start_hour") or 2)
    end_hour = int(training.get("idle_end_hour") or 6)
    quiet_minutes = max(int(training.get("idle_min_quiet_minutes") or 45), 0)
    allow_quiet_idle = _as_bool(training.get("allow_quiet_idle"), default=True)
    in_idle_window = _hour_in_window(local_time.tm_hour, start_hour, end_hour)

    latest_mtime = _latest_sample_mtime(_sample_source_paths(config, config_path))
    if latest_mtime is None:
        quiet_for_seconds = float("inf")
    else:
        quiet_for_seconds = max(now - latest_mtime, 0.0)
    quiet_enough = quiet_for_seconds >= quiet_minutes * 60

    if in_idle_window and quiet_enough:
        return True, f"local hour {local_time.tm_hour} is in idle window and chat has been quiet", idle_interval
    if in_idle_window and not quiet_enough:
        return False, f"local hour {local_time.tm_hour} is idle window but samples updated recently", active_interval
    if allow_quiet_idle and quiet_enough:
        minutes = int(quiet_for_seconds // 60) if quiet_for_seconds != float("inf") else -1
        return True, f"chat has been quiet for {minutes} minutes outside idle window", idle_interval
    return False, f"local hour {local_time.tm_hour} is outside idle window", active_interval


def _run_training_task(
    *,
    task_name: str,
    count: int,
    dataset_path: Path,
    output_dir: Path,
    config_path: Path,
    state: dict[str, Any],
    min_samples: int,
    min_new_samples: int,
    command: str,
    busy_command: str,
    busy_training_enabled: bool,
    idle_timeout_seconds: int,
    busy_timeout_seconds: int,
    can_train: bool,
    idle_reason: str,
) -> None:
    trained_key = f"{task_name}_trained_samples"
    latest_key = f"{task_name}_latest_dataset_samples"
    trained_samples = int(state.get(trained_key) or (state.get("trained_samples") if task_name == "reply_gate" else 0) or 0)

    if count < min_samples:
        print(f"{task_name}: not enough samples: {count}/{min_samples}")
        state[latest_key] = count
        return
    if count - trained_samples < min_new_samples:
        print(f"{task_name}: not enough new samples: {count - trained_samples}/{min_new_samples}")
        state[latest_key] = count
        return
    if not can_train:
        if not busy_training_enabled:
            print(f"{task_name}: deferred training because not idle enough: {idle_reason}")
            state[latest_key] = count
            return
        command = busy_command
        mode = "busy"
        timeout_seconds = busy_timeout_seconds
    else:
        mode = "idle"
        timeout_seconds = idle_timeout_seconds
    if not command:
        print(f"{task_name}: dataset ready at {dataset_path}; set a {mode} training command to run automatic training")
        state[latest_key] = count
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = _format_command(command, dataset_path=dataset_path, output_dir=output_dir, config_path=config_path)
    print(f"{task_name}: running {mode} local training command: {rendered}")
    env = dict(os.environ, CATTY_TRAINING_MODE=mode, CATTY_TRAINING_TASK=task_name)
    creationflags = 0
    if mode == "busy" and os.name == "nt":
        creationflags = getattr(subprocess, "IDLE_PRIORITY_CLASS", 0)
    try:
        subprocess.run(
            rendered,
            cwd=str(config_path.parent),
            shell=True,
            check=True,
            env=env,
            timeout=timeout_seconds if timeout_seconds > 0 else None,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        print(f"{task_name}: {mode} training timed out after {timeout_seconds} seconds")
        state[latest_key] = count
        return
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 2:
            print(f"{task_name}: {mode} training skipped by safe wrapper")
            state[latest_key] = count
            return
        raise
    state[trained_key] = count
    state[latest_key] = count
    if task_name == "reply_gate":
        state["trained_samples"] = count


def run_once(config_path: Path) -> int:
    config = _load_config(config_path)
    base_dir = config_path.parent
    training = config.get("local_training", {})
    if not isinstance(training, dict) or not bool(training.get("enabled")):
        print("local_training is disabled")
        return 0

    datasets = export_all_datasets(config_path)
    state_path = _resolve(base_dir, str(training.get("state_path") or "training/reply_gate_train_state.json"))
    state = _load_state(state_path)
    can_train, idle_reason, _ = _idle_decision(config, config_path, state)
    busy_training_enabled = _as_bool(training.get("busy_training_enabled"), default=True)
    idle_timeout_seconds = int(training.get("idle_training_max_seconds") or 0)
    busy_timeout_seconds = int(training.get("busy_training_max_seconds") or 600)
    print(f"local training idle decision: {'idle' if can_train else 'busy'} ({idle_reason})")

    reply_count, reply_dataset_path = datasets["reply_gate"]
    _run_training_task(
        task_name="reply_gate",
        count=reply_count,
        dataset_path=reply_dataset_path,
        output_dir=_resolve(base_dir, str(training.get("output_dir") or "training/reply_gate_lora")),
        config_path=config_path,
        state=state,
        min_samples=int(training.get("min_samples") or 200),
        min_new_samples=int(training.get("min_new_samples") or 50),
        command=_resolve_training_command(
            training,
            explicit_key="train_command",
            config_path=config_path,
            task_name="reply_gate",
            mode="idle",
        ),
        busy_command=_resolve_training_command(
            training,
            explicit_key="busy_train_command",
            config_path=config_path,
            task_name="reply_gate",
            mode="busy",
        ),
        busy_training_enabled=busy_training_enabled,
        idle_timeout_seconds=idle_timeout_seconds,
        busy_timeout_seconds=busy_timeout_seconds,
        can_train=can_train,
        idle_reason=idle_reason,
    )

    assistant_count, assistant_dataset_path = datasets["assistant_reply"]
    _run_training_task(
        task_name="assistant_reply",
        count=assistant_count,
        dataset_path=assistant_dataset_path,
        output_dir=_resolve(base_dir, str(training.get("assistant_output_dir") or "training/assistant_reply_lora")),
        config_path=config_path,
        state=state,
        min_samples=int(training.get("assistant_min_samples") or training.get("min_samples") or 200),
        min_new_samples=int(training.get("assistant_min_new_samples") or training.get("min_new_samples") or 50),
        command=_resolve_training_command(
            training,
            explicit_key="assistant_train_command",
            config_path=config_path,
            task_name="assistant_reply",
            mode="idle",
        ),
        busy_command=_resolve_training_command(
            training,
            explicit_key="assistant_busy_train_command",
            config_path=config_path,
            task_name="assistant_reply",
            mode="busy",
        ),
        busy_training_enabled=busy_training_enabled,
        idle_timeout_seconds=idle_timeout_seconds,
        busy_timeout_seconds=busy_timeout_seconds,
        can_train=can_train,
        idle_reason=idle_reason,
    )

    _save_state(state_path, state)
    return 0


def run_watch(config_path: Path) -> int:
    while True:
        config = _load_config(config_path)
        base_dir = config_path.parent
        training = config.get("local_training", {}) or {}
        state_path = _resolve(base_dir, str(training.get("state_path") or "training/reply_gate_train_state.json"))
        state = _load_state(state_path)
        _, _, interval = _idle_decision(config, config_path, state)
        _save_state(state_path, state)
        run_once(config_path)
        time.sleep(max(interval, 60))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if args.watch:
        raise SystemExit(run_watch(config_path))
    raise SystemExit(run_once(config_path))


if __name__ == "__main__":
    main()
