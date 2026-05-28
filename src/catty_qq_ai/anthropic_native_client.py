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

import hashlib
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger("catty_qq_ai.anthropic_native")

# 主人 2026-05-28 cache 诊断: module-level 最近一次 system_blocks + stable_msgs
# 快照, 用于 per-block diff log 找出真实对话 prefix 漂移源.
_last_diff_snapshot: dict[str, Any] = {}

# 主人 2026-05-28 C8: 默认 betas 列表对齐 CC 角色扮演场景 (claude-code-best/src/services/api/claude.ts).
# 删: prompt-caching-2024-07-31 + extended-cache-ttl-2025-04-11 (已 GA, MD section 1.6 + 6 推荐删).
# 加: interleaved-thinking-2025-05-14 + prompt-caching-scope-2026-01-05 (CC 角色扮演标配).
# 保留: context-management-2025-06-27, compact-2026-01-12, cache-diagnosis-2026-04-07.
_DEFAULT_BETAS_LIST: list[str] = [
    # 主人 2026-05-28 C18 → C18-fix: 17:26 实测删 prompt-caching 后 NewAPI relay 返
    # 500 InternalServerError. 主人 C13 commit 早就贴过 "NewAPI relay 不识别 cache 没这个
    # beta" — vscode 直连 anthropic API 已 GA, 但 catty 走 NewAPI relay 仍需 beta header
    # 触发 cache 转发. 加回 prompt-caching-2024-07-31.
    "prompt-caching-2024-07-31",
    # 保留 (vscode chatEndpoint.ts:228-249 同款):
    "interleaved-thinking-2025-05-14",
    "context-management-2025-06-27",
]


# 主人 2026-05-28 C2: 记录每个 scope 上一次 chat completion 的 message_id, 用来给
# 下一次请求传 diagnostics.previous_message_id, 让 Anthropic 告诉我们 cache miss 原因.
_LAST_MESSAGE_ID_BY_SCOPE: dict[str, str] = {}

# 主人 2026-05-28 C8: catty 启动时生成的 session_id, 跟 CC getAPIMetadata 同款做 metadata.user_id
# JSON 字段. NewAPI relay 如果按 user_id hash 选账号, 同一 session 稳定路由同一账号 → cache 命中.
_BOT_SESSION_ID: str = uuid.uuid4().hex[:16]


def _build_beta_header(extra_betas: list[str] | None = None) -> str:
    """合并默认 betas + extra_betas, sort + dedupe, 用 `,` 连接生成 anthropic-beta header."""
    combined = list(_DEFAULT_BETAS_LIST)
    if extra_betas:
        combined.extend(extra_betas)
    # 去重 + 排序保证字节稳定 (set 内部哈希序 sorted 才确定)
    return ",".join(sorted({b.strip() for b in combined if b and b.strip()}))


def _cache_diag_log(msg: str, *args: Any) -> None:
    """诊断日志路由: catty_cache_diag_enabled=True 时 INFO 留生产, False 降 DEBUG."""
    try:
        from . import config as _mc
        if bool(getattr(_mc.config, "catty_cache_diag_enabled", True)):
            logger.info(msg, *args)
            return
    except Exception:  # noqa: BLE001
        pass
    logger.debug(msg, *args)


def _warn_inline_system_messages(messages: list[dict]) -> int:
    """Phase 1.3 守卫: 扫 messages 数组找 role=system 漏出条数. 返回数量.
    会被 _split_system_and_messages 自动 sweep 到 system_blocks, 这里只打日志辅助定位元凶."""
    if not messages:
        return 0
    leak = 0
    sample = []
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "system":
            leak += 1
            if len(sample) < 3:
                c = m.get("content")
                head = (c[:80] if isinstance(c, str) else (str(c)[:80] if c else ""))
                sample.append(f"[{i}]={head.replace(chr(10), ' ')}")
    if leak > 0:
        try:
            from . import config as _mc
            if bool(getattr(_mc.config, "catty_cache_warn_on_inline_system", True)):
                logger.warning(
                    "inline_system_leak: %d role=system in messages (会被 sweep 但破 cache 风险): %s",
                    leak, " | ".join(sample),
                )
        except Exception:  # noqa: BLE001
            pass
    return leak


def _strip_existing_cache_control(
    system_blocks: list[dict] | None,
    messages: list[dict],
    tools: list[dict] | None,
) -> int:
    """剥掉 system_blocks / messages / tools 上所有现存 cache_control 字段, 返回剥掉的总数.

    保证 _apply_anthropic_cache_breakpoints 下手时是干净 prefix, 避免 multiple owners 残留:
    - history messages 里的 assistant turn 可能含上一轮 cache_control (走 session_cache 持久化回来)
    - 上游 deepcopy 漏改 / 路径切换时 marker 没清干净
    - relay 字节级透传, 同一 block 上叠 2 个 cache_control 会让第二轮 cache lookup 500
    """
    stripped = 0
    if system_blocks:
        for blk in system_blocks:
            if isinstance(blk, dict) and blk.pop("cache_control", None) is not None:
                stripped += 1
    if messages:
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


def _apply_anthropic_cache_breakpoints(
    system_blocks: list[dict] | None,
    messages: list[dict],
    tools: list[dict] | None,
) -> None:
    """单一 owner 标 Anthropic cache_control 4 breakpoint (vscode messagesApi.ts 公式).

    主人 2026-05-28 C18: 移植 microsoft/vscode addToolsAndSystemCacheControl +
    addMessagesApiCacheControl 公式. 4 breakpoint 满配:
      1. tools[-1] 顶层 cache_control (Anthropic native tools 字段格式)
      2. system_blocks[-1] 末 block cache_control (跳 thinking)
      3. messages 倒数第 1 条「可缓存」block cache_control
      4. messages 倒数第 2 条「可缓存」block cache_control (TTL fallback)

    参考: extensions/copilot/src/platform/endpoint/node/messagesApi.ts:558-604
    设计要点: 跳过 thinking_block / redacted_thinking_block (Anthropic 限制),
    最多 2 条 messages 给 fallback ‒ 一条 TTL 过期还有另一条 anchor 防止整段 cache reset.

    主人 2026-05-28 Phase 1.2: 标位前先剥所有现存 cache_control (defensive single-owner),
    防止 history / 上游路径残留多份 cache_control 导致 relay 500.

    in-place 修改, 调用前请确保 system_blocks / messages / tools 是最终请求数据.
    """
    _stripped = _strip_existing_cache_control(system_blocks, messages, tools)
    if _stripped > 0:
        logger.debug("cache_control single-owner: stripped %d residual marker(s)", _stripped)

    from .prompt_cache import _build_cache_control_dict
    cc = _build_cache_control_dict()

    # 1. tools[-1] (Anthropic native: 顶层 cache_control 字段)
    if tools:
        for i in range(len(tools) - 1, -1, -1):
            if isinstance(tools[i], dict):
                tools[i]["cache_control"] = cc
                break

    # 2. system_blocks[-1] 末 block (跳 thinking) — vscode 公式
    # 主人 2026-05-28 撤回 P5.7 sys[0] 改动: 严格走 vscode messagesApi.ts 公式.
    # 真正 fix 在 caller: NSFW spark 不再 inject 动态段到 sys 末尾, 改 user content.
    if system_blocks:
        for i in range(len(system_blocks) - 1, -1, -1):
            blk = system_blocks[i]
            if (
                isinstance(blk, dict)
                and blk.get("type") not in ("thinking", "redacted_thinking")
            ):
                blk["cache_control"] = cc
                break

    # 3 + 4. messages 倒数 2 条可缓存 message 末 block — vscode 公式 (撤回 P5 砍位)
    marked = 0
    for i in range(len(messages) - 1, -1, -1):
        if marked >= 2:
            break
        m = messages[i]
        if not isinstance(m, dict):
            continue
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            m["content"] = [{"type": "text", "text": content, "cache_control": cc}]
            marked += 1
        elif isinstance(content, list) and content:
            for j in range(len(content) - 1, -1, -1):
                blk = content[j]
                if isinstance(blk, dict) and blk.get("type") not in ("thinking", "redacted_thinking"):
                    blk["cache_control"] = cc
                    marked += 1
                    break


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


def _sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
    """Anthropic 硬约束: assistant.tool_use[id] 必须紧跟一条 user 含对应 tool_result.
    catty history rotation / batch trim / 中间插 system 会切断配对 → API 返回
    `status_code=500, messages.N: tool_use ids were found without tool_result blocks`.

    兜底: 扫描配对, 删除孤立 tool_use 和 tool_result blocks, 保证发请求时全部配对.
    主人 2026-05-28 C19: 500 根因 fix, 在 _convert_history_for_anthropic 后立刻调用.
    """
    if not messages:
        return messages

    sanitized: list[dict] = []
    dropped_uses = 0
    dropped_results = 0

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            sanitized.append(msg)
            continue

        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant" and isinstance(content, list):
            tool_use_ids = [
                b.get("id") for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
            ]
            if tool_use_ids:
                next_msg = messages[idx + 1] if idx + 1 < len(messages) else None
                next_results: set[str] = set()
                if isinstance(next_msg, dict) and next_msg.get("role") == "user":
                    next_content = next_msg.get("content")
                    if isinstance(next_content, list):
                        for b in next_content:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                tr_id = b.get("tool_use_id")
                                if tr_id:
                                    next_results.add(tr_id)
                orphans = {tid for tid in tool_use_ids if tid not in next_results}
                if orphans:
                    new_content: list[dict] = []
                    for b in content:
                        if (
                            isinstance(b, dict)
                            and b.get("type") == "tool_use"
                            and b.get("id") in orphans
                        ):
                            dropped_uses += 1
                            continue
                        new_content.append(b)
                    if not new_content:
                        new_content.append({"type": "text", "text": "[tool call dropped]"})
                    msg = {**msg, "content": new_content}

        elif role == "user" and isinstance(content, list):
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if has_tool_result:
                prev_msg = sanitized[-1] if sanitized else None
                prev_uses: set[str] = set()
                if isinstance(prev_msg, dict) and prev_msg.get("role") == "assistant":
                    prev_content = prev_msg.get("content")
                    if isinstance(prev_content, list):
                        for b in prev_content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                uid = b.get("id")
                                if uid:
                                    prev_uses.add(uid)
                new_content_u: list[dict] = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        if b.get("tool_use_id") in prev_uses:
                            new_content_u.append(b)
                        else:
                            dropped_results += 1
                    else:
                        new_content_u.append(b)
                if not new_content_u:
                    continue
                msg = {**msg, "content": new_content_u}

        sanitized.append(msg)

    if dropped_uses or dropped_results:
        logger.warning(
            "tool pair sanitize: dropped %d orphan tool_use + %d orphan tool_result",
            dropped_uses, dropped_results,
        )

    return sanitized


def _normalize_anthropic_input_schema(schema: Any) -> dict:
    """规范化 Anthropic input_schema — 对齐 VSCode tool 序列化:
    - 必须是 dict, 否则替换成空 object schema
    - type 强制 = "object"
    - 必须含 properties dict (Anthropic 要求, 空 properties 用 {})
    - 剥离 $schema 字段 (Anthropic 不接受 JSON Schema meta 字段)
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    normalized = {k: v for k, v in schema.items() if k != "$schema"}
    normalized["type"] = "object"
    if not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def convert_openai_tool_to_anthropic(openai_tool: dict) -> dict:
    """OpenAI function-calling 工具定义 → Anthropic tool 定义 (对齐 VSCode 序列化).

    OpenAI 格式:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}

    Anthropic 格式:
        {"name": ..., "description": ..., "input_schema": {"type": "object", "properties": {...}}}

    规范化点:
    - input_schema 强制 {type: object, properties: dict}
    - 剥离 $schema 字段 (JSON Schema meta, Anthropic 拒收)
    - 已是 Anthropic 格式时仍规范化 input_schema (防御外部传入半成品)
    """
    if not isinstance(openai_tool, dict):
        return openai_tool  # 让 SDK 自己抛错
    # 已经是 Anthropic 格式 (有 input_schema 或者顶层 name 没 type=function 嵌套)
    if "input_schema" in openai_tool and "name" in openai_tool:
        return {
            "name": openai_tool["name"],
            "description": openai_tool.get("description", "") or "",
            "input_schema": _normalize_anthropic_input_schema(openai_tool.get("input_schema")),
        }
    # 标准 OpenAI 格式 {"type": "function", "function": {...}}
    if openai_tool.get("type") == "function" and isinstance(openai_tool.get("function"), dict):
        fn = openai_tool["function"]
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", "") or "",
            "input_schema": _normalize_anthropic_input_schema(fn.get("parameters")),
        }
    # OpenAI 简化格式 (无 type=function 嵌套但有 name + parameters)
    if "name" in openai_tool and "parameters" in openai_tool:
        return {
            "name": openai_tool["name"],
            "description": openai_tool.get("description", "") or "",
            "input_schema": _normalize_anthropic_input_schema(openai_tool.get("parameters")),
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
        # 主人 2026-05-28: 把 scope_key (qq_private_xxx / qq_group_xxx) 也带上, 让 dashboard 按 scope 显示
        _scope_for_dash = ""
        try:
            from .openai_client import get_current_scope_key
            _scope_for_dash = get_current_scope_key() or ""
        except Exception:  # noqa: BLE001
            pass
        _dash.push_cache_stats(
            _scope_for_dash or model,  # fallback 到 model 兼容老 dashboard
            usage,
            model=model,
        )
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

    # 主人 2026-05-28 C8: 显式设 anthropic-version + anthropic-beta, 对齐 MD section 9.1 模板.
    # anthropic-version 在 anthropic-beta 前面 (key 顺序), 确保 NewAPI relay 看到的 header 一致.
    headers: dict[str, str] = {
        "anthropic-version": "2023-06-01",
        "anthropic-beta": _build_beta_header(extra_betas),
    }
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
    # 主人 2026-05-28 C19: tool_use/tool_result 配对兜底 — history rotation / batch
    # trim 切断配对 → Anthropic 500. 在转换格式之后立刻 sanitize, 删孤立 block.
    messages = _sanitize_tool_pairs(messages)
    # 主人 2026-05-28 C11 重启 sweep: 主人贴 10:34-10:36 sweep 时代 cache hit 85%+ 实证.
    # 回退 sweep 后 sys_blocks 含动态段, NewAPI relay 看 sys 数组不稳定 → cache miss.
    # 重启让 sys 段只剩静态 (persona+override+supplement), 动态段 inline 到 user [DYNAMIC_CONTEXT].
    # history 不进 cache (messages marker C10 已删), 只 sys static prefix 进 cache. 拒绝问题
    # 另外修 (扩 soft-refusal 关键词等).
    try:
        from .prompt_cache import sweep_floating_systems_into_user_content
        messages = sweep_floating_systems_into_user_content(messages)
    except Exception as _sweep_exc:  # noqa: BLE001
        logger.debug(f"post_messages_native sweep failed (non-fatal): {_sweep_exc}")
    # Phase 1.3 守卫: sweep 后再扫一遍, 看动态段是否仍以 system role 漏出. 不 raise (主人选 A
    # 留生产但 DEBUG/WARNING 提示), 漏出会被 _split_system_and_messages 自动归并到 system_blocks.
    _warn_inline_system_messages(messages)
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

    # 主人 2026-05-28 C18 (vscode messagesApi.ts 公式): 4 breakpoint 满配 + 单一 owner.
    # 参考: extensions/copilot/src/platform/endpoint/node/messagesApi.ts:558-604
    # 设计原则: cache_control placement 必须由请求层最后一刻单一函数收口,
    # prompt 渲染层 / caller 层不要瞎插 marker (multiple owners 会冲突 → cache miss).
    # 4 breakpoint 分配:
    #   1. tools[-1] 顶层 cache_control (vscode addToolsAndSystemCacheControl)
    #   2. system_blocks[-1] 末 block cache_control
    #   3. messages 倒数第 1 条「可缓存」block cache_control
    #   4. messages 倒数第 2 条「可缓存」block cache_control (TTL 漂移 fallback)
    try:
        from .prompt_cache import is_claude_endpoint
        if is_claude_endpoint(base_url, model):
            _apply_anthropic_cache_breakpoints(
                create_kwargs.get("system"),
                create_kwargs["messages"],
                create_kwargs.get("tools"),
            )
    except Exception as _cc_exc:  # noqa: BLE001
        logger.debug(f"cache_control 标位失败 (non-fatal): {_cc_exc}")

    # 主人 2026-05-28 C13: metadata.user_id 回短字符串 — 10:08 黄金状态实证短字符串 cache hit.
    # C8 改 JSON 是错的诊断, NewAPI 不按 metadata hash 路由账号. 短字符串 'qq_private_xxx' 工作.
    if metadata_user_id:
        create_kwargs["metadata"] = {"user_id": metadata_user_id}

    # 主人 2026-05-28 C2 → Phase 1.2: cache_diagnostics 通过 extra_body 传 (SDK 不支持顶层 diagnostics).
    # NewAPI/中转 relay 不识别 diagnostics 字段时第二轮会 500, 改成 config 开关默认 OFF.
    # 直连 anthropic.com 才打开 (catty_cache_diag_previous_message_id_enabled=true).
    _prev_msg_id = ""
    try:
        from . import config as _module_config
        _diag_enabled = bool(getattr(
            _module_config.config, "catty_cache_diag_previous_message_id_enabled", False,
        ))
    except Exception:  # noqa: BLE001
        _diag_enabled = False
    if _diag_enabled:
        try:
            if metadata_user_id:
                _prev_msg_id = _LAST_MESSAGE_ID_BY_SCOPE.get(metadata_user_id, "")
            if _prev_msg_id:
                extra_body = create_kwargs.get("extra_body") or {}
                extra_body = dict(extra_body)
                extra_body["diagnostics"] = {"previous_message_id": _prev_msg_id}
                create_kwargs["extra_body"] = extra_body
        except Exception:  # noqa: BLE001
            pass

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
    # C8 fix: 删 `import hashlib` (顶部已 import). 函数内有 import 会让整个函数 hashlib
    # 变 local variable, 但改动 4 的 metadata.user_id JSON 在它之前用 hashlib.sha256,
    # 导致 UnboundLocalError (11:57 log 实证) → opus 调用失败 fallback 到 deepseek.
    try:
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
        _cache_diag_log(
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
                _cache_diag_log("cache_diff sys_blocks_changed_count=%d", len(sys_changed))
                for c in sys_changed[:15]:
                    _cache_diag_log(
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
                _cache_diag_log("cache_diff stable_msgs_changed_count=%d", len(msgs_changed))
                for c in msgs_changed[:10]:
                    _cache_diag_log(
                        "  msg_diff [%d] role=%s %s→%s len %s→%s cur_head=%s",
                        c["i"], c["role"], c["prev_h"], c["cur_h"], c["prev_len"], c["cur_len"],
                        c.get("cur_head", ""),
                    )
        _last_diff_snapshot["snapshot"] = {"sys": cur_sys_blocks_dump, "msgs": cur_stable_msgs_dump}
        # 主人 2026-05-28: 加完整 sys_blocks dump 一次性看顺序对不对
        if prev is None:  # 仅在第一次 dump (没历史对照时) 输出完整列表
            _cache_diag_log("full_sys_blocks_dump count=%d", len(cur_sys_blocks_dump))
            for c in cur_sys_blocks_dump:
                _cache_diag_log(
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
        _cache_diag_log(
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
            _cache_diag_log(
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
    # SDK streaming API: `client.beta.messages.stream(**create_kwargs)` 返回 context manager,
    # `await stream.get_final_message()` 拿完整 Message 对象.
    # 主人 2026-05-28 C9 关键修复: 用 client.beta.messages.stream 走 **beta endpoint**, 跟 CC
    # (claude.ts:549 anthropic.beta.messages.create) 完全一致. Anthropic 服务端通过 beta endpoint
    # 识别请求支持 prompt-caching / cache-diagnosis 等 beta 特性. 之前用 client.messages.stream
    # 走 stable endpoint, Anthropic 可能静默忽略 cache_control 字段 → cache 永久 0 hit.

    # 主人 2026-05-28 C15: raw body dump — 看 sys_blocks / messages / tools 真实 size, 找砍点大头.
    # 默认开启 (dump 到 D:\CattyQQAI\diagnostics\catty_body_*.json), 主人测一条消息就能看大头.
    # 不影响 cache (只是写盘, 不动 payload).
    try:
        from pathlib import Path as _RawDumpPath
        import time as _rawdump_time
        _dump_dir = _RawDumpPath("diagnostics")
        _dump_dir.mkdir(exist_ok=True, parents=True)
        _scope_tag = (metadata_user_id or "noscope").replace("/", "_").replace(":", "_")[:32]
        _dump_path = _dump_dir / f"catty_body_{_scope_tag}_{int(_rawdump_time.time())}.json"
        _sys_arr = create_kwargs.get("system") or []
        _msg_arr = create_kwargs.get("messages") or []
        _tools_arr = create_kwargs.get("tools") or []
        # 算 size breakdown
        _breakdown = {
            "model": create_kwargs.get("model"),
            "metadata": create_kwargs.get("metadata"),
            "headers_bytes": sum(len(k) + len(v) for k, v in headers.items()),
            "headers": headers,
            "sys_block_count": len(_sys_arr),
            "sys_blocks_bytes": [
                {"i": i, "len": len(json.dumps(b, ensure_ascii=False)), "text_len": len((b.get("text") or "") if isinstance(b, dict) else ""), "head": ((b.get("text") or "")[:80] if isinstance(b, dict) else "")[:80]}
                for i, b in enumerate(_sys_arr)
            ],
            "sys_total_text_bytes": sum(len((b.get("text") or "") if isinstance(b, dict) else "") for b in _sys_arr),
            "msg_count": len(_msg_arr),
            "msg_bytes": [
                {"i": i, "role": m.get("role"), "len": len(json.dumps(m, ensure_ascii=False)) if isinstance(m, dict) else 0}
                for i, m in enumerate(_msg_arr)
            ],
            "msg_total_bytes": sum(len(json.dumps(m, ensure_ascii=False)) for m in _msg_arr if isinstance(m, dict)),
            "tools_count": len(_tools_arr),
            "tools_total_bytes": sum(len(json.dumps(t, ensure_ascii=False)) for t in _tools_arr if isinstance(t, dict)),
            "tools_breakdown": [
                {"name": t.get("name") if isinstance(t, dict) else "?", "desc_len": len((t.get("description") or "") if isinstance(t, dict) else ""), "schema_len": len(json.dumps(t.get("input_schema") or {}, ensure_ascii=False)) if isinstance(t, dict) else 0}
                for t in _tools_arr
            ],
        }
        _dump_path.write_text(json.dumps(_breakdown, ensure_ascii=False, indent=2), encoding="utf-8")
        _cache_diag_log(
            "raw_body_dump: %s sys=%d/%dB msgs=%d/%dB tools=%d/%dB",
            _dump_path.name, _breakdown["sys_block_count"], _breakdown["sys_total_text_bytes"],
            _breakdown["msg_count"], _breakdown["msg_total_bytes"],
            _breakdown["tools_count"], _breakdown["tools_total_bytes"],
        )
    except Exception as _dump_exc:  # noqa: BLE001
        logger.debug(f"raw_body_dump failed: {_dump_exc}")

    try:
        async with client.beta.messages.stream(**create_kwargs) as stream:
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

    # 主人 2026-05-28 C2: 保存 message_id 供下次 diagnostics 用 + 解析 cache_miss_reason
    try:
        msg_id = getattr(response, "id", "") or ""
        if metadata_user_id and msg_id:
            _LAST_MESSAGE_ID_BY_SCOPE[metadata_user_id] = msg_id
        # 解析 diagnostics.cache_miss_reason (Anthropic 自己告诉我们为啥 miss)
        diag = getattr(response, "diagnostics", None)
        if diag is not None:
            miss_reason = getattr(diag, "cache_miss_reason", None)
            if miss_reason is not None:
                # miss_reason 可能是 dict / Pydantic model, 都安全 stringify
                miss_type = getattr(miss_reason, "type", None) or (
                    miss_reason.get("type") if isinstance(miss_reason, dict) else ""
                )
                miss_details = ""
                try:
                    if hasattr(miss_reason, "model_dump"):
                        miss_details = str(miss_reason.model_dump())[:300]
                    elif isinstance(miss_reason, dict):
                        miss_details = str(miss_reason)[:300]
                    else:
                        miss_details = str(miss_reason)[:300]
                except Exception:  # noqa: BLE001
                    miss_details = "<unparseable>"
                logger.info(
                    "cache_miss_reason scope=%s type=%s prev_msg=%s details=%s",
                    metadata_user_id or "?", miss_type, _prev_msg_id[:24] or "(none)", miss_details,
                )
            else:
                # 没 miss_reason 字段 — 说明上一次 prev_msg 找到匹配 (cache hit)
                logger.info(
                    "cache hit scope=%s prev_msg=%s msg_id=%s",
                    metadata_user_id or "?", _prev_msg_id[:24] or "(none)", msg_id[:24],
                )
    except Exception as _diag_exc:  # noqa: BLE001
        logger.debug(f"diagnostics parse failed (non-fatal): {_diag_exc}")

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

    主人 2026-05-28 C18 (vscode 公式): cache_control 单一 owner — 全部在
    post_messages_native 内部 sweep+split_system 之后由 _apply_anthropic_cache_breakpoints
    统一标. caller 层 (这里) 不再插 cache_control marker, 避免 multiple owners 冲突.
    """
    # 主人 2026-05-28: native /v1/messages 不接受末尾 assistant prefill
    from .prompt_cache import adapt_assistant_prefill_for_strict_user_end
    messages_for_native = adapt_assistant_prefill_for_strict_user_end(messages)

    return await post_messages_native(
        base_url=config.catty_openai_base_url,
        api_key=config.catty_openai_api_key,
        model=config.catty_openai_model,
        messages=messages_for_native,
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
