from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping


class TaskError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TaskIdentity:
    user_id: str
    scope: str
    persona: str = "fadianji"
    is_owner: bool = False

    def __post_init__(self):
        for value in (self.user_id, self.scope, self.persona):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError("task identity fields must be nonempty bounded strings")
            if any(ord(character) < 32 for character in value):
                raise ValueError("task identity cannot contain control characters")
        if not self.scope.startswith(("group:", "private:")):
            raise ValueError("task scope must identify a group or private conversation")
        if type(self.is_owner) is not bool:
            raise ValueError("is_owner must be a verified boolean")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def ownership_key(self) -> str:
        encoded = json.dumps([self.user_id, self.scope, self.persona], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TaskTier:
    name: str
    free_per_day: int
    points: int
    max_tokens: int
    timeout_seconds: int
    max_steps: int


DEFAULT_TIERS = {
    "light": TaskTier("light", 3, 20, 32_000, 180, 4),
    "standard": TaskTier("standard", 1, 80, 128_000, 300, 8),
    "deep": TaskTier("deep", 0, 200, 256_000, 600, 12),
}
TASK_ROLES = frozenset({"research", "document", "analysis", "artifact"})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "expired"})


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    enabled: bool = True
    data_dir: str = "data/agent_tasks"
    timezone: str = "Asia/Shanghai"
    daily_token_limit: int = 600_000
    daily_attempt_limit: int = 20
    per_user_active: int = 1
    global_concurrency: int = 4
    worker_parallel: int = 2
    max_agents: int = 3
    max_depth: int = 1
    quote_ttl_seconds: int = 300
    worker_model: str = "deepseek-v4.1-flash"
    max_input_tokens: int = 64_000
    max_output_tokens: int = 4096
    summary_max_chars: int = 1500
    main_dispatch_enabled: bool = True
    main_input_target: int = 22_000
    main_input_limit: int = 0
    main_recent_messages: int = 8
    tiers: Mapping[str, TaskTier] = field(default_factory=lambda: dict(DEFAULT_TIERS))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None = None) -> TaskPolicy:
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError("agent_tasks must be an object")
        values = {key: value for key, value in raw.items() if key in {item.name for item in fields(cls)} and key != "tiers"}
        for key in ("enabled", "main_dispatch_enabled"):
            if key in values and type(values[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        limits = {
            "daily_token_limit": (1, 100_000_000),
            "daily_attempt_limit": (1, 10_000),
            "per_user_active": (1, 100),
            "global_concurrency": (1, 100),
            "worker_parallel": (1, 10),
            "max_agents": (1, 10),
            "max_depth": (1, 1),
            "quote_ttl_seconds": (1, 86_400),
            "max_input_tokens": (256, 128_000),
            "max_output_tokens": (64, 32_000),
            "summary_max_chars": (128, 10_000),
            "main_input_target": (1024, 2_000_000),
            "main_input_limit": (0, 2_000_000),
            "main_recent_messages": (0, 32),
        }
        for key, bounds in limits.items():
            if key in values:
                _integer(values[key], key, *bounds)
        for key in ("data_dir", "timezone", "worker_model"):
            if key in values and (not isinstance(values[key], str) or not values[key].strip()):
                raise ValueError(f"{key} must be a nonempty string")
        if "worker_model" in values and not values["worker_model"].lower().startswith("deepseek-"):
            raise ValueError("task workers must use a DeepSeek model")
        raw_tiers = raw.get("tiers", {})
        if not isinstance(raw_tiers, Mapping) or set(raw_tiers) - set(DEFAULT_TIERS):
            raise ValueError("unknown task tier")
        tiers = {}
        for name, default in DEFAULT_TIERS.items():
            changes = raw_tiers.get(name, {})
            if not isinstance(changes, Mapping) or set(changes) - (set(asdict(default)) - {"name"}):
                raise ValueError(f"invalid tier configuration: {name}")
            tier_values = {**asdict(default), **changes}
            for key, minimum, maximum in (
                ("free_per_day", 0, 10_000), ("points", 0, 1_000_000),
                ("max_tokens", 256, 100_000_000), ("timeout_seconds", 1, 3600),
                ("max_steps", 1, 100),
            ):
                _integer(tier_values[key], f"tiers.{name}.{key}", minimum, maximum)
            tiers[name] = TaskTier(**tier_values)
        policy = cls(**values, tiers=tiers)
        if policy.main_input_limit and policy.main_input_target > policy.main_input_limit:
            raise ValueError("main_input_target must not exceed main_input_limit")
        from zoneinfo import ZoneInfo

        ZoneInfo(policy.timezone)
        return policy

    @property
    def root(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    def tier(self, name: str) -> TaskTier:
        if name not in self.tiers:
            raise TaskError("invalid_tier", "unknown task tier")
        return self.tiers[name]


@dataclass(frozen=True, slots=True)
class TaskResult:
    summary: str
    artifacts: tuple[Mapping[str, Any], ...] = ()
    sources: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def task_policy(config: Any) -> TaskPolicy:
    raw = config if isinstance(config, Mapping) else getattr(config, "catty_agent_tasks", {})
    return TaskPolicy.from_mapping(raw)