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
    """给 messages 数组**第一个 user** 标 cache_control: ephemeral (永远稳定的 prefix anchor).

    主人 2026-05-28 经多轮实测 + standalone test 100% hit 验证的最简单 cache 策略:

    1. Anthropic cache 是 prefix-based, 严格字节级匹配. cache_control 标在 messages 数组
       的哪个位置, 决定 cache prefix 长度 (= tools + system + messages[0..marker_idx]).
    2. catty 真实对话每轮 history 顺序追加 (user_1, asst_1, ..., user_curr). 如果 cache
       marker 在末尾 (depth=N 处 / 倒数第二个 user / current user), 每轮 marker 位置都在
       变 → cache prefix 长度变 → 严格 prefix matching 失败 → cache 永远 miss.
    3. **唯一稳定 cache prefix 的方法**: marker 永远标在第一个 user (messages 里出现的
       第一个 role=user 位置). 这条 user 内容随 history 滚动 (catty_history_turns=16 内)
       不变 → 每轮 cache prefix 字节一致 → 真能 hit.
    4. Anthropic 只接受 cache_control 在 user / system / tool content blocks 上,
       assistant 上的被忽略.

    主人 2026-05-28 进一步发现: messages 数组里如果只有**1 个 user** (= current user),
    内容每次变 → 标在它上面 cache 永远 miss. 必须**至少有 2 个 user** (≥1 个 history user)
    才标在第一个 history user 上 (current 之前的某个 user). 只 1 个 user 时不标 marker
    (cache 不写, 但不浪费).

    cachingAtDepth 参数保留向后兼容 (不再生效).

    Args:
        messages: ChatMessage list (会就地修改 + 返回相同 list)
        cachingAtDepth: 保留兼容, 不再生效
    """
    # 主人 2026-05-28: Anthropic 写 cache 的硬性要求 — cache_control 必须**至少有 1 个
    # 在 messages 数组上** (standalone test 验证 system-only cache_control = cache_create=0).
    # 多个 breakpoints 设计: Anthropic 找最长匹配 prefix → 即使 messages 末尾 user content
    # 每次变, system 末尾 marker (inject_system_tail_cache 标的 boundary block) 那个
    # breakpoint 还能 hit prefix 段.
    user_indices = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"]
    if not user_indices:
        return messages
    # 策略: 永远标 messages 数组的**第一个 user** + 当 user >= 2 时, 也标当前(最后)
    # user 一次. 单 user 场景(只 current user)就标 current — 让 cache 至少写入, sys[21]
    # 那个 breakpoint 仍能跨请求 hit.
    _mark_cache_control(messages[user_indices[0]])
    # 若有 history user (>= 2 个 user), 额外在最后一个 user (current) 也标 marker.
    # Anthropic 4 breakpoints 上限: sys[boundary] + msg[first_user] + msg[last_user] = 3,
    # 留 1 个余量给 tools cache (CC 不加, catty 也不加).
    if len(user_indices) >= 2:
        _mark_cache_control(messages[user_indices[-1]])
    return messages


def _mark_cache_control(msg: dict[str, Any]) -> None:
    """把 cache_control: {type: ephemeral} 加到 message content 的最后一个 block.

    Claude API 要求 cache_control 必须在 content block 上, 不能在 message 顶层.
    自动把 str content 转成 list[{type: text, text: ..., cache_control: ...}] 单 block 格式.
    """
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = {"type": "ephemeral"}


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
    # Phase A3: 优先找 boundary marker
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            break  # 离开顶部 system 块, 停止 (boundary 必须在顶部 system 块内)
        if _has_marker_in_content(msg.get("content"), _CACHE_BOUNDARY_MARKER):
            _mark_cache_control(msg)
            return messages

    # fallback: 老逻辑 (顶部连续 system 末尾)
    last_top_system = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            last_top_system = i
        else:
            break
    if last_top_system >= 0:
        _mark_cache_control(messages[last_top_system])
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
        last_copy["cache_control"] = {"type": "ephemeral"}
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


__all__ = [
    "adapt_assistant_prefill_for_strict_user_end",
    "cachingAtDepthForClaude",
    "inject_system_tail_cache",
    "inject_tools_cache",
    "is_claude_endpoint",
]
