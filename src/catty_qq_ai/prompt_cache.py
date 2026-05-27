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
    # 主人 2026-05-28 C3: CC 风格 — 让 cache prefix 包含整个 history, 而不是只到 msg[0].
    # 之前: cache_control 标 msg[0] (first user), prefix = sys + msg[0] = ~6K, 浪费 history.
    # 现在: 同时标 user_indices[0] (头 anchor) + user_indices[-2] (上一轮 user, byte 稳定).
    # cache prefix 含完整 history (到上一轮 user), cache_create 涨到 ~20K+.
    #
    # 为什么标 user_indices[-2] 而非 [-1]:
    # - [-1] = 当前 user msg, 内容每轮变 (volatile) → cache 永远 miss
    # - [-2] = 上一轮 user msg, 已固化进 history (byte 稳定) → cache 命中
    # - 上一轮 assistant reply 也在 prefix 里 (在 user[-2] 之前), 完整 history 进 cache
    #
    # Anthropic 注: cache_control 只在 user / system / tool blocks 生效, assistant 上被忽略.
    # 所以必须用 user_indices, 不能用 messages[-2] (可能是 assistant).
    user_indices = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"]
    if not user_indices:
        return messages
    # 标 msg[user_indices[0]] (history 头) — 单 user 场景就这一个 anchor
    _mark_cache_control(messages[user_indices[0]])
    # 标 msg[user_indices[-2]] (上一轮 user, history 里最新固化的 user msg) — 多 user 场景才有
    # 这让 prefix 包含完整 history, cache_create 大幅增加.
    if len(user_indices) >= 3:
        # ≥3 个 user: 至少 [first_user, history_user(s), current_user], -2 是中间或上一轮
        anchor_idx = user_indices[-2]
        if anchor_idx != user_indices[0]:  # 避免单 history 场景重复标
            _mark_cache_control(messages[anchor_idx])
    return messages


def _get_cache_ttl() -> str | None:
    """读 config.catty_cache_ttl, 决定 cache TTL ('1h' / '5min' / None=默认5min).

    主人 2026-05-28: CC 在用 1h TTL 让长会话不掉 cache, catty 跟进.
    价格: 1h cache write 2x base (5min write 1.25x), cache read 都 0.1x.
    长会话 (>5min) 场景 1h 显著省钱.
    """
    try:
        from . import config as _module_config
        ttl = getattr(_module_config.config, "catty_cache_ttl", "1h")
        if isinstance(ttl, str) and ttl.strip().lower() in ("1h", "5min", "5m"):
            normalized = ttl.strip().lower().replace("5m", "5min")
            return "1h" if normalized == "1h" else None  # 5min 是默认, 不需要显式传
    except Exception:  # noqa: BLE001
        pass
    return "1h"  # 默认 1h


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
    # 主人 2026-05-28 prompt 优化 C2: 修 bug — 之前在 boundary marker 处直接 return,
    # 但 sweep 漏掉的场景 / 异常路径下 boundary 之后还可能有 sys 段, 不标会让 cache prefix
    # 不完整. 正确做法: **同时记录 boundary_idx 和 last_top_sys, 标在两者较后的位置**
    # (实际上几乎总是 boundary_idx == last_top_sys, 但万一不同就用 last_top_sys 兜底).
    boundary_idx = -1
    last_top_system = -1
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            break  # 离开顶部 system 块
        last_top_system = i
        if boundary_idx < 0 and _has_marker_in_content(msg.get("content"), _CACHE_BOUNDARY_MARKER):
            boundary_idx = i

    # 标在顶部最后一个 sys (可能等于 boundary, 可能是 boundary 之后某段 sweep 漏的 sys)
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
