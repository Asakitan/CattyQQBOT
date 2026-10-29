"""Trusted, bounded offline unittest runner; not an operating-system sandbox."""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import Workspace

_OUTPUT_LIMIT = 64 * 1024
_OUTPUT_TOTAL_LIMIT = 2 * 1024 * 1024
_RESULT_LIMIT = 8 * 1024 * 1024
_OUTCOMES = {"success", "failure", "error", "skip", "expected_failure", "unexpected_success"}
_RED = {"failure", "error", "unexpected_success"}

_RUNNER = r'''
import hashlib, io, json, os, pathlib, re, sys, traceback, unittest
if sys.stdin.readline() != "go\n":
    raise SystemExit(70)
root = pathlib.Path(sys.argv[1]).resolve()
module = sys.argv[2]
result_file = pathlib.Path(sys.argv[3])
temporary = pathlib.Path(sys.argv[4]).resolve()
site_dirs = json.loads(sys.argv[5])
sources = json.loads(sys.argv[6])
sys.dont_write_bytecode = True
sys.path[:0] = [str(root / "tests"), str(root / "src"), str(root)] + site_dirs
_open, _dumps = io.open, json.dumps
runtime = [pathlib.Path(sys.base_prefix).resolve(), pathlib.Path(sys.prefix).resolve()]
runtime.extend(pathlib.Path(value).resolve() for value in site_dirs)

def within(path, roots):
    return any(path == parent or parent in path.parents for parent in roots)

def safe_path(value):
    if isinstance(value, int):
        return None
    return pathlib.Path(os.fsdecode(value) if value is not None else os.getcwd()).resolve()

def audit(event, args):
    if event == "open":
        path = safe_path(args[0])
        if path is None:
            return
        mode, flags = args[1], args[2]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        allowed = [temporary] if writing else [root, temporary] + runtime
        if not within(path, allowed):
            raise PermissionError("validation file access outside frozen/runtime/temp roots")
    elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
        path = safe_path(args[0])
        if path is not None and not within(path, [temporary]):
            raise PermissionError("validation writes are limited to its own temp directory")
    elif event in {"os.rename", "os.link", "os.symlink"}:
        for value in args[:2]:
            path = safe_path(value)
            if path is not None and not within(path, [temporary]):
                raise PermissionError("validation cannot alter frozen files")
    elif event in {"os.listdir", "os.scandir"}:
        path = safe_path(args[0])
        if path is not None and not within(path, [root, temporary] + runtime):
            raise PermissionError("validation directory access outside permitted roots")
    elif event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "pty.spawn"}:
        raise PermissionError("validation tests may not launch child commands")
    elif event in {"socket.connect", "socket.bind"}:
        address = args[1]
        if not isinstance(address, tuple) or str(address[0]) not in {"127.0.0.1", "::1", "localhost"}:
            raise PermissionError("validation external networking is disabled")
    elif event == "socket.getaddrinfo" and str(args[0]) not in {"127.0.0.1", "::1", "localhost"}:
        raise PermissionError("validation external DNS is disabled")

sys.addaudithook(audit)

def normalize(text):
    for path, label in ((root, "<SNAPSHOT>"), (temporary, "<TEMP>")):
        # OSError renders filenames with repr(), doubling Windows separators.
        # Replace escaped prefixes before raw ones; keep relative filenames.
        paths = {str(path), path.as_posix()}
        forms = paths | {repr(value)[1:-1] for value in paths}
        forms.update(json.dumps(value, ensure_ascii=ascii_only)[1:-1]
                     for value in paths for ascii_only in (False, True))
        for value in sorted(forms, key=len, reverse=True):
            text = text.replace(value, label)
    # Only object-repr addresses are volatile; HRESULT/status codes are evidence.
    return re.sub(r"(?<= at )0x[0-9a-fA-F]+(?=>)", "0xADDR", text)

def error_detail(err):
    kind = err[0].__module__ + "." + err[0].__qualname__
    message = normalize(str(err[1]))
    identity = hashlib.sha256((kind + ":" + message).encode("utf-8", "replace")).hexdigest()
    return {"error_key": identity, "detail": normalize("".join(traceback.format_exception(*err, limit=8)))[-8000:]}

class Recorder(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.cases = []
        self.active = {}
        self.fixture_errors = []
    def startTest(self, test):
        super().startTest(test)
        row = {"id": test.id(), "outcome": "incomplete", "detail": "", "error_key": "", "subtests": []}
        self.cases.append(row)
        self.active[id(test)] = row
    def record(self, test, outcome, detail="", err=None):
        row = self.active.get(id(test))
        if row is None:
            self.fixture_errors.append({"id": test.id(), "outcome": outcome, "detail": detail, **(error_detail(err) if err else {})})
            return
        row.update(outcome=outcome, detail=detail)
        if err:
            row.update(error_detail(err))
    def addSuccess(self, test):
        self.record(test, "success")
    def addFailure(self, test, err):
        self.record(test, "failure", err=err)
    def addError(self, test, err):
        self.record(test, "error", err=err)
    def addSkip(self, test, reason):
        parent = self.active.get(id(getattr(test, "test_case", None)))
        if parent is not None:
            parent["subtests"].append({"id": test.id(), "outcome": "skip", "detail": normalize(str(reason)), "error_key": ""})
        else:
            self.record(test, "skip", normalize(str(reason)))
    def addExpectedFailure(self, test, err):
        self.record(test, "expected_failure", err=err)
    def addUnexpectedSuccess(self, test):
        self.record(test, "unexpected_success")
    def addSubTest(self, test, subtest, err):
        outcome = "success" if err is None else ("failure" if issubclass(err[0], test.failureException) else "error")
        row = {"id": subtest.id(), "outcome": outcome, "detail": "", "error_key": ""}
        if err:
            row.update(error_detail(err))
        parent = self.active[id(test)]
        parent["subtests"].append(row)
        if err:
            parent["outcome"] = outcome
            parent.update(error_detail(err))
    def stopTest(self, test):
        super().stopTest(test)
        self.active.pop(id(test), None)

def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item

report = {"module": module, "status": "runner_error", "discovered": [], "cases": [], "tests_run": 0, "fixture_errors": [], "loader_errors": []}
try:
    for relative in sources:
        source = root / relative
        compile(source.read_bytes(), relative, "exec", dont_inherit=True)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(module)
    planned = [case.id() for case in flatten(suite)]
    report["discovered"] = planned
    report["loader_errors"] = [normalize(str(value))[-8000:] for value in loader.errors]
    if loader.errors or not planned or len(planned) != len(set(planned)) or len(planned) > 10000:
        report["status"] = "discovery_error"
    else:
        result = Recorder()
        suite.run(result)
        report.update(status="completed", tests_run=result.testsRun, cases=result.cases, fixture_errors=result.fixture_errors)
except BaseException:
    report["diagnostic"] = normalize(traceback.format_exc())[-8000:]
with _open(result_file, "w", encoding="utf-8") as stream:
    stream.write(_dumps(report, ensure_ascii=False, allow_nan=False))
    stream.flush()
    os.fsync(stream.fileno())
raise SystemExit(0 if report["status"] == "completed" else 71)
'''


class _WindowsJob:
    """Kill-on-close process tree, attached before releasing the stdin gate."""
    def __init__(self, process: subprocess.Popen) -> None:
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000
        try:
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


class _Output:
    def __init__(self, pipe: Any) -> None:
        self.pipe, self.tail, self.total = pipe, bytearray(), 0
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        try:
            while True:
                chunk = self.pipe.read(8192)
                if not chunk:
                    return
                self.total += len(chunk)
                self.tail.extend(chunk)
                if len(self.tail) > _OUTPUT_LIMIT:
                    del self.tail[:-_OUTPUT_LIMIT]
        finally:
            self.pipe.close()

    def text(self) -> str:
        self.thread.join(3)
        return bytes(self.tail).decode("utf-8", "replace")


def _environment(temporary: Path) -> dict[str, str]:
    result = {
        "HOME": str(temporary), "USERPROFILE": str(temporary), "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
        "APPDATA": str(temporary / "appdata"), "LOCALAPPDATA": str(temporary / "localappdata"),
        "XDG_CACHE_HOME": str(temporary / "cache"), "XDG_CONFIG_HOME": str(temporary / "config"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "CATTY_SELF_REPAIR_VALIDATION": "1",
    }
    paths = [str(Path(sys.executable).parent)]
    if os.name == "nt":
        system = os.environ.get("SystemRoot", os.environ.get("WINDIR", r"C:\Windows"))
        result.update(SystemRoot=system, WINDIR=system)
        paths.extend([str(Path(system) / "System32"), system])
    result["PATH"] = os.pathsep.join(paths)
    return result


def _site_directories() -> list[str]:
    return sorted({str(Path(sysconfig.get_path(key)).resolve()) for key in ("purelib", "platlib") if sysconfig.get_path(key)})


def _kill_tree(process: subprocess.Popen, job: _WindowsJob | None) -> None:
    try:
        if job is not None:
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def _run_module(snapshot: Path, phase: str, module: str, sources: list[str], timeout: float, scratch: Path) -> dict:
    from .workspace import _read, _sha
    started = time.monotonic()
    result: dict[str, Any] = {"phase": phase, "module": module, "status": "launch_error"}
    with tempfile.TemporaryDirectory(prefix=f"{phase}-{module}-", dir=scratch) as name:
        temporary = Path(name).resolve()
        report_file = temporary / "result.json"
        command = [sys.executable, "-I", "-S", "-B", "-X", "utf8", "-c", _RUNNER,
                   str(snapshot), module, str(report_file), str(temporary), json.dumps(_site_directories()), json.dumps(sources)]
        process: subprocess.Popen | None = None
        job = None
        stdout = stderr = None
        try:
            kwargs: dict[str, Any] = {"cwd": snapshot, "env": _environment(temporary), "stdin": subprocess.PIPE,
                                      "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "close_fds": True}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **kwargs)
            stdout, stderr = _Output(process.stdout), _Output(process.stderr)
            if os.name == "nt":
                job = _WindowsJob(process)
            process.stdin.write(b"go\n")
            process.stdin.flush()
            process.stdin.close()
            while True:
                if stdout.total + stderr.total > _OUTPUT_TOTAL_LIMIT:
                    result["status"] = "output_limit"
                    break
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    result["status"] = "timeout"
                    break
                try:
                    process.wait(timeout=min(0.1, remaining))
                    result["status"] = "finished"
                    break
                except subprocess.TimeoutExpired:
                    continue
        except (OSError, ValueError) as exc:
            result["diagnostic"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
        finally:
            if process is not None:
                _kill_tree(process, job)
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                result["exit_code"] = process.returncode
            if stdout is not None and stderr is not None:
                result["stdout"], result["stderr"] = stdout.text(), stderr.text()
                result["output_bytes"] = stdout.total + stderr.total
        result["elapsed_seconds"] = time.monotonic() - started
        if result["status"] == "finished":
            if result.get("output_bytes", 0) > _OUTPUT_TOTAL_LIMIT:
                result["status"] = "output_limit"
            elif not report_file.is_file():
                result["status"] = "missing_result"
            else:
                try:
                    data = _read(report_file)
                    if len(data) > _RESULT_LIMIT:
                        raise ValueError("result exceeded byte limit")
                    payload = json.loads(data)
                    if not isinstance(payload, dict) or payload.get("module") != module:
                        raise ValueError("invalid result identity")
                    result.update({key: value for key, value in payload.items() if key not in {"phase", "module", "stdout", "stderr", "exit_code", "elapsed_seconds"}})
                    result["result_sha256"] = _sha(data)
                    if result.get("exit_code") != 0:
                        result["status"] = "runner_error"
                except (OSError, ValueError) as exc:
                    result.update(status="invalid_result", diagnostic=str(exc)[:1000])
        return result


def _case_map(record: dict) -> tuple[dict[str, dict], list[str]]:
    issues: list[str] = []
    planned, cases = record.get("discovered"), record.get("cases")
    if record.get("status") != "completed" or record.get("exit_code") != 0:
        return {}, ["module did not complete successfully in the trusted runner"]
    if (not isinstance(planned, list) or not planned or len(planned) > 10000 or not all(isinstance(value, str) and value for value in planned)
            or len(set(planned)) != len(planned) or not isinstance(cases, list)):
        return {}, ["missing, zero, duplicate or malformed discovered test IDs"]
    if record.get("loader_errors") or record.get("fixture_errors"):
        issues.append("test loading or class/module fixtures did not complete")
    if type(record.get("tests_run")) is not int or record["tests_run"] != len(planned):
        issues.append("executed test count differs from discovered IDs")
    mapped: dict[str, dict] = {}
    for row in cases:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or row.get("outcome") not in _OUTCOMES or row["id"] in mapped
                or not isinstance(row.get("detail", ""), str) or not isinstance(row.get("error_key", ""), str) or not isinstance(row.get("subtests", []), list)):
            issues.append("invalid or duplicate case outcome")
            continue
        mapped[row["id"]] = row
        seen: set[str] = set()
        for sub in row.get("subtests", []):
            if (not isinstance(sub, dict) or not isinstance(sub.get("id"), str) or sub.get("outcome") not in _OUTCOMES or sub["id"] in seen
                    or not isinstance(sub.get("detail", ""), str) or not isinstance(sub.get("error_key", ""), str)):
                issues.append("invalid or duplicate subtest outcome")
                continue
            seen.add(sub["id"])
    if set(mapped) != set(planned):
        issues.append("discovered and executed case IDs differ")
    if not any(row["outcome"] != "skip" for row in mapped.values()):
        issues.append("module executed no non-skipped tests")
    return mapped, issues


def _acceptable(old: dict, new: dict) -> bool:
    before, after = old["outcome"], new["outcome"]
    if after == "success":
        return True
    if before != after:
        return False
    if after == "skip":
        return old.get("detail", "") == new.get("detail", "")
    if after in {"failure", "error", "expected_failure"}:
        return bool(old.get("error_key")) and old.get("error_key") == new.get("error_key")
    return after == "unexpected_success"


def compare_results(baseline: list[dict], candidate: list[dict]) -> dict:
    issues: list[dict] = []
    changes: list[dict] = []
    baseline_red: list[dict] = []
    candidate_red: list[dict] = []
    if not baseline or [row.get("module") for row in baseline] != [row.get("module") for row in candidate]:
        return {"ok": False, "issues": [{"reason": "missing or changed test modules"}], "changes": [], "baseline_red": [], "candidate_red": []}
    for old_module, new_module in zip(baseline, candidate):
        module = old_module.get("module")
        old, old_issues = _case_map(old_module)
        new, new_issues = _case_map(new_module)
        issues.extend({"module": module, "phase": phase, "reason": reason} for phase, values in (("baseline", old_issues), ("candidate", new_issues)) for reason in values)
        if old_issues or new_issues:
            continue
        for name in sorted(old.keys() | new.keys()):
            if name not in old or name not in new:
                issues.append({"module": module, "id": name, "reason": "added or dropped test ID"})
                continue
            first, second = old[name], new[name]
            for row, target in ((first, baseline_red), (second, candidate_red)):
                if row["outcome"] in _RED:
                    target.append({"module": module, **row})
            before_sub = {row["id"]: row for row in first.get("subtests", [])}
            after_sub = {row["id"]: row for row in second.get("subtests", [])}
            if before_sub.keys() != after_sub.keys():
                issues.append({"module": module, "id": name, "reason": "added or dropped subtest ID"})
            pairs = [(name, first, second)] + [(key, before_sub[key], after_sub[key]) for key in sorted(before_sub.keys() & after_sub.keys())]
            for identifier, previous, following in pairs:
                allowed = _acceptable(previous, following)
                changed = (previous["outcome"] != following["outcome"] or previous.get("error_key") != following.get("error_key")
                           or (previous["outcome"] == "skip" and previous.get("detail") != following.get("detail")))
                if changed or not allowed:
                    changes.append({"module": module, "id": identifier, "before": previous["outcome"], "after": following["outcome"], "allowed": allowed})
                if not allowed:
                    issues.append({"module": module, "id": identifier, "reason": "new/changed failure, error or skip"})
    return {"ok": not issues, "issues": issues, "changes": changes, "baseline_red": baseline_red, "candidate_red": candidate_red}


def validate_workspace(workspace: "Workspace", *, deadline: float | None = None) -> dict:
    from .workspace import WorkspaceError, _digest, _mkdir, _sha, _source_path
    started = time.monotonic()
    expires = started + workspace.config.validation_timeout_seconds
    if deadline is not None:
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise WorkspaceError("validation deadline must be finite monotonic time")
        expires = min(expires, deadline)
    report: dict[str, Any] = {"schema": 1, "ok": False, "config_fingerprint": workspace.config.fingerprint(),
                              "runner_sha256": _sha(_RUNNER.encode("utf-8")), "baseline": [], "candidate": [],
                              "integrity": [], "issues": [], "before": {}, "after": {}, "is_os_sandbox": False}
    try:
        before, after = workspace._checked_images()
        report.update(before=before, after=after)
        scratch = workspace.run_dir / "validation-tmp"
        _mkdir(scratch)
        for phase, snapshot, images in (("baseline", workspace.baseline, before), ("candidate", workspace.candidate, after)):
            sources = sorted(name for name in images if _source_path(name))
            for module in workspace.config.test_modules:
                current = workspace._checked_images()
                if current != (before, after):
                    raise WorkspaceError("source changed between validation subprocesses")
                report["integrity"].append({"stage": f"before:{phase}:{module}", "baseline_sha256": _digest(current[0]), "candidate_sha256": _digest(current[1])})
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceError("validation total deadline exceeded")
                record = _run_module(snapshot, phase, module, sources, min(workspace.config.test_timeout_seconds, remaining), scratch)
                report[phase].append(record)
                current = workspace._checked_images()
                if current != (before, after):
                    raise WorkspaceError("test execution changed frozen source/test/support files")
                report["integrity"].append({"stage": f"after:{phase}:{module}", "baseline_sha256": _digest(current[0]), "candidate_sha256": _digest(current[1])})
        comparison = compare_results(report["baseline"], report["candidate"])
        report["comparison"] = comparison
        report["issues"].extend(comparison["issues"])
        if time.monotonic() > expires:
            report["issues"].append({"reason": "validation total deadline exceeded"})
        report["ok"] = comparison["ok"] and not report["issues"]
    except (OSError, ValueError) as exc:
        report["issues"].append({"reason": f"{type(exc).__name__}: {str(exc)[:2000]}"})
    report["elapsed_seconds"] = time.monotonic() - started
    return report