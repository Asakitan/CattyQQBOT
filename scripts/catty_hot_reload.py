from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Iterator


DEFAULT_POLL_SECONDS = 1.5
DEFAULT_DEBOUNCE_SECONDS = 1.0
# 优雅退出等待上限: uvicorn 收到 CTRL_BREAK 后要关 WS、跑 on_shutdown 落盘钩子
DEFAULT_STOP_TIMEOUT_SECONDS = 20.0
# bot 每 15s 写一次心跳; 超过这个时长没更新就当事件循环卡死, 重启 bot (0 = 关闭)
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 180.0
HEARTBEAT_ENV = "CATTY_HEARTBEAT_FILE"
HEARTBEAT_RELATIVE_PATH = Path("logs") / "bot_heartbeat"
STARTUP_ENV = "CATTY_SELF_REPAIR_STARTUP_FILE"
STARTUP_RELATIVE_PATH = Path("logs") / "bot_startup_ready.json"
# bot 自己退出 (崩溃/启动失败) 后的重启等待; 稳定运行超过 RESET 秒后从头计
CRASH_BACKOFF_SECONDS = (2.0, 5.0, 15.0, 30.0, 60.0)
CRASH_BACKOFF_RESET_SECONDS = 600.0
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
    "src/catty_qq_ai/data/fdj_scene_delta",
)

FileSignature = tuple[int, int]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_ignored_relative(relative: str) -> bool:
    if any(part in IGNORED_DIR_NAMES for part in relative.split("/")):
        return True
    # 主人 2026-05-29: 路径前缀排除 (CPU 引擎数据目录由进程内 watch_loop 热重载)
    return any(relative.startswith(prefix) for prefix in IGNORED_PATH_PREFIXES)


def is_ignored_path(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    return _is_ignored_relative(relative.as_posix())


def _scan_watch_entries(root: Path) -> Iterator[tuple[str, Path, FileSignature]]:
    """遍历监听范围, 逐个给出 (相对路径, 路径, (mtime_ns, size))。

    忽略目录在遍历时直接剪枝, 签名取 scandir 目录项自带的 mtime/size。旧实现先 rglob
    全部 ~5k 个文件 (含 2.5k 个 cpu_engine 数据文件) 再逐个 resolve() 过滤,
    守护进程常驻吃掉约 1/3 个核。
    """
    for name in WATCH_FILES:
        path = root / name
        if path.suffix.lower() not in WATCH_EXTENSIONS or _is_ignored_relative(name):
            continue
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        yield name, path, (stat.st_mtime_ns, stat.st_size)
    for name in WATCH_DIRS:
        if _is_ignored_relative(name):
            continue
        pending: list[tuple[Path, str]] = [(root / name, name)]
        while pending:
            directory, relative_dir = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError:
                continue
            for entry in entries:
                relative = f"{relative_dir}/{entry.name}"
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not _is_ignored_relative(relative):
                            pending.append((Path(entry.path), relative))
                        continue
                    if not entry.is_file():
                        continue
                    if os.path.splitext(entry.name)[1].lower() not in WATCH_EXTENSIONS:
                        continue
                    if _is_ignored_relative(relative):
                        continue
                    stat = entry.stat()
                except OSError:
                    continue
                yield relative, Path(entry.path), (stat.st_mtime_ns, stat.st_size)


def iter_watch_files(root: Path) -> list[Path]:
    return sorted({path for _relative, path, _signature in _scan_watch_entries(root)})


def snapshot_files(root: Path) -> dict[str, FileSignature]:
    return {relative: signature for relative, _path, signature in _scan_watch_entries(root)}


def changed_files(before: dict[str, FileSignature], after: dict[str, FileSignature]) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [key for key in keys if before.get(key) != after.get(key)]


def _load_settings(root: Path) -> dict[str, object]:
    defaults: dict[str, object] = {
        "enabled": True,
        "poll_seconds": DEFAULT_POLL_SECONDS,
        "debounce_seconds": DEFAULT_DEBOUNCE_SECONDS,
        "restart_on_code_change": True,
        "restart_on_crash": True,
        "stop_timeout_seconds": DEFAULT_STOP_TIMEOUT_SECONDS,
        "heartbeat_timeout_seconds": DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
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


def disable_console_quick_edit() -> None:
    """关掉控制台「快速编辑」。

    开着时 (RDP 进来) 在窗口里点一下就进入选择状态, 所有往这个控制台写输出的进程
    都会被挂起直到取消选择。控制台模式按控制台共享, 守护进程和 bot 一起受影响。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))  # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        enable_quick_edit, enable_extended_flags = 0x0040, 0x0080
        new_mode = (mode.value | enable_extended_flags) & ~enable_quick_edit
        if new_mode != mode.value:
            kernel32.SetConsoleMode(handle, new_mode)
    except Exception:  # noqa: BLE001
        pass


def heartbeat_path(root: Path) -> Path:
    return root / HEARTBEAT_RELATIVE_PATH


class HeartbeatMonitor:
    """读 bot 事件循环写的心跳文件, 判断它是不是卡死了。

    只认本次启动之后写下的心跳: 还没写过 (启动中 / 不写心跳的旧版 bot) 一律不判死,
    避免误杀。
    """

    def __init__(self, path: Path, started_at: float) -> None:
        self.path = path
        self.started_at = started_at

    def is_stale(self, now: float, timeout: float) -> bool:
        if timeout <= 0:
            return False
        try:
            written_at = self.path.stat().st_mtime
        except OSError:
            return False
        if written_at < self.started_at:
            return False
        return now - written_at > timeout


def crash_backoff_seconds(streak: int) -> float:
    return CRASH_BACKOFF_SECONDS[min(max(streak, 0), len(CRASH_BACKOFF_SECONDS) - 1)]


def _child_popen_kwargs() -> dict[str, object]:
    # Windows: 独立进程组才能单独给 bot 发 CTRL_BREAK 走优雅退出; 副作用是控制台
    # Ctrl+C 只会到守护进程, 由守护进程负责停掉 bot。
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


def _start_bot(root: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["CATTY_HOT_RELOAD_CHILD"] = "1"
    env[HEARTBEAT_ENV] = str(heartbeat_path(root))
    env[STARTUP_ENV] = str(root / STARTUP_RELATIVE_PATH)
    return subprocess.Popen(
        [sys.executable, str(root / "bot.py")],
        cwd=str(root),
        env=env,
        **_child_popen_kwargs(),
    )


def _stop_bot(process: subprocess.Popen[bytes], timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
    """先请求优雅退出, 超时再强杀。

    Windows 上 Popen.send_signal(SIGTERM) 等于 TerminateProcess: 旧实现每次热重载都是
    硬杀, on_shutdown 落盘钩子一次都没执行过 (生产日志 664 次启动 / 0 次 shutdown)。
    uvicorn 在 Windows 上会处理 SIGBREAK, 所以改发 CTRL_BREAK_EVENT。
    """
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
    except (OSError, ValueError):
        # 发不出信号 (例如守护进程没挂在控制台上): 退回旧的直接结束, 不白等超时
        timeout = 0.0
    try:
        process.wait(timeout=max(float(timeout), 0.0))
        return
    except subprocess.TimeoutExpired:
        print(f"Catty did not exit {timeout:.0f}s after the stop signal; killing it.", flush=True)
    process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _clear_heartbeat(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _self_key(root: Path) -> str:
    try:
        return Path(__file__).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""


def _respawn_watcher(root: Path) -> None:
    # Windows: os.execv is implemented as spawn-and-wait (NOT a true
    # replace), so the parent process keeps living and we get a
    # fork-tree of stacked hot_reload + bot.py processes. Spawn the
    # new watcher explicitly, then exit *this* process cleanly.
    subprocess.Popen([sys.executable, *sys.argv], cwd=str(root))


def _wait_before_crash_restart(root: Path, snapshot: dict[str, FileSignature], delay: float) -> list[str]:
    """崩溃后的退避等待; 期间有代码变更 (多半是推了修复) 就提前返回变更列表。"""
    deadline = time.monotonic() + delay
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        time.sleep(min(remaining, 1.0))
        changes = changed_files(snapshot, snapshot_files(root))
        if changes:
            return changes


def _watcher_event(root: Path, level: str, message: str) -> None:
    """Keep crash/heartbeat evidence even if bot.py fails before logging starts."""
    print(message, flush=True)
    try:
        path = root / "logs/hot_reload.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix(".previous.log"))
        text = message.replace("\r", " ").replace("\n", " ")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {level} | catty_hot_reload:watcher:0 - {text}\n")
    except OSError:
        pass


def _stop_for_repair(child: subprocess.Popen[bytes], timeout: float) -> None:
    _stop_bot(child, timeout)
    if child.poll() is None:
        raise RuntimeError("Bot did not terminate; refusing to publish or roll back source files")


def _run_watcher(root: Path, repair) -> int:
    crash_streak = 0
    while True:
        # No bot exists here: recover/apply whole transactions before any import of new code.
        repair.poll_worker()
        repair.apply_pending()
        settings = _load_settings(root)
        stop_timeout = _as_float(settings.get("stop_timeout_seconds"), DEFAULT_STOP_TIMEOUT_SECONDS, 1.0)
        beat_path = heartbeat_path(root)
        _clear_heartbeat(beat_path)
        _clear_heartbeat(root / STARTUP_RELATIVE_PATH)
        started_wall = time.time()
        try:
            child = _start_bot(root)
        except OSError:
            if repair.active:
                repair.rollback("process_start_failed")
                continue
            _watcher_event(root, "ERROR", "Catty process could not be started.")
            raise
        repair.bot_started(child.pid, started_wall)
        started_monotonic = time.monotonic()
        heartbeat = HeartbeatMonitor(beat_path, started_at=started_wall)
        snapshot = snapshot_files(root)
        restart_reason = ""
        try:
            while child.poll() is None:
                settings = _load_settings(root)
                poll_seconds = _as_float(settings.get("poll_seconds"), DEFAULT_POLL_SECONDS, 0.2)
                stop_timeout = _as_float(settings.get("stop_timeout_seconds"), DEFAULT_STOP_TIMEOUT_SECONDS, 1.0)
                time.sleep(poll_seconds)
                repair.poll_worker()
                rollback_reason = repair.health_action(running=child.poll() is None)
                if rollback_reason:
                    _stop_for_repair(child, stop_timeout)
                    repair.rollback(rollback_reason)
                    restart_reason = "repair_rollback"
                    break
                if repair.has_ready():
                    _stop_for_repair(child, stop_timeout)
                    repair.apply_pending()
                    restart_reason = "repair_candidate"
                    break
                beat_timeout = _as_float(
                    settings.get("heartbeat_timeout_seconds"), DEFAULT_HEARTBEAT_TIMEOUT_SECONDS, 0.0
                )
                if heartbeat.is_stale(time.time(), beat_timeout):
                    _watcher_event(root, "WARNING", f"Catty heartbeat is older than {beat_timeout:.0f}s (event loop frozen?); restarting it.")
                    _stop_for_repair(child, stop_timeout)
                    if repair.active:
                        repair.rollback("stale_heartbeat")
                    restart_reason = "heartbeat"
                    break
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
                print("Hot reload restarting Catty after file changes: " + ", ".join(changes[:8]), flush=True)
                _stop_for_repair(child, stop_timeout)
                if repair.active:
                    repair.rollback("source_changed_during_health_check")
                if _self_key(root) in changes:
                    repair.close()
                    _respawn_watcher(root)
                    return 0
                restart_reason = "code"
                break
        except KeyboardInterrupt:
            _stop_bot(child, stop_timeout)
            return 130
        except BaseException:
            _stop_bot(child, stop_timeout)
            raise

        if restart_reason:
            crash_streak = 0
            continue

        rollback_reason = repair.health_action(running=False)
        if rollback_reason:
            repair.rollback(rollback_reason)
            crash_streak = 0
            continue

        # bot 自己退出了 (崩溃 / 启动失败 / 被外部结束): 旧实现在这里直接 return,
        # 守护进程跟着退出, 之后推代码也不会再拉起 bot。
        exit_code = child.returncode
        if not _as_bool(settings.get("restart_on_crash"), default=True):
            return exit_code or 0
        ran_for = time.monotonic() - started_monotonic
        if ran_for >= CRASH_BACKOFF_RESET_SECONDS:
            crash_streak = 0
        delay = crash_backoff_seconds(crash_streak)
        crash_streak += 1
        _watcher_event(root, "ERROR", f"Catty exited with code {exit_code} after {ran_for:.0f}s; restarting in {delay:.0f}s.")
        try:
            changes = _wait_before_crash_restart(root, snapshot, delay)
        except KeyboardInterrupt:
            return 130
        if changes:
            print("Code changed while waiting; restarting Catty now: " + ", ".join(changes[:8]), flush=True)
            if _self_key(root) in changes:
                repair.close()
                _respawn_watcher(root)
                return 0


def main() -> int:
    root = _project_root()
    disable_console_quick_edit()
    sys.path.insert(0, str(root / "src"))
    from catty_self_repair.supervisor import RecoveryBlocked, RepairSupervisor

    repair = RepairSupervisor(root)
    print("Catty hot reload watcher started.", flush=True)
    try:
        repair.start()
        return _run_watcher(root, repair)
    except RecoveryBlocked as exc:
        print(str(exc), flush=True)
        return 2
    finally:
        repair.close()


if __name__ == "__main__":
    raise SystemExit(main())
