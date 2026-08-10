"""多人格系统 — Persona registry + resolver（单一真相源）。

设计（主人 2026-07-06 plan 多人格系统）:
- Persona 是 frozen dataclass, 字段默认 None = 「用笨猫现有内容/行为」。
  catty 实例因此几乎全空 → prompt 管线在 catty 下逐字节走老路径, cache prefix 不变。
- 非 catty 人格通过字段覆盖内容, 通过 disabled_prompt_segments / disabled_features
  关掉笨猫强绑定的段和功能(不重写)。
- 不同 persona = 不同 cache prefix 是预期行为(同私聊/群聊两份前缀);
  只要求同一 persona 内所有 pre-boundary 段跨轮 byte-stable —
  因此 Persona 上的 prompt 内容必须全是模块级常量, 禁止运行时插值。

resolve 优先级: override store(命令切换, 持久化) > config.catty_group_personas[gid]
             > config.catty_default_persona > "catty"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..character_card import CharacterBookEntry


@dataclass(frozen=True, slots=True)
class PersonaReplyCatalog:
    """人格专属的业务 fallback 文案与指令。"""
    slow_reply_placeholders: tuple[str, ...] = ()
    slow_reply_owner_placeholders: tuple[str, ...] = ()
    force_reply_instruction: str = ""
    no_reply_image_fallback: str = ""
    no_reply_reply_fallback: str = ""
    no_reply_mention_fallback: str = ""
    no_reply_default_fallback: str = ""
    api_timeout_reply: str = ""
    api_transport_reply: str = ""
    image_send_failure_reply: str = ""
    tool_result_follow_up_instruction: str = ""
    turtle_soup_cooldown_reply: str = ""
    turtle_soup_rule_line: str = ""
    api_key_missing_reply: str = ""
    web_search_cooldown_reply: str = ""
    web_search_failure_instruction: str = ""
    web_search_disabled_instruction: str = ""
    busy_fallback_reply: str = ""
    # 签到/积分命令的固定兜底文案 (AI caption 失败时使用)
    signin_success_fallback: str = ""
    signin_already_fallback: str = ""
    points_summary_fallback: str = ""

    @property
    def no_reply_fallbacks(self) -> tuple[str, str, str, str]:
        return (
            self.no_reply_image_fallback,
            self.no_reply_reply_fallback,
            self.no_reply_mention_fallback,
            self.no_reply_default_fallback,
        )


NEUTRAL_REPLY_CATALOG = PersonaReplyCatalog(
    slow_reply_placeholders=(
        "稍等，我在处理。",
        "正在整理，马上。",
    ),
    force_reply_instruction=(
        "刚才没有回复成功。按当前上下文直接回复用户，不要再次沉默。"
    ),
    no_reply_image_fallback="图片收到了。你想让我看哪里？",
    no_reply_reply_fallback="收到。请继续。",
    no_reply_mention_fallback="在。需要我做什么？",
    no_reply_default_fallback="收到。刚才漏回了，请继续。",
    api_timeout_reply="请求超时了，请稍后再试。",
    api_transport_reply="服务暂时连不上，请稍后再试。",
    image_send_failure_reply="图片发送失败了，请稍后再试。",
    tool_result_follow_up_instruction=(
        "请根据工具结果直接回复用户，不要复述原始数据或调用过程。"
    ),
    turtle_soup_cooldown_reply=(
        "这个群刚开过海龟汤，还剩 {remaining} 才能继续。可以先问上一题。"
    ),
    turtle_soup_rule_line="规则：只能问能用“是/否/无关”回答的问题，答案暂不公布。",
    api_key_missing_reply="还没有配置 API Key，请先在 config.json 中填写 ai.api_key。",
    web_search_cooldown_reply=(
        "{user_title}刚刚已经使用过联网搜索，还剩 {remaining}，请稍后再试。"
    ),
    web_search_failure_instruction=(
        "本轮用户明确要求联网搜索「{query}」，但本地 Google/Bing 搜索插件调用失败。"
        "请如实说明这次联网查询失败，不要编造搜索结果、链接、日期或来源；"
        "可以基于已有知识给出有限建议，并提醒用户稍后重试。"
    ),
    web_search_disabled_instruction=(
        "本轮用户要求联网搜索，但当前配置关闭了 web_search.enabled。"
        "请说明联网搜索暂时不可用。"
    ),
    busy_fallback_reply="本地服务正被游戏占用，请稍后再试。",
    signin_success_fallback="签到成功，积分已到账，请查看卡片。",
    signin_already_fallback="今天已经签过到了，明天再来。",
    points_summary_fallback="积分卡已生成，请查看。",
)


@dataclass(frozen=True)
class PersonaImagegen:
    """persona 专属画图配置(自画像参考图 + 外观锁 + planner 人格简介)。"""
    girl_tags: str                 # NAI 外观锁 tags, e.g. "1girl, purple hair, ..."
    ref_path: str                  # SFW 自画像参考图 (相对 repo 根)
    ref_nsfw_path: str = ""        # NSFW 参考图; 空 = 复用 ref_path
    ref_path_extra: tuple[str, ...] = field(default_factory=tuple)  # 额外参考图(sfw/nsfw 都会带上)
    planner_brief: str = ""        # 画图 planner (deepseek) 的人格简介段
    short_review_style: str = ""   # 发图配文的口吻说明
    # NAI Precise Reference 力度 (主人 2026-07-06: 1.0 会锁死站姿没动作, 机机降 0.9)。
    # catty 走 imagegen=None 老路径, 恒 1.0 不受影响。
    ref_strength: float = 1.0      # director_reference_strength
    ref_fidelity: float = 1.0      # 1 - secondary_strength


@dataclass(frozen=True)
class Persona:
    """单个人格的内容包 + 行为开关。

    所有 `str | None` 内容字段: None = 用笨猫默认(现有常量/builder), 保证 catty
    路径零改动; 非 None = 该 persona 的模块级常量文本。
    """
    name: str
    char_name: str                              # macro {{char}} + reply_gate 文案插值
    core_persona: str | None = None             # order 100 cache prefix base
    group_silence: str | None = None            # order 105 (仅群聊)
    first_mes: str | None = None                # 冷会话开场
    mes_example: str | None = None              # order 161 对话示例
    style_examples: str | None = None           # order 159 (catty=catgirl_examples builder)
    disambiguation_examples: str | None = None  # order 160
    character_book: "tuple[CharacterBookEntry, ...] | None" = None
    persona_reminder_text: str | None = None    # 每 N 轮贴身防漂移提醒
    reply_gate_style: str | None = None         # reply_gate prompt 里的口吻短语, e.g. "用笨猫口吻"
    owner_concept: bool = True                  # False = 无「主人」概念(称呼走中性分支)
    disabled_prompt_segments: frozenset[str] = field(default_factory=frozenset)
    disabled_features: frozenset[str] = field(default_factory=frozenset)
    imagegen: PersonaImagegen | None = None
    chat_rhythm: str | None = None              # order 153 QQ 碎句节奏段 (catty=颜文字库原文)
    # conversation key → 静态 prompt。每轮只选择当前对话的一段, 不把其它会话
    # prompt 拼入请求。内容必须是模块级常量以保持该会话 cache 稳定。
    conversation_prompts: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # 唤醒词 (主人 2026-07-06: 猫娘版是猫的, 机机版是发电机的)。
    # None = 用 config.catty_trigger_prefixes / catty_directed_keywords (catty 默认);
    # 非 None = **整组替换** config 值 (extract_incoming_message 按 scope 生效)。
    trigger_prefixes: "tuple[str, ...] | None" = None
    directed_keywords: "tuple[str, ...] | None" = None
    # 主模型覆写 (主人 2026-08-02: 机机与 catty 统一使用 flash, 不再自动升 pro)。
    # None = 用 config.catty_openai_model; 仅覆盖 OpenAI-compat 主回复路径, base_url/key 不变。
    model_override: str | None = None
    # None = 使用默认人格的模块级业务 fallback catalog，避免改变 catty 旧路径。
    reply_catalog: PersonaReplyCatalog | None = None
    # 机机 (主人 2026-08-10): 群聊非续聊窗口下只回真 @ 的消息 — 提示词/直接称呼/
    # 引用等一律不触发。False = 沿用 catty 的 mentioned/prefix/directed 多信号触发。
    mention_only_trigger: bool = False
    # 机机 (主人 2026-08-10): @ 触发回复后的续聊预算。@ 回复后最多再判断
    # 「下面 N 条消息和自己有没有关系」: 有关系 → 回复 (+1) 并消耗 1 个预算,
    # 预算归零或出现无关消息 (主 AI 判 NO_REPLY) → 关窗结束。
    # None = 旧行为 (每次回复满血刷新窗口); 机机 = 2。
    followup_reply_budget: int | None = None

    def segment_disabled(self, identifier: str) -> bool:
        return identifier in self.disabled_prompt_segments

    def feature_disabled(self, feature: str) -> bool:
        return feature in self.disabled_features

    def conversation_prompt_for(self, conversation_key: str) -> str:
        normalized = str(conversation_key or "").strip()
        if not normalized:
            return ""
        for target_conversation, prompt in self.conversation_prompts:
            if normalized == target_conversation:
                return prompt
        return ""


@dataclass(frozen=True, slots=True)
class PersonaReplyContext:
    """已解析人格 + 当前事件事实，供回复 fallback 路径复用。"""
    persona: Persona
    is_owner: bool
    reply_catalog: PersonaReplyCatalog

    @property
    def catalog(self) -> PersonaReplyCatalog:
        return self.reply_catalog

    @property
    def owner_concept(self) -> bool:
        return self.persona.owner_concept

    @property
    def owner_address_allowed(self) -> bool:
        return self.is_owner and self.owner_concept

    def feature_disabled(self, feature: str) -> bool:
        return self.persona.feature_disabled(feature)

    @property
    def placeholder_pool(self) -> tuple[str, ...]:
        if self.owner_address_allowed:
            return (
                self.reply_catalog.slow_reply_placeholders
                + self.reply_catalog.slow_reply_owner_placeholders
            )
        return self.reply_catalog.slow_reply_placeholders

    def render(self, template: str, /, **values: object) -> str:
        """渲染静态目录模板；保留字段不可覆盖，缺字段按 `KeyError` 快速失败。"""
        reserved: dict[str, object] = {
            "char": self.persona.char_name,
            "char_name": self.persona.char_name,
            "owner_address": "主人" if self.owner_address_allowed else "你",
        }
        collisions = reserved.keys() & values.keys()
        if collisions:
            names = ", ".join(sorted(collisions))
            raise ValueError(f"reserved reply template field(s): {names}")
        fields: dict[str, object] = dict(values)
        fields.update(reserved)
        return template.format_map(fields)


DEFAULT_PERSONA_NAME = "catty"

from .catty import CATTY_PERSONA, CATTY_REPLY_CATALOG  # noqa: E402
from .fadianji import FADIANJI_PERSONA, FADIANJI_REPLY_CATALOG  # noqa: E402

PERSONAS: dict[str, Persona] = {
    CATTY_PERSONA.name: CATTY_PERSONA,
    FADIANJI_PERSONA.name: FADIANJI_PERSONA,
}

# 命令/配置里的别名(中文名 → registry key)。
PERSONA_ALIASES: dict[str, str] = {
    "笨猫": "catty",
    "猫猫": "catty",
    "机机": "fadianji",
    "小机": "fadianji",
    "发电机": "fadianji",
    "不稳定发电机": "fadianji",
}


def normalize_persona_name(raw: str) -> str | None:
    """别名/大小写归一; 未知名返回 None(调用方决定 fallback 还是报错)。"""
    key = str(raw or "").strip()
    if not key:
        return None
    key = PERSONA_ALIASES.get(key, key).lower()
    return key if key in PERSONAS else None


def get_persona(name: str | None) -> Persona:
    """未知/空名 fallback catty — 人格解析永不抛异常。"""
    return PERSONAS.get(str(name or "").strip().lower(), CATTY_PERSONA)


def get_reply_catalog(persona: Persona) -> PersonaReplyCatalog:
    """返回人格目录；只有 Catty 使用 Catty fallback，其它缺省人格走中性目录。"""
    if persona.reply_catalog is not None:
        return persona.reply_catalog
    if persona.name == CATTY_PERSONA.name:
        return CATTY_REPLY_CATALOG
    return NEUTRAL_REPLY_CATALOG


def build_persona_reply_context(
    persona: Persona,
    *,
    is_owner: bool,
) -> PersonaReplyContext:
    """由已解析的 Persona 和真实 owner 事实构造上下文，不接收原始别名。"""
    return PersonaReplyContext(
        persona=persona,
        is_owner=bool(is_owner),
        reply_catalog=get_reply_catalog(persona),
    )


def resolve_persona_name(
    scope: str,
    *,
    config: Any = None,
    override_store: Any = None,
) -> str:
    """scope → persona name 单一真相源。

    scope 形如 "group:{gid}" / "group:{gid}:user:{uid}" / "private:{uid}"。
    """
    if override_store is not None and scope:
        # per-user 群会话 key ("group:{gid}:user:{uid}") 也要能命中群级覆写 ("group:{gid}")
        # — /人格 命令按 _conversation_queue_key 存, _build_messages 按 history key 查。
        candidates = [scope]
        if scope.startswith("group:"):
            parts = scope.split(":")
            if len(parts) > 2:
                candidates.append(f"group:{parts[1]}")
        for candidate in candidates:
            try:
                override = override_store.get(candidate)
            except Exception:  # noqa: BLE001
                override = None
            if override and override in PERSONAS:
                return override
    if config is not None and scope and scope.startswith("group:"):
        parts = scope.split(":")
        gid = parts[1] if len(parts) > 1 else ""
        mapping = getattr(config, "catty_group_personas", None) or {}
        mapped = normalize_persona_name(mapping.get(gid, "")) if gid else None
        if mapped:
            return mapped
    default = normalize_persona_name(getattr(config, "catty_default_persona", "") or "")
    return default or DEFAULT_PERSONA_NAME


__all__ = [
    "CATTY_REPLY_CATALOG",
    "DEFAULT_PERSONA_NAME",
    "FADIANJI_REPLY_CATALOG",
    "NEUTRAL_REPLY_CATALOG",
    "PERSONAS",
    "PERSONA_ALIASES",
    "Persona",
    "PersonaImagegen",
    "PersonaReplyCatalog",
    "PersonaReplyContext",
    "build_persona_reply_context",
    "get_persona",
    "get_reply_catalog",
    "normalize_persona_name",
    "resolve_persona_name",
]
