from __future__ import annotations

import asyncio
import base64
import contextvars
from collections import deque
from io import BytesIO
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageSequence

from .config import Config
from .mc_status import mc_has_players
from .parsers import lenient_json_object
from .reply_markers import INLINE_IMAGE_PREFIX, INLINE_IMAGE_SUFFIX


_logger = logging.getLogger("catty_qq_ai")

# 全局云端健康状态：> time.monotonic() 时主回复直接走 fallback 不再尝试云。
_cloud_fail_until: float = 0.0


def _cloud_is_unhealthy() -> bool:
    return time.monotonic() < _cloud_fail_until


def _cloud_health_remaining_seconds() -> float:
    return max(_cloud_fail_until - time.monotonic(), 0.0)


def _mark_cloud_unhealthy(cooldown_seconds: float) -> None:
    global _cloud_fail_until
    _cloud_fail_until = time.monotonic() + max(cooldown_seconds, 0.0)


def _mark_cloud_healthy() -> None:
    global _cloud_fail_until
    _cloud_fail_until = 0.0


def _reset_cloud_health_for_tests() -> None:
    """Test-only helper: reset the global health timer."""
    _mark_cloud_healthy()


# Fallback warmup: 第一次 fallback 调用走冷启动会同时付出"模型 load 到显存"+"大 prompt 处理"
# 两份成本(~60-120s)。先发一个 1-token "hi" 把模型 load 上来,再发真正的请求,
# 让真正回复只付"prompt 处理"那一份(KV cache 已经热了)。
_fallback_warmed_at: float = 0.0
_FALLBACK_WARMUP_WINDOW_SECONDS = 120.0
_FALLBACK_WARMUP_TIMEOUT_SECONDS = 180.0


def _mark_fallback_warmed() -> None:
    global _fallback_warmed_at
    _fallback_warmed_at = time.monotonic()


def _fallback_is_warm() -> bool:
    return time.monotonic() - _fallback_warmed_at < _FALLBACK_WARMUP_WINDOW_SECONDS


ChatMessage = dict[str, Any]


class OpenAICompatibleError(Exception):
    def __init__(self, public_message: str, detail: str | None = None) -> None:
        super().__init__(detail or public_message)
        self.public_message = public_message


class MCBusyError(OpenAICompatibleError):
    """MC server has players online and local fallback is gated off."""


# 同一错误码多个 catty 变体——避免连续 503 时用户看到同一句死板提示;
# random.choice 让群友觉得猫猫真的"每次都在挣扎",而不是机器复读。
_HTTP_503_VARIANTS = (
    "喵呜～{svc}那边人太多挤爆了（503），主人 30 秒后再戳人家一下嘛 ฅฅ",
    "嗷呜～{svc}炸毛了（503），猫猫帮你按 F5 几次都没用，等半分钟再来叭(尾巴垂垂)",
    "哼…{svc}今天闹脾气（503），人家也没办法喵，30s 后重戳大概率好啦~",
    "{svc}排队人太多了喵（503）！主人稍等半分钟，猫猫会接着回的~",
)
_HTTP_429_VARIANTS = (
    "喵呜~{svc}嫌猫猫戳太快啦（429），让人家缓 1 分钟再试嘛(爪爪)",
    "哼…{svc}限流了（429），主人冷静一下喵，等 60 秒猫猫就能接着回~",
    "嗷呜~{svc}说人家戳得太频繁（429），等会儿就好啦 ฅฅ",
)
_HTTP_5XX_VARIANTS = (
    "喵呜～{svc}临时炸毛了（{code}），主人稍后再戳一下喵 ฅฅ",
    "嗷呜～{svc}那边好像在抢修（{code}），等会儿再来叭(尾巴轻晃)",
    "{svc}打了个喷嚏（{code}），猫猫也没辙喵，主人等 1-2 分钟再戳~",
)


def _catty_http_status_message(service_name: str, status_code: int) -> str:
    import random as _random
    if status_code == 503:
        return _random.choice(_HTTP_503_VARIANTS).format(svc=service_name)
    if status_code == 429:
        return _random.choice(_HTTP_429_VARIANTS).format(svc=service_name)
    if status_code in {401, 403}:
        return f"喵呜，{service_name}不让猫猫进去（{status_code}），主人检查一下 API Key 或权限。"
    if 500 <= status_code < 600:
        return _random.choice(_HTTP_5XX_VARIANTS).format(svc=service_name, code=status_code)
    return f"喵呜，{service_name}请求没过（{status_code}），主人检查一下配置或稍后再试。"


def _chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _normalize_openai_tool_parameters(params: Any) -> dict[str, Any]:
    """规范化 OpenAI tool parameters — 对齐 VSCode tool 序列化:
    - 必须是 dict
    - type 强制 "object"
    - 必须含 properties dict
    - 剥离 $schema 字段 (供应商对 JSON Schema meta 字段处理不一致, 部分中转直接拒)
    """
    if not isinstance(params, dict):
        return {"type": "object", "properties": {}}
    normalized = {k: v for k, v in params.items() if k != "$schema"}
    normalized["type"] = "object"
    if not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def normalize_openai_tool_schemas(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把任意形态的 tool 定义统一规整成标准 OpenAI function-calling 形态:
        {"type": "function", "function": {"name", "description", "parameters": {...}}}
    """
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # 已是标准 OpenAI 形态
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            out.append({
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", "") or "",
                    "parameters": _normalize_openai_tool_parameters(fn.get("parameters")),
                },
            })
            continue
        # Anthropic 形态 (name + input_schema) → 转 OpenAI
        if "input_schema" in t and "name" in t:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", "") or "",
                    "parameters": _normalize_openai_tool_parameters(t.get("input_schema")),
                },
            })
            continue
        # 简化形态 (name + parameters)
        if "name" in t and "parameters" in t:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", "") or "",
                    "parameters": _normalize_openai_tool_parameters(t.get("parameters")),
                },
            })
    return out


def _ollama_chat_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1", "/api/chat", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/api/chat"


def _looks_like_ollama_route(base_url: str, api_key: str, extra_body: dict[str, Any]) -> bool:
    native_flag = extra_body.get("native_ollama")
    if isinstance(native_flag, bool):
        return native_flag
    parsed = urlparse(base_url)
    return parsed.port == 11434 or api_key.strip().lower() == "ollama"


# 主 AI 多模态输出里的图片(image_url / base64)在文本里用 INLINE_IMAGE 占位符表达,
# 让发送链路看到后转成 MessageSegment.image。占位符常量在 reply_markers 里统一定义;
# history 写入前要 strip 掉(否则 base64 会污染 prompt token)。


def _coerce_multimodal_part(item: dict[str, Any]) -> str:
    """把多模态 content list 里的一项渲染成纯文本片段(含 INLINE_IMAGE 占位符)。"""
    text = item.get("text") or item.get("content")
    if isinstance(text, str) and text:
        return text

    item_type = str(item.get("type") or "").lower()
    if item_type in {"image_url", "image", "input_image", "output_image"}:
        url = ""
        image_url = item.get("image_url")
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "")
        elif isinstance(image_url, str):
            url = image_url
        if not url:
            url = str(item.get("url") or "")
        # Gemini 风格: {"type": "image", "data": "<base64>", "mime_type": "image/png"}
        if not url and isinstance(item.get("data"), str):
            mime = str(item.get("mime_type") or "image/png")
            url = f"data:{mime};base64,{item['data']}"
        if url:
            return f"{INLINE_IMAGE_PREFIX}{url}{INLINE_IMAGE_SUFFIX}"
    return ""


def _render_message_images(message: dict[str, Any]) -> str:
    """gpt-5.5 / codex 网关把原生生成的图放在 message.images 字段, 不走 content 也不走 tool_calls。

    每项格式 {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    或 {"type": "image", "image_url": "..."} 等变体。
    拼成 INLINE_IMAGE marker 序列让发送链路自动渲染成 MessageSegment.image。
    """
    if not isinstance(message, dict):
        return ""
    images = message.get("images")
    if not isinstance(images, list) or not images:
        return ""
    parts: list[str] = []
    for item in images:
        if isinstance(item, str):
            if item:
                parts.append(f"{INLINE_IMAGE_PREFIX}{item}{INLINE_IMAGE_SUFFIX}")
            continue
        if not isinstance(item, dict):
            continue
        rendered = _coerce_multimodal_part(item)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def _extract_content(data: dict[str, Any]) -> str:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenAICompatibleError("AI 返回格式不符合 Chat Completions。", repr(data)[:500]) from exc

    message = choice.get("message") or {}
    content = message.get("content")
    finish_reason = choice.get("finish_reason")

    if isinstance(content, str):
        # content 有文本,但有些模型(gpt-5.5)同时把图放在 message.images 字段——
        # 文本+图都拼上,让发送链路一起处理。
        rendered = (content.strip() + "\n" + _render_message_images(message)).strip()
        return rendered
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
            elif isinstance(item, dict):
                rendered = _coerce_multimodal_part(item)
                if rendered:
                    parts.append(rendered)
        merged = "\n".join(part for part in parts if part).strip()
        images_inline = _render_message_images(message)
        if images_inline:
            merged = (merged + "\n" + images_inline).strip() if merged else images_inline
        if merged:
            return merged
        # content 是 list 但里面没有任何可用的文本或图片,继续往下走兜底

    # content 为空/None:gpt-5.5 / codex 网关常把原生生成的图直接放 message.images 字段
    # (不走 tool_calls 也不走 content),老逻辑直接当 error 处理,导致用户看到"AI 没有返回可读文本"
    # 实际上模型已经把图生好了。这里识别这种格式,把图转成 INLINE_IMAGE marker 让发送链路渲染。
    images_only = _render_message_images(message)
    if images_only:
        _logger.info(
            "AI returned native images-only response (no text), rendered as INLINE_IMAGE. "
            "finish_reason=%s image_count=%d",
            finish_reason,
            len(message.get("images") or []) if isinstance(message, dict) else 0,
        )
        return images_only

    # content 为空/None：先看 reasoning_content / reasoning(R1 / QwQ / DeepSeek-Reasoner 风格)
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        _logger.warning(
            "AI message.content empty but reasoning present; using reasoning as reply. "
            "finish_reason=%s content=%r reasoning_preview=%r",
            finish_reason,
            content,
            reasoning.strip()[:120],
        )
        return reasoning.strip()

    # 顶层 text 字段(非 chat / legacy completions)
    raw_text = choice.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()

    # 真的什么都没有 — 把诊断信息打到日志(整段 response 切片 800 字够定位)
    _logger.error(
        "AI returned no readable content. finish_reason=%s content=%r "
        "message_keys=%s tool_calls=%s response_preview=%r",
        finish_reason,
        content,
        list(message.keys()) if isinstance(message, dict) else type(message).__name__,
        bool(message.get("tool_calls")) if isinstance(message, dict) else False,
        repr(data)[:800],
    )
    detail = (
        f"finish_reason={finish_reason} content={content!r} "
        f"message_keys={list(message.keys()) if isinstance(message, dict) else '?'} "
        f"raw={repr(data)[:400]}"
    )
    raise OpenAICompatibleError("AI 没有返回可读文本。", detail)


def _extract_ollama_chat_content(data: dict[str, Any]) -> str:
    message = data.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
    response = data.get("response")
    if isinstance(response, str):
        return response.strip()
    raise OpenAICompatibleError("Ollama 没有返回可读文本。", repr(data)[:500])


def _client_kwargs(timeout: float, proxy: str) -> dict[str, Any]:
    # 分开 connect / read：网络不通 10s 内快速判死,但慢慢吐 token 的云端要给完整 read 窗口。
    # 整个 timeout 当成 read timeout(LLM 慢回复的瓶颈)。pool/write 一并用同样的 read 时长,
    # connect 固定 10s 避开"网络挂了还要等 read 超时"的等待。
    connect_timeout = min(float(timeout), 10.0) if timeout and timeout > 0 else 10.0
    read_timeout = float(timeout) if timeout and timeout > 0 else None
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(read_timeout, connect=connect_timeout),
        "follow_redirects": True,
    }
    if proxy.strip():
        kwargs["proxy"] = proxy.strip()
    return kwargs


def _ollama_options(
    *,
    temperature: float | None,
    max_tokens: int | None,
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    raw_options = extra_body.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    if temperature is not None and "temperature" not in options:
        options["temperature"] = temperature
    if max_tokens is not None and "num_predict" not in options:
        options["num_predict"] = max_tokens
    return options


async def _post_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout: float,
    proxy: str,
    temperature: float | None,
    max_tokens: int | None,
    extra_headers: dict[str, str],
    extra_body: dict[str, Any],
    enable_cache: bool = False,
    cache_depth: int = 2,
) -> str:
    # 主人 2026-05-28 Step 1: 非 Claude 端点 (DeepSeek 等 OpenAI compat) 自动开真流式.
    # Claude 中间人路径保持非流式 (cache_control 注入 + Anthropic SDK 流式走 native 路径).
    try:
        from .prompt_cache import is_claude_endpoint
        _stream = not is_claude_endpoint(base_url, model)
    except Exception:  # noqa: BLE001
        _stream = False
    data = await _post_chat_completion_raw(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=timeout,
        proxy=proxy,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_headers=extra_headers,
        extra_body=extra_body,
        tools=None,
        enable_cache=enable_cache,
        cache_depth=cache_depth,
        stream=_stream,
    )
    return _extract_content(data)


# 主人 2026-05-28: contextvar 让 scope_key 跨 async/await 透传, 不破坏 chat_completion 签名.
# bot handler (handle_chat / handle_group_msg 等) 入口 set, 内部 _post_anthropic_native_chat
# 读取生成 metadata.user_id 让 Anthropic 路由到同一 cache backend.
_current_scope_key_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "catty_current_scope_key", default=None,
)


def set_current_scope_key(scope_key: str | None) -> contextvars.Token:
    """bot handler 入口调用, 设置当前对话 scope (private:<uid> / group:<gid>).

    返回的 token 可用于 reset (一般不需要, async task 自动隔离 contextvar).
    """
    return _current_scope_key_var.set(scope_key)


def get_current_scope_key() -> str | None:
    """读当前 async context 的 scope_key. None 时不发 metadata (兼容老代码)."""
    return _current_scope_key_var.get()


def _scope_to_metadata_user_id(scope_key: str | None) -> str | None:
    """scope key (private:123 / group:456) → Anthropic metadata.user_id 格式.

    Anthropic 文档要求 user_id 不能含 PII (name / email / phone). QQ 号是不可逆映射不算
    name, 但稳妥起见加前缀 qq_ 区分类型.
    """
    if not scope_key:
        return None
    sk = scope_key.strip()
    if sk.startswith("private:"):
        return f"qq_private_{sk[len('private:'):]}"
    if sk.startswith("group:"):
        # 处理 group:gid 和 group:gid:user:uid 两种 (后者主人虽用 group_history_scope=group
        # 不会出现, 但兼容 group_history_scope=user 时也工作).
        rest = sk[len("group:"):]
        return f"qq_group_{rest.replace(':', '_')}"
    return f"qq_scope_{sk.replace(':', '_').replace('/', '_')}"


async def _post_anthropic_native_chat(
    config: Config, messages: list[ChatMessage]
) -> str:
    """走 anthropic SDK 的 /v1/messages 路径 — Anthropic 原生协议.

    主人 2026-05-28: NewAPI SG relay pass_through_body_enabled=true 字节级透传 body,
    catty 可以直接发原生请求享受 3 个 beta:
      prompt-caching-2024-07-31, compact-2026-01-12, context-management-2025-06-27
    server-side compaction 在 input>150K 时自动触发并在 response 插 compaction block.

    主人 2026-05-28 C18 (vscode 公式): cache_control 单一 owner —
    全部在 post_messages_native 内部 sweep+split_system 之后由
    _apply_anthropic_cache_breakpoints 统一标. caller 不再插 marker.
    """
    from .anthropic_native_client import post_messages_native

    # 主人 2026-05-28: native /v1/messages 不接受末尾 assistant prefill
    from .prompt_cache import adapt_assistant_prefill_for_strict_user_end
    messages_for_native = adapt_assistant_prefill_for_strict_user_end(messages)
    # sweep 和 cache_control 标位都已由 post_messages_native 内部统一处理.
    prepared_messages = messages_for_native

    data = await post_messages_native(
        base_url=config.catty_openai_base_url,
        api_key=config.catty_openai_api_key,
        model=config.catty_openai_model,
        messages=prepared_messages,
        max_tokens=config.catty_max_tokens or 4096,
        temperature=config.catty_temperature,
        timeout=float(config.catty_request_timeout),
        enable_compaction=bool(getattr(config, "catty_compaction_enabled", False)),
        compaction_trigger_tokens=int(getattr(config, "catty_compaction_trigger_tokens", 150_000)),
        metadata_user_id=_scope_to_metadata_user_id(get_current_scope_key()),
    )
    return _extract_content(data)


# DeepSeek rolling hit_rate 监控 (主人目标 95-98%, 掉到 90% warn)
# maxlen=20: 最近 20 次请求滑动平均, 平滑单次噪声
_DEEPSEEK_CACHE_ROLLING: deque[tuple[int, int]] = deque(maxlen=20)
_DEEPSEEK_LOW_HIT_STREAK = 0  # 连续 < 90% 的次数, 连 3 次才 warn


def _log_cache_stats(data: dict[str, Any], model: str) -> None:
    """从 chat completion response 提取 cache hit 统计 + log.

    优先级 (单次请求只走一条分支):
    1. DeepSeek 风格: usage.prompt_cache_hit_tokens / usage.prompt_cache_miss_tokens
       (DeepSeek 独有字段, 分开 hit/miss 比 OpenAI cached_tokens 更精确)
    2. Anthropic 风格: usage.cache_read_input_tokens / usage.cache_creation_input_tokens
    3. OpenAI 风格 (兜底): usage.prompt_tokens_details.cached_tokens / usage.prompt_tokens
    """
    global _DEEPSEEK_LOW_HIT_STREAK
    usage = data.get("usage") or {}

    # === (1) DeepSeek 风格 ===
    ds_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    ds_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    if ds_hit or ds_miss:
        total = ds_hit + ds_miss
        hit_rate = ds_hit / total if total > 0 else 0.0
        # rolling 平均 (最近 20 次)
        _DEEPSEEK_CACHE_ROLLING.append((ds_hit, total))
        roll_hits = sum(h for h, _ in _DEEPSEEK_CACHE_ROLLING)
        roll_total = sum(t for _, t in _DEEPSEEK_CACHE_ROLLING)
        roll_rate = roll_hits / roll_total if roll_total > 0 else 0.0
        _logger.info(
            f"cache stats(deepseek) model={model[:20]} hit={ds_hit} "
            f"miss={ds_miss} hit_rate={hit_rate:.0%} "
            f"rolling{len(_DEEPSEEK_CACHE_ROLLING)}={roll_rate:.0%}",
        )
        # 主人 2026-05-29 Round 1: 显眼 HIT_TARGET 状态 (grep 用), 目标 95-98%
        _target_min = 0.95
        if roll_rate >= _target_min:
            _status = "OK"
        else:
            _gap = int((_target_min - roll_rate) * 100)
            _status = f"LOW(-{_gap}pp)"
        _logger.info(
            f"HIT_TARGET model={model[:20]} this={hit_rate:.1%} "
            f"rolling{len(_DEEPSEEK_CACHE_ROLLING)}={roll_rate:.1%} "
            f"target=95-98% status={_status} "
            f"hit_tok={ds_hit} miss_tok={ds_miss}",
        )
        # rolling 命中率连续 3 次 < 90% → warn (主人目标 95-98%)
        if len(_DEEPSEEK_CACHE_ROLLING) >= 5 and roll_rate < 0.9:
            _DEEPSEEK_LOW_HIT_STREAK += 1
            if _DEEPSEEK_LOW_HIT_STREAK >= 3:
                _logger.warning(
                    f"deepseek cache rolling hit_rate dropped to {roll_rate:.0%} "
                    f"(target 95-98%), check prefix stability",
                )
                _DEEPSEEK_LOW_HIT_STREAK = 0  # 提醒后重置, 避免刷屏
        else:
            _DEEPSEEK_LOW_HIT_STREAK = 0
        # 主人 2026-05-28: 推 dashboard (DeepSeek 路径 cache 实时显示)
        try:
            from . import dashboard_state as _dash
            _scope = get_current_scope_key() or ""
            _dash.push_cache_stats(_scope or model, usage, model=model)
        except Exception:  # noqa: BLE001
            pass
        return

    # === (2) Anthropic 风格 ===
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    # === (3) OpenAI 风格 (兜底) ===
    if not (cache_read or cache_create):
        prompt_details = usage.get("prompt_tokens_details") or {}
        cache_read = int(prompt_details.get("cached_tokens") or 0)
        input_tokens = int(usage.get("prompt_tokens") or 0) - cache_read
    total_input = cache_read + cache_create + input_tokens
    if total_input <= 0:
        return
    hit_rate = cache_read / total_input
    _logger.info(
        f"cache stats model={model[:20]} read={cache_read} create={cache_create} "
        f"new={input_tokens} hit={hit_rate:.0%}"
    )


async def _stream_chat_completion_attempt(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    proxy: str,
    dash_stream_id: str | None,
    dash_mod: Any,
) -> dict[str, Any]:
    """单次流式请求 attempt — 拼接 SSE chunks 成跟非流式相同的 response dict.

    主人 2026-05-28 plan-quizzical-crane Step 1: tool-based summary retrieval 链路
    依赖流式 (AI 边输出 tool_call delta 边推 dashboard). DeepSeek OpenAI compat 协议:
    `payload["stream"] = True` + `payload["stream_options"] = {"include_usage": True}`
    让最后一个 chunk 含 usage.

    成功 → 返回 {"choices": [{"message": {...}, "finish_reason": ...}], "usage": {...}}
    HTTP 错误 → raise OpenAICompatibleError (exc.status_code 标 5xx, caller 决定重试).
    """
    text_accum = ""
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    role = "assistant"

    async with httpx.AsyncClient(**_client_kwargs(timeout, proxy)) as client:
        async with client.stream(
            "POST", url, headers=headers, json=payload,
        ) as response:
            if response.status_code >= 400:
                try:
                    error_text = (await response.aread()).decode("utf-8", "ignore")[:500]
                except Exception:  # noqa: BLE001
                    error_text = f"<read failed status={response.status_code}>"
                err = OpenAICompatibleError(
                    _catty_http_status_message("AI 接口", response.status_code),
                    error_text,
                )
                err.status_code = response.status_code  # type: ignore[attr-defined]
                raise err

            async for line in response.aiter_lines():
                if not line:
                    continue
                # SSE 标准行: "data: {...}\n\n", 也兼容无 space "data:{...}"
                if line.startswith("data:"):
                    data_str = line[5:].lstrip()
                elif line.startswith(":"):
                    # SSE 注释 (keep-alive heartbeat), 跳过
                    continue
                else:
                    continue
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except (ValueError, json.JSONDecodeError):
                    continue

                # usage chunk (最后一个; DeepSeek/OpenAI 在 stream_options.include_usage 时返回)
                if chunk.get("usage"):
                    usage = chunk["usage"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}

                # role (一般出现在第一个 chunk)
                if delta.get("role"):
                    role = delta["role"]

                # 累积 text content
                content_delta = delta.get("content")
                if content_delta:
                    text_accum += content_delta
                    if dash_stream_id is not None and dash_mod is not None:
                        try:
                            dash_mod.push_event(dash_stream_id, {
                                "delta_text": content_delta,
                                "event_type": "deepseek_chunk",
                            })
                        except Exception:  # noqa: BLE001
                            pass

                # 累积 tool_calls (按 index 拼名字 + 参数)
                tc_deltas = delta.get("tool_calls") or []
                for tc_delta in tc_deltas:
                    if not isinstance(tc_delta, dict):
                        continue
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_by_index:
                        tool_calls_by_index[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    tc = tool_calls_by_index[idx]
                    if tc_delta.get("id"):
                        tc["id"] = tc_delta["id"]
                    if tc_delta.get("type"):
                        tc["type"] = tc_delta["type"]
                    fn_delta = tc_delta.get("function") or {}
                    if fn_delta.get("name"):
                        tc["function"]["name"] += fn_delta["name"]
                    if fn_delta.get("arguments"):
                        tc["function"]["arguments"] += fn_delta["arguments"]

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

    # 拼成跟非流式相同的 dict
    message: dict[str, Any] = {"role": role, "content": text_accum}
    if tool_calls_by_index:
        message["tool_calls"] = [
            tool_calls_by_index[i] for i in sorted(tool_calls_by_index.keys())
        ]
    return {
        "choices": [
            {"message": message, "finish_reason": finish_reason or "stop", "index": 0},
        ],
        "usage": usage or {},
    }


async def _post_chat_completion_raw(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout: float,
    proxy: str,
    temperature: float | None,
    max_tokens: int | None,
    extra_headers: dict[str, str],
    extra_body: dict[str, Any],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "auto",
    enable_cache: bool = False,
    cache_depth: int = 2,
    stream: bool = False,
) -> dict[str, Any]:
    """返回完整 response JSON,供 function calling 链路读 tool_calls。

    enable_cache: ST PR #3085 风 Anthropic Prompt Caching 注入 — 给 messages 末尾倒数
        depth/depth+2 处 role 切换 + system 末尾打 cache_control: ephemeral breakpoint,
        header 加 anthropic-beta: prompt-caching-2024-07-31. 仅 Claude/Anthropic 协议生效;
        OpenAI native 是 implicit caching 不需要此参数.
    cache_depth: 倒数第 N 处 role 切换打 breakpoint, ST 推荐 2.
    """
    if not base_url.strip():
        raise OpenAICompatibleError("AI 接口地址为空。")
    if not model.strip():
        raise OpenAICompatibleError("AI 模型名为空。")

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        **extra_headers,
    }
    # === ST PR #3085 移植: cache_control 注入 (Anthropic Prompt Caching) ===
    # 主人原话『prompt 也更聪明一点不要一直变不能 hit cache』.
    # 调用方传 enable_cache=True 时, 给 messages 注入最多 3 个 message-level breakpoint
    # (depth 和 depth+2 处 role 切换 + system 末尾) + 加 anthropic-beta header.
    # OpenAI native 收到 cache_control 字段会忽略 (unknown field); Claude / 中间人走
    # Claude 协议时才真正命中 cache.
    if enable_cache:
        from .prompt_cache import (
            cachingAtDepthForClaude,
            inject_system_tail_cache,
            is_claude_endpoint,
        )
        try:
            # 深拷贝避免修改调用方 messages (会被多次注入污染)
            import copy
            messages = copy.deepcopy(messages)
            # 主人 2026-05-28 Phase 1.2: 标位前先剥所有现存 cache_control (defensive single-owner).
            # history messages 持久化回来可能含上一轮 cache_control, 叠 2 份会让 relay 第二轮 500.
            _stripped = 0
            for _m in messages:
                if not isinstance(_m, dict):
                    continue
                _c = _m.get("content")
                if isinstance(_c, list):
                    for _blk in _c:
                        if isinstance(_blk, dict) and _blk.pop("cache_control", None) is not None:
                            _stripped += 1
            if _stripped > 0:
                _logger.debug(
                    "openai-compat cache single-owner: stripped %d residual marker(s)", _stripped,
                )
            cachingAtDepthForClaude(messages, cachingAtDepth=cache_depth)
            inject_system_tail_cache(messages)
            # 仅 Claude endpoint 加 anthropic-beta header (避免 OpenAI 报 unknown header)
            if is_claude_endpoint(base_url, model):
                headers["anthropic-beta"] = "prompt-caching-2024-07-31"
                # 主人 2026-05-28: 不给 tools[-1] 加 cache_control (复刻 CC). CC
                # toolToAPISchema (claude.ts) 完全不带 cacheControl 参数, 仅在
                # MCP needsToolBasedCacheMarker=true 场景才 fallback 到 tool-based
                # cache strategy. catty 无 MCP, 加 tools cache 反而让 tools 序列化
                # 字节差异破 cache 命中 (77% breaks per CC promptCacheBreakDetection).
        except Exception as exc:  # noqa: BLE001
            _logger.warning(f"prompt cache 注入失败 (降级到无 cache): {exc}")

    # === DeepSeek KV cache 前缀稳定优化 (OpenAI compat path 专属) ===
    # 主人 2026-05-28: DeepSeek 硬盘缓存是服务端自动 token 级精确前缀匹配,
    # 不需要 cache_control 字段, 但前缀必须字节稳定. 仅在非 Claude 端点跑.
    # 参考: dist/deepseek硬盘缓存规则.txt + QwenLM/qwen-code#4065.
    try:
        from .prompt_cache import (
            compute_prefix_hash,
            is_claude_endpoint,
            merge_consecutive_system_messages,
            stabilize_tools_order,
            strip_all_cache_control,
            strip_inline_dynamic_segments_from_history,
        )
        if not is_claude_endpoint(base_url, model):
            import copy as _copy
            messages = _copy.deepcopy(messages)
            # (a) strip 残留 cache_control (防 DeepSeek 未来严格校验未知字段)
            stripped = strip_all_cache_control(messages, tools)
            if stripped > 0:
                _logger.debug(
                    "deepseek prefix opt: stripped %d cache_control", stripped,
                )
            # (a2) 剥离历史 user 里的 inline 动态段 (主人 22:08 case: 历史 first_user_md5
            # 每次都变 → 6656 hit / 4827 miss / 58%. 这步让 history user 字节稳定.
            # current turn 最后一条 user 保留完整版给 LLM 读.)
            strip_hist = strip_inline_dynamic_segments_from_history(messages)
            # 主人 22:16: 命中率没起来, 加 info 级诊断看 strip 实际剥了多少 + first user 实际样子
            _diag_first_user_preview = ""
            _diag_first_user_len = 0
            for _m in messages[:5]:
                if isinstance(_m, dict) and _m.get("role") == "user":
                    _c = _m.get("content", "")
                    if isinstance(_c, list):
                        for _b in _c:
                            if isinstance(_b, dict) and _b.get("type") == "text":
                                _c = _b.get("text", "")
                                break
                    if isinstance(_c, str):
                        _diag_first_user_len = len(_c)
                        _diag_first_user_preview = _c[:200].replace("\n", "\\n")
                    break
            _logger.info(
                "deepseek prefix opt: strip_inline_history=%d first_user_len=%d "
                "first_user_head=%r",
                strip_hist, _diag_first_user_len, _diag_first_user_preview,
            )
            # (b) 合并开头连续 system → 单条 (前缀更紧凑)
            merged = merge_consecutive_system_messages(messages)
            if merged > 0:
                _logger.debug(
                    "deepseek prefix opt: merged %d system blocks", merged,
                )
            # (c) tools 字典序排锁死 (QwenLM 翻车: 顺序变→97.5%→81.5%)
            if tools:
                tools = _copy.deepcopy(tools)
                reordered = stabilize_tools_order(tools)
                if reordered:
                    _logger.debug(
                        "deepseek prefix opt: tools reordered to lexicographic",
                    )
            # (d) 前缀 hash 诊断 (对比上一轮看是否漂移)
            h = compute_prefix_hash(messages, tools, n=5)
            _logger.info(
                f"prefix_hash model={model[:20]} sys_count={h['sys_count']} "
                f"msg_count={h['msg_count']} sys_md5={h['sys_md5']} "
                f"first_user_md5={h['first_user_md5']} "
                f"tools_count={h['tools_count']} tools_md5={h['tools_md5']}",
            )
            # 主人 2026-05-29 Round 1: per-msg hash 定位 history 中段漂移
            _logger.info(f"per_msg_hash: {h.get('per_msg', '')}")
    except Exception as exc:  # noqa: BLE001
        _logger.warning(f"deepseek prefix opt 失败 (降级到原 messages): {exc}")

    # 主人 2026-05-28: claude/sonnet 经中转拒末尾 assistant prefill → 把 prefill 内容
    # 追加到最近的 user message 作为强语气 hint (NSFW spark messages 末尾结构是
    # [..., user, system, system, assistant(prefill)], user 不在倒数第二) + drop 末尾
    # assistant + 把 user 移到末尾, 保留 IC 起手机制. 真 Anthropic native API 走
    # /v1/messages (不是这里的 chat/completions), 所以这里所有 claude 都是中转.
    try:
        from .prompt_cache import adapt_assistant_prefill_for_strict_user_end, is_claude_endpoint
        if (
            messages
            and isinstance(messages[-1], dict)
            and messages[-1].get("role") == "assistant"
            and is_claude_endpoint(base_url, model)
        ):
            messages = adapt_assistant_prefill_for_strict_user_end(messages)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(f"claude assistant prefill 适配失败 (降级到原 messages): {exc}")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    payload.update(extra_body)
    if tools:
        payload["tools"] = normalize_openai_tool_schemas(tools)
        payload["tool_choice"] = tool_choice

    # === Streaming params (主人 2026-05-28 Step 1: 真流式) ===
    if stream:
        payload["stream"] = True
        # include_usage: 让最后一个 chunk 返回 usage (DeepSeek / OpenAI 都支持)
        payload["stream_options"] = {"include_usage": True}

    # 主人 2026-05-29 Round 10 回滚: 完全不传 user 字段, 让 DeepSeek 后端公共前缀检测
    # 跨所有 catty 请求自动落盘共享 cache 池. 之前 Round 6 user=scope_key, Round 7 升级
    # scope+sys_md5 — 反而让每种 prefix 独立 namespace 都 cold start (主人 dashboard
    # 看到 67% / 27% 是各自独立 cache cold 的结果).
    # DeepSeek 文档明确: "多请求公共前缀检测落盘" — 不传 user 时自动跨请求共享 prefix.

    # 主人 2026-05-29 Round 10: dump 完整 payload 到磁盘 (验证 user/stream 等改动真生效)
    # 放在 payload 完整构造之后, 在 retry loop 之前.
    try:
        import time as _t
        from pathlib import Path as _Path
        _dump_root = _Path("D:/CattyQQAI/logs/req_dumps")
        _dump_root.mkdir(parents=True, exist_ok=True)
        _scope_safe = (get_current_scope_key() or "noscope").replace(
            ":", "_",
        ).replace("/", "_").replace("\\", "_")
        _ts = int(_t.time() * 1000)
        _dump = _dump_root / f"{_scope_safe}_{_ts}.json"
        _dump_obj = {
            "model": model,
            "base_url_head": base_url[:50],
            "payload_keys": sorted(payload.keys()),
            "payload_user": payload.get("user"),
            "payload_stream": payload.get("stream", False),
            "stream_param": stream,
            "enable_cache_param": enable_cache,
            "scope_key": get_current_scope_key(),
            "messages": messages,
            "tools": tools,
        }
        _dump_str = json.dumps(_dump_obj, ensure_ascii=False, indent=2, default=str)
        _dump.write_text(_dump_str, encoding="utf-8")
        _olds = sorted(_dump_root.glob(f"{_scope_safe}_*.json"))
        for _o in _olds[:-8]:
            try:
                _o.unlink()
            except Exception:  # noqa: BLE001
                pass
    except Exception as _dump_exc:  # noqa: BLE001
        import traceback as _tb
        _logger.warning(
            f"req dump failed: {type(_dump_exc).__name__}: {_dump_exc}\n"
            + _tb.format_exc()[:600],
        )

    # === Dashboard stream lifecycle hook (DeepSeek / OpenAI compat 路径) ===
    # 主人 2026-05-28: 让非 Anthropic 路径也能在 dashboard 上看到对话.
    # stream=True: chunk-by-chunk push (helper 内部已做)
    # stream=False: 收到完整 response 后一次性 push 完整 text 当一个 delta
    # finally 块兜底清理 active stream (失败 raise 路径).
    _dash_stream_id: str | None = None
    try:
        from . import dashboard_state as _dash_mod
        _dash_stream_id = _dash_mod.start_stream(model=model)
    except Exception:  # noqa: BLE001
        _dash_mod = None  # type: ignore[assignment]

    try:
        # 主人:任何 5xx 自动 retry 3 次(共 4 次尝试),3 次都失败才上抛
        # 4xx 是 client error,重试也是一样的错,不重试
        last_error: OpenAICompatibleError | None = None
        for attempt in range(4):
            if stream:
                # === Streaming branch (主人 2026-05-28 Step 1) ===
                try:
                    data = await _stream_chat_completion_attempt(
                        url=_chat_completions_url(base_url),
                        headers=headers,
                        payload=payload,
                        timeout=timeout,
                        proxy=proxy,
                        dash_stream_id=_dash_stream_id,
                        dash_mod=_dash_mod,
                    )
                except OpenAICompatibleError as exc:
                    _status = getattr(exc, "status_code", None)
                    if _status and 500 <= _status < 600:
                        # 5xx → retry
                        last_error = exc
                        if attempt < 3:
                            backoff = 0.5 * (2 ** attempt)
                            _logger.info(
                                f"AI 接口 (stream) {_status} retry {attempt + 1}/3 after {backoff}s",
                            )
                            await asyncio.sleep(backoff)
                            continue
                        raise
                    # 4xx 直接抛
                    raise
                _log_cache_stats(data, model)
                # 流式分支: chunk 已经 chunk-by-chunk 推过, 这里只 end_stream
                if _dash_stream_id and _dash_mod is not None:
                    try:
                        _dash_mod.end_stream(_dash_stream_id, final_usage=data.get("usage"))
                        _dash_stream_id = None
                    except Exception:  # noqa: BLE001
                        pass
                return data

            # === Non-streaming branch (现有逻辑保留) ===
            async with httpx.AsyncClient(**_client_kwargs(timeout, proxy)) as client:
                response = await client.post(
                    _chat_completions_url(base_url), headers=headers, json=payload,
                )

            if response.status_code < 400:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise OpenAICompatibleError(
                        "AI 返回的不是 JSON。", response.text[:500],
                    ) from exc
                # === cache hit 监测 (DeepSeek / Anthropic / OpenAI 都识别) ===
                # 主人 2026-05-28: 改为始终调用, 函数内部自己判断有无 cache 字段
                # (DeepSeek 路径 enable_cache=False 但仍想看 prompt_cache_hit_tokens).
                _log_cache_stats(data, model)
                # === Dashboard: 推完整 text 当一次 delta + end_stream ===
                if _dash_stream_id and _dash_mod is not None:
                    try:
                        _text = ""
                        _choices = data.get("choices") or []
                        if _choices:
                            _msg = _choices[0].get("message") or {}
                            _content = _msg.get("content")
                            if isinstance(_content, str):
                                _text = _content
                            elif _content is not None:
                                _text = str(_content)
                        if _text:
                            _dash_mod.push_event(_dash_stream_id, {
                                "delta_text": _text,
                                "event_type": "openai_completion",
                            })
                        _dash_mod.end_stream(_dash_stream_id, final_usage=data.get("usage"))
                        _dash_stream_id = None  # 标记已清理, finally 不再重复 end
                    except Exception:  # noqa: BLE001
                        pass
                return data

            detail = response.text[:500]
            if not (500 <= response.status_code < 600):
                # 4xx 直接抛,无重试意义
                raise OpenAICompatibleError(
                    _catty_http_status_message("AI 接口", response.status_code), detail,
                )

            last_error = OpenAICompatibleError(
                _catty_http_status_message("AI 接口", response.status_code), detail,
            )
            if attempt < 3:
                backoff = 0.5 * (2 ** attempt)  # 0.5 / 1.0 / 2.0 s
                _logger.info(
                    f"AI 接口 {response.status_code} retry {attempt + 1}/3 after {backoff}s"
                )
                await asyncio.sleep(backoff)

        assert last_error is not None
        raise last_error
    finally:
        # 异常路径兜底清理 (200 OK 后 _dash_stream_id 已设 None, 这里只处理 raise 出去的情况)
        if _dash_stream_id:
            try:
                from . import dashboard_state as _dash_mod2
                _dash_mod2.end_stream(_dash_stream_id)
            except Exception:  # noqa: BLE001
                pass


async def _post_ollama_chat(
    *,
    base_url: str,
    model: str,
    messages: list[ChatMessage],
    timeout: float,
    proxy: str,
    temperature: float | None,
    max_tokens: int | None,
    extra_headers: dict[str, str],
    extra_body: dict[str, Any],
) -> str:
    if not base_url.strip():
        raise OpenAICompatibleError("Ollama 接口地址为空。")
    if not model.strip():
        raise OpenAICompatibleError("Ollama 模型名为空。")

    options = _ollama_options(temperature=temperature, max_tokens=max_tokens, extra_body=extra_body)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if options:
        payload["options"] = options
    if "keep_alive" in extra_body:
        payload["keep_alive"] = extra_body["keep_alive"]
    if "think" in extra_body:
        payload["think"] = extra_body["think"]

    headers = {
        "Content-Type": "application/json",
        **extra_headers,
    }
    async with httpx.AsyncClient(**_client_kwargs(timeout, proxy)) as client:
        response = await client.post(_ollama_chat_url(base_url), headers=headers, json=payload)

    if response.status_code >= 400:
        detail = response.text[:500]
        raise OpenAICompatibleError(_catty_http_status_message("Ollama 接口", response.status_code), detail)

    try:
        data = response.json()
    except ValueError as exc:
        raise OpenAICompatibleError("Ollama 返回的不是 JSON。", response.text[:500]) from exc

    return _extract_ollama_chat_content(data)


def _needs_first_frame(url: str, content_type: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".gif", ".webp")) or "gif" in content_type or "webp" in content_type


def _first_frame_data_url(data: bytes) -> str | None:
    try:
        with Image.open(BytesIO(data)) as image:
            frame_count = getattr(image, "n_frames", 1)
            image_format = (image.format or "").upper()
            if image_format not in {"GIF", "WEBP"} and frame_count <= 1:
                return None
            frame = next(ImageSequence.Iterator(image)).convert("RGBA")
            output = BytesIO()
            frame.save(output, format="PNG")
    except Exception:
        return None
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _data_url_content_type_and_bytes(url: str) -> tuple[str, bytes] | None:
    if not url.startswith("data:"):
        return None
    header, separator, encoded = url.partition(",")
    if not separator or ";base64" not in header.lower():
        return None
    content_type = header[5:].split(";", 1)[0].lower()
    try:
        data = base64.b64decode(encoded, validate=False)
    except Exception:
        return None
    return content_type, data


async def _vision_image_url(config: Config, url: str) -> str:
    if url.startswith("data:"):
        parsed = _data_url_content_type_and_bytes(url)
        if parsed is not None:
            content_type, data = parsed
            if _needs_first_frame(url, content_type):
                return _first_frame_data_url(data) or url
        return url
    timeout = config.catty_vision_request_timeout or config.catty_request_timeout
    async with httpx.AsyncClient(**_client_kwargs(timeout, config.catty_http_proxy)) as client:
        response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if not _needs_first_frame(url, content_type):
        data_url = _first_frame_data_url(response.content)
        return data_url or url
    return _first_frame_data_url(response.content) or url


def _image_analysis_from_reply(reply: str) -> dict[str, Any]:
    if not reply.strip():
        return {}
    parsed = _json_object(reply) or {}
    try:
        interest = int(parsed.get("interest", 0))
    except (TypeError, ValueError):
        interest = 0
    emotion_tags = parsed.get("emotion_tags")
    if isinstance(emotion_tags, str):
        tags = [item.strip() for item in emotion_tags.replace("，", ",").split(",") if item.strip()]
    elif isinstance(emotion_tags, list):
        tags = [str(item).strip() for item in emotion_tags if str(item).strip()]
    else:
        tags = []
    return {
        "summary": str(parsed.get("summary") or "").strip() or reply.strip(),
        "interest": max(min(interest, 100), 0),
        "emotion_tags": tags,
        "expression": str(parsed.get("expression") or "").strip(),
        "emoji_query": str(parsed.get("emoji_query") or "").strip(),
        "save_as_emoji": bool(parsed.get("save_as_emoji")),
        "raw": reply,
    }


async def download_binary(config: Config, url: str, *, timeout: float | None = None) -> tuple[bytes, str]:
    request_timeout = timeout or config.catty_vision_request_timeout or config.catty_request_timeout
    async with httpx.AsyncClient(**_client_kwargs(request_timeout, config.catty_http_proxy)) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.content, response.headers.get("content-type", "")


def _fallback_is_configured(config: Config) -> bool:
    # 主人 2026-05-28: 恢复真实判断 — 主人意图『sonnet 主, deepseek 备, 所有部分自动 fallback』.
    # 之前 (5 月初) 硬关闭是因为本地 ollama qwen 推理慢; 现在 ai_fallback 指向 deepseek API,
    # 性能跟主 cloud 接近, 让所有路径都能在 sonnet 失败时自动降到 deepseek.
    if not bool(getattr(config, "catty_ai_fallback_enabled", False)):
        return False
    if not str(getattr(config, "catty_ai_fallback_base_url", "") or "").strip():
        return False
    if not str(getattr(config, "catty_ai_fallback_api_key", "") or "").strip():
        return False
    if not str(getattr(config, "catty_ai_fallback_model", "") or "").strip():
        return False
    return True


def _fallback_should_strip_system(config: Config) -> bool:
    """catty-* 派生模型把人格烧进了 SYSTEM 层,再发 system role 会破坏 KV cache。"""
    if bool(getattr(config, "catty_ai_fallback_strip_system_messages", False)):
        return True
    model = str(getattr(config, "catty_ai_fallback_model", "") or "").strip().lower()
    return model.startswith("catty-")


def _strip_system_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if m.get("role") != "system"]


async def _check_mc_gate_or_raise(config: Config) -> None:
    """If MC has players online, refuse to run the local fallback model."""
    if not bool(getattr(config, "catty_ai_fallback_mc_gate_enabled", False)):
        return
    host = str(getattr(config, "catty_ai_fallback_mc_server_host", "") or "").strip()
    if not host:
        return
    port = int(getattr(config, "catty_ai_fallback_mc_server_port", 0) or 0)
    if port <= 0:
        return
    timeout = float(getattr(config, "catty_ai_fallback_mc_ping_timeout_seconds", 3.0) or 3.0)
    try:
        has_players = await mc_has_players(host, port, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("MC gate ping failed (%s); allowing fallback to proceed", exc)
        return
    if has_players:
        _logger.info("MC gate: players online at %s:%d, refusing local fallback", host, port)
        raise MCBusyError(
            "喵呜，MC 群友正在玩游戏中，猫猫这会儿不能用本地脑子顶上来——主人稍等一下再戳。",
            "MC has players online; local 7B fallback is gated off to protect game performance.",
        )


async def _warmup_fallback_if_cold(config: Config) -> bool:
    """如果本地 fallback 模型最近没有被打到,先发一个 1-token "hi" 把它 load 上来。

    Ollama 第一次推理同时付"GGUF 从磁盘 load 到显存"+"prompt 处理"两份成本——
    在 5070Ti Laptop 上对 7B Q4_K_M 大概 60-120s。先用最小 prompt(1 token)单独
    付掉 load 那一份,然后真正用户请求只付 prompt 处理那一份(模型已经在显存里,
    KV cache 在 OLLAMA_KEEP_ALIVE=-1 下持久)。

    返回 True 代表本次确实做了 warmup;False 代表跳过(已经 warm 或者不支持)。
    失败不抛异常——真正的 fallback 调用会自己再试一次,该报错就让它报。
    """
    if _fallback_is_warm():
        return False
    base_url = config.catty_ai_fallback_base_url
    api_key = config.catty_ai_fallback_api_key
    model = config.catty_ai_fallback_model
    if not str(base_url or "").strip() or not str(model or "").strip():
        return False
    # 只对 Ollama 后端做 warmup;OpenAI-compatible HTTP 后端用法不一样,跳过。
    if not _looks_like_ollama_route(base_url, api_key, {}):
        return False
    _logger.info("Fallback cold-start: warming %s with 1-token hi", model)
    try:
        await _post_ollama_chat(
            base_url=base_url,
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            timeout=_FALLBACK_WARMUP_TIMEOUT_SECONDS,
            proxy=config.catty_http_proxy,
            temperature=None,
            max_tokens=1,
            extra_headers={},
            extra_body={"keep_alive": -1},
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "Fallback warmup failed (%s: %s); real call will retry directly",
            exc.__class__.__name__,
            exc,
        )
        return False
    _mark_fallback_warmed()
    _logger.info("Fallback %s warmed up; real request can stream immediately", model)
    return True


async def _post_fallback_chat(config: Config, messages: list[ChatMessage]) -> str:
    await _check_mc_gate_or_raise(config)

    # 冷启动时先单独 warmup,避免真正请求同时承担 load + 大 prompt 处理被卡 60s+。
    await _warmup_fallback_if_cold(config)

    base_url = config.catty_ai_fallback_base_url
    api_key = config.catty_ai_fallback_api_key
    model = config.catty_ai_fallback_model
    extra_body = dict(config.catty_ai_fallback_extra_body or {})
    extra_headers = config.catty_ai_fallback_extra_headers or {}
    timeout = config.catty_ai_fallback_request_timeout or config.catty_request_timeout
    temperature = config.catty_ai_fallback_temperature
    max_tokens = config.catty_ai_fallback_max_tokens

    if _fallback_should_strip_system(config):
        stripped = _strip_system_messages(messages)
        dropped = len(messages) - len(stripped)
        if dropped > 0:
            _logger.info("fallback: dropped %d system message(s) for %s (persona baked in)", dropped, model)
        messages = stripped

    # 内存预算够时让 7B 留在内存复用更快。想要立刻卸载,在 config 的
    # ai_fallback.extra_body 里加 "keep_alive": 0 即可。
    if _looks_like_ollama_route(base_url, api_key, extra_body):
        result = await _post_ollama_chat(
            base_url=base_url,
            model=model,
            messages=messages,
            timeout=timeout,
            proxy=config.catty_http_proxy,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_headers=extra_headers,
            extra_body=extra_body,
        )
        _mark_fallback_warmed()
        return result
    result = await _post_chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=timeout,
        proxy=config.catty_http_proxy,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_headers=extra_headers,
        extra_body=extra_body,
    )
    _mark_fallback_warmed()
    return result


async def chat_completion_with_tools(
    config: Config,
    messages: list[ChatMessage],
    *,
    tools: list[dict[str, Any]] | None,
    tool_executor: Any | None,
    max_rounds: int = 3,
    max_calls_per_round: int = 3,
    router_base_url: str = "",
    router_api_key: str = "",
    router_model: str = "",
    router_label: str = "",
) -> str:
    """OpenAI function calling 主回复循环。

    tools/tool_executor 任一为空 → 退化到 plain chat_completion(完整 fallback 链)。
    tool 调度过程中云端抛错也直接降级到 chat_completion(保留本地 7B 兜底)。
    tool_executor 签名:async (name: str, arguments_json: str) -> dict。

    router_* 参数: 三件套都填时, 把这次 tool 调用整段切到 router endpoint
    (例如 catty_imagegen 意图命中 → 切 DeepSeek codex_instant 替主 AI 出 tool_call,
    省 Opus token 并避开 OOC 触发). 不带 router 时走 catty_openai_* 主通道.
    Native /v1/messages (catty_anthropic_native_enabled=True) 与 router 互斥, 一旦
    router 三件套齐就强制 OpenAI-compat 路径.
    """
    if not tools or tool_executor is None:
        _logger.info("tool_chat: tools/executor empty → fallback to plain chat_completion")
        return await chat_completion(config, messages)
    if not getattr(config, "catty_tools_enabled", True):
        _logger.info("tool_chat: catty_tools_enabled=False → fallback to plain chat_completion")
        return await chat_completion(config, messages)
    _router_active = bool(router_base_url.strip() and router_api_key.strip() and router_model.strip())
    if _cloud_is_unhealthy() and not _router_active:
        # 云端冷却期不带 tools 试,直接走 fallback 链 (router 走独立 endpoint, 不受主云冷却影响)。
        _logger.info("tool_chat: cloud unhealthy → fallback to plain chat_completion (no tools)")
        return await chat_completion(config, messages)
    # 主人 2026-05-28: native_enabled 时 with_tools 走 native /v1/messages 完整 tool
    # calling loop (post_messages_native_data 自动转换 OpenAI tools → Anthropic tools
    # 格式 + history 里 OpenAI 风格 tool 消息 → Anthropic native tool_use/tool_result
    # blocks), 享受 cache hit 100% 同时保留 tool 调用能力.
    # router 模式强制走 OpenAI-compat (DeepSeek 等不走 Anthropic /v1/messages).
    _native_route = bool(getattr(config, "catty_anthropic_native_enabled", False)) and not _router_active
    if _router_active:
        _logger.info(
            "tool_chat: router=%s model=%s starting with %d tools (OpenAI-compat, bypass main AI)",
            router_label or "custom", router_model, len(tools),
        )
    elif _native_route:
        _logger.info("tool_chat: starting with %d tools available (native /v1/messages)", len(tools))
    else:
        _logger.info("tool_chat: starting with %d tools available (OpenAI-compat)", len(tools))

    history: list[ChatMessage] = list(messages)
    for round_idx in range(max(1, max_rounds)):
        try:
            if _native_route:
                from .anthropic_native_client import post_messages_native_data
                data = await post_messages_native_data(
                    config, history, tools=tools,
                    metadata_user_id=_scope_to_metadata_user_id(get_current_scope_key()),
                )
            elif _router_active:
                # 主人 2026-05-28 Step 1: 非 Claude 端点开真流式 (chunk push dashboard)
                try:
                    from .prompt_cache import is_claude_endpoint
                    _router_stream = not is_claude_endpoint(router_base_url, router_model)
                except Exception:  # noqa: BLE001
                    _router_stream = False
                data = await _post_chat_completion_raw(
                    base_url=router_base_url,
                    api_key=router_api_key,
                    model=router_model,
                    messages=history,
                    timeout=config.catty_request_timeout,
                    proxy=config.catty_http_proxy,
                    temperature=config.catty_temperature,
                    max_tokens=config.catty_max_tokens,
                    extra_headers={},
                    extra_body={},
                    tools=tools,
                    tool_choice="auto",
                    enable_cache=False,
                    cache_depth=2,
                    stream=_router_stream,
                )
            else:
                try:
                    from .prompt_cache import is_claude_endpoint
                    _openai_stream = not is_claude_endpoint(
                        config.catty_openai_base_url, config.catty_openai_model,
                    )
                except Exception:  # noqa: BLE001
                    _openai_stream = False
                data = await _post_chat_completion_raw(
                    base_url=config.catty_openai_base_url,
                    api_key=config.catty_openai_api_key,
                    model=config.catty_openai_model,
                    messages=history,
                    timeout=config.catty_request_timeout,
                    proxy=config.catty_http_proxy,
                    temperature=config.catty_temperature,
                    max_tokens=config.catty_max_tokens,
                    extra_headers=config.catty_openai_extra_headers,
                    extra_body=config.catty_openai_extra_body,
                    tools=tools,
                    tool_choice="auto",
                    enable_cache=bool(getattr(config, "catty_prompt_cache_enabled", False)),
                    cache_depth=int(getattr(config, "catty_prompt_cache_depth", 2) or 2),
                    stream=_openai_stream,
                )
        except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as exc:
            _logger.warning(
                "chat_completion_with_tools: round %d cloud call failed (%s); degrading to plain chat_completion",
                round_idx,
                exc.__class__.__name__,
            )
            # 降级到 plain 调用,让原有 fallback/cooldown 逻辑接管(它会自己 mark unhealthy)。
            return await chat_completion(config, messages)

        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAICompatibleError("AI 返回格式不符合 Chat Completions。", repr(data)[:500]) from exc
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None

        if not tool_calls:
            # 模型直接给了最终回复
            if _cloud_is_unhealthy():
                _mark_cloud_healthy()
            return _extract_content(data)

        # 协议要求:把 assistant 含 tool_calls 的消息原样写回 history
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "tool_calls": tool_calls,
        }
        # 保留 content(可能为 None / 空字符串都行,OpenAI 协议允许)
        if isinstance(message.get("content"), (str, list)):
            assistant_msg["content"] = message["content"]
        else:
            assistant_msg["content"] = None
        history.append(assistant_msg)

        # 限制每轮最多执行 N 个 tool_call,超出的直接回 truncated 提示
        executed: list[tuple[str, str, str]] = []  # (call_id, name, args_json)
        for call in tool_calls[: max(1, max_calls_per_round)]:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "").strip()
            func = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(func.get("name") or "").strip()
            args_json = func.get("arguments")
            if not isinstance(args_json, str):
                try:
                    args_json = json.dumps(args_json or {}, ensure_ascii=False)
                except (TypeError, ValueError):
                    args_json = "{}"
            if not call_id or not name:
                continue
            executed.append((call_id, name, args_json))

        # 并发执行 tool calls(每个都自己有 TTL 缓存,不会真的并发打爆 memory_store)
        if executed:
            # info-level 可观察性:每轮记录 AI 触发了哪些 tool + 参数前缀,
            # 不输出完整 args(NSFW 搜索/笔记内容可能敏感,只截前 80 字看意图)。
            for _call_id, name, args_json in executed:
                _aj = args_json or ""
                _logger.info(
                    "tool_call: %s args_len=%d args=%s%s",
                    name, len(_aj), _aj[:400],
                    f"...(+{len(_aj)-400} chars)" if len(_aj) > 400 else "",
                )
        tool_results = await asyncio.gather(
            *[tool_executor(name, args_json) for _call_id, name, args_json in executed],
            return_exceptions=True,
        )
        # Phase B1: 收集成功的 tool result 给 cascade 检查用
        cascade_inputs: list[tuple[str, Any]] = []
        # 主人 2026-05-29: tool short-circuit — executor return dict 含 "_short_circuit_reply"
        # 字段时, 直接 return 那段文本作为最终回复, 跳过 follow-up 主 AI 调用. 用于
        # catty_imagegen agent 模式 (tool 内部叫 deepseek 出 prompt+短评 + 把图 push 到群,
        # 主 sonnet 不需要再发 100K+ follow-up — 杜绝 cache miss 二次炸 + tool_use 配对 500).
        # 只在单 tool_call 一轮短路, 多 call 时让主 AI 接着综合.
        short_circuit_reply: str | None = None
        for (call_id, name, _args_json), result in zip(executed, tool_results):
            if isinstance(result, BaseException):
                payload = {"error": f"{name} 抛异常: {result.__class__.__name__}: {result}"}
                _logger.warning("Tool %s raised: %s", name, result)
            elif not isinstance(result, dict):
                payload = {"value": result}
            else:
                payload = result
                cascade_inputs.append((name, payload))
            if (
                len(executed) == 1
                and isinstance(payload, dict)
                and isinstance(payload.get("_short_circuit_reply"), str)
                and payload["_short_circuit_reply"].strip()
            ):
                short_circuit_reply = payload["_short_circuit_reply"].strip()
                _logger.info(
                    "tool_chat: %s short-circuit return (reply_len=%d, skip follow-up)",
                    name, len(short_circuit_reply),
                )
            # 写 history 时剥掉 _short_circuit_* 内部 sentinel, 避免污染 follow-up token
            payload_for_history = (
                {k: v for k, v in payload.items() if not str(k).startswith("_short_circuit") and k != "_deepseek_plan"}
                if isinstance(payload, dict) else payload
            )
            try:
                content_str = json.dumps(payload_for_history, ensure_ascii=False)
            except (TypeError, ValueError):
                content_str = json.dumps({"error": "结果无法序列化为 JSON"}, ensure_ascii=False)
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": content_str,
                }
            )

        if short_circuit_reply:
            if _cloud_is_unhealthy():
                _mark_cloud_healthy()
            return short_circuit_reply

        # Phase B1: tool cascade — 看上一轮 tool 结果, 给 AI 一个『下一步推荐调 X』hint
        if cascade_inputs:
            try:
                from .tool_cascade import build_post_tool_hint
                _cascade_hint = build_post_tool_hint(cascade_inputs)
                if _cascade_hint:
                    history.append({"role": "system", "content": _cascade_hint})
            except Exception as _cascade_exc:  # noqa: BLE001
                _logger.debug("tool_cascade check failed (non-fatal): %s", _cascade_exc)

        # Phase B3: tool result post-process hint — 防 AI 拿到 result 后复读 JSON / 列字段名
        if executed:
            history.append(
                {
                    "role": "system",
                    "content": (
                        "请基于以上 tool 结果**用笨猫口吻**回答, **绝不**复读 JSON 字段名 "
                        "(如 'long_term_summary' / 'matches' / 'extract' 等), "
                        "**绝不**贴原始字典/数组. 把结果挑出 1-2 个有用点用猫娘语气短句串起来即可."
                    ),
                }
            )

        # 处理被截断的 tool_calls:给模型一条提示,下一轮可以继续
        truncated = len(tool_calls) - len(executed)
        if truncated > 0:
            history.append(
                {
                    "role": "system",
                    "content": f"上一轮还有 {truncated} 个 tool 调用被截断,本轮请基于已收到的工具结果继续。",
                }
            )

    # 循环达到上限还在调 tool — 强制再发一次纯回复,禁掉 tools。
    history.append(
        {
            "role": "system",
            "content": "已达到本次主回复的工具调用上限,请直接基于已有信息和上下文给最终回复,不再调用工具。",
        }
    )
    return await chat_completion(config, history)


async def chat_completion(config: Config, messages: list[ChatMessage]) -> str:
    fallback_ready = _fallback_is_configured(config)
    cooldown = float(getattr(config, "catty_ai_fallback_cooldown_seconds", 300.0))

    # 云端在冷却期：直接走 fallback，不再戳云白白等超时。
    # MCBusyError（MC 有玩家）会自然向上传播给用户看到。
    if fallback_ready and _cloud_is_unhealthy():
        _logger.info(
            "chat_completion: cloud in cooldown (%.0fs remaining), routing to local fallback %s",
            _cloud_health_remaining_seconds(),
            config.catty_ai_fallback_model,
        )
        return await _post_fallback_chat(config, messages)

    # Anthropic native /v1/messages 分支 (主人 2026-05-28: NewAPI SG relay 透传 body 已开,
    # context_management server-side compaction 已在 Anthropic 端激活).
    # 失败降级走原 OpenAI-compat /chat/completions 路径 (兜底保证主链路不挂).
    if getattr(config, "catty_anthropic_native_enabled", False):
        try:
            result = await _post_anthropic_native_chat(config, messages)
            if _cloud_is_unhealthy():
                _logger.info("chat_completion: native path recovered, clearing cooldown")
                _mark_cloud_healthy()
            return result
        except Exception as native_exc:  # noqa: BLE001
            _logger.warning(
                "chat_completion: native /v1/messages failed (%s), fallback to /chat/completions",
                native_exc.__class__.__name__,
            )
            # 不熔断 (cloud_unhealthy 不标记), 直接降级到 OpenAI-compat 同一中转端点

    # 正常走云。
    # Phase A: 主回复路径补 enable_cache (之前 bug: 只 with_tools / codex_instant 路径有 cache).
    # 动态 cache_depth: 热 session (history>=12 条 user/assistant) → 4, 冷 → 2.
    # depth=4 多覆盖 history (热 session prefix 稳定到更深), depth=2 避免冷 session 频繁 invalidate.
    _cache_enable = bool(getattr(config, "catty_prompt_cache_enabled", True))
    _non_system_count = sum(
        1 for m in messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    )
    _cache_depth_base = int(getattr(config, "catty_prompt_cache_depth", 2))
    _cache_depth_dynamic = 4 if _non_system_count >= 12 else _cache_depth_base
    try:
        result = await _post_chat_completion(
            base_url=config.catty_openai_base_url,
            api_key=config.catty_openai_api_key,
            model=config.catty_openai_model,
            messages=messages,
            timeout=config.catty_request_timeout,
            proxy=config.catty_http_proxy,
            temperature=config.catty_temperature,
            max_tokens=config.catty_max_tokens,
            extra_headers=config.catty_openai_extra_headers,
            extra_body=config.catty_openai_extra_body,
            enable_cache=_cache_enable,
            cache_depth=_cache_depth_dynamic,
        )
        # 云端成功:清除任何残留 unhealthy 标记
        if _cloud_is_unhealthy():
            _logger.info("chat_completion: cloud recovered, clearing cooldown")
            _mark_cloud_healthy()
        return result
    except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as cloud_exc:
        if not fallback_ready:
            raise
        _mark_cloud_unhealthy(cooldown)
        _logger.warning(
            "chat_completion: cloud call failed (%s), routing to local fallback %s for next %.0fs",
            cloud_exc.__class__.__name__,
            config.catty_ai_fallback_model,
            cooldown,
        )
        try:
            return await _post_fallback_chat(config, messages)
        except MCBusyError:
            # MC 有人 → 直接抛"游戏中不可用"给用户，不被云错误覆盖
            raise
        except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as fb_exc:
            # 云端和 fallback 都挂了:抛云的原始错误,把 fallback 异常作为 __cause__
            raise cloud_exc from fb_exc


async def local_critic_completion(
    config: Config,
    messages: list[ChatMessage],
    *,
    timeout: float | None = None,
    max_tokens: int | None = None,
    extra_body: dict[str, Any] | None = None,
) -> str:
    body = extra_body if extra_body is not None else config.catty_local_critic_extra_body
    request_timeout = timeout or config.catty_local_critic_request_timeout or config.catty_request_timeout
    request_max_tokens = max_tokens if max_tokens is not None else config.catty_local_critic_max_tokens
    if _looks_like_ollama_route(config.catty_local_critic_base_url, config.catty_local_critic_api_key, body):
        return await _post_ollama_chat(
            base_url=config.catty_local_critic_base_url,
            model=config.catty_local_critic_model,
            messages=messages,
            timeout=request_timeout,
            proxy=config.catty_http_proxy,
            temperature=config.catty_local_critic_temperature,
            max_tokens=request_max_tokens,
            extra_headers=config.catty_local_critic_extra_headers,
            extra_body=body,
        )

    return await _post_chat_completion(
        base_url=config.catty_local_critic_base_url,
        api_key=config.catty_local_critic_api_key,
        model=config.catty_local_critic_model,
        messages=messages,
        timeout=request_timeout,
        proxy=config.catty_http_proxy,
        temperature=config.catty_local_critic_temperature,
        max_tokens=request_max_tokens,
        extra_headers=config.catty_local_critic_extra_headers,
        extra_body=body,
    )


def _json_decision(text: str, key: str) -> bool:
    raw = text.strip()
    if not raw:
        return False
    parsed = _json_object(raw)
    if isinstance(parsed, dict):
        value = parsed.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "y", "1", "reply", "split"}

    normalized = raw.strip().lower()
    if key == "reply":
        return normalized in {"reply", "yes", "true", "1", "回复"}
    if key == "split":
        return normalized in {"split", "yes", "true", "1", "拆分"}
    return False


def _json_object(text: str) -> dict[str, Any] | None:
    """LLM 输出宽容 JSON 解析:fence / 智能引号 / 尾逗号 / 单引号 / mixed text 都能恢复。

    实际逻辑在 parsers.lenient_json_object;保留本函数名是为了不动现有 caller。
    """
    return lenient_json_object(text)


async def chat_completion_summary(config: Config, messages: list[ChatMessage]) -> str:
    """专用后台总结路径：优先走 ai_fallback (deepseek)，避免烧主 AI (opus/sonnet) token。

    主人 2026-05-28: 群/私聊/成员/游戏摘要属于后台批处理, deepseek-v4-flash 完全够用,
    没必要每天用 opus 烧 ¥. fallback 不可用或调用失败时回退到 chat_completion (主 AI)。
    """
    if _fallback_is_configured(config):
        try:
            return await _post_fallback_chat(config, messages)
        except MCBusyError:
            _logger.info("chat_completion_summary: MC busy, falling back to main AI for summary")
        except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as exc:  # noqa: BLE001
            _logger.warning(
                "chat_completion_summary: ai_fallback failed (%s), falling back to main AI",
                exc.__class__.__name__,
            )
    return await chat_completion(config, messages)


async def chat_completion_instant(config: Config, messages: list[ChatMessage], *, fallback_max_tokens: int = 80) -> str:
    """走 catty_filter_* 配置(spark 这种小快模型)的瞬时完成。

    用途:placeholder 等候语、签到/积分卡 caption 这种 1-2 句猫娘短话——
    主回复模型 (gpt-5.5 等) 响应慢,这里需要『立刻』出文案不能等。
    复用 _filter_completion 的路由逻辑;无 spark 配置时自动回退到 audit/openai。
    """
    return await _filter_completion(config, messages, fallback_max_tokens=fallback_max_tokens)


async def _post_with_fallback(
    config: Config,
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[ChatMessage],
    timeout: float,
    temperature: float | None,
    max_tokens: int | None,
    extra_headers: dict[str, str],
    extra_body: dict[str, Any],
    enable_cache: bool = False,
    cache_depth: int = 2,
    label: str = "",
) -> str:
    """主 endpoint 调用失败 → 自动 fallback 到 ai_fallback config (sonnet → deepseek).

    主人 2026-05-28: 覆盖 spark / filter / mood / anger / summarize 等所有路径.
    vision 除外 (主人原话 'vision 不用 fallback', 因 deepseek-v4-flash 不支持图).
    主回复 chat_completion 已有 fallback 链, 本函数给小模型短路径用.

    主人 2026-05-28: native_enabled + Claude endpoint 时, 也走 native /v1/messages
    (绕开 NewAPI 中转层 OpenAI→Anthropic 转换 bug: codex_instant 等路径 OpenAI-compat
    带 role=system 被中转层转 Anthropic 时不提到顶层 system, Anthropic 直接 400 拒收).
    """
    try:
        # native /v1/messages 路径优先 (Claude endpoint + native_enabled)
        if getattr(config, "catty_anthropic_native_enabled", False):
            from .prompt_cache import is_claude_endpoint
            if is_claude_endpoint(base_url, model):
                from .anthropic_native_client import post_messages_native
                from .prompt_cache import adapt_assistant_prefill_for_strict_user_end
                # 主人 2026-05-28 C18: caller 不标 cache_control, 全由 post_messages_native
                # 内部 _apply_anthropic_cache_breakpoints 单一 owner 处理 (vscode 公式).
                # sweep 也在 post_messages_native 内部做, 这里只做 prefill adapter.
                prepared_messages = adapt_assistant_prefill_for_strict_user_end(messages)
                data = await post_messages_native(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=prepared_messages,
                    max_tokens=max_tokens or 4096,
                    temperature=temperature,
                    timeout=float(timeout),
                    enable_compaction=bool(getattr(config, "catty_compaction_enabled", False)),
                    compaction_trigger_tokens=int(getattr(config, "catty_compaction_trigger_tokens", 150_000)),
                    metadata_user_id=_scope_to_metadata_user_id(get_current_scope_key()),
                )
                return _extract_content(data)
        return await _post_chat_completion(
            base_url=base_url, api_key=api_key, model=model,
            messages=messages, timeout=timeout, proxy=config.catty_http_proxy,
            temperature=temperature, max_tokens=max_tokens,
            extra_headers=extra_headers, extra_body=extra_body,
            enable_cache=enable_cache, cache_depth=cache_depth,
        )
    except (OpenAICompatibleError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        if not _fallback_is_configured(config):
            raise
        _logger.warning(
            "%s cloud call failed (%s); falling back to %s",
            label or "primary",
            exc.__class__.__name__,
            config.catty_ai_fallback_model,
        )
        return await _post_fallback_chat(config, messages)
    except Exception as exc:  # noqa: BLE001
        # native SDK 抛的 anthropic.* 异常不属于上面捕获范围; 让它也走 fallback
        if not _fallback_is_configured(config):
            raise
        _logger.warning(
            "%s cloud call failed (%s); falling back to %s",
            label or "primary",
            exc.__class__.__name__,
            config.catty_ai_fallback_model,
        )
        return await _post_fallback_chat(config, messages)


async def chat_completion_codex_instant(
    config: Config,
    messages: list[ChatMessage],
    *,
    max_tokens: int = 800,
    model_override: str = "",
) -> str:
    """通用 5.3-codex 短回复路径 — placeholder / 签到 caption / NSFW deep 共用。
    model_override 优先, 否则 catty_nsfw_spark_model, 兜底 catty_filter_model.
    走 catty_filter_base_url + catty_filter_api_key (跟 spark 同 host) 但 model 单独配,
    跟 _filter_completion 的 catty_filter_model (mood classifier / spark 等) 解耦.
    主人原话:placeholder / 签到 caption / NSFW deep 三处都不要走 spark, 统一走 codex.
    """
    nsfw_model = (
        (model_override or "").strip()
        or (config.catty_codex_instant_model or "").strip()
        or (config.catty_nsfw_spark_model or "").strip()
        or config.catty_filter_model
    )
    # nsfw_spark 独立 endpoint (主人 2026-05-27 选项 2): 配上就走独立, 空时走 filter 同站
    base_url = (
        (config.catty_nsfw_spark_base_url or "").strip()
        or config.catty_filter_base_url
        or config.catty_audit_ai_base_url
        or config.catty_openai_base_url
    )
    api_key = (
        (config.catty_nsfw_spark_api_key or "").strip()
        or config.catty_filter_api_key
        or config.catty_audit_ai_api_key
        or config.catty_openai_api_key
    )
    return await _post_with_fallback(
        config,
        base_url=base_url,
        api_key=api_key,
        model=nsfw_model,
        messages=messages,
        timeout=config.catty_request_timeout,
        temperature=config.catty_temperature,
        max_tokens=max_tokens,
        extra_headers=config.catty_filter_extra_headers or config.catty_openai_extra_headers,
        extra_body=config.catty_filter_extra_body or config.catty_openai_extra_body,
        enable_cache=bool(getattr(config, "catty_prompt_cache_enabled", False)),
        cache_depth=int(getattr(config, "catty_prompt_cache_depth", 2) or 2),
        label=f"codex_instant({nsfw_model})",
    )


async def _filter_completion(config: Config, messages: list[ChatMessage], *, fallback_max_tokens: int = 64) -> str:
    use_filter_route = bool(config.catty_filter_model.strip())
    base_url = (
        (config.catty_filter_base_url if use_filter_route else "")
        or config.catty_audit_ai_base_url
        or config.catty_openai_base_url
    )
    api_key = (
        (config.catty_filter_api_key if use_filter_route else "")
        or config.catty_audit_ai_api_key
        or config.catty_openai_api_key
    )
    model = config.catty_filter_model if use_filter_route else (config.catty_audit_ai_model or config.catty_openai_model)
    temperature = config.catty_filter_temperature if use_filter_route else config.catty_audit_ai_temperature
    max_tokens = config.catty_filter_max_tokens if use_filter_route else config.catty_audit_ai_max_tokens
    return await _post_with_fallback(
        config,
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=(
            (config.catty_filter_request_timeout if use_filter_route else None)
            or config.catty_audit_ai_request_timeout
            or config.catty_request_timeout
        ),
        temperature=temperature,
        max_tokens=max_tokens or fallback_max_tokens,
        extra_headers=(
            (config.catty_filter_extra_headers if use_filter_route else {})
            or config.catty_audit_ai_extra_headers
            or config.catty_openai_extra_headers
        ),
        extra_body=(
            (config.catty_filter_extra_body if use_filter_route else {})
            or config.catty_audit_ai_extra_body
            or config.catty_openai_extra_body
        ),
        label=f"filter({model})",
    )


async def should_reply_to_group_message(config: Config, message_text: str, *, has_image: bool = False) -> bool:
    if not config.catty_filter_enabled:
        return False
    if not (config.catty_filter_api_key or config.catty_audit_ai_api_key or config.catty_openai_api_key):
        return False

    content = message_text.strip() or ("[图片]" if has_image else "")
    if not content:
        return False
    prompt = (
        "你是QQ群AI回复过滤器，只判断普通群消息是否明显指向机器人/AI并需要机器人回复。"
        "规则：明确在问机器人、AI、猫猫、助手，或明显要求机器人帮忙/回答/看图，才回复；"
        "普通闲聊、群友互相对话、吐槽、表情包、没有指向对象的问题，一律不回复。"
        "只输出JSON：{\"reply\":true|false}，不要解释。"
    )
    user_content = f"消息：{content}\n是否有图片：{'是' if has_image else '否'}"
    reply = await _filter_completion(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}],
    )
    return _json_decision(reply, "reply")


_CATTY_MOOD_DIMS: tuple[str, ...] = (
    "happy", "excited", "annoyed", "shy", "sad", "sleepy", "sulky", "bored",
)
_CATTY_MOOD_BASE_DELTA = 18.0  # weight=1.0 时的 delta 上限


async def classify_catty_mood(config: Config, text: str) -> list[tuple[str, float]]:
    """让 spark 小模型判断一条用户消息会触发笨猫哪些情绪维度。

    返回 [(dim, delta)] 列表, delta = weight * _CATTY_MOOD_BASE_DELTA;
    走 catty_filter_* 路由(spark), 无 filter 配置则回退 audit/openai。
    LLM 失败、JSON 解析失败、无命中 → 返回 []。
    """
    if not text or not text.strip():
        return []
    if not (config.catty_filter_api_key or config.catty_audit_ai_api_key or config.catty_openai_api_key):
        return []
    prompt = (
        "你是QQ猫娘机器人『笨猫』的情绪分类器。读用户这一条消息，判断它会让笨猫产生哪些情绪反应。"
        "8 个情绪维度:\n"
        "- happy   开心(好消息/被夸/有趣/逗笑)\n"
        "- excited 兴奋(惊喜/激动/牛 b 事/突发好运)\n"
        "- annoyed 烦躁(冒犯/吐槽/无理/重复打扰)\n"
        "- shy     害羞(暧昧/亲密/告白/性暗示)\n"
        "- sad     难过(分享负面/伤心事/委屈)\n"
        "- sleepy  困倦(深夜/熬夜/晚安/疲惫)\n"
        "- sulky   生闷气(被指责/被嫌弃/赶人/羞辱)\n"
        "- bored   无聊(无意义闲话/划水/没话找话)\n\n"
        "只输出 JSON: {\"hits\":[{\"dim\":\"happy\",\"weight\":0.8}, ...]}\n"
        "- weight 在 [0.0, 1.0]，1.0 = 极强，0.5 = 中等，0.0 = 完全没\n"
        "- 只输出 weight >= 0.3 的维度，最多 3 个\n"
        "- 没有命中就 hits: []\n"
        "- 不要解释，不要多余字段"
    )
    try:
        reply = await _filter_completion(
            config,
            [{"role": "system", "content": prompt}, {"role": "user", "content": text.strip()}],
            fallback_max_tokens=120,
        )
    except Exception:  # noqa: BLE001
        return []
    parsed = _json_object(reply)
    if not isinstance(parsed, dict):
        return []
    hits = parsed.get("hits")
    if not isinstance(hits, list):
        return []
    out: list[tuple[str, float]] = []
    for item in hits:
        if not isinstance(item, dict):
            continue
        dim = item.get("dim")
        if not isinstance(dim, str) or dim not in _CATTY_MOOD_DIMS:
            continue
        try:
            w = float(item.get("weight", 0.0))
        except (TypeError, ValueError):
            continue
        if w < 0.3:
            continue
        w = min(max(w, 0.0), 1.0)
        out.append((dim, round(w * _CATTY_MOOD_BASE_DELTA, 2)))
    return out


async def summarize_scope_lore(
    config: Config,
    history_excerpt: str,
    scope_label: str = "",
) -> list[dict]:
    """从 scope 最近对话总结出值得长期记下来的 lorebook entry。

    主人 2026-05-28: 改走 chat_completion_summary (deepseek 优先), 和其它后台
    总结 (_summary_loop 4 段) 统一,不烧 opus token. deepseek-v4-flash 做 JSON
    提取够用; ai_fallback 不可用时自动回退到主 AI。
    返回 list of {"keys": [...], "content": "..."} (0-3 条), 失败 / 无输出返回 []。

    每条 entry:
    - keys: 1-3 个关键词, 以后命中 user_text substring 时触发 prompt 注入
    - content: 一句口语化的『这个群值得记的事』, 给笨猫长期记忆用
    """
    history_excerpt = (history_excerpt or "").strip()
    if not history_excerpt:
        return []
    prompt = (
        "你在帮一只 QQ 群猫娘机器人(『笨猫』)整理她对当前群聊场景的『长期记忆』。\n"
        "下面是这个 scope 最近的一段对话历史。你的任务:**从中挑出 0-3 条**值得笨猫长期记住的"
        "『这个群专属的小事』,做成 lorebook 条目。\n\n"
        "**该挑的**(请选这类):\n"
        "- 这个群特有的梗 / 黑话 / 内部段子(以后再听到能接住)\n"
        "- 重要群友的特征 / 偏好 / 称呼 (比如『张三特别喜欢被叫小张』)\n"
        "- 群规 / 风气 / 重大事件(比如『这群禁止水深夜话题』)\n"
        "- 群里反复出现的话题或关键人物\n\n"
        "**不要挑的**:\n"
        "- 一次性闲聊 / 笑话(没复现价值)\n"
        "- 个人隐私 / 敏感信息(隐私优先于记忆)\n"
        "- 笨猫自己的人设(已经写在 character_card 里了, 不需要再记)\n"
        "- 完全 trivial 的事(早安/吃饭这种)\n\n"
        f"当前 scope 标签: {scope_label or '<unknown>'}\n\n"
        "对话历史:\n"
        "------\n"
        f"{history_excerpt[-6000:]}\n"
        "------\n\n"
        "**严格输出 JSON**(不要解释、不要 markdown、不要多余字段):\n"
        "{\"entries\":[{\"keys\":[\"关键词1\",\"关键词2\"],\"content\":\"该记的事(口语化,30-100 字)\"}]}\n\n"
        "- keys 数组 1-3 个, 每个关键词 2-6 个中文字最好(以后 substring 命中触发)\n"
        "- content 30-100 字, 口语化, 描述清楚『这个群有这么个事』即可\n"
        "- 没值得挑的就 entries: []\n"
        "- 最多 3 条, 宁缺毋滥"
    )
    try:
        reply = await chat_completion_summary(
            config,
            [
                {"role": "system", "content": "你是一个对话总结器, 严格按 JSON 输出。"},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception:  # noqa: BLE001
        return []
    parsed = _json_object(reply)
    if not isinstance(parsed, dict):
        return []
    raw_entries = parsed.get("entries")
    if not isinstance(raw_entries, list):
        return []
    out: list[dict] = []
    for item in raw_entries[:3]:  # 最多 3 条
        if not isinstance(item, dict):
            continue
        raw_keys = item.get("keys")
        content = item.get("content")
        if not isinstance(raw_keys, list) or not isinstance(content, str):
            continue
        keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        content = content.strip()
        if not keys or not content:
            continue
        out.append({"keys": keys[:3], "content": content[:300]})  # content 硬上限 300 字
    return out


async def should_request_reply_split(config: Config, user_content: str, *, min_chars: int) -> bool:
    if not config.catty_filter_enabled:
        return False
    if not (config.catty_filter_api_key or config.catty_audit_ai_api_key or config.catty_openai_api_key):
        return False

    prompt = (
        "你是QQ回复分段判断器，只判断本轮回复是否值得允许主AI按语义拆成多条消息。"
        f"只有当用户问题大概率需要不少于约{min_chars}个中文字符、且拆分会更像自然聊天时，返回true；"
        "短答、斗嘴、普通闲聊、简单问候、只需一句话回答时返回false。"
        "只输出JSON：{\"split\":true|false}，不要解释。"
    )
    reply = await _filter_completion(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": user_content.strip()}],
    )
    return _json_decision(reply, "split")


async def assess_user_anger(config: Config, message_text: str, *, current_anger: int, has_image: bool = False) -> dict[str, Any]:
    if not config.catty_filter_enabled or not config.catty_filter_anger_enabled:
        return {"useless": False, "anger_delta": -5, "reason": ""}
    if not (config.catty_filter_api_key or config.catty_audit_ai_api_key or config.catty_openai_api_key):
        return {"useless": False, "anger_delta": 0, "reason": ""}

    content = message_text.strip() or ("[图片]" if has_image else "")
    prompt = (
        "你是QQ群机器人耐心条评估器，判断用户这条发给机器人的消息是否无用、复读、刷屏、纠缠或故意消耗机器人。"
        "结合当前怒气值给出本条对怒气的增减：有实质问题/正常交流应降低怒气；复读、无意义短句、反复挑衅、只发无关表情应增加怒气。"
        "anger_delta 范围 -20 到 40；useless 为 true 表示这条确实无用或复读。"
        "只输出JSON：{\"useless\":true|false,\"anger_delta\":整数,\"reason\":\"<=30字\"}，不要解释。"
    )
    user_content = f"当前怒气值：{max(min(current_anger, 100), 0)}/100\n消息：{content}\n是否有图片：{'是' if has_image else '否'}"
    reply = await _filter_completion(
        config,
        [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}],
        fallback_max_tokens=96,
    )
    parsed = _json_object(reply) or {}
    useless = parsed.get("useless")
    if isinstance(useless, str):
        useless_value = useless.strip().lower() in {"true", "yes", "1", "无用", "复读"}
    else:
        useless_value = bool(useless)
    try:
        anger_delta = int(parsed.get("anger_delta", 0))
    except (TypeError, ValueError):
        anger_delta = 0
    reason = str(parsed.get("reason") or "").strip()
    return {
        "useless": useless_value,
        "anger_delta": max(min(anger_delta, 40), -20),
        "reason": reason,
    }


async def describe_images(config: Config, image_urls: list[str], context: str) -> str:
    if not image_urls:
        return ""

    base_url = config.catty_vision_base_url or config.catty_openai_base_url
    api_key = config.catty_vision_api_key or config.catty_openai_api_key
    model = config.catty_vision_model or config.catty_openai_model
    prompt = config.catty_vision_prompt.strip() or "请识别图片内容，提取和聊天回复相关的信息。"
    text = f"{prompt}\n\n聊天上下文：\n{context}".strip()
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]

    for url in image_urls:
        prepared_url = await _vision_image_url(config, url)
        content.append({"type": "image_url", "image_url": {"url": prepared_url}})

    return await _post_chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": content}],
        timeout=config.catty_vision_request_timeout or config.catty_request_timeout,
        proxy=config.catty_http_proxy,
        temperature=config.catty_vision_temperature,
        max_tokens=config.catty_vision_max_tokens,
        extra_headers=config.catty_vision_extra_headers or config.catty_openai_extra_headers,
        extra_body=config.catty_vision_extra_body,
    )


async def analyze_images_for_reply(config: Config, image_urls: list[str], context: str) -> dict[str, Any]:
    if not image_urls:
        return {}

    prompt = (
        "请识别图片内容并只输出 JSON。字段："
        "summary 字符串，描述图片/表情内容；"
        "interest 0-100 整数，表示这张图作为聊天表情或回复素材的有趣程度；"
        "emotion_tags 字符串数组，提取情绪/语气标签，如 开心、震惊、无语、害羞、嘲笑、疑惑；"
        "expression 字符串，说明它适合表达什么；"
        "emoji_query 字符串，给主 AI 匹配本地表情库用；"
        "save_as_emoji 布尔值，只有非常适合作为表情复用时为 true。"
        "不要输出 Markdown，不要解释。"
    )
    text = f"{prompt}\n\n聊天上下文：\n{context}".strip()
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for url in image_urls:
        prepared_url = await _vision_image_url(config, url)
        content.append({"type": "image_url", "image_url": {"url": prepared_url}})

    reply = await _post_chat_completion(
        base_url=config.catty_vision_base_url or config.catty_openai_base_url,
        api_key=config.catty_vision_api_key or config.catty_openai_api_key,
        model=config.catty_vision_model or config.catty_openai_model,
        messages=[{"role": "user", "content": content}],
        timeout=config.catty_vision_request_timeout or config.catty_request_timeout,
        proxy=config.catty_http_proxy,
        temperature=config.catty_vision_temperature,
        max_tokens=config.catty_vision_max_tokens,
        extra_headers=config.catty_vision_extra_headers or config.catty_openai_extra_headers,
        extra_body=config.catty_vision_extra_body,
    )
    return _image_analysis_from_reply(reply)
