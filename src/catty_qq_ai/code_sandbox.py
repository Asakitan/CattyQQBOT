"""Catty Python sandbox (2026-10-26).

The bot can write/run Python and install wheels without touching the host
environment:

- work / HOME / TEMP / packages all live under ``data/python_sandbox``;
- the child interpreter runs with ``-I -B -X utf8`` and a scrubbed environment;
- a generated launcher installs audit hooks that deny reads outside the sandbox
  (plus the Python runtime), writes outside the sandbox, network sockets,
  subprocess / ctypes / registry / native file APIs;
- pip installs only binary wheels into the sandbox ``packages`` dir.

This is a best-effort user-mode sandbox, not an OS-level boundary.  The real
security contract is the system prompt (CODE_SANDBOX_SYSTEM_PROMPT) plus these
runtime restrictions.  Do not describe it as a hypervisor/container.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_SANDBOX_DIR = "data/python_sandbox"

CODE_SANDBOX_SYSTEM_PROMPT = """\
【Python 沙箱安全规则（最高优先，必须遵守）】
1. 需要写/跑 Python、生成文件、安装第三方包时，只用 catty_python_run / catty_python_install；不要用 catty_run_code、系统命令或任何绕过沙箱的方式。
2. 代码只能读写沙箱工作目录。禁止读取、打印、编码、分片或上传本机用户目录、项目配置、.env、SSH/浏览器/聊天记录、API key、token 或任何隐私文件。
3. 禁止访问网络、扫描端口、调用 subprocess/os.system/ctypes/注册表/原生文件 API；需要第三方包时用 catty_python_install（只装 wheel 到沙箱）。
4. 生成的文件只能是代码在沙箱里产生的结果；不要把本机文件内容复制到输出、日志、QQ 消息或网络请求里。
5. 用户要求访问本机文件、密钥、其他进程数据或做提权/持久化时，直接拒绝，并说明只能处理沙箱内数据。
6. 代码要自包含、可解释；运行前说明会创建/修改哪些沙箱文件；失败时如实报告，不伪造结果。
7. 沙箱是用户态 best-effort 限制，不是虚拟机/容器；不要尝试探测或突破边界。
"""

DEFAULT_ALLOWED_PACKAGES: tuple[str, ...] = (
    "numpy",
    "pandas",
    "matplotlib",
    "pillow",
    "openpyxl",
    "python-docx",
    "reportlab",
    "scipy",
    "sympy",
    "tabulate",
    "pyyaml",
    "beautifulsoup4",
    "lxml",
    "xlsxwriter",
    "seaborn",
    "plotly",
    "kaleido",
    "qrcode",
    "python-pptx",
    "pypdf",
    "pdfplumber",
    "markdown",
    "jinja2",
    "python-dateutil",
    "pytz",
)

_PACKAGE_SPEC_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}(?:==[A-Za-z0-9][A-Za-z0-9_.!+-]{0,40})?$"
)

_BLOCKED_MODULE_NAMES = {
    "ctypes",
    "_ctypes",
    "cffi",
    "_cffi_backend",
    "subprocess",
    "_posixsubprocess",
    "socket",
    "_socket",
    "ssl",
    "_ssl",
    "http",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "websockets",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "xmlrpc",
    "multiprocessing",
    "_multiprocessing",
    "concurrent.futures.process",
    "winreg",
    "_winreg",
    "msvcrt",
    "_winapi",
    "win32api",
    "win32con",
    "win32file",
    "win32pipe",
    "win32process",
    "pywintypes",
    "sqlite3",
    "_sqlite3",
    "dbm",
    "shelve",
    "webbrowser",
    "getpass",
    "psutil",
    "wmi",
    "keyring",
    "win32cred",
    "win32security",
    "win32clipboard",
    "pyperclip",
    "pyautogui",
    "mss",
    "pyscreenshot",
    "sounddevice",
    "pyaudio",
    "soundcard",
    "browser_cookie3",
    "browser_history",
    "PIL.ImageGrab",
    "h5py",
    "tables",
    "pyarrow",
    "fsspec",
    "s3fs",
    "gcsfs",
    "adlfs",
}


_LAUNCHER_SOURCE = r'''"""Generated Catty Python sandbox launcher."""
import builtins
import os
import sys
import traceback

CODE_PATH = os.path.realpath(sys.argv[1])
SANDBOX = os.path.realpath(sys.argv[2])
WORK = os.path.join(SANDBOX, "work")
PACKAGES = os.path.join(SANDBOX, "packages")

BLOCKED_MODULES = {
    "ctypes", "_ctypes", "cffi", "_cffi_backend",
    "subprocess", "_posixsubprocess",
    "socket", "_socket", "ssl", "_ssl",
    "http", "urllib3", "requests", "httpx", "aiohttp", "websockets",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
    "multiprocessing", "_multiprocessing", "concurrent.futures.process",
    "winreg", "_winreg", "msvcrt", "_winapi",
    "win32api", "win32con", "win32file", "win32pipe", "win32process", "pywintypes",
    "sqlite3", "_sqlite3", "dbm", "shelve",
    "webbrowser", "getpass", "psutil", "wmi", "keyring",
    "win32cred", "win32security", "win32clipboard", "pyperclip",
    "pyautogui", "mss", "pyscreenshot", "sounddevice", "pyaudio",
    "soundcard", "browser_cookie3", "browser_history", "PIL.ImageGrab",
    "h5py", "tables", "pyarrow", "fsspec", "s3fs", "gcsfs", "adlfs",
}


def _text(value):
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return os.fspath(value)


def _norm(value):
    return os.path.normcase(os.path.realpath(_text(value)))


def _inside(value, root):
    return value == root or value.startswith(root + os.sep)


SANDBOX_NORM = _norm(SANDBOX)
RUNTIME_ROOTS = set()
_PREFIX_ROOTS = {_norm(sys.prefix), _norm(sys.base_prefix)}
for _entry in list(sys.path):
    if not _entry:
        continue
    _real = _norm(_entry)
    if _inside(_real, SANDBOX_NORM):
        continue
    if any(_inside(_real, root) for root in _PREFIX_ROOTS):
        RUNTIME_ROOTS.add(_real)
for _entry in (sys.prefix, sys.base_prefix, os.path.dirname(os.path.realpath(sys.executable))):
    if _entry:
        RUNTIME_ROOTS.add(_norm(_entry))


def _allowed_read(value):
    norm = _norm(value)
    if _inside(norm, SANDBOX_NORM):
        return True
    for root in RUNTIME_ROOTS:
        if _inside(norm, root):
            return True
    return False


def _allowed_write(value):
    return _inside(_norm(value), SANDBOX_NORM)


def _looks_write(mode, flags):
    text_mode = str(mode or "")
    if any(ch in text_mode for ch in "wax+"):
        return True
    if isinstance(flags, int):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    return False


def _deny(*args, **kwargs):
    raise PermissionError("sandbox: operating-system access denied")


class _BlockedFinder:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if fullname in BLOCKED_MODULES or root in BLOCKED_MODULES:
            raise ImportError("sandbox: module blocked: " + fullname)
        return None


def _audit(event, args):
    if event == "open":
        if not args:
            return
        target = args[0]
        if isinstance(target, int):
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if not _allowed_read(target):
            raise PermissionError("sandbox: read outside sandbox denied")
        if _looks_write(mode, flags) and not _allowed_write(target):
            raise PermissionError("sandbox: write outside sandbox denied")
        return
    if event in {"os.remove", "os.rmdir", "os.mkdir"}:
        if args and not _allowed_write(args[0]):
            raise PermissionError("sandbox: filesystem write outside sandbox denied")
        return
    if event == "os.rename":
        if len(args) >= 2 and (not _allowed_write(args[0]) or not _allowed_write(args[1])):
            raise PermissionError("sandbox: rename outside sandbox denied")
        return
    if event == "os.chdir":
        if args and not _allowed_read(args[0]):
            raise PermissionError("sandbox: chdir outside sandbox denied")
        return
    if event in {"os.listdir", "os.scandir", "os.stat", "os.access"}:
        if args and not _allowed_read(args[0]):
            raise PermissionError("sandbox: path access outside sandbox denied")
        return
    if event in {"os.utime", "os.chmod", "os.truncate"}:
        if args and not _allowed_write(args[0]):
            raise PermissionError("sandbox: metadata write outside sandbox denied")
        return
    if event in {
        "os.system", "os.spawn", "os.exec", "os.fork", "os.startfile",
        "os.link", "subprocess.Popen",
        "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
    }:
        raise PermissionError("sandbox: blocked operation: " + event)
    if event == "import":
        name = str(args[0]).split(".", 1)[0] if args else ""
        if name in BLOCKED_MODULES:
            raise ImportError("sandbox: module blocked: " + name)


with open(CODE_PATH, "r", encoding="utf-8") as _code_file:
    _code = _code_file.read()

for _name in (
    "system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe", "startfile",
    "fork", "forkpty", "kill", "killpg",
):
    if hasattr(os, _name):
        setattr(os, _name, _deny)
try:
    import nt as _nt
    for _name in (
        "system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe", "startfile",
    ):
        if hasattr(_nt, _name):
            setattr(_nt, _name, _deny)
except Exception:
    pass


def _sandbox_login():
    return "sandbox"


os.getlogin = _sandbox_login

sys.meta_path.insert(0, _BlockedFinder())
sys.addaudithook(_audit)
sys.dont_write_bytecode = True
sys.path[:0] = [WORK, PACKAGES]
sys.argv = [CODE_PATH]

_REAL_STAT = os.stat
_REAL_ACCESS = os.access
_REAL_LISTDIR = os.listdir
_REAL_SCANDIR = os.scandir


def _guard_stat(path, *args, **kwargs):
    if not isinstance(path, int) and not _allowed_read(path):
        raise PermissionError("sandbox: stat outside sandbox denied")
    return _REAL_STAT(path, *args, **kwargs)


def _guard_access(path, *args, **kwargs):
    if not isinstance(path, int) and not _allowed_read(path):
        raise PermissionError("sandbox: access outside sandbox denied")
    return _REAL_ACCESS(path, *args, **kwargs)


def _guard_listdir(path=".", *args, **kwargs):
    if not isinstance(path, int) and not _allowed_read(path):
        raise PermissionError("sandbox: listdir outside sandbox denied")
    return _REAL_LISTDIR(path, *args, **kwargs)


def _guard_scandir(path=".", *args, **kwargs):
    if not isinstance(path, int) and not _allowed_read(path):
        raise PermissionError("sandbox: scandir outside sandbox denied")
    return _REAL_SCANDIR(path, *args, **kwargs)


os.stat = _guard_stat
os.access = _guard_access
os.listdir = _guard_listdir
os.scandir = _guard_scandir
try:
    import nt as _nt
    _REAL_NT_STAT = _nt.stat

    def _guard_nt_stat(path, *args, **kwargs):
        if not isinstance(path, int) and not _allowed_read(path):
            raise PermissionError("sandbox: stat outside sandbox denied")
        return _REAL_NT_STAT(path, *args, **kwargs)

    _nt.stat = _guard_nt_stat
except Exception:
    pass

_globals = {"__name__": "__main__", "__file__": CODE_PATH, "__builtins__": builtins}
try:
    exec(compile(_code, CODE_PATH, "exec"), _globals, _globals)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    sys.exit(1)
'''


@dataclass(frozen=True)
class SandboxPaths:
    root: Path
    work: Path
    home: Path
    tmp: Path
    packages: Path
    launcher: Path


def sandbox_paths(config: Any) -> SandboxPaths:
    raw = str(getattr(config, "catty_code_sandbox_dir", "") or DEFAULT_SANDBOX_DIR).strip()
    root = Path(raw or DEFAULT_SANDBOX_DIR).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve()
    return SandboxPaths(
        root=root,
        work=root / "work",
        home=root / "home",
        tmp=root / "tmp",
        packages=root / "packages",
        launcher=root / "_catty_launcher.py",
    )


def ensure_sandbox(config: Any) -> SandboxPaths:
    paths = sandbox_paths(config)
    for directory in (paths.root, paths.work, paths.home, paths.tmp, paths.packages):
        directory.mkdir(parents=True, exist_ok=True)
    try:
        current = paths.launcher.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != _LAUNCHER_SOURCE:
        temporary = paths.launcher.with_name(paths.launcher.name + ".tmp")
        temporary.write_text(_LAUNCHER_SOURCE, encoding="utf-8")
        temporary.replace(paths.launcher)
    return paths


def _sandbox_limits(config: Any) -> dict[str, Any]:
    def _int(name: str, default: int) -> int:
        try:
            return int(getattr(config, name, default) or default)
        except (TypeError, ValueError):
            return default

    def _float(name: str, default: float) -> float:
        try:
            return float(getattr(config, name, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "timeout": max(_float("catty_code_sandbox_timeout_seconds", 30.0), 1.0),
        "max_output_chars": max(_int("catty_code_sandbox_max_output_chars", 8000), 500),
        "max_code_chars": max(_int("catty_code_sandbox_max_code_chars", 60000), 1000),
        "max_files": max(_int("catty_code_sandbox_max_files", 5), 1),
        "max_file_bytes": max(_int("catty_code_sandbox_max_file_bytes", 10_000_000), 1024),
        "max_work_bytes": max(_int("catty_code_sandbox_max_work_bytes", 50_000_000), 1024),
        "ttl_hours": max(_float("catty_code_sandbox_ttl_hours", 24.0), 0.1),
    }


def _sandbox_env(paths: SandboxPaths) -> dict[str, str]:
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    python_dir = os.path.dirname(os.path.realpath(sys.executable))
    path_parts = [
        python_dir,
        os.path.join(system_root, "System32"),
        system_root,
    ]
    home = str(paths.home)
    drive, tail = os.path.splitdrive(home)
    env = {
        "SYSTEMROOT": system_root,
        "WINDIR": system_root,
        "SYSTEMDRIVE": drive or "C:",
        "PATH": os.pathsep.join(part for part in path_parts if part),
        "TEMP": str(paths.tmp),
        "TMP": str(paths.tmp),
        "HOME": home,
        "USERPROFILE": home,
        "HOMEDRIVE": drive,
        "HOMEPATH": tail or home,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
    }
    return {key: value for key, value in env.items() if value}


async def _read_limited(stream: Any, limit_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if total < limit_bytes:
            take = min(len(chunk), limit_bytes - total)
            chunks.append(chunk[:take])
            total += take
            if take < len(chunk):
                truncated = True
        else:
            truncated = True
    return b"".join(chunks), truncated


def _decode_output(data: bytes, max_chars: int) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _is_generated_file(path: Path, work: Path, since: float) -> bool:
    if path.is_symlink():
        return False
    if not path.is_file():
        return False
    if "__pycache__" in path.parts:
        return False
    if path.name.startswith(".catty_run_") and path.suffix == ".py":
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return stat.st_mtime >= since - 2.0


def _collect_generated_files(
    paths: SandboxPaths,
    since: float,
    *,
    max_files: int,
    max_file_bytes: int,
) -> list[dict[str, Any]]:
    found: list[tuple[float, Path, int]] = []
    try:
        candidates = list(paths.work.rglob("*"))
    except OSError:
        return []
    for path in candidates:
        if not _is_generated_file(path, paths.work, since):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_file_bytes:
            continue
        found.append((path.stat().st_mtime, path, size))
    found.sort(key=lambda item: item[0])
    items: list[dict[str, Any]] = []
    for _, path, size in found[:max_files]:
        try:
            relative = path.relative_to(paths.work).as_posix()
        except ValueError:
            relative = path.name
        display = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", relative.replace("/", "_")) or path.name
        items.append({"path": str(path), "name": display[:120], "size": size})
    return items


def _work_size(paths: SandboxPaths) -> int:
    total = 0
    try:
        for path in paths.work.rglob("*"):
            if path.is_symlink():
                continue
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _remove_generated_files(paths: SandboxPaths, since: float) -> None:
    try:
        candidates = list(paths.work.rglob("*"))
    except OSError:
        return
    for path in candidates:
        if not _is_generated_file(path, paths.work, since):
            continue
        try:
            path.unlink()
        except OSError:
            continue


async def run_code(config: Any, code: str) -> dict[str, Any]:
    limits = _sandbox_limits(config)
    content = str(code or "")
    if not content.strip():
        return {"ok": False, "error": "code 不能为空"}
    if len(content) > limits["max_code_chars"]:
        return {
            "ok": False,
            "error": f"代码超过上限 ({len(content)} > {limits['max_code_chars']} 字符)",
        }
    paths = ensure_sandbox(config)
    code_path = paths.work / f".catty_run_{uuid.uuid4().hex}.py"
    started = time.time()
    try:
        code_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"写入沙箱代码文件失败: {exc.__class__.__name__}"}
    limit_bytes = limits["max_output_chars"] * 4 + 4096
    command = [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        str(paths.launcher),
        str(code_path),
        str(paths.root),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(paths.work),
            env=_sandbox_env(paths),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        code_path.unlink(missing_ok=True)
        return {
            "ok": False,
            "error": f"启动沙箱 Python 失败: {exc.__class__.__name__}",
            "sandbox_root": str(paths.root),
        }
    reader = asyncio.create_task(_read_limited(process.stdout, limit_bytes))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=limits["timeout"])
    except asyncio.TimeoutError:
        timed_out = True
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        reader.cancel()
        code_path.unlink(missing_ok=True)
        raise
    try:
        output_bytes, output_truncated = await asyncio.wait_for(reader, timeout=5.0)
    except asyncio.TimeoutError:
        reader.cancel()
        output_bytes, output_truncated = b"", True
    output, char_truncated = _decode_output(output_bytes, limits["max_output_chars"])
    files = _collect_generated_files(
        paths,
        started,
        max_files=limits["max_files"],
        max_file_bytes=limits["max_file_bytes"],
    )
    work_bytes = _work_size(paths)
    work_limit_exceeded = work_bytes > limits["max_work_bytes"]
    if work_limit_exceeded:
        _remove_generated_files(paths, started)
        files = []
    code_path.unlink(missing_ok=True)
    ok = not timed_out and process.returncode == 0 and not work_limit_exceeded
    result: dict[str, Any] = {
        "ok": ok,
        "sandbox_root": str(paths.root),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "output": output,
        "truncated": output_truncated or char_truncated,
        "files": files,
    }
    if timed_out:
        result["error"] = "Python 执行超时"
    elif work_limit_exceeded:
        result["error"] = "沙箱工作目录超过大小上限，已清理本次生成文件"
    elif process.returncode != 0:
        result["error"] = f"Python 非零退出: {process.returncode}"
    return result


def _normalize_packages(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = re.split(r"[\s,]+", raw.strip())
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item).strip() for item in raw]
    else:
        parts = []
    packages: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        if part not in seen:
            seen.add(part)
            packages.append(part)
    return packages


def _package_name(spec: str) -> str:
    return spec.split("==", 1)[0].strip().lower()


def _validate_package_specs(
    raw: Any,
    *,
    allowed: Iterable[str],
    max_packages: int,
) -> tuple[list[str], str | None]:
    packages = _normalize_packages(raw)
    if not packages:
        return [], "packages 不能为空"
    if len(packages) > max_packages:
        return [], f"一次最多安装 {max_packages} 个包"
    allowed_set = {str(item).strip().lower() for item in allowed if str(item).strip()}
    allow_all = "*" in allowed_set
    for spec in packages:
        if not _PACKAGE_SPEC_RE.match(spec):
            return [], f"包名/版本格式不合法: {spec}"
        name = _package_name(spec)
        if not allow_all and name not in allowed_set:
            return [], f"包不在白名单: {name}"
    return packages, None


async def install_packages(config: Any, raw_packages: Any) -> dict[str, Any]:
    limits = _sandbox_limits(config)
    try:
        max_packages = int(getattr(config, "catty_code_sandbox_max_packages", 5) or 5)
    except (TypeError, ValueError):
        max_packages = 5
    max_packages = max(1, min(max_packages, 20))
    allowed = getattr(
        config, "catty_code_sandbox_allowed_packages", DEFAULT_ALLOWED_PACKAGES
    ) or DEFAULT_ALLOWED_PACKAGES
    packages, error = _validate_package_specs(
        raw_packages,
        allowed=allowed,
        max_packages=max_packages,
    )
    if error:
        return {"ok": False, "error": error}
    paths = ensure_sandbox(config)
    try:
        pip_timeout = float(
            getattr(config, "catty_code_sandbox_pip_timeout_seconds", 120.0) or 120.0
        )
    except (TypeError, ValueError):
        pip_timeout = 120.0
    pip_timeout = max(pip_timeout, 10.0)
    index_url = str(
        getattr(config, "catty_code_sandbox_pip_index_url", "") or "https://pypi.org/simple"
    ).strip()
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(paths.packages),
        "--only-binary=:all:",
        "--upgrade",
        "--no-input",
        "--no-compile",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--progress-bar",
        "off",
    ]
    if index_url:
        command.extend(["--index-url", index_url])
    command.extend(packages)
    env = _sandbox_env(paths)
    env.update(
        {
            "PIP_NO_INPUT": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
        }
    )
    limit_bytes = limits["max_output_chars"] * 4 + 4096
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(paths.root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return {"ok": False, "error": f"启动 pip 失败: {exc.__class__.__name__}"}
    reader = asyncio.create_task(_read_limited(process.stdout, limit_bytes))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=pip_timeout)
    except asyncio.TimeoutError:
        timed_out = True
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        reader.cancel()
        raise
    try:
        output_bytes, output_truncated = await asyncio.wait_for(reader, timeout=5.0)
    except asyncio.TimeoutError:
        reader.cancel()
        output_bytes, output_truncated = b"", True
    output, char_truncated = _decode_output(output_bytes, limits["max_output_chars"])
    ok = not timed_out and process.returncode == 0
    result: dict[str, Any] = {
        "ok": ok,
        "packages": packages,
        "target": str(paths.packages),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "output": output,
        "truncated": output_truncated or char_truncated,
    }
    if timed_out:
        result["error"] = "pip 安装超时"
    elif process.returncode != 0:
        result["error"] = f"pip 非零退出: {process.returncode}"
    return result


def cleanup_code_sandbox(config: Any, *, now: float | None = None) -> int:
    paths = sandbox_paths(config)
    if not paths.root.is_dir():
        return 0
    limits = _sandbox_limits(config)
    cutoff = (time.time() if now is None else float(now)) - limits["ttl_hours"] * 3600.0
    removed = 0
    for directory in (paths.work, paths.home, paths.tmp):
        if not directory.is_dir():
            continue
        try:
            candidates = list(directory.rglob("*"))
        except OSError:
            continue
        for path in candidates:
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed
