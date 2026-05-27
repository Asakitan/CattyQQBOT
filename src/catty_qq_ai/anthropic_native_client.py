"""Anthropic 原生 /v1/messages 路径 — 走 anthropic SDK.

主人 2026-05-28: NewAPI SG relay 通过 pass_through_body_enabled=true 字节级透传 body
后, catty 可以直接发 native /v1/messages 走 anthropic SDK, 享受三个 beta:
1. prompt-caching-2024-07-31  — cache_control breakpoint (OAuth MAX 静默忽略, 等付费 key 自动 90% 折扣)
2. compact-2026-01-12         — server-side conversation compaction (input>150K 自动触发 server 摘要)
3. context-management-2025-06-27 — context_management 字段官方接受标识

不取代 _post_chat_completion (OpenAI-compat 路径仍可用), 仅做新分支. chat_completion
依据 config.catty_anthropic_native_enabled 决定走哪条路.

输入 / 输出契约:
- 输入 messages: OpenAI 风格 (顶部 system messages + user/assistant 序列), 可能已经
  被 prompt_cache._mark_cache_control 转成 content=list[{type:text,text,cache_control}]
- 输出: dict shape 跟 _post_chat_completion_raw 一致 (choices[0].message.content + usage)
  额外字段:
    _native_raw_content       — list[block] 原始 response.content (含可能的 compaction block)
    _native_applied_edits     — list[dict] response.context_management.applied_edits (compaction trigger 标记)
    usage.iterations          — Anthropic Compaction API 在 compaction 触发时返回
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("catty_qq_ai.anthropic_native")

_DEFAULT_BETAS = (
    "prompt-caching-2024-07-31,"
    "compact-2026-01-12,"
    "context-management-2025-06-27"
)


def _split_system_and_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """OpenAI 风格 messages → Anthropic native 分离 system / 对话.

    /v1/messages 协议: system 是顶层字段 (list of TextBlock), messages 数组**绝对不能**
    含 role=system 的元素 (会 400 "Unexpected role system"). catty 的 prompt_manager
    在顶部连续注册若干 system 段, 中间还可能动态插 system (author_note / user_vibe /
    NSFW gate / reply_self_check 等 order 460+ 的"动态段"), 所以这里必须把**全部**
    role=system 都提到顶层 system blocks (按原顺序), 不能只提顶部连续段.

    cache_control 的 native 位置: 在 block 上, 不在 message 顶层. prompt_cache.py 已经
    把 system message 的 content 转成 list[{type:text,text,cache_control?}] 格式,
    所以这里直接复用 block 即可保留 cache 标记.

    Args:
        messages: OpenAI 风格 ChatMessage list

    Returns:
        (system_blocks, non_system_messages)
        system_blocks: [{"type": "text", "text": str, "cache_control"?: {...}}, ...]
        non_system_messages: [{"role": "user"|"assistant", "content": ...}, ...]
    """
    system_blocks: list[dict] = []
    other_messages: list[dict] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            # **所有** role=system 都提到顶层 system 字段, 按原 messages 顺序保留语义.
            # 中间夹的 system (动态段) 也算 prompt 一部分, 拼到 top-level 上下文里效果相同.
            if isinstance(content, str):
                system_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                # 已经是 native block 格式 (prompt_cache._mark_cache_control 转过的)
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        # 浅拷贝避免污染入参
                        system_blocks.append(dict(blk))
            # 其它非 str/list content 类型 (None, dict, etc.) 跳过避免污染
        else:
            # 保留原始 message (可能含 multimodal content list)
            other_messages.append(msg)

    return system_blocks, other_messages


def _normalize_message_content(msg: dict) -> dict:
    """把 OpenAI 风格 user/assistant message 转成 Anthropic native block format.

    Anthropic /v1/messages 接受两种 content:
    - str: 简单文本 (会被 SDK 自动包成 [{type:text,text}])
    - list[block]: 多模态 / 含 cache_control 时必须用这个

    OpenAI 风格:
    - str: 同样接受
    - list[part] (multimodal): {type: text|image_url, ...}

    转换规则:
    - str → 不动 (SDK 自动包装)
    - list[block]: 如果 block 已经是 Anthropic 格式 (type=text/image/tool_use/tool_result/compaction), 不动
    - list[part]: 把 OpenAI image_url 转成 Anthropic image source 格式

    保留 cache_control 字段不动.
    """
    role = msg.get("role")
    content = msg.get("content")
    if not isinstance(content, list):
        # str / None / 其它: 直接返回
        return msg
    # list content: 检查每个 block 是否需要转换
    new_blocks: list[dict] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        btype = blk.get("type")
        if btype == "image_url":
            # OpenAI 风格 → Anthropic 风格
            img = blk.get("image_url") or {}
            url = img.get("url") if isinstance(img, dict) else None
            if isinstance(url, str) and url.startswith("data:"):
                # data:image/png;base64,xxx → base64 source
                try:
                    header, b64 = url.split(",", 1)
                    media_type = header.split(";")[0].replace("data:", "")
                    new_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    })
                except ValueError:
                    logger.warning("malformed data URL in image_url block, skipped")
            elif isinstance(url, str):
                # http(s) URL
                new_blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
            continue
        # 其它 type (text / image / tool_use / tool_result / compaction) 复用
        new_blocks.append(dict(blk))
    new_msg = dict(msg)
    new_msg["content"] = new_blocks
    return new_msg


def _extract_text_and_raw(response: Any) -> tuple[str, list[dict]]:
    """从 anthropic response.content 提取文本 + 保留 raw blocks (含 compaction block).

    Returns:
        (text, raw_blocks)
        text: 所有 type=text 的 block 拼接
        raw_blocks: list[dict] 原始 content (用于上层识别 compaction block 持久化)
    """
    text_parts: list[str] = []
    raw_blocks: list[dict] = []
    for block in response.content or []:
        if hasattr(block, "model_dump"):
            blk_dict = block.model_dump(exclude_none=True)
        elif isinstance(block, dict):
            blk_dict = dict(block)
        else:
            continue
        raw_blocks.append(blk_dict)
        if blk_dict.get("type") == "text":
            txt = blk_dict.get("text")
            if isinstance(txt, str):
                text_parts.append(txt)
    return "".join(text_parts), raw_blocks


def _extract_applied_edits(response: Any) -> list[dict]:
    """从 response.context_management.applied_edits 提取 compaction trigger 信息.

    Anthropic Compaction API: 当 input_tokens > trigger 时, server 端跑摘要并在
    response.content 里插入 compaction block, 同时 response.context_management.applied_edits
    会列出"已应用"的 edit (例如 [{"type": "compact_20260112"}]).
    """
    cm = getattr(response, "context_management", None)
    if cm is None:
        return []
    ae = getattr(cm, "applied_edits", None)
    if not ae:
        return []
    edits: list[dict] = []
    for edit in ae:
        if hasattr(edit, "model_dump"):
            edits.append(edit.model_dump(exclude_none=True))
        elif isinstance(edit, dict):
            edits.append(dict(edit))
    return edits


def _native_response_to_openai_shape(response: Any) -> dict[str, Any]:
    """anthropic.types.Message → OpenAI chat completion shape.

    让 chat_completion 上层完全无感切换. 额外字段以 `_native_*` 前缀传递.
    """
    text, raw_blocks = _extract_text_and_raw(response)
    applied_edits = _extract_applied_edits(response)

    usage_obj = getattr(response, "usage", None)
    if hasattr(usage_obj, "model_dump"):
        usage = usage_obj.model_dump(exclude_none=True)
    elif isinstance(usage_obj, dict):
        usage = dict(usage_obj)
    else:
        usage = {}

    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": text,
                    "_native_raw_content": raw_blocks,
                },
                "finish_reason": getattr(response, "stop_reason", None) or "stop",
            }
        ],
        "usage": usage,
        "model": getattr(response, "model", ""),
        "_native_applied_edits": applied_edits,
    }


def _log_native_usage(data: dict[str, Any], model: str) -> None:
    """打印 native /v1/messages 的 usage + compaction 触发情况.

    跟 _log_cache_stats 同款字段, 但额外打印 applied_edits[] 标记 compaction 是否触发.
    """
    usage = data.get("usage") or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    total_input = cache_read + cache_create + input_tokens
    hit_rate = (cache_read / total_input) if total_input > 0 else 0.0
    applied_edits = data.get("_native_applied_edits") or []
    compaction_flag = ""
    for edit in applied_edits:
        if isinstance(edit, dict) and "compact" in str(edit.get("type", "")):
            compaction_flag = " COMPACTION_TRIGGERED"
            break
    logger.info(
        "native /v1/messages model=%s read=%d create=%d new=%d hit=%.0f%%%s",
        (model or "")[:20], cache_read, cache_create, input_tokens, hit_rate * 100, compaction_flag,
    )


async def post_messages_native(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float | None = None,
    timeout: float = 600.0,
    enable_compaction: bool = False,
    compaction_trigger_tokens: int = 150000,
    extra_betas: list[str] | None = None,
    tools: list[dict] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """走 anthropic AsyncAnthropic SDK 发 /v1/messages, 返回 OpenAI-compat shape response.

    Args:
        base_url:  NewAPI SG relay (e.g., http://localhost:3000) 或 native api.anthropic.com
        api_key:   中转 token (sk-19...) 或付费 key (sk-ant-api03-...)
        model:     claude-sonnet-4-6 / claude-opus-4-7 ...
        messages:  OpenAI 风格 ChatMessage list (含顶部 system 段 + 对话)
        max_tokens: 单次回复上限
        temperature: 可选 sampling
        timeout:   网络超时 (默认 600s, 跟 Claude Code 一样支持 long context)
        enable_compaction: 是否启用 server-side compaction (context_management edits)
        compaction_trigger_tokens: input_tokens 超此值时 server 自动跑摘要
        extra_betas: 额外 anthropic-beta header (附加在默认 3 个之后)
        tools: 可选 Anthropic tools 列表
        extra_headers: 额外 default_headers (debug 用)

    Returns:
        OpenAI-compat dict, 见 _native_response_to_openai_shape doc.
    """
    # lazy import 避免 anthropic 未装时整个模块加载失败
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic SDK 未安装. 请运行 pip install anthropic>=0.45.0"
        ) from exc

    betas = _DEFAULT_BETAS
    if extra_betas:
        betas = betas + "," + ",".join(extra_betas)

    headers: dict[str, str] = {"anthropic-beta": betas}
    if extra_headers:
        headers.update(extra_headers)

    # base_url normalize: anthropic SDK 内部会拼 /v1/messages, base_url 末尾若有 /v1 要剥掉
    # 主人 config.json 用的是 OpenAI-compat 风格 "http://localhost:3000/v1", 走 native 时必须改回根
    sdk_base_url = base_url.rstrip("/")
    if sdk_base_url.endswith("/v1"):
        sdk_base_url = sdk_base_url[:-3].rstrip("/")

    # AsyncAnthropic 实例不复用 (单次 chat_completion 调用就建一次, 跟 _post_chat_completion 行为一致)
    # 主人后续若要复用可拉到 module-level singleton
    client = anthropic.AsyncAnthropic(
        base_url=sdk_base_url,
        api_key=api_key,
        default_headers=headers,
        timeout=timeout,
    )

    # 拆分顶部 system + 对话, 并 normalize multimodal content
    system_blocks, other_messages = _split_system_and_messages(messages)
    other_messages = [_normalize_message_content(m) for m in other_messages]

    create_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": other_messages,
    }
    if system_blocks:
        create_kwargs["system"] = system_blocks
    if temperature is not None:
        create_kwargs["temperature"] = temperature
    if tools:
        create_kwargs["tools"] = tools

    # server-side compaction (compact-2026-01-12)
    if enable_compaction:
        create_kwargs["extra_body"] = {
            "context_management": {
                "edits": [
                    {
                        "type": "compact_20260112",
                        "trigger": {
                            "type": "input_tokens",
                            "value": compaction_trigger_tokens,
                        },
                    }
                ]
            }
        }

    # 主人 2026-05-28 cache 诊断: 算 system + messages 的 sha256, 对比同 scope 连发
    # 时 prefix 是否字节一致. cache_create > 0 但 cache_read=0 表示 Anthropic 在写 cache
    # 但 catty 下一轮 prefix hash 不同 → 找不到上次的 cache. 用 prefix_hash log 可以
    # 看出 5min TTL 内同 scope 是否字节稳定 (sys hash 同→应该 cache hit).
    try:
        import hashlib
        import json as _json
        sys_hash = hashlib.sha256(
            _json.dumps(system_blocks, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        stable_msgs = other_messages[:-1] if other_messages else []
        msg_hash = hashlib.sha256(
            _json.dumps(stable_msgs, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        logger.info(
            "prefix_hash sys=%s stable_msgs=%s sys_blocks=%d msgs=%d",
            sys_hash, msg_hash, len(system_blocks), len(other_messages),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"prefix hash compute failed: {exc}")

    try:
        response = await client.messages.create(**create_kwargs)
    except Exception as exc:
        logger.warning("anthropic /v1/messages call failed: %s", exc)
        raise

    data = _native_response_to_openai_shape(response)
    _log_native_usage(data, model)
    return data


__all__ = [
    "post_messages_native",
]
