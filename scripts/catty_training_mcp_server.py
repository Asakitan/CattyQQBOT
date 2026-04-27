from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


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


def _status_file(output_dir: Path, mode: str) -> dict[str, Any]:
    return _load_json(output_dir / f"last_{mode}_status.json")


def build_server(config_path: Path) -> FastMCP:
    config_path = config_path.resolve()
    base_dir = config_path.parent
    app = FastMCP("catty-training")

    @app.tool()
    def training_status() -> dict[str, Any]:
        config = _load_json(config_path)
        training = config.get("local_training", {})
        if not isinstance(training, dict):
            training = {}
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
        return {
            "config": str(config_path),
            "datasets": {
                "reply_gate": {"path": str(reply_dataset), "samples": _count_jsonl(reply_dataset)},
                "assistant_reply": {"path": str(assistant_dataset), "samples": _count_jsonl(assistant_dataset)},
            },
            "latest_status": {
                "reply_gate_idle": _status_file(reply_output, "idle"),
                "reply_gate_busy": _status_file(reply_output, "busy"),
                "assistant_reply_idle": _status_file(assistant_output, "idle"),
                "assistant_reply_busy": _status_file(assistant_output, "busy"),
            },
        }

    @app.tool()
    def training_config_summary() -> dict[str, Any]:
        config = _load_json(config_path)
        training = config.get("local_training", {})
        ai = config.get("ai", {})
        audit_ai = config.get("audit_ai", {})
        local_critic = config.get("local_critic", {})
        ollama = config.get("ollama", {})
        if not isinstance(training, dict):
            training = {}
        if not isinstance(audit_ai, dict):
            audit_ai = {}
        return {
            "enabled": bool(training.get("enabled")),
            "auto_train_on_startup": bool(training.get("auto_train_on_startup")),
            "watch_interval_seconds": training.get("watch_interval_seconds"),
            "artifact_audit_enabled": bool(training.get("artifact_audit_enabled", True)),
            "artifact_audit_route": training.get("artifact_audit_route") or "audit_ai",
            "artifact_audit_model": training.get("artifact_audit_model")
            or audit_ai.get("model")
            or (ai.get("model") if isinstance(ai, dict) else ""),
            "ollama_model": ollama.get("model") if isinstance(ollama, dict) else "",
            "local_critic_model": local_critic.get("model") if isinstance(local_critic, dict) else "",
            "has_backend_command": bool(training.get("backend_command") or training.get("assistant_backend_command")),
            "has_busy_backend_command": bool(
                training.get("busy_backend_command") or training.get("assistant_busy_backend_command")
            ),
            "has_apply_hook": bool(
                training.get("apply_trained_adapter_command")
                or training.get("assistant_apply_trained_adapter_command")
            ),
            "has_merge_hook": bool(
                training.get("merge_trained_model_command")
                or training.get("assistant_merge_trained_model_command")
            ),
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    build_server(Path(args.config)).run()


if __name__ == "__main__":
    main()
