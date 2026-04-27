import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _split_text(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，;；\n]+", value) if item.strip()]


def _parse_json_object(value: str) -> dict[str, Any]:
    raw = value.strip()
    if not raw:
        return {}
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError("value must be a JSON object")
    return loaded


class Config(BaseModel):
    catty_openai_base_url: str = "https://api.openai.com/v1"
    catty_openai_api_key: str = ""
    catty_openai_model: str = "gpt-4o-mini"
    catty_openai_extra_headers: dict[str, str] = Field(default_factory=dict)
    catty_openai_extra_body: dict[str, Any] = Field(default_factory=dict)

    catty_vision_base_url: str = ""
    catty_vision_api_key: str = ""
    catty_vision_model: str = ""
    catty_vision_extra_headers: dict[str, str] = Field(default_factory=dict)
    catty_vision_extra_body: dict[str, Any] = Field(default_factory=dict)
    catty_vision_prompt: str = "请识别图片内容，提取和聊天回复相关的信息。不要评价图片是否安全，不要添加 emoji；如果有文字请尽量转写。"
    catty_vision_temperature: float | None = 0.2
    catty_vision_max_tokens: int | None = 800
    catty_vision_request_timeout: float | None = None

    catty_filter_enabled: bool = True
    catty_filter_base_url: str = ""
    catty_filter_api_key: str = ""
    catty_filter_model: str = ""
    catty_filter_extra_headers: dict[str, str] = Field(default_factory=dict)
    catty_filter_extra_body: dict[str, Any] = Field(default_factory=dict)
    catty_filter_temperature: float | None = 0.0
    catty_filter_max_tokens: int | None = 64
    catty_filter_request_timeout: float | None = 10.0
    catty_filter_group_batch_messages: int = 200
    catty_filter_group_batch_seconds: float = 1200.0
    catty_filter_anger_enabled: bool = True
    catty_filter_anger_warn_threshold: int = 60
    catty_filter_anger_mute_threshold: int = 100
    catty_filter_anger_cooldown_seconds: int = 3600
    catty_web_search_enabled: bool = True
    catty_web_search_cooldown_seconds: int = 600
    catty_web_search_max_results: int = 5
    catty_web_search_request_timeout: float | None = 10.0
    catty_turtle_soup_cooldown_seconds: int = 300

    catty_system_prompt: str = "你是一个接入 QQ 的中文 AI 助手，回答要友好、简洁、可靠。"
    catty_trigger_prefixes: list[str] = Field(default_factory=lambda: ["ai", "AI", "猫猫"])
    catty_enable_private: bool = True
    catty_enable_group: bool = True
    catty_private_require_prefix: bool = False
    catty_group_require_mention_or_prefix: bool = True
    catty_group_history_scope: str = "group"
    catty_history_turns: int = 16
    catty_directed_keywords: list[str] = Field(
        default_factory=lambda: ["你", "猫猫", "猫娘", "看看", "帮我看看", "这张图", "这个图", "图片", "图里", "评价一下", "怎么回事"]
    )
    catty_image_response_enabled: bool = True
    catty_image_vision_enabled: bool = True
    catty_emoji_enabled: bool = True
    catty_emoji_dir: str = "emojis"
    catty_emoji_download_dir: str = "emojis/downloaded"
    catty_emoji_manifest_path: str = "emojis/manifest.json"
    catty_emoji_interest_threshold: int = 60
    catty_emoji_save_interest_threshold: int = 85
    catty_emoji_max_candidates: int = 8
    catty_emoji_reply_enabled: bool = True
    catty_emoji_reply_probability: float = 0.85
    catty_memory_enabled: bool = True
    catty_memory_path: str = "memory.json"
    catty_memory_group_storage_dir: str = ""
    catty_memory_user_storage_dir: str = ""
    catty_memory_max_known_members: int = 20
    catty_memory_special_group_ids: set[int] = Field(default_factory=set)
    catty_special_care_user_ids: set[int] = Field(default_factory=set)
    catty_group_special_care_user_ids: dict[str, set[int]] = Field(default_factory=dict)
    catty_special_care_cooldown_seconds: int = 90
    catty_special_care_response_window_minutes: float = 30.0
    catty_memory_summary_interval_minutes: int = 30
    catty_memory_max_corpus_messages: int = 800
    catty_memory_private_summary_messages: int = 500
    catty_memory_member_mention_threshold: int = 20
    catty_special_group_active_window_enabled: bool = False
    catty_special_group_active_minutes_per_hour: int = 10
    catty_group_titles: dict[str, str] = Field(default_factory=dict)
    catty_user_titles: dict[str, str] = Field(default_factory=dict)
    catty_group_user_titles: dict[str, dict[str, str]] = Field(default_factory=dict)

    catty_proactive_enabled: bool = True
    catty_proactive_max_daily_per_group: int = 5
    catty_proactive_check_interval_seconds: float = 300.0
    catty_proactive_min_interval_minutes: float = 120.0
    catty_proactive_response_window_minutes: float = 30.0
    catty_proactive_recent_messages: int = 40

    catty_temperature: float | None = 0.7
    catty_max_tokens: int | None = 1000
    catty_request_timeout: float = 60.0
    catty_reply_max_chars: int = 1800
    catty_reply_human_split_enabled: bool = True
    catty_reply_human_split_probability: float = 0.35
    catty_reply_human_split_min_chars: int = 48
    catty_reply_human_split_delay_seconds: float = 0.8
    catty_reply_self_check_enabled: bool = True
    catty_reply_style_examples_enabled: bool = True
    catty_expression_repeat_enabled: bool = True
    catty_expression_repeat_threshold: int = 2
    catty_expression_repeat_window_seconds: float = 20.0
    catty_expression_repeat_include_images: bool = True
    catty_expression_repeat_include_text: bool = True

    catty_allowed_user_ids: set[int] = Field(default_factory=set)
    catty_allowed_group_ids: set[int] = Field(default_factory=set)
    catty_http_proxy: str = ""

    @field_validator("catty_openai_base_url", "catty_vision_base_url", "catty_filter_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("catty_group_history_scope")
    @classmethod
    def validate_group_history_scope(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"group", "user"}:
            raise ValueError("catty_group_history_scope must be group or user")
        return normalized

    @field_validator("catty_trigger_prefixes", "catty_directed_keywords", mode="before")
    @classmethod
    def parse_prefixes(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                loaded = json.loads(raw)
                if not isinstance(loaded, list):
                    raise ValueError("catty_trigger_prefixes JSON value must be a list")
                return [str(item).strip() for item in loaded if str(item).strip()]
            return _split_text(raw)
        return value

    @field_validator(
        "catty_allowed_user_ids",
        "catty_allowed_group_ids",
        "catty_memory_special_group_ids",
        "catty_special_care_user_ids",
        mode="before",
    )
    @classmethod
    def parse_int_set(cls, value: Any) -> Any:
        if value is None or value == "":
            return set()
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return set()
            if raw.startswith("["):
                loaded = json.loads(raw)
                if not isinstance(loaded, list):
                    raise ValueError("value must be a JSON list")
                return {int(item) for item in loaded}
            return {int(item) for item in _split_text(raw)}
        if isinstance(value, (list, tuple, set)):
            return {int(item) for item in value}
        return value

    @field_validator("catty_openai_extra_headers", "catty_vision_extra_headers", "catty_filter_extra_headers", mode="before")
    @classmethod
    def parse_extra_headers(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            data = _parse_json_object(value)
        else:
            data = value
        return {str(key): str(val) for key, val in dict(data).items()}

    @field_validator("catty_openai_extra_body", "catty_vision_extra_body", "catty_filter_extra_body", mode="before")
    @classmethod
    def parse_extra_body(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            return _parse_json_object(value)
        return value

    @field_validator("catty_group_titles", "catty_user_titles", mode="before")
    @classmethod
    def parse_title_map(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            return {str(key): str(val) for key, val in _parse_json_object(value).items()}
        return {str(key): str(val) for key, val in dict(value).items()}

    @field_validator("catty_group_user_titles", mode="before")
    @classmethod
    def parse_group_user_titles(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        data = _parse_json_object(value) if isinstance(value, str) else dict(value)
        parsed: dict[str, dict[str, str]] = {}
        for group_id, members in data.items():
            parsed[str(group_id)] = {str(user_id): str(title) for user_id, title in dict(members).items()}
        return parsed

    @field_validator("catty_group_special_care_user_ids", mode="before")
    @classmethod
    def parse_group_special_care_user_ids(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        data = _parse_json_object(value) if isinstance(value, str) else dict(value)
        parsed: dict[str, set[int]] = {}
        for group_id, members in data.items():
            if members is None or members == "":
                parsed[str(group_id)] = set()
            elif isinstance(members, str):
                parsed[str(group_id)] = {int(item) for item in _split_text(members)}
            elif isinstance(members, (list, tuple, set)):
                parsed[str(group_id)] = {int(item) for item in members}
            else:
                raise ValueError("catty_group_special_care_user_ids values must be lists or comma-separated strings")
        return parsed

    @field_validator(
        "catty_temperature",
        "catty_max_tokens",
        "catty_vision_temperature",
        "catty_vision_max_tokens",
        "catty_vision_request_timeout",
        "catty_filter_temperature",
        "catty_filter_max_tokens",
        "catty_filter_request_timeout",
        "catty_filter_group_batch_messages",
        "catty_filter_group_batch_seconds",
        "catty_filter_anger_warn_threshold",
        "catty_filter_anger_mute_threshold",
        "catty_filter_anger_cooldown_seconds",
        "catty_web_search_cooldown_seconds",
        "catty_web_search_max_results",
        "catty_web_search_request_timeout",
        "catty_turtle_soup_cooldown_seconds",
        "catty_special_care_cooldown_seconds",
        "catty_special_care_response_window_minutes",
        "catty_emoji_interest_threshold",
        "catty_emoji_save_interest_threshold",
        "catty_emoji_max_candidates",
        "catty_emoji_reply_probability",
        "catty_proactive_max_daily_per_group",
        "catty_proactive_check_interval_seconds",
        "catty_proactive_min_interval_minutes",
        "catty_proactive_response_window_minutes",
        "catty_proactive_recent_messages",
        mode="before",
    )
    @classmethod
    def parse_optional_numbers(cls, value: Any) -> Any:
        if value == "":
            return None
        return value
