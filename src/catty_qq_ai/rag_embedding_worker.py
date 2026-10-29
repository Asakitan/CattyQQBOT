import contextlib
import json
import math
import os
import queue
import sys
import threading


_PREFIX = b"CATTY_RAG_V1="
_MAX_MESSAGE_BYTES = 1024 * 1024
_MAX_BATCH_ITEMS = 128
_MAX_PENDING_REQUESTS = 32
_MAX_PENDING_OUTPUTS = 32
_EMBEDDING_DIM = 512
_MAX_TEXT_CHARS = 256


class MessageTooLargeError(ValueError):
    pass


class RequestQueueFullError(RuntimeError):
    pass


class EmbeddingValidationError(ValueError):
    pass


def _reject_constant(_value):
    raise ValueError


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_message(line):
    return json.loads(
        line.decode("utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )


def _recover_id(line):
    try:
        message = _decode_message(line)
        if isinstance(message, dict) and type(message.get("id")) is int:
            return message["id"]
    except Exception:
        pass
    return None


def _error(request_id, error_type):
    return {"event": "error", "id": request_id, "error_type": error_type}


def _publish(outputs, event, *, from_reader=False, wait=False):
    done = threading.Event() if wait else None
    try:
        if from_reader:
            outputs.put_nowait((event, done))
        else:
            outputs.put((event, done))
    except queue.Full:
        os._exit(1)
    if done is not None:
        done.wait()


def _write_events(outputs, protocol):
    while True:
        event, done = outputs.get()
        try:
            payload = json.dumps(
                event, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            ).encode("ascii")
            protocol.write(_PREFIX + payload + b"\n")
            protocol.flush()
        except BaseException:
            os._exit(1)
        if done is not None:
            done.set()


def _read_requests(requests, outputs, stdin):
    try:
        while True:
            line = stdin.readline(_MAX_MESSAGE_BYTES + 1)
            if not line:
                os._exit(0)
            if len(line) > _MAX_MESSAGE_BYTES:
                _publish(
                    outputs,
                    _error(None, MessageTooLargeError.__name__),
                    from_reader=True,
                )
                while not line.endswith(b"\n"):
                    line = stdin.readline(_MAX_MESSAGE_BYTES + 1)
                    if not line:
                        os._exit(0)
                continue
            try:
                requests.put_nowait(line)
            except queue.Full:
                _publish(
                    outputs,
                    _error(_recover_id(line), RequestQueueFullError.__name__),
                    from_reader=True,
                )
    except BaseException:
        os._exit(1)


def _start_parent_watch():
    parent = int(os.environ.get("CATTY_RAG_PARENT_PID") or os.getppid())
    if parent <= 1:
        raise OSError("invalid parent process")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel.OpenProcess(0x00100000, False, parent)
        if not handle:
            raise OSError("cannot monitor parent process")

        def watch():
            kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
            os._exit(0)
    else:
        def watch():
            wake = threading.Event()
            while os.getppid() == parent and parent > 1:
                wake.wait(0.5)
            os._exit(0)

    threading.Thread(target=watch, name="catty-rag-parent-watch", daemon=True).start()


@contextlib.contextmanager
def _silence_dependency_output():
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)


def _offline_audit(event, _args):
    if event in {
        "socket.bind",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.getnameinfo",
        "socket.sendmsg",
        "socket.sendto",
    }:
        raise PermissionError


def _load_model():
    model_name = os.environ.get("CATTY_RAG_MODEL", "").strip()
    if not model_name:
        raise RuntimeError("CATTY_RAG_MODEL is not configured")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    sys.addaudithook(_offline_audit)

    import torch

    torch.set_num_threads(1)
    from huggingface_hub import snapshot_download
    from text2vec import SentenceModel

    snapshot = snapshot_download(repo_id=model_name, local_files_only=True)
    return SentenceModel(snapshot, device="cpu")


def _encode(model, texts):
    if not texts:
        return []
    with _silence_dependency_output():
        vectors = model.encode(texts, normalize_embeddings=True)
    if len(vectors) != len(texts):
        raise EmbeddingValidationError
    embeddings = []
    for vector in vectors:
        if len(vector) != _EMBEDDING_DIM:
            raise EmbeddingValidationError
        row = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in row):
            raise EmbeddingValidationError
        embeddings.append(row)
    return embeddings


def _main():
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    requests = queue.Queue(maxsize=_MAX_PENDING_REQUESTS)
    outputs = queue.Queue(maxsize=_MAX_PENDING_OUTPUTS)
    threading.Thread(target=_write_events, args=(outputs, protocol), daemon=True).start()

    try:
        _start_parent_watch()
        with _silence_dependency_output():
            model = _load_model()
    except BaseException as exc:
        _publish(
            outputs,
            {"event": "startup_error", "error_type": type(exc).__name__},
            wait=True,
        )
        return 1
    # A blocked stdin reader can deadlock NumPy DLL initialization on Windows.
    threading.Thread(
        target=_read_requests, args=(requests, outputs, sys.stdin.buffer), daemon=True
    ).start()
    _publish(outputs, {"event": "ready"})

    while True:
        line = requests.get()
        request_id = None
        try:
            message = _decode_message(line)
            if not isinstance(message, dict):
                raise TypeError
            request_id = message.get("id")
            if type(request_id) is not int:
                request_id = None
                raise TypeError
            texts = message.get("input")
            if not isinstance(texts, list) or any(
                not isinstance(text, str) for text in texts
            ):
                raise TypeError
            if len(texts) > _MAX_BATCH_ITEMS:
                raise ValueError
            texts = [text[:_MAX_TEXT_CHARS] for text in texts]
            embeddings = _encode(model, texts)
        except BaseException as exc:
            _publish(outputs, _error(request_id, type(exc).__name__))
        else:
            _publish(
                outputs,
                {"event": "result", "id": request_id, "embeddings": embeddings},
            )


if __name__ == "__main__":
    raise SystemExit(_main())
