from __future__ import annotations

import base64
from io import BytesIO
import json
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageSequence

from .config import Config


ChatMessage = dict[str, Any]


class OpenAICompatibleError(Exception):
    def __init__(self, public_message: str, detail: str | None = None) -> None:
        super().__init__(detail or public_message)
        self.public_message = public_message


def _chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


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


def _client_kwargs(timeout: float, proxy: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": True,
    }
    if proxy.strip():
        kwargs["proxy"] = proxy.strip()
    return kwargs


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
        raise OpenAICompatibleError(f"AI 接口 HTTP {response.status_code}。", detail)

    try:
        data = response.json()
    except ValueError as exc:
        raise OpenAICompatibleError("AI 返回的不是 JSON。", response.text[:500]) from exc

    return _extract_content(data)


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


async def _vision_image_url(config: Config, url: str) -> str:
    timeout = config.catty_vision_request_timeout or config.catty_request_timeout
    async with httpx.AsyncClient(**_client_kwargs(timeout, config.catty_http_proxy)) as client:
        response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if not _needs_first_frame(url, content_type):
        data_url = _first_frame_data_url(response.content)
        return data_url or url
    return _first_frame_data_url(response.content) or url


async def download_binary(config: Config, url: str, *, timeout: float | None = None) -> tuple[bytes, str]:
    request_timeout = timeout or config.catty_vision_request_timeout or config.catty_request_timeout
    async with httpx.AsyncClient(**_client_kwargs(request_timeout, config.catty_http_proxy)) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.content, response.headers.get("content-type", "")


async def chat_completion(config: Config, messages: list[ChatMessage]) -> str:
    return await _post_chat_completion(
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
    base_url = config.catty_filter_base_url or config.catty_openai_base_url
    api_key = config.catty_filter_api_key or config.catty_openai_api_key
    model = config.catty_filter_model or config.catty_openai_model
    return await _post_chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        timeout=config.catty_filter_request_timeout or config.catty_request_timeout,
        proxy=config.catty_http_proxy,
        temperature=config.catty_filter_temperature,
        max_tokens=config.catty_filter_max_tokens or fallback_max_tokens,
        extra_headers=config.catty_filter_extra_headers or config.catty_openai_extra_headers,
        extra_body=config.catty_filter_extra_body,
    )


async def should_reply_to_group_message(config: Config, message_text: str, *, has_image: bool = False) -> bool:
    if not config.catty_filter_enabled:
        return False
    if not (config.catty_filter_api_key or config.catty_openai_api_key):
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
    if not (config.catty_filter_api_key or config.catty_openai_api_key):
        return False

    prompt = (
        "你是QQ回复分段判断器，只判断本轮回复是否值得允许主AI按语义拆成两条消息。"
        f"只有当用户问题大概率需要不少于约{min_chars}个中文字符、且拆成两条会更像自然聊天时，返回true；"
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
    if not (config.catty_filter_api_key or config.catty_openai_api_key):
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
