"""SillyTavern 风 PromptManager - 带 identifier / order / enabled 的 system prompt 注册器。

设计目标:
- 每条 prompt 段有唯一 identifier + 排序 order,运行时按 order 升序合并
- 通过 config.catty_prompt_order 可以覆盖默认 order(JSON 配,不改代码)
- 通过 config.catty_prompts_disabled 可以单独关任意段
- 段内容用 lazy callable 提供(只有被 enable 才计算,省钱)
- 输出 list[{"role": "system", "content": ...}] 直接 extend 进 messages

参考 ST default Default.json 的 prompt_order:
    main → worldInfoBefore → personaDescription → charDescription → charPersonality
    → scenario → enhanceDefinitions → nsfw → worldInfoAfter → dialogueExamples
    → chatHistory → jailbreak(post_history_instructions)

我们的 catty identifier 映射(对齐 ST):

    | order | identifier                  | ST 类比          | 来源 |
    |-------|-----------------------------|------------------|------|
    |   100 | catty_main_intel            | main             | persona_prompts.build_reply_intelligence_prompt |
    |   110 | catty_identity_anchor       | (no anchor in ST)| persona_prompts.IDENTITY_ANCHOR_PROMPT |
    |   120 | catty_char_description      | charDescription  | character_card.get_description |
    |   130 | catty_char_personality      | charPersonality  | character_card.get_personality |
    |   140 | catty_scenario              | scenario         | character_card.get_scenario |
    |   150 | catty_persona_memory        | (combined)       | persona_prompts.build_persona_memory_prompt |
    |   160 | catty_group_meme_literacy   | (extra)          | persona_prompts.build_group_meme_literacy_prompt |
    |   170 | catty_conversation_flow     | (extra)          | persona_prompts.build_conversation_flow_prompt |
    |   180 | catty_semantic_perception   | (extra)          | persona_prompts.build_semantic_perception_prompt |
    |   190 | catty_scenario_playbook     | (extra)          | persona_prompts.build_scenario_playbook_prompt |
    |   195 | catty_scene_discrimination  | (extra)          | persona_prompts.build_scene_discrimination_prompt |
    |   200 | catty_daily_life            | (catty-specific) | daily_life.build_daily_life_prompt |
    |   210 | catty_qq_chat_rhythm        | (extra)          | persona_prompts.build_qq_chat_rhythm_prompt |
    |   220 | catty_reply_self_check      | (extra)          | persona_prompts.build_reply_self_check_prompt |
    |   230 | catty_image_literacy        | (conditional)    | persona_prompts.build_image_literacy_prompt |
    |   240 | catty_catgirl_examples      | dialogueExamples | persona_prompts.build_catgirl_examples_prompt |
    |   250 | catty_disambiguation        | (extra)          | persona_prompts.build_disambiguation_examples_prompt |
    |   300 | catty_world_info            | worldInfoAfter   | world_info.build_world_info_block |
    |   320 | catty_mes_example           | dialogueExamples | character_card.get_mes_example |
    |   350 | catty_story_arc             | (catty-specific) | story_arc.build_story_arc_prompt |
    |   500 | catty_post_history          | jailbreak        | character_card.get_post_history |

注:
- catty_post_history 走 prepend(role=system + content_only),
  但**真正紧贴 user 最后一条**的注入由 author_note.inject_author_note 完成,
  这里只是 prompt 链路最后的保险段
- chatHistory 在 PromptManager **之外**追加(messages.extend(history) + 当前 user message)

JSON 配置示例(config.json 顶层):
    {
      "prompt_order": ["catty_main_intel", "catty_char_description", "catty_world_info"],
      "prompts_disabled": ["catty_daily_life"]
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class PromptEntry:
    identifier: str
    order: int = 100
    role: str = "system"
    enabled: bool = True
    content_fn: Callable[[], str] | None = None  # lazy 计算,启用时才跑

    def materialize(self) -> dict[str, str] | None:
        """生成 {"role": ..., "content": ...} 或 None(空内容/失败)。"""
        if not self.enabled or self.content_fn is None:
            return None
        try:
            content = self.content_fn()
        except Exception:  # noqa: BLE001
            return None
        if not content or not str(content).strip():
            return None
        return {"role": self.role, "content": str(content)}


class PromptManager:
    """register-then-build 模式。每次 build 调用都新建一份 entries 列表。

    不持久化、不全局单例 — __init__.py handle_chat 每轮 new 一个 manager,
    register 完之后 build_messages() 拿到结果。
    """

    def __init__(self) -> None:
        self._entries: list[PromptEntry] = []

    def register(
        self,
        identifier: str,
        content_fn: Callable[[], str],
        *,
        order: int = 100,
        role: str = "system",
        enabled: bool = True,
    ) -> None:
        self._entries.append(PromptEntry(
            identifier=identifier, order=order, role=role,
            enabled=enabled, content_fn=content_fn,
        ))

    def register_static(
        self,
        identifier: str,
        content: str,
        *,
        order: int = 100,
        role: str = "system",
    ) -> None:
        """已经计算好的字符串直接注册(空字符串自动跳过)。

        和 register() 的区别是 content 已是字符串而非 callable,
        适合 LayerD/E 各种已经在外部完成 build 的 context。
        """
        if not content or not str(content).strip():
            return
        self._entries.append(PromptEntry(
            identifier=identifier, order=order, role=role,
            enabled=True, content_fn=lambda c=content: c,
        ))

    def apply_config(
        self,
        *,
        order_override: list[str] | None = None,
        disabled: list[str] | None = None,
    ) -> None:
        """根据 config.json 调 order 和 enabled。

        order_override 是 identifier 列表,出现在列表里的按列表位置(乘 100)排序,
        没出现的保持原 order。disabled 里的 identifier 直接 enabled=False。
        """
        disabled_set = {s.strip() for s in (disabled or []) if str(s).strip()}
        if disabled_set:
            for e in self._entries:
                if e.identifier in disabled_set:
                    e.enabled = False
        if order_override:
            position_map = {ident.strip(): (i + 1) * 100 for i, ident in enumerate(order_override) if str(ident).strip()}
            for e in self._entries:
                if e.identifier in position_map:
                    e.order = position_map[e.identifier]

    def build_messages(self) -> list[dict[str, str]]:
        """按 order 升序输出非空 entry 的 system messages。"""
        sorted_entries = sorted(self._entries, key=lambda e: (e.order, e.identifier))
        out: list[dict[str, str]] = []
        for entry in sorted_entries:
            mat = entry.materialize()
            if mat is not None:
                out.append(mat)
        return out

    def __len__(self) -> int:
        return sum(1 for e in self._entries if e.enabled)


# ── 统一的 catty 人格注册器 ───────────────────────────────────────────────
# 把 character_card + persona_prompts + 三个 ST 风新模块 全部注册进 manager,
# 调用方只需 register_catty_persona(manager, ctx) + apply_config + build_messages。


def register_catty_persona(
    mgr: PromptManager,
    ctx: dict[str, Any],
) -> None:
    """把笨猫的所有 ST 风段注册到 manager。

    ctx 字典里要有:
        config            : Config 对象(用于读 catty_parsing_layers_disabled 兼容老开关)
        scope             : str  conversation queue key
        user_text         : str  当前消息文本(world_info 关键词扫描用)
        user_display      : str  对方称呼字面(macros render 用)
        affection_level   : int
        is_owner          : bool
        has_image         : bool (决定要不要挂 image_literacy)
        story_arc_store   : StoryArcStore | None
        no_reply_marker   : str
        reply_split_marker: str
        system_prompt     : str  (config.catty_system_prompt 原文,作为 persona_memory base)

    每段都是 lazy(content_fn),被 disable 不会被调用。
    """
    from . import character_card as _cc
    from . import daily_life as _dl
    from . import persona_prompts as _pp
    from . import story_arc as _sa
    from . import world_info as _wi

    cfg = ctx["config"]
    scope = ctx["scope"]
    user_text = ctx.get("user_text", "") or ""
    user_display = ctx.get("user_display", "用户")
    aff_level = int(ctx.get("affection_level", 0))
    is_owner = bool(ctx.get("is_owner", False))
    has_image = bool(ctx.get("has_image", False))
    arc_store = ctx.get("story_arc_store")
    no_reply = ctx.get("no_reply_marker", "")
    split_marker = ctx.get("reply_split_marker", "")
    system_prompt = ctx.get("system_prompt", "") or ""
    is_cold_session = bool(ctx.get("is_cold_session", False))
    self_check_enabled = bool(ctx.get("reply_self_check_enabled", True))
    style_examples_enabled = bool(ctx.get("reply_style_examples_enabled", True))
    # 完整 macro ctx,传给 character_card 各段做 {{user}}/{{date}}/{{idleDuration}} 等替换
    macro_ctx = {
        "char": "笨猫",
        "user": user_display,
        "group": ctx.get("group_display", ""),
        "last_user_message": ctx.get("last_user_message", ""),
        "last_char_message": ctx.get("last_char_message", ""),
        "last_active_at": ctx.get("last_active_at"),
    }

    # 兼容老开关:catty_parsing_layers_disabled 里的 daily_life/world_info/story_arc 依然生效
    legacy_disabled = set(getattr(cfg, "catty_parsing_layers_disabled", None) or [])

    # === ST main / charDescription / charPersonality / scenario (固定挂) ===
    mgr.register(
        "catty_main_intel",
        content_fn=lambda: _pp.build_reply_intelligence_prompt(no_reply),
        order=100,
    )
    mgr.register(
        "catty_identity_anchor",
        content_fn=lambda: _pp.IDENTITY_ANCHOR_PROMPT,
        order=110,
    )
    mgr.register(
        "catty_char_description",
        content_fn=lambda: _cc.get_description(ctx=macro_ctx, user_display=user_display),
        order=120,
    )
    mgr.register(
        "catty_char_personality",
        content_fn=lambda: _cc.get_personality(ctx=macro_ctx),
        order=130,
    )
    mgr.register(
        "catty_scenario",
        content_fn=lambda: _cc.get_scenario(ctx=macro_ctx),
        order=140,
    )
    if system_prompt.strip():
        mgr.register(
            "catty_persona_memory",
            content_fn=lambda: _pp.build_persona_memory_prompt(system_prompt),
            order=150,
        )

    # === 群聊/对话流/语义/场景 playbook (一坨补充) ===
    mgr.register(
        "catty_group_meme_literacy",
        content_fn=lambda: _pp.build_group_meme_literacy_prompt(),
        order=160,
    )
    mgr.register(
        "catty_conversation_flow",
        content_fn=lambda: _pp.build_conversation_flow_prompt(),
        order=170,
    )
    mgr.register(
        "catty_semantic_perception",
        content_fn=lambda: _pp.build_semantic_perception_prompt(),
        order=180,
    )
    mgr.register(
        "catty_scenario_playbook",
        content_fn=lambda: _pp.build_scenario_playbook_prompt(no_reply),
        order=190,
    )
    mgr.register(
        "catty_scene_discrimination",
        content_fn=lambda: _pp.build_scene_discrimination_prompt(no_reply),
        order=195,
    )

    # === ST 风新模块: daily_life / world_info / story_arc ===
    if "daily_life" not in legacy_disabled:
        mgr.register(
            "catty_daily_life",
            content_fn=lambda: _dl.build_daily_life_prompt(scope),
            order=200,
        )
    if "world_info" not in legacy_disabled:
        mgr.register(
            "catty_world_info",
            content_fn=lambda: _wi.build_world_info_block(
                user_text, scope, position="after_char",
                affection_level=aff_level, is_owner=is_owner,
            ),
            order=300,
        )
    if "story_arc" not in legacy_disabled and arc_store is not None:
        mgr.register(
            "catty_story_arc",
            content_fn=lambda: _sa.build_story_arc_prompt(arc_store.get_active(scope)),
            order=350,
        )

    # === QQ 节奏 + 自检 + image + 示例 (后段) ===
    mgr.register(
        "catty_qq_chat_rhythm",
        content_fn=lambda: _pp.build_qq_chat_rhythm_prompt(split_marker),
        order=210,
    )
    if self_check_enabled:
        mgr.register(
            "catty_reply_self_check",
            content_fn=lambda: _pp.build_reply_self_check_prompt(no_reply, split_marker),
            order=220,
        )
    if has_image:
        mgr.register(
            "catty_image_literacy",
            content_fn=lambda: _pp.build_image_literacy_prompt(),
            order=230,
        )
    # 示例对话只在冷会话(<HOT_SESSION 阈值)注入 — 热会话从历史里就能学到口吻,
    # 这两段加起来 ~1.5K token,省下 30-40% system prompt 体积。
    if style_examples_enabled and is_cold_session:
        mgr.register(
            "catty_catgirl_examples",
            content_fn=lambda: _pp.build_catgirl_examples_prompt(no_reply, split_marker),
            order=240,
        )
        mgr.register(
            "catty_disambiguation",
            content_fn=lambda: _pp.build_disambiguation_examples_prompt(no_reply),
            order=250,
        )
        mgr.register(
            "catty_mes_example",
            content_fn=lambda: _cc.get_mes_example(ctx=macro_ctx, user_display=user_display),
            order=320,
        )

    # === User Vibe Profile - 对方画像 (order=460 在 anti_repetition 之前) ===
    user_vibe_store = ctx.get("user_vibe_store")
    user_id = ctx.get("user_id", "")
    if user_vibe_store is not None and user_id:
        from . import user_vibe as _uv
        mgr.register(
            "catty_user_vibe",
            content_fn=lambda: _uv.build_user_vibe_prompt(
                user_vibe_store.profile_for(user_id),
                user_display=user_display,
            ),
            order=460,
        )

    # === Anti-Repetition Tracker - 防复读 (order=480 紧贴 post_history 之前) ===
    from . import anti_repetition as _ar
    mgr.register(
        "catty_anti_repetition",
        content_fn=lambda: _ar.build_anti_repetition_prompt(scope),
        order=480,
    )

    # === post_history (jailbreak slot) - 最末 ===
    mgr.register(
        "catty_post_history",
        content_fn=lambda: _cc.get_post_history(ctx=macro_ctx),
        order=500,
    )


__all__ = [
    "PromptEntry",
    "PromptManager",
    "register_catty_persona",
]
