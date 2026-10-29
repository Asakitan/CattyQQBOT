from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from .openai_client import _extract_content, _post_chat_completion_raw, aux_route
from .persona_evolution_budget import BudgetLedger
from .persona_evolution_store import PersonaEvolutionStore, build_evolution_messages, parse_evolution_response

_MAX_OUTPUT_TOKENS = 4096
# build_evolution_messages 最多 40 条 × 600 字 + persona 正文；32K 足够覆盖真实输入，
# 不再拿整条线路的上下文窗口做每次预留（旧口径 1M/次 会白吃日预算）。
_MAX_INPUT_TOKENS = 32_000


def persona_base_text(persona: Any) -> str:
    from .catty_core_persona import CATTY_CORE_PERSONA

    fields = {
        "name": str(getattr(persona, "name", "") or ""),
        "char_name": str(getattr(persona, "char_name", "") or ""),
        "core_persona": str(getattr(persona, "core_persona", "") or CATTY_CORE_PERSONA),
        "owner_concept": bool(getattr(persona, "owner_concept", True)),
        "definition_revision": getattr(persona, "definition_revision", None),
    }
    for field in ("chat_rhythm", "first_mes", "mes_example", "style_examples",
                  "disambiguation_examples", "persona_reminder_text"):
        fields[field] = str(getattr(persona, field, "") or "")
    fields["conversation_prompts"] = list(getattr(persona, "conversation_prompts", ()) or ())
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def scoped_examples(items: list[Any], identity: str, *, now: float | None = None) -> list[dict[str, str]]:
    cutoff = (time.monotonic() if now is None else now) - 86400.0
    recent = [item for item in items if float(getattr(item, "created_at", 0.0)) >= cutoff]
    for index in range(len(recent) - 1, -1, -1):
        item = recent[index]
        if getattr(item, "is_self", False) and getattr(item, "persona_identity", "") != identity:
            recent = recent[index + 1:]
            break
    if sum(bool(getattr(item, "is_self", False)) and getattr(item, "persona_identity", "") == identity
           for item in recent) < 3:
        return []
    result: list[dict[str, str]] = []
    for item in recent[-40:]:
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        if getattr(item, "is_self", False):
            if getattr(item, "persona_identity", "") != identity:
                continue
            role = "assistant"
        elif not getattr(item, "is_bot", False):
            role = "user"
        else:
            continue
        result.append({"role": role, "content": text[:600]})
    return result


def _reported_total(data: dict[str, Any]) -> int | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if type(total) is int and total > 0:
        if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
            return max(total, prompt + completion)
        return total
    if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
        value = prompt + completion
        return value if value > 0 else None
    return None


async def evolve_once(
    *,
    config: Any,
    name: str,
    base_text: str,
    examples: list[dict[str, str]],
    store: PersonaEvolutionStore,
    ledger: BudgetLedger,
    still_current: Callable[[], bool],
    commit: Callable[[str | None, str], Awaitable[bool]],
) -> str:
    if not bool(getattr(config, "catty_persona_evolution_enabled", True)):
        return "disabled"
    # 语气进化属于后台杂活：走次要 AI 线路 (audit_ai/deepseek)，不占主模型。
    route = aux_route(config)
    if not route.base_url or not route.api_key or not route.model:
        return "route_unavailable"
    context_limit = int(route.context_tokens or 0)
    if context_limit <= 0:
        return "context_limit_unavailable"
    current, revision = await asyncio.to_thread(store.load, name, base_text)
    messages = build_evolution_messages(name, base_text, current, examples)
    reserved = min(context_limit, _MAX_INPUT_TOKENS) + _MAX_OUTPUT_TOKENS
    reservation = await asyncio.to_thread(ledger.reserve, reserved)
    if reservation is None:
        return "budget_exhausted"
    if not still_current():
        await asyncio.to_thread(ledger.settle, reservation, 0)
        return "stale"
    configured_body = route.extra_body
    extra_body = {
        key: configured_body[key]
        for key in ("thinking", "reasoning_effort")
        if isinstance(configured_body, dict) and key in configured_body
    }
    data = await _post_chat_completion_raw(
        base_url=route.base_url,
        api_key=route.api_key,
        model=route.model,
        messages=messages,
        timeout=float(route.timeout or getattr(config, "catty_request_timeout", 60.0) or 60.0),
        proxy=str(getattr(config, "catty_http_proxy", "") or ""),
        temperature=route.temperature,
        max_tokens=_MAX_OUTPUT_TOKENS,
        extra_headers=dict(route.extra_headers or {}),
        extra_body=extra_body,
        tools=None,
        stream=False,
        request_route="persona_evolution",
        max_attempts=1,
    )
    used = _reported_total(data)
    if used is None or not await asyncio.to_thread(ledger.settle, reservation, used):
        return "usage_unconfirmed"
    try:
        style = parse_evolution_response(_extract_content(data))
    except (TypeError, ValueError):
        return "invalid_response"
    if style == current:
        return "unchanged"
    return "updated" if await commit(revision, style) else "stale"
