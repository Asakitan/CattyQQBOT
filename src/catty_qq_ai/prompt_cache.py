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
    """ST PR #3085 算法移植 — 末尾倒数 role 转换处注 2 个 breakpoint (depth 和 depth+2).

    Args:
        messages: ChatMessage list (会就地修改 + 返回相同 list, 调用方可链式)
        cachingAtDepth: 从倒数第 N 处 role 切换开始打 breakpoint, 推荐偶数.
            默认 2: 最近第 2 + 第 4 处 role 切换打 breakpoint, 覆盖最近一两轮.

    特性:
    - 从末尾倒数, 跳过尾部 prefill (assistant role 末尾段)
    - role 切换时计 depth, 打 cache_control 在 content 最后一个 block
    - 自动把 str content 转成 list[dict] (Claude 要求 cache_control 在 block 上)
    """
    if cachingAtDepth < 0:
        return messages

    passed_prefill = False
    depth = 0
    prev_role = ""

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        # 跳过末尾的 assistant prefill (continuation hint)
        if not passed_prefill and msg.get("role") == "assistant":
            continue
        passed_prefill = True

        if msg.get("role") != prev_role:
            if depth == cachingAtDepth or depth == cachingAtDepth + 2:
                _mark_cache_control(msg)
            if depth == cachingAtDepth + 2:
                break
            depth += 1
            prev_role = msg.get("role", "")

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


def inject_system_tail_cache(messages: list[dict]) -> list[dict]:
    """给最后一个 system message 注入 cache_control (ST 标准做法的另一个 breakpoint).

    这一个 breakpoint 覆盖整个 system 块前缀 (persona + override + recency_reminder).
    对主人对话 system 块基本静态, 每轮都能命中.
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "system":
            _mark_cache_control(messages[i])
            break
    return messages


def is_claude_endpoint(base_url: str, model: str) -> bool:
    """判断当前 endpoint 是否走 Claude / Anthropic 协议 — 决定要不要注入 cache_control."""
    bu = (base_url or "").lower()
    m = (model or "").lower()
    if "anthropic" in bu or "claude" in bu:
        return True
    if "claude" in m or "anthropic" in m:
        return True
    # 中间人 (hugou.cc / openrouter) 模型名含 claude/anthropic 也走 cache_control
    return False


__all__ = [
    "cachingAtDepthForClaude",
    "inject_system_tail_cache",
    "is_claude_endpoint",
]
