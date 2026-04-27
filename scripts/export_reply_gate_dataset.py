from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    return data


def _resolve(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


def _training_message(record: dict[str, Any]) -> dict[str, Any] | None:
    critic = record.get("critic")
    if not isinstance(critic, dict):
        return None
    gate = critic.get("reply_gate")
    if not isinstance(gate, dict):
        return None
    event = record.get("event")
    if not isinstance(event, dict):
        return None

    target = {
        "should_reply": bool(gate.get("should_reply")),
        "confidence": int(gate.get("confidence") or 0),
        "reason": str(gate.get("reason") or ""),
        "training_tags": gate.get("training_tags") if isinstance(gate.get("training_tags"), list) else [],
    }
    prompt_payload = {
        "message_type": event.get("message_type"),
        "user_message": event.get("user_message"),
        "plain_text": event.get("plain_text"),
        "has_image": event.get("has_image"),
        "mentioned": event.get("mentioned"),
        "replied_to_self": event.get("replied_to_self"),
        "used_prefix": event.get("used_prefix"),
        "directed": event.get("directed"),
        "directed_strength": event.get("directed_strength"),
        "directly_requested": event.get("directly_requested"),
        "opportunistic": event.get("opportunistic"),
    }
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 QQ 猫娘机器人笨猫的本地 reply gate。"
                    "只判断本轮是否应该交给主 AI 回复，并只输出 JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)},
        ]
    }


def _assistant_training_message(record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("kind") != "assistant_reply":
        return None
    messages = record.get("messages")
    if not isinstance(messages, list):
        return None
    final_reply = str(record.get("final_reply") or "").strip()
    if not final_reply:
        return None

    exported_messages: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role not in {"system", "user", "assistant"}:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        exported_messages.append({"role": role, "content": content})
    if not exported_messages:
        return None
    exported_messages.append({"role": "assistant", "content": final_reply})
    return {"messages": exported_messages}


def _export_jsonl(source_path: Path, dataset_path: Path, converter) -> tuple[int, Path]:
    dataset_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with dataset_path.open("w", encoding="utf-8") as output:
        if source_path.is_file():
            with source_path.open("r", encoding="utf-8") as input_file:
                for line in input_file:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    message = converter(record)
                    if message is None:
                        continue
                    output.write(json.dumps(message, ensure_ascii=False) + "\n")
                    count += 1
    return count, dataset_path


def export_reply_gate_dataset(config_path: Path) -> tuple[int, Path]:
    config = _load_config(config_path)
    base_dir = config_path.parent
    training = config.get("local_training", {})
    if not isinstance(training, dict):
        training = {}
    local_critic = config.get("local_critic", {})
    if not isinstance(local_critic, dict):
        local_critic = {}

    source_path = _resolve(
        base_dir,
        str(training.get("source_samples_path") or local_critic.get("training_samples_path") or "local_critic_samples.jsonl"),
    )
    dataset_path = _resolve(base_dir, str(training.get("dataset_path") or "training/reply_gate_dataset.jsonl"))
    return _export_jsonl(source_path, dataset_path, _training_message)


def export_assistant_reply_dataset(config_path: Path) -> tuple[int, Path]:
    config = _load_config(config_path)
    base_dir = config_path.parent
    training = config.get("local_training", {})
    if not isinstance(training, dict):
        training = {}

    source_path = _resolve(
        base_dir,
        str(training.get("assistant_samples_path") or "training/assistant_reply_samples.jsonl"),
    )
    dataset_path = _resolve(
        base_dir,
        str(training.get("assistant_dataset_path") or "training/assistant_reply_dataset.jsonl"),
    )
    return _export_jsonl(source_path, dataset_path, _assistant_training_message)


def export_all_datasets(config_path: Path) -> dict[str, tuple[int, Path]]:
    return {
        "reply_gate": export_reply_gate_dataset(config_path),
        "assistant_reply": export_assistant_reply_dataset(config_path),
    }


def export_dataset(config_path: Path) -> tuple[int, Path]:
    return export_reply_gate_dataset(config_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    count, dataset_path = export_reply_gate_dataset(Path(args.config).resolve())
    print(f"exported {count} reply gate samples to {dataset_path}")
    assistant_count, assistant_dataset_path = export_assistant_reply_dataset(Path(args.config).resolve())
    print(f"exported {assistant_count} assistant reply samples to {assistant_dataset_path}")


if __name__ == "__main__":
    main()
