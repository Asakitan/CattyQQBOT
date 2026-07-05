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


@dataclass(frozen=True)
class PersonaImagegen:
    """persona 专属画图配置(自画像参考图 + 外观锁 + planner 人格简介)。"""
    girl_tags: str                 # NAI 外观锁 tags, e.g. "1girl, purple hair, ..."
    ref_path: str                  # SFW 自画像参考图 (相对 repo 根)
    ref_nsfw_path: str = ""        # NSFW 参考图; 空 = 复用 ref_path
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
    # 唤醒词 (主人 2026-07-06: 猫娘版是猫的, 机机版是发电机的)。
    # None = 用 config.catty_trigger_prefixes / catty_directed_keywords (catty 默认);
    # 非 None = **整组替换** config 值 (extract_incoming_message 按 scope 生效)。
    trigger_prefixes: "tuple[str, ...] | None" = None
    directed_keywords: "tuple[str, ...] | None" = None

    def segment_disabled(self, identifier: str) -> bool:
        return identifier in self.disabled_prompt_segments

    def feature_disabled(self, feature: str) -> bool:
        return feature in self.disabled_features


DEFAULT_PERSONA_NAME = "catty"

from .catty import CATTY_PERSONA  # noqa: E402
from .fadianji import FADIANJI_PERSONA  # noqa: E402

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
    "DEFAULT_PERSONA_NAME",
    "PERSONAS",
    "PERSONA_ALIASES",
    "Persona",
    "PersonaImagegen",
    "get_persona",
    "normalize_persona_name",
    "resolve_persona_name",
]
