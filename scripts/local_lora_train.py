from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx


SKIPPED_EXIT_CODE = 2


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    return data


def _dataset_count(path: Path) -> int:
    if not path.is_file():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                count += 1
    return count


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _backend_command(training: dict[str, Any], *, task: str, mode: str) -> str:
    task_prefix = "assistant_" if task == "assistant_reply" else ""
    if mode == "busy":
        return _first_text(
            training.get(f"{task_prefix}busy_backend_command"),
            training.get("busy_backend_command"),
            training.get(f"{task_prefix}backend_command"),
            training.get("backend_command"),
        )
    return _first_text(
        training.get(f"{task_prefix}backend_command"),
        training.get("backend_command"),
    )


def _max_steps(training: dict[str, Any], *, mode: str) -> int:
    if mode == "busy":
        return int(training.get("busy_training_max_steps") or 20)
    return int(training.get("idle_training_max_steps") or 200)


def _artifact_candidates(output_dir: Path) -> list[Path]:
    names = [
        "adapter_model.safetensors",
        "adapter_config.json",
        "adapter_model.bin",
        "Modelfile",
    ]
    candidates = [output_dir / name for name in names]
    candidates.extend(output_dir.glob("*.gguf"))
    candidates.extend(output_dir.glob("*.safetensors"))
    return [path for path in candidates if path.exists()]


def _artifact_summary(output_dir: Path) -> dict[str, Any]:
    artifacts = _artifact_candidates(output_dir)
    if not artifacts:
        return {"has_artifact": False, "files": []}
    return {
        "has_artifact": True,
        "files": [str(path) for path in artifacts],
        "primary": str(artifacts[0]),
        "has_modelfile": (output_dir / "Modelfile").exists(),
        "has_gguf": any(path.suffix.lower() == ".gguf" for path in artifacts),
        "has_lora_adapter": any(path.name in {"adapter_model.safetensors", "adapter_model.bin"} for path in artifacts),
    }


def _command_for(training: dict[str, Any], *, task: str, key: str) -> str:
    task_prefix = "assistant_" if task == "assistant_reply" else ""
    return _first_text(training.get(f"{task_prefix}{key}"), training.get(key))


def _format_backend_command(
    command: str,
    *,
    dataset_path: Path,
    output_dir: Path,
    config_path: Path,
    task: str,
    mode: str,
    max_steps: int,
    artifact: str = "",
) -> str:
    return command.format(
        dataset=str(dataset_path),
        output_dir=str(output_dir),
        config=str(config_path),
        python=__import__("sys").executable,
        scripts_dir=str(config_path.parent / "scripts"),
        task=task,
        mode=mode,
        max_steps=max_steps,
        artifact=artifact,
    )


def _json_object(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            loaded = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    return value if isinstance(value, dict) else {}


def _route_text(config: dict[str, Any], training: dict[str, Any], key: str, *sections: str) -> str:
    values: list[object] = [training.get(f"artifact_audit_{key}")]
    values.extend(_section(config, section).get(key) for section in sections)
    return _first_text(*values)


def _route_number(config: dict[str, Any], training: dict[str, Any], key: str, default: float, *sections: str) -> float:
    for value in [training.get(f"artifact_audit_{key}"), *(_section(config, section).get(key) for section in sections)]:
        if value is not None and value != "":
            return float(value)
    return default


def _route_dict(config: dict[str, Any], key: str, *sections: str) -> dict[str, Any]:
    for section in sections:
        value = _section(config, section).get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _audit_model_config(config: dict[str, Any], training: dict[str, Any]) -> dict[str, Any]:
    route = str(training.get("artifact_audit_route") or "audit_ai").strip() or "audit_ai"
    sections = ("ai", "audit_ai") if route == "ai" else ("audit_ai", "ai")
    return {
        "route": route,
        "base_url": _route_text(config, training, "base_url", *sections),
        "api_key": _route_text(config, training, "api_key", *sections),
        "model": _route_text(config, training, "model", *sections),
        "temperature": _route_number(config, training, "temperature", 0.1, *sections),
        "max_tokens": int(_route_number(config, training, "max_tokens", 320, *sections)),
        "timeout": _route_number(config, training, "timeout_seconds", 0, *sections)
        or _route_number(config, training, "request_timeout", 60, *sections),
        "extra_headers": _route_dict(config, "extra_headers", *sections),
        "extra_body": _route_dict(config, "extra_body", *sections),
    }


def _audit_training_artifact(
    config: dict[str, Any],
    training: dict[str, Any],
    *,
    task: str,
    mode: str,
    dataset_path: Path,
    output_dir: Path,
    sample_count: int,
    max_steps: int,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    if not _as_bool(training.get("artifact_audit_enabled"), default=True):
        return {"status": "disabled", "allow_apply": True, "allow_merge": True}
    model_config = _audit_model_config(config, training)
    if not model_config["base_url"] or not model_config["model"]:
        return {
            "status": "unavailable",
            "allow_apply": False,
            "allow_merge": False,
            "reason": "artifact audit model is not configured",
        }

    merge_threshold = int(
        training.get("assistant_merge_min_samples" if task == "assistant_reply" else "merge_min_samples")
        or training.get("merge_min_samples")
        or 1000
    )
    payload = {
        "task": task,
        "mode": mode,
        "dataset": str(dataset_path),
        "output_dir": str(output_dir),
        "sample_count": sample_count,
        "max_steps": max_steps,
        "merge_threshold": merge_threshold,
        "artifact": artifact,
        "permissions": {
            "can_approve_apply": _as_bool(training.get("artifact_audit_can_approve_apply"), default=True),
            "can_approve_merge": _as_bool(training.get("artifact_audit_can_approve_merge"), default=True),
            "cannot_execute_shell": True,
        },
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Catty 本地训练成果审核官。只审核训练产物是否适合进入下一步，"
                "不要生成 shell 命令，不要要求删除文件，只输出 JSON。"
                "JSON 字段：allow_apply(bool), allow_merge(bool), risk_level(low|medium|high), "
                "reason(str), checks(list)。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    body = {
        "model": model_config["model"],
        "messages": messages,
        "temperature": model_config["temperature"],
        "max_tokens": model_config["max_tokens"],
    }
    body.update(model_config["extra_body"])
    headers = {"Content-Type": "application/json", **model_config["extra_headers"]}
    if model_config["api_key"]:
        headers["Authorization"] = f"Bearer {model_config['api_key']}"

    try:
        with httpx.Client(timeout=model_config["timeout"], follow_redirects=True) as client:
            response = client.post(_chat_completions_url(model_config["base_url"]), headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {
            "status": "error",
            "allow_apply": False,
            "allow_merge": False,
            "reason": f"artifact audit failed: {exc}",
        }

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = ""
    parsed = _json_object(str(content))
    if not parsed:
        return {
            "status": "invalid",
            "allow_apply": False,
            "allow_merge": False,
            "reason": "artifact audit returned non-JSON output",
            "raw": str(content)[:500],
        }

    allow_apply = bool(parsed.get("allow_apply")) and _as_bool(training.get("artifact_audit_can_approve_apply"), default=True)
    allow_merge = bool(parsed.get("allow_merge")) and _as_bool(training.get("artifact_audit_can_approve_merge"), default=True)
    return {
        "status": "approved" if allow_apply or allow_merge else "denied",
        "allow_apply": allow_apply,
        "allow_merge": allow_merge,
        "risk_level": str(parsed.get("risk_level") or "medium"),
        "reason": str(parsed.get("reason") or ""),
        "checks": parsed.get("checks") if isinstance(parsed.get("checks"), list) else [],
        "model": model_config["model"],
        "route": model_config["route"],
    }


def _run_optional_hook(
    training: dict[str, Any],
    *,
    hook_key: str,
    enabled_key: str,
    task: str,
    dataset_path: Path,
    output_dir: Path,
    config_path: Path,
    mode: str,
    max_steps: int,
    artifact: str,
    audit: dict[str, Any] | None = None,
    audit_key: str = "allow_apply",
) -> dict[str, Any]:
    if audit is not None and not bool(audit.get(audit_key)):
        return {"status": "audit_blocked", "reason": str(audit.get("reason") or "artifact audit denied")}
    if not _as_bool(training.get(enabled_key), default=True):
        return {"status": "disabled"}
    command = _command_for(training, task=task, key=hook_key)
    if not command:
        return {"status": "pending", "reason": f"no {hook_key} configured"}
    rendered = _format_backend_command(
        command,
        dataset_path=dataset_path,
        output_dir=output_dir,
        config_path=config_path,
        task=task,
        mode=mode,
        max_steps=max_steps,
        artifact=artifact,
    )
    subprocess.run(rendered, cwd=str(config_path.parent), shell=True, check=True)
    return {"status": "completed", "command": rendered}


def _merge_allowed(training: dict[str, Any], *, task: str, sample_count: int, mode: str) -> bool:
    if mode != "idle":
        return False
    if not _as_bool(training.get("merge_trained_model_enabled"), default=True):
        return False
    task_prefix = "assistant_" if task == "assistant_reply" else ""
    threshold = int(training.get(f"{task_prefix}merge_min_samples") or training.get("merge_min_samples") or 1000)
    return sample_count >= threshold


def _write_status(output_dir: Path, status: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir / f"last_{status['mode']}_status.json"
    with status_path.open("w", encoding="utf-8") as file:
        json.dump(status, file, ensure_ascii=False, indent=2)
        file.write("\n")


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = _load_config(config_path)
    training = config.get("local_training", {})
    if not isinstance(training, dict):
        training = {}

    dataset_path = Path(args.dataset).resolve()
    output_dir = Path(args.output_dir).resolve()
    sample_count = _dataset_count(dataset_path)
    max_steps = _max_steps(training, mode=args.mode)
    command = _backend_command(training, task=args.task, mode=args.mode)
    artifact = _artifact_summary(output_dir)
    status = {
        "task": args.task,
        "mode": args.mode,
        "dataset": str(dataset_path),
        "output_dir": str(output_dir),
        "sample_count": sample_count,
        "max_steps": max_steps,
        "created_at": int(time.time()),
        "artifact": artifact,
    }

    if not command:
        status.update(
            {
                "status": "skipped",
                "reason": "no backend command configured",
                "hint": (
                    "Set local_training.backend_command / assistant_backend_command, "
                    "or busy_backend_command / assistant_busy_backend_command for real LoRA training."
                ),
            }
        )
        _write_status(output_dir, status)
        print(f"{args.task}: safe wrapper skipped; no backend command configured")
        return SKIPPED_EXIT_CODE

    rendered = _format_backend_command(
        command,
        dataset_path=dataset_path,
        output_dir=output_dir,
        config_path=config_path,
        task=args.task,
        mode=args.mode,
        max_steps=max_steps,
    )
    status["status"] = "running"
    status["backend_command"] = rendered
    _write_status(output_dir, status)
    subprocess.run(rendered, cwd=str(config_path.parent), shell=True, check=True)
    artifact = _artifact_summary(output_dir)
    status["artifact"] = artifact
    status["status"] = "completed"
    if artifact.get("has_artifact"):
        primary_artifact = str(artifact.get("primary") or "")
        audit = _audit_training_artifact(
            config,
            training,
            task=args.task,
            mode=args.mode,
            dataset_path=dataset_path,
            output_dir=output_dir,
            sample_count=sample_count,
            max_steps=max_steps,
            artifact=artifact,
        )
        status["artifact_audit"] = audit
        status["apply_adapter"] = _run_optional_hook(
            training,
            hook_key="apply_trained_adapter_command",
            enabled_key="apply_trained_adapter_enabled",
            task=args.task,
            dataset_path=dataset_path,
            output_dir=output_dir,
            config_path=config_path,
            mode=args.mode,
            max_steps=max_steps,
            artifact=primary_artifact,
            audit=audit,
            audit_key="allow_apply",
        )
        if _merge_allowed(training, task=args.task, sample_count=sample_count, mode=args.mode):
            status["merge_model"] = _run_optional_hook(
                training,
                hook_key="merge_trained_model_command",
                enabled_key="merge_trained_model_enabled",
                task=args.task,
                dataset_path=dataset_path,
                output_dir=output_dir,
                config_path=config_path,
                mode=args.mode,
                max_steps=max_steps,
                artifact=primary_artifact,
                audit=audit,
                audit_key="allow_merge",
            )
        else:
            status["merge_model"] = {"status": "not_eligible"}
    else:
        status["apply_adapter"] = {"status": "not_found"}
        status["merge_model"] = {"status": "not_found"}
    _write_status(output_dir, status)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--task", choices=["reply_gate", "assistant_reply"], required=True)
    parser.add_argument("--mode", choices=["idle", "busy"], required=True)
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
