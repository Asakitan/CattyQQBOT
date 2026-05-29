from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Iterable


DEFAULT_POLL_SECONDS = 1.5
DEFAULT_DEBOUNCE_SECONDS = 1.0
WATCH_DIRS = ("src", "scripts")
WATCH_FILES = (
    "bot.py",
    "catty_config_loader.py",
    "catty_integrations.py",
    "pyproject.toml",
    "CattyQQAI.spec",
    "README.md",
    "config.example.json",  # 模板文件: 改它=代码级变更 → 重启
    "start_catty.bat",
)
# 主人 2026-05-29 hotreload guard: 运行时配置 config.json 【故意】不在 WATCH_FILES /
# WATCH_DIRS(src/scripts) 内 — 守护进程绝不为它 kill/重启 bot.py。改 config.json 只走
# bot 进程内 _hot_reload_loop: 重建全局 config + get_router/get_distiller 的 update_config
# 刷新单例, distill 阈值 / skip_private / catnify 参数等当场热生效。
# 「能不关 bot.py 就不关、只重载参数」就靠这条分工 —— 别把 config.json 加进上面的监听清单。
WATCH_EXTENSIONS = {".bat", ".json", ".md", ".ps1", ".py", ".spec", ".toml", ".yaml", ".yml"}
IGNORED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".sync_tmp",
    ".venv",
    "__pycache__",
    "backup",
    "build",
    "dist",
    "memory_groups",
    "memory_users",
    "models",
    "training",
}
# 主人 2026-05-29: 排除 CPU 引擎数据目录 (routes/fragments/replies), 这些 yaml/json
# 由 _cpu_engine_routes_watch_loop 进程内热重载, 不要 kill bot.py 几十秒下线.
IGNORED_PATH_PREFIXES = (
    "src/catty_qq_ai/data/cpu_engine",
    "src/catty_qq_ai/data/nlu_cache",
)

FileSignature = tuple[int, int]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_ignored_path(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    if any(part in IGNORED_DIR_NAMES for part in relative.parts):
        return True
    # 主人 2026-05-29: 路径前缀排除 (CPU 引擎数据目录由进程内 watch_loop 热重载)
    relative_str = relative.as_posix()
    return any(relative_str.startswith(prefix) for prefix in IGNORED_PATH_PREFIXES)


def _iter_files(root: Path, paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if not path.exists() or is_ignored_path(path, root):
            continue
        if path.is_file():
            if path.suffix.lower() in WATCH_EXTENSIONS:
                yield path
            continue
        for child in path.rglob("*"):
            if child.is_dir() or is_ignored_path(child, root):
                continue
            if child.suffix.lower() in WATCH_EXTENSIONS:
                yield child


def iter_watch_files(root: Path) -> list[Path]:
    candidates = [root / name for name in WATCH_FILES]
    candidates.extend(root / name for name in WATCH_DIRS)
    return sorted(set(_iter_files(root, candidates)))


def snapshot_files(root: Path) -> dict[str, FileSignature]:
    snapshot: dict[str, FileSignature] = {}
    for path in iter_watch_files(root):
        try:
            stat = path.stat()
            key = path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        snapshot[key] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def changed_files(before: dict[str, FileSignature], after: dict[str, FileSignature]) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [key for key in keys if before.get(key) != after.get(key)]


def _load_settings(root: Path) -> dict[str, object]:
    defaults: dict[str, object] = {
        "enabled": True,
        "poll_seconds": DEFAULT_POLL_SECONDS,
        "debounce_seconds": DEFAULT_DEBOUNCE_SECONDS,
        "restart_on_code_change": True,
    }
    config_path = root / "config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    hot_reload = data.get("hot_reload", {})
    if not isinstance(hot_reload, dict):
        return defaults
    return {**defaults, **hot_reload}


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_float(value: object, default: float, minimum: float) -> float:
    try:
        parsed = float(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def _start_bot(root: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["CATTY_HOT_RELOAD_CHILD"] = "1"
    return subprocess.Popen([sys.executable, str(root / "bot.py")], cwd=str(root), env=env)


def _stop_bot(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
    except (OSError, ValueError):
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    root = _project_root()
    print("Catty hot reload watcher started.")
    while True:
        settings = _load_settings(root)
        child = _start_bot(root)
        snapshot = snapshot_files(root)
        restarting = False
        try:
            while child.poll() is None:
                settings = _load_settings(root)
                poll_seconds = _as_float(settings.get("poll_seconds"), DEFAULT_POLL_SECONDS, 0.2)
                time.sleep(poll_seconds)
                if not _as_bool(settings.get("enabled"), default=True):
                    continue
                if not _as_bool(settings.get("restart_on_code_change"), default=True):
                    continue
                current = snapshot_files(root)
                changes = changed_files(snapshot, current)
                if not changes:
                    continue
                debounce = _as_float(settings.get("debounce_seconds"), DEFAULT_DEBOUNCE_SECONDS, 0.0)
                if debounce:
                    time.sleep(debounce)
                    current = snapshot_files(root)
                    changes = changed_files(snapshot, current)
                if not changes:
                    snapshot = current
                    continue
                print("Hot reload restarting Catty after file changes: " + ", ".join(changes[:8]))
                _stop_bot(child)
                try:
                    self_key = Path(__file__).resolve().relative_to(root.resolve()).as_posix()
                except ValueError:
                    self_key = ""
                if self_key in changes:
                    # Windows: os.execv is implemented as spawn-and-wait (NOT a true
                    # replace), so the parent process keeps living and we get a
                    # fork-tree of stacked hot_reload + bot.py processes. Spawn the
                    # new watcher explicitly, then exit *this* process cleanly.
                    subprocess.Popen([sys.executable, *sys.argv], cwd=str(root))
                    sys.exit(0)
                restarting = True
                break
        except KeyboardInterrupt:
            _stop_bot(child)
            return 130

        if restarting:
            continue
        return child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
