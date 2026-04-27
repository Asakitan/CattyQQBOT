from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


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
    root.geometry("920x640")
    root.minsize(760, 520)

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

    def replace_text(widget: ScrolledText, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

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
