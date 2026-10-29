"""Hot-configured staging worker. Only the parent supervisor may apply releases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import signal
import threading
import time
from typing import Any, Sequence
import uuid

import httpx

from .agent import RepairCancelled, known_secrets, redact_data, run_repair
from .config import RepairConfig, load_config, state_dir
from .logs import LogMonitor
from .storage import FileLock, atomic_json, read_json, reject_links


MAX_STATUS_BYTES = 128 * 1024
_SUCCESS = {"ready", "candidate", "no_changes", "idle", "disabled", "unconfigured", "paused"}


def _root(path: Path) -> Path:
    absolute = Path(path).absolute()
    reject_links(absolute)
    return absolute.resolve()


def _state_exists(path: Path) -> bool:
    reject_links(path)
    return path.exists()


def _public_record(value: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    # Never copy arbitrary model/provider fields, routes or diagnostic dumps to status.
    allowed = ("status", "summary", "run_id", "release_path", "config_fingerprint", "budget",
               "updated_at", "ready_staged")
    result = {key: value[key] for key in allowed if key in value}
    if isinstance(value.get("requests"), list):
        result["request_count"] = len(value["requests"])
    elif type(value.get("request_count")) is int:
        result["request_count"] = value["request_count"]
    validation = value.get("validation")
    if isinstance(validation, dict):
        result["validation"] = {"ok": validation.get("ok") is True}
        run_id = str(value.get("run_id", ""))
        if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            result["validation"]["report"] = f"data/self_repair/runs/{run_id}/run.json"
    result = redact_data(result, secrets)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_STATUS_BYTES:
        result = {key: result[key] for key in ("status", "run_id", "config_fingerprint", "validation") if key in result}
        result["summary"] = "状态详情超限；完整验证回执保留在运行目录"
    return result


def public_status(root: Path) -> dict[str, Any]:
    """Read status/config only. No monitor, model, budget reservation or release apply."""
    try:
        root = _root(root)
        reject_links(root / "config.json")
        config = load_config(root)
        secrets = known_secrets(root, config) if (root / "config.json").exists() else ()
        state = state_dir(root)
        result: dict[str, Any] = {
            "status": "status", "enabled": config.enabled, "configured": config.configured,
            "auto_apply": config.auto_apply, "config_fingerprint": config.fingerprint(),
            "ready": _state_exists(state / "ready.json"),
            "active_release": _state_exists(state / "active_release.json"),
        }
        path = state / "status.json"
        if _state_exists(path):
            if path.stat().st_size > MAX_STATUS_BYTES:
                result["last"] = {"status": "unavailable", "summary": "状态文件超过读取上限"}
            else:
                result["last"] = _public_record(read_json(path), secrets)
        return redact_data(result, secrets)
    except Exception as exc:
        return {"status": "configuration_error", "summary": "无法安全读取状态：" + type(exc).__name__}


class RepairWorker:
    def __init__(self, root: Path, *, transport: httpx.BaseTransport | None = None) -> None:
        self.root = _root(root)
        self.state = state_dir(self.root)
        self.transport = transport
        self.stop = threading.Event()
        self._monitor: LogMonitor | None = None
        self._monitor_identity: tuple[str, tuple[str, ...]] | None = None
        self._config = RepairConfig()
        self._secrets: tuple[str, ...] = ()
        self._in_repair = False
        self._cancelling = False

    def _record(self, result: dict[str, Any]) -> dict[str, Any]:
        result = _public_record({**result, "updated_at": time.time()}, self._secrets)
        atomic_json(self.state / "status.json", result)
        return result

    def _pause_reason(self) -> str | None:
        if _state_exists(self.state / "active_release.json"):
            return "父守护正在处理 active_release；暂停生成候选"
        if _state_exists(self.state / "ready.json"):
            return "已有 ready 等待父守护处理；暂停生成候选"
        return None

    def _publish(self, result: dict[str, Any], config: RepairConfig) -> dict[str, Any]:
        # Re-read after validation, not just before model generation. Parent checks again.
        reject_links(self.root / "config.json")
        fresh = load_config(self.root)
        if self.stop.is_set():
            return {**result, "status": "cancelled", "summary": "已取消；保留验证候选但不发布 ready"}
        if not fresh.enabled:
            return {**result, "status": "disabled", "summary": "配置已关闭；保留候选但不发布 ready"}
        if not fresh.configured or fresh.fingerprint() != config.fingerprint():
            return {**result, "status": "config_changed", "summary": "配置已改变；保留候选但不发布 ready"}
        self._secrets = known_secrets(self.root, fresh)
        run_id = result.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("invalid staged run identity")
        release_value = result.get("release_path")
        if not isinstance(release_value, str) or not release_value:
            raise ValueError("missing release manifest")
        release = Path(release_value)
        if not release.is_absolute():
            release = self.root / release
        reject_links(release)
        expected = self.state / "runs" / run_id / "release.json"
        reject_links(expected)
        if release.resolve() != expected.resolve() or not release.is_file():
            raise ValueError("release must be this run's fixed manifest under state/runs")
        relative = release.resolve().relative_to(self.root).as_posix()
        staged = {**result, "release_path": relative, "config_fingerprint": fresh.fingerprint()}
        if not fresh.auto_apply:
            return {**staged, "status": "candidate", "summary": "仅保留已验证候选，auto_apply=false；" + str(result.get("summary", ""))}
        reason = self._pause_reason()
        if reason:
            return {**staged, "status": "paused", "summary": reason}
        atomic_json(self.state / "ready.json", {
            "run_id": run_id, "release_path": relative,
            "config_fingerprint": fresh.fingerprint(),
        })
        return {**staged, "ready_staged": True}

    def _tick(self, request: str | None = None) -> dict[str, Any]:
        try:
            reject_links(self.root / "config.json")
            config = load_config(self.root)
            self._config = config
            self._secrets = known_secrets(self.root, config) if (self.root / "config.json").exists() else ()
        except Exception as exc:
            self._monitor = None
            self._monitor_identity = None
            # Do not reuse an old enabled config or echo a malformed config's payload.
            return self._record({"status": "configuration_error", "summary": "独立修复配置不可用：" + type(exc).__name__})
        if self.stop.is_set():
            return self._record({"status": "cancelled", "summary": "worker 正在停止"})
        if not config.enabled:
            self._monitor = None
            self._monitor_identity = None
            return self._record({"status": "disabled", "summary": "独立修复未启用；未读取日志或请求模型"})
        if not config.configured:
            return self._record({"status": "unconfigured", "summary": "独立模型路由不完整；不复用主 AI 配置"})
        reason = self._pause_reason()
        if reason:
            return self._record({"status": "paused", "summary": reason})
        if request is not None:
            if not isinstance(request, str) or not request.strip():
                return self._record({"status": "invalid_request", "summary": "手动请求不能为空"})
            if len(request.encode("utf-8")) > config.max_incident_chars:
                return self._record({"status": "invalid_request", "summary": "手动请求超过事件字节上限；没有截断任务或请求模型"})
            now = time.time()
            incidents = [{"id": "manual-" + uuid.uuid4().hex, "level": "MANUAL", "source": "manual",
                          "text": request, "occurrences": 1, "first_seen": now, "last_seen": now}]
        else:
            identity = (config.fingerprint(), self._secrets)
            if self._monitor is None or self._monitor_identity != identity:
                self._monitor = LogMonitor(self.root, config, self._secrets)
                self._monitor_identity = identity
            incidents = self._monitor.poll()
        if not incidents:
            return self._record({"status": "idle", "summary": "等待达到触发条件的 WARNING/ERROR；没有模型请求"})
        self._record({"status": "running", "summary": "正在生成隔离候选；尚未验证或应用",
                      "config_fingerprint": config.fingerprint()})
        if self.stop.is_set():
            return self._record({"status": "cancelled", "summary": "请求前取消，没有发送模型请求"})
        self._in_repair = True
        try:
            result = run_repair(self.root, config, redact_data(incidents, self._secrets), transport=self.transport)
        except (RepairCancelled, KeyboardInterrupt):
            result = {"status": "cancelled", "summary": "修复已取消；未知费用不退款"}
        finally:
            self._in_repair = False
        if result.get("status") == "ready":
            result = self._publish(result, config)
        return self._record(result)

    def tick(self, request: str | None = None) -> dict[str, Any]:
        """One exclusive tick for embedding/tests; run() holds the same lock continuously."""
        lock = FileLock(self.state / "worker.lock")
        if not lock.acquire():
            return {"status": "busy", "summary": "另一个修复 worker 持有全局锁；未消费请求"}
        try:
            return self._safe_tick(request)
        finally:
            lock.close()

    def _safe_tick(self, request: str | None) -> dict[str, Any]:
        try:
            return self._tick(request)
        except (RepairCancelled, KeyboardInterrupt):
            return self._record({"status": "cancelled", "summary": "worker 已取消；未知费用不退款"})
        except Exception as exc:
            return self._record({"status": "failed", "summary": "worker 停止本次任务：" + type(exc).__name__})

    def _signal(self, _number: int, _frame: Any) -> None:
        self.stop.set()
        if self._in_repair and not self._cancelling:
            self._cancelling = True
            raise RepairCancelled()

    def run(self, *, once: bool = False, request: str | None = None) -> dict[str, Any]:
        lock = FileLock(self.state / "worker.lock")
        if not lock.acquire():
            return {"status": "busy", "summary": "另一个修复 worker 持有全局锁；未消费请求"}
        previous: dict[int, Any] = {}
        result: dict[str, Any] = {"status": "cancelled", "summary": "worker 已停止"}
        try:
            if threading.current_thread() is threading.main_thread():
                for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                    number = getattr(signal, name, None)
                    if number is not None:
                        previous[number] = signal.getsignal(number)
                        signal.signal(number, self._signal)
            while not self.stop.is_set():
                result = self._safe_tick(request)
                if once or request is not None:
                    break
                self.stop.wait(self._config.poll_seconds)
            return result
        finally:
            self._in_repair = False
            for number, handler in previous.items():
                signal.signal(number, handler)
            lock.close()


def main(argv: Sequence[str] | None = None, *, default_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立预算的 Catty 修复候选 worker；不直接应用源码")
    parser.add_argument("--root", type=Path, default=default_root or Path(__file__).resolve().parents[2])
    parser.add_argument("--once", action="store_true", help="仅处理一次日志触发")
    parser.add_argument("--status", action="store_true", help="只读脱敏状态，不请求模型")
    parser.add_argument("--request", metavar="TEXT", help="执行一次手动升级；仍遵守预算、验证与发布门控")
    args = parser.parse_args(argv)
    if args.status and (args.once or args.request is not None):
        parser.error("--status 不能与 --once/--request 同用")
    if args.status:
        result = public_status(args.root)
    else:
        try:
            result = RepairWorker(args.root).run(once=args.once, request=args.request)
        except (RepairCancelled, KeyboardInterrupt):
            result = {"status": "cancelled", "summary": "worker 已取消"}
        except Exception as exc:
            result = {"status": "failed", "summary": "worker 无法启动：" + type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    status = result.get("status")
    if status in _SUCCESS or status == "status":
        return 0
    return {"busy": 2, "budget_exhausted": 3, "cancelled": 130}.get(str(status), 1)
