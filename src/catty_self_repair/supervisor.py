"""Watcher-owned worker lifecycle, publication and post-restart health checks."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import time
from typing import Any, Callable

from .config import ConfigError, RepairConfig, load_config, state_dir
from .storage import FileLock, atomic_json, read_json, reject_links


STARTUP_ENV = "CATTY_SELF_REPAIR_STARTUP_FILE"
STARTUP_RELATIVE_PATH = "logs/bot_startup_ready.json"


class RecoveryBlocked(RuntimeError):
    """An unfinished release needs operator reconciliation before bot startup."""


def _windows_parent_pid(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent_pid", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
            ("executable", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.pid == pid:
                return int(entry.parent_pid)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        kernel.CloseHandle(snapshot)


def _startup_pid_matches(expected: int, actual: object) -> bool:
    if type(actual) is not int or actual <= 0 or expected <= 0:
        return False
    if actual == expected:
        return True
    if os.name != "nt":
        return False
    try:
        return _windows_parent_pid(actual) == expected
    except OSError:
        return False


def _worker_env() -> dict[str, str]:
    names = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return env


class RepairSupervisor:
    """Only this owner may publish; callers must stop/join the bot first."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = root.absolute()
        self.state = state_dir(self.root)
        self.clock = clock
        self.worker: subprocess.Popen[bytes] | None = None
        self._worker_output: Any = None
        self._worker_fingerprint = ""
        self._next_spawn = 0.0
        self._config = RepairConfig()
        self._lock: FileLock | None = None
        self._active_run = ""
        self._health_config = RepairConfig()
        self._started_at = 0.0
        self._bot_pid = 0
        self._healthy_since: float | None = None

    @property
    def active(self) -> bool:
        return bool(self._active_run)

    def _record(self, status: str, **extra: Any) -> None:
        # Deliberately exclude raw exception strings, config and provider bodies.
        atomic_json(self.state / "supervisor_status.json", {
            "status": status, "at": self.clock(), "run_id": self._active_run, **extra,
        })

    @staticmethod
    def _require_recovered(result: dict[str, Any]) -> None:
        if result.get("status") == "conflict":
            raise RecoveryBlocked("self repair rollback conflicts; inspect data/self_repair/active_release.json")

    def start(self) -> None:
        reject_links(self.root)
        self._lock = FileLock(self.state / "supervisor.lock")
        if not self._lock.acquire():
            raise RecoveryBlocked("another watcher owns self repair for this workspace")
        # Recovery is mandatory even when the operator has disabled new repairs.
        if (self.state / "active_release.json").exists():
            from .workspace import recover_release
            result = recover_release(self.root)
            self._require_recovered(result)
            self._record("recovered", recovery=result.get("status"))

    def _stop_worker(self) -> None:
        process = self.worker
        self.worker = None
        try:
            if process is not None and process.poll() is None:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                    process.wait(timeout=5)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    # Own process group only. A validator child must not survive disable/shutdown.
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, timeout=10,
                        )
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
        finally:
            if self._worker_output is not None:
                self._worker_output.close()
                self._worker_output = None

    def poll_worker(self) -> None:
        try:
            config = load_config(self.root)
        except (ConfigError, ValueError, OSError):
            self._stop_worker()
            self._config = RepairConfig()
            self._record("invalid_config")
            return
        self._config = config
        fingerprint = config.fingerprint()
        if not config.enabled or not config.configured:
            if self.worker is not None:
                self._stop_worker()
            self._worker_fingerprint = ""
            return
        if self.worker is not None and self._worker_fingerprint != fingerprint:
            self._stop_worker()
            self._next_spawn = 0.0
        if self.worker is not None:
            code = self.worker.poll()
            if code is None:
                return
            self._stop_worker()
            self._next_spawn = self.clock() + 30.0
            self._record("worker_exited", exit_code=code)
        if self.active or (self.state / "ready.json").exists() or self.clock() < self._next_spawn:
            return
        log_path = self.state / "worker.log"
        reject_links(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            log_path.replace(log_path.with_suffix(".previous.log"))
        self._worker_output = log_path.open("ab")
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self.worker = subprocess.Popen(
                [sys.executable, "-B", "-X", "utf8", str(self.root / "scripts/catty_self_repair.py"), "--root", str(self.root)],
                cwd=self.root, env=_worker_env(), stdin=subprocess.DEVNULL,
                stdout=self._worker_output, stderr=subprocess.STDOUT, **kwargs,
            )
        except OSError as exc:
            self._worker_output.close()
            self._worker_output = None
            self._next_spawn = self.clock() + 30.0
            self._record("worker_start_failed", error_type=type(exc).__name__)
            return
        self._worker_fingerprint = fingerprint

    def _reject_ready(self, reason: str) -> None:
        path = self.state / "ready.json"
        reject_links(path)
        if path.exists():
            reject_links(self.state / "rejected_ready.json")
            path.replace(self.state / "rejected_ready.json")
        self._record("candidate_rejected", reason=reason)

    def _ready_release(self) -> tuple[Path, RepairConfig] | None:
        path = self.state / "ready.json"
        if not path.exists() or self.active:
            return None
        try:
            reject_links(path)
            if path.stat().st_size > 16_384:
                raise ValueError("oversize ready marker")
            ready = read_json(path)
            config = load_config(self.root)
            if not config.enabled or not config.auto_apply or ready.get("config_fingerprint") != config.fingerprint():
                self._reject_ready("disabled_or_config_changed")
                return None
            run_id = ready.get("run_id")
            if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
                raise ValueError("invalid run id")
            relative = ready.get("release_path")
            expected = f"data/self_repair/runs/{run_id}/release.json"
            if not isinstance(relative, str) or PurePosixPath(relative).as_posix() != expected or relative != expected:
                raise ValueError("invalid release path")
            release = self.root / relative
            reject_links(release)
            if not release.is_file():
                raise ValueError("missing release")
            return release, config
        except (OSError, ValueError, ConfigError):
            self._reject_ready("invalid_ready_marker")
            return None

    def has_ready(self) -> bool:
        return self._ready_release() is not None

    def apply_pending(self) -> bool:
        """Call only after the previous bot process has actually terminated."""
        ready = self._ready_release()
        if ready is None:
            return False
        release, config = ready
        self._stop_worker()
        try:
            current = load_config(self.root)
            if not current.enabled or not current.auto_apply or not current.configured or current.fingerprint() != config.fingerprint():
                self._reject_ready("config_changed_while_stopping_worker")
                return False
        except (ValueError, OSError):
            self._reject_ready("invalid_config_after_worker_stop")
            return False
        from .workspace import apply_release, recover_release
        try:
            result = apply_release(self.root, release, config)
            if result.get("status") != "applied":
                self._require_recovered(result)
                self._reject_ready("not_applied")
                return False
        except RecoveryBlocked:
            raise
        except Exception as exc:
            recovered = recover_release(self.root)
            self._require_recovered(recovered)
            self._reject_ready("publication_failed_" + type(exc).__name__)
            return False
        self._active_run = str(result["run_id"])
        self._health_config = config
        self._healthy_since = None
        (self.state / "ready.json").unlink(missing_ok=True)
        self._record("awaiting_health")
        return True

    def bot_started(self, pid: int, started_at: float) -> None:
        self._bot_pid = pid
        self._started_at = started_at
        self._healthy_since = None

    def health_action(self, *, running: bool, now: float | None = None) -> str:
        """Return a rollback reason, or confirm after a continuous healthy window."""
        if not self.active:
            return ""
        now = self.clock() if now is None else now
        if not running:
            return "process_exit"
        healthy = False
        try:
            startup = self.root / STARTUP_RELATIVE_PATH
            reject_links(startup)
            if startup.stat().st_size > 4_096:
                raise ValueError("oversize startup marker")
            record = read_json(startup)
            at = record.get("at")
            beat = (self.root / "logs/bot_heartbeat").stat().st_mtime
            healthy = bool(
                record.get("plugin_loaded") is True and _startup_pid_matches(self._bot_pid, record.get("pid"))
                and type(at) in (float, int) and math.isfinite(at) and self._started_at <= at <= now
                and self._started_at <= beat <= now and now - beat <= 45.0
            )
        except (OSError, ValueError):
            pass
        if healthy:
            if self._healthy_since is None:
                self._healthy_since = now
            if now - self._healthy_since >= self._health_config.health_grace_seconds:
                from .workspace import confirm_release
                result = confirm_release(self.root)
                self._require_recovered(result)
                if result.get("status") != "confirmed":
                    raise RecoveryBlocked("self repair confirmation did not complete")
                self._record("confirmed")
                self._active_run = ""
                return ""
        else:
            self._healthy_since = None
        if now - self._started_at >= self._health_config.health_timeout_seconds:
            return "startup_or_heartbeat_timeout"
        return ""

    def rollback(self, reason: str) -> None:
        from .workspace import rollback_release
        self._stop_worker()
        result = rollback_release(self.root, reason)
        self._require_recovered(result)
        if self.active and result.get("status") != "rolled_back":
            raise RecoveryBlocked("active release did not produce a completed rollback receipt")
        self._record("rolled_back", reason=reason)
        self._active_run = ""
        self._healthy_since = None

    def close(self) -> None:
        try:
            self._stop_worker()
        finally:
            if self._lock is not None:
                self._lock.close()
                self._lock = None