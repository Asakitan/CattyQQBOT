"""Independent, fail-closed configuration for the source repair worker."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


HARD_WEEKLY_LIMIT = 100_000_000
STATE_RELATIVE_PATH = "data/self_repair"
DEFAULT_TEST_MODULES = (
    "test_config", "test_config_loader", "test_hot_reload", "test_harness_interface",
    "test_harness_v2", "test_harness_prompt", "test_reply_markers", "test_tool_dispatcher",
)
DEFAULT_ALLOWED_PATHS = (
    "src/catty_qq_ai", "bot.py", "catty_config_loader.py", "catty_integrations.py",
)


class ConfigError(ValueError):
    """Invalid repair settings; the caller must not keep an old enabled config."""


@dataclass(frozen=True)
class RepairConfig:
    enabled: bool = False
    auto_apply: bool = True
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    model: str = ""
    http_proxy: str = field(default="", repr=False)
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)
    extra_body: dict[str, Any] = field(default_factory=dict)
    weekly_token_budget: int = HARD_WEEKLY_LIMIT
    max_input_tokens: int = 65_536
    max_output_tokens: int = 8_192
    max_rounds: int = 16
    request_timeout_seconds: float = 120.0
    run_timeout_seconds: float = 900.0
    poll_seconds: float = 15.0
    debounce_seconds: float = 30.0
    cooldown_seconds: float = 21_600.0
    log_paths: tuple[str, ...] = ("logs/bot_live.log", "logs/hot_reload.log")
    initial_tail_bytes: int = 65_536
    max_log_bytes: int = 262_144
    max_incident_chars: int = 16_000
    max_incidents_per_run: int = 3
    allowed_paths: tuple[str, ...] = DEFAULT_ALLOWED_PATHS
    max_files: int = 6
    max_patch_bytes: int = 200_000
    test_modules: tuple[str, ...] = DEFAULT_TEST_MODULES
    test_timeout_seconds: float = 120.0
    validation_timeout_seconds: float = 600.0
    health_grace_seconds: float = 60.0
    health_timeout_seconds: float = 180.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RepairConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ConfigError("self_repair must be an object")
        known = {item.name for item in fields(cls)}
        unknown = set(raw) - known - {"_note"}
        if unknown:
            raise ConfigError("unknown self_repair fields: " + ", ".join(sorted(unknown)))
        values: dict[str, Any] = {key: value for key, value in raw.items() if key in known}
        for name in ("enabled", "auto_apply"):
            if name in values and not isinstance(values[name], bool):
                raise ConfigError(f"self_repair.{name} must be boolean")
        for name in ("base_url", "api_key", "model", "http_proxy"):
            if name in values:
                if not isinstance(values[name], str):
                    raise ConfigError(f"self_repair.{name} must be a string")
                values[name] = values[name].strip()
        for name in ("log_paths", "allowed_paths", "test_modules"):
            if name in values:
                value = values[name]
                if not isinstance(value, (list, tuple)) or not value or not all(
                    isinstance(item, str) and item.strip() for item in value
                ):
                    raise ConfigError(f"self_repair.{name} must be a nonempty string list")
                values[name] = tuple(dict.fromkeys(item.strip() for item in value))
        integer_bounds = {
            "weekly_token_budget": (0, HARD_WEEKLY_LIMIT),
            "max_input_tokens": (256, 2_000_000), "max_output_tokens": (64, 131_072),
            "max_rounds": (1, 64), "initial_tail_bytes": (0, 4_194_304),
            "max_log_bytes": (1_024, 4_194_304), "max_incident_chars": (256, 65_536),
            "max_incidents_per_run": (1, 20), "max_files": (1, 20),
            "max_patch_bytes": (1_024, 2_000_000),
        }
        for name, (low, high) in integer_bounds.items():
            if name in values and (
                type(values[name]) is not int or not low <= values[name] <= high
            ):
                raise ConfigError(f"self_repair.{name} must be an integer in {low}..{high}")
        for name in (
            "request_timeout_seconds", "run_timeout_seconds", "poll_seconds",
            "debounce_seconds", "cooldown_seconds", "test_timeout_seconds",
            "validation_timeout_seconds", "health_grace_seconds", "health_timeout_seconds",
        ):
            if name in values:
                value = values[name]
                minimum = 0.0 if name in {"debounce_seconds", "cooldown_seconds", "health_grace_seconds"} else 0.1
                if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 604_800:
                    raise ConfigError(f"self_repair.{name} has an invalid duration")
                values[name] = float(value)
        for name in ("extra_headers", "extra_body"):
            if name in values:
                if not isinstance(values[name], dict):
                    raise ConfigError(f"self_repair.{name} must be an object")
                values[name] = json.loads(json.dumps(values[name], allow_nan=False))
        result = cls(**values)
        result.validate()
        return result

    def validate(self) -> None:
        if self.base_url and self.base_url not in {"HOST", "https://HOST/v1"}:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ConfigError("self_repair.base_url must be an HTTP(S) API URL without credentials or query")
            if parsed.path.rstrip("/").endswith("/messages"):
                raise ConfigError("self_repair requires a Chat Completions or Responses endpoint")
        if self.http_proxy:
            parsed_proxy = urlsplit(self.http_proxy)
            if parsed_proxy.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed_proxy.hostname or parsed_proxy.query or parsed_proxy.fragment:
                raise ConfigError("self_repair.http_proxy must be a valid HTTP(S) or SOCKS5 URL")
        if self.health_timeout_seconds <= self.health_grace_seconds:
            raise ConfigError("health_timeout_seconds must exceed health_grace_seconds")
        for relative in self.log_paths + self.allowed_paths:
            path = PurePosixPath(relative)
            if path.is_absolute() or "\\" in relative or ":" in relative or any(
                item in {"", ".", ".."} or item.endswith((" ", ".")) for item in relative.split("/")
            ):
                raise ConfigError("self_repair paths must be plain root-relative forward-slash paths")
        if any(not path.startswith("logs/") for path in self.log_paths):
            raise ConfigError("self_repair.log_paths must stay under logs/")
        for path in self.allowed_paths:
            if not any(path == allowed or path.startswith(allowed + "/") for allowed in DEFAULT_ALLOWED_PATHS):
                raise ConfigError("self_repair.allowed_paths can only narrow the Catty source roots")
        if any(not re.fullmatch(r"test_[a-zA-Z0-9_]+", name) for name in self.test_modules):
            raise ConfigError("self_repair.test_modules must contain unittest module names")
        if set(self.extra_body) - {"temperature", "top_p", "reasoning_effort", "thinking"}:
            raise ConfigError("self_repair.extra_body cannot override routing, tools or token limits")
        for name, value in self.extra_headers.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]+", name) or not isinstance(value, str) or "\n" in value or "\r" in value:
                raise ConfigError("invalid self_repair.extra_headers")
            if name.lower() in {"host", "content-length", "transfer-encoding"}:
                raise ConfigError("self_repair.extra_headers cannot override transport framing")

    @property
    def configured(self) -> bool:
        return bool(
            self.base_url and "HOST" not in self.base_url and self.model and self.model != "TARGET"
            and self.api_key != "TOKEN"
        )

    def fingerprint(self) -> str:
        # Hash the entire route/config, including credentials, without persisting them.
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("api_key")
        result.pop("http_proxy")
        result.pop("extra_headers")
        return result


DEFAULT_SELF_REPAIR = json.loads(json.dumps(asdict(RepairConfig())))


def load_config(root: Path) -> RepairConfig:
    path = root / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return RepairConfig()
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read valid config.json; self repair is paused") from exc
    if not isinstance(data, dict):
        raise ConfigError("config.json must be an object")
    config = RepairConfig.from_mapping(data.get("self_repair"))
    if not config.enabled or config.model not in {"", "TARGET"}:
        return config
    main = data.get("ai")
    if not isinstance(main, Mapping):
        return config
    route: dict[str, str] = {}
    for target, source in (("base_url", "base_url"), ("api_key", "api_key"),
                           ("model", "model"), ("http_proxy", "http_proxy")):
        value = main.get(source, "")
        if not isinstance(value, str):
            raise ConfigError(f"ai.{source} must be a string for self repair")
        route[target] = value.strip()
    resolved = replace(config, **route)
    resolved.validate()
    return resolved


def state_dir(root: Path) -> Path:
    return root / STATE_RELATIVE_PATH


def load_secret_values(root: Path) -> tuple[str, ...]:
    """Known credential values used only to redact outbound evidence, never prompts."""
    data = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    secrets: set[str] = set()

    def collect(value: Any, sensitive: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                collect(child, sensitive or bool(re.search(r"api.?key|token|secret|password|cookie|authorization|extra_headers", str(key), re.I)))
        elif isinstance(value, list):
            for child in value:
                collect(child, sensitive)
        elif sensitive and isinstance(value, str) and len(value.strip()) >= 4:
            secrets.add(value)

    collect(data)
    return tuple(sorted(secrets, key=len, reverse=True))