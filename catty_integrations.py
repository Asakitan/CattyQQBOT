from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlretrieve
import zipfile


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


def _project_path(raw: Any, default: str, base_dir: Path, field: str) -> tuple[str, Path]:
    value = os.path.expandvars(str(raw or default)).strip() or default
    path = Path(value).expanduser()
    if path.is_absolute():
        raise ValueError(f"ollama.{field} must be a relative path inside the project folder: {value}")
    resolved = (base_dir / path).resolve()
    project_root = base_dir.resolve()
    if not resolved.is_relative_to(project_root):
        raise ValueError(f"ollama.{field} must stay inside the project folder: {value}")
    relative = str(resolved.relative_to(project_root))
    return relative, resolved


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
    ollama = config.get("ollama", {})
    if isinstance(ollama, dict) and _as_bool(ollama.get("enabled"), default=False):
        try:
            _start_ollama(ollama, config, config_dir)
        except Exception as exc:
            print(f"Failed to start integrated Ollama: {exc}")

    local_training = config.get("local_training", {})
    if isinstance(local_training, dict) and _as_bool(local_training.get("enabled"), default=False):
        _start_local_training(local_training, config_dir)

    qq = config.get("qq", {})
    if not isinstance(qq, dict):
        return
    if _as_bool(qq.get("auto_start_napcat"), default=False):
        _start_napcat(qq, config_dir)


def _default_ollama_archive_url() -> str:
    if os.name == "nt":
        return "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"
    return "https://ollama.com/download/ollama-linux-amd64.tgz"


def _default_ollama_executable() -> str:
    return "ollama.exe" if os.name == "nt" else "ollama"


def _download_file(url: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(url).path).name or "ollama-download"
    archive_path = target_dir / filename
    print(f"Downloading Ollama package: {url}")
    urlretrieve(url, archive_path)
    return archive_path


def _extract_archive(archive_path: Path, install_dir: Path) -> None:
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(install_dir)
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as archive:
            archive.extractall(install_dir)
    else:
        raise ValueError(f"Unsupported Ollama package archive: {archive_path.name}")
    archive_path.unlink(missing_ok=True)


def _find_ollama_executable(install_dir: Path) -> Path | None:
    executable_name = _default_ollama_executable()
    direct_path = install_dir / executable_name
    if direct_path.exists():
        return direct_path
    matches = list(install_dir.rglob(executable_name))
    return matches[0] if matches else None


def _deploy_ollama(ollama: dict[str, Any], install_dir: Path) -> Path:
    executable = _find_ollama_executable(install_dir)
    if executable is not None:
        return executable
    if not _as_bool(ollama.get("auto_install"), default=True):
        raise FileNotFoundError(f"Ollama executable not found in {install_dir}")

    archive_url = str(ollama.get("download_url") or _default_ollama_archive_url()).strip()
    archive_path = _download_file(archive_url, install_dir)
    _extract_archive(archive_path, install_dir)
    executable = _find_ollama_executable(install_dir)
    if executable is None:
        raise FileNotFoundError(f"Ollama package did not contain {_default_ollama_executable()}")
    if os.name != "nt":
        executable.chmod(executable.stat().st_mode | 0o755)
    return executable


def _stop_existing_ollama() -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/IM", "ollama.exe", "/F"], capture_output=True, check=False)
        return
    subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True, check=False)


def _ollama_env(base_dir: Path, models_relative: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = models_relative
    env["PATH"] = str(base_dir / "tools" / "ollama") + os.pathsep + env.get("PATH", "")
    return env


def _wait_ollama_ready(api_url: str, timeout_seconds: float) -> None:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    version_url = api_url.rstrip("/") + "/api/version"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(version_url, timeout=2) as response:
                if response.status < 500:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(1)
    raise TimeoutError(f"Ollama did not become ready at {version_url}: {last_error}")


def _model_available(executable: Path, model: str, *, cwd: Path, env: dict[str, str]) -> bool:
    result = subprocess.run(
        [str(executable), "list"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    wanted = model.strip()
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if columns and columns[0] == wanted:
            return True
    return False


def _ollama_models_to_check(config: dict[str, Any], ollama: dict[str, Any]) -> list[str]:
    models: list[str] = []
    for value in [ollama.get("model")]:
        text = str(value or "").strip()
        if text and text not in models:
            models.append(text)
    local_critic = config.get("local_critic", {})
    if isinstance(local_critic, dict):
        text = str(local_critic.get("model") or "").strip()
        if text and text not in models:
            models.append(text)
    return models


def _pull_ollama_model(
    executable: Path,
    model: str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
) -> None:
    if _model_available(executable, model, cwd=cwd, env=env):
        print(f"Ollama model already available in project folder: {model}")
        return
    print(f"Pulling Ollama model into project folder: {model}")
    subprocess.run(
        [str(executable), "pull", model],
        cwd=str(cwd),
        env=env,
        timeout=max(timeout_seconds, 1.0),
        check=True,
    )


def _start_ollama(ollama: dict[str, Any], config: dict[str, Any], config_dir: Path) -> None:
    if not _as_bool(ollama.get("auto_start"), default=True):
        return

    install_relative, install_dir = _project_path(ollama.get("install_dir"), "tools/ollama", config_dir, "install_dir")
    models_relative, models_dir = _project_path(ollama.get("models_dir"), "models/ollama", config_dir, "models_dir")
    install_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    executable_value = str(ollama.get("executable") or "").strip()
    if executable_value:
        _, executable = _project_path(executable_value, executable_value, config_dir, "executable")
        if not executable.exists():
            raise FileNotFoundError(f"Ollama executable not found: {executable}")
    else:
        executable = _deploy_ollama(ollama, install_dir)

    env = _ollama_env(config_dir, models_relative)
    if _as_bool(ollama.get("stop_existing"), default=True):
        _stop_existing_ollama()
        time.sleep(1)

    creationflags = 0
    if os.name == "nt" and _as_bool(ollama.get("new_console"), default=False):
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    subprocess.Popen(
        [str(executable), "serve"],
        cwd=str(config_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    api_url = str(ollama.get("api_url") or "http://127.0.0.1:11434").strip()
    _wait_ollama_ready(api_url, float(ollama.get("startup_timeout_seconds") or 60))

    print(f"Started project-local Ollama: {Path(install_relative) / executable.name}")
    print(f"Ollama models folder: {models_relative}")

    if _as_bool(ollama.get("auto_pull_model"), default=True):
        for model in _ollama_models_to_check(config, ollama):
            _pull_ollama_model(
                executable,
                model,
                cwd=config_dir,
                env=env,
                timeout_seconds=float(ollama.get("pull_timeout_seconds") or 1800),
            )


def _start_local_training(local_training: dict[str, Any], config_dir: Path) -> None:
    if not _as_bool(local_training.get("auto_train_on_startup"), default=False):
        return
    script_path = config_dir / "scripts" / "auto_train_reply_gate.py"
    config_path = config_dir / "config.json"
    if not script_path.exists():
        print(f"Local training script not found: {script_path}")
        return
    args = [sys.executable, str(script_path), "--config", str(config_path)]
    if int(float(local_training.get("watch_interval_seconds") or 0)) > 0:
        args.append("--watch")
    else:
        args.append("--once")
    subprocess.Popen(
        args,
        cwd=str(config_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("Started local training exporter/watcher")


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
