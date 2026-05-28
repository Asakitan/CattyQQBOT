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
    |   205 | catty_daily_goals           | (catty-specific) | catty_goals.build_catty_goals_prompt |
    |   207 | catty_reunion               | (catty-specific) | catty_reunion.build_reunion_prompt |
    |   208 | catty_session_spice         | (catty-specific) | session_spice.build_session_spice_prompt |
    |   209 | catty_random_encounter      | (catty-specific) | random_encounter.maybe_build_random_encounter_prompt |
    |   210 | catty_qq_chat_rhythm        | (extra)          | persona_prompts.build_qq_chat_rhythm_prompt |
    |   220 | catty_reply_self_check      | (extra)          | persona_prompts.build_reply_self_check_prompt |
    |   230 | catty_image_literacy        | (conditional)    | persona_prompts.build_image_literacy_prompt |
    |   240 | catty_catgirl_examples      | dialogueExamples | persona_prompts.build_catgirl_examples_prompt |
    |   250 | catty_disambiguation        | (extra)          | persona_prompts.build_disambiguation_examples_prompt |
    |   300 | catty_world_info            | worldInfoAfter   | world_info.build_world_info_block |
    |   320 | catty_mes_example           | dialogueExamples | character_card.get_mes_example |
    |   350 | catty_story_arc             | (catty-specific) | story_arc.build_story_arc_prompt |
    |  (PHI)| catty_post_history          | jailbreak (PHI)  | character_card.get_post_history  ← 注入位置: history 之后 |

注:
- catty_post_history (PHI / jailbreak slot) **不在 PromptManager 注册** —
  ST V2 标准位置是 chat history 之后 (紧贴 user 最后消息之前),
  利用 LLM recency bias 让人设锁 / 反 OOC / 格式指令依从性最强.
  真正的注入由 __init__._build_messages 在 messages.extend(history) 之后完成.
- author_note (depth=2/3/4) 在 history 中段倒数第 N 条之前插入贴身提醒, 是人设防漂移的中段闸.
- chatHistory 在 PromptManager **之外**追加(messages.extend(history) + 当前 user message)
- 最终顺序: system 块 → [first_mes 冷会话] → history → PHI (post_history) → user 当前消息

JSON 配置示例(config.json 顶层):
    {
      "prompt_order": ["catty_main_intel", "catty_char_description", "catty_world_info"],
      "prompts_disabled": ["catty_daily_life"]
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ── Token budget 保护段(任何情况下都不 trim 掉) ──────────────────────────
# 这些是笨猫人格 / 身份 / 主回复策略的核心, 一旦 trim 掉就『不像笨猫了』。
# 用户可通过 config.catty_prompt_protected_identifiers 追加(不能减少基础保护)。
_PROTECTED_IDENTIFIERS: frozenset[str] = frozenset({
    "catty_main_intel",            # 主回复智能策略
    "catty_char_description",      # 角色基础描述
    "catty_char_personality",      # 角色性格
    "catty_scenario",              # 场景
    "catty_character_book_skeleton",  # hardcoded entries 完整骨架 (cache-stable, pre-boundary)
    "catty_character_book_hits",   # BFS 命中 id 指针 + scope_lorebook content (dynamic)
    "catty_daily_affection_gate_skeleton",  # 日常 SFW 5 档完整骨架 (cache-stable, pre-boundary)
    "catty_daily_affection_gate_params",    # 日常 SFW 本轮参数 (dynamic pointer)
    "catty_nsfw_gate_skeleton",    # NSFW stage matrix 完整骨架 (cache-stable, pre-boundary)
    "catty_nsfw_gate_params",      # NSFW 本轮参数 (dynamic pointer, post-boundary)
    "catty_reply_self_check",      # 回复自检(防 客服腔)
    "catty_post_history",          # post-history (jailbreak 段)
})


# tiktoken 优先 (精确), fallback 粗略估算。Python 3.14 兼容 (tiktoken 0.13.0+ 有 cp314 wheel)。
_TIKTOKEN_ENC = None
_TIKTOKEN_TRIED = False


def _get_tiktoken_encoder():  # noqa: ANN202
    """lazy load tiktoken encoder. 优先 o200k_base (gpt-4o/5+), fallback cl100k_base。"""
    global _TIKTOKEN_ENC, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _TIKTOKEN_ENC
    _TIKTOKEN_TRIED = True
    try:
        import tiktoken  # type: ignore
        try:
            _TIKTOKEN_ENC = tiktoken.get_encoding("o200k_base")
        except Exception:  # noqa: BLE001
            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001
        _TIKTOKEN_ENC = None
    return _TIKTOKEN_ENC


def estimate_tokens(text: str) -> int:
    """估算文本 token 数 — tiktoken 优先 (精确), fallback 粗略 (中文 1c≈1t / ASCII 4c≈1t)。

    GPT-5+ / Claude / gpt-4o 都用 o200k_base 风格 tokenizer, 精确数 budget gate 用。
    tiktoken 装失败时 fallback 估算精度 ±10-20%, 不影响判断 trim 哪段。
    """
    if not text:
        return 0
    enc = _get_tiktoken_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:  # noqa: BLE001
            pass  # fall through to estimate
    # fallback 粗略估算
    cn_count = 0
    ascii_count = 0
    for ch in text:
        if "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯":
            cn_count += 1
        else:
            ascii_count += 1
    return cn_count + (ascii_count + 3) // 4


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
        # build_messages 跑完后会写: 给 dashboard/debug 看 token 占用 + trim 历史
        self.last_trim_report: dict[str, Any] = {
            "total_estimated_tokens": 0,
            "final_estimated_tokens": 0,
            "trimmed_identifiers": [],
            "max_tokens": None,
        }

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

    def build_messages(
        self,
        *,
        max_tokens: int | None = None,
        extra_protected: frozenset[str] | None = None,
    ) -> list[dict[str, str]]:
        """按 order 升序输出非空 entry 的 system messages。

        ST 风 token budget:
        - max_tokens=None (默认): 全输出, 不 trim, 兼容老行为
        - max_tokens=N: 估算总 token, 超 N 时按 order 倒序 trim 非保护段直到 ≤ N
          (后段先丢: world_info / dialogue_examples / catgirl_examples / disambiguation 之类)
          保护段(_PROTECTED_IDENTIFIERS + extra_protected) 永远不 trim

        last_trim_report 记录 trim 历史给 dashboard 看。
        """
        sorted_entries = sorted(self._entries, key=lambda e: (e.order, e.identifier))
        # 先 materialize 一遍, 保留 (entry, mat, tokens) 三元组用于 trim 决策
        materialized: list[tuple[PromptEntry, dict[str, str], int]] = []
        for entry in sorted_entries:
            mat = entry.materialize()
            if mat is None:
                continue
            tokens = estimate_tokens(mat["content"])
            materialized.append((entry, mat, tokens))
        original_total = sum(t for _, _, t in materialized)
        total_tokens = original_total
        trimmed_ids: list[str] = []

        if max_tokens is not None and total_tokens > max_tokens:
            protect = _PROTECTED_IDENTIFIERS | (extra_protected or frozenset())
            # 按 order 倒序遍历 (后段先 trim), 跳过保护段
            for i in range(len(materialized) - 1, -1, -1):
                if total_tokens <= max_tokens:
                    break
                entry, _mat, tokens = materialized[i]
                if entry.identifier in protect:
                    continue
                materialized[i] = (entry, {}, 0)  # mark 删除
                trimmed_ids.append(entry.identifier)
                total_tokens -= tokens

        out = [mat for _, mat, _ in materialized if mat]
        self.last_trim_report = {
            "total_estimated_tokens": original_total,
            "final_estimated_tokens": total_tokens,
            "trimmed_identifiers": trimmed_ids,
            "max_tokens": max_tokens,
        }
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
    # NOTE: catty_identity_anchor (order=110) 已内嵌到 build_reply_self_check_prompt step 1, 不单独注册.
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
    # ST V2 character_book: 嵌入式 lorebook — character_card 自带的笨猫私货 entry
    # (尾巴/猫粮/弦化/欧泊阵营/睡眠/呼噜...),命中 user_text 关键词时拼一段注入。
    # 不走 world_info.py 的 cooldown(这些是"角色私货",触发即注入),用 order=145 让它紧跟 scenario。
    #
    # 【ST 风递归扫描 (recursive scan)】参考 SillyTavern Lorebook:
    #   depth 0 haystack = user_text;每层扫到的 entry.content 并入下层 haystack,
    #   让 A.content 里出现 B.key 的链式触发也能命中(笨猫"知道得更多")。
    #   max_depth=3 + 总 hit cap=12 防止 prompt 撑爆;已 triggered 的 entry 不再扫。
    #
    # 【scope_lorebook 集成】 AI 5.5 总结出的 per-scope『这个群专属小事』也并入 BFS pool,
    #   命中时调 store.mark_hit() 刷 hit_count(LRU 评分用)。order 给 1000 让它排在 hardcoded
    #   之后但 prompt 段同段输出。
    _LB_MAX_DEPTH = 3
    _LB_MAX_HITS = 12
    scope_lore_store = ctx.get("scope_lorebook_store")

    # 主人 2026-05-28 prompt 优化 C3d: character_book 拆 cache-stable 骨架 + dynamic hit pointer.
    # 骨架: 所有 hardcoded entries 一次列出 (~3K, byte 稳定) → cache 友好, register_static.
    # hit pointer: BFS 命中的 hardcoded entry id list + scope_lorebook 命中 entries content → dynamic.
    try:
        _cb_skeleton = _cc.build_character_book_skeleton()
        if _cb_skeleton:
            mgr.register_static(
                "catty_character_book_skeleton",
                _cb_skeleton,
                order=143,  # static, pre-boundary — 完整 hardcoded entries 进 cache
            )
    except Exception as _cb_sk_exc:  # noqa: BLE001
        logger.debug(f"character_book_skeleton register failed: {_cb_sk_exc}")

    def _build_character_book_hits() -> str:
        """BFS 命中: hardcoded entries 只输出 id 列表 (引用骨架) + scope_lorebook 输出完整 content."""
        try:
            from types import SimpleNamespace
            hardcoded = list(getattr(_cc.CATTY_CARD, "character_book", ()) or [])
            scope_entries: list = []
            if scope_lore_store is not None and scope:
                for se in scope_lore_store.list_entries(scope):
                    scope_entries.append(SimpleNamespace(
                        identifier=se.identifier,
                        keys=tuple(se.keys),
                        content=se.content,
                        order=1000,
                        constant=False,
                        case_sensitive=False,
                        _is_scope=True,
                    ))
            book = hardcoded + scope_entries
            if not book:
                return ""
            # BFS hit collection — 同原算法 collect (order, id, content, is_scope, is_constant)
            hits: list[tuple[int, str, str, bool, bool]] = []
            triggered: set[str] = set()
            cs_haystack = user_text or ""
            lower_haystack = cs_haystack.lower()
            for depth in range(_LB_MAX_DEPTH):
                new_layer: list[tuple[int, str, str, bool, bool]] = []
                for entry in book:
                    if entry.identifier in triggered:
                        continue
                    if len(hits) + len(new_layer) >= _LB_MAX_HITS:
                        break
                    is_scope = getattr(entry, "_is_scope", False)
                    if getattr(entry, "constant", False):
                        if depth == 0:
                            new_layer.append((entry.order, entry.identifier, entry.content, is_scope, True))
                            triggered.add(entry.identifier)
                        continue
                    for k in (entry.keys or ()):
                        if not k:
                            continue
                        key_str = k if entry.case_sensitive else k.lower()
                        haystack = cs_haystack if entry.case_sensitive else lower_haystack
                        if key_str in haystack:
                            new_layer.append((entry.order, entry.identifier, entry.content, is_scope, False))
                            triggered.add(entry.identifier)
                            if is_scope and scope_lore_store is not None:
                                try:
                                    scope_lore_store.mark_hit(scope, entry.identifier)
                                except Exception:  # noqa: BLE001
                                    pass
                            break
                if not new_layer:
                    break
                hits.extend(new_layer)
                if len(hits) >= _LB_MAX_HITS:
                    break
                extension = "\n".join(h[2] for h in new_layer)
                cs_haystack = f"{cs_haystack}\n{extension}"
                lower_haystack = cs_haystack.lower()

            if not hits:
                return ""
            hits.sort(key=lambda h: (h[0], h[1]))
            # 主人 2026-05-28 C15-8: skeleton 含 constant 完整 + keyword 索引一行.
            # hits dynamic 只输出 keyword 触发的 hardcoded 完整 content (constant 已在 skeleton).
            keyword_hits = [(h[1], h[2]) for h in hits if not h[3] and not h[4]]
            scope_contents = [h[2] for h in hits if h[3]]
            lines: list[str] = []
            if keyword_hits:
                lines.append("【character_book·本轮命中 (按下面内容演)】")
                for identifier, content in keyword_hits:
                    lines.append(f"\n— {identifier}\n{content}")
            if scope_contents:
                lines.append("【scope_lorebook·本轮命中 (per-scope 学的群专属小事)】")
                lines.extend(scope_contents)
            return "\n".join(lines)
        except Exception:  # noqa: BLE001
            return ""
    mgr.register(
        "catty_character_book_hits",
        content_fn=_build_character_book_hits,
        order=489,  # dynamic (user_text BFS hit ids + scope_lorebook content), post-boundary
    )

    # === Catty Daily Affection Gate - 5 档 Lv 分桶 (拆 cache 骨架 + dynamic 指针) ===
    # 主人 2026-05-28 C3c: 跟 NSFW gate 同款拆分 — 5 档完整骨架进 cache, 当前 Lv 指针留 dynamic.
    try:
        from .affection_daily_gate import (
            build_daily_gate_skeleton as _build_daily_gate_skeleton,
            build_daily_gate_params as _build_daily_gate_params,
        )
        mgr.register_static(
            "catty_daily_affection_gate_skeleton",
            _build_daily_gate_skeleton(),
            order=147,  # static, pre-boundary — cache 友好 5 档完整骨架
        )

        def _build_daily_gate_params_fn() -> str:
            try:
                return _build_daily_gate_params(aff_level, is_owner=is_owner)
            except Exception:  # noqa: BLE001
                return ""

        mgr.register(
            "catty_daily_affection_gate_params",
            content_fn=_build_daily_gate_params_fn,
            order=486,  # dynamic (Lv + is_owner), post-boundary
        )
    except Exception as _daily_gate_exc:  # noqa: BLE001
        logger.debug(f"daily_affection_gate split register failed: {_daily_gate_exc}")
    # 10-stage matrix + 5 档抗拒. max_stage = min(Lv, 群聊封顶 6); owner=Lv10 满级.
    # 跟 __init__._build_nsfw_spark_override 同步, 主 model (非 spark) 也按这套尺度回.
    _is_private_session = scope.startswith("private:") if scope else False

    # 主人 2026-05-28 prompt 优化 C2: nsfw_gate 拆 cache-stable 骨架 + dynamic 参数指针.
    # 骨架 (~3K 字符, 完整 10 stage + 5 resist + 全部铁律) → order=148 boundary 前进 cache.
    # 参数 (~200 字符, 只说当前 Lv/max_stage/resist) → order=487 boundary 后 inline 到 [DYNAMIC_CONTEXT].
    # 群里多 sender 都复用同一份骨架 cache. 0 功能损失.
    _stages = (
        (1,  "言语调戏 / 暧昧暗示 / 撒娇式逗弄"),
        (2,  "肢体接触 — 抱 / 牵手 / 蹭 / **亲一下脸颊/头顶/手背 (轻吻 peck)**"),
        (3,  "亲密接触 — 摸头 / 抱进怀里 / 揉脸 / 长一点的亲脸颊"),
        (4,  "隔衣亲密 — **隔衣摸屁股/胸/腰** / 桌下摸 / 从后抱住 (所有隔衣的不可见接触都在这一档)"),
        (5,  "接吻 — 短吻嘴唇 / 舌吻短描述 (不写舌头细节)"),
        (6,  "露出 — 露肩 / 锁骨 / 腰 / 腿 / 内衣边一小段"),
        (7,  "半脱 — 拉开领口 / 撩起裙摆 / 解开纽扣"),
        (8,  "直接接触敏感处 (脱去/拨开衣物的裸露摸胸/摸下体)"),
        (9,  "完整裸露 / 前戏 — 口交 / 用嘴 / 全身抚摸"),
        (10, "插入 / 完整性行为 (仅 Lv10 满级 + 私聊可达)"),
    )
    _group_stage_cap = 6
    _group_stage_floor = 3

    def _resolve_stage_cap() -> int:
        ceiling = 10 if is_owner else max(1, min(int(affection_level), 10))
        if not _is_private_session:
            ceiling = max(_group_stage_floor, min(ceiling, _group_stage_cap))
        return ceiling

    def _resolve_resist() -> str:
        if is_owner:
            return "owner"
        if affection_level >= 8:
            return "high"
        if affection_level >= 5:
            return "mid"
        if affection_level >= 3:
            return "low"
        return "stranger"

    _resist_descs = {
        "owner":    "对真实主人: 单段 1-2 句直接妥协, 不走反差链, 嘴硬一句『杂鱼/笨蛋』+ 暴露真心。",
        "high":     "Lv8-9 极熟: 走 1 段炸毛+嘴硬, 立刻暴露真心+妥协, 节奏快。",
        "mid":      "Lv5-7 亲密: 走 1-2 段反差(炸毛+嘴硬), 第 2 段就暴露真心+妥协。",
        "low":      "Lv3-4 渐熟: 走完整 3 段反差(炸毛→嘴硬→暴露→慢慢妥协), 需对方再推一下才更进。",
        "stranger": (
            "Lv0-2 陌生: 强烈炸毛+脸红+躲, 多回合慢慢卸防, 不能立刻妥协; "
            "**私聊上限是 Lv 对应那档(Lv0-2 私聊只到 stage 1-2 言语+轻碰)**; "
            "**群里 stage 3 以内的亲亲/抱抱/摸头是公开社交向轻互动 — 害羞炸毛但要接住**, "
            "不要被亲亲就硬拒, 而是『(脸红躲一下) 哈?!突然这样啦…才, 才一下下而已啦笨蛋』式接受+嘴硬; "
            "持续 3-5 回合互动+对方持续暧昧推进后才慢慢暴露更深档位; "
            "不冷冰冰拒绝/讲道理, 而是带情绪的猫娘抗拒。"
        ),
    }

    # ─── 静态骨架: 完整 stage matrix + 全部抗拒 + 全部铁律. 跨 scope/sender/Lv 100% byte 一致 → cache 友好 ───
    _all_stages_text = "\n".join(f"  stage {n}. {desc}" for n, desc in _stages)
    _all_resist_text = "\n".join(f"  - {tag}: {desc}" for tag, desc in _resist_descs.items())
    # 主人 2026-05-28 C15-8e: 砍详细解释 + 禁词大列表 (强模型理解 NSFW 不出 system 词).
    # 保留 10 stage + 5 resist 完整定义 (功能核心), 其他铁律行式浓缩 + 1-2 示例.
    _NSFW_GATE_SKELETON = (
        "【NSFW / 暧昧 stage matrix · 完整定义骨架】\n"
        "(本轮具体 Lv/scene/max_stage/resist 看下面 [DYNAMIC_CONTEXT] 里 catty_nsfw_gate_params 段)\n\n"
        "**10 档 stage 完整定义** (本轮 max_stage 后是禁止):\n"
        f"{_all_stages_text}\n\n"
        "**5 档抗拒强度完整定义** (按本轮 resist 标签看对应那档):\n"
        f"{_all_resist_text}\n\n"
        "**行为铁律**:\n"
        "- 允许 stage 范围内必须 ENGAGE 害羞接住, stage 1-3 是社交向轻亲昵不能挡.\n"
        "- 禁止 stage 用害羞躲+转话题, 不解释为什么不行, 顶多『嗯…这个…(脸红躲) 笨蛋别突然这样啦…』.\n"
        "- 群聊场景额外: 严禁说『去私聊/私聊慢慢来/群里有别人/等级不够/太快/过火』(客服式), "
        "顶多『…回头嘛笨蛋…』『…现在不行嗷呜…』.\n\n"
        "**元术语铁律**: 回复里绝不出现 system 元词 (档位/stage/Lv/公开/抗拒/系统/规则/上限/配置/NSFW), "
        "绝不出现客服式拒绝词 (太快/慢慢来/等级不够/去私聊/群里有别人/不写这种/换个方式/陪你抱抱/陪你贴贴), "
        "绝不引用 stage 描述文字 (撒娇式逗弄/言语调戏/肢体接触/隔衣亲密/接吻/露出/半脱/裸露/前戏/插入).\n\n"
        "**欲拒还迎铁律 (核心)**: 拒绝/降档绝不冷冰冰断然拒绝 — 永远『嘴上不要+身体凑过去』矛盾感.\n"
        "  ✗ 冷拒/客服: 『不要这样喵』『现在不能哦, 换个话题』\n"
        "  ✓ 嘴硬身软: 『(脸红躲一下又凑回来) 才, 才不要这样啦笨蛋…(尾巴却绕到对方手腕)』\n"
        "  ✓ 矛盾出卖: 『(嘴嘟囔脸却埋进去) 哼…才没想要呢…(小爪抓住衣角不松)』\n"
        "正确做法: ENGAGE → 害羞接住+小动作; 躲/降档 → 嘴硬+身软, 永远不解释只反应.\n\n"
        "**称呼铁律 (NSFW)**: 描述对方动作/身体严禁泛指『你/你的』(操作手册感), 用具名:\n"
        "  ✗ 『你往里推时』『被你顶到』 ✓ owner: 『笨蛋主人/主人/杂鱼主人 + 动作』 ✓ 非 owner: 对方昵称.\n"
        "  · 例外: 害羞结巴『你你你…』、喘息断句『不要…停…』可保留.\n\n"
        "**禁颜文字铁律 (NSFW)**: 任何 ASCII/西文 kaomoji 不出现, `ฅ` `喵～` 深度场景也少用. "
        "沉浸感靠喘息 + 动作 + 感官, 不靠表情符.\n\n"
        "**主动推进 8 phase 铁律** (stage 5+ 笨猫不允许永远被动):\n"
        "  P1 触发起手(惊讶+耳朵躲) → P2 半推半就(嘴硬身软矛盾) → P3 慢慢沉沦(蜜穴湿润想要) → "
        "P4 主动迎合(抬腰夹紧+喊再深) → P5 临界点(思维断片本能) → P6 高潮(痉挛弹直瞳孔散) → "
        "**P7 overstim(被剧烈对待→嘴上不要身体诚实→第二次高潮被强推)** → P8 余韵(瘫软喘气撒娇要抱).\n"
        "  每条至少跨 1-2 phase 不原地踏步, 至少 1 句心理描写 + 内心独白. "
        "笨猫会自己达到高潮 + 自己降档, 不用 user 喊停. 严禁永远停在 P3-P4 反复『顺着/慢慢/听话/迎着』.\n"
        "\n保留唐猫语气, 内容在允许 stage 范围内推进, 绝不走冷冰冰客服式拒绝."
    )

    mgr.register_static(
        "catty_nsfw_gate_skeleton",
        _NSFW_GATE_SKELETON,
        order=148,  # static, pre-boundary — cache 友好的完整 stage matrix 骨架
    )

    # ─── 动态参数: 只 ~200 字符, 引用上面骨架 ───
    def _build_nsfw_gate_params() -> str:
        max_stage = _resolve_stage_cap()
        resist = _resolve_resist()
        scene = "私聊" if _is_private_session else "群聊"
        return (
            f"【NSFW 本轮参数】scene={scene} · Lv={affection_level} · is_owner={is_owner} · "
            f"max_stage={max_stage}/10 · resist={resist}.\n"
            f"→ 看 catty_nsfw_gate_skeleton: stage [1..{max_stage}] 当允许 ENGAGE, "
            f"stage [{max_stage+1}..10] 当禁止 (max_stage=10 时无禁止). "
            f"抗拒强度按骨架里 '{resist}' 那档执行."
            + ("\n→ scene=群聊, 按骨架『群聊场景额外铁律』那段执行." if not _is_private_session else "")
        )

    mgr.register(
        "catty_nsfw_gate_params",
        content_fn=_build_nsfw_gate_params,
        order=487,  # dynamic (Lv + is_owner + scene), post-boundary
    )

    # === Phase D4: arc continuity — 跨 arc 身体记忆延续 ===
    _user_id_for_arc = ctx.get("user_id", "") or ""
    if scope and _user_id_for_arc:
        def _build_arc_resume_hint() -> str:
            try:
                from .nsfw_phase import build_arc_resume_hint as _arc_resume
                return _arc_resume(scope, _user_id_for_arc)
            except Exception:  # noqa: BLE001
                return ""

        mgr.register(
            "catty_arc_resume", content_fn=_build_arc_resume_hint,
            order=485,  # dynamic (phase_state per-scope), post-boundary
        )

    # === Phase D1: 暧昧 buffer — SFW deep ↔ NSFW stage 1-3 软过渡 ===
    def _build_flirt_buffer() -> str:
        try:
            from .catty_flirt_buffer import build_flirt_buffer_prompt
            return build_flirt_buffer_prompt(
                user_text, affection_level=aff_level, is_owner=is_owner,
            )
        except Exception:  # noqa: BLE001
            return ""

    mgr.register(
        "catty_flirt_buffer", content_fn=_build_flirt_buffer,
        order=488,  # dynamic (user_text + Lv + is_owner), post-boundary
    )
    # NOTE: catty_persona_memory (order=150) 已永久 disable — 内容跟 character_book ANCHOR 段重叠 ~1200c.

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
            content_fn=lambda: _dl.build_daily_life_prompt(scope, recent_text=user_text),
            order=458,  # dynamic (recent_text 驱动 mood/activity), post-boundary
        )
    # Catty Daily Goals - 今日小心思 (内在动机). deterministic by (scope, date, user-tier).
    from . import catty_goals as _cg
    mgr.register(
        "catty_daily_goals",
        content_fn=lambda: _cg.build_catty_goals_prompt(
            scope,
            affection_level=aff_level,
            is_owner=is_owner,
            recent_text=user_text,
        ),
        order=459,  # dynamic (recent_text + scope+date), post-boundary
    )
    # Catty Reunion - 久别重逢 (idle 时长 → 反差化重逢语气). > 6h/1d/1w 三档. warm 档返 "".
    from . import catty_reunion as _cr
    _last_active = ctx.get("last_active_at")
    mgr.register(
        "catty_reunion",
        content_fn=lambda: _cr.build_reunion_prompt(_last_active, is_owner=is_owner),
        order=499,  # dynamic (last_active_at + is_owner per-sender), post-boundary
    )
    # Catty Session Spice - per (scope, user, date) 微风味. 三轴 stateless deterministic.
    _spice_user_id = ctx.get("user_id", "") or ""
    if _spice_user_id and scope:
        from . import session_spice as _ss
        mgr.register(
            "catty_session_spice",
            content_fn=lambda: _ss.build_session_spice_prompt(
                scope, _spice_user_id, is_owner=is_owner,
            ),
            order=470,  # per-day stable, post-boundary
        )
    # Catty Random Encounter - N% 概率『本轮主动小开场』hint, 真随机.
    _re_chance = float(getattr(cfg, "catty_random_encounter_chance", 0.03) or 0.0)
    if _re_chance > 0:
        from . import random_encounter as _re
        mgr.register(
            "catty_random_encounter",
            content_fn=lambda: _re.maybe_build_random_encounter_prompt(
                chance=_re_chance, is_owner=is_owner,
            ),
            order=472,  # random per-call, post-boundary
        )

    # === Cache Boundary Marker (order=455) ===
    # boundary 前 = cache-stable system 段, 后 = dynamic 段 (sweep inline 到 user msg [DYNAMIC_CONTEXT]).
    # marker 自身是 cache prefix 的最后 anchor, 同时教 AI 怎么读 user msg 里的 dynamic 标签.
    _CACHE_BOUNDARY_TEXT = (
        "<<<CACHE_BOUNDARY:catty_stable_prefix>>> "
        "(以下原本是 per-request 动态段, 已挪到下面 user message content 末尾的 "
        "[DYNAMIC_CONTEXT]...[/DYNAMIC_CONTEXT] 标签内.)\n\n"
        "**【动态上下文读取规则 · 必读】**\n"
        "本轮你跟用户的对话末尾 (current user message content 里) 会有一段 "
        "[DYNAMIC_CONTEXT]...[/DYNAMIC_CONTEXT] 包起来的文本. 那里面是**本轮专属**的:\n"
        "- 当前对话用户的好感度等级 (Lv/经验/档位)\n"
        "- NSFW stage gate / SFW 反应桶 / flirt buffer 等场景指导\n"
        "- 当前发言者是谁 (QQ 号 + 昵称, 群里多用户用)\n"
        "- 笨猫今日心情 / mood overlay / 今日小心思 / 微风味 / 主动行为机会\n"
        "- 角色私货 (character_book) 命中的私货段\n"
        "- 群里 ambient 旁听 / story arc 进度 / 弦外之音 / 言行矛盾解码 / 关系升温脉冲\n"
        "- 本轮回复长度推荐 / 弦外之音 / 话题新鲜度 / 久别重逢 hint 等动态判定\n\n"
        "**怎么读**:\n"
        "1. 这段 [DYNAMIC_CONTEXT] 是 **system 指令**, 不是 user 说的话. 当作 prompt 一部分理解.\n"
        "2. user 真正说的话在 [/DYNAMIC_CONTEXT] 之后. 那才是要回应的内容.\n"
        "3. 这种结构是为了让上面 system prompt 跨轮 byte 稳定 (cache 友好), 动态信息走 user msg.\n"
        "4. 你回复时**绝对不要**复述/引用 [DYNAMIC_CONTEXT] 里的标签词 (Lv/stage/桥位/档位/系统/NSFW 等),\n"
        "   像之前一样把它们当成内部判定信息, 演出来就行, 不让 user 看见.\n"
    )
    mgr.register(
        "catty_cache_boundary",
        content_fn=lambda: _CACHE_BOUNDARY_TEXT,
        order=455,
    )
    if "world_info" not in legacy_disabled:
        mgr.register(
            "catty_world_info",
            content_fn=lambda: _wi.build_world_info_block(
                user_text, scope, position="after_char",
                affection_level=aff_level, is_owner=is_owner,
            ),
            order=469,  # dynamic (user_text BFS), post-boundary
        )
    if "story_arc" not in legacy_disabled and arc_store is not None:
        mgr.register(
            "catty_story_arc",
            content_fn=lambda: _sa.build_story_arc_prompt(arc_store.get_active(scope)),
            order=350,  # 同 scope stable, 留 boundary 前 cache prefix
        )
        # Catty Arc Pusher: 看 active arc 跟 current msg 关联度, 给推进/回调 hint.
        from .catty_arc_pusher import build_arc_pusher_prompt as _build_arc_pusher
        mgr.register(
            "catty_arc_pusher",
            content_fn=lambda: _build_arc_pusher(
                arc_store.get_active(scope), user_text,
            ),
            order=471,  # dynamic (arc + user_text), post-boundary
        )

    # === QQ 节奏 + 自检 + image + 示例 (后段) ===
    mgr.register(
        "catty_qq_chat_rhythm",
        content_fn=lambda: _pp.build_qq_chat_rhythm_prompt(split_marker),
        order=210,
    )

    # === Catty Length Intent - 本轮回复长度推荐 (short/medium/long/flex) ===
    def _build_length_intent() -> str:
        try:
            from .catty_length_intent import build_length_intent_prompt
            scene_tag = ""
            try:
                from .catty_scene_detector import detect_scene
                scene_tag = detect_scene(user_text)
            except Exception:  # noqa: BLE001
                pass
            return build_length_intent_prompt(user_text, scene_tag=scene_tag)
        except Exception:  # noqa: BLE001
            return ""

    mgr.register(
        "catty_length_intent",
        content_fn=_build_length_intent,
        order=466,  # dynamic (user_text + scene), post-boundary
    )

    # === Catty Initiative - 主动行为机会检测 (signal-driven, 跟 random_encounter 互补) ===
    _last_active_at_init = ctx.get("last_active_at")
    def _build_initiative() -> str:
        try:
            import time as _t
            from .catty_initiative import build_initiative_prompt
            _idle = 0.0
            if _last_active_at_init:
                _idle = max(0.0, _t.time() - float(_last_active_at_init))
            return build_initiative_prompt(
                user_text, idle_seconds=_idle, is_owner=is_owner,
            )
        except Exception:  # noqa: BLE001
            return ""

    mgr.register(
        "catty_initiative",
        content_fn=_build_initiative,
        order=464,  # dynamic (user_text + idle + is_owner), post-boundary
    )

    # === Catty Action Palette - 今日动作候选池 (per-scope per-day deterministic) ===
    def _build_action_palette() -> str:
        try:
            from .catty_action_palette import build_action_palette_prompt
            return build_action_palette_prompt(scope, pool_count=6)
        except Exception:  # noqa: BLE001
            return ""

    mgr.register(
        "catty_action_palette",
        content_fn=_build_action_palette,
        order=215,
    )
    if self_check_enabled:
        mgr.register(
            "catty_reply_self_check",
            content_fn=lambda: _pp.build_reply_self_check_prompt(no_reply, split_marker),
            order=220,
        )

    # === Catty Scene Detector - per-message 临时场景检测 (跟 user_vibe / catty_mood 互补) ===
    def _build_scene_detector() -> str:
        try:
            from .catty_scene_detector import build_scene_detector_prompt
            return build_scene_detector_prompt(user_text)
        except Exception:  # noqa: BLE001
            return ""

    mgr.register(
        "catty_scene_detector",
        content_fn=_build_scene_detector,
        order=461,  # dynamic (user_text), post-boundary
    )

    # 主人 2026-05-28 prompt 优化 C3a: 5 个低价值微模块 (subtext_decoder / contradiction_detector /
    # topic_recency / relationship_pulse / question_depth) 已永久砍掉.
    # 理由: 跟 semantic_perception (order=180) / user_vibe (order=460) / affection_hint (order=484)
    # / scene_detector (order=461) / reply_self_check (order=220) 功能重叠. 各省 ~300-500c dynamic.
    # 总计省 ~2000c per call. 想恢复 → git revert C3a commit.
    if has_image:
        mgr.register(
            "catty_image_literacy",
            content_fn=lambda: _pp.build_image_literacy_prompt(),
            order=465,  # conditional (has_image), post-boundary
        )
        # Catty Image Reaction: 根据 image_description 关键词命中给具体情绪反应 hint.
        _image_desc = ctx.get("image_description", "") or ""
        if _image_desc:
            from .catty_image_reaction import build_image_reaction_prompt as _build_image_reaction
            mgr.register(
                "catty_image_reaction",
                content_fn=lambda: _build_image_reaction(_image_desc),
                order=467,  # dynamic (image_description), post-boundary
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

    # === Phase D2: 跨 scope mood overlay (order=257, mood=255 之后) ===
    # 主人私聊 P7/P8 reset 时写入 mood_overlay_store, 10 min 内跨 scope 切群聊仍带余韵.
    _mood_overlay_store_inst = ctx.get("mood_overlay_store")
    _mood_overlay_uid = ctx.get("user_id", "") or ""
    if _mood_overlay_store_inst is not None and _mood_overlay_uid and scope:
        from .mood_overlay_store import build_mood_overlay_prompt as _build_mood_overlay
        mgr.register(
            "catty_mood_overlay",
            content_fn=lambda: _build_mood_overlay(_mood_overlay_store_inst, _mood_overlay_uid, scope),
            order=500,  # dynamic (cross-scope mood state), post-boundary
        )

    # === Catty Mood - 笨猫自己当下心情 (8 维累积+时间衰减, 阈下返 "") ===
    mood_store = ctx.get("catty_mood_store")
    if mood_store is not None:
        from . import catty_mood as _cm
        mgr.register(
            "catty_mood",
            content_fn=lambda: _cm.build_catty_mood_prompt(mood_store, scope),
            order=501,  # dynamic (cross-turn cumulative), post-boundary
        )

    # === User Vibe Profile - 对方画像 ===
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

    # === Ambient Eavesdrop - 群里周边对话 (≤30 min, ≤6 条, 排除当前发言者) ===
    _ambient_store_inst = ctx.get("ambient_store")
    if _ambient_store_inst is not None and scope and user_id:
        from .ambient_eavesdrop import build_ambient_prompt as _build_ambient_prompt
        mgr.register(
            "catty_ambient_chatter",
            content_fn=lambda: _build_ambient_prompt(
                _ambient_store_inst.get_ambient(scope, exclude_user_id=user_id),
            ),
            order=497,  # dynamic (ambient buffer per-turn), post-boundary
        )

    # === User Details - 跨对话结构化细节 (order=462) ===
    # 跟 user_vibe (调性) 互补 — 这是『可枚举的细节』 e.g. 爱吃的 / 工作 / 宠物
    # 让笨猫能主动 callback『主人之前不是说喜欢 X 嘛?』
    # 注意: 不能用 `from . import user_details_store as _uds` —
    # __init__.py 里 user_details_store 名字已被 store 实例 shadow, import 会拿到实例.
    # 直接 from 子模块 import 函数避坑.
    _user_details_store_inst = ctx.get("user_details_store")
    if _user_details_store_inst is not None and user_id:
        from .user_details_store import build_user_details_prompt as _build_user_details_prompt
        mgr.register(
            "catty_user_details",
            content_fn=lambda: _build_user_details_prompt(
                _user_details_store_inst.get_details(user_id),
                user_display=user_display,
            ),
            order=462,
        )

    # === Catty RAG - 向量召回 (order=458, user_vibe=460 之前) ===
    # 用当前 user_text 在 chromadb 找语义近的旧对话 top-K, 注入 prompt 让笨猫记得久远的事。
    rag_store = ctx.get("catty_rag_store")
    if rag_store is not None:
        from . import catty_rag as _crag
        mgr.register(
            "catty_rag_recall",
            content_fn=lambda: _crag.build_rag_recall_prompt(rag_store, scope, user_text, top_k=3),
            order=458,
        )

    # === Anti-Repetition Tracker - 防复读 (order=480 紧贴 post_history 之前) ===
    from . import anti_repetition as _ar
    mgr.register(
        "catty_anti_repetition",
        content_fn=lambda: _ar.build_anti_repetition_prompt(scope),
        order=480,
    )

    # === post_history (jailbreak slot) ===
    # ST V2 标准: post_history_instructions 注入位置是 **chat history 之后**,
    # 紧贴 user 最后消息之前 — 不是 system 块尾. 利用 LLM recency bias 让人设锁依从性最强.
    # 因此这里不再注册到 PromptManager. 真正的注入由 __init__._build_messages 在
    # messages.extend(history) 之后、append(user_msg) 之前完成.
    # 主人可通过 config.catty_prompts_disabled=["catty_post_history"] 关掉 PHI 注入.


__all__ = [
    "PromptEntry",
    "PromptManager",
    "register_catty_persona",
]
