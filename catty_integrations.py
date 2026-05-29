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


def _ssl_context_with_certifi():
    """Build an SSL context that uses certifi's CA bundle when available.

    Python 3.14 on freshly provisioned Windows often lacks the system CA chain
    GitHub uses, causing `CERTIFICATE_VERIFY_FAILED` during the Ollama download.
    certifi ships an up-to-date Mozilla bundle that resolves it; we fall back
    to the platform default when certifi isn't installed.
    """
    import ssl
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download_file(url: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(url).path).name or "ollama-download"
    archive_path = target_dir / filename
    print(f"Downloading Ollama package: {url}")
    import urllib.request
    ctx = _ssl_context_with_certifi()
    with urllib.request.urlopen(url, context=ctx, timeout=600) as response:
        with open(archive_path, "wb") as fh:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
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


def _ollama_env(
    base_dir: Path,
    models_relative: str,
    *,
    num_thread: int | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = models_relative
    env["PATH"] = str(base_dir / "tools" / "ollama") + os.pathsep + env.get("PATH", "")
    # 任何 model 一旦 load 就永驻内存,避免反复 cold start(35s)。warmup loop 会定期 ping。
    env["OLLAMA_KEEP_ALIVE"] = "-1"
    # 限制 Ollama 推理线程数,给系统和 MC 留余量(不锁 affinity,只控总线程)
    if num_thread is not None and num_thread > 0:
        env["OLLAMA_NUM_THREAD"] = str(num_thread)
        env["OLLAMA_NUM_THREADS"] = str(num_thread)  # 老版别名兼容
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


def _ollama_is_healthy(api_url: str, *, attempts: int = 3) -> bool:
    """快速探活: ollama 已经在 serve 且 API 响应就返回 True。

    用于 bot 重启时判断要不要复用已有 ollama —— 不像 _wait_ollama_ready 会长轮询
    等待启动, 这里只做几次轻量重试就拍板。重试是为了抗瞬时抖动(首连冷 socket /
    CPU 尖峰偶尔 >1s): 误判成"没起来"会害已 load 的模型被冤杀冷重载(~35s), 代价远
    高于多探两下, 所以宁可多试。真没起来时几次很快失败, 落到正常 kill+start 流程。
    """
    import urllib.error
    import urllib.request

    version_url = api_url.rstrip("/") + "/api/version"
    for attempt in range(max(attempts, 1)):
        try:
            with urllib.request.urlopen(version_url, timeout=3) as response:
                return response.status < 500
        except (OSError, urllib.error.URLError):
            if attempt + 1 < attempts:
                time.sleep(0.5)
    return False


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
    # ollama list 列出的是 "name:tag" 形式(e.g. "catty-7b:latest"),用户配置里可能不带 tag。
    # 当查询的名字本身没带 ":" 时,把 "wanted" 和 "wanted:latest" 都当成命中。
    candidates = {wanted}
    if ":" not in wanted:
        candidates.add(f"{wanted}:latest")
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if columns and columns[0] in candidates:
            return True
    return False


def _ollama_models_to_check(config: dict[str, Any], ollama: dict[str, Any]) -> list[str]:
    models: list[str] = []
    for value in [ollama.get("model")]:
        text = str(value or "").strip()
        if text and text not in models:
            models.append(text)
    extra = ollama.get("extra_models") or []
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, list):
        for item in extra:
            text = str(item or "").strip()
            if text and text not in models:
                models.append(text)
    local_critic = config.get("local_critic", {})
    if isinstance(local_critic, dict):
        text = str(local_critic.get("model") or "").strip()
        if text and text not in models:
            models.append(text)
    # ai_fallback.model (例如 catty-7b) 在 cloud 超时后被立即调用。如果它没在 ollama
    # 注册表里,_pull_ollama_model 会尝试 pull,本地 Modelfile 派生的模型 pull 会失败但
    # 至少给出明确报错;比"runtime 第一条用户消息才发现 404"要好得多。
    # 主人 2026-05-28: ai_fallback 改云端 deepseek 后, base_url 是 api.deepseek.com,
    # 不能再无条件 ollama pull "deepseek-v4-flash" (会 timeout 1800s). 只在 base_url
    # 是本地 (localhost / 127.0.0.1 / 空 / 含 ollama) 时才 pull.
    ai_fallback = config.get("ai_fallback", {})
    if isinstance(ai_fallback, dict):
        fallback_url = str(ai_fallback.get("base_url") or "").lower()
        is_local_fallback = (
            not fallback_url
            or "localhost" in fallback_url
            or "127.0.0.1" in fallback_url
            or "ollama" in fallback_url
        )
        if is_local_fallback:
            text = str(ai_fallback.get("model") or "").strip()
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

    num_thread_raw = ollama.get("num_thread")
    try:
        num_thread = int(num_thread_raw) if num_thread_raw not in (None, "", 0) else None
    except (TypeError, ValueError):
        num_thread = None
    env = _ollama_env(config_dir, models_relative, num_thread=num_thread)
    api_url = str(ollama.get("api_url") or "http://127.0.0.1:11434").strip()

    # 主人 2026-05-29: bot 热重启不应连累 ollama。若 ollama 已经在 serve 且 API 健康,
    # 直接复用现有进程 —— 不 kill、不重启, 让 OLLAMA_KEEP_ALIVE=-1 常驻内存的模型
    # 不必冷重载(~35s)。只有 ollama 真没起来时才往下走原来的 kill+start 流程。
    if _as_bool(ollama.get("reuse_if_running"), default=True) and _ollama_is_healthy(api_url):
        print(f"Ollama already serving at {api_url}; reusing existing process (skip stop/restart).")
        # ollama list 只读注册表, 不会卸载已 load 的模型, 安全确认模型在册。
        if _as_bool(ollama.get("auto_pull_model"), default=True):
            for model in _ollama_models_to_check(config, ollama):
                _pull_ollama_model(
                    executable,
                    model,
                    cwd=config_dir,
                    env=env,
                    timeout_seconds=float(ollama.get("pull_timeout_seconds") or 1800),
                )
        _ensure_catty_derived_model(config, executable, config_dir, env)
        return

    if _as_bool(ollama.get("stop_existing"), default=True):
        _stop_existing_ollama()
        time.sleep(1)

    creationflags = 0
    if os.name == "nt":
        if _as_bool(ollama.get("new_console"), default=False):
            creationflags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        # 默认让 ollama 走 BelowNormal,Windows 调度器自动让位给前台 MC/系统
        if _as_bool(ollama.get("below_normal_priority"), default=True):
            creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

    proc = subprocess.Popen(
        [str(executable), "serve"],
        cwd=str(config_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    affinity_label = ""
    affinity_raw = ollama.get("cpu_affinity_mask")
    if affinity_raw not in (None, "", 0) and os.name == "nt":
        try:
            mask = int(affinity_raw, 0) if isinstance(affinity_raw, str) else int(affinity_raw)
        except (TypeError, ValueError):
            mask = 0
        if mask > 0:
            try:
                import ctypes
                # PROCESS_SET_INFORMATION (0x0200) | PROCESS_QUERY_INFORMATION (0x0400)
                hproc = ctypes.windll.kernel32.OpenProcess(0x0200 | 0x0400, False, proc.pid)
                if hproc:
                    ok = ctypes.windll.kernel32.SetProcessAffinityMask(hproc, mask)
                    ctypes.windll.kernel32.CloseHandle(hproc)
                    if ok:
                        affinity_label = f", affinity={hex(mask)}"
            except Exception:  # noqa: BLE001
                pass

    if num_thread:
        print(f"Ollama OLLAMA_NUM_THREAD={num_thread}, BelowNormal priority{affinity_label}")
    else:
        print(f"Ollama starting with default thread count, BelowNormal priority{affinity_label}")
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

    # 把人格烧进 derived 模型(catty-7b),让 fallback 调用不再发 system prompt
    _ensure_catty_derived_model(config, executable, config_dir, env)


def _ensure_catty_derived_model(
    config: dict[str, Any],
    executable: Path,
    config_dir: Path,
    env: dict[str, str],
) -> None:
    """Build a Catty-flavored Ollama model from a base model + persona Modelfile.

    Reads ``ai_fallback.derived_model_name`` (default ``catty-7b``) and
    ``ai_fallback.base_model`` (default ``qwen2.5:7b``). Skips if disabled or
    base model isn't pulled yet.
    """
    fallback = config.get("ai_fallback")
    if not isinstance(fallback, dict):
        return
    if not _as_bool(fallback.get("enabled"), default=False):
        return
    # 默认不在 bot 启动时主动 create——人家会从远端单独控制烧模型,启动时只有人为
    # 把 auto_build_derived_model=true 打开后才会自己 build。
    if not _as_bool(fallback.get("auto_build_derived_model"), default=False):
        return
    derived_name = str(fallback.get("derived_model_name") or "catty-7b").strip()
    base_model = str(fallback.get("base_model") or "qwen2.5:7b").strip()
    if not derived_name or not base_model:
        return
    if not _model_available(executable, base_model, cwd=config_dir, env=env):
        print(f"derived model skipped: base {base_model} not pulled yet")
        return

    # Import builder lazily; needs src on sys.path
    import sys as _sys
    src_dir = config_dir / "src"
    if str(src_dir) not in _sys.path:
        _sys.path.insert(0, str(src_dir))
    try:
        from catty_qq_ai.modelfile_builder import (
            build_modelfile_content,
            content_signature,
            read_recorded_signature,
            write_modelfile_if_changed,
            write_recorded_signature,
        )
    except ImportError as exc:
        print(f"derived model skipped: cannot import modelfile_builder ({exc})")
        return

    chat = config.get("chat") if isinstance(config.get("chat"), dict) else {}
    system_prompt = str(chat.get("system_prompt") or "")
    num_ctx = int(fallback.get("num_ctx") or 4096)
    try:
        temperature = float(fallback.get("temperature") if fallback.get("temperature") is not None else 0.7)
    except (TypeError, ValueError):
        temperature = 0.7
    extra_body = fallback.get("extra_body") if isinstance(fallback.get("extra_body"), dict) else {}
    nt_raw = (extra_body.get("options") or {}).get("num_thread") if isinstance(extra_body.get("options"), dict) else None
    num_thread: int | None
    try:
        num_thread = int(nt_raw) if nt_raw is not None else None
    except (TypeError, ValueError):
        num_thread = None

    content = build_modelfile_content(
        base_model,
        system_prompt,
        num_ctx=num_ctx,
        temperature=temperature,
        num_thread=num_thread,
    )
    modelfile_path = config_dir / "tools" / "catty_modelfile" / f"{derived_name}.Modelfile"
    file_changed = write_modelfile_if_changed(modelfile_path, content)
    new_sig = content_signature(content)
    old_sig = read_recorded_signature(modelfile_path)
    already_built = _model_available(executable, derived_name, cwd=config_dir, env=env)

    if not file_changed and old_sig == new_sig and already_built:
        print(f"Catty derived model {derived_name} already up to date (sig {new_sig[:12]})")
        return

    print(f"Building Catty derived model {derived_name} from {base_model} (sig {new_sig[:12]})")
    try:
        subprocess.run(
            [str(executable), "create", derived_name, "-f", str(modelfile_path)],
            cwd=str(config_dir),
            env=env,
            check=True,
            timeout=600,
        )
        write_recorded_signature(modelfile_path, new_sig)
        print(f"Catty derived model {derived_name} created ✓")
    except subprocess.CalledProcessError as exc:
        print(f"failed to create derived model {derived_name}: {exc}")
    except subprocess.TimeoutExpired:
        print(f"derived model {derived_name} create timed out")


def _start_local_training(local_training: dict[str, Any], config_dir: Path) -> None:
    if not _as_bool(local_training.get("auto_train_on_startup"), default=False):
        _start_training_dashboard(local_training, config_dir)
        return
    script_path = config_dir / "scripts" / "auto_train_reply_gate.py"
    config_path = config_dir / "config.json"
    if not script_path.exists():
        print(f"Local training script not found: {script_path}")
        _start_training_dashboard(local_training, config_dir)
        return
    args = [sys.executable, str(script_path), "--config", str(config_path)]
    if int(float(local_training.get("watch_interval_seconds") or 0)) > 0:
        args.append("--watch")
    else:
        args.append("--once")

    log_path = _resolve_path(local_training.get("progress_log_path", "training/local_training.log"), config_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    subprocess.Popen(
        args,
        cwd=str(config_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        display_log_path = log_path.relative_to(config_dir)
    except ValueError:
        display_log_path = log_path
    print(f"Started local training exporter/watcher; log: {display_log_path}")
    _start_training_dashboard(local_training, config_dir)


def _gui_python_executable() -> str:
    if os.name != "nt":
        return sys.executable
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(executable)


def _start_training_dashboard(local_training: dict[str, Any], config_dir: Path) -> None:
    if not _as_bool(local_training.get("progress_window_enabled"), default=False):
        return
    script_path = _resolve_path(
        local_training.get("progress_window_script", "scripts/catty_training_dashboard.py"),
        config_dir,
    )
    config_path = config_dir / "config.json"
    if not script_path.exists():
        print(f"Local training progress window script not found: {script_path}")
        return

    poll_seconds = max(float(local_training.get("progress_window_poll_seconds") or 5), 1.0)
    args = [
        _gui_python_executable(),
        str(script_path),
        "--config",
        str(config_path),
        "--poll-seconds",
        str(poll_seconds),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        args,
        cwd=str(config_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    print("Started local training progress window")


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
