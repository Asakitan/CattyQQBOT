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


ChatMessage = dict[str, Any]


class OpenAICompatibleError(Exception):
    def __init__(self, public_message: str, detail: str | None = None) -> None:
        super().__init__(detail or public_message)
        self.public_message = public_message


class MCBusyError(OpenAICompatibleError):
    """MC server has players online and local fallback is gated off."""


def _catty_http_status_message(service_name: str, status_code: int) -> str:
    if status_code == 503:
        return f"喵呜，{service_name}那边暂时忙到炸毛了（503），主人稍后再戳一下猫猫吧。"
    if status_code == 429:
        return f"喵呜，{service_name}被戳太快啦（429），主人让猫猫缓一小会儿再试。"
    if status_code in {401, 403}:
        return f"喵呜，{service_name}不让猫猫进去（{status_code}），主人检查一下 API Key 或权限。"
    if 500 <= status_code < 600:
        return f"喵呜，{service_name}那边临时炸毛了（{status_code}），主人稍后再试一下。"
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


def _extract_content(data: dict[str, Any]) -> str:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenAICompatibleError("AI 返回格式不符合 Chat Completions。", repr(data)[:500]) from exc

    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()

    text = choice.get("text")
    if isinstance(text, str):
        return text.strip()

    raise OpenAICompatibleError("AI 没有返回可读文本。", repr(data)[:500])


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
    kwargs: dict[str, Any] = {
        "timeout": timeout,
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

    async with httpx.AsyncClient(**_client_kwargs(timeout, proxy)) as client:
        response = await client.post(_chat_completions_url(base_url), headers=headers, json=payload)

    if response.status_code >= 400:
        detail = response.text[:500]
        raise OpenAICompatibleError(_catty_http_status_message("AI 接口", response.status_code), detail)

    try:
        data = response.json()
    except ValueError as exc:
        raise OpenAICompatibleError("AI 返回的不是 JSON。", response.text[:500]) from exc

    return _extract_content(data)


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
    if not bool(getattr(config, "catty_ai_fallback_enabled", False)):
        return False
    base = str(getattr(config, "catty_ai_fallback_base_url", "") or "").strip()
    model = str(getattr(config, "catty_ai_fallback_model", "") or "").strip()
    return bool(base) and bool(model)


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


async def _post_fallback_chat(config: Config, messages: list[ChatMessage]) -> str:
    await _check_mc_gate_or_raise(config)

    base_url = config.catty_ai_fallback_base_url
    api_key = config.catty_ai_fallback_api_key
    model = config.catty_ai_fallback_model
    extra_body = dict(config.catty_ai_fallback_extra_body or {})
    extra_headers = config.catty_ai_fallback_extra_headers or {}
    timeout = config.catty_ai_fallback_request_timeout or config.catty_request_timeout
    temperature = config.catty_ai_fallback_temperature
    max_tokens = config.catty_ai_fallback_max_tokens

    # 内存预算够时让 7B 留在内存复用更快。想要立刻卸载,在 config 的
    # ai_fallback.extra_body 里加 "keep_alive": 0 即可。
    if _looks_like_ollama_route(base_url, api_key, extra_body):
        return await _post_ollama_chat(
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
    return await _post_chat_completion(
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
    raw = text.strip()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                loaded = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    return loaded if isinstance(loaded, dict) else None


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
