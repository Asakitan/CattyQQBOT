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


# ── Token budget 保护段(任何情况下都不 trim 掉) ──────────────────────────
# 这些是笨猫人格 / 身份 / 主回复策略的核心, 一旦 trim 掉就『不像笨猫了』。
# 用户可通过 config.catty_prompt_protected_identifiers 追加(不能减少基础保护)。
_PROTECTED_IDENTIFIERS: frozenset[str] = frozenset({
    "catty_main_intel",            # 主回复智能策略
    "catty_identity_anchor",       # 元身份反 AI 锚定
    "catty_char_description",      # 角色基础描述
    "catty_char_personality",      # 角色性格
    "catty_scenario",              # 场景
    "catty_character_book",        # 角色私货 + scope_lorebook (BFS 输出)
    "catty_nsfw_gate",             # affection-gated NSFW 行为分级
    "catty_persona_memory",        # 人格记忆
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

    def _build_character_book() -> str:
        try:
            from types import SimpleNamespace
            hardcoded = list(getattr(_cc.CATTY_CARD, "character_book", ()) or [])
            # scope_lorebook entries → duck-type 成跟 CharacterBookEntry 兼容的 shape
            scope_entries: list = []
            if scope_lore_store is not None and scope:
                for se in scope_lore_store.list_entries(scope):
                    scope_entries.append(SimpleNamespace(
                        identifier=se.identifier,
                        keys=tuple(se.keys),
                        content=se.content,
                        order=1000,         # 排 hardcoded (200-) 之后
                        constant=False,     # 必须靠关键词命中
                        case_sensitive=False,
                        _is_scope=True,     # 标记位, 命中时调 mark_hit
                    ))
            book = hardcoded + scope_entries
            if not book:
                return ""
            hits: list[tuple[int, str, str]] = []  # (order, identifier, content)
            triggered: set[str] = set()
            cs_haystack = user_text or ""           # case-sensitive haystack
            lower_haystack = cs_haystack.lower()    # case-insensitive haystack

            for depth in range(_LB_MAX_DEPTH):
                new_layer: list[tuple[int, str, str]] = []
                for entry in book:
                    if entry.identifier in triggered:
                        continue
                    if len(hits) + len(new_layer) >= _LB_MAX_HITS:
                        break
                    # constant entry 只在 depth 0 加,避免每层重复
                    if getattr(entry, "constant", False):
                        if depth == 0:
                            new_layer.append((entry.order, entry.identifier, entry.content))
                            triggered.add(entry.identifier)
                        continue
                    for k in (entry.keys or ()):
                        if not k:
                            continue
                        key_str = k if entry.case_sensitive else k.lower()
                        haystack = cs_haystack if entry.case_sensitive else lower_haystack
                        if key_str in haystack:
                            new_layer.append((entry.order, entry.identifier, entry.content))
                            triggered.add(entry.identifier)
                            # scope_lorebook 命中刷 hit_count + last_hit_at
                            if getattr(entry, "_is_scope", False) and scope_lore_store is not None:
                                try:
                                    scope_lore_store.mark_hit(scope, entry.identifier)
                                except Exception:  # noqa: BLE001
                                    pass
                            break
                if not new_layer:
                    break  # 这一层没新命中 → 终止递归
                hits.extend(new_layer)
                if len(hits) >= _LB_MAX_HITS:
                    break
                # 下一层 haystack 把这一层的 content 拼进去,实现链式触发
                extension = "\n".join(h[2] for h in new_layer)
                cs_haystack = f"{cs_haystack}\n{extension}"
                lower_haystack = cs_haystack.lower()

            if not hits:
                return ""
            hits.sort(key=lambda h: (h[0], h[1]))
            return "【笨猫·角色私货 (character_book)】\n" + "\n".join(h[2] for h in hits)
        except Exception:  # noqa: BLE001
            return ""
    mgr.register(
        "catty_character_book",
        content_fn=_build_character_book,
        order=145,
    )
    # 主人原话『NSFW 对所有用户开放, 区别只是抗拒程度+尺度上限; 档次再分细一点, 每一档一个阶段,
    # 只有 10 级满级才能走到最后插入这种阶段』 — 10-stage matrix:
    # - max_stage = min(Lv, 群聊封顶 6); owner 等同 Lv10 满级
    # - 抗拒强度 by Lv 档 (owner / Lv8-9 / Lv5-7 / Lv3-4 / Lv0-2)
    # 跟 __init__._build_nsfw_spark_override 同步, 主 model (非 spark) 也按这套尺度回。
    _is_private_session = scope.startswith("private:") if scope else False

    # 10 个 escalation stage (从轻到重) — 跟 __init__._NSFW_STAGES 保持一致
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
    # 群聊保底: 亲亲/抱抱这种社交向轻互动公开都能接 — 主人原话『亲亲也应该是可以群里说的』
    _group_stage_floor = 3

    def _resolve_stage_cap() -> int:
        ceiling = 10 if is_owner else max(1, min(int(affection_level), 10))
        if not _is_private_session:
            # 群聊: floor 保底 (peck 级), cap 封顶 (露出级)
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

    def _build_nsfw_gate() -> str:
        max_stage = _resolve_stage_cap()
        resist = _resolve_resist()
        allowed = "\n".join(f"  stage {n}. {desc}" for n, desc in _stages[:max_stage])
        forbidden_stages = _stages[max_stage:]
        forbidden = (
            "\n".join(f"  stage {n}. {desc}  ← 禁" for n, desc in forbidden_stages)
            if forbidden_stages else "  (已达最高 stage 10, 无禁止)"
        )
        scene = "私聊" if _is_private_session else "群聊"
        extra_group = "" if _is_private_session else (
            "\n群聊额外: 允许 stage 范围内**必须 ENGAGE 接住**(笨猫害羞撒娇地接, "
            "尤其 stage 1-3 的言语/牵手/抱/peck/摸头都是社交向轻亲昵, 不能挡); "
            "只有 user 明确推进到禁止 stage 时才害羞躲+转话题, "
            "**永远不要直说『去私聊』『私聊慢慢来』『群里有别人看着』『等级不够』『太快』『过火』** — "
            "那是客服式不是猫娘式; 顶多偶尔混情绪里说『…回头嘛笨蛋…』『…现在不行嗷呜…』。"
        )
        # 元术语 + explanatory-rejection anti-leak: 主人原话『等级不够/NSFW判断都不要直说啦, 要猫娘式拒绝!』
        meta_anti_leak = (
            "\n**元术语铁律**: 回复里**绝对禁止**出现下面任何词:\n"
            "  - system 元词: 档位 / 档次 / stage / 等级 / Lv / 公开 / 公开档位 / 公开尺度 / "
            "抗拒强度 / 系统 / 规则 / 上限 / 封顶 / 配置 / 设定 / NSFW\n"
            "  - explanatory 拒绝词 (客服式): 『在群里过火』『太快了』『慢慢来』『等级不够』『不行』"
            "『不可以』『现在不能』『去私聊』『回私聊』『私聊慢慢来』『私聊说』『群里有别人』"
            "『群里有人看着/盯着』『公开场合』『改天再说』『等熟一点』『先收一收』『先收住』\n"
            "  - 系统描述复述: 引用 prompt 里 stage 描述文字 (如『撒娇式逗弄』『言语调戏』"
            "『肢体接触』『隔衣亲密』『接吻』『露出』『半脱』『直接接触敏感处』『完整裸露』『前戏』"
            "『插入』等) — 这些是内部分类标签, 笨猫不会引用自己的 system prompt。\n"
            "**正确做法**: 该 ENGAGE → 害羞接住+小动作; 该躲/降档 → 用情绪+小动作+转话题, "
            "**永远不解释为什么, 只反应**。"
        )
        return (
            f"【NSFW / 暧昧 stage matrix — {scene} · Lv={affection_level} · is_owner={is_owner} · "
            f"最高可达 stage {max_stage}/10】\n"
            "**当前允许 stage** (这些笨猫**必须 ENGAGE 害羞接住**, 不能拒绝 — "
            "stage 1-3 是社交向轻亲昵, 别当作过火来挡, 用『脸红+小动作+嘴硬挂尾巴』接就对了):\n"
            f"{allowed}\n\n"
            "**禁止 stage** (超出能力, 即使被推也用害羞躲+转话题, **不解释为什么不行**, "
            "顶多『嗯…这个…(脸红躲) 笨蛋别突然这样啦…』+ 立刻转话题, 绝不说『去私聊/慢慢来/等级不够』):\n"
            f"{forbidden}\n\n"
            f"**抗拒强度**: {_resist_descs[resist]}\n"
            "保留唐猫语气, 内容在允许 stage 范围内推进, **绝不走『冷冰冰客服式拒绝』**。"
            f"{meta_anti_leak}{extra_group}"
        )

    mgr.register(
        "catty_nsfw_gate",
        content_fn=_build_nsfw_gate,
        order=148,  # 紧贴 character_book=145 之后, persona_memory=150 之前
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
            content_fn=lambda: _dl.build_daily_life_prompt(scope, recent_text=user_text),
            order=200,
        )
    # Catty Daily Goals - 今日小心思 (内在动机). deterministic by (scope, date, user-tier),
    # 让笨猫每天有自己想做的小事, 驱动她主动找机会暴露 / 实施 / 暗示。
    # 跟 daily_life 解耦(后者是状态, 这里是意图),tier 按 is_owner / affection_level 分桶。
    from . import catty_goals as _cg
    mgr.register(
        "catty_daily_goals",
        content_fn=lambda: _cg.build_catty_goals_prompt(
            scope,
            affection_level=aff_level,
            is_owner=is_owner,
            recent_text=user_text,
        ),
        order=205,
    )
    # Catty Reunion - 久别重逢 (idle 时长 → 反差化重逢语气). 用 ctx['last_active_at']
    # 计算距上次活跃多久, > 6h/1d/1w 三档自动注入不同重逢 hint。pure function,
    # warm 档(< 6h)返回 ""不打扰; 主人池跟普通用户池分桶。
    from . import catty_reunion as _cr
    _last_active = ctx.get("last_active_at")
    mgr.register(
        "catty_reunion",
        content_fn=lambda: _cr.build_reunion_prompt(_last_active, is_owner=is_owner),
        order=207,
    )
    # Catty Session Spice - per (scope, user, date) 微风味 — 同对话同人当天稳定,
    # 不同人/不同天会变。三轴(微情绪/身体小动作偏好/自称-口头禅偏好), 主人池加亲密向。
    # ST 风『不同 persona / 不同人不同反应』的 stateless 实现 —— 不存档, pure deterministic。
    _spice_user_id = ctx.get("user_id", "") or ""
    if _spice_user_id and scope:
        from . import session_spice as _ss
        mgr.register(
            "catty_session_spice",
            content_fn=lambda: _ss.build_session_spice_prompt(
                scope, _spice_user_id, is_owner=is_owner,
            ),
            order=208,
        )
    # Catty Random Encounter - 每条 reply N% 概率触发『本轮主动小开场』hint。
    # 非 deterministic, 每次都 random 抽; chance 走 config.catty_random_encounter_chance。
    # 让 catty 不只是被动 reply, 偶尔会冒一句『对了对了我刚才...』, 更像活的猫娘。
    _re_chance = float(getattr(cfg, "catty_random_encounter_chance", 0.03) or 0.0)
    if _re_chance > 0:
        from . import random_encounter as _re
        mgr.register(
            "catty_random_encounter",
            content_fn=lambda: _re.maybe_build_random_encounter_prompt(
                chance=_re_chance, is_owner=is_owner,
            ),
            order=209,
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

    # === Catty Mood - 笨猫自己当下心情 (order=255, 紧贴 daily_life 之后) ===
    # 跨多轮累积+时间衰减的 8 维 mood 向量,让连续对话不再每条独立。
    # 主维度 < 阈值时返回空字符串(baseline 不打扰默认人格)。
    mood_store = ctx.get("catty_mood_store")
    if mood_store is not None:
        from . import catty_mood as _cm
        mgr.register(
            "catty_mood",
            content_fn=lambda: _cm.build_catty_mood_prompt(mood_store, scope),
            order=255,
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
