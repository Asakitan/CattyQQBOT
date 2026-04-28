from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error, request


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


def _json_object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _route_config(config: dict[str, Any]) -> dict[str, Any]:
    ai = _json_object(config.get("ai"))
    local_critic = _json_object(config.get("local_critic"))
    training = _json_object(config.get("local_training"))
    return {
        "base_url": str(local_critic.get("base_url") or "http://127.0.0.1:11434/v1"),
        "api_key": str(local_critic.get("api_key") or "ollama"),
        "model": str(local_critic.get("model") or ""),
        "temperature": local_critic.get("temperature", ai.get("temperature", 0.7)),
        "max_tokens": int(training.get("model_test_max_tokens") or ai.get("max_tokens") or 480),
        "timeout": float(training.get("model_test_request_timeout") or local_critic.get("request_timeout") or 60),
        "extra_headers": _json_object(local_critic.get("extra_headers")),
        "extra_body": _json_object(local_critic.get("extra_body")),
    }


def build_model_test_messages(config_path: Path, user_text: str) -> list[dict[str, object]]:
    config = _load_json(config_path)
    chat = _json_object(config.get("chat"))
    system_prompt = str(chat.get("system_prompt") or "").strip()
    messages: list[dict[str, object]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append(
        {
            "role": "system",
            "content": (
                "这是训练测试窗口的人工验收请求。请按主 AI 的人格、语气和回复规则正常回答用户，"
                "不要解释测试环境，不要输出 NO_REPLY；信息不足时用角色口吻短问一句。"
            ),
        }
    )
    messages.append({"role": "user", "content": user_text.strip()})
    return messages


def run_model_test(config_path: Path, user_text: str) -> dict[str, Any]:
    config = _load_json(config_path)
    route = _route_config(config)
    if not route["model"]:
        raise ValueError("local_critic.model is empty")
    messages = build_model_test_messages(config_path, user_text)
    payload: dict[str, Any] = {
        "model": route["model"],
        "messages": messages,
        "stream": False,
        "temperature": route["temperature"],
        "max_tokens": route["max_tokens"],
    }
    payload.update(route["extra_body"])
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {route['api_key']}",
        "Content-Type": "application/json",
        **route["extra_headers"],
    }
    req = request.Request(_chat_completions_url(route["base_url"]), data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with request.urlopen(req, timeout=route["timeout"]) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    elapsed = time.perf_counter() - started
    data = json.loads(raw)
    choice = data["choices"][0]
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(str(item.get("text") or item.get("content") or "") for item in content if isinstance(item, dict))
    text = str(content or choice.get("text") or "").strip()
    return {
        "created_at": int(time.time()),
        "model": route["model"],
        "base_url": route["base_url"],
        "elapsed_seconds": round(elapsed, 3),
        "prompt": user_text,
        "messages": messages,
        "response": text,
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
    ttk.Label(controls, text="Score").grid(row=0, column=1, padx=(12, 4))
    score_var = tk.IntVar(value=5)
    score_box = ttk.Combobox(controls, textvariable=score_var, values=[1, 2, 3, 4, 5], width=4, state="readonly")
    score_box.grid(row=0, column=2, sticky="w")
    save_button = ttk.Button(controls, text="Save score")
    save_button.grid(row=0, column=3, padx=(8, 0), sticky="w")
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
        test_status_var.set("Asking model...")
        set_output("")

        def worker() -> None:
            try:
                result = run_model_test(config_path, prompt)
            except Exception as exc:
                root.after(0, lambda: finish_error(exc))
            else:
                root.after(0, lambda: finish_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def finish_success(result: dict[str, Any]) -> None:
        latest_result.clear()
        latest_result.update(result)
        elapsed = result.get("elapsed_seconds", "-")
        model = result.get("model", "-")
        usage = result.get("usage")
        usage_text = f"\n\nusage={json.dumps(usage, ensure_ascii=False)}" if usage else ""
        set_output(str(result.get("response") or "") + usage_text)
        test_status_var.set(f"{model} replied in {elapsed}s")
        ask_button.configure(state="normal")
        save_button.configure(state="normal")

    def finish_error(exc: Exception) -> None:
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
