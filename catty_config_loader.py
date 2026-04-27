from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Any


CONFIG_FILENAME = "config.json"


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
    },
    "filter": {
        "enabled": True,
        "base_url": "",
        "api_key": "",
        "model": "",
        "extra_headers": {},
        "extra_body": {},
        "temperature": 0,
        "max_tokens": 64,
        "request_timeout": 10,
        "anger_enabled": True,
        "anger_warn_threshold": 60,
        "anger_mute_threshold": 100,
        "anger_cooldown_seconds": 3600,
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
        "image_response_enabled": True,
        "image_vision_enabled": True,
        "reply_max_chars": 1800,
        "reply_human_split_enabled": True,
        "reply_human_split_probability": 0.35,
        "reply_human_split_min_chars": 48,
        "reply_human_split_delay_seconds": 0.8,
        "expression_repeat_enabled": True,
        "expression_repeat_threshold": 3,
        "expression_repeat_window_seconds": 20,
        "expression_repeat_include_images": True,
    },
    "memory": {
        "enabled": True,
        "path": "memory.json",
        "group_storage_dir": "memory_groups",
        "user_storage_dir": "memory_users",
        "max_known_members": 20,
        "special_group_ids": [],
        "summary_interval_minutes": 30,
        "max_corpus_messages": 800,
        "private_summary_messages": 500,
        "member_mention_threshold": 20,
        "special_group_active_window_enabled": False,
        "special_group_active_minutes_per_hour": 10,
        "group_titles": {},
        "user_titles": {},
        "group_user_titles": {},
    },
    "access": {
        "allowed_user_ids": [],
        "allowed_group_ids": [],
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

    filter_config = _section(data, "filter")
    _set_env("CATTY_FILTER_ENABLED", filter_config.get("enabled"))
    _set_env("CATTY_FILTER_BASE_URL", filter_config.get("base_url"))
    _set_env("CATTY_FILTER_API_KEY", filter_config.get("api_key"))
    _set_env("CATTY_FILTER_MODEL", filter_config.get("model"))
    _set_env("CATTY_FILTER_EXTRA_HEADERS", filter_config.get("extra_headers"), json_value=True)
    _set_env("CATTY_FILTER_EXTRA_BODY", filter_config.get("extra_body"), json_value=True)
    _set_env("CATTY_FILTER_TEMPERATURE", filter_config.get("temperature"))
    _set_env("CATTY_FILTER_MAX_TOKENS", filter_config.get("max_tokens"))
    _set_env("CATTY_FILTER_REQUEST_TIMEOUT", filter_config.get("request_timeout"))
    _set_env("CATTY_FILTER_ANGER_ENABLED", filter_config.get("anger_enabled"))
    _set_env("CATTY_FILTER_ANGER_WARN_THRESHOLD", filter_config.get("anger_warn_threshold"))
    _set_env("CATTY_FILTER_ANGER_MUTE_THRESHOLD", filter_config.get("anger_mute_threshold"))
    _set_env("CATTY_FILTER_ANGER_COOLDOWN_SECONDS", filter_config.get("anger_cooldown_seconds"))

    chat = _section(data, "chat")
    _set_env("CATTY_SYSTEM_PROMPT", chat.get("system_prompt"))
    _set_env("CATTY_TRIGGER_PREFIXES", chat.get("trigger_prefixes"), json_value=True)
    _set_env("CATTY_ENABLE_PRIVATE", chat.get("enable_private"))
    _set_env("CATTY_ENABLE_GROUP", chat.get("enable_group"))
    _set_env("CATTY_PRIVATE_REQUIRE_PREFIX", chat.get("private_require_prefix"))
    _set_env("CATTY_GROUP_REQUIRE_MENTION_OR_PREFIX", chat.get("group_require_mention_or_prefix"))
    _set_env("CATTY_GROUP_HISTORY_SCOPE", chat.get("group_history_scope"))
    _set_env("CATTY_HISTORY_TURNS", chat.get("history_turns"))
    _set_env("CATTY_DIRECTED_KEYWORDS", chat.get("directed_keywords"), json_value=True)
    _set_env("CATTY_IMAGE_RESPONSE_ENABLED", chat.get("image_response_enabled"))
    _set_env("CATTY_IMAGE_VISION_ENABLED", chat.get("image_vision_enabled"))
    _set_env("CATTY_REPLY_MAX_CHARS", chat.get("reply_max_chars"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_ENABLED", chat.get("reply_human_split_enabled"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_PROBABILITY", chat.get("reply_human_split_probability"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_MIN_CHARS", chat.get("reply_human_split_min_chars"))
    _set_env("CATTY_REPLY_HUMAN_SPLIT_DELAY_SECONDS", chat.get("reply_human_split_delay_seconds"))
    _set_env("CATTY_EXPRESSION_REPEAT_ENABLED", chat.get("expression_repeat_enabled"))
    _set_env("CATTY_EXPRESSION_REPEAT_THRESHOLD", chat.get("expression_repeat_threshold"))
    _set_env("CATTY_EXPRESSION_REPEAT_WINDOW_SECONDS", chat.get("expression_repeat_window_seconds"))
    _set_env("CATTY_EXPRESSION_REPEAT_INCLUDE_IMAGES", chat.get("expression_repeat_include_images"))

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
    _set_env("CATTY_MEMORY_SUMMARY_INTERVAL_MINUTES", memory.get("summary_interval_minutes"))
    _set_env("CATTY_MEMORY_MAX_CORPUS_MESSAGES", memory.get("max_corpus_messages"))
    _set_env("CATTY_MEMORY_PRIVATE_SUMMARY_MESSAGES", memory.get("private_summary_messages"))
    _set_env("CATTY_MEMORY_MEMBER_MENTION_THRESHOLD", memory.get("member_mention_threshold"))
    _set_env("CATTY_SPECIAL_GROUP_ACTIVE_WINDOW_ENABLED", memory.get("special_group_active_window_enabled"))
    _set_env("CATTY_SPECIAL_GROUP_ACTIVE_MINUTES_PER_HOUR", memory.get("special_group_active_minutes_per_hour"))
    _set_env("CATTY_GROUP_TITLES", memory.get("group_titles"), json_value=True)
    _set_env("CATTY_USER_TITLES", memory.get("user_titles"), json_value=True)
    _set_env("CATTY_GROUP_USER_TITLES", memory.get("group_user_titles"), json_value=True)

    access = _section(data, "access")
    _set_env("CATTY_ALLOWED_USER_IDS", access.get("allowed_user_ids"), json_value=True)
    _set_env("CATTY_ALLOWED_GROUP_IDS", access.get("allowed_group_ids"), json_value=True)


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
