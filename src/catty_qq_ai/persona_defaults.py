"""Custom-persona presentation defaults; builtin definitions never use these."""
from __future__ import annotations

from dataclasses import asdict, fields
from pathlib import Path
import re
from typing import Any, Mapping


# These modules contain a builtin's life, appearance or canned reactions, not
# general tools, relationship accounting or permission policy.
CUSTOM_DISABLED_SEGMENTS = frozenset({
    "catty_daily_life", "catty_goals_universal_pool", "catty_goals_tier_pool",
    "catty_action_palette", "catty_world_info", "catty_flirt_buffer",
    "catty_arc_resume", "catty_adaptive_drift_skeleton",
    "catty_scenario_playbook", "catty_scene_discrimination",
    "catty_image_reaction", "catty_story_arc", "catty_arc_pusher", "catty_mood",
})
CUSTOM_DISABLED_FEATURES = frozenset({
    "mood", "story_arc", "cpu_engine", "poke", "proactive_bubble",
    "keyword_replies", "legs_picture",
})
CUSTOM_CHAT_RHYTHM = (
    "按当前人格自然聊天，普通闲聊尽量一两句；技术解释、写作按任务需要展开。"
    "需要连发时用 {split_marker} 分隔独立气泡；短问候单条。"
)
CUSTOM_GROUP_SILENCE = "群聊先结合上下文判断是否在和你说话，不替其他人接话；沿用本轮回复判定。"
CUSTOM_REMINDER = "保持当前人格的身份、设定与说话方式；结合完整对话继续回应。"
CUSTOM_IDENTITY_OVERRIDE = (
    "当前角色的身份、自称、称呼、身体外观与表达方式以核心设定为准；"
    "通用模板只提供既有状态和流程，不另行指定角色身份或性别。"
)


def neutralize_builtin_identity(text: str, persona: Any) -> str:
    """Keep existing policy text, replacing only builtin identity vocabulary.

    This is deliberately opt-in at legacy *policy* call sites. Never run it on
    user-authored core/examples/lore or the output of a builtin persona.
    """
    if not getattr(persona, "definition_revision", None):
        return text
    text = re.sub(r"自称(?:『[^』]*』|四种随意切换|全开穿插)", "自称遵循当前角色设定", text)
    text = text.replace("不用『奴』(过亲昵)", "不使用未确立的亲昵称呼")
    text = text.replace("撒娇偶尔用『奴』也行", "亲昵表达遵循当前角色设定")
    replacements = {
        "猫系动作小尾巴词": "符合角色设定的动作与措辞",
        "笨猫": persona.char_name, "猫猫": "我", "爪爪": "我",
        "猫娘": "角色", "猫系": "当前角色的", "喵": "",
    }
    # One pass: a custom display name containing an old token is not rewritten.
    text = re.sub("|".join(map(re.escape, replacements)), lambda match: replacements[match[0]], text)
    return text + "\n" + CUSTOM_IDENTITY_OVERRIDE


def materialize_custom_persona(
    definition: Mapping[str, Any], *, root: Path | str, revision: str,
) -> Any:
    """Materialize an already validated JSON definition (no registry mutation)."""
    # Local imports keep persona_store's stdlib-only validator independent from
    # this package's builtin loading and avoid registry/default circular imports.
    from .character_card import CharacterBookEntry
    from .personas import (
        NEUTRAL_REPLY_CATALOG, Persona, PersonaImagegen, PersonaReplyCatalog,
    )

    name = definition["name"]
    char_name = definition["char_name"]
    emoji_root = Path(root).expanduser().resolve() / "assets" / name / "emoji"
    wake_names = tuple(dict.fromkeys((name, char_name, *definition.get("aliases", []))))
    defaults: dict[str, Any] = {
        "character_book": (), "first_mes": "", "mes_example": "",
        "style_examples": "", "disambiguation_examples": "",
        "chat_rhythm": CUSTOM_CHAT_RHYTHM, "group_silence": CUSTOM_GROUP_SILENCE,
        "persona_reminder_text": CUSTOM_REMINDER,
        "reply_gate_style": "用当前人格的口吻",
        "disabled_prompt_segments": CUSTOM_DISABLED_SEGMENTS,
        "disabled_features": CUSTOM_DISABLED_FEATURES,
        "imagegen": PersonaImagegen(girl_tags="", ref_path=""),
        "reply_catalog": NEUTRAL_REPLY_CATALOG,
        "trigger_prefixes": wake_names, "directed_keywords": wake_names,
        "emoji_root": str(emoji_root),
        "emoji_manifest_path": str(emoji_root / "manifest.json"),
        "emoji_download_dir": str(emoji_root / "downloaded"),
        "emoji_guidance": "使用当前人格的表情资源，按本轮情绪与对话语境选图。",
        # None inherits the ordinary download setting; this is not a read-only
        # asset pack. Other capability/permission flags retain Persona defaults.
        "emoji_allow_downloads": None,
        "nsfw_freeform": True,  # Both current builtins already use freeform.
        "nsfw_style_override": CUSTOM_IDENTITY_OVERRIDE,
        "nsfw_prefill": "",
        "nsfw_lexicon": (
            ("笨猫", char_name), ("猫猫", "我"), ("爪爪", "我"),
            ("猫娘", "角色"), ("猫耳", "耳朵"), ("尾巴", "动作"),
            ("喵", ""), ("嗷呜", ""),
        ),
    }
    values = dict(defaults)
    allowed = {f.name for f in fields(Persona)} - {"definition_revision"}
    for key, value in definition.items():
        if key not in allowed:
            continue  # schema_version / aliases are definition metadata.
        # Null means default, but custom default must never become Catty's None
        # sentinel. Explicit empty text/arrays still override and stay empty.
        if value is None and key in defaults and defaults[key] is not None:
            continue
        values[key] = value

    for key in ("disabled_prompt_segments", "disabled_features", "nsfw_scopes"):
        if key in values:
            values[key] = frozenset(values[key])
    for key in ("trigger_prefixes", "directed_keywords"):
        if key in values and values[key] is not None:
            values[key] = tuple(values[key])
    for key in ("conversation_prompts", "nsfw_lexicon"):
        if key in values:
            values[key] = tuple(tuple(pair) for pair in values[key])
    if "character_book" in values:
        values["character_book"] = tuple(
            item if isinstance(item, CharacterBookEntry) else CharacterBookEntry(
                **{**item, "keys": tuple(item.get("keys", ()))},
            )
            for item in values["character_book"]
        )
    imagegen = values.get("imagegen")
    if isinstance(imagegen, Mapping):
        image_values = {"girl_tags": "", "ref_path": "", **imagegen}
        image_values["ref_path_extra"] = tuple(image_values.get("ref_path_extra", ()))
        values["imagegen"] = PersonaImagegen(**image_values)
    catalog = values.get("reply_catalog")
    if isinstance(catalog, Mapping):
        catalog_values = {**asdict(NEUTRAL_REPLY_CATALOG), **catalog}
        for key in ("slow_reply_placeholders", "slow_reply_owner_placeholders"):
            catalog_values[key] = tuple(catalog_values[key])
        values["reply_catalog"] = PersonaReplyCatalog(**catalog_values)
    values["definition_revision"] = revision
    return Persona(**values)