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


class KeywordReplyRule(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    reply: str = ""
    enabled: bool = True
    # 同一会话(以群为单位)在该秒数内不再重复回此规则;0 表示无冷却。
    cooldown_seconds: float = 0.0

    @field_validator("keywords", mode="before")
    @classmethod
    def parse_keywords(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                loaded = json.loads(raw)
                if not isinstance(loaded, list):
                    raise ValueError("keyword reply keywords JSON value must be a list")
                return [str(item).strip() for item in loaded if str(item).strip()]
            return _split_text(raw)
        return value


class Config(BaseModel):
    catty_openai_base_url: str = "https://api.openai.com/v1"
    catty_openai_api_key: str = ""
    catty_openai_model: str = "gpt-4o-mini"
    catty_openai_extra_headers: dict[str, str] = Field(default_factory=dict)
    catty_openai_extra_body: dict[str, Any] = Field(default_factory=dict)

    # 主 AI 主动生图(catty_imagegen tool)— 走 OpenAI Image API。
    # base_url/api_key 留空则复用上面 catty_openai_* 同一通道(codex/gpt-5.5 同 host 都行)。
    # 默认 low quality 1024x1024,省钱;主 AI 可在 args 里覆盖。
    catty_imagegen_enabled: bool = True
    catty_imagegen_base_url: str = ""
    catty_imagegen_api_key: str = ""
    catty_imagegen_model: str = "gpt-image-2"
    catty_imagegen_default_size: str = "1024x1024"
    catty_imagegen_default_quality: str = "low"
    catty_imagegen_default_format: str = "png"
    catty_imagegen_cooldown_seconds: int = 180
    # 生图慢:gpt-image-2 high quality 大图可能要 200s+,默认 300s 给足。
    catty_imagegen_timeout_seconds: float = 300.0
    catty_imagegen_max_chars: int = 2000
    # 写本地缓存目录(napcat 走本地文件比 base64 inline 大图稳)。相对路径 = bot cwd
    catty_imagegen_cache_dir: str = "pictures/imagegen_cache"
    # 缓存目录最多保留 N 张,LRU(超出删最老)
    catty_imagegen_cache_max_files: int = 200
    # 把 imagegen 请求的 https:// 强制改成 http:// 绕过上游 CF 反代 100s Origin Timeout 硬限。
    # 主人贴的账单实测 OpenAI gpt-image-2 经常 150-200s 才完成,HTTPS 走 CF 边缘 124s 就 524,
    # HTTP 直连 origin 没有这个限制。chat_completion 短不受影响,保持 HTTPS。
    catty_imagegen_force_http_scheme: bool = True

    # 慢请求 placeholder:主回复 chat_completion 进入后超过该秒数没回,先 send 一句轻量占位
    # 避免用户以为 bot 卡死了/被忽略了。0 或负数 = 禁用。
    catty_slow_reply_placeholder_seconds: float = 8.0
    # 占位消息后台 task 异常时静默(主回复链路不能被打断)
    catty_slow_reply_placeholder_max_messages: int = 1

    # IDE 多 tab 风格的会话排队:每群最多 N 个并发回复(默认 3),不同用户能并行;
    # 同一用户在同群仍串行(防同人乱序爆消息)。0 或负数 = 退化回老的一群一把大锁。
    catty_reply_group_concurrency: int = 3

    # 主 AI 不可用时的本地 fallback（默认指向项目内 Ollama qwen2.5:7b）
    catty_ai_fallback_enabled: bool = False
    catty_ai_fallback_base_url: str = ""
    catty_ai_fallback_api_key: str = ""
    catty_ai_fallback_model: str = ""
    catty_ai_fallback_extra_headers: dict[str, str] = Field(default_factory=dict)
    catty_ai_fallback_extra_body: dict[str, Any] = Field(default_factory=dict)
    catty_ai_fallback_temperature: float | None = None
    catty_ai_fallback_max_tokens: int | None = 4096
    catty_ai_fallback_request_timeout: float | None = 180.0
    # 触发 fallback 后，主 AI 在该秒数内直接走本地不再重试云
    catty_ai_fallback_cooldown_seconds: float = 300.0
    # MC 有玩家在线时禁止本地 fallback 推理（保护游戏流畅度）
    catty_ai_fallback_mc_gate_enabled: bool = True
    catty_ai_fallback_mc_server_host: str = "localhost"
    catty_ai_fallback_mc_server_port: int = 26843
    catty_ai_fallback_mc_ping_timeout_seconds: float = 3.0
    # 兜底模型已经把人格烧进 Modelfile SYSTEM 时,剥离 system role 消息避免 KV cache 失效
    # 留空(默认)时:若 model 以 "catty-" 开头自动启用,否则不剥离
    catty_ai_fallback_strip_system_messages: bool = False

    catty_audit_ai_base_url: str = ""
    catty_audit_ai_api_key: str = ""
    catty_audit_ai_model: str = ""
    catty_audit_ai_extra_headers: dict[str, str] = Field(default_factory=dict)
    catty_audit_ai_extra_body: dict[str, Any] = Field(default_factory=dict)
    catty_audit_ai_temperature: float | None = 0.1
    catty_audit_ai_max_tokens: int | None = 320
    catty_audit_ai_request_timeout: float | None = 60.0

    catty_vision_base_url: str = ""
    catty_vision_api_key: str = ""
    catty_vision_model: str = ""
    catty_vision_extra_headers: dict[str, str] = Field(default_factory=dict)
    catty_vision_extra_body: dict[str, Any] = Field(default_factory=dict)
    catty_vision_prompt: str = "请识别图片内容，提取和聊天回复相关的信息。不要评价图片是否安全，不要添加 emoji；如果有文字请尽量转写。"
    catty_vision_temperature: float | None = 0.2
    catty_vision_max_tokens: int | None = 800
    catty_vision_request_timeout: float | None = None
    # vision 走异步:消息一进来就 fire-and-forget 启 task,主回复链路最多短等
    # catty_vision_inline_max_wait_seconds 秒;等不到就不带 vision 描述直接回,
    # 后台 task 跑完写 memory_store,下一轮自动复用。
    # 注:默认值要大于 vision 模型的平均响应时间(8-12s 常见),否则主回复总赶在
    # vision 完成前就走,看起来像"猫猫不识图"。配合 reply_queue_max_wait_seconds
    # 排队雪崩防护使用,排队消息超 25s 才丢弃,所以这里给 15s 是安全的。
    catty_vision_async_enabled: bool = True
    catty_vision_inline_max_wait_seconds: float = 15.0

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
    catty_local_critic_enabled: bool = False
    catty_local_critic_mode: str = "reply_gate_only"
    catty_local_critic_base_url: str = "http://127.0.0.1:11434/v1"
    catty_local_critic_api_key: str = "ollama"
    catty_local_critic_model: str = "qwen2.5:1.5b"
    catty_local_critic_extra_headers: dict[str, str] = Field(default_factory=dict)
    catty_local_critic_extra_body: dict[str, Any] = Field(default_factory=lambda: {"think": False})
    catty_local_critic_temperature: float | None = None
    catty_local_critic_max_tokens: int | None = 16
    catty_local_critic_request_timeout: float | None = 4.0
    catty_local_critic_rewrite_when_score_below: int = 0
    catty_local_critic_reply_gate_enabled: bool = True
    catty_local_critic_reply_gate_min_confidence: int = 55
    catty_local_critic_reply_gate_examples: int = 0
    catty_local_critic_reply_gate_max_tokens: int | None = 16
    catty_local_critic_reply_gate_request_timeout: float | None = 4.0
    catty_local_critic_reply_gate_user_message_chars: int = 120
    catty_local_critic_reply_gate_plain_text_chars: int = 60
    catty_local_critic_reply_gate_context_chars: int = 80
    catty_local_critic_warmup_enabled: bool = True
    catty_local_critic_warmup_keep_alive: str = "30m"
    catty_local_critic_warmup_interval_seconds: float = 300.0
    catty_local_critic_warmup_request_timeout: float = 60.0
    catty_local_critic_force_direct_reply: bool = True
    catty_local_critic_collect_training_samples: bool = True
    catty_local_critic_training_samples_path: str = "local_critic_samples.jsonl"
    catty_local_training_collect_assistant_samples: bool = True
    catty_local_training_assistant_samples_path: str = "training/assistant_reply_samples.jsonl"
    catty_web_search_enabled: bool = True
    catty_web_search_cooldown_seconds: int = 60
    catty_web_search_max_results: int = 5
    catty_web_search_request_timeout: float | None = 10.0
    catty_web_search_engines: list[str] = Field(default_factory=lambda: ["google", "bing"])
    catty_nsfw_search_enabled: bool = True
    catty_nsfw_search_max_results: int = 4
    catty_nsfw_search_request_timeout: float | None = 15.0
    catty_nsfw_search_cooldown_seconds: int = 30
    catty_nsfw_image_send_count: int = 2
    catty_nsfw_pixiv_cookie: str = ""
    catty_nsfw_pixiv_image_size: str = "regular"

    catty_owner_qq: int = 0
    catty_owner_forward_enabled: bool = False
    catty_owner_forward_private_messages: bool = True
    catty_owner_forward_block_ai_reply: bool = True
    # 猫猫(笨猫人格本身)各平台社交账号:被群友问起"你的 steam"/"猫猫 steam"
    # 或聊到某游戏想给出猫猫自己对应平台账号时引用,空字符串表示猫猫在那个平台没账号。
    catty_social_steam: str = ""
    catty_turtle_soup_cooldown_seconds: int = 300

    catty_system_prompt: str = "你是一个接入 QQ 的中文 AI 助手，回答要友好、简洁、可靠。"
    catty_trigger_prefixes: list[str] = Field(default_factory=lambda: ["ai", "AI", "猫猫"])
    catty_enable_private: bool = True
    catty_enable_group: bool = True
    catty_private_require_prefix: bool = False
    catty_group_require_mention_or_prefix: bool = True
    catty_group_history_scope: str = "group"
    catty_history_turns: int = 16
    catty_session_cache_persistence_enabled: bool = True
    catty_session_cache_dir: str = "sessions"
    catty_session_cache_max_sessions: int = 200
    catty_session_cache_save_debounce_seconds: float = 2.0
    # bot 主进程 CPU affinity (Windows). 0 = 不绑核 (默认 OS 自由调度)
    # 6 核机器:1 = 核0 only,把 Ollama 留给核1-5
    catty_cpu_affinity_mask: int = 0
    catty_directed_keywords: list[str] = Field(
        default_factory=lambda: ["你", "猫猫", "猫娘", "看看", "帮我看看", "这张图", "这个图", "图片", "图里", "评价一下", "怎么回事"]
    )
    catty_keyword_replies: list[KeywordReplyRule] = Field(default_factory=list)
    catty_soft_directed_reply_probability: float = 0.65
    catty_direct_address_reply_probability: float = 0.9
    catty_image_response_enabled: bool = True
    catty_image_vision_enabled: bool = True
    # 同会话排队雪崩防护:消息在锁队列里等待超这个秒数就放弃当前消息,
    # 避免视觉/AI 卡顿后积压消息一起爆出来。
    catty_reply_queue_max_wait_seconds: float = 25.0
    catty_emoji_enabled: bool = True
    catty_emoji_dir: str = "emojis"
    catty_emoji_download_dir: str = "emojis/downloaded"
    catty_emoji_manifest_path: str = "emojis/manifest.json"
    catty_emoji_interest_threshold: int = 60
    catty_emoji_save_interest_threshold: int = 85
    catty_emoji_max_candidates: int = 8
    catty_emoji_reply_enabled: bool = True
    catty_emoji_reply_probability: float = 0.85
    catty_emoji_auto_fallback_enabled: bool = False
    catty_emoji_diversity_enabled: bool = True
    catty_emoji_diversity_recent_window: int = 8
    catty_emoji_diversity_candidate_pool: int = 6
    catty_legs_enabled: bool = True
    catty_legs_pictures_dir: str = "pictures"
    catty_legs_cooldown_seconds: float = 3.0
    # 戳一戳：避免被刷屏。同一用户在同一会话内的最小回复间隔与回应概率
    catty_poke_cooldown_seconds: float = 45.0
    catty_poke_reply_probability: float = 0.85
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
    catty_memory_reply_boost_enabled: bool = True
    catty_memory_reply_boost_min_corpus_messages: int = 80
    catty_memory_reply_boost_probability_bonus: float = 0.15
    catty_memory_reply_boost_max_probability: float = 0.95
    # 记忆落盘 debounce：把高频群消息触发的 _save() 合并成最多每 N 秒一次实际写盘。
    catty_memory_save_debounce_seconds: float = 2.0
    catty_special_group_active_window_enabled: bool = False
    catty_special_group_active_minutes_per_hour: int = 10
    catty_group_titles: dict[str, str] = Field(default_factory=dict)
    catty_user_titles: dict[str, str] = Field(default_factory=dict)
    catty_group_user_titles: dict[str, dict[str, str]] = Field(default_factory=dict)

    catty_game_context_star_resonance_group_ids: set[int] = Field(default_factory=set)
    catty_game_context_strinova_group_ids: set[int] = Field(default_factory=set)

    catty_proactive_enabled: bool = True
    catty_proactive_max_daily_per_group: int = 5
    catty_proactive_check_interval_seconds: float = 300.0
    catty_proactive_min_interval_minutes: float = 120.0
    catty_proactive_response_window_minutes: float = 30.0
    catty_proactive_recent_messages: int = 40
    # 活跃门槛：最近 active_window_minutes 内群友消息条数 < active_min_messages 时不主动冒泡。
    # 设为 0 即关闭该门槛(回到旧的"按目标和间隔无脑冒泡"行为)。
    catty_proactive_active_window_minutes: float = 15.0
    catty_proactive_active_min_messages: int = 5

    # ── 游戏记忆库(独立于 user/group 记忆) ──────────────────────────────
    # AI 调 catty_web_search 在游戏群拿到结果时自动 sink;AI 也可以主动用
    # catty_game_remember 写入。catty_game_recall 是双向查询接口。
    # 文件结构:每个游戏一个 JSON,memory_games/game_{name}.json。
    catty_memory_game_storage_dir: str = "memory_games"
    catty_memory_max_game_facts: int = 200  # 每个游戏最多保留多少条事实(超出按时间淘汰最旧)
    catty_memory_max_game_fact_chars: int = 360  # 单条事实文本上限
    # 周期性摘要:每个游戏 facts 数 >= min_facts 且距上次摘要 >= interval_minutes 时触发 LLM 压缩,
    # 摘要后只保留最近 keep_recent_facts 条原始事实,其它压缩进 summary。
    catty_memory_game_summary_min_facts: int = 60
    catty_memory_game_summary_interval_minutes: float = 360.0  # 6 小时
    catty_memory_game_keep_recent_facts: int = 20
    # 单游戏 JSON 文件超过此字节数,强制触发一次 LLM 压缩(走和 due_games_for_summary 同一通道),
    # 避免长期积累让 game_<name>.json 无限膨胀。默认 200KB。
    catty_memory_game_size_compress_threshold_bytes: int = 200_000

    # ── 主 AI 工具调用(OpenAI function calling 协议) ──────────────────
    # 主回复时给云端模型挂 catty_recall / catty_user_profile / catty_mc_status 三套 tool schema,
    # 由模型自行判断要不要查询。默认常驻挂载,让上游 prompt cache 命中 tools 部分。
    # 上游返回 400 或 tool 调度异常时会自动降级一次纯文本回复。
    catty_tools_enabled: bool = True
    # 本地解析层(在 system prompt 注入前运行) — 出问题时可临时关掉某层。
    # 合法值: slang / pulse / intent / entity / hints
    # 留空表示全部启用(默认推荐)。例如 ["hints"] 表示只关 hints,其它都开。
    catty_parsing_layers_disabled: list[str] = Field(default_factory=list)
    # 单次主回复最多允许的 tool 调用轮次(防止模型反复循环调 tool)
    catty_tools_max_rounds: int = 3
    # 每个 tool 结果的 in-process LRU TTL(秒);0 表示不缓存
    catty_tools_cache_ttl_seconds: float = 60.0
    # 单次 tool 调用上限(每轮多个 tool_calls 也算)
    catty_tools_max_calls_per_round: int = 3
    # 私聊场景下额外排除哪些 tool;群聊默认全开
    catty_tools_disabled_in_private: list[str] = Field(default_factory=lambda: ["catty_user_profile"])

    catty_temperature: float | None = 0.7
    # 拉高 max_tokens 默认值——gpt-5.5 调 catty_imagegen 时 tool_calls 和最终回复
    # 都要装下,12800 偶尔不够长 prompt 转写 + 多轮 + 短评。改 32000 给足缓冲。
    catty_max_tokens: int | None = 32000
    catty_request_timeout: float = 60.0
    catty_reply_max_chars: int = 1800
    catty_reply_human_split_enabled: bool = True
    catty_reply_human_split_probability: float = 0.35
    catty_reply_human_split_min_chars: int = 48
    catty_reply_human_split_max_chunks: int = 4
    catty_reply_human_split_delay_seconds: float = 0.8
    catty_reply_mix_emoji_with_text: bool = True
    catty_reply_quote_enabled: bool = True
    catty_reply_quote_private_enabled: bool = False
    catty_reply_self_check_enabled: bool = True
    catty_reply_style_examples_enabled: bool = True
    catty_expression_repeat_enabled: bool = True
    catty_expression_repeat_threshold: int = 2
    catty_expression_repeat_window_seconds: float = 20.0
    catty_expression_repeat_include_images: bool = True
    catty_expression_repeat_include_text: bool = True

    catty_allowed_user_ids: set[int] = Field(default_factory=set)
    catty_allowed_group_ids: set[int] = Field(default_factory=set)
    # bot-loop 防护三层：
    # 1) 硬黑名单 QQ 号（如 Q 群管家明确加进来）
    # 2) QQ 协议层机器人标记（NapCat / OneBot sender 字段中的 is_bot/role=bot/category 等）
    # 3) 启发式自介模板兜底（"我是 X 助手"、"暂时还不能和你对话" 等）
    catty_ignored_user_ids: set[int] = Field(default_factory=set)
    catty_ignore_marked_bots: bool = True
    catty_ignore_bot_self_intro_enabled: bool = True
    catty_http_proxy: str = ""
    catty_hot_reload_enabled: bool = True
    catty_hot_reload_poll_seconds: float = 1.5
    catty_hot_reload_debounce_seconds: float = 1.0
    catty_hot_reload_restart_on_code_change: bool = True

    @field_validator(
        "catty_openai_base_url",
        "catty_audit_ai_base_url",
        "catty_vision_base_url",
        "catty_filter_base_url",
        "catty_local_critic_base_url",
    )
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

    @field_validator("catty_local_critic_mode")
    @classmethod
    def validate_local_critic_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        aliases = {
            "gate": "reply_gate_only",
            "gate_only": "reply_gate_only",
            "reply_gate": "reply_gate_only",
            "reply_gate_only": "reply_gate_only",
            "critic": "reply_gate_and_critic",
            "full": "reply_gate_and_critic",
            "reply_gate_and_critic": "reply_gate_and_critic",
        }
        if normalized not in aliases:
            raise ValueError("catty_local_critic_mode must be reply_gate_only or reply_gate_and_critic")
        return aliases[normalized]

    @field_validator("catty_trigger_prefixes", "catty_directed_keywords", "catty_web_search_engines", mode="before")
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

    @field_validator("catty_keyword_replies", mode="before")
    @classmethod
    def parse_keyword_replies(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            return json.loads(raw)
        if isinstance(value, dict):
            return [{"keywords": [key], "reply": reply} for key, reply in value.items()]
        return value

    @field_validator(
        "catty_allowed_user_ids",
        "catty_allowed_group_ids",
        "catty_ignored_user_ids",
        "catty_memory_special_group_ids",
        "catty_special_care_user_ids",
        "catty_game_context_star_resonance_group_ids",
        "catty_game_context_strinova_group_ids",
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

    @field_validator(
        "catty_openai_extra_headers",
        "catty_ai_fallback_extra_headers",
        "catty_audit_ai_extra_headers",
        "catty_vision_extra_headers",
        "catty_filter_extra_headers",
        "catty_local_critic_extra_headers",
        mode="before",
    )
    @classmethod
    def parse_extra_headers(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            data = _parse_json_object(value)
        else:
            data = value
        return {str(key): str(val) for key, val in dict(data).items()}

    @field_validator(
        "catty_openai_extra_body",
        "catty_ai_fallback_extra_body",
        "catty_audit_ai_extra_body",
        "catty_vision_extra_body",
        "catty_filter_extra_body",
        "catty_local_critic_extra_body",
        mode="before",
    )
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
        "catty_audit_ai_temperature",
        "catty_audit_ai_max_tokens",
        "catty_audit_ai_request_timeout",
        "catty_vision_temperature",
        "catty_vision_max_tokens",
        "catty_vision_request_timeout",
        "catty_vision_inline_max_wait_seconds",
        "catty_reply_queue_max_wait_seconds",
        "catty_filter_temperature",
        "catty_filter_max_tokens",
        "catty_filter_request_timeout",
        "catty_filter_group_batch_messages",
        "catty_filter_group_batch_seconds",
        "catty_filter_anger_warn_threshold",
        "catty_filter_anger_mute_threshold",
        "catty_filter_anger_cooldown_seconds",
        "catty_local_critic_temperature",
        "catty_local_critic_max_tokens",
        "catty_local_critic_request_timeout",
        "catty_local_critic_rewrite_when_score_below",
        "catty_local_critic_reply_gate_min_confidence",
        "catty_local_critic_reply_gate_examples",
        "catty_local_critic_reply_gate_max_tokens",
        "catty_local_critic_reply_gate_request_timeout",
        "catty_local_critic_reply_gate_user_message_chars",
        "catty_local_critic_reply_gate_plain_text_chars",
        "catty_local_critic_reply_gate_context_chars",
        "catty_local_critic_warmup_interval_seconds",
        "catty_local_critic_warmup_request_timeout",
        "catty_web_search_cooldown_seconds",
        "catty_web_search_max_results",
        "catty_web_search_request_timeout",
        "catty_nsfw_search_max_results",
        "catty_nsfw_search_request_timeout",
        "catty_nsfw_search_cooldown_seconds",
        "catty_nsfw_image_send_count",
        "catty_turtle_soup_cooldown_seconds",
        "catty_special_care_cooldown_seconds",
        "catty_special_care_response_window_minutes",
        "catty_soft_directed_reply_probability",
        "catty_direct_address_reply_probability",
        "catty_memory_reply_boost_min_corpus_messages",
        "catty_memory_reply_boost_probability_bonus",
        "catty_memory_reply_boost_max_probability",
        "catty_emoji_interest_threshold",
        "catty_emoji_save_interest_threshold",
        "catty_emoji_max_candidates",
        "catty_emoji_reply_probability",
        "catty_emoji_diversity_recent_window",
        "catty_emoji_diversity_candidate_pool",
        "catty_proactive_max_daily_per_group",
        "catty_proactive_check_interval_seconds",
        "catty_proactive_min_interval_minutes",
        "catty_proactive_response_window_minutes",
        "catty_proactive_recent_messages",
        "catty_proactive_active_window_minutes",
        "catty_proactive_active_min_messages",
        "catty_reply_human_split_max_chunks",
        "catty_hot_reload_poll_seconds",
        "catty_hot_reload_debounce_seconds",
        mode="before",
    )
    @classmethod
    def parse_optional_numbers(cls, value: Any) -> Any:
        if value == "":
            return None
        return value
