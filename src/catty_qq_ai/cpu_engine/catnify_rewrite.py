"""S5 L4 catnify_rewrite (2026-05-29 主人方案).

全量改写: 对任意 L1/L2/L3 命中候选, 用本地 Qwen3-4B 改写成笨猫体质 (米雪儿语气);
同时让本地 LLM 自决是否输出 ``<DEEPSEEK reason="...">`` 把消息透传到 L5 主链路.

跟旧 ``ollama_stylize.stylize_candidate`` 的差异:
- 旧: 只在 L2/L3 中信心 (0.70-0.82) 区间触发, 输出纯改写文本, 失败 → None 透传
- 新: 全量触发 (主人决策"信任 Qwen3"), 输出可能含 ``<DEEPSEEK>`` 标记 → 透传 L5
- 旧默认 qwen2.5:7b 800ms / 新默认 qwen3:4b 3000ms (4000 token prefill 需要更长)

返回 ``CatnifyResult{text, deepseek_reason, latency_ms}``:
- ``deepseek_reason`` 非空 → 调用方应走 L5 + 扣积分链
- ``text`` 是去掉 ``<DEEPSEEK>`` 后的剩余文本 (可能空); ``deepseek_reason`` 空时是改写结果
- 整体失败/超时 → 返回 None, 调用方按 fallback 策略处理 (主人决策: 用原候选)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger


_MICHEL_PERSONA_HINT = (
    "你是米雪儿·李, 16 岁猫娘搜查官 (卡拉彼丘欧泊阵营). "
    "元气软萌傲娇, 嘴硬心软, 黏人调皮. "
    "回复必须 1-3 句, 必带猫系后缀 (喵～/喵呜/嗷呜～/ฅฅ/爪爪/贴贴), "
    "先傲娇否定再撒娇, 用 (动作/心情) + 颜表情. "
    "对真主人才说『主人』, 群友说『你/杂鱼』. "
    "自称『人家』『猫猫』『笨猫』 (绝不裸说『喵～』丢自称)."
)

_DEEPSEEK_RULE_HINT = (
    "**透传规则**: 如果用户问题不该用 CPU 候选回复, 而是需要主 AI (DeepSeek) 深度处理, "
    "**只**输出标记, 不要任何其他内容:\n"
    "  <DEEPSEEK reason=\"nsfw\">       NSFW / 强情感 / 表白 / 撒娇 / 黏人\n"
    "  <DEEPSEEK reason=\"academic\">    学术 / 论文 / 原理 / 推导 / 证明 / 代码深度\n"
    "  <DEEPSEEK reason=\"search\">      实时信息 / 新闻 / 价格 / 天气 / 排行 / 最新\n"
    "  <DEEPSEEK reason=\"long_chain\"> 多步推理 / 复杂分析 / 长解释\n"
    "其他场景请改写候选成笨猫语气, 不要输出 DEEPSEEK 标记."
)


# 匹配 <DEEPSEEK reason="..."> 或 <deepseek reason="..."> (大小写、引号容错)
_DEEPSEEK_RE = re.compile(
    r"<\s*deepseek\s+reason\s*=\s*[\"']?([a-z_]+)[\"']?\s*/?\s*>",
    re.IGNORECASE,
)
_KNOWN_REASONS = {"nsfw", "academic", "search", "long_chain"}


@dataclass(slots=True)
class CatnifyResult:
    """L4 catnify 改写结果.

    - text: 去掉 DEEPSEEK 标记后的剩余文本 (改写正文); deepseek 透传时通常为空
    - deepseek_reason: 非空 → 调用方走 L5 + 扣积分; 空 → 用 text 直接发
    - latency_ms: 本次 LLM 调用耗时
    """

    text: str
    deepseek_reason: str
    latency_ms: float


def _parse_deepseek_marker(raw: str) -> tuple[str, str]:
    """从 LLM 输出抓 <DEEPSEEK reason="..."> 标记.

    返回 (剩余文本, reason). 没标记时返回 (raw, ""), reason 不在白名单时归 "long_chain".
    """
    if not raw:
        return "", ""
    m = _DEEPSEEK_RE.search(raw)
    if not m:
        return raw.strip(), ""
    reason = m.group(1).lower()
    if reason not in _KNOWN_REASONS:
        reason = "long_chain"
    remaining = (raw[: m.start()] + raw[m.end() :]).strip()
    return remaining, reason


def _build_user_prompt(
    *,
    user_text: str,
    candidate: str,
    user_addr: str,
    scope_type: str,
    group_name: str = "",
    history: list[tuple[str, str]] | None = None,
) -> str:
    """构造 user prompt. 主人决策: 输入总长 ≤ 4000 token, 留 1500 给 history."""
    parts: list[str] = []
    parts.append(f"[场景]: scope_type={scope_type}, user_addr={user_addr}")
    if group_name:
        parts.append(f"[群名]: {group_name}")
    if history:
        # 主人决策 history_turns=4, 超长裁旧的. 简单按字符 1500 上限裁.
        budget = 1500
        kept: list[str] = []
        for who, txt in reversed(history):
            line = f"  {who}: {txt}"
            if budget - len(line) < 0:
                break
            kept.append(line)
            budget -= len(line)
        if kept:
            parts.append("[近期对话]:")
            parts.extend(reversed(kept))
    parts.append(f"[用户消息]: {user_text}")
    parts.append(f"[CPU 候选]: {candidate}")
    parts.append("请按规则输出改写后的笨猫体质回复, 或仅输出 <DEEPSEEK reason=\"...\"> 标记.")
    return "\n".join(parts)


async def catnify_rewrite(
    *,
    config: Any,
    candidate: str,
    user_text: str,
    user_addr: str = "你",
    scope_type: str = "private",
    group_name: str = "",
    history: list[tuple[str, str]] | None = None,
    persona_hint: str = "",
    timeout_ms: int | None = None,
) -> CatnifyResult | None:
    """S5 L4 全量改写 + 自决透传 DeepSeek.

    返回 CatnifyResult 或 None (Ollama 不可用/超时/空响应 → 调用方走 fallback).
    """
    # 主人原则 (2026-05-29): 配置唯一来源是 config.json (catty_config_loader → env → pydantic),
    # 这里不再硬编码 fallback 字面值; pydantic 在 config.py 已设默认.
    base_url = str(getattr(config, "catty_ai_fallback_base_url", "") or "").rstrip("/")
    api_key = str(getattr(config, "catty_ai_fallback_api_key", "") or "")
    model = str(
        getattr(config, "catty_cpu_engine_l4_catnify_model", "")
        or getattr(config, "catty_cpu_engine_l4_ollama_model", "")
    )
    if not base_url or not model:
        logger.warning(
            f"[cpu_engine.L4_catnify] config missing: base_url={base_url!r} model={model!r} -> skip"
        )
        return None
    timeout_ms = (
        timeout_ms
        if timeout_ms is not None
        else int(getattr(config, "catty_cpu_engine_l4_catnify_timeout_ms", 0) or 3000)
    )
    temperature = float(getattr(config, "catty_cpu_engine_l4_catnify_temperature", 0.7))
    max_tokens = int(getattr(config, "catty_cpu_engine_l4_catnify_max_tokens", 0) or 200)
    timeout_s = max(timeout_ms / 1000.0, 0.5)

    system_prompt = (persona_hint or _MICHEL_PERSONA_HINT) + "\n\n" + _DEEPSEEK_RULE_HINT
    user_prompt = _build_user_prompt(
        user_text=user_text,
        candidate=candidate,
        user_addr=user_addr,
        scope_type=scope_type,
        group_name=group_name,
        history=history,
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    t0 = time.monotonic()
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
        latency = (time.monotonic() - t0) * 1000.0
        logger.info(f"[cpu_engine.L4_catnify] timeout ({timeout_ms}ms latency={latency:.0f}ms)")
        return None
    except httpx.HTTPError as exc:
        logger.warning(f"[cpu_engine.L4_catnify] http error: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cpu_engine.L4_catnify] unexpected error: {exc}")
        return None

    latency = (time.monotonic() - t0) * 1000.0
    try:
        raw = str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning(f"[cpu_engine.L4_catnify] bad response shape: {exc}, data={data}")
        return None

    text, reason = _parse_deepseek_marker(raw)
    if reason:
        logger.info(
            f"[cpu_engine.L4_catnify] DEEPSEEK reason={reason} latency={latency:.0f}ms "
            f"raw_len={len(raw)} user_text[:40]={user_text[:40]!r}"
        )
        return CatnifyResult(text=text, deepseek_reason=reason, latency_ms=latency)

    if not text:
        logger.info(f"[cpu_engine.L4_catnify] empty rewrite latency={latency:.0f}ms")
        return None
    logger.info(
        f"[cpu_engine.L4_catnify] OK latency={latency:.0f}ms "
        f"in_len={len(user_text)} out_len={len(text)}"
    )
    return CatnifyResult(text=text, deepseek_reason="", latency_ms=latency)
