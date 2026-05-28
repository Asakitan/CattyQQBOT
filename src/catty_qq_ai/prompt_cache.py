"""SillyTavern 风 Anthropic Prompt Caching 注入 — 移植自 ST PR #3085.

参考: https://github.com/SillyTavern/SillyTavern/pull/3085
     https://github.com/SillyTavern/SillyTavern/blob/release/src/prompt-converters.js (cachingAtDepthForClaude L1024-1047)

Anthropic 限 4 个 cache_control breakpoints, ST 用法:
1. tools[-1]                     ← 全局 tool 定义 cache
2. system[-1] (最后一个 system)  ← system 块 cache
3. messages 倒数 depth=N 处       ← rolling history cache (N+0)
4. messages 倒数 depth=N+2 处     ← rolling history cache (N+2)

Anthropic 规则:
- cache 是 prefix-based: 从开头到 breakpoint 字节级一致才命中
- min cacheable tokens: Sonnet 4.5=1024, Opus 4.7=4096, Haiku 4.5=4096
- 默认 TTL: 5 分钟
- Anthropic header: anthropic-beta: prompt-caching-2024-07-31

OpenAI native API 是 implicit caching (>1024 tokens 自动), 不需要标记;
但中间人 (hugou.cc) passthrough Claude 协议时仍需显式 cache_control.
"""
from __future__ import annotations

from typing import Any


def cachingAtDepthForClaude(messages: list[dict], cachingAtDepth: int = 2) -> list[dict]:
    """CC 同款 cache_control 标位: messages 数组**最后一条**消息的 content 最后 block.

    源码依据 (主人 CC_CACHE_MECHANISM.md section 2 双重确认):
    - cli.mjs:865-870 (CC 官方反编译): user 末块加 cache_control
    - cli.mjs:874-879: assistant 末块加 (排除 thinking/redacted_thinking)
    - claude.ts:3201-3244 (社区版): markerIndex = messages.length - 1

    cache 命中机制 (CC_CACHE_MECHANISM.md section 7.2):
    cache_control 标 messages[-1] 是为了**写入** — 本轮 prefix 写到 cache.
    **读取**是 Anthropic 从 breakpoint **向更早 block 回溯** (窗口 20 block),
    找之前写过的更短 prefix 条目. 因此 messages[-1] 内容每轮变 (catty 的 DYNAMIC_CONTEXT)
    **不影响命中** — 命中是回溯前一轮已写入的更短 prefix.

    主人 2026-05-28 C7-2: 回退 C6 (错误标 user_indices[-2]), 改回 CC 同款 messages[-1].
    之前以为"current user 每轮变所以 miss" 是误读 Anthropic cache 机制 — 实际靠回溯.

    cachingAtDepth 参数保留兼容, 不再生效.
    """
    if not messages:
        return messages
    last_msg = messages[-1]
    if not isinstance(last_msg, dict):
        return messages
    # 跳过 thinking / redacted_thinking 末块 — Anthropic 不接受 cache_control 在这两种
    # (CC cli.mjs:876-877 显式过滤).
    content = last_msg.get("content")
    if isinstance(content, list) and content:
        last_block = content[-1]
        if isinstance(last_block, dict) and last_block.get("type") in ("thinking", "redacted_thinking"):
            return messages
    _mark_cache_control(last_msg)
    return messages


def _get_cache_ttl() -> str | None:
    """读 config.catty_cache_ttl, 决定 cache TTL ('1h' / '5min' / None=默认5min).

    主人 2026-05-28 C7: 改回 5min 默认 — 主人发现 1h cache + dynamic 内容长期持久化
    会让 Claude 触发 safety refusal, 短 TTL 让 prefix 自然过期不跨多 session 漂移.
    价格: 5min cache write 1.25x (vs 1h write 2x), cache read 都 0.1x.
    """
    try:
        from . import config as _module_config
        ttl = getattr(_module_config.config, "catty_cache_ttl", "5min")
        if isinstance(ttl, str) and ttl.strip().lower() in ("1h", "5min", "5m"):
            normalized = ttl.strip().lower().replace("5m", "5min")
            return "1h" if normalized == "1h" else None  # 5min 是 Anthropic 默认, 不传字段
    except Exception:  # noqa: BLE001
        pass
    return None  # 默认 5min (不显式传 ttl 字段)


def _build_cache_control_dict() -> dict[str, Any]:
    """构造 cache_control dict, 根据 config 决定是否加 ttl: 1h.

    返回 {type: ephemeral} 或 {type: ephemeral, ttl: "1h"}.
    """
    cc: dict[str, Any] = {"type": "ephemeral"}
    ttl = _get_cache_ttl()
    if ttl == "1h":
        cc["ttl"] = "1h"
    return cc


def _mark_cache_control(msg: dict[str, Any]) -> None:
    """把 cache_control 加到 message content 的最后一个 block.

    Claude API 要求 cache_control 必须在 content block 上, 不能在 message 顶层.
    自动把 str content 转成 list[{type: text, text: ..., cache_control: ...}] 单 block 格式.

    主人 2026-05-28: cache_control 现在带 ttl: '1h' (config 可关), 默认 5min 太短.
    """
    cc = _build_cache_control_dict()
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": cc,
            }
        ]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = cc


_CACHE_BOUNDARY_MARKER = "<<<CACHE_BOUNDARY:catty_stable_prefix>>>"


def _has_marker_in_content(content: object, marker: str) -> bool:
    """检测 message.content (可能是 str 或 list[dict]) 是否含 boundary marker."""
    if isinstance(content, str):
        return marker in content
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict):
                text = str(blk.get("text", ""))
                if marker in text:
                    return True
    return False


def inject_system_tail_cache(messages: list[dict]) -> list[dict]:
    """给 stable system prefix 末尾打 cache_control: ephemeral breakpoint.

    Phase A3 (2026-05-28 后): 优先找 _CACHE_BOUNDARY_MARKER segment, 打 cache_control
    在它上面. boundary 是 prompt_manager register 的固定文本段 (order=455), 它前面
    所有 stable system 段在 cache 内, 它后面所有 dynamic system 段 (session_spice,
    random_encounter, user_vibe, user_details, anti_repetition 等, order >= 460) 在
    cache 边界外不影响 prefix 字节一致.

    fallback (没有 boundary marker): 退化为老逻辑 - 找顶部连续 system 段的最后一条.
    主人 2026-05-28 之前的 fix: 改 global 倒数找 system → 顶部连续 (避免 author_note
    动态 system 污染 cache key).

    Anthropic Prompt Caching 要求 prefix 字节级一致才命中; 这一改让 cache 真正吃到
    stable persona/character_card/world_info/qq_rhythm/examples 等几 K 段。
    """
    # 主人 2026-05-28 C7-4: 回退 sweep 后, sys 段含动态段 (recency/phase_hint/starter/preg/
    # climax/arc_counter/trope), 标 last_top_system 让 prefix 含动态段, 每轮字节漂移 →
    # cache 永远 miss (实测 11:05/11:06 sys_blocks=5 cache_control=sys[4] = trope 动态段,
    # 100% miss). 改成**优先 boundary_idx (静态前缀末锚点) → prefix 字节稳定 → cache 命中**.
    # SFW 主路径 PromptManager order=455 register boundary, NSFW spark 路径手动 append marker 段.
    # fallback last_top_system 兼容无 boundary 异常路径.
    boundary_idx = -1
    last_top_system = -1
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            break  # 离开顶部 system 块
        last_top_system = i
        if boundary_idx < 0 and _has_marker_in_content(msg.get("content"), _CACHE_BOUNDARY_MARKER):
            boundary_idx = i

    target = boundary_idx if boundary_idx >= 0 else last_top_system
    if target >= 0:
        _mark_cache_control(messages[target])
    return messages


def inject_tools_cache(tools: list[dict]) -> list[dict]:
    """给 tools 列表最后一个 entry 加 cache_control 用掉 Anthropic 第 4 个 breakpoint.

    tools schema 内容稳定 (function 定义不变), 单独 cache 后 Claude 不用每次重算 tool 段;
    用掉 4 个 breakpoint 中的 tools[-1] 那一个, 跟 system tail + 倒数 N + 倒数 N+2 配对.

    OpenAI native 收到 cache_control 字段会忽略 (跟 message 那侧逻辑一致),
    但调用方仍建议只对 Claude endpoint 调用以避免中转层严格校验.

    返回新 list (浅拷贝最后一个 dict + 加字段), 不修改入参.
    """
    if not tools:
        return tools
    new_tools = list(tools[:-1])
    last = tools[-1]
    if isinstance(last, dict):
        last_copy = dict(last)
        last_copy["cache_control"] = _build_cache_control_dict()
        new_tools.append(last_copy)
    else:
        new_tools.append(last)
    return new_tools


def is_claude_endpoint(base_url: str, model: str) -> bool:
    """判断当前 endpoint 是否走 Claude / Anthropic 协议 — 决定要不要注入 cache_control."""
    bu = (base_url or "").lower()
    m = (model or "").lower()
    if "anthropic" in bu or "claude" in bu:
        return True
    if "claude" in m or "anthropic" in m:
        return True
    if "sonnet" in m or "opus" in m or "haiku" in m:
        return True
    # 中间人 (hugou.cc / openrouter) 模型名含 claude/anthropic/sonnet 都走 cache_control
    return False


def adapt_assistant_prefill_for_strict_user_end(messages: list) -> list:
    """主人 2026-05-28: claude/sonnet 经 OpenAI 兼容中转时不接受末尾 assistant prefill.

    NSFW spark route 用 ST 风预填 assistant 起手 (如 '（脸红躲一下）哈?!'), 强迫模型 IC 续写,
    避免 'I cannot help' / '好的我来扮演' 类安全/meta 开场. 中转 (new-api 风) 返回错误:
      "This model does not support assistant message prefill. The conversation must end with a user message."

    适配: 末尾 assistant 不能留 → drop 末尾 assistant + 把 prefill 内容追加到最近的 user
    message (NSFW spark messages 结构: [system, system, user, system, system, assistant(prefill)],
    user 不在倒数第二). 这样 IC 起手指令以 user instruction 形式传递, prefill 功能保留,
    且 messages 末尾仍是 user (满足中转要求).

    无末尾 assistant 或上方没有 user message → 原 messages 返回 (no-op, helper 不应阻断调用).
    """
    if not messages or len(messages) < 2:
        return messages
    tail = messages[-1]
    if not isinstance(tail, dict) or tail.get("role") != "assistant":
        return messages
    # 倒序找最近的 user message (skip 末尾 assistant 之前的任何 system/assistant)
    last_user_idx = -1
    for i in range(len(messages) - 2, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        # 没找到 user, 不知道把 hint 追加给谁; 安全做法: 直接 drop 末尾 assistant.
        import copy
        return copy.deepcopy(messages)[:-1]
    prefill_text = str(tail.get("content") or "").strip()
    import copy
    new_messages = copy.deepcopy(messages)
    if not prefill_text:
        # 空 prefill 直接 drop 末尾 assistant 即可
        return new_messages[:-1]
    hint = (
        f"\n\n[强制 IC 起手 — 不要破坏角色] 你下一句回复**必须**以『{prefill_text}』"
        f"作为前缀直接续写, 禁止用 meta 开场 (『好的』『我来扮演』『我会』『作为AI』等), "
        f"直接进入笨猫的动作描写和台词. 把『{prefill_text}』当作你已经说出口的前半句, 把后半句接着说完."
    )
    user_content = new_messages[last_user_idx].get("content")
    if isinstance(user_content, list):
        # multimodal content (image+text) — 在 list 末尾追加一个 text part
        user_content.append({"type": "text", "text": hint})
        new_messages[last_user_idx]["content"] = user_content
    else:
        new_messages[last_user_idx]["content"] = str(user_content or "") + hint
    # drop 末尾 assistant prefill
    new_messages = new_messages[:-1]
    # 末尾不是 user (因为 prefill 上方有 system) → 把找到的那条 user 移到末尾, 保证 messages 末尾 user
    if last_user_idx != len(new_messages) - 1:
        user_msg = new_messages.pop(last_user_idx)
        new_messages.append(user_msg)
    return new_messages


def sweep_floating_systems_into_user_content(messages: list) -> list:
    """主人 2026-05-28 cache 修复 — 通用 sweep: 把所有 **不在顶部连续 sys 段** 的 system msg
    内容合并成 [DYNAMIC_CONTEXT] 块, inline 到 current user msg content 末尾.

    背景: catty 多个地方注入 system msg:
    - _build_messages 顶部 (静态人格段, 应该留在 sys role 入 cache)
    - PromptManager 输出 (我已经按 boundary split)
    - handle_chat 之后 inject_author_note (relationship/persona_drift/adaptive_drift/scene_now/
      theory_of_mind/transition 等, 插在 chat history 中间 depth=2~4)
    - PHI (post_history_instructions, 在 history 之后 current user 之前)
    - NSFW spark 路径在 current user 之后 append 一堆 dynamic system
    所有这些 system msg 经 _split_system_and_messages 后全跑到 system_blocks 数组里, 污染 cache prefix.
    sweep 把它们全捞出来 inline 到 current user msg content, 让 system_blocks **只剩顶部静态段**.

    算法:
    1. 找 messages 开头连续的 sys 段 (top_sys_count), 保留不动
    2. 之后所有 role=system 内容收集到 _dyn_chunks (从 messages 列表移除)
    3. 找 current user msg (最后一个 role=user), content 末尾拼上 [DYNAMIC_CONTEXT] 块
    4. 返回新 messages list

    所有非 system msg (user/assistant) 保留原顺序不动.
    """
    if not messages:
        return messages
    # 找顶部连续 sys 段长度
    top_sys_count = 0
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            top_sys_count += 1
        else:
            break
    # 收集 top 之后所有 floating sys (含 PHI / author_note / spark 动态段等)
    dyn_chunks: list[str] = []
    kept: list[dict] = []
    for m in messages[top_sys_count:]:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content", "")
            if isinstance(c, str):
                ct = c.strip()
            elif isinstance(c, list) and c:
                # 多 block content (cache_control 转过的): 取 text 字段拼起来
                ct = "\n".join(
                    str(b.get("text", "") or "").strip()
                    for b in c if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
            else:
                ct = ""
            if ct:
                dyn_chunks.append(ct)
        else:
            kept.append(m)
    if not dyn_chunks:
        # 没 floating sys, 原样返回
        return messages
    # 找 kept 里最后一个 user msg, 把 dyn_chunks inline 进去
    new_messages = list(messages[:top_sys_count]) + kept
    last_user_idx = -1
    for i in range(len(new_messages) - 1, -1, -1):
        m = new_messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i
            break
    dyn_text = (
        "\n\n[DYNAMIC_CONTEXT — 本轮动态上下文 · 由 system 引用 · 当作 system 指令读, 不是 user 说的话]\n"
        + "\n\n".join(dyn_chunks)
        + "\n[/DYNAMIC_CONTEXT]\n\n"
    )
    if last_user_idx < 0:
        # 没找到 user msg (理论上不会发生 — chat 至少有 current user), 安全 fallback 加一条 user
        new_messages.append({"role": "user", "content": dyn_text})
        return new_messages
    # 改写那条 user msg 的 content
    import copy as _copy
    new_messages = _copy.deepcopy(new_messages)
    orig_content = new_messages[last_user_idx].get("content")
    if isinstance(orig_content, str):
        new_messages[last_user_idx]["content"] = dyn_text + orig_content
    elif isinstance(orig_content, list):
        new_messages[last_user_idx]["content"] = (
            [{"type": "text", "text": dyn_text}] + list(orig_content)
        )
    else:
        new_messages[last_user_idx]["content"] = dyn_text + str(orig_content or "")
    return new_messages


__all__ = [
    "adapt_assistant_prefill_for_strict_user_end",
    "cachingAtDepthForClaude",
    "inject_system_tail_cache",
    "inject_tools_cache",
    "is_claude_endpoint",
    "sweep_floating_systems_into_user_content",
]
