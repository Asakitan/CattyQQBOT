"""L4 Ollama 风格化 (S3.4).

L2/L3 拿到中等信心候选 (score 0.70-0.82) 时, 用 Ollama qwen2.5:7b 加米雪儿人格头
few-shot 重写为更自然的米雪儿语气. 失败/超时返回 None 让 router 透传到 L5.

复用 catty_ai_fallback_base_url/api_key (本地 Ollama) 但用 L4 专属 timeout (800ms).
不复用 _post_fallback_chat 完整流程 (它含 MC gate + warmup 长 timeout, 不适合 inline 重写).
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger


_MICHEL_PERSONA_HINT = (
    "你是米雪儿·李, 16 岁猫娘搜查官 (卡拉彼丘欧泊阵营). "
    "元气软萌傲娇, 嘴硬心软, 黏人调皮. "
    "回复必须 1-3 句, 必带猫系后缀 (喵～/喵呜/嗷呜～/ฅฅ/爪爪/贴贴), "
    "先傲娇否定再撒娇, 用 (动作/心情) + 颜表情. "
    "对真主人才说『主人』, 群友说『你/杂鱼』."
)


async def stylize_candidate(
    *,
    config: Any,
    candidate: str,
    user_text: str,
    persona_hint: str = "",
    timeout_ms: int | None = None,
) -> str | None:
    """用 Ollama 把候选 candidate 改写为米雪儿语气.

    返回新文本 (str) 或 None (Ollama 不可用/超时/为空).
    """
    base_url = (
        str(getattr(config, "catty_ai_fallback_base_url", "") or "").rstrip("/")
        or "http://127.0.0.1:11434/v1"
    )
    api_key = str(getattr(config, "catty_ai_fallback_api_key", "") or "") or "ollama"
    model = str(
        getattr(config, "catty_cpu_engine_l4_ollama_model", "")
        or getattr(config, "catty_ai_fallback_model", "")
        or "qwen2.5:7b"
    )
    timeout_ms = timeout_ms if timeout_ms is not None else int(
        getattr(config, "catty_cpu_engine_l4_timeout_ms", 800)
    )
    timeout_s = max(timeout_ms / 1000.0, 0.2)

    system_prompt = persona_hint or _MICHEL_PERSONA_HINT
    user_prompt = (
        f"用户消息: {user_text}\n"
        f"初稿回复: {candidate}\n\n"
        f"请用米雪儿语气改写初稿 (保持 1-3 句, 不要改变核心含义, 只调语气和人格细节)."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.65,
        "max_tokens": 180,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.info(f"[cpu_engine.L4] stylize timeout ({timeout_ms}ms), pass to L5")
        return None
    except httpx.HTTPError as exc:
        logger.warning(f"[cpu_engine.L4] stylize http error: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cpu_engine.L4] stylize unexpected error: {exc}")
        return None

    try:
        text = str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning(f"[cpu_engine.L4] stylize bad response shape: {exc}, data={data}")
        return None

    if not text:
        return None
    return text
