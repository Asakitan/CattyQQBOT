from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _resolve_path(raw: Any, base_dir: Path) -> Path:
    value = os.path.expandvars(str(raw or "")).strip()
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _windows_process_running(process_name: str) -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    return process_name.lower() in result.stdout.lower()


def _list_args(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def start_integrated_processes(config: dict[str, Any], config_dir: Path) -> None:
    qq = config.get("qq", {})
    if not isinstance(qq, dict):
        return
    if _as_bool(qq.get("auto_start_napcat"), default=False):
        _start_napcat(qq, config_dir)


def _start_napcat(qq: dict[str, Any], config_dir: Path) -> None:
    workdir = _resolve_path(qq.get("napcat_workdir", "tools/napcat-onekey/bootmain"), config_dir)
    executable = str(qq.get("napcat_executable") or "NapCatWinBootMain.exe")
    exe_path = Path(executable)
    if not exe_path.is_absolute():
        exe_path = workdir / exe_path

    if _as_bool(qq.get("skip_if_napcat_running"), default=True) and _windows_process_running(exe_path.name):
        print(f"NapCat already seems to be running: {exe_path.name}")
        return

    if not exe_path.exists():
        installer = _resolve_path("tools/napcat-onekey/NapCatInstaller.exe", config_dir)
        print(f"NapCat executable not found: {exe_path}")
        if installer.exists():
            print(f"Run this once to initialize NapCat: {installer}")
        return

    args = _list_args(qq.get("napcat_args"))
    account = str(qq.get("account") or "").strip()
    if not args and account:
        args = [account]

    creationflags = 0
    if os.name == "nt" and _as_bool(qq.get("napcat_new_console"), default=True):
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    subprocess.Popen(
        [str(exe_path), *args],
        cwd=str(workdir),
        creationflags=creationflags,
    )
    suffix = f" with account {account}" if account else ""
    print(f"Started NapCat: {exe_path}{suffix}")
