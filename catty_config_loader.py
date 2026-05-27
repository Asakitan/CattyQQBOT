from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Any


CONFIG_FILENAME = "config.json"
REPLY_GATE_TRAIN_COMMAND = (
    '"{python}" "{scripts_dir}/local_lora_train.py" --dataset "{dataset}" '
    '--output-dir "{output_dir}" --config "{config}" --task reply_gate --mode idle'
)
REPLY_GATE_BUSY_TRAIN_COMMAND = (
    '"{python}" "{scripts_dir}/local_lora_train.py" --dataset "{dataset}" '
    '--output-dir "{output_dir}" --config "{config}" --task reply_gate --mode busy'
)
ASSISTANT_TRAIN_COMMAND = (
    '"{python}" "{scripts_dir}/local_lora_train.py" --dataset "{dataset}" '
    '--output-dir "{output_dir}" --config "{config}" --task assistant_reply --mode idle'
)
ASSISTANT_BUSY_TRAIN_COMMAND = (
    '"{python}" "{scripts_dir}/local_lora_train.py" --dataset "{dataset}" '
    '--output-dir "{output_dir}" --config "{config}" --task assistant_reply --mode busy'
)


@dataclass(frozen=True)
class LoadedConfig:
    path: Path
    data: dict[str, Any]

    def __str__(self) -> str:
        return str(self.path)

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8080,
        "driver": "~fastapi",
        "log_level": "INFO",
        "command_start": ["/"],
    },
    "qq": {
        "account": "",
        "onebot_reverse_ws_url": "ws://127.0.0.1:8080/onebot/v11/",
        "napcat_webui_url": "http://127.0.0.1:6099",
        "napcat_access_token": "",
        "auto_start_napcat": True,
        "napcat_workdir": "tools/napcat-onekey/bootmain",
        "napcat_executable": "NapCatWinBootMain.exe",
        "napcat_args": [],
        "napcat_new_console": True,
        "skip_if_napcat_running": True,
        "note": "QQ 登录由 NapCatQQ 完成。本程序只记录 QQ 号和 OneBot 连接参数，不建议保存 QQ 密码。",
    },
    "ai": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "extra_headers": {},
        "extra_body": {},
        "temperature": 0.7,
        "max_tokens": 1000,
        "request_timeout": 60,
        "http_proxy": "",
    },
    "ai_fallback": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "model": "",
        "extra_headers": {},
        "extra_body": {},
        "temperature": None,
        "max_tokens": 4096,
        "request_timeout": 180,
        "cooldown_seconds": 300,
        "mc_gate_enabled": True,
        "mc_server_host": "localhost",
        "mc_server_port": 26843,
        "mc_ping_timeout_seconds": 3.0,
        "strip_system_messages": False,
    },
    "audit_ai": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "extra_headers": {},
        "extra_body": {},
        "temperature": 0.1,
        "max_tokens": 320,
        "request_timeout": 60,
    },
    "vision": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "extra_headers": {},
        "extra_body": {},
        "prompt": "请识别图片内容，提取和聊天回复相关的信息。不要评价图片是否安全，不要添加 emoji；如果有文字请尽量转写。",
        "temperature": 0.2,
        "max_tokens": 800,
        "request_timeout": 60,
        "async_enabled": True,
        "inline_max_wait_seconds": 15.0,
    },
    "filter": {
        "enabled": True,
        "base_url": "",
        "api_key": "",
        "model": "",
        "nsfw_spark_model": "",
        "nsfw_spark_base_url": "",
        "nsfw_spark_api_key": "",
        "codex_instant_model": "",
        "nsfw_fallback_model": "",
        "nsfw_softrefuse_threshold": 2,
        "extra_headers": {},
        "extra_body": {},
        "temperature": 0,
        "max_tokens": 64,
        "request_timeout": 10,
        "group_batch_messages": 200,
        "group_batch_seconds": 1200,
        "anger_enabled": True,
        "anger_warn_threshold": 60,
        "anger_mute_threshold": 100,
        "anger_cooldown_seconds": 3600,
    },
    "ollama": {
        "enabled": False,
        "auto_install": True,
        "auto_start": True,
        "auto_pull_model": True,
        "model": "qwen2.5:1.5b",
        "num_thread": None,
        "below_normal_priority": True,
        "install_dir": "tools/ollama",
        "models_dir": "models/ollama",
        "executable": "",
        "download_url": "",
        "api_url": "http://127.0.0.1:11434",
        "startup_timeout_seconds": 60,
        "pull_timeout_seconds": 1800,
        "stop_existing": True,
        "new_console": False,
    },
    "local_critic": {
        "enabled": False,
        "mode": "reply_gate_only",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "model": "qwen2.5:1.5b",
        "extra_headers": {},
        "extra_body": {"think": False},
        "temperature": None,
        "max_tokens": 16,
        "request_timeout": 4,
        "rewrite_when_score_below": 0,
        "reply_gate_enabled": True,
        "reply_gate_min_confidence": 55,
        "reply_gate_examples": 0,
        "reply_gate_max_tokens": 16,
        "reply_gate_request_timeout": 4,
        "reply_gate_user_message_chars": 120,
        "reply_gate_plain_text_chars": 60,
        "reply_gate_context_chars": 80,
        "warmup_enabled": True,
        "warmup_keep_alive": "30m",
        "warmup_interval_seconds": 300,
        "warmup_request_timeout": 60,
        "force_direct_reply": True,
        "collect_training_samples": True,
        "training_samples_path": "local_critic_samples.jsonl",
    },
    "local_training": {
        "enabled": False,
        "auto_train_on_startup": False,
        "source_samples_path": "local_critic_samples.jsonl",
        "dataset_path": "training/reply_gate_dataset.jsonl",
        "output_dir": "training/reply_gate_lora",
        "state_path": "training/reply_gate_train_state.json",
        "min_samples": 200,
        "min_new_samples": 50,
        "train_command": REPLY_GATE_TRAIN_COMMAND,
        "collect_assistant_samples": True,
        "assistant_samples_path": "training/assistant_reply_samples.jsonl",
        "assistant_dataset_path": "training/assistant_reply_dataset.jsonl",
        "assistant_output_dir": "training/assistant_reply_lora",
        "assistant_train_command": ASSISTANT_TRAIN_COMMAND,
        "auto_fill_training_commands": True,
        "backend_command": "",
        "assistant_backend_command": "",
        "busy_backend_command": "",
        "assistant_busy_backend_command": "",
        "artifact_audit_enabled": True,
        "artifact_audit_route": "audit_ai",
        "artifact_audit_base_url": "",
        "artifact_audit_api_key": "",
        "artifact_audit_model": "",
        "artifact_audit_temperature": 0.5,
        "artifact_audit_max_tokens": 640,
        "artifact_audit_timeout_seconds": 60,
        "artifact_audit_can_approve_apply": True,
        "artifact_audit_can_approve_merge": True,
        "artifact_audit_next_suggestions_enabled": True,
        "artifact_audit_suggestions_path": "training/glm_audit_suggestions.jsonl",
        "apply_trained_adapter_enabled": True,
        "apply_trained_adapter_command": "",
        "assistant_apply_trained_adapter_command": "",
        "merge_trained_model_enabled": True,
        "merge_trained_model_command": "",
        "assistant_merge_trained_model_command": "",
        "merge_min_samples": 1000,
        "assistant_merge_min_samples": 1000,
        "busy_training_max_steps": 20,
        "idle_training_max_steps": 200,
        "busy_training_enabled": True,
        "busy_train_command": REPLY_GATE_BUSY_TRAIN_COMMAND,
        "assistant_busy_train_command": ASSISTANT_BUSY_TRAIN_COMMAND,
        "busy_training_max_seconds": 600,
        "idle_training_max_seconds": 0,
        "idle_training_enabled": True,
        "idle_start_hour": 2,
        "idle_end_hour": 6,
        "idle_min_quiet_minutes": 45,
        "allow_quiet_idle": True,
        "active_check_interval_seconds": 900,
        "idle_check_interval_seconds": 3600,
        "mcp_server_enabled": True,
        "mcp_server_script": "scripts/catty_training_mcp_server.py",
        "progress_window_enabled": False,
        "progress_window_script": "scripts/catty_training_dashboard.py",
        "progress_window_poll_seconds": 5,
        "progress_log_path": "training/local_training.log",
        "model_test_max_tokens": 480,
        "model_test_request_timeout": 60,
        "model_test_thinking_max_tokens": 96,
        "model_test_thinking_timeout": 20,
        "model_test_scores_path": "training/model_eval_scores.jsonl",
        "assistant_min_samples": 200,
        "assistant_min_new_samples": 50,
        "watch_interval_seconds": 0,
    },
    "web_search": {
        "enabled": True,
        "cooldown_seconds": 600,
        "max_results": 5,
        "request_timeout": 10,
        "engines": ["google", "bing"],
    },
    "nsfw_search": {
        "enabled": True,
        "max_results": 4,
        "request_timeout": 15,
        "cooldown_seconds": 30,
        "image_send_count": 2,
        "pixiv_cookie": "",
        "pixiv_image_size": "regular",
    },
    "image_search": {
        "enabled": True,
        "saucenao_api_key": "",
        "cooldown_seconds": 60,
        "max_results": 5,
        "request_timeout": 15,
    },
    "owner_forward": {
        "enabled": False,
        "owner_qq": 0,
        "forward_private_messages": True,
        "block_ai_reply": True,
    },
    "turtle_soup": {
        "cooldown_seconds": 300,
    },
    "chat": {
        "system_prompt": "你是一个接入 QQ 的中文 AI 助手，回答要友好、简洁、可靠。",
        "trigger_prefixes": ["ai", "AI", "猫猫"],
        "enable_private": True,
        "enable_group": True,
        "private_require_prefix": False,
        "group_require_mention_or_prefix": True,
        "group_history_scope": "group",
        "history_turns": 16,
        "directed_keywords": ["你", "猫猫", "猫娘", "看看", "帮我看看", "这张图", "这个图", "图片", "图里", "评价一下", "怎么回事"],
        "keyword_replies": [],
        "soft_directed_reply_probability": 0.65,
        "direct_address_reply_probability": 0.9,
        "image_response_enabled": True,
        "image_vision_enabled": True,
        "reply_queue_max_wait_seconds": 25.0,
        "reply_max_chars": 1800,
        "reply_human_split_enabled": True,
        "reply_human_split_probability": 0.35,
        "reply_human_split_min_chars": 48,
        "reply_human_split_max_chunks": 4,
        "reply_human_split_delay_seconds": 0.8,
        "reply_mix_emoji_with_text": True,
        "reply_quote_enabled": True,
        "reply_quote_private_enabled": False,
        "reply_self_check_enabled": True,
        "reply_style_examples_enabled": True,
        "expression_repeat_enabled": True,
        "expression_repeat_threshold": 2,
        "expression_repeat_window_seconds": 20,
        "expression_repeat_include_images": True,
        "expression_repeat_include_text": True,
        "prompt_order": [],
        "prompts_disabled": [],
    },
    "social": {
        "steam": "",
    },
    "emoji": {
        "enabled": True,
        "dir": "emojis",
        "download_dir": "emojis/downloaded",
        "manifest_path": "emojis/manifest.json",
        "interest_threshold": 60,
        "save_interest_threshold": 85,
        "max_candidates": 8,
        "reply_enabled": True,
        "reply_probability": 0.85,
        "auto_fallback_enabled": False,
        "diversity_enabled": True,
        "diversity_recent_window": 8,
        "diversity_candidate_pool": 6,
    },
    "memory": {
        "enabled": True,
        "path": "memory.json",
        "group_storage_dir": "memory_groups",
        "user_storage_dir": "memory_users",
        "max_known_members": 20,
        "special_group_ids": [],
        "special_care_user_ids": [],
        "group_special_care_user_ids": {},
        "special_care_cooldown_seconds": 90,
        "special_care_response_window_minutes": 30,
        "summary_interval_minutes": 30,
        "max_corpus_messages": 800,
        "private_summary_messages": 500,
        "member_mention_threshold": 20,
        "reply_boost_enabled": True,
        "reply_boost_min_corpus_messages": 80,
        "reply_boost_probability_bonus": 0.15,
        "reply_boost_max_probability": 0.95,
        "special_group_active_window_enabled": False,
        "special_group_active_minutes_per_hour": 10,
        "group_titles": {},
        "user_titles": {},
        "group_user_titles": {},
        "game_size_compress_threshold_bytes": 200000,
    },
    "game_context": {
        "star_resonance_group_ids": [],
        "strinova_group_ids": [],
    },
    "proactive": {
        "enabled": True,
        "max_daily_per_group": 5,
        "check_interval_seconds": 300,
        "min_interval_minutes": 120,
        "response_window_minutes": 30,
        "recent_messages": 40,
        "active_window_minutes": 15,
        "active_min_messages": 5,
    },
    "access": {
        "allowed_user_ids": [],
        "allowed_group_ids": [],
    },
    "hot_reload": {
        "enabled": True,
        "poll_seconds": 1.5,
        "debounce_seconds": 1.0,
        "restart_on_code_change": True,
    },
}


def _app_root() -> Path:
    return Path(__file__).resolve().parent


def _candidate_dirs() -> list[Path]:
    dirs = [Path.cwd()]
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(_app_root())

    unique: list[Path] = []
    seen: set[Path] = set()
    for directory in dirs:
        resolved = directory.resolve()
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def _find_config_path() -> Path | None:
    for directory in _candidate_dirs():
        path = directory / CONFIG_FILENAME
        if path.is_file():
            return path
    return None


def _default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / CONFIG_FILENAME
    return Path.cwd() / CONFIG_FILENAME


def _write_default_config(path: Path) -> None:
    path.write_text(
        json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _format_json_error(path: Path, exc: json.JSONDecodeError) -> str:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    start = max(exc.lineno - 2, 1)
    end = min(exc.lineno + 2, len(lines))
    excerpt: list[str] = []
    for line_no in range(start, end + 1):
        line = lines[line_no - 1]
        excerpt.append(f"{line_no:>4}: {line}")
        if line_no == exc.lineno:
            excerpt.append(f"      {' ' * max(exc.colno - 1, 0)}^")

    return (
        f"config.json 语法错误: {path}\n"
        f"位置: 第 {exc.lineno} 行，第 {exc.colno} 列\n"
        f"原因: {exc.msg}\n"
        "提示: JSON 对象的键必须用英文双引号包住，字符串也必须用英文双引号；"
        "上一行和下一项之间要有逗号。\n"
        + "\n".join(excerpt)
    )


def _as_env_value(value: Any, *, json_value: bool = False, csv_value: bool = False) -> str:
    if json_value:
        return json.dumps(value, ensure_ascii=False)
    if csv_value and isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _set_env(name: str, value: Any, *, json_value: bool = False, csv_value: bool = False) -> None:
    if value is None:
        return
    os.environ[name] = _as_env_value(value, json_value=json_value, csv_value=csv_value)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config section {name!r} must be an object")
    return value


def _apply_config(data: dict[str, Any], base_dir: Path) -> None:
    server = _section(data, "server")
    _set_env("HOST", server.get("host"))
    _set_env("PORT", server.get("port"))
    _set_env("DRIVER", server.get("driver"))
    _set_env("LOG_LEVEL", server.get("log_level"))
    _set_env("COMMAND_START", server.get("command_start"), json_value=True)

    qq = _section(data, "qq")
    _set_env("CATTY_QQ_ACCOUNT", qq.get("account"))
    _set_env("CATTY_ONEBOT_REVERSE_WS_URL", qq.get("onebot_reverse_ws_url"))
    _set_env("CATTY_NAPCAT_WEBUI_URL", qq.get("napcat_webui_url"))
    _set_env("CATTY_NAPCAT_ACCESS_TOKEN", qq.get("napcat_access_token"))
    _set_env("ONEBOT_ACCESS_TOKEN", qq.get("napcat_access_token"))
    _set_env("ONEBOT_V11_ACCESS_TOKEN", qq.get("napcat_access_token"))

    ai = _section(data, "ai")
    _set_env("CATTY_OPENAI_BASE_URL", ai.get("base_url"))
    _set_env("CATTY_OPENAI_API_KEY", ai.get("api_key"))
    _set_env("CATTY_OPENAI_MODEL", ai.get("model"))
    _set_env("CATTY_OPENAI_EXTRA_HEADERS", ai.get("extra_headers"), json_value=True)
    _set_env("CATTY_OPENAI_EXTRA_BODY", ai.get("extra_body"), json_value=True)
    _set_env("CATTY_TEMPERATURE", ai.get("temperature"))
    _set_env("CATTY_MAX_TOKENS", ai.get("max_tokens"))
    _set_env("CATTY_REQUEST_TIMEOUT", ai.get("request_timeout"))
    _set_env("CATTY_HTTP_PROXY", ai.get("http_proxy"))
    # 主人 2026-05-28: Anthropic 原生 /v1/messages + server-side compaction
    _set_env("CATTY_ANTHROPIC_NATIVE_ENABLED", ai.get("anthropic_native_enabled"))
    _set_env("CATTY_COMPACTION_ENABLED", ai.get("compaction_enabled"))
    _set_env("CATTY_COMPACTION_TRIGGER_TOKENS", ai.get("compaction_trigger_tokens"))

    ai_fallback = _section(data, "ai_fallback")
    _set_env("CATTY_AI_FALLBACK_ENABLED", ai_fallback.get("enabled"))
    _set_env("CATTY_AI_FALLBACK_BASE_URL", ai_fallback.get("base_url"))
    _set_env("CATTY_AI_FALLBACK_API_KEY", ai_fallback.get("api_key"))
    _set_env("CATTY_AI_FALLBACK_MODEL", ai_fallback.get("model"))
    _set_env("CATTY_AI_FALLBACK_EXTRA_HEADERS", ai_fallback.get("extra_headers"), json_value=True)
    _set_env("CATTY_AI_FALLBACK_EXTRA_BODY", ai_fallback.get("extra_body"), json_value=True)
    _set_env("CATTY_AI_FALLBACK_TEMPERATURE", ai_fallback.get("temperature"))
    _set_env("CATTY_AI_FALLBACK_MAX_TOKENS", ai_fallback.get("max_tokens"))
    _set_env("CATTY_AI_FALLBACK_REQUEST_TIMEOUT", ai_fallback.get("request_timeout"))
    _set_env("CATTY_AI_FALLBACK_COOLDOWN_SECONDS", ai_fallback.get("cooldown_seconds"))
    _set_env("CATTY_AI_FALLBACK_MC_GATE_ENABLED", ai_fallback.get("mc_gate_enabled"))
    _set_env("CATTY_AI_FALLBACK_MC_SERVER_HOST", ai_fallback.get("mc_server_host"))
    _set_env("CATTY_AI_FALLBACK_MC_SERVER_PORT", ai_fallback.get("mc_server_port"))
    _set_env("CATTY_AI_FALLBACK_MC_PING_TIMEOUT_SECONDS", ai_fallback.get("mc_ping_timeout_seconds"))
    _set_env("CATTY_AI_FALLBACK_STRIP_SYSTEM_MESSAGES", ai_fallback.get("strip_system_messages"))

    audit_ai = _section(data, "audit_ai")
    _set_env("CATTY_AUDIT_AI_BASE_URL", audit_ai.get("base_url"))
    _set_env("CATTY_AUDIT_AI_API_KEY", audit_ai.get("api_key"))
    _set_env("CATTY_AUDIT_AI_MODEL", audit_ai.get("model"))
    _set_env("CATTY_AUDIT_AI_EXTRA_HEADERS", audit_ai.get("extra_headers"), json_value=True)
    _set_env("CATTY_AUDIT_AI_EXTRA_BODY", audit_ai.get("extra_body"), json_value=True)
    _set_env("CATTY_AUDIT_AI_TEMPERATURE", audit_ai.get("temperature"))
    _set_env("CATTY_AUDIT_AI_MAX_TOKENS", audit_ai.get("max_tokens"))
    _set_env("CATTY_AUDIT_AI_REQUEST_TIMEOUT", audit_ai.get("request_timeout"))

    imagegen = _section(data, "imagegen")
    _set_env("CATTY_IMAGEGEN_ENABLED", imagegen.get("enabled"))
    _set_env("CATTY_IMAGEGEN_BASE_URL", imagegen.get("base_url"))
    _set_env("CATTY_IMAGEGEN_API_KEY", imagegen.get("api_key"))
    _set_env("CATTY_IMAGEGEN_MODEL", imagegen.get("model"))
    _set_env("CATTY_IMAGEGEN_DEFAULT_SIZE", imagegen.get("default_size"))
    _set_env("CATTY_IMAGEGEN_DEFAULT_QUALITY", imagegen.get("default_quality"))
    _set_env("CATTY_IMAGEGEN_DEFAULT_FORMAT", imagegen.get("default_format"))
    _set_env("CATTY_IMAGEGEN_COOLDOWN_SECONDS", imagegen.get("cooldown_seconds"))
    _set_env("CATTY_IMAGEGEN_TIMEOUT_SECONDS", imagegen.get("timeout_seconds"))
    _set_env("CATTY_IMAGEGEN_MAX_CHARS", imagegen.get("max_chars"))
    _set_env("CATTY_IMAGEGEN_CACHE_DIR", imagegen.get("cache_dir"))
    _set_env("CATTY_IMAGEGEN_CACHE_MAX_FILES", imagegen.get("cache_max_files"))
    _set_env("CATTY_IMAGEGEN_FORCE_HTTP_SCHEME", imagegen.get("force_http_scheme"))
    nai_raw = imagegen.get("nai")
    nai = nai_raw if isinstance(nai_raw, dict) else {}
    _set_env("CATTY_IMAGEGEN_NAI_ENABLED", nai.get("enabled"))
    _set_env("CATTY_IMAGEGEN_NAI_TOKEN", nai.get("token"))
    _set_env("CATTY_IMAGEGEN_NAI_MODEL", nai.get("model"))
    _set_env("CATTY_IMAGEGEN_NAI_DEFAULT_ASPECT", nai.get("default_aspect"))
    _set_env("CATTY_IMAGEGEN_NAI_STEPS", nai.get("steps"))
    _set_env("CATTY_IMAGEGEN_NAI_SCALE", nai.get("scale"))
    _set_env("CATTY_IMAGEGEN_NAI_SAMPLER", nai.get("sampler"))
    _set_env("CATTY_IMAGEGEN_NAI_NOISE_SCHEDULE", nai.get("noise_schedule"))
    _set_env("CATTY_IMAGEGEN_NAI_DEFAULT_NEGATIVE", nai.get("default_negative"))
    _set_env("CATTY_IMAGEGEN_NAI_TIMEOUT_SECONDS", nai.get("timeout_seconds"))
    _set_env("CATTY_IMAGEGEN_NAI_BASE_POINTS", nai.get("base_points"))
    _set_env("CATTY_IMAGEGEN_NAI_POINTS_PER_ANLAS", nai.get("points_per_anlas"))

    vision = _section(data, "vision")
    _set_env("CATTY_VISION_BASE_URL", vision.get("base_url"))
    _set_env("CATTY_VISION_API_KEY", vision.get("api_key"))
    _set_env("CATTY_VISION_MODEL", vision.get("model"))
    _set_env("CATTY_VISION_EXTRA_HEADERS", vision.get("extra_headers"), json_value=True)
    _set_env("CATTY_VISION_EXTRA_BODY", vision.get("extra_body"), json_value=True)
    _set_env("CATTY_VISION_PROMPT", vision.get("prompt"))
    _set_env("CATTY_VISION_TEMPERATURE", vision.get("temperature"))
    _set_env("CATTY_VISION_MAX_TOKENS", vision.get("max_tokens"))
    _set_env("CATTY_VISION_REQUEST_TIMEOUT", vision.get("request_timeout"))
    _set_env("CATTY_VISION_ASYNC_ENABLED", vision.get("async_enabled"))
    _set_env("CATTY_VISION_INLINE_MAX_WAIT_SECONDS", vision.get("inline_max_wait_seconds"))

    filter_config = _section(data, "filter")
    _set_env("CATTY_FILTER_ENABLED", filter_config.get("enabled"))
    _set_env("CATTY_FILTER_BASE_URL", filter_config.get("base_url"))
    _set_env("CATTY_FILTER_API_KEY", filter_config.get("api_key"))
    _set_env("CATTY_FILTER_MODEL", filter_config.get("model"))
    # NSFW deep 路径专用 model (默认 gpt-5.3-codex, 跟 filter 同 base/key 但 model 单独配)
    _set_env("CATTY_NSFW_SPARK_MODEL", filter_config.get("nsfw_spark_model"))
    # NSFW spark 独立 endpoint (主人 2026-05-27 选项 2): 空时 fallback 到 filter base/key
    _set_env("CATTY_NSFW_SPARK_BASE_URL", filter_config.get("nsfw_spark_base_url"))
    _set_env("CATTY_NSFW_SPARK_API_KEY", filter_config.get("nsfw_spark_api_key"))
    # placeholder / 签到 caption 等短回复路径专用 model (主人原话: 不要走 spark, 走 5.3-codex).
    # 空时 fallback 到 nsfw_spark_model 或 filter_model.
    _set_env("CATTY_CODEX_INSTANT_MODEL", filter_config.get("codex_instant_model"))
    # NSFW fallback model (主 model 累计软拒达 threshold 时自动切)
    _set_env("CATTY_NSFW_FALLBACK_MODEL", filter_config.get("nsfw_fallback_model"))
    _set_env("CATTY_NSFW_SOFTREFUSE_THRESHOLD", filter_config.get("nsfw_softrefuse_threshold"))
    _set_env("CATTY_FILTER_EXTRA_HEADERS", filter_config.get("extra_headers"), json_value=True)
    _set_env("CATTY_FILTER_EXTRA_BODY", filter_config.get("extra_body"), json_value=True)
    _set_env("CATTY_FILTER_TEMPERATURE", filter_config.get("temperature"))
    _set_env("CATTY_FILTER_MAX_TOKENS", filter_config.get("max_tokens"))
    _set_env("CATTY_FILTER_REQUEST_TIMEOUT", filter_config.get("request_timeout"))
    _set_env("CATTY_FILTER_GROUP_BATCH_MESSAGES", filter_config.get("group_batch_messages"))
    _set_env("CATTY_FILTER_GROUP_BATCH_SECONDS", filter_config.get("group_batch_seconds"))
    _set_env("CATTY_FILTER_ANGER_ENABLED", filter_config.get("anger_enabled"))
    _set_env("CATTY_FILTER_ANGER_WARN_THRESHOLD", filter_config.get("anger_warn_threshold"))
    _set_env("CATTY_FILTER_ANGER_MUTE_THRESHOLD", filter_config.get("anger_mute_threshold"))
    _set_env("CATTY_FILTER_ANGER_COOLDOWN_SECONDS", filter_config.get("anger_cooldown_seconds"))

    # ── Local NLU enrichment (jieba + text2vec + HanLP) ───────────────
    # 主人 2026-05-28: 加 encoder-only 本地 NLU 增强层. config.json 可以放顶层
    # "catty_use_jieba": true 或 nlu section. 两种写法都支持.
    nlu = data.get("nlu", {}) if isinstance(data.get("nlu"), dict) else {}
    def _nlu(key: str, default: Any = None) -> Any:
        # 优先 nlu section, 没有就读 top-level
        if key in nlu:
            return nlu.get(key)
        # top-level fallback (主人最爱直接顶层 toggle)
        flat_key = f"catty_{key}" if not key.startswith("catty_") else key
        return data.get(flat_key, default)
    _set_env("CATTY_USE_JIEBA", _nlu("use_jieba"))
    _set_env("CATTY_USE_TEXT2VEC", _nlu("use_text2vec"))
    _set_env("CATTY_USE_HANLP", _nlu("use_hanlp"))
    _set_env("CATTY_TEXT2VEC_USE_ONNX", _nlu("text2vec_use_onnx"))
    _set_env("CATTY_TEXT2VEC_MODEL_NAME", _nlu("text2vec_model_name"))
    _set_env("CATTY_HANLP_PIPELINE", _nlu("hanlp_pipeline"))
    _set_env("CATTY_TEXT2VEC_TOPIC_THRESHOLD", _nlu("text2vec_topic_threshold"))
    _set_env("CATTY_TEXT2VEC_EMOTION_THRESHOLD", _nlu("text2vec_emotion_threshold"))
    _set_env("CATTY_TEXT2VEC_TREND_THRESHOLD", _nlu("text2vec_trend_threshold"))
    _set_env("CATTY_NLU_CACHE_DIR", _nlu("nlu_cache_dir"))
    _set_env("CATTY_NLU_WARMUP_ON_STARTUP", _nlu("nlu_warmup_on_startup"))
    _set_env("CATTY_NLU_HF_ENDPOINT", _nlu("nlu_hf_endpoint"))

    local_critic = _section(data, "local_critic")
    local_critic_extra_body = local_critic.get("extra_body")
    if not isinstance(local_critic_extra_body, dict):
        local_critic_extra_body = {}
    merged_local_critic_extra_body = {
        "think": False,
        **local_critic_extra_body,
    }
    _set_env("CATTY_LOCAL_CRITIC_ENABLED", local_critic.get("enabled"))
    _set_env("CATTY_LOCAL_CRITIC_MODE", local_critic.get("mode") or "reply_gate_only")
    _set_env("CATTY_LOCAL_CRITIC_BASE_URL", local_critic.get("base_url"))
    _set_env("CATTY_LOCAL_CRITIC_API_KEY", local_critic.get("api_key"))
    _set_env("CATTY_LOCAL_CRITIC_MODEL", local_critic.get("model"))
    _set_env("CATTY_LOCAL_CRITIC_EXTRA_HEADERS", local_critic.get("extra_headers"), json_value=True)
    _set_env("CATTY_LOCAL_CRITIC_EXTRA_BODY", merged_local_critic_extra_body, json_value=True)
    _set_env("CATTY_LOCAL_CRITIC_TEMPERATURE", local_critic.get("temperature"))
    _set_env("CATTY_LOCAL_CRITIC_MAX_TOKENS", local_critic.get("max_tokens"))
    _set_env("CATTY_LOCAL_CRITIC_REQUEST_TIMEOUT", local_critic.get("request_timeout"))
    _set_env("CATTY_LOCAL_CRITIC_REWRITE_WHEN_SCORE_BELOW", local_critic.get("rewrite_when_score_below"))
    _set_env("CATTY_LOCAL_CRITIC_REPLY_GATE_ENABLED", local_critic.get("reply_gate_enabled"))
    _set_env("CATTY_LOCAL_CRITIC_REPLY_GATE_MIN_CONFIDENCE", local_critic.get("reply_gate_min_confidence"))
    _set_env("CATTY_LOCAL_CRITIC_REPLY_GATE_EXAMPLES", local_critic.get("reply_gate_examples"))
    _set_env("CATTY_LOCAL_CRITIC_REPLY_GATE_MAX_TOKENS", local_critic.get("reply_gate_max_tokens"))
    _set_env("CATTY_LOCAL_CRITIC_REPLY_GATE_REQUEST_TIMEOUT", local_critic.get("reply_gate_request_timeout"))
    _set_env("CATTY_LOCAL_CRITIC_REPLY_GATE_USER_MESSAGE_CHARS", local_critic.get("reply_gate_user_message_chars"))
    _set_env("CATTY_LOCAL_CRITIC_REPLY_GATE_PLAIN_TEXT_CHARS", local_critic.get("reply_gate_plain_text_chars"))
    _set_env("CATTY_LOCAL_CRITIC_REPLY_GATE_CONTEXT_CHARS", local_critic.get("reply_gate_context_chars"))
    _set_env("CATTY_LOCAL_CRITIC_WARMUP_ENABLED", local_critic.get("warmup_enabled"))
    _set_env("CATTY_LOCAL_CRITIC_WARMUP_KEEP_ALIVE", local_critic.get("warmup_keep_alive"))
    _set_env("CATTY_LOCAL_CRITIC_WARMUP_INTERVAL_SECONDS", local_critic.get("warmup_interval_seconds"))
    _set_env("CATTY_LOCAL_CRITIC_WARMUP_REQUEST_TIMEOUT", local_critic.get("warmup_request_timeout"))
    _set_env("CATTY_LOCAL_CRITIC_FORCE_DIRECT_REPLY", local_critic.get("force_direct_reply"))
    _set_env("CATTY_LOCAL_CRITIC_COLLECT_TRAINING_SAMPLES", local_critic.get("collect_training_samples"))
    training_samples_path = local_critic.get("training_samples_path")
    if training_samples_path:
        resolved_training_samples_path = Path(str(training_samples_path)).expanduser()
        if not resolved_training_samples_path.is_absolute():
            resolved_training_samples_path = base_dir / resolved_training_samples_path
        _set_env("CATTY_LOCAL_CRITIC_TRAINING_SAMPLES_PATH", resolved_training_samples_path)

    local_training = _section(data, "local_training")
    _set_env("CATTY_LOCAL_TRAINING_COLLECT_ASSISTANT_SAMPLES", local_training.get("collect_assistant_samples"))
    assistant_samples_path = local_training.get("assistant_samples_path")
    if assistant_samples_path:
        resolved_assistant_samples_path = Path(str(assistant_samples_path)).expanduser()
        if not resolved_assistant_samples_path.is_absolute():
            resolved_assistant_samples_path = base_dir / resolved_assistant_samples_path
        _set_env("CATTY_LOCAL_TRAINING_ASSISTANT_SAMPLES_PATH", resolved_assistant_samples_path)

    web_search = _section(data, "web_search")
    _set_env("CATTY_WEB_SEARCH_ENABLED", web_search.get("enabled"))
    _set_env("CATTY_WEB_SEARCH_COOLDOWN_SECONDS", web_search.get("cooldown_seconds"))
    _set_env("CATTY_WEB_SEARCH_MAX_RESULTS", web_search.get("max_results"))
    _set_env("CATTY_WEB_SEARCH_REQUEST_TIMEOUT", web_search.get("request_timeout"))
    _set_env("CATTY_WEB_SEARCH_ENGINES", web_search.get("engines"), json_value=True)

    nsfw_search = _section(data, "nsfw_search")
    _set_env("CATTY_NSFW_SEARCH_ENABLED", nsfw_search.get("enabled"))
    _set_env("CATTY_NSFW_SEARCH_MAX_RESULTS", nsfw_search.get("max_results"))
    _set_env("CATTY_NSFW_SEARCH_REQUEST_TIMEOUT", nsfw_search.get("request_timeout"))
    _set_env("CATTY_NSFW_SEARCH_COOLDOWN_SECONDS", nsfw_search.get("cooldown_seconds"))
    _set_env("CATTY_NSFW_IMAGE_SEND_COUNT", nsfw_search.get("image_send_count"))
    _set_env("CATTY_NSFW_PIXIV_COOKIE", nsfw_search.get("pixiv_cookie"))
    _set_env("CATTY_NSFW_PIXIV_IMAGE_SIZE", nsfw_search.get("pixiv_image_size"))

    image_search = _section(data, "image_search")
    _set_env("CATTY_IMAGE_SEARCH_ENABLED", image_search.get("enabled"))
    _set_env("CATTY_IMAGE_SEARCH_SAUCENAO_API_KEY", image_search.get("saucenao_api_key"))
    _set_env("CATTY_IMAGE_SEARCH_COOLDOWN_SECONDS", image_search.get("cooldown_seconds"))
    _set_env("CATTY_IMAGE_SEARCH_MAX_RESULTS", image_search.get("max_results"))
    _set_env("CATTY_IMAGE_SEARCH_REQUEST_TIMEOUT", image_search.get("request_timeout"))

    owner_forward = _section(data, "owner_forward")
    _set_env("CATTY_OWNER_FORWARD_ENABLED", owner_forward.get("enabled"))
    _set_env("CATTY_OWNER_QQ", owner_forward.get("owner_qq"))
    _set_env("CATTY_OWNER_FORWARD_PRIVATE_MESSAGES", owner_forward.get("forward_private_messages"))
    _set_env("CATTY_OWNER_FORWARD_BLOCK_AI_REPLY", owner_forward.get("block_ai_reply"))

    turtle_soup = _section(data, "turtle_soup")
    _set_env("CATTY_TURTLE_SOUP_COOLDOWN_SECONDS", turtle_soup.get("cooldown_seconds"))

    chat = _section(data, "chat")
    _set_env("CATTY_SYSTEM_PROMPT", chat.get("system_prompt"))
    _set_env("CATTY_TRIGGER_PREFIXES", chat.get("trigger_prefixes"), json_value=True)
    _set_env("CATTY_ENABLE_PRIVATE", chat.get("enable_private"))
    _set_env("CATTY_ENABLE_GROUP", chat.get("enable_group"))
    _set_env("CATTY_PRIVATE_REQUIRE_PREFIX", chat.get("private_require_prefix"))
    _set_env("CATTY_GROUP_REQUIRE_MENTION_OR_PREFIX", chat.get("group_require_mention_or_prefix"))
    _set_env("CATTY_GROUP_HISTORY_SCOPE", chat.get("group_history_scope"))
    _set_env("CATTY_HISTORY_TURNS", chat.get("history_turns"))
    _set_env("CATTY_CPU_AFFINITY_MASK", chat.get("cpu_affinity_mask"))
    _set_env("CATTY_DIRECTED_KEYWORDS", chat.get("directed_keywords"), json_value=True)
    _set_env("CATTY_IGNORED_USER_IDS", chat.get("ignored_user_ids"), json_value=True)
    _set_env("CATTY_IGNORE_MARKED_BOTS", chat.get("ignore_marked_bots"))
    _set_env("CATTY_IGNORE_BOT_SELF_INTRO_ENABLED", chat.get("ignore_bot_self_intro_enabled"))
    _set_env("CATTY_KEYWORD_REPLIES", chat.get("keyword_replies"), json_value=True)
    _set_env("CATTY_SOFT_DIRECTED_REPLY_PROBABILITY", chat.get("soft_directed_reply_probability"))
    _set_env("CATTY_DIRECT_ADDRESS_REPLY_PROBABILITY", chat.get("direct_address_reply_probability"))
    _set_env("CATTY_IMAGE_RESPONSE_ENABLED", chat.get("image_response_enabled"))
    _set_env("CATTY_IMAGE_VISION_ENABLED", chat.get("image_vision_enabled"))
    _set_env("CATTY_REPLY_QUEUE_MAX_WAIT_SECONDS", chat.get("reply_queue_max_wait_seconds"))
    social = _section(data, "social")
    _set_env("CATTY_SOCIAL_STEAM", social.get("steam"))
    _set_env("CATTY_REPLY_MAX_CHARS", chat.get("reply_max_chars"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_ENABLED", chat.get("reply_human_split_enabled"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_PROBABILITY", chat.get("reply_human_split_probability"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_MIN_CHARS", chat.get("reply_human_split_min_chars"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_MAX_CHUNKS", chat.get("reply_human_split_max_chunks"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_DELAY_SECONDS", chat.get("reply_human_split_delay_seconds"))
    _set_env("CATTY_REPLY_MIX_EMOJI_WITH_TEXT", chat.get("reply_mix_emoji_with_text"))
    _set_env("CATTY_REPLY_QUOTE_ENABLED", chat.get("reply_quote_enabled"))
    _set_env("CATTY_REPLY_QUOTE_PRIVATE_ENABLED", chat.get("reply_quote_private_enabled"))
    _set_env("CATTY_REPLY_SELF_CHECK_ENABLED", chat.get("reply_self_check_enabled"))
    _set_env("CATTY_REPLY_STYLE_EXAMPLES_ENABLED", chat.get("reply_style_examples_enabled"))
    _set_env("CATTY_EXPRESSION_REPEAT_ENABLED", chat.get("expression_repeat_enabled"))
    _set_env("CATTY_EXPRESSION_REPEAT_THRESHOLD", chat.get("expression_repeat_threshold"))
    _set_env("CATTY_EXPRESSION_REPEAT_WINDOW_SECONDS", chat.get("expression_repeat_window_seconds"))
    _set_env("CATTY_EXPRESSION_REPEAT_INCLUDE_IMAGES", chat.get("expression_repeat_include_images"))
    _set_env("CATTY_EXPRESSION_REPEAT_INCLUDE_TEXT", chat.get("expression_repeat_include_text"))
    # SillyTavern 风 PromptManager: 让主人通过 config.json 调 ST 风段顺序/单独关
    _set_env("CATTY_PROMPT_ORDER", chat.get("prompt_order"), json_value=True)
    _set_env("CATTY_PROMPTS_DISABLED", chat.get("prompts_disabled"), json_value=True)

    emoji = _section(data, "emoji")
    _set_env("CATTY_EMOJI_ENABLED", emoji.get("enabled"))
    emoji_dir = emoji.get("dir")
    if emoji_dir:
        resolved_emoji_dir = Path(str(emoji_dir)).expanduser()
        if not resolved_emoji_dir.is_absolute():
            resolved_emoji_dir = base_dir / resolved_emoji_dir
        _set_env("CATTY_EMOJI_DIR", resolved_emoji_dir)
    emoji_download_dir = emoji.get("download_dir")
    if emoji_download_dir:
        resolved_emoji_download_dir = Path(str(emoji_download_dir)).expanduser()
        if not resolved_emoji_download_dir.is_absolute():
            resolved_emoji_download_dir = base_dir / resolved_emoji_download_dir
        _set_env("CATTY_EMOJI_DOWNLOAD_DIR", resolved_emoji_download_dir)
    emoji_manifest_path = emoji.get("manifest_path")
    if emoji_manifest_path:
        resolved_emoji_manifest_path = Path(str(emoji_manifest_path)).expanduser()
        if not resolved_emoji_manifest_path.is_absolute():
            resolved_emoji_manifest_path = base_dir / resolved_emoji_manifest_path
        _set_env("CATTY_EMOJI_MANIFEST_PATH", resolved_emoji_manifest_path)
    _set_env("CATTY_EMOJI_INTEREST_THRESHOLD", emoji.get("interest_threshold"))
    _set_env("CATTY_EMOJI_SAVE_INTEREST_THRESHOLD", emoji.get("save_interest_threshold"))
    _set_env("CATTY_EMOJI_MAX_CANDIDATES", emoji.get("max_candidates"))
    _set_env("CATTY_EMOJI_REPLY_ENABLED", emoji.get("reply_enabled"))
    _set_env("CATTY_EMOJI_REPLY_PROBABILITY", emoji.get("reply_probability"))
    _set_env("CATTY_EMOJI_AUTO_FALLBACK_ENABLED", emoji.get("auto_fallback_enabled"))
    _set_env("CATTY_EMOJI_DIVERSITY_ENABLED", emoji.get("diversity_enabled"))
    _set_env("CATTY_EMOJI_DIVERSITY_RECENT_WINDOW", emoji.get("diversity_recent_window"))
    _set_env("CATTY_EMOJI_DIVERSITY_CANDIDATE_POOL", emoji.get("diversity_candidate_pool"))

    memory = _section(data, "memory")
    memory_path = memory.get("path")
    if memory_path:
        resolved_memory_path = Path(str(memory_path)).expanduser()
        if not resolved_memory_path.is_absolute():
            resolved_memory_path = base_dir / resolved_memory_path
        _set_env("CATTY_MEMORY_PATH", resolved_memory_path)
    group_storage_dir = memory.get("group_storage_dir")
    if group_storage_dir:
        resolved_group_storage_dir = Path(str(group_storage_dir)).expanduser()
        if not resolved_group_storage_dir.is_absolute():
            resolved_group_storage_dir = base_dir / resolved_group_storage_dir
        _set_env("CATTY_MEMORY_GROUP_STORAGE_DIR", resolved_group_storage_dir)
    user_storage_dir = memory.get("user_storage_dir")
    if user_storage_dir:
        resolved_user_storage_dir = Path(str(user_storage_dir)).expanduser()
        if not resolved_user_storage_dir.is_absolute():
            resolved_user_storage_dir = base_dir / resolved_user_storage_dir
        _set_env("CATTY_MEMORY_USER_STORAGE_DIR", resolved_user_storage_dir)
    _set_env("CATTY_MEMORY_ENABLED", memory.get("enabled"))
    _set_env("CATTY_MEMORY_MAX_KNOWN_MEMBERS", memory.get("max_known_members"))
    _set_env("CATTY_MEMORY_SPECIAL_GROUP_IDS", memory.get("special_group_ids"), json_value=True)
    _set_env("CATTY_SPECIAL_CARE_USER_IDS", memory.get("special_care_user_ids"), json_value=True)
    _set_env("CATTY_GROUP_SPECIAL_CARE_USER_IDS", memory.get("group_special_care_user_ids"), json_value=True)
    _set_env("CATTY_SPECIAL_CARE_COOLDOWN_SECONDS", memory.get("special_care_cooldown_seconds"))
    _set_env("CATTY_SPECIAL_CARE_RESPONSE_WINDOW_MINUTES", memory.get("special_care_response_window_minutes"))
    _set_env("CATTY_MEMORY_SUMMARY_INTERVAL_MINUTES", memory.get("summary_interval_minutes"))
    _set_env("CATTY_MEMORY_MAX_CORPUS_MESSAGES", memory.get("max_corpus_messages"))
    _set_env("CATTY_MEMORY_PRIVATE_SUMMARY_MESSAGES", memory.get("private_summary_messages"))
    _set_env("CATTY_MEMORY_MEMBER_MENTION_THRESHOLD", memory.get("member_mention_threshold"))
    _set_env("CATTY_MEMORY_REPLY_BOOST_ENABLED", memory.get("reply_boost_enabled"))
    _set_env("CATTY_MEMORY_REPLY_BOOST_MIN_CORPUS_MESSAGES", memory.get("reply_boost_min_corpus_messages"))
    _set_env("CATTY_MEMORY_REPLY_BOOST_PROBABILITY_BONUS", memory.get("reply_boost_probability_bonus"))
    _set_env("CATTY_MEMORY_REPLY_BOOST_MAX_PROBABILITY", memory.get("reply_boost_max_probability"))
    _set_env("CATTY_SPECIAL_GROUP_ACTIVE_WINDOW_ENABLED", memory.get("special_group_active_window_enabled"))
    _set_env("CATTY_SPECIAL_GROUP_ACTIVE_MINUTES_PER_HOUR", memory.get("special_group_active_minutes_per_hour"))
    _set_env("CATTY_GROUP_TITLES", memory.get("group_titles"), json_value=True)
    _set_env("CATTY_USER_TITLES", memory.get("user_titles"), json_value=True)
    _set_env("CATTY_GROUP_USER_TITLES", memory.get("group_user_titles"), json_value=True)
    _set_env("CATTY_MEMORY_GAME_SIZE_COMPRESS_THRESHOLD_BYTES", memory.get("game_size_compress_threshold_bytes"))

    game_context = _section(data, "game_context")
    _set_env("CATTY_GAME_CONTEXT_STAR_RESONANCE_GROUP_IDS", game_context.get("star_resonance_group_ids"), json_value=True)
    _set_env("CATTY_GAME_CONTEXT_STRINOVA_GROUP_IDS", game_context.get("strinova_group_ids"), json_value=True)

    proactive = _section(data, "proactive")
    _set_env("CATTY_PROACTIVE_ENABLED", proactive.get("enabled"))
    _set_env("CATTY_PROACTIVE_MAX_DAILY_PER_GROUP", proactive.get("max_daily_per_group"))
    _set_env("CATTY_PROACTIVE_CHECK_INTERVAL_SECONDS", proactive.get("check_interval_seconds"))
    _set_env("CATTY_PROACTIVE_MIN_INTERVAL_MINUTES", proactive.get("min_interval_minutes"))
    _set_env("CATTY_PROACTIVE_RESPONSE_WINDOW_MINUTES", proactive.get("response_window_minutes"))
    _set_env("CATTY_PROACTIVE_RECENT_MESSAGES", proactive.get("recent_messages"))
    _set_env("CATTY_PROACTIVE_ACTIVE_WINDOW_MINUTES", proactive.get("active_window_minutes"))
    _set_env("CATTY_PROACTIVE_ACTIVE_MIN_MESSAGES", proactive.get("active_min_messages"))

    access = _section(data, "access")
    _set_env("CATTY_ALLOWED_USER_IDS", access.get("allowed_user_ids"), json_value=True)
    _set_env("CATTY_ALLOWED_GROUP_IDS", access.get("allowed_group_ids"), json_value=True)

    hot_reload = _section(data, "hot_reload")
    _set_env("CATTY_HOT_RELOAD_ENABLED", hot_reload.get("enabled"))
    _set_env("CATTY_HOT_RELOAD_POLL_SECONDS", hot_reload.get("poll_seconds"))
    _set_env("CATTY_HOT_RELOAD_DEBOUNCE_SECONDS", hot_reload.get("debounce_seconds"))
    _set_env("CATTY_HOT_RELOAD_RESTART_ON_CODE_CHANGE", hot_reload.get("restart_on_code_change"))


def load_config_to_env() -> LoadedConfig | None:
    path = _find_config_path()
    if path is None:
        path = _default_config_path()
        try:
            _write_default_config(path)
        except OSError:
            return None

    try:
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise SystemExit(_format_json_error(path, exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("config.json root must be an object")

    _apply_config(data, path.parent)
    return LoadedConfig(path=path, data=data)
