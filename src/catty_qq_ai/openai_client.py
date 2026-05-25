from __future__ import annotations

import asyncio
import base64
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
) -> str:
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
    )
    return _extract_content(data)


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
) -> dict[str, Any]:
    """返回完整 response JSON,供 function calling 链路读 tool_calls。"""
    if not base_url.strip():
        raise OpenAICompatibleError("AI 接口地址为空。")
    if not model.strip():
        raise OpenAICompatibleError("AI 模型名为空。")

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        **extra_headers,
    }
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
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    async with httpx.AsyncClient(**_client_kwargs(timeout, proxy)) as client:
        response = await client.post(_chat_completions_url(base_url), headers=headers, json=payload)

    if response.status_code >= 400:
        detail = response.text[:500]
        raise OpenAICompatibleError(_catty_http_status_message("AI 接口", response.status_code), detail)

    try:
        return response.json()
    except ValueError as exc:
        raise OpenAICompatibleError("AI 返回的不是 JSON。", response.text[:500]) from exc


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
    # 代码层面硬关闭本地 AI fallback：忽略 ai_fallback.* 配置，永远不路由到本地模型。
    # 云端调用失败时直接把云端的异常抛给上层，不再尝试本地兜底。
    del config
    return False


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
) -> str:
    """OpenAI function calling 主回复循环。

    tools/tool_executor 任一为空 → 退化到 plain chat_completion(完整 fallback 链)。
    tool 调度过程中云端抛错也直接降级到 chat_completion(保留本地 7B 兜底)。
    tool_executor 签名:async (name: str, arguments_json: str) -> dict。
    """
    if not tools or tool_executor is None:
        _logger.info("tool_chat: tools/executor empty → fallback to plain chat_completion")
        return await chat_completion(config, messages)
    if not getattr(config, "catty_tools_enabled", True):
        _logger.info("tool_chat: catty_tools_enabled=False → fallback to plain chat_completion")
        return await chat_completion(config, messages)
    if _cloud_is_unhealthy():
        # 云端冷却期不带 tools 试,直接走 fallback 链。
        _logger.info("tool_chat: cloud unhealthy → fallback to plain chat_completion (no tools)")
        return await chat_completion(config, messages)
    _logger.info("tool_chat: starting with %d tools available", len(tools))

    history: list[ChatMessage] = list(messages)
    for round_idx in range(max(1, max_rounds)):
        try:
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
        for (call_id, name, _args_json), result in zip(executed, tool_results):
            if isinstance(result, BaseException):
                payload = {"error": f"{name} 抛异常: {result.__class__.__name__}: {result}"}
                _logger.warning("Tool %s raised: %s", name, result)
            elif not isinstance(result, dict):
                payload = {"value": result}
            else:
                payload = result
            try:
                content_str = json.dumps(payload, ensure_ascii=False)
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

    # 正常走云。
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


async def chat_completion_instant(config: Config, messages: list[ChatMessage], *, fallback_max_tokens: int = 80) -> str:
    """走 catty_filter_* 配置(spark 这种小快模型)的瞬时完成。

    用途:placeholder 等候语、签到/积分卡 caption 这种 1-2 句猫娘短话——
    主回复模型 (gpt-5.5 等) 响应慢,这里需要『立刻』出文案不能等。
    复用 _filter_completion 的路由逻辑;无 spark 配置时自动回退到 audit/openai。
    """
    return await _filter_completion(config, messages, fallback_max_tokens=fallback_max_tokens)


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
    return await _post_chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=(
            (config.catty_filter_request_timeout if use_filter_route else None)
            or config.catty_audit_ai_request_timeout
            or config.catty_request_timeout
        ),
        proxy=config.catty_http_proxy,
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
