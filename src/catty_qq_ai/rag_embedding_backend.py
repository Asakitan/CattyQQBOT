from __future__ import annotations

import builtins
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


logger = logging.getLogger(__name__)
_PROTOCOL_PREFIX = b"CATTY_RAG_V1="
_MAX_BATCH_SIZE = 128
_MAX_INPUT_LINE_BYTES = 1024 * 1024
_MAX_OUTPUT_LINE_BYTES = 8 * 1024 * 1024
_VECTOR_SIZE = 512
_POLL_INTERVAL = 0.1
_STOP_WAIT = 0.25
_BUSY_WAIT = 0.25
_ENV_KEYS = (
    "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "HOME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "USER", "USERNAME", "LOGNAME", "LNAME",
    "LOCALAPPDATA", "APPDATA", "TEMP", "TMP",
    "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE",
    "XDG_CACHE_HOME",
)


class LocalBGEError(RuntimeError):
    pass


class LocalBGENotReadyError(LocalBGEError):
    pass


class LocalBGEBusyError(LocalBGEError):
    pass


class LocalBGEClosedError(LocalBGEError):
    pass


class LocalBGETimeoutError(LocalBGEError, TimeoutError):
    pass


class LocalBGEWorkerError(LocalBGEError):
    pass


class LocalBGEProtocolError(LocalBGEError):
    pass


def _error_type(value):
    if value == "LocalEntryNotFoundError":
        return value
    if isinstance(value, str) and value.isascii() and value.isidentifier() and len(value) <= 100:
        candidate = getattr(builtins, value, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            return value
    return "WorkerError"


def _child_environment():
    environment = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    environment.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "CATTY_RAG_PARENT_PID": str(os.getpid()),
    })
    return environment


def _stop_process(proc):
    try:
        if proc.poll() is not None:
            return True
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=_STOP_WAIT)
            return True
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=_STOP_WAIT)
            return True
        except subprocess.TimeoutExpired:
            return False
    except OSError:
        return False


class _Generation:
    def __init__(self, proc):
        self.proc = proc
        self.outgoing = queue.Queue(maxsize=1)
        self.responses = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.ready = False
        self.pending_id = None
        self.response_posted = False


class LocalBGEEmbeddingFunction:
    def __init__(self, worker_path=None, startup_timeout=30.0, request_timeout=5.0, restart_delay=10.0):
        self._worker_path = Path(worker_path) if worker_path is not None else Path(__file__).with_name("rag_embedding_worker.py")
        self._startup_timeout = float(startup_timeout)
        self._request_timeout = float(request_timeout)
        self._restart_delay = float(restart_delay)
        if not math.isfinite(self._startup_timeout) or self._startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive and finite")
        if not math.isfinite(self._request_timeout) or self._request_timeout <= 0:
            raise ValueError("request_timeout must be positive and finite")
        if not math.isfinite(self._restart_delay) or self._restart_delay < 0:
            raise ValueError("restart_delay must be non-negative and finite")
        # This lock guards process publication, readiness, and the single request slot.
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._waiter_slot = threading.BoundedSemaphore(1)
        self._shutdown = threading.Event()
        self._closed = False
        self._supervisor = None
        self._generation = None
        self._proc = None
        self._next_id = 0
        self._last_error_type = ""

    @staticmethod
    def name():
        return "local-bge-small-zh"

    @staticmethod
    def is_legacy():
        return True

    @property
    def ready(self):
        with self._lock:
            gen = self._generation
            return bool(not self._closed and gen is not None and gen.ready and not gen.stopped.is_set())

    def start(self):
        with self._lock:
            if self._closed:
                raise LocalBGEClosedError("local BGE worker is closed")
            if getattr(sys, "frozen", False):
                raise LocalBGENotReadyError("local BGE worker is unavailable in a frozen application (RuntimeError)")
            if self._supervisor is not None:
                return
            thread = threading.Thread(target=self._supervise, name="catty-rag-supervisor", daemon=True)
            self._supervisor = thread
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                if self._supervisor is thread:
                    self._supervisor = None
            raise LocalBGEWorkerError(f"local BGE supervisor failed ({_error_type(type(exc).__name__)})") from None

    def close(self):
        with self._condition:
            self._closed = True
            self._shutdown.set()
            gen = self._generation
            proc = self._proc
            thread = self._supervisor
            self._condition.notify_all()
        if gen is not None:
            self._retire(gen, "Closed")
        if proc is not None:
            _stop_process(proc)
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=2 * _STOP_WAIT + _POLL_INTERVAL)

    def __call__(self, input):
        if not isinstance(input, (list, tuple)):
            raise TypeError("local BGE input must be a sequence of strings")
        if len(input) > _MAX_BATCH_SIZE:
            raise ValueError("local BGE input exceeds 128 items")
        if any(not isinstance(item, str) for item in input):
            raise TypeError("local BGE input must be a sequence of strings")
        texts = list(input)
        deadline = time.monotonic() + self._request_timeout
        self.start()
        waiting = False
        try:
            with self._condition:
                while True:
                    if self._closed:
                        raise LocalBGEClosedError("local BGE worker is closed")
                    gen = self._generation
                    if gen is None or not gen.ready or gen.stopped.is_set():
                        suffix = f" ({self._last_error_type})" if self._last_error_type else ""
                        raise LocalBGENotReadyError(f"local BGE worker is not ready{suffix}")
                    if not texts:
                        return []
                    if gen.pending_id is None:
                        now = time.monotonic()
                        if waiting and now >= busy_deadline:
                            raise LocalBGEBusyError("local BGE worker is busy")
                        if now >= deadline:
                            raise LocalBGETimeoutError("local BGE request timed out (TimeoutError)")
                        self._next_id += 1
                        request_id = self._next_id
                        gen.pending_id = request_id
                        gen.response_posted = False
                        break
                    if not waiting:
                        if not self._waiter_slot.acquire(blocking=False):
                            raise LocalBGEBusyError("local BGE worker is busy")
                        waiting = True
                        busy_deadline = min(deadline, time.monotonic() + _BUSY_WAIT)
                    remaining = busy_deadline - time.monotonic()
                    if remaining <= 0:
                        raise LocalBGEBusyError("local BGE worker is busy")
                    self._condition.wait(remaining)
        finally:
            if waiting:
                self._waiter_slot.release()
        try:
            try:
                gen.outgoing.put_nowait((request_id, texts))
            except queue.Full:
                self._retire(gen, "ProtocolError")
                _stop_process(gen.proc)
                raise LocalBGEProtocolError("local BGE request queue is unavailable") from None
            try:
                event, payload = gen.responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                self._retire(gen, "TimeoutError")
                _stop_process(gen.proc)
                with self._lock:
                    closed = self._closed
                if closed:
                    raise LocalBGEClosedError("local BGE worker is closed") from None
                raise LocalBGETimeoutError("local BGE inference timed out (TimeoutError)") from None
            with self._lock:
                if self._closed:
                    raise LocalBGEClosedError("local BGE worker is closed")
            if event == "input_error":
                raise ValueError(f"local BGE input is invalid ({payload})")
            if event == "error":
                raise LocalBGEWorkerError(f"local BGE inference failed ({payload})")
            if event == "stopped":
                raise LocalBGEWorkerError(f"local BGE worker stopped ({payload})")
            try:
                return self._validate_embeddings(payload, len(texts))
            except (TypeError, ValueError, OverflowError):
                self._retire(gen, "ProtocolError")
                _stop_process(gen.proc)
                raise LocalBGEProtocolError("local BGE worker returned invalid embeddings (ValueError)") from None
        finally:
            with self._condition:
                if gen.pending_id == request_id:
                    gen.pending_id = None
                    gen.response_posted = False
                    self._condition.notify_all()

    def embed_query(self, input):
        return self(input)

    @staticmethod
    def _validate_embeddings(embeddings, count):
        if not isinstance(embeddings, list) or len(embeddings) != count:
            raise ValueError
        result = []
        for row in embeddings:
            if not isinstance(row, list) or len(row) != _VECTOR_SIZE:
                raise ValueError
            output_row = []
            for value in row:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError
                output_row.append(number)
            result.append(output_row)
        return result

    def _post_response(self, gen, request_id, event, payload):
        with self._lock:
            if (self._generation is not gen or gen.stopped.is_set()
                    or gen.pending_id != request_id or gen.response_posted):
                return
            gen.response_posted = True
            gen.responses.put_nowait((event, payload))

    def _retire(self, gen, error_type):
        report = False
        with self._condition:
            if gen.stopped.is_set():
                return
            gen.ready = False
            gen.stopped.set()
            if self._generation is gen:
                report = error_type != "Closed" and self._last_error_type != error_type
                self._last_error_type = error_type
            if gen.pending_id is not None and not gen.response_posted:
                gen.response_posted = True
                gen.responses.put_nowait(("stopped", error_type))
            self._condition.notify_all()
        if report:
            logger.warning("catty_rag: local embedding worker unavailable (%s)", error_type)

    def _read_stdout(self, gen):
        try:
            stream = gen.proc.stdout
            while not gen.stopped.is_set():
                line = stream.readline(_MAX_OUTPUT_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > _MAX_OUTPUT_LINE_BYTES:
                    while line and not line.endswith(b"\n") and not gen.stopped.is_set():
                        line = stream.readline(_MAX_OUTPUT_LINE_BYTES + 1)
                    continue
                if not line.startswith(_PROTOCOL_PREFIX):
                    continue
                try:
                    message = json.loads(line[len(_PROTOCOL_PREFIX):])
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(message, dict):
                    continue
                event = message.get("event")
                if event == "ready":
                    report = False
                    with self._lock:
                        if self._generation is gen and not gen.stopped.is_set() and not self._closed:
                            report = not gen.ready
                            gen.ready = True
                            self._last_error_type = ""
                    if report:
                        logger.info("catty_rag: local embedding worker ready")
                elif event == "startup_error":
                    self._retire(gen, _error_type(message.get("error_type")))
                    break
                elif event in ("result", "error"):
                    request_id = message.get("id")
                    if not isinstance(request_id, int) or isinstance(request_id, bool):
                        continue
                    payload = message.get("embeddings") if event == "result" else _error_type(message.get("error_type"))
                    self._post_response(gen, request_id, event, payload)
        except Exception as exc:
            self._retire(gen, _error_type(type(exc).__name__))
        finally:
            self._retire(gen, "WorkerExited")
            try:
                gen.proc.stdout.close()
            except (OSError, ValueError):
                pass

    def _write_stdin(self, gen):
        try:
            while not gen.stopped.is_set():
                try:
                    request_id, texts = gen.outgoing.get(timeout=_POLL_INTERVAL)
                except queue.Empty:
                    continue
                if gen.stopped.is_set():
                    break
                try:
                    line = json.dumps({"id": request_id, "input": texts}, ensure_ascii=False, separators=(",", ":"))
                    data = line.encode("utf-8") + b"\n"
                    if len(data) > _MAX_INPUT_LINE_BYTES:
                        self._post_response(gen, request_id, "input_error", "ValueError")
                        continue
                except (TypeError, ValueError, UnicodeError) as exc:
                    self._post_response(gen, request_id, "input_error", _error_type(type(exc).__name__))
                    continue
                if gen.stopped.is_set():
                    break
                gen.proc.stdin.write(data)
                gen.proc.stdin.flush()
        except Exception as exc:
            self._retire(gen, _error_type(type(exc).__name__))
        finally:
            try:
                gen.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def _supervise(self):
        try:
            while not self._shutdown.is_set():
                if getattr(sys, "frozen", False):
                    with self._lock:
                        self._last_error_type = "RuntimeError"
                    break
                try:
                    proc = subprocess.Popen(
                        [sys.executable, "-I", "-B", "-u", "-X", "utf8", str(self._worker_path)],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        env=_child_environment(),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                    )
                except Exception as exc:
                    error_type = _error_type(type(exc).__name__)
                    with self._lock:
                        report = self._last_error_type != error_type
                        self._last_error_type = error_type
                    if report:
                        logger.warning("catty_rag: local embedding worker launch failed (%s)", error_type)
                    if self._shutdown.wait(max(self._restart_delay, 0.01)):
                        break
                    continue
                gen = _Generation(proc)
                # A spawn already in progress remains owned even if close raced it.
                with self._lock:
                    self._proc = proc
                    self._generation = gen
                    closed = self._closed
                try:
                    if closed:
                        self._retire(gen, "Closed")
                    else:
                        threading.Thread(target=self._read_stdout, args=(gen,), name="catty-rag-reader", daemon=True).start()
                        threading.Thread(target=self._write_stdin, args=(gen,), name="catty-rag-writer", daemon=True).start()
                        deadline = time.monotonic() + self._startup_timeout
                        while not gen.stopped.wait(_POLL_INTERVAL):
                            if proc.poll() is not None:
                                self._retire(gen, "WorkerExited")
                                break
                            with self._lock:
                                ready = gen.ready
                            if not ready and time.monotonic() >= deadline:
                                self._retire(gen, "TimeoutError")
                                break
                except Exception as exc:
                    self._retire(gen, _error_type(type(exc).__name__))
                finally:
                    self._retire(gen, "WorkerExited")
                    while not _stop_process(proc):
                        time.sleep(_POLL_INTERVAL)
                    with self._lock:
                        if self._generation is gen:
                            self._generation = None
                        if self._proc is proc:
                            self._proc = None
                if self._shutdown.wait(max(self._restart_delay, 0.01)):
                    break
        finally:
            with self._lock:
                if self._supervisor is threading.current_thread():
                    self._supervisor = None
