"""Catty Python sandbox (2026-10-26).

The bot can write/run Python and install wheels without touching the host
environment:

- each run has private work / HOME / TEMP; installed packages remain shared;
- explicit scopes resume successful work and exports are run-owned copies;
- the child interpreter runs with ``-I -B -X utf8`` and a scrubbed environment;
- a generated launcher installs audit hooks that deny reads outside the run
    (plus packages and the Python runtime), writes outside the run, network sockets,
  subprocess / ctypes / registry / native file APIs;
- pip installs only binary wheels into the sandbox ``packages`` dir.

This is a best-effort user-mode sandbox, not an OS-level boundary.  The real
security contract is the system prompt (CODE_SANDBOX_SYSTEM_PROMPT) plus these
runtime restrictions.  Do not describe it as a hypervisor/container.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

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
PACKAGES = os.path.realpath(sys.argv[3])
STORAGE = os.path.dirname(PACKAGES)

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
PACKAGES_NORM = _norm(PACKAGES)
STORAGE_NORM = _norm(STORAGE)
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
    if _inside(norm, SANDBOX_NORM) or _inside(norm, PACKAGES_NORM):
        return True
    # A sandbox placed under sys.prefix must not expose sibling runs/scopes.
    if _inside(norm, STORAGE_NORM):
        return False
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


_ENSURE_LOCK = threading.Lock()


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
    # Windows denies replacing a file while another thread has it open for
    # reading. Serialize the whole compare/publish transaction, not just rename.
    with _ENSURE_LOCK:
        try:
            current = paths.launcher.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        if current != _LAUNCHER_SOURCE:
            temporary = paths.launcher.with_name(paths.launcher.name + f".{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(_LAUNCHER_SOURCE, encoding="utf-8")
                temporary.replace(paths.launcher)
            finally:
                temporary.unlink(missing_ok=True)
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
    # Retain a bounded prefix and rolling suffix, but always drain the pipe.
    limit_bytes = max(0, limit_bytes)
    head_limit = (limit_bytes + 1) // 2
    tail_limit = limit_bytes - head_limit
    head = bytearray()
    tail = bytearray()
    total = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        total += len(chunk)
        take = min(len(chunk), head_limit - len(head))
        head.extend(chunk[:take])
        if tail_limit:
            tail.extend(chunk[take:])
            if len(tail) > tail_limit:
                del tail[:-tail_limit]
    truncated = total > limit_bytes
    marker = b"\n... [stdout truncated] ...\n" if truncated else b""
    return bytes(head) + marker + bytes(tail), truncated


def _decode_output(data: bytes, max_chars: int) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        marker = "\n... [stdout truncated] ...\n"
        if max_chars <= len(marker):
            return text[-max_chars:] if max_chars > 0 else "", True
        head = (max_chars - len(marker)) // 2
        tail = max_chars - len(marker) - head
        return text[:head] + marker + text[-tail:], True
    return text, False


_LIFECYCLE_LOCK = threading.RLock()
_ACTIVE_PATHS: set[Path] = set()
_SCOPE_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def _scope_lock(root: Path, identity: str) -> asyncio.Lock:
    key = (str(root), identity, id(asyncio.get_running_loop()))
    with _LIFECYCLE_LOCK:
        lock = _SCOPE_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SCOPE_LOCKS[key] = lock
        return lock


async def _disk_io(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Do not release ownership while a cancelled filesystem worker still runs."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


def _regular_files(directory: Path) -> Iterable[tuple[Path, os.stat_result]]:
    """Walk without following symlinks, junctions or other reparse points."""
    try:
        root_info = directory.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(root_info.st_mode) or getattr(root_info, "st_file_attributes", 0) & 0x400:
        return
    pending = [directory]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        continue
                    path = Path(entry.path)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        yield path, info
        except FileNotFoundError:
            continue


def _fingerprint(path: Path, info: os.stat_result) -> tuple[int, int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(128 * 1024), b""):
            digest.update(block)
    return info.st_size, info.st_mtime_ns, digest.hexdigest()


def _work_inventory(work: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(work).as_posix(): _fingerprint(path, info)
        for path, info in _regular_files(work)
    }


def _copy_work(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path, _ in _regular_files(source):
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _collect_generated_files(
    paths: SandboxPaths,
    since: float,
    *,
    max_files: int,
    max_file_bytes: int,
    baseline: Mapping[str, tuple[int, int, str]] | None = None,
    export_dir: Path | None = None,
    run_id: str = "",
) -> list[dict[str, Any]]:
    # ``since`` remains positional for compatibility; a private run plus its
    # input inventory, never wall-clock rounding, determines ownership.
    del since
    before = baseline or {}
    items: list[dict[str, Any]] = []
    for path, info in _regular_files(paths.work):
        if len(items) >= max_files:
            break
        if path.name.startswith(".catty_run_") and path.suffix == ".py":
            continue
        if info.st_size > max_file_bytes:
            continue
        relative = path.relative_to(paths.work).as_posix()
        if "__pycache__" in path.relative_to(paths.work).parts:
            continue
        if relative in before and _fingerprint(path, info) == before[relative]:
            continue
        target = path
        if export_dir is not None:
            target = export_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # A copy, not a hardlink: subsequent edits cannot mutate delivery.
            shutil.copyfile(path, target)
        display = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", relative.replace("/", "_")) or path.name
        items.append({
            "path": str(target), "name": display[:120], "size": info.st_size,
            "run_id": run_id,
        })
    return items


def _work_size(paths: SandboxPaths) -> int:
    return sum(info.st_size for _, info in _regular_files(paths.work))


def _run_paths(base: SandboxPaths, run_id: str) -> SandboxPaths:
    root = base.root / "runs" / run_id
    return SandboxPaths(
        root=root, work=root / "work", home=root / "home", tmp=root / "tmp",
        packages=base.packages, launcher=root / "_catty_launcher.py",
    )


def _prepare_run(
    config: Any,
    paths: SandboxPaths,
    scope_dir: Path | None,
    content: str,
    input_files: Mapping[str, str | bytes] | None,
) -> dict[str, tuple[int, int, str]]:
    ensure_sandbox(config)
    for directory in (paths.root, paths.work, paths.home, paths.tmp):
        directory.mkdir(parents=True, exist_ok=True)
    paths.launcher.write_text(_LAUNCHER_SOURCE, encoding="utf-8")
    if scope_dir is not None:
        _copy_work(scope_dir / "work", paths.work)
    for name, value in (input_files or {}).items():
        relative = Path(str(name).replace("\\", "/"))
        if (not relative.parts or relative.is_absolute()
                or ".." in relative.parts or any(":" in part for part in relative.parts)):
            raise ValueError("input_files 必须使用工作目录内的相对文件名")
        if not isinstance(value, (str, bytes)):
            raise ValueError("input_files 内容必须是文本或 bytes")
        target = paths.work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
    if _work_size(paths) > _sandbox_limits(config)["max_work_bytes"]:
        raise ValueError("沙箱输入超过工作目录大小上限")
    (paths.root / ".catty_run.py").write_text(content, encoding="utf-8")
    return _work_inventory(paths.work)


def _save_scope_work(paths: SandboxPaths, scope_dir: Path) -> None:
    scope_dir.mkdir(parents=True, exist_ok=True)
    staged = scope_dir / (".next-" + paths.root.name)
    previous = scope_dir / (".old-" + paths.root.name)
    work = scope_dir / "work"
    _copy_work(paths.work, staged)
    if work.exists():
        work.replace(previous)
    try:
        staged.replace(work)
    except OSError:
        if previous.exists():
            previous.replace(work)
        raise
    shutil.rmtree(previous, ignore_errors=True)
    os.utime(scope_dir, None)


def _finish_run(
    paths: SandboxPaths,
    scope_dir: Path | None,
    export_dir: Path,
    baseline: Mapping[str, tuple[int, int, str]],
    limits: Mapping[str, Any],
    success: bool,
) -> tuple[list[dict[str, Any]], bool]:
    (paths.root / ".catty_run.py").unlink(missing_ok=True)
    paths.launcher.unlink(missing_ok=True)
    work_bytes = sum(info.st_size for _, info in _regular_files(paths.root))
    if work_bytes > limits["max_work_bytes"]:
        # Never walk/delete sibling runs, exports, shared packages or scope data.
        shutil.rmtree(paths.root)
        return [], True
    files = _collect_generated_files(
        paths, 0.0, max_files=limits["max_files"],
        max_file_bytes=limits["max_file_bytes"], baseline=baseline,
        export_dir=export_dir, run_id=paths.root.name,
    )
    if success and scope_dir is not None:
        _save_scope_work(paths, scope_dir)
    os.utime(paths.root, None)
    if export_dir.exists():
        os.utime(export_dir, None)
    return files, False


async def run_code(
    config: Any,
    code: str,
    *,
    scope: str | None = None,
    input_files: Mapping[str, str | bytes] | None = None,
) -> dict[str, Any]:
    """Execute privately; an explicit scope resumes its last successful files.

    Unchanged inputs are not delivered. Failed runs do not advance scope state.
    ``input_files`` contains relative names and contents, never host file paths.
    """
    limits = _sandbox_limits(config)
    content = str(code or "")
    if not content.strip():
        return {"ok": False, "error": "code 不能为空"}
    if len(content) > limits["max_code_chars"]:
        return {
            "ok": False,
            "error": f"代码超过上限 ({len(content)} > {limits['max_code_chars']} 字符)",
        }
    base = sandbox_paths(config)
    run_id = uuid.uuid4().hex
    scope_id = hashlib.sha256(str(scope).encode("utf-8")).hexdigest() if scope else ""
    scope_dir = base.root / "scopes" / scope_id if scope_id else None
    paths = _run_paths(base, run_id)
    export_dir = base.root / "exports" / run_id
    async with _scope_lock(base.root, scope_id or run_id):
        active = {paths.root, export_dir}
        if scope_dir is not None:
            active.add(scope_dir)
        with _LIFECYCLE_LOCK:
            _ACTIVE_PATHS.update(active)
        try:
            return await _run_code_in_directory(
                config, content, input_files, base, paths, scope_dir, export_dir, limits,
            )
        finally:
            with _LIFECYCLE_LOCK:
                _ACTIVE_PATHS.difference_update(active)


async def _run_code_in_directory(
    config: Any,
    content: str,
    input_files: Mapping[str, str | bytes] | None,
    base: SandboxPaths,
    paths: SandboxPaths,
    scope_dir: Path | None,
    export_dir: Path,
    limits: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = paths.root.name
    identity = {
        "sandbox_root": str(base.root), "work_dir": str(paths.work),
        "run_id": run_id, "files": [], "partial_files": [],
    }
    try:
        baseline = await _disk_io(_prepare_run, config, paths, scope_dir, content, input_files)
    except (OSError, ValueError) as exc:
        await _disk_io(shutil.rmtree, paths.root, ignore_errors=True)
        detail = str(exc) if isinstance(exc, ValueError) else exc.__class__.__name__
        return {**identity, "ok": False, "error": f"准备沙箱工作目录失败: {detail}"}
    except asyncio.CancelledError:
        await _disk_io(shutil.rmtree, paths.root, ignore_errors=True)
        raise
    code_path = paths.root / ".catty_run.py"
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
        str(paths.packages),
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
        await _disk_io(code_path.unlink, missing_ok=True)
        return {
            **identity,
            "ok": False,
            "error": f"启动沙箱 Python 失败: {exc.__class__.__name__}",
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
        await asyncio.gather(reader, return_exceptions=True)
        await _disk_io(code_path.unlink, missing_ok=True)
        raise
    try:
        output_bytes, output_truncated = await asyncio.wait_for(reader, timeout=5.0)
    except asyncio.TimeoutError:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        output_bytes, output_truncated = b"", True
    output, char_truncated = _decode_output(output_bytes, limits["max_output_chars"])
    success = not timed_out and process.returncode == 0
    try:
        artifacts, work_limit_exceeded = await _disk_io(
            _finish_run, paths, scope_dir, export_dir, baseline, limits, success,
        )
    except OSError as exc:
        return {
            **identity, "ok": False, "exit_code": process.returncode,
            "timed_out": timed_out, "output": output,
            "truncated": output_truncated or char_truncated,
            "error": f"保存沙箱产物失败: {exc.__class__.__name__}",
        }
    ok = success and not work_limit_exceeded
    result: dict[str, Any] = {
        **identity,
        "ok": ok,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "output": output,
        "truncated": output_truncated or char_truncated,
        "files": artifacts if ok else [],
        "partial_files": artifacts if not ok else [],
    }
    if timed_out:
        result["error"] = "Python 执行超时"
    elif work_limit_exceeded:
        result["error"] = "沙箱工作目录超过大小上限，已清理本次运行目录"
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
    paths = await _disk_io(ensure_sandbox, config)
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


def _expire_directory(directory: Path, cutoff: float) -> int:
    # Rename under the ownership lock, then do the potentially large scan and
    # deletion outside it. A concurrent new scope starts at the original path.
    with _LIFECYCLE_LOCK:
        if directory in _ACTIVE_PATHS:
            return 0
        try:
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or info.st_mtime > cutoff):
                return 0
            expired = directory.with_name(".expired-" + uuid.uuid4().hex)
            directory.replace(expired)
        except OSError:
            return 0
    try:
        count = sum(1 for _ in _regular_files(expired))
        shutil.rmtree(expired)
        return count
    except OSError:
        # The tombstone remains eligible for the next cleanup pass.
        return 0


def cleanup_code_sandbox(config: Any, *, now: float | None = None) -> int:
    """Synchronous maintenance API; async callers must use asyncio.to_thread."""
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
            for path, info in _regular_files(directory):
                try:
                    if info.st_mtime <= cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            continue
    for name in ("runs", "exports", "scopes"):
        try:
            with os.scandir(paths.root / name) as entries:
                for entry in entries:
                    removed += _expire_directory(Path(entry.path), cutoff)
        except OSError:
            continue
    return removed
