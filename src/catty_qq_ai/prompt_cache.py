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
    """vscode messagesApi.ts:593-604 公式: 倒数 2 条可缓存 messages 末 block 标 cache_control.

    主人 2026-05-28 C18 (基于 microsoft/vscode 调研):
    之前的 user_indices[0] + user_indices[-1] 在 history batch trim (40→20) 时 user[0] 漂移,
    那条 cache prefix 直接废. vscode 标"倒数 2 条 messages"在稳态下够用, 即使第一条 TTL 过期/漂移
    还有第二条 fallback, 最多丢 1 轮对话而不是整段 cache reset.

    标位逻辑:
    - 从 messages 末尾倒序遍历, 找到第一条 content 含可缓存 block 的 → 标
    - 继续倒序, 找到第二条可缓存的 → 标
    - 最多标 2 条 (cachingAtDepth 参数控制)
    - 跳过 thinking_block / redacted_thinking_block (Anthropic 限制, 不能标 cache)

    参考: vscode extensions/copilot/src/platform/endpoint/node/messagesApi.ts:593-604
    """
    if not messages:
        return messages
    marked = 0
    for i in range(len(messages) - 1, -1, -1):
        if marked >= cachingAtDepth:
            break
        m = messages[i]
        if not isinstance(m, dict):
            continue
        # 只标 user/assistant; system 由 inject_system_tail_cache 处理
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        # 检查 content 是否含可缓存 block (跳 thinking)
        if isinstance(content, str) and content.strip():
            _mark_cache_control(m)
            marked += 1
        elif isinstance(content, list):
            # 倒序找最后一个非 thinking 的 block
            has_cacheable = any(
                isinstance(b, dict)
                and b.get("type") not in ("thinking", "redacted_thinking")
                for b in content
            )
            if has_cacheable:
                _mark_cache_control(m)
                marked += 1
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
    """把 cache_control 加到 message content 的最后一个**可缓存** block.

    Claude API 要求 cache_control 必须在 content block 上, 不能在 message 顶层.
    自动把 str content 转成 list[{type: text, text: ..., cache_control: ...}] 单 block 格式.

    主人 2026-05-28 C18 (vscode messagesApi.ts contentBlockSupportsCacheControl):
    跳过 thinking_block / redacted_thinking_block — Anthropic 限制这俩不能标 cache.
    倒序找最后一个非 thinking 的 block 标 cache_control.
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
        # 倒序找最后一个可缓存 block (跳 thinking / redacted_thinking)
        for i in range(len(content) - 1, -1, -1):
            blk = content[i]
            if isinstance(blk, dict) and blk.get("type") not in ("thinking", "redacted_thinking"):
                blk["cache_control"] = cc
                return


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
    # 主人 2026-05-28 C8: 全局扫描 boundary, fallback 仍用顶部连续段末尾.
    # 之前 bug: 循环遇到非 system msg 就 break, 错过 inject_author_note 中间插入后的
    # boundary marker (boundary 在 history 中间夹的 author_note 后面那段 system 里).
    # 修复: boundary 全局扫描所有 role=system msg; fallback last_top_system 仍记顶部连续段
    # (保证 fallback 路径标在静态段附近, 不标到动态尾段).
    boundary_idx = -1
    last_top_system = -1
    top_continuous_ended = False
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            top_continuous_ended = True
            continue  # 不 break, 继续全局扫描 boundary
        if not top_continuous_ended:
            last_top_system = i  # 仅在顶部连续段记 last_top
        if boundary_idx < 0 and _has_marker_in_content(msg.get("content"), _CACHE_BOUNDARY_MARKER):
            boundary_idx = i  # boundary 全局扫描

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


# ============================================================================
# DeepSeek KV cache 前缀稳定优化工具集 (OpenAI compat path 专用)
# 主人 2026-05-28: DeepSeek 硬盘缓存是服务端自动 token 级精确前缀匹配, 不需要
# cache_control 字段, 但前缀必须字节稳定. 这组工具用于 _post_chat_completion_raw
# 在发请求前对 messages/tools 做规整, 让 cache 命中率稳到 95-98%.
# 参考: dist/deepseek硬盘缓存规则.txt + QwenLM/qwen-code#4065 翻车案例.
# ============================================================================


def strip_all_cache_control(
    messages: list[dict], tools: list[dict] | None = None,
) -> int:
    """深扫所有 message.content blocks + tools, pop cache_control 字段.

    DeepSeek OpenAI compat 端点忽略未知字段, 但 strip 掉能减少网络传输 + 让
    prefix_hash 诊断日志更干净. 与 anthropic_native_client._strip_existing_cache_control
    保持独立 (Anthropic 模块自包含).
    """
    stripped = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.pop("cache_control", None) is not None:
                    stripped += 1
    if tools:
        for t in tools:
            if isinstance(t, dict) and t.pop("cache_control", None) is not None:
                stripped += 1
    return stripped


def merge_consecutive_system_messages(messages: list[dict]) -> int:
    """合并 messages 开头连续的 role=system 段成单条 (content 用 \\n\\n 拼接).

    返回合并掉的条数 (减少的 messages 长度).
    in-place 修改 messages.

    content 形态处理:
    - str: 直接拼
    - list[{type:text, text}]: 提取 text 拼成 str (DeepSeek OpenAI compat 标准是 str)
    - 其他: str() fallback
    """
    if not messages:
        return 0
    head_sys_idx: list[int] = []
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "system":
            head_sys_idx.append(i)
        else:
            break
    if len(head_sys_idx) < 2:
        return 0

    def _to_text(c: object) -> str:
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts: list[str] = []
            for blk in c:
                if isinstance(blk, dict):
                    t = blk.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            return "\n\n".join(parts)
        return str(c or "")

    merged_text = "\n\n".join(_to_text(messages[i].get("content")) for i in head_sys_idx)
    keep = head_sys_idx[0]
    messages[keep]["content"] = merged_text
    # 倒序删除其余 head_sys_idx
    for i in reversed(head_sys_idx[1:]):
        del messages[i]
    return len(head_sys_idx) - 1


def stabilize_tools_order(tools: list[dict]) -> int:
    """tools 数组按 function.name 字典序排序 (in-place), 返回是否发生重排 (0/1).

    应对 QwenLM/qwen-code#4065 实测翻车: tools 顺序变 → 命中 97.5% → 81.5%.
    DeepSeek 不依赖 tools 顺序做语义判断, 字典序排序无副作用且锁死前缀字节.
    """
    if not tools or len(tools) < 2:
        return 0

    def _key(t: object) -> str:
        if not isinstance(t, dict):
            return ""
        fn = t.get("function")
        if isinstance(fn, dict):
            n = fn.get("name")
            if isinstance(n, str):
                return n
        n = t.get("name")
        return n if isinstance(n, str) else ""

    original = [_key(t) for t in tools]
    tools.sort(key=_key)
    sorted_keys = [_key(t) for t in tools]
    return 1 if original != sorted_keys else 0


# 历史 user 里的 inline 动态段 — 每次内容都变 (author_note / PHI / NSFW spark /
# vibe / 时间感等), 留在 history 会让 first_user_md5 每次都变 → DeepSeek 从 sys
# 段之后全 miss. current turn 最后一条 user 必须保留完整版给 LLM 读, 但 history
# 中的旧 user 只该保留主人原始问答.
import re as _re_inline_strip

_INLINE_DYNAMIC_CONTEXT_RE = _re_inline_strip.compile(
    r"\n*\[DYNAMIC_CONTEXT[^\]]*\].*?\[/DYNAMIC_CONTEXT\]\n*",
    flags=_re_inline_strip.DOTALL,
)
_INLINE_INTERNAL_INSTRUCTION_RE = _re_inline_strip.compile(
    r"\n*<<<CATTY_INTERNAL_INSTRUCTION.*?<<<END_INTERNAL>>>\n*",
    flags=_re_inline_strip.DOTALL,
)
# 主人 2026-05-28 plan-quizzical-crane Step 5: 历史 user 开头 [QQ:数字] 发言者前缀剥离.
# 群聊场景每条 user 开头都带不同的 [QQ:发言者ID], 历史里完全没用 (LLM 看 current turn 知道
# 当前是谁) 且每条字节都不同 → DeepSeek cache 命中率从 history[0] 开始全 miss.
# 主人原话: "依赖 QQ 号, QQ 号不放 cache 里面就行了, 不要一次性把 QQ 号都喂给 AI,
# 只喂发言的不就行了? 还有发言提到的?" → 剥开头, 中部 @QQ mention 保留.
_HISTORY_SPEAKER_PREFIX_RE = _re_inline_strip.compile(r"^\[QQ:\d+\]\s*")


def strip_inline_dynamic_from_text(text: str) -> str:
    """单段文本版本: 剥 [DYNAMIC_CONTEXT...] 和 <<<CATTY_INTERNAL_INSTRUCTION...>>> 段.

    主要给 NLU / token estimator 用 — 让算 token 时看 "主人原话" 版本,
    不被注入的指令段污染. messages 实际内容不动.
    """
    if not text or not isinstance(text, str):
        return text or ""
    text = _INLINE_DYNAMIC_CONTEXT_RE.sub("", text)
    text = _INLINE_INTERNAL_INSTRUCTION_RE.sub("", text)
    return text


def strip_inline_dynamic_segments_from_history(messages: list[dict]) -> int:
    """剥离历史 user message 里的 inline 动态段, 保留 current turn 最后一条 user 不动.

    背景: 主人 commits 828fb43 / 45be439 把 floating system 段 inline 到 user
    content 末尾 (防 cache_control 污染), 但每次动态段内容都变 (author_note /
    PHI / NSFW spark / vibe / 时间感), 导致上一轮的 enriched user 留在历史中
    → first_user_md5 每次都变 → DeepSeek 从 sys 段之后全 miss
    (实测: 6656 hit / 4827 miss / 58% hit_rate, 跟 CherryHQ/cherry-studio#14695
    同款翻车).

    修复: history 中的 user 只保留主人原始问答, current turn 最后一条 user 不动
    (LLM 必须读到完整动态段). 这样 sys + history user/asst 全字节稳定, DeepSeek
    cache 能从前缀末端一路命中到 history 倒数第二条.

    in-place 修改 messages. 返回剥离段数 (诊断用).
    """
    if not messages:
        return 0
    # 找最后一条 user 的 index — 不能 strip 它
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i
            break

    stripped = 0
    for i, m in enumerate(messages):
        if i == last_user_idx:
            continue  # current turn 不动 (含 QQ 号让 LLM 知道当前发言者)
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            new_content, n1 = _INLINE_DYNAMIC_CONTEXT_RE.subn("", content)
            new_content, n2 = _INLINE_INTERNAL_INSTRUCTION_RE.subn("", new_content)
            # 主人 2026-05-29 P0: 不再 strip 开头 [QQ:数字] 前缀. 旧逻辑(Step 5)剥它本想稳前缀,
            # 实则制造 full-vs-stripped 漂移 — 上一轮 current user 带 [QQ:], 存进 history 也带,
            # 但发请求时被剥 → 同一位置「上轮当 current(带前缀) vs 本轮当 history(被剥)」字节不同
            # → 群聊前缀每轮断在最近一条. 保留 [QQ:] 让发送版与存储版一致(不漂移), 且 per-message
            # 发言者归属对 AI 更清晰(配合 catty_qq_nickname_map 翻译). DYNAMIC_CONTEXT/INTERNAL
            # 仍剥(清理 legacy 持久化 history; 新 history 已不再内联).
            if n1 or n2:
                m["content"] = new_content
                stripped += n1 + n2
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    txt = blk.get("text", "")
                    if isinstance(txt, str):
                        new_txt, n1 = _INLINE_DYNAMIC_CONTEXT_RE.subn("", txt)
                        new_txt, n2 = _INLINE_INTERNAL_INSTRUCTION_RE.subn("", new_txt)
                        # 主人 2026-05-29 P0: 同上, 不再 strip [QQ:数字] 前缀 (防 current↔history 漂移).
                        if n1 or n2:
                            blk["text"] = new_txt
                            stripped += n1 + n2
    return stripped


def collapse_trailing_systems_into_last_user(messages: list[dict]) -> int:
    """把 current user **之后**的末尾连续 system 段, inline 合并进那条 user 的 content,
    让 messages 结尾从 system 变回 user — 修 DeepSeek 群聊 history 不进 cache 的根因。

    主人 2026-05-30 决定性实验 (真实 dump 重放 aigcbest+flash+21tools, 各发2次):
      - B0 原样(末尾=system, 带tools): r2 hit 死锁 7808 — history **永不进 cache**
      - V_user 去trailing(末尾=user): r2 hit 7808→10240 — history 进了
      - V_inline trailing拼进user(末尾=user): r2 hit 7808→12224 — history 进了
      - V_asst 末尾加assistant prefill: r2 hit 7808→12224 — history 进了
    根因: DeepSeek 缓存只在【用户输入结束位置】/【模型输出结束位置】落盘 turn 边界单元
    (deepseek硬盘缓存规则.txt:10). catty 群聊把 trailing system (mood/sender/vibe/ambient)
    放在 current user 之后 → messages 结尾是 system, 既非 user-end 也非 output-end →
    history 那段永远落不了可复用单元, 只剩 system[0] 公共前缀命中 (死锁 7808).
    spark(pro) 末尾是 assistant prefill (= output-end) 所以能命中 history — 这就是
    pro/flash 表现差异的真因 (模型机制一样, 差在请求结构, 主人 2026-05-30 已指出).

    本函数把末尾连续 system 用 [DYNAMIC_CONTEXT] wrap 后 append 进最近的 user content,
    并删掉那些 system, 使 messages 结尾 = user → history 进 cache. Round 18 之前的
    sweep_floating_systems_into_user_content 是同思路, 但那个会卷入 history 中间的
    author_note; 本函数**只动末尾连续 system**, 更精准且零误伤.

    **自动区分路径 (关键设计)**:
      - 群聊主路径: 末尾是 system → 触发 collapse → 修复
      - spark 路径: 末尾是 assistant prefill → **不触发** (末尾非 system) → spark 完全不受影响
    下一轮这条 enriched user 进 history 后, current user 之前的 history 由
    strip_inline_dynamic_segments_from_history 剥掉 [DYNAMIC_CONTEXT] → 字节稳定不漂移.

    in-place 修改 messages (调用方已 deepcopy). 返回合并掉的 system 段数 (0=未触发).
    """
    if not messages:
        return 0
    # 末尾必须是 system 才触发 (末尾是 user/assistant 一律不动 → 保护 spark prefill 结尾)
    last = messages[-1]
    if not (isinstance(last, dict) and last.get("role") == "system"):
        return 0
    # 从末尾往前找连续 system 段的起点
    i = len(messages) - 1
    while i >= 0 and isinstance(messages[i], dict) and messages[i].get("role") == "system":
        i -= 1
    tail_start = i + 1  # messages[tail_start:] 全是末尾连续 system
    # 优先贴到紧邻 tail 的 current user; 若中间被空/未知消息隔开, 向前找最近 user。
    # 但遇到 assistant/tool 说明已经越过模型输出边界, 不折叠以免改变语义。
    user_idx = i
    if not (user_idx >= 0 and isinstance(messages[user_idx], dict) and messages[user_idx].get("role") == "user"):
        user_idx = -1
        for j in range(i, -1, -1):
            msg = messages[j]
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "user":
                user_idx = j
                break
            if role in {"assistant", "tool"}:
                break
        if user_idx < 0:
            return 0

    def _to_text(c: object) -> str:
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(
                str(b.get("text", "") or "")
                for b in c if isinstance(b, dict) and b.get("type") == "text"
            )
        return str(c or "")

    chunks = [
        _to_text(m.get("content")).strip()
        for m in messages[tail_start:]
        if isinstance(m, dict) and _to_text(m.get("content")).strip()
    ]
    if not chunks:
        # 末尾 system 全是空 — 直接删掉让结尾回到 user
        n_removed = len(messages) - tail_start
        del messages[tail_start:]
        return n_removed
    dyn_text = (
        "\n\n[DYNAMIC_CONTEXT — 本轮动态上下文 · 由 system 引用 · 当作 system 指令读, 不是 user 说的话]\n"
        + "\n\n".join(chunks)
        + "\n[/DYNAMIC_CONTEXT]"
    )
    user_content = messages[user_idx].get("content")
    if isinstance(user_content, list):
        # multimodal (image+text): 末尾追加一个 text part, 保持结尾 user
        user_content.append({"type": "text", "text": dyn_text})
        messages[user_idx]["content"] = user_content
    else:
        messages[user_idx]["content"] = str(user_content or "") + dyn_text
    n_removed = len(messages) - tail_start
    del messages[tail_start:]
    return n_removed


def compute_prefix_hash(
    messages: list[dict], tools: list[dict] | None = None, n: int = 5,
) -> dict[str, Any]:
    """计算 messages 前 N 条 + tools 序列的 content hash, 诊断前缀漂移用.

    返回 {
        "sys_count": int,
        "msg_count": int,
        "sys_md5": "xxxxxxxx",         # 前 N 条 system 段拼接后 md5[:8]
        "first_user_md5": "xxxxxxxx",  # 第一条 user msg content md5[:8]
        "tools_count": int,
        "tools_md5": "xxxxxxxx",       # tools 序列化后 md5[:8]
    }
    """
    import hashlib
    import json

    def _to_text(c: object) -> str:
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts: list[str] = []
            for blk in c:
                if isinstance(blk, dict):
                    t = blk.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            return "\n\n".join(parts)
        return str(c or "")

    def _md5_8(s: str) -> str:
        return hashlib.md5(s.encode("utf-8", errors="replace")).hexdigest()[:8]

    sys_count = 0
    sys_parts: list[str] = []
    first_user_text = ""
    head = messages[:n] if messages else []
    for m in head:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            sys_count += 1
            sys_parts.append(_to_text(m.get("content")))
        elif role == "user" and not first_user_text:
            first_user_text = _to_text(m.get("content"))

    tools_count = 0
    tools_serialized = ""
    if tools:
        tools_count = len(tools)
        try:
            tools_serialized = json.dumps(
                tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            )
        except Exception:
            tools_serialized = str(tools)

    # 主人 2026-05-29 Round 1 诊断: per-message hash 定位 history 中段漂移
    # 输出每条 message 的 (role, len, md5) 拼成短字符串, 直接 log 出来对比两次请求.
    per_msg: list[str] = []
    for i, m in enumerate(messages[:20]):  # 只算前 20 条 (头部 prefix 才进 cache)
        if not isinstance(m, dict):
            continue
        r = m.get("role", "?")[:1]  # s/u/a/t (system/user/assistant/tool)
        c = _to_text(m.get("content"))
        per_msg.append(f"{i}{r}{len(c)}#{_md5_8(c)}")

    return {
        "sys_count": sys_count,
        "msg_count": len(messages),
        "sys_md5": _md5_8("\n\n".join(sys_parts)),
        "first_user_md5": _md5_8(first_user_text),
        "tools_count": tools_count,
        "tools_md5": _md5_8(tools_serialized),
        "per_msg": " ".join(per_msg),  # 诊断: 每条 message 的 idx + role + len + md5
    }


__all__ = [
    "adapt_assistant_prefill_for_strict_user_end",
    "cachingAtDepthForClaude",
    "collapse_trailing_systems_into_last_user",
    "compute_prefix_hash",
    "inject_system_tail_cache",
    "inject_tools_cache",
    "is_claude_endpoint",
    "merge_consecutive_system_messages",
    "stabilize_tools_order",
    "strip_all_cache_control",
    "strip_inline_dynamic_from_text",
    "strip_inline_dynamic_segments_from_history",
    "sweep_floating_systems_into_user_content",
]
