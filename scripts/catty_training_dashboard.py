from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS_PATH = _ROOT / "src" / "catty_qq_ai" / "persona_prompts.py"
_PROMPTS_SPEC = importlib.util.spec_from_file_location("catty_dashboard_persona_prompts", _PROMPTS_PATH)
assert _PROMPTS_SPEC is not None and _PROMPTS_SPEC.loader is not None
_PROMPTS = importlib.util.module_from_spec(_PROMPTS_SPEC)
_PROMPTS_SPEC.loader.exec_module(_PROMPTS)

NO_REPLY_MARKER = "<<<CATTY_NO_REPLY>>>"
REPLY_SPLIT_MARKER = "<<<CATTY_REPLY_SPLIT>>>"
THINKING_TEST_PROMPT = (
    "Thinking 测试模式：允许短暂思考来对比质量，但必须尽快给最终正文；"
    "不要卡在思维链，不要只输出思考过程，最终回复仍按主 AI 人格和 QQ 口吻输出。"
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve(base_dir: Path, value: object, default: str) -> Path:
    raw = str(value or default)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else base_dir / path


def _count_jsonl(path: Path) -> int:
    if not path.is_file():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                count += 1
    return count


def _tail_text(path: Path, *, max_lines: int = 80) -> str:
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def _chat_completions_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _ollama_chat_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1", "/api/chat", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/api/chat"


def _looks_like_ollama_route(base_url: str, api_key: str, extra_body: dict[str, Any]) -> bool:
    native_flag = extra_body.get("native_ollama")
    if isinstance(native_flag, bool):
        return native_flag
    parsed = urlparse(base_url)
    return parsed.port == 11434 or api_key.strip().lower() == "ollama"


def _json_object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ollama_options(*, temperature: object, max_tokens: int, extra_body: dict[str, Any]) -> dict[str, Any]:
    raw_options = extra_body.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    if temperature is not None and "temperature" not in options:
        options["temperature"] = temperature
    if "num_predict" not in options:
        options["num_predict"] = max_tokens
    return options


def _route_config(config: dict[str, Any]) -> dict[str, Any]:
    ai = _json_object(config.get("ai"))
    local_critic = _json_object(config.get("local_critic"))
    training = _json_object(config.get("local_training"))
    timeout = float(training.get("model_test_request_timeout") or local_critic.get("request_timeout") or 60)
    local_critic_extra_body = _json_object(local_critic.get("extra_body"))
    extra_body = {
        "think": False,
        **local_critic_extra_body,
    }
    max_tokens = int(training.get("model_test_max_tokens") or ai.get("max_tokens") or 480)
    return {
        "base_url": str(local_critic.get("base_url") or "http://127.0.0.1:11434/v1"),
        "api_key": str(local_critic.get("api_key") or "ollama"),
        "model": str(local_critic.get("model") or ""),
        "temperature": local_critic.get("temperature", ai.get("temperature", 0.7)),
        "max_tokens": max_tokens,
        "thinking_max_tokens": int(training.get("model_test_thinking_max_tokens") or min(max_tokens, 96)),
        "timeout": timeout,
        "thinking_timeout": float(training.get("model_test_thinking_timeout") or min(timeout, 20.0)),
        "extra_headers": _json_object(local_critic.get("extra_headers")),
        "extra_body": extra_body,
    }


def _thread_test_prompt() -> str:
    return (
        "这是训练测试窗口的主线程模拟请求。请像真实 QQ 回复主线程一样回答："
        "已经通过本地 reply gate，不要再做是否回复判断，不要解释测试环境，不要输出 "
        f"{NO_REPLY_MARKER}；信息不足时用笨猫口吻短问一句。"
    )


def _test_memory_context(config_path: Path, config: dict[str, Any]) -> str:
    memory = _json_object(config.get("memory"))
    if memory.get("enabled") is False:
        return "测试窗口模拟记忆：当前关闭 memory.enabled。"
    lines = ["测试窗口模拟记忆：", "- 当前输入按私聊/主线程验收处理，目标是观察模型真实回复质量。"]
    user_titles = _json_object(memory.get("user_titles"))
    owner_ids = [str(user_id) for user_id, title in user_titles.items() if str(title).strip() == "主人"]
    if owner_ids:
        owner_id = owner_ids[0]
        lines.append(f"- 配置里 QQ {owner_id} 的称呼是「主人」；测试里可以按主人语气回应。")
        storage_dir = str(memory.get("user_storage_dir") or "memory_users")
        user_dir = _resolve(config_path.resolve().parent, storage_dir, "memory_users")
        user_file = user_dir / f"user_{owner_id}.json"
        user_data = _load_json(user_file)
        data = _json_object(user_data.get("data"))
        summary = str(data.get("private_summary") or "").strip()
        if summary:
            lines.append("- 私聊摘要：" + summary[:500])
        profile = _json_object(data.get("private_profile"))
        impression = str(profile.get("impression") or "").strip()
        if impression:
            lines.append("- 私聊画像：" + impression[:160])
    else:
        lines.append("- 未配置主人称呼；测试里不要强行叫主人。")
    lines.append("- 这是记忆上下文，不要原样背诵，只在回答相关时自然使用。")
    return "\n".join(lines)


def build_model_test_messages(config_path: Path, user_text: str, *, thinking: bool = False) -> list[dict[str, object]]:
    config = _load_json(config_path)
    chat = _json_object(config.get("chat"))
    system_prompt = str(chat.get("system_prompt") or "").strip()
    messages: list[dict[str, object]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "system", "content": _PROMPTS.build_persona_memory_prompt(system_prompt)})
    messages.append({"role": "system", "content": _PROMPTS.build_reply_intelligence_prompt(NO_REPLY_MARKER)})
    messages.append({"role": "system", "content": _PROMPTS.build_reply_self_check_prompt(NO_REPLY_MARKER, REPLY_SPLIT_MARKER)})
    messages.append({"role": "system", "content": _PROMPTS.build_catgirl_examples_prompt(NO_REPLY_MARKER)})
    messages.append({"role": "system", "content": _thread_test_prompt()})
    messages.append({"role": "system", "content": _test_memory_context(config_path, config)})
    if thinking:
        messages.append({"role": "system", "content": THINKING_TEST_PROMPT})
    else:
        messages.append({"role": "system", "content": "/no_think"})
    messages.append({"role": "user", "content": user_text.strip()})
    return messages


def run_model_test(config_path: Path, user_text: str, *, thinking: bool = False) -> dict[str, Any]:
    config = _load_json(config_path)
    route = _route_config(config)
    if not route["model"]:
        raise ValueError("local_critic.model is empty")
    messages = build_model_test_messages(config_path, user_text, thinking=thinking)
    request_timeout = float(route["thinking_timeout"] if thinking else route["timeout"])
    extra_body = dict(route["extra_body"])
    extra_body["think"] = bool(thinking)
    request_max_tokens = int(route["thinking_max_tokens"] if thinking else route["max_tokens"])
    use_native_ollama = _looks_like_ollama_route(route["base_url"], route["api_key"], extra_body)
    if use_native_ollama:
        payload = {
            "model": route["model"],
            "messages": messages,
            "stream": False,
            "options": _ollama_options(
                temperature=route["temperature"],
                max_tokens=request_max_tokens,
                extra_body=extra_body,
            ),
        }
        if "keep_alive" in extra_body:
            payload["keep_alive"] = extra_body["keep_alive"]
        if "think" in extra_body:
            payload["think"] = extra_body["think"]
        url = _ollama_chat_url(route["base_url"])
        transport = "ollama_api_chat"
    else:
        payload = {
            "model": route["model"],
            "messages": messages,
            "stream": False,
            "temperature": route["temperature"],
            "max_tokens": request_max_tokens,
        }
        payload.update(extra_body)
        url = _chat_completions_url(route["base_url"])
        transport = "openai_chat_completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {route['api_key']}",
        "Content-Type": "application/json",
        **route["extra_headers"],
    }
    req = request.Request(url, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with request.urlopen(req, timeout=request_timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        mode = "thinking" if thinking else "no_think"
        raise RuntimeError(f"{mode} request timed out after {request_timeout:g}s") from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
            mode = "thinking" if thinking else "no_think"
            raise RuntimeError(f"{mode} request timed out after {request_timeout:g}s") from exc
        raise
    elapsed = time.perf_counter() - started
    data = json.loads(raw)
    if use_native_ollama:
        message = data.get("message") if isinstance(data, dict) else {}
        content = message.get("content") if isinstance(message, dict) else ""
        text = str(content or data.get("response") or "").strip()
    else:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text") or item.get("content") or "") for item in content if isinstance(item, dict)
            )
        text = str(content or choice.get("text") or "").strip()
    return {
        "created_at": int(time.time()),
        "model": route["model"],
        "base_url": route["base_url"],
        "elapsed_seconds": round(elapsed, 3),
        "prompt": user_text,
        "messages": messages,
        "response": text,
        "thinking_enabled": bool(thinking),
        "transport": transport,
        "request_timeout_seconds": request_timeout,
        "request_max_tokens": request_max_tokens,
        "request_extra_body": extra_body,
        "usage": data.get("usage") if isinstance(data, dict) else None,
    }


def model_eval_path(config_path: Path) -> Path:
    config = _load_json(config_path)
    training = _json_object(config.get("local_training"))
    return _resolve(config_path.resolve().parent, training.get("model_test_scores_path"), "training/model_eval_scores.jsonl")


def save_model_eval(config_path: Path, result: dict[str, Any], *, score: int, note: str = "") -> Path:
    path = model_eval_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **result,
        "score": max(min(int(score), 5), 1),
        "note": note.strip(),
        "scored_at": int(time.time()),
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def latest_model_evals(config_path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    path = model_eval_path(config_path)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
    return records


def _latest_status(output_dir: Path) -> dict[str, Any]:
    idle = _load_json(output_dir / "last_idle_status.json")
    busy = _load_json(output_dir / "last_busy_status.json")
    candidates = [status for status in [idle, busy] if status]
    if not candidates:
        return {}
    return max(candidates, key=lambda item: int(item.get("created_at") or 0))


def _suggestions_from(status: dict[str, Any]) -> list[str]:
    audit = status.get("artifact_audit")
    if not isinstance(audit, dict):
        return []
    value = audit.get("next_suggestions")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def build_snapshot(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    base_dir = config_path.parent
    config = _load_json(config_path)
    training = config.get("local_training", {})
    audit_ai = config.get("audit_ai", {})
    if not isinstance(training, dict):
        training = {}
    if not isinstance(audit_ai, dict):
        audit_ai = {}

    reply_dataset = _resolve(base_dir, training.get("dataset_path"), "training/reply_gate_dataset.jsonl")
    assistant_dataset = _resolve(
        base_dir,
        training.get("assistant_dataset_path"),
        "training/assistant_reply_dataset.jsonl",
    )
    reply_output = _resolve(base_dir, training.get("output_dir"), "training/reply_gate_lora")
    assistant_output = _resolve(
        base_dir,
        training.get("assistant_output_dir"),
        "training/assistant_reply_lora",
    )
    state_path = _resolve(base_dir, training.get("state_path"), "training/reply_gate_train_state.json")
    log_path = _resolve(base_dir, training.get("progress_log_path"), "training/local_training.log")
    reply_status = _latest_status(reply_output)
    assistant_status = _latest_status(assistant_output)

    return {
        "config": str(config_path),
        "updated_at": int(time.time()),
        "enabled": bool(training.get("enabled")),
        "auto_train_on_startup": bool(training.get("auto_train_on_startup")),
        "watch_interval_seconds": int(training.get("watch_interval_seconds") or 0),
        "audit_model": str(training.get("artifact_audit_model") or audit_ai.get("model") or ""),
        "audit_temperature": float(training.get("artifact_audit_temperature") or 0.5),
        "state": _load_json(state_path),
        "datasets": {
            "reply_gate": {
                "path": str(reply_dataset),
                "samples": _count_jsonl(reply_dataset),
                "min_samples": int(training.get("min_samples") or 200),
                "status": reply_status,
            },
            "assistant_reply": {
                "path": str(assistant_dataset),
                "samples": _count_jsonl(assistant_dataset),
                "min_samples": int(training.get("assistant_min_samples") or training.get("min_samples") or 200),
                "status": assistant_status,
            },
        },
        "suggestions": {
            "reply_gate": _suggestions_from(reply_status),
            "assistant_reply": _suggestions_from(assistant_status),
        },
        "log_path": str(log_path),
        "log_tail": _tail_text(log_path),
        "model_eval_path": str(model_eval_path(config_path)),
        "model_evals": latest_model_evals(config_path),
    }


def _status_line(name: str, item: dict[str, Any]) -> str:
    samples = int(item.get("samples") or 0)
    minimum = int(item.get("min_samples") or 1)
    status = item.get("status")
    if not isinstance(status, dict) or not status:
        return f"{name}: samples {samples}/{minimum}; status waiting"
    audit = status.get("artifact_audit") if isinstance(status.get("artifact_audit"), dict) else {}
    audit_text = ""
    if audit:
        audit_text = (
            f"; GLM {audit.get('status', 'unknown')}"
            f" apply={bool(audit.get('allow_apply'))}"
            f" merge={bool(audit.get('allow_merge'))}"
            f" risk={audit.get('risk_level', 'unknown')}"
        )
    return (
        f"{name}: samples {samples}/{minimum}; "
        f"{status.get('mode', '-')}/{status.get('status', '-')}; "
        f"steps={status.get('max_steps', '-')}{audit_text}"
    )


def _run_gui(config_path: Path, poll_seconds: float) -> None:
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title("Catty training progress")
    root.geometry("1040x760")
    root.minsize(860, 620)

    header = ttk.Label(root, text="Catty training progress / GLM-5.1 audit", font=("Microsoft YaHei UI", 12, "bold"))
    header.pack(fill="x", padx=12, pady=(10, 4))

    summary_var = tk.StringVar(value="Loading...")
    summary = ttk.Label(root, textvariable=summary_var, justify="left")
    summary.pack(fill="x", padx=12, pady=(0, 8))

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    status_text = ScrolledText(notebook, height=12, wrap="word")
    audit_text = ScrolledText(notebook, height=12, wrap="word")
    log_text = ScrolledText(notebook, height=12, wrap="word")
    for widget in [status_text, audit_text, log_text]:
        widget.configure(state="disabled", font=("Consolas", 10))

    notebook.add(status_text, text="Progress")
    notebook.add(audit_text, text="GLM audit")
    notebook.add(log_text, text="Log")

    test_frame = ttk.Frame(notebook)
    notebook.add(test_frame, text="Ollama test")

    test_frame.columnconfigure(0, weight=1)
    test_frame.rowconfigure(1, weight=1)
    test_frame.rowconfigure(4, weight=2)
    test_frame.rowconfigure(6, weight=1)

    prompt_label = ttk.Label(test_frame, text="Prompt")
    prompt_label.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
    prompt_text = ScrolledText(test_frame, height=5, wrap="word", font=("Microsoft YaHei UI", 10))
    prompt_text.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

    controls = ttk.Frame(test_frame)
    controls.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
    controls.columnconfigure(5, weight=1)
    ask_button = ttk.Button(controls, text="Ask model")
    ask_button.grid(row=0, column=0, sticky="w")
    thinking_var = tk.BooleanVar(value=False)
    thinking_check = ttk.Checkbutton(controls, text="Thinking", variable=thinking_var)
    thinking_check.grid(row=0, column=1, padx=(12, 0), sticky="w")
    ttk.Label(controls, text="Score").grid(row=0, column=2, padx=(12, 4))
    score_var = tk.IntVar(value=5)
    score_box = ttk.Combobox(controls, textvariable=score_var, values=[1, 2, 3, 4, 5], width=4, state="readonly")
    score_box.grid(row=0, column=3, sticky="w")
    save_button = ttk.Button(controls, text="Save score")
    save_button.grid(row=0, column=4, padx=(8, 0), sticky="w")
    test_status_var = tk.StringVar(value="Ready")
    ttk.Label(controls, textvariable=test_status_var).grid(row=0, column=5, sticky="e")

    output_label = ttk.Label(test_frame, text="Output")
    output_label.grid(row=3, column=0, sticky="w", padx=8, pady=(0, 2))
    output_text = ScrolledText(test_frame, height=10, wrap="word", font=("Microsoft YaHei UI", 10))
    output_text.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 8))

    note_label = ttk.Label(test_frame, text="Note")
    note_label.grid(row=5, column=0, sticky="w", padx=8, pady=(0, 2))
    note_text = ScrolledText(test_frame, height=3, wrap="word", font=("Microsoft YaHei UI", 10))
    note_text.grid(row=6, column=0, sticky="nsew", padx=8, pady=(0, 8))

    history_label = ttk.Label(test_frame, text="Recent scores")
    history_label.grid(row=7, column=0, sticky="w", padx=8, pady=(0, 2))
    eval_text = ScrolledText(test_frame, height=7, wrap="word")
    eval_text.configure(state="disabled", font=("Consolas", 9))
    eval_text.grid(row=8, column=0, sticky="nsew", padx=8, pady=(0, 8))
    latest_result: dict[str, Any] = {}
    active_request = {"id": 0, "done": True}

    def replace_text(widget: ScrolledText, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def render_eval_history(records: list[dict[str, Any]]) -> str:
        if not records:
            return f"No scores yet: {model_eval_path(config_path)}"
        lines: list[str] = []
        for record in reversed(records[-10:]):
            elapsed = record.get("elapsed_seconds", "-")
            score = record.get("score", "-")
            prompt = str(record.get("prompt") or "").replace("\n", " ")[:80]
            response = str(record.get("response") or "").replace("\n", " ")[:120]
            note = str(record.get("note") or "").replace("\n", " ")[:80]
            lines.append(f"[score={score} time={elapsed}s] {prompt}")
            lines.append(f"  -> {response}")
            if note:
                lines.append(f"  note: {note}")
        return "\n".join(lines)

    def set_output(text: str) -> None:
        output_text.configure(state="normal")
        output_text.delete("1.0", "end")
        output_text.insert("1.0", text)
        output_text.configure(state="normal")

    def ask_model() -> None:
        prompt = prompt_text.get("1.0", "end").strip()
        if not prompt:
            test_status_var.set("Prompt is empty")
            return
        ask_button.configure(state="disabled")
        save_button.configure(state="disabled")
        mode = "thinking" if thinking_var.get() else "no_think"
        timeout = 60.0
        try:
            route = _route_config(_load_json(config_path))
            timeout = float(route["thinking_timeout"] if thinking_var.get() else route["timeout"])
            test_status_var.set(f"Asking model ({mode}, timeout {timeout:g}s)...")
        except Exception:
            test_status_var.set(f"Asking model ({mode})...")
        set_output("")
        active_request["id"] = int(active_request["id"]) + 1
        active_request["done"] = False
        request_id = int(active_request["id"])
        thinking_enabled = bool(thinking_var.get())

        def still_waiting() -> bool:
            return int(active_request["id"]) == request_id and not bool(active_request["done"])

        def wall_clock_timeout() -> None:
            if not still_waiting():
                return
            finish_error(
                RuntimeError(
                    f"{mode} wall-clock timeout after {timeout:g}s; "
                    "Ollama may still be generating in the background. "
                    "For qwen2.5 on Xeon v4, uncheck Thinking first."
                ),
                request_id=request_id,
            )

        def worker() -> None:
            try:
                result = run_model_test(config_path, prompt, thinking=thinking_enabled)
            except Exception as exc:
                root.after(0, lambda: finish_error(exc, request_id=request_id))
            else:
                root.after(0, lambda: finish_success(result, request_id=request_id))

        root.after(int(max(timeout, 1.0) * 1000), wall_clock_timeout)
        threading.Thread(target=worker, daemon=True).start()

    def finish_success(result: dict[str, Any], *, request_id: int | None = None) -> None:
        if request_id is not None and (int(active_request["id"]) != request_id or bool(active_request["done"])):
            return
        active_request["done"] = True
        latest_result.clear()
        latest_result.update(result)
        elapsed = result.get("elapsed_seconds", "-")
        model = result.get("model", "-")
        mode = "thinking" if result.get("thinking_enabled") else "no_think"
        usage = result.get("usage")
        request_meta = {
            "transport": result.get("transport"),
            "timeout_seconds": result.get("request_timeout_seconds"),
            "max_tokens": result.get("request_max_tokens"),
            "extra_body": result.get("request_extra_body"),
        }
        meta_text = "\n\nrequest=" + json.dumps(request_meta, ensure_ascii=False)
        usage_text = f"\nusage={json.dumps(usage, ensure_ascii=False)}" if usage else ""
        set_output(str(result.get("response") or "") + meta_text + usage_text)
        test_status_var.set(f"{model} {mode} replied in {elapsed}s")
        ask_button.configure(state="normal")
        save_button.configure(state="normal")

    def finish_error(exc: Exception, *, request_id: int | None = None) -> None:
        if request_id is not None and (int(active_request["id"]) != request_id or bool(active_request["done"])):
            return
        active_request["done"] = True
        latest_result.clear()
        set_output(f"{exc.__class__.__name__}: {exc}")
        test_status_var.set("Request failed")
        ask_button.configure(state="normal")
        save_button.configure(state="disabled")

    def save_score() -> None:
        if not latest_result:
            test_status_var.set("No model output to score")
            return
        path = save_model_eval(
            config_path,
            latest_result,
            score=int(score_var.get()),
            note=note_text.get("1.0", "end").strip(),
        )
        note_text.delete("1.0", "end")
        test_status_var.set(f"Saved score to {path}")
        replace_text(eval_text, render_eval_history(latest_model_evals(config_path)))

    ask_button.configure(command=ask_model)
    save_button.configure(command=save_score, state="disabled")

    def refresh() -> None:
        snapshot = build_snapshot(config_path)
        summary_var.set(
            "enabled={enabled}  auto_train={auto_train}  watch={watch}s  "
            "audit_model={model}  audit_temperature={temperature}".format(
                enabled=snapshot["enabled"],
                auto_train=snapshot["auto_train_on_startup"],
                watch=snapshot["watch_interval_seconds"],
                model=snapshot["audit_model"] or "-",
                temperature=snapshot["audit_temperature"],
            )
        )

        datasets = snapshot["datasets"]
        progress_lines = [
            time.strftime("Updated at %Y-%m-%d %H:%M:%S", time.localtime(snapshot["updated_at"])),
            _status_line("reply_gate", datasets["reply_gate"]),
            _status_line("assistant_reply", datasets["assistant_reply"]),
            "",
            "State:",
            json.dumps(snapshot["state"], ensure_ascii=False, indent=2) or "{}",
        ]
        replace_text(status_text, "\n".join(progress_lines))

        audit_lines: list[str] = []
        for task, item in datasets.items():
            status = item.get("status")
            audit = status.get("artifact_audit") if isinstance(status, dict) else {}
            audit_lines.append(f"[{task}]")
            if isinstance(audit, dict) and audit:
                audit_lines.append(json.dumps(audit, ensure_ascii=False, indent=2))
            else:
                audit_lines.append("No GLM audit result yet.")
            suggestions = snapshot["suggestions"].get(task, [])
            if suggestions:
                audit_lines.append("Next suggestions:")
                audit_lines.extend(f"- {item}" for item in suggestions)
            audit_lines.append("")
        replace_text(audit_text, "\n".join(audit_lines).rstrip())
        replace_text(log_text, snapshot["log_tail"] or f"No log yet: {snapshot['log_path']}")
        replace_text(eval_text, render_eval_history(snapshot["model_evals"]))
        root.after(int(max(poll_seconds, 1.0) * 1000), refresh)

    refresh()
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    _run_gui(Path(args.config), args.poll_seconds)


if __name__ == "__main__":
    main()
