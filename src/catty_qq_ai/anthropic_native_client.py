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

# 主人 2026-05-28 cache 诊断: module-level 最近一次 system_blocks + stable_msgs
# 快照, 用于 per-block diff log 找出真实对话 prefix 漂移源.
_last_diff_snapshot: dict[str, Any] = {}

# 主人 2026-05-28: beta header list 必须 sort + dedupe 后再 join, 跨调用顺序一致才
# 不破 cache (CC promptCacheBreakDetection.ts:289 同款做法).
_DEFAULT_BETAS_LIST: list[str] = [
    "prompt-caching-2024-07-31",
    "compact-2026-01-12",
    "context-management-2025-06-27",
    # 主人 2026-05-28: 启用 1h cache TTL — Anthropic 已 GA, beta header 在
    # extended-cache-ttl-2025-04-11. cache_control: {ttl: '1h'} 必须搭这个 header.
    "extended-cache-ttl-2025-04-11",
]


def _build_beta_header(extra_betas: list[str] | None = None) -> str:
    """合并默认 betas + extra_betas, sort + dedupe, 用 `,` 连接生成 anthropic-beta header."""
    combined = list(_DEFAULT_BETAS_LIST)
    if extra_betas:
        combined.extend(extra_betas)
    # 去重 + 排序保证字节稳定 (set 内部哈希序 sorted 才确定)
    return ",".join(sorted({b.strip() for b in combined if b and b.strip()}))


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


def _convert_history_for_anthropic(messages: list[dict]) -> list[dict]:
    """把 OpenAI 风格 tool history → Anthropic native messages.

    OpenAI 多轮 tool 格式 (chat_completion_with_tools 写回 history):
        [{role: assistant, content: ..., tool_calls: [{id, type:function, function:{name, arguments_json}}]},
         {role: tool, tool_call_id, name, content},   ← 一个或连续多个
         {role: assistant, content: ...}]

    Anthropic 多轮 tool 格式:
        [{role: assistant, content: [{type:text}, {type:tool_use, id, name, input}]},
         {role: user, content: [{type:tool_result, tool_use_id, content}]},   ← 连续 tool result 合一条
         {role: assistant, content: [{type:text}]}]

    其它 role 原样透传. system message 不在此处理 (_split_system_and_messages 负责).
    """
    import json as _json

    result: list[dict] = []
    pending_tool_results: list[dict] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            result.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            # OpenAI tool result → Anthropic tool_result block
            tool_use_id = str(msg.get("tool_call_id") or "")
            raw_content = msg.get("content")
            if isinstance(raw_content, str):
                tr_content: Any = raw_content
            elif isinstance(raw_content, list):
                tr_content = raw_content
            else:
                try:
                    tr_content = _json.dumps(raw_content or "", ensure_ascii=False)
                except (TypeError, ValueError):
                    tr_content = str(raw_content)
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": tr_content,
            })
        elif role == "assistant" and msg.get("tool_calls"):
            # OpenAI assistant.tool_calls → Anthropic content blocks (text + tool_use)
            flush_tool_results()
            content = msg.get("content")
            blocks: list[dict] = []
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        text_val = blk.get("text") or ""
                        if text_val:
                            blocks.append({"type": "text", "text": text_val})
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                args_str = func.get("arguments")
                if isinstance(args_str, str):
                    try:
                        args_dict = _json.loads(args_str) if args_str.strip() else {}
                    except (ValueError, TypeError):
                        args_dict = {}
                elif isinstance(args_str, dict):
                    args_dict = args_str
                else:
                    args_dict = {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(func.get("name") or ""),
                    "input": args_dict,
                })
            if not blocks:
                # 极端情况: 空 blocks, 给一个占位 text 避免 Anthropic 报 empty content
                blocks.append({"type": "text", "text": ""})
            result.append({"role": "assistant", "content": blocks})
        else:
            flush_tool_results()
            result.append(msg)

    flush_tool_results()
    return result


def convert_openai_tool_to_anthropic(openai_tool: dict) -> dict:
    """OpenAI function-calling 工具定义 → Anthropic tool 定义.

    OpenAI 格式:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}

    Anthropic 格式:
        {"name": ..., "description": ..., "input_schema": {...}}

    若入参已经是 Anthropic 格式 (含 name + input_schema) 则原样返回.
    """
    if not isinstance(openai_tool, dict):
        return openai_tool  # 让 SDK 自己抛错
    # 已经是 Anthropic 格式 (有 input_schema 或者顶层 name 没 type=function 嵌套)
    if "input_schema" in openai_tool and "name" in openai_tool:
        return openai_tool
    # 标准 OpenAI 格式 {"type": "function", "function": {...}}
    if openai_tool.get("type") == "function" and isinstance(openai_tool.get("function"), dict):
        fn = openai_tool["function"]
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", "") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }
    # OpenAI 简化格式 (无 type=function 嵌套但有 name + parameters)
    if "name" in openai_tool and "parameters" in openai_tool:
        return {
            "name": openai_tool["name"],
            "description": openai_tool.get("description", "") or "",
            "input_schema": openai_tool["parameters"],
        }
    return openai_tool


def _extract_tool_uses(raw_blocks: list[dict]) -> list[dict]:
    """从 native response.content 提取 tool_use blocks (Anthropic format).

    Returns list of {"id", "name", "input"} dict (Anthropic native format).
    """
    tool_uses: list[dict] = []
    for blk in raw_blocks or []:
        if isinstance(blk, dict) and blk.get("type") == "tool_use":
            tool_uses.append({
                "id": blk.get("id", ""),
                "name": blk.get("name", ""),
                "input": blk.get("input") or {},
            })
    return tool_uses


def _native_response_to_openai_shape(response: Any) -> dict[str, Any]:
    """anthropic.types.Message → OpenAI chat completion shape.

    让 chat_completion 上层完全无感切换. 额外字段以 `_native_*` 前缀传递.

    Tool use blocks 同时以 Anthropic native (在 _native_raw_content) 和 OpenAI-style
    tool_calls (在 message.tool_calls) 两种形式提供, 兼容两种上层调用方.
    """
    import json as _json
    text, raw_blocks = _extract_text_and_raw(response)
    applied_edits = _extract_applied_edits(response)
    tool_uses = _extract_tool_uses(raw_blocks)

    # 把 Anthropic tool_use blocks 转 OpenAI tool_calls 格式给上层 with_tools loop
    openai_tool_calls: list[dict] = []
    for tu in tool_uses:
        try:
            args_json = _json.dumps(tu.get("input") or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            args_json = "{}"
        openai_tool_calls.append({
            "id": tu.get("id", ""),
            "type": "function",
            "function": {"name": tu.get("name", ""), "arguments": args_json},
        })

    usage_obj = getattr(response, "usage", None)
    if hasattr(usage_obj, "model_dump"):
        usage = usage_obj.model_dump(exclude_none=True)
    elif isinstance(usage_obj, dict):
        usage = dict(usage_obj)
    else:
        usage = {}

    message_dict: dict[str, Any] = {
        "role": "assistant",
        "content": text,
        "_native_raw_content": raw_blocks,
    }
    if openai_tool_calls:
        message_dict["tool_calls"] = openai_tool_calls

    return {
        "choices": [
            {
                "message": message_dict,
                "finish_reason": getattr(response, "stop_reason", None) or "stop",
            }
        ],
        "usage": usage,
        "model": getattr(response, "model", ""),
        "_native_applied_edits": applied_edits,
        "_native_tool_uses": tool_uses,
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
    # 主人 2026-05-28 C6: 推送到 dashboard 让前端实时显示 cache hit ratio
    try:
        from . import dashboard_state as _dash
        _dash.push_cache_stats(model, usage)
    except Exception:  # noqa: BLE001
        pass


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
    metadata_user_id: str | None = None,
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

    headers: dict[str, str] = {"anthropic-beta": _build_beta_header(extra_betas)}
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

    # Pre-step: OpenAI 风格 tool history (role=tool, assistant.tool_calls) → Anthropic
    # native (assistant.content=[tool_use], user.content=[tool_result]).
    messages = _convert_history_for_anthropic(messages)
    # 主人 2026-05-28 cache 修复 — 最后一道兜底 sweep: catch 所有上游漏掉的 floating system
    # (author_note depth-inject / PHI / spark 动态段 等). 通过 idempotent 设计安全地双重调用.
    try:
        from .prompt_cache import sweep_floating_systems_into_user_content
        messages = sweep_floating_systems_into_user_content(messages)
    except Exception as _sweep_exc:  # noqa: BLE001
        logger.debug(f"post_messages_native sweep failed (non-fatal): {_sweep_exc}")
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
        # 自动检测 + 转换 OpenAI tools 格式到 Anthropic 格式
        # convert_openai_tool_to_anthropic 对已经是 Anthropic 格式的 tool 原样返回
        create_kwargs["tools"] = [convert_openai_tool_to_anthropic(t) for t in tools]

    # 主人 2026-05-28: metadata.user_id 注入 (Anthropic cache routing 关键).
    # CC `messages.create(metadata={user_id: ...})` 用 metadata.user_id 路由请求到同
    # backend, 同 scope cache 物理位置稳定. 这是 catty session 管理的本质实现.
    # 私聊: user_id="qq_private_<uid>"; 群聊: user_id="qq_group_<gid>"
    if metadata_user_id:
        create_kwargs["metadata"] = {"user_id": metadata_user_id}

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
    # 时 prefix 是否字节一致. 真实对话 sys/stable_msgs hash 每轮都变 → 加 per-block diff
    # 跟上次比较, 找出具体哪个 block 漂移.
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
        # 看 cache_control 字段在哪 (诊断: 应该挂在 boundary block 上)
        cc_positions = []
        for i, blk in enumerate(system_blocks):
            if isinstance(blk, dict) and "cache_control" in blk:
                cc_positions.append(f"sys[{i}]={blk.get('cache_control')}")
        for i, m in enumerate(other_messages):
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                for bi, blk in enumerate(content):
                    if isinstance(blk, dict) and "cache_control" in blk:
                        cc_positions.append(f"msg[{i}/{m.get('role')}].content[{bi}]={blk.get('cache_control')}")
        logger.info(
            "prefix_hash sys=%s stable_msgs=%s sys_blocks=%d msgs=%d cache_control=%s",
            sys_hash, msg_hash, len(system_blocks), len(other_messages),
            "|".join(cc_positions) if cc_positions else "NONE",
        )
        # per-block diff vs 上次 (module-level, 简单近似 — 不区分 scope, 多 user 并发会混)
        cur_sys_blocks_dump = []
        for i, blk in enumerate(system_blocks):
            text = (blk.get("text") if isinstance(blk, dict) else "") or ""
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
            cur_sys_blocks_dump.append({"i": i, "h": h, "len": len(text), "head": text[:50]})
        cur_stable_msgs_dump = []
        for i, m in enumerate(stable_msgs):
            txt = _json.dumps(m, sort_keys=True, ensure_ascii=False)
            h = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:8]
            # 主人 2026-05-28: dump content head 让 diff 时能看具体内容差异
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, str):
                head = content[:80]
            elif isinstance(content, list) and content:
                first_blk = content[0] if isinstance(content[0], dict) else {}
                head = str(first_blk.get("text", ""))[:80] if first_blk.get("type") == "text" else f"[{first_blk.get('type', '?')}]"
            else:
                head = ""
            cur_stable_msgs_dump.append({
                "i": i, "role": m.get("role", ""), "h": h, "len": len(txt),
                "head": head.replace("\n", "\\n"),
            })
        prev = _last_diff_snapshot.get("snapshot")
        if prev is not None:
            # diff system blocks
            prev_sys = prev.get("sys", [])
            sys_changed = []
            for i in range(max(len(prev_sys), len(cur_sys_blocks_dump))):
                pv = prev_sys[i] if i < len(prev_sys) else None
                cv = cur_sys_blocks_dump[i] if i < len(cur_sys_blocks_dump) else None
                if pv is None or cv is None or pv.get("h") != cv.get("h"):
                    sys_changed.append({
                        "i": i,
                        "prev_h": (pv or {}).get("h"),
                        "cur_h": (cv or {}).get("h"),
                        "prev_len": (pv or {}).get("len"),
                        "cur_len": (cv or {}).get("len"),
                        "cur_head": (cv or {}).get("head", "")[:60].replace("\n", "\\n"),
                    })
            if sys_changed:
                logger.info("cache_diff sys_blocks_changed_count=%d", len(sys_changed))
                for c in sys_changed[:15]:
                    logger.info(
                        "  sys_diff [%d] %s→%s len %s→%s head=%s",
                        c["i"], c["prev_h"], c["cur_h"], c["prev_len"], c["cur_len"], c["cur_head"],
                    )
            # diff stable msgs
            prev_msgs = prev.get("msgs", [])
            msgs_changed = []
            for i in range(max(len(prev_msgs), len(cur_stable_msgs_dump))):
                pv = prev_msgs[i] if i < len(prev_msgs) else None
                cv = cur_stable_msgs_dump[i] if i < len(cur_stable_msgs_dump) else None
                if pv is None or cv is None or pv.get("h") != cv.get("h"):
                    msgs_changed.append({
                        "i": i,
                        "role": (cv or {}).get("role"),
                        "prev_h": (pv or {}).get("h"),
                        "cur_h": (cv or {}).get("h"),
                        "prev_len": (pv or {}).get("len"),
                        "cur_len": (cv or {}).get("len"),
                        "cur_head": (cv or {}).get("head", ""),
                    })
            if msgs_changed:
                logger.info("cache_diff stable_msgs_changed_count=%d", len(msgs_changed))
                for c in msgs_changed[:10]:
                    logger.info(
                        "  msg_diff [%d] role=%s %s→%s len %s→%s cur_head=%s",
                        c["i"], c["role"], c["prev_h"], c["cur_h"], c["prev_len"], c["cur_len"],
                        c.get("cur_head", ""),
                    )
        _last_diff_snapshot["snapshot"] = {"sys": cur_sys_blocks_dump, "msgs": cur_stable_msgs_dump}
        # 主人 2026-05-28: 加完整 sys_blocks dump 一次性看顺序对不对
        if prev is None:  # 仅在第一次 dump (没历史对照时) 输出完整列表
            logger.info("full_sys_blocks_dump count=%d", len(cur_sys_blocks_dump))
            for c in cur_sys_blocks_dump:
                logger.info(
                    "  full [%d] h=%s len=%d head=%s",
                    c["i"], c["h"], c["len"], c["head"].replace("\n", "\\n"),
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"prefix hash compute failed: {exc}")

    # 主人 2026-05-28 调试: dump 实际发出去的 create_kwargs 看 cache_control 是不是真带上
    try:
        import json as _jdbg
        _sys = create_kwargs.get("system") or []
        _msgs = create_kwargs.get("messages") or []
        _sys_cc = sum(
            1 for b in _sys if isinstance(b, dict) and "cache_control" in b
        ) if isinstance(_sys, list) else 0
        _msg_cc = 0
        for m in _msgs:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and "cache_control" in blk:
                        _msg_cc += 1
        logger.info(
            "actual_kwargs: system_blocks=%d (%d with cc), messages=%d (%d cc), metadata=%s, betas_header=%s",
            len(_sys) if isinstance(_sys, list) else -1, _sys_cc,
            len(_msgs), _msg_cc,
            create_kwargs.get("metadata"),
            headers.get("anthropic-beta", "")[:80],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"actual_kwargs dump failed: {exc}")

    # 主人 2026-05-28: cache_read=0 诊断 — 把 system[0..1] prefix 的 sha256 + 前 200 字
    # 内容当场 dump. 同 scope 连发两条对比, 如果 hash 同 + 内容同但 cache_read 仍 0 →
    # 不是 prompt 字节问题, 是 NewAPI relay / Anthropic 后端路由问题 (多账号 round-robin
    # 让缓存写在 acc A 但下次请求路由到 acc B 看不到 A 的 cache).
    try:
        import hashlib as _hl
        import json as _jdbg2
        _sys = create_kwargs.get("system") or []
        # 算 cache prefix (sys[0..cc_idx], 找第一个含 cache_control 的位置)
        _cc_idx = -1
        for _i, _b in enumerate(_sys):
            if isinstance(_b, dict) and "cache_control" in _b:
                _cc_idx = _i
                break
        if _cc_idx >= 0:
            _prefix_blocks = _sys[: _cc_idx + 1]
            _prefix_bytes = _jdbg2.dumps(_prefix_blocks, ensure_ascii=False).encode("utf-8")
            _prefix_sha = _hl.sha256(_prefix_bytes).hexdigest()[:16]
            # 前 200 字让对比时眼看清差异 (主人 logs grep)
            _peek_text = ""
            for _b in _prefix_blocks:
                if isinstance(_b, dict):
                    _peek_text += (_b.get("text") or "")[:100] + "‖"
            _peek_text = _peek_text[:300].replace("\n", "\\n")
            logger.info(
                "cache_prefix_dump: sys[0..%d] sha256=%s bytes=%d metadata=%s peek=%s",
                _cc_idx, _prefix_sha, len(_prefix_bytes),
                create_kwargs.get("metadata"), _peek_text,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"cache_prefix_dump failed: {exc}")

    # 主人 2026-05-28 C5: 改成 streaming + buffer 完整 message 再返回.
    # 外层 chat_completion 接口仍返回 str (兼容), 内部走 SDK stream() async iterator.
    # 流式优势:
    # 1. usage / cache stats 反馈早 (final_message.usage)
    # 2. 中间 chunk 能 push 到 dashboard 实时显示 (C6 用)
    # 3. 不会因为长回复 timeout (HTTP 接收时间均摊到每个 chunk)
    # SDK streaming API: `client.messages.stream(**create_kwargs)` 返回 context manager,
    # `await stream.get_final_message()` 拿完整 Message 对象.
    try:
        async with client.messages.stream(**create_kwargs) as stream:
            try:
                from . import dashboard_state as _dash
                _stream_id = _dash.start_stream(model=model)
            except Exception:  # noqa: BLE001
                _dash = None
                _stream_id = None
            try:
                async for event in stream:
                    # event 类型多样: MessageStart, ContentBlockStart, ContentBlockDelta,
                    # ContentBlockStop, MessageDelta, MessageStop, etc. 不需要逐个处理,
                    # SDK 内部维护完整 message buffer. 但可以把 text delta 推到 dashboard.
                    if _dash is not None and _stream_id is not None:
                        try:
                            _dash.push_event(_stream_id, event)
                        except Exception:  # noqa: BLE001
                            pass
                response = await stream.get_final_message()
            finally:
                if _dash is not None and _stream_id is not None:
                    try:
                        _dash.end_stream(_stream_id)
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as exc:
        logger.warning("anthropic /v1/messages stream call failed: %s", exc)
        raise

    data = _native_response_to_openai_shape(response)
    _log_native_usage(data, model)
    return data


async def post_messages_native_data(
    config: Any,  # Config 类型, 避免循环 import
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    metadata_user_id: str | None = None,
) -> dict[str, Any]:
    """走 native /v1/messages, 返回完整 OpenAI-compat response dict (含 tool_calls).

    给 chat_completion_with_tools 用 — 它需要 dict 形式 response 才能跑 tool calling
    loop (检查 message.tool_calls, 提取 function name/args, 执行, 写 result 回 history).
    跟 _post_anthropic_native_chat 区别: 它返回纯 text (str), 这个返回完整 dict.

    复用 prompt_cache.py 同款 cache_control 注入逻辑 (cachingAtDepthForClaude +
    inject_system_tail_cache) 让 native + with_tools 路径也享受 cache hit.
    """
    _cache_enable = bool(getattr(config, "catty_prompt_cache_enabled", True))
    _non_system_count = sum(
        1 for m in messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    )
    _cache_depth_base = int(getattr(config, "catty_prompt_cache_depth", 2))
    _cache_depth_dynamic = 4 if _non_system_count >= 12 else _cache_depth_base

    # 主人 2026-05-28: native /v1/messages 不接受末尾 assistant prefill
    from .prompt_cache import adapt_assistant_prefill_for_strict_user_end
    messages_for_native = adapt_assistant_prefill_for_strict_user_end(messages)

    prepared_messages = messages_for_native
    if _cache_enable:
        import copy as _copy

        from .prompt_cache import (
            cachingAtDepthForClaude,
            inject_system_tail_cache,
        )
        try:
            prepared_messages = _copy.deepcopy(messages_for_native)
            cachingAtDepthForClaude(prepared_messages, cachingAtDepth=_cache_depth_dynamic)
            inject_system_tail_cache(prepared_messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("native_data path: cache injection failed (%s), 降级到无 cache", exc)
            prepared_messages = messages_for_native

    return await post_messages_native(
        base_url=config.catty_openai_base_url,
        api_key=config.catty_openai_api_key,
        model=config.catty_openai_model,
        messages=prepared_messages,
        max_tokens=config.catty_max_tokens or 4096,
        temperature=config.catty_temperature,
        metadata_user_id=metadata_user_id,
        timeout=float(config.catty_request_timeout),
        enable_compaction=bool(getattr(config, "catty_compaction_enabled", False)),
        compaction_trigger_tokens=int(getattr(config, "catty_compaction_trigger_tokens", 150_000)),
        tools=tools,
    )


__all__ = [
    "post_messages_native",
    "post_messages_native_data",
    "convert_openai_tool_to_anthropic",
]
