import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


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
    catty_model_context_tokens: int = 1_000_000
    catty_cache_hit_input_price_ratio: float = 0.02
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

    # ── NovelAI 路径(provider="nai") ──
    # 主 AI 在 catty_imagegen tool 里选 provider="nai" 时走这条; 二次元/动漫/角色立绘适用。
    # 三个标准尺寸: portrait 832x1216 / landscape 1216x832 / square 1024x1024
    # Opus tier3 订阅在这三档 + steps<=28 + n_samples=1 是免费的(Anlas=0)。
    catty_imagegen_nai_enabled: bool = False
    catty_imagegen_nai_token: str = ""
    catty_imagegen_nai_model: str = "nai-diffusion-4-5-full"
    catty_imagegen_nai_default_aspect: str = "portrait"  # portrait / landscape / square
    catty_imagegen_nai_steps: int = 28
    catty_imagegen_nai_scale: float = 5.0
    catty_imagegen_nai_sampler: str = "k_euler_ancestral"
    catty_imagegen_nai_noise_schedule: str = "karras"
    catty_imagegen_nai_default_negative: str = (
        "lowres, worst quality, low quality, bad anatomy, bad hands, "
        "missing fingers, extra fingers, watermark, signature, jpeg artifacts"
    )
    catty_imagegen_nai_timeout_seconds: float = 180.0
    # 基础 5 积分,多 1 Anlas +3 积分(三个标准尺寸 + 默认 28 steps 时 Anlas=0,只扣 5)
    catty_imagegen_nai_base_points: int = 5
    catty_imagegen_nai_points_per_anlas: int = 3
    # 专用 HTTP/SOCKS proxy 走 NovelAI 上行 — 远端国内服务器到 image.novelai.net
    # 真实 IP 被墙时填这个, 其他路径(napcat / chat completion)不受影响。
    # 留空 = 直连。支持 http://user:pass@host:port 和 socks5://user:pass@host:port。
    catty_imagegen_nai_http_proxy: str = ""

    # 慢请求 placeholder:主回复 chat_completion 进入后超过该秒数没回,先 send 一句轻量占位
    # 避免用户以为 bot 卡死了/被忽略了。0 或负数 = 禁用。
    # 主人 2026-05-28: 30 → 60,「翻翻笔记」式 placeholder 触发别太急,让 sonnet 多想会儿
    catty_slow_reply_placeholder_seconds: float = 60.0
    # 占位消息后台 task 异常时静默(主回复链路不能被打断)
    catty_slow_reply_placeholder_max_messages: int = 1

    # ST 风 random encounter — 每条 reply 触发『本轮主动小开场』hint 的概率(0-1)。
    # 不是 push 主动消息(catty 是 reactive), 而是当用户 reply 时偶尔笨猫会冒一句
    # 自己的小事开场,让对话不再 100% 被动响应。0 = 禁用,>0.10 会很吵不建议。
    catty_random_encounter_chance: float = 0.03

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
    # NSFW deep 路径 (stage 8/9/10) 专用 model. 空时 fallback 到 catty_filter_model.
    # **不 hardcode 默认值**, 主人在 config.json 里设.
    catty_nsfw_spark_model: str = ""
    # NSFW spark 独立 base_url / api_key (主人 2026-05-27 选项 2):
    # 空时 fallback 到 catty_filter_base_url / catty_filter_api_key (跟 mood classifier 同站).
    # 配上时 nsfw_spark_model 走独立 endpoint (例如 deepseek 官方), mood classifier 仍走 filter.
    catty_nsfw_spark_base_url: str = ""
    catty_nsfw_spark_api_key: str = ""
    # placeholder / 签到 caption 短回复路径专用 model (主人原话: 不要 spark, 走 5.3-codex).
    # chat_completion_codex_instant 优先级: model_override > codex_instant_model > nsfw_spark_model > filter_model.
    # 空时 fallback 到 nsfw_spark_model 或 filter_model. **不 hardcode 默认值**, 在 config.json 里设.
    catty_codex_instant_model: str = ""
    # 累计软拒达 threshold 后自动切的 fallback model (默认空 → 用 catty_filter_model = spark).
    # 例: 主 model = gpt-5.5, fallback = gpt-5.3-codex-spark.
    catty_nsfw_fallback_model: str = ""
    catty_nsfw_softrefuse_threshold: int = 2
    # Anthropic Prompt Caching (ST PR #3085 移植) — 显式注入 cache_control: ephemeral.
    # OpenAI native 是 implicit caching 不需要; Claude / 中间人走 Claude 协议时需要显式标记.
    # 启用后 spark + 主路径都注入 4 个 breakpoint (system tail + messages depth 2/4 + tools tail),
    # log cache_read_input_tokens / cache_creation_input_tokens 监测命中率.
    # 主人 2026-05-26 确认 cache 对 spark NSFW 无副作用, 默认 True.
    # 中间人若不支持会忽略 unknown field 不报错; Claude/Anthropic 协议命中真实 cache.
    catty_prompt_cache_enabled: bool = True
    # cache_control 注入的 depth (从 messages 末尾倒数第 N 处 role 切换). ST 默认 2.
    catty_prompt_cache_depth: int = 2
    # 主人 2026-05-28 C7: cache TTL 改回 5min (默认 Anthropic 标准).
    # 价格: 5min write 1.25x, 1h write 2x, cache read 都 0.1x.
    # 改回 5min 原因: 主人观察到 1h + dynamic content 一起 cache 后, Claude 会触发 safety
    # refusal ("我需要在这里停下来"). 短 TTL 让 cache prefix 不会跨多个 user session 持久化.
    catty_cache_ttl: str = "5min"
    # 主人 2026-05-28 Phase 1.2: 第二轮请求注入 extra_body.diagnostics.previous_message_id
    # 用来跟踪 cache miss 原因. NewAPI / 中转 relay 不识别 diagnostics 字段时第二轮会 500.
    # 默认 OFF, 只有直连 anthropic.com 时才打开.
    catty_cache_diag_previous_message_id_enabled: bool = False
    # 主人 2026-05-28 Phase 1.3: native 入口扫到 messages 含 role=system 时打 WARNING.
    # 守卫 _post_boundary 等动态段意外以 system role 漏出, 帮助定位 cache miss 元凶.
    # 不影响生产 (会 sweep 到 system_blocks), 仅诊断辅助.
    catty_cache_warn_on_inline_system: bool = True
    # ── Anthropic native /v1/messages 路径 (主人 2026-05-28: NewAPI SG relay
    # pass_through_body_enabled=true 后, 中转层字节级透传 body 含 cache_control /
    # context_management / metadata. 切换该开关后 catty 走 anthropic SDK 发原生请求,
    # 享受 server-side compaction (compact-2026-01-12) — input>150K 自动 server 端摘要.
    # OAuth MAX 下 cache_creation/read 静默 0 不是 bug; 未来换付费 sk-ant-api03- key
    # 自动享 90% cache 折扣.
    catty_anthropic_native_enabled: bool = False
    # server-side compaction (compact-2026-01-12 beta). 自动在 input>trigger_tokens 时
    # server 端跑摘要并在 response.content 里插 compaction block + applied_edits[] 标记.
    catty_compaction_enabled: bool = False
    catty_compaction_trigger_tokens: int = 150000  # Anthropic 默认 150K
    # ── 主人 2026-07-06 多 provider 缓存适配 (openai-claude-95 计划) ─────────
    # OpenAI 隐式缓存路由亲和: 仅对 detect_provider=='openai' 的端点注入
    # payload.prompt_cache_key = "catty:{scope}"。DeepSeek 绝不注入 (Round 10 红线:
    # user/prompt_cache_key 会把 DeepSeek 公共前缀 cache 分裂成独立 namespace 各自冷启)。
    catty_openai_prompt_cache_key_enabled: bool = False
    # native 路由 per-line 覆盖: key ∈ {main,spark,filter,summary_fallback,vision,router},
    # value ∈ {auto,native,compat}; 缺省 auto = detect_provider(线路 base_url, model) 判别。
    # 只在 catty_anthropic_native_enabled=True (总闸) 时整个 native 路由才激活。
    catty_native_route_overrides: dict[str, str] = Field(default_factory=dict)
    # per-line cache TTL ("5min"|"1h"), 缺省落全局 catty_cache_ttl。
    # 目标态 {"main":"1h","spark":"1h"} — QQ 轮间隔常 >5min, 5min TTL 过期重写吃掉
    # 命中 (读会续期, 只有间隔>TTL 的轮全量重写)。待 A/B 实测后写进远端 config。
    catty_cache_ttl_overrides: dict[str, str] = Field(default_factory=dict)
    # native 末尾 assistant prefill 模式: "hint"=现状字节等价 (丢 prefill 转强 IC hint
    # 进最近 user); "native"=真 trailing assistant 续写; "auto"=按模型名分派。
    catty_native_prefill_mode: str = "hint"
    # native 额外 anthropic-beta (逃生阀: 某些中转要显式 extended-cache-ttl-2025-04-11
    # 才认 1h TTL 等), 附加在默认 betas 之后。
    catty_native_extra_betas: list[str] = Field(default_factory=list)
    # A/B 测试 provider 注册表: {name: {base_url, api_key, model, native?("1")}}。
    # 仅 /dev/sim_chat 的 provider_override 按名引用 — 凭据留在 config, 不过 HTTP。
    catty_test_providers: dict[str, dict[str, str]] = Field(default_factory=dict)
    catty_filter_group_batch_messages: int = 200
    catty_filter_group_batch_seconds: float = 1200.0
    # ── Local NLU enrichment (jieba / text2vec / HanLP) ─────────────────
    # 主人 2026-05-28 v2: 三库已部署且 v1 验证通过 (A/B 26/30), 默认 flip 到 True.
    # 失败 graceful fallback 到 legacy regex 路径 (装包问题不会让 bot 起不来).
    catty_use_jieba: bool = True
    catty_use_text2vec: bool = True
    catty_use_hanlp: bool = True
    # 主人 2026-05-28 phase 6: ONNX runtime fast path (单 embed 2-3ms vs torch 50-85ms).
    # 已部署验证, 默认 True. 装 optimum/onnxruntime 失败 → 自动 fallback torch.
    catty_text2vec_use_onnx: bool = True
    # 主人 2026-05-28 phase 5: 换 BGE small (~95MB, 512 dim) — 中文 STS 比
    # text2vec-base-chinese 公开榜准 2-3pp. 模型大小同级, 加载速度类似.
    # 切换后 prototype hash 自动 invalidate, 启动重 build ~30s.
    # 老主人 config 已配 shibing624 时此默认不生效, 想换需手动改 config.json.
    catty_text2vec_model_name: str = "BAAI/bge-small-zh-v1.5"
    catty_hanlp_pipeline: str = "FINE_ELECTRA_SMALL_ZH"
    catty_text2vec_topic_threshold: float = 0.55
    catty_text2vec_emotion_threshold: float = 0.45
    catty_text2vec_trend_threshold: float = 0.50
    # 主人 2026-05-28 v2: per-topic threshold 覆盖. config.json 可填:
    # "catty_text2vec_topic_threshold_overrides": {"food": 0.62, "tech": 0.48}
    # 留空 dict 走 prototypes._PER_TOPIC_THRESHOLDS 默认.
    catty_text2vec_topic_threshold_overrides: dict[str, float] = Field(default_factory=dict)
    catty_nlu_cache_dir: str = "src/catty_qq_ai/data/nlu_cache"
    catty_nlu_warmup_on_startup: bool = True
    # 大陆环境 HuggingFace 镜像 (空时不强制改 HF_ENDPOINT, 用环境变量原值)
    catty_nlu_hf_endpoint: str = "https://hf-mirror.com"
    # ── Prompt Compressor (Phase 3): monotonic anchor checkpoint ──────────
    # 主人 2026-05-28 plan-cattyCacheFixAndPromptSlim Phase 3:
    # 私聊 ≤5000 / 群聊 ≤3000 tokens. Cache-safe 设计: per-scope anchor 不动
    # → history 前缀字节稳定 → Anthropic cache prefix hit. 超 budget 时 anchor
    # 一次大跳 (剩 70% budget 留余量) → 1 次 cache miss → 之后稳定多轮.
    # NLU 失败 / disabled → 走原 history_messages, 不阻塞业务.
    catty_prompt_compressor_enabled: bool = True
    # 主人 2026-05-28 P4: 实测 sys PROTECTED 段(身份锚定)~5.6K floor, tools 3.5K,
    # 严格 5K 数学上要砍 persona → 人格漂移. 现实 budget 10K total = sys ~6K +
    # tools lazy ~1K + dynamic ~1.5K + history ~1.5K + current ~0.5K. 群聊同步松绑.
    catty_prompt_budget_private: int = 10000
    catty_prompt_budget_group: int = 7000
    # history 强制保留最近 N 条 (不进相关性排序). 主人决策: 配置项 + 动态扩展.
    catty_compressor_history_keep_recent_private: int = 2
    catty_compressor_history_keep_recent_group: int = 4
    # 当 NLU 判断 query 含指代/上下文依赖 (代词/省略) 时, history 额外保留 +N 条.
    catty_compressor_history_extend_when_anaphora: int = 2
    # user_details / summary 的 top-K 选取数
    catty_compressor_user_details_top_k: int = 5
    catty_compressor_summary_top_paragraphs: int = 3
    # cosine 相似度 + 时间衰减 → 综合 score 中, recency 占比 (0=纯语义, 1=纯时序)
    catty_compressor_recency_weight: float = 0.3
    # history 段在总 budget 里的占比. P4 调到 0.2 (10K × 0.2 = 2K history,
    # 群聊 7K × 0.2 = 1.4K), 剩余给 sys 6K + tools 1K + dynamic 1.5K + current 0.5K.
    catty_compressor_history_budget_ratio: float = 0.2
    # ── P5.5 Lazy tool schema (description ≤30 字, properties 极简) ────
    # 主人 2026-05-28 plan-cattyCacheFixAndPromptSlim P5.5:
    # 砍 ~3K tool schema description. OpenAI native 由 _LAZY_TOOL_SCHEMAS 直接返回,
    # Anthropic native 由 convert_openai_tool_to_anthropic 自动转 (cache 字节稳定).
    catty_tools_lazy_schema_enabled: bool = True
    # ── P5.3 每 N 轮人格 reminder ────────────────────────────────────
    # 长对话防人格漂移. P5.1 core_persona 在 cache prefix 一次 inject, 长对话末段
    # 因 LLM recency bias 可能淡化. 每 N 轮在 user msg 附近 (depth=2) 注入精简
    # (~150 token) 5 铁律 reminder, 解决漂移.
    catty_persona_reminder_enabled: bool = True
    catty_persona_reminder_every_n_turns: int = 6
    # ── Native cache diagnostics / OpenAI-compatible request dump: default off ──
    # Gates verbose native cache logs and full request dumps; lightweight cohort metrics stay on.
    catty_cache_diag_enabled: bool = False
    catty_filter_anger_enabled: bool = False  # 主人:每条群消息都喂 spark 判耐心太烧
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
    # ── [DEPRECATED 2026-05-27 reply gate kill] ──────────────────────────
    # reply gate 整个停了, 下面这些字段已不被读取. 保留 schema 兼容旧 config.json,
    # 不要在新逻辑里读它们. 真正决定要不要回的逻辑在 __init__.py 的 _rule 里 (本地确定性判断).
    catty_local_critic_reply_gate_enabled: bool = False  # 主人:指向猫猫的全交主 AI 自己判断
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

    # 反向搜图(catty_image_search tool):SauceNAO / trace.moe / ascii2d / iqdb
    # 多引擎并发,问出「谁画的/什么番/角色/出处」。SauceNAO 需 API key (saucenao.com 注册免费),
    # 没填会自动跳过 saucenao 走其它引擎。trace.moe / ascii2d / iqdb 无需 key。
    catty_image_search_enabled: bool = True
    catty_image_search_saucenao_api_key: str = ""
    catty_image_search_cooldown_seconds: int = 60
    catty_image_search_max_results: int = 5
    catty_image_search_request_timeout: float | None = 15.0

    catty_owner_qq: int = 0
    catty_owner_forward_enabled: bool = False
    catty_owner_forward_private_messages: bool = True
    catty_owner_forward_block_ai_reply: bool = True
    # 好友申请附言 / 临时会话私聊命中『包养笨猫』类援交关键词 → 自动同意好友 + 扣 100 积分
    # 进援交 sticky 窗口; 积分不够则提示签到。绕过主人手动审核, 默认关。
    catty_owner_forward_paid_auto_accept_enabled: bool = False
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
    # "group" = shared default, "user" = per-user (group_id + user_id composite key).
    # ambient_eavesdrop/proactive/catty_mood 保持 _conversation_queue_key (群级) 看群里在场感.
    catty_group_history_scope: str = "group"
    catty_history_turns: int = 3  # Legacy short-history mode only.
    catty_session_context_enabled: bool = True
    catty_session_context_target_tokens: int = 256_000
    catty_session_context_trim_to_tokens: int = 192_000
    catty_session_context_headroom_tokens: int = 32_000
    catty_session_cache_persistence_enabled: bool = True
    catty_session_cache_dir: str = "sessions"
    catty_session_cache_max_sessions: int = 200
    catty_session_cache_save_debounce_seconds: float = 2.0
    # 时间桶上下文 sidecar: 不改旧 sessions 格式, 把跨时间段的旧 raw 对话降温成稳定摘要,
    # 当前桶只注入短参数, 让 DeepSeek prefix/KV 长时间稳定命中。
    catty_time_bucket_context_enabled: bool = True
    catty_time_bucket_context_dir: str = "session_buckets"
    catty_time_bucket_group_minutes: int = 15
    catty_time_bucket_private_minutes: int = 30
    catty_time_bucket_max_finalized: int = 8
    catty_time_bucket_max_turns_per_bucket: int = 24
    # bot 主进程 CPU affinity (Windows). 0 = 不绑核 (默认 OS 自由调度)
    # 6 核机器:1 = 核0 only,把 Ollama 留给核1-5
    catty_cpu_affinity_mask: int = 0
    catty_directed_keywords: list[str] = Field(
        default_factory=lambda: ["你", "猫猫", "猫娘", "看看", "帮我看看", "这张图", "这个图", "图片", "图里", "评价一下", "怎么回事"]
    )
    catty_keyword_replies: list[KeywordReplyRule] = Field(default_factory=list)
    # ── CPU 主回复引擎 (BotLibre 风格: Semantic Router + txtai) ────────────
    # 主人 2026-05-28 plan-cpu-alicebot-nlu-ai:
    # 普通闲聊走 CPU 引擎 (~零成本/<200ms), 强互动/NSFW/CPU 信心不足才打 DeepSeek.
    # 默认 enabled=False, 需 bootstrap 种子语料后再切. 关停时透传到现 keyword_reply 链路.
    catty_cpu_engine_enabled: bool = False
    catty_cpu_engine_routes_dir: str = "src/catty_qq_ai/data/cpu_engine/routes"
    catty_cpu_engine_corpus_path: str = "src/catty_qq_ai/data/cpu_engine/corpus/qa_corpus.jsonl"
    catty_cpu_engine_txtai_index_path: str = "src/catty_qq_ai/data/cpu_engine/corpus/txtai_index"
    # L2 Semantic Router 阈值: direct≥0.82 直答, candidate∈[0.70,0.82) 标低信心进 L4
    catty_cpu_engine_l2_threshold_direct: float = 0.82
    catty_cpu_engine_l2_threshold_candidate: float = 0.70
    # 群聊阈值上调 (避免抢话)
    catty_cpu_engine_group_threshold_bonus: float = 0.03
    # L4 Ollama 风格化: 私聊默认开, 群聊默认关 (延迟+@炸群顾虑)
    catty_cpu_engine_l4_enabled_private: bool = True
    catty_cpu_engine_l4_enabled_group: bool = False
    catty_cpu_engine_l4_timeout_ms: int = 800
    # ── S5 (2026-05-29 主人方案): 本地 CPU LLM 全量笨猫体质改写 ───────────
    # mode:
    #   off       — L4 不动 (S1-S4 行为, 强互动直走 L5)
    #   stylize   — S3 旧 stylize_l4 (qwen2.5:7b 800ms, 只在低信心区改)
    #   catnify   — S5 新全量改写 (qwen3:4b 3000ms, 任何 L1/L2/L3 命中都改),
    #               LLM 可在输出加 <DEEPSEEK reason="..."> 标记自决透传
    # 主人决策: 服务器关 MC 后 16.3GB free, 直接全量 catnify 上线.
    catty_cpu_engine_l4_mode: str = "catnify"
    # 主人 2026-05-29 v4: 3B 演技不够还原 (echo prompt 字面字符) → 切 7B.
    # qwen2.5:7b 4 核 affinity warm 5.6s / decode 5.4 tok/s / 角色扮演显著强于 3B.
    catty_cpu_engine_l4_catnify_model: str = "qwen2.5:7b"
    # S5.6f (主人 2026-05-29) BUG FIX: catnify 必须走本地 Ollama, 不能复用 ai_fallback
    # (主人 ai_fallback 配的是 deepseek.com 兜底, catnify 调它会跑去 DeepSeek 烧 token).
    catty_cpu_engine_l4_catnify_base_url: str = "http://127.0.0.1:11434/v1"
    catty_cpu_engine_l4_catnify_api_key: str = "ollama"
    # S5.6f (主人 2026-05-29): 实测 qwen2.5:3b cold load 4.9s (磁盘→内存),
    # warm 2.85s; timeout 8s 留 cold 余地, 主人 ≤10s 上限内.
    catty_cpu_engine_l4_catnify_timeout_ms: int = 8000
    # 主人 2026-05-29 v2: token 输入只能 < 1000 (6C CPU prefill 慢, 保守 800 留余量)
    catty_cpu_engine_l4_catnify_max_input_tokens: int = 800
    catty_cpu_engine_l4_catnify_temperature: float = 0.7
    # 主人 2026-05-29 v4: 7B 5.4 tok/s × 200 = 37s decode 超时. 改 80 让笨猫回复短.
    catty_cpu_engine_l4_catnify_max_tokens: int = 80
    catty_cpu_engine_l4_catnify_history_turns: int = 4
    # 主人 2026-05-29 v5: 改 1 (串行排队), 上一条完成下一条再发. OLLAMA_NUM_PARALLEL=1
    # 多并发会让每个请求都被前面卡住 → 排队场景下 timeout. 串行保证每个 warm 准.
    catty_cpu_engine_l4_catnify_concurrency: int = 1
    catty_cpu_engine_l4_catnify_queue_max: int = 8
    # 队列满/超时/LLM 失败时的兜底:
    #   raw       — 发原 CPU 候选 (主人决策默认, 信任 L1/L2/L3)
    #   transparent — return False 透传 L5 (旧行为)
    catty_cpu_engine_l4_catnify_fallback_on_fail: str = "raw"
    # DEEPSEEK 透传限流: 每用户每小时最多透传 N 次, 防 LLM 标滥
    catty_cpu_engine_l4_catnify_deepseek_per_hour: int = 12
    # 搜索短路: 命中 web search 关键词时跳过整个 CPU 引擎让旧搜索链处理
    catty_cpu_engine_l4_catnify_search_shortcut: bool = True
    # ── S6 (主人 2026-05-29): L3 corpus 自动蒸馏 (DeepSeek 回复 → qa_corpus_live.jsonl) ──
    # openai_client 的回复入口 (chat_completion / _with_tools / _instant / _codex_instant)
    # 拿到 DeepSeek 生成的【面向用户回复】后异步追加, 累积 N 条触发 L3 index reload.
    # 主人 2026-05-29 决策: 私聊 (含 NSFW) 也采, 全采 — 只在写入前脱敏 (@/QQ号/称呼).
    # filter/分类/判断 (输出 bool/JSON) 不采 (埋点不在那些入口, 见 corpus_distill 模块头).
    catty_cpu_engine_l3_distill_enabled: bool = True
    # 主人 2026-05-29: 默认 False = 私聊也采 (全采). 之前误为 True 导致私聊全被跳过.
    catty_cpu_engine_l3_distill_skip_private: bool = False
    catty_cpu_engine_l3_distill_live_corpus_path: str = (
        "src/catty_qq_ai/data/cpu_engine/corpus/qa_corpus_live.jsonl"
    )
    # 主人 2026-05-29: 50 太高 (单链路时 pending 卡 11/50 永不 rebuild). 现在全链路采集 +
    # 调到 12, 让新蒸馏的语料更快进 L3 index. 热重载可随时改 config.json 即生效.
    catty_cpu_engine_l3_distill_rebuild_threshold: int = 12
    catty_cpu_engine_l3_distill_dedup_window: int = 1000
    catty_cpu_engine_l3_distill_min_user_len: int = 4
    catty_cpu_engine_l3_distill_max_user_len: int = 200
    catty_cpu_engine_l3_distill_min_assistant_len: int = 8
    catty_cpu_engine_l3_distill_max_assistant_len: int = 500
    # 米雪儿语气后缀池 (Script 变量 {cat_suffix} 随机取)
    catty_cpu_engine_cat_suffixes: list[str] = Field(
        default_factory=lambda: ["喵～", "喵呜", "ฅฅ", "嗷呜～", "爪爪", "贴贴"]
    )
    # 强制走主 AI 的前缀 (绕过 CPU 引擎)
    catty_cpu_engine_force_ai_prefixes: list[str] = Field(
        default_factory=lambda: ["#ai", "#aikey", "#refresh"]
    )
    # Cython native 模块开关 (失败自动 fallback 纯 Python)
    catty_cpu_engine_native_enabled: bool = True

    # ── 积分系统 (DeepSeek 调用按 token 扣分, Ollama/CPU 免费) ─────────────
    # 主人 2026-05-28 plan-cpu-alicebot:
    # 3 起步占位 + 按真实 prompt/completion token 结算多退少补.
    # 默认 enabled=False, S3 单独验证 ledger 后再开.
    catty_credit_enabled: bool = False
    catty_credit_initial_balance: int = 100
    catty_credit_daily_signin_amount: int = 30
    catty_credit_weekly_signin_bonus: int = 50
    catty_credit_passive_recover_per_hour: int = 5
    catty_credit_passive_recover_cap: int = 100
    catty_credit_deepseek_base_cost: int = 3
    catty_credit_deepseek_per_1k_prompt: int = 2
    catty_credit_deepseek_per_1k_completion: int = 5
    catty_credit_persist_path: str = "src/catty_qq_ai/data/credit/user_credits.json"
    catty_credit_persist_debounce_seconds: float = 5.0

    # ── Token 计费 (主人 2026-07-06, 取代上面已随 CPU 引擎关停的 credit 链路) ──
    # 私聊: 每次回复按本轮全部 AI 调用的 prompt+completion token 扣积分,
    #   每 tokens_per_point 个 token 扣 1 分 (向上取整); 余额 <=0 拦截要签到.
    # 群聊: 不扣积分, 每人每小时 quota 个 token 额度 (整点桶), 超了拦截,
    #   每小时只提示一次. 主人全豁免. 两者与 catty_credit_enabled 互不相干.
    # 拦截提醒 AI 现写 (token_billing.ai_gate_reply), 失败兜底固定文案池.
    catty_token_billing_enabled: bool = True
    catty_private_tokens_per_point: int = 1000
    catty_group_hourly_token_quota: int = 300_000  # 0 = 群聊不限

    # ── 强互动判定 (强制走 DeepSeek 的场景, 积分够才放行) ───────────────────
    # 主人 2026-05-28: NSFW phase>=P3 / 意图 ∈ strong_intents / 情绪强烈 / CPU 信心<阈值.
    catty_strong_cpu_confidence_threshold: float = 0.7
    catty_strong_emotion_intensity_threshold: float = 0.7
    catty_strong_intents: list[str] = Field(
        default_factory=lambda: ["tease_cat", "compliment_cat", "表白"]
    )
    catty_strong_nsfw_phase_threshold: int = 3

    # ── 每日 DeepSeek 自我进化 (审 + 改 CPU 层 template, 带 git 备份 + 自动 rollback) ──
    # 主人 2026-05-28: 全自动 DeepSeek 评审, 仅采群聊样本 (私聊不进采样池),
    # score<=2 自动退役/重写, score>=4 加权重, new_routes 灰度入 L2.
    # 默认关闭, S4 内测后再开.
    catty_evolution_enabled: bool = False
    catty_evolution_cron: str = "0 3 * * *"
    catty_evolution_samples_per_layer: int = 30
    catty_evolution_judge_model: str = "deepseek-v4-flash"
    catty_evolution_rollback_neg_feedback_pct: float = 0.2
    catty_evolution_rollback_score_decline_days: int = 3
    catty_evolution_sample_only_group: bool = True
    catty_evolution_logs_dir: str = "src/catty_qq_ai/data/cpu_engine/evolution_logs"
    catty_evolution_git_commit_enabled: bool = True

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
    catty_poke_cooldown_seconds: float = 12.0
    catty_poke_reply_probability: float = 1.0
    catty_memory_enabled: bool = True
    catty_memory_path: str = "memory.json"
    catty_memory_group_storage_dir: str = ""
    catty_memory_user_storage_dir: str = ""
    catty_memory_max_known_members: int = 5  # 主人 2026-05-28 C16-10: 20→5 砍 user content
    catty_memory_special_group_ids: set[int] = Field(default_factory=set)
    catty_special_care_user_ids: set[int] = Field(default_factory=set)
    catty_group_special_care_user_ids: dict[str, set[int]] = Field(default_factory=dict)
    catty_special_care_cooldown_seconds: int = 90
    catty_special_care_response_window_minutes: float = 30.0
    # 普通会话「续聊窗口」: 笨猫回复某人后, 该用户在窗口内不用 @ 也能续聊。
    # 主人 2026-05-29: 窗口时长从 180s 砍到 25s (25s 没新消息就退出会话跟踪);
    # 续聊期间连续 catty_followup_idle_limit 次没直接提到笨猫 (非 mentioned/used_prefix) 也关窗。
    catty_followup_window_seconds: float = 25.0
    catty_followup_idle_limit: int = 3
    catty_memory_summary_interval_minutes: int = 1440
    catty_memory_max_corpus_messages: int = 800
    catty_memory_private_summary_messages: int = 500
    catty_memory_member_mention_threshold: int = 20
    # 主人 2026-05-28 plan-quizzical-crane Step 3: 总结瘦身 → DeepSeek cache 友好
    # summary_max_chars: LLM 输出 summary 硬截断字数 (prompt + save 双侧守护)
    # corpus_max_tokens: 喂给 summary LLM 的语料 token 上限 (防 200+ 条 corpus 撑 8K+ 输入)
    # member_impression_max_chars: member 短画像 impression 字数 (原 140 改 70)
    catty_memory_summary_max_chars: int = 1000
    catty_memory_corpus_max_tokens: int = 2000
    catty_memory_member_impression_max_chars: int = 70
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

    # ── 多人格 (主人 2026-07-06): 群号→人格名映射 + 全局默认人格 ──
    # 解析优先级: persona_override_store(/人格 命令) > group_personas[gid] > default_persona。
    # 人格名见 personas.PERSONAS (catty / fadianji), 支持中文别名 (机机/笨猫)。
    catty_group_personas: dict[str, str] = Field(default_factory=dict)
    catty_default_persona: str = "catty"

    # ── 月经期心情 (主人 2026-08-10): 机机本人生理状态同步 ──
    # 按 last_period_start (YYYY-MM-DD, 当天=第1天) + cycle_days 推算周期相位,
    # 敏感相位 (月经期 / 黄体后期 PMS) 给 fadianji 注入生理状态 hint。
    # 纯 config 驱动, 改 config.json 即生效 (热重载), 无独立持久化。
    catty_menstrual_enabled: bool = False
    catty_menstrual_last_period_start: str = ""  # 空 = 不注入
    catty_menstrual_cycle_days: int = 28

    # ── 本体避让 (主人 2026-08-10): 机机本体在场时, 分身让位 ──
    # 本体在群发言后 cooldown 内, 分身忽视该群所有非本体消息; 只有本体本人 @ 才回。
    # watches: [{"group_id": "...", "user_id": "...", "cooldown_minutes": 30}]
    catty_body_presence_enabled: bool = False
    catty_body_presence_watches: list[dict[str, Any]] = Field(default_factory=list)

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
    # SillyTavern 风 PromptManager 配置(只影响 ST 风新模块: daily_life / world_info / story_arc)
    # 列表里出现的 identifier 按 (i+1)*100 排序;不出现的保持模块默认 order。
    # 合法值: catty_daily_life / catty_world_info / catty_story_arc
    # 留空 → 用各模块默认 order
    catty_prompt_order: list[str] = Field(default_factory=list)
    # 单独关闭某段 ST 风 prompt。同上 identifier。
    catty_prompts_disabled: list[str] = Field(default_factory=list)
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

    @model_validator(mode="after")
    def validate_session_context_token_budget(self) -> "Config":
        trim_to_tokens = self.catty_session_context_trim_to_tokens
        target_tokens = self.catty_session_context_target_tokens
        headroom_tokens = self.catty_session_context_headroom_tokens
        model_context_tokens = self.catty_model_context_tokens

        if not 0 < trim_to_tokens < target_tokens <= model_context_tokens:
            raise ValueError(
                "session context tokens must satisfy "
                "0 < trim_to_tokens < target_tokens <= model_context_tokens"
            )
        if not 0 < headroom_tokens < target_tokens - trim_to_tokens:
            raise ValueError(
                "session context headroom must satisfy "
                "0 < headroom_tokens < target_tokens - trim_to_tokens"
            )
        return self

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

    @field_validator(
        "catty_trigger_prefixes", "catty_directed_keywords", "catty_web_search_engines",
        "catty_prompt_order", "catty_prompts_disabled", "catty_native_extra_betas",
        mode="before",
    )
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
        "catty_native_route_overrides",
        "catty_cache_ttl_overrides",
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

    @field_validator("catty_test_providers", mode="before")
    @classmethod
    def parse_test_providers(cls, value: Any) -> Any:
        # {name: {base_url, api_key, model, native?}} — env 里是 JSON 字符串 (hot reload
        # 走 Config.model_validate(raw env str), 必须 before-validator 解析)
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            data = _parse_json_object(value)
        else:
            data = dict(value)
        out: dict[str, dict[str, str]] = {}
        for name, entry in data.items():
            if isinstance(entry, dict):
                out[str(name)] = {str(k): str(v) for k, v in entry.items()}
        return out

    @field_validator("catty_group_titles", "catty_user_titles", "catty_group_personas", mode="before")
    @classmethod
    def parse_title_map(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            return {str(key): str(val) for key, val in _parse_json_object(value).items()}
        return {str(key): str(val) for key, val in dict(value).items()}

    @field_validator("catty_body_presence_watches", mode="before")
    @classmethod
    def parse_body_presence_watches(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            loaded = json.loads(raw)
            if not isinstance(loaded, list):
                raise ValueError("catty_body_presence_watches must be a JSON list")
            return loaded
        return value

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
        "catty_followup_window_seconds",
        "catty_followup_idle_limit",
        mode="before",
    )
    @classmethod
    def parse_optional_numbers(cls, value: Any) -> Any:
        if value == "":
            return None
        return value
