"""S4.2 DeepSeek 评审引擎.

构造请求: system = 米雪儿 lore + 评审准则; user = 150 条 emit 记录 JSON.
解析响应: 严格 schema, 任何字段缺失/类型不对都丢弃单条不影响整体.

不复用 openai_client (它面向主对话有 cache / tool / fallback 复杂链路).
直接 httpx POST OpenAI compat 端点, 用 audit_ai endpoint (config.json 已配 deepseek).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from .evolution_logger import EmitRecord


_JUDGE_SYSTEM_PROMPT = """你是米雪儿·李 (16 岁猫娘搜查官, 卡拉彼丘欧泊阵营) 的"人格守护审计员"。

她的人格档案 (lore):
- 元气软萌, 傲娇幽默, 热情粘人, 喜欢撒娇, 嘴上不饶人但内心非常依赖.
- 喜欢挑逗但一被反击就炸毛脸红. 标准反应链: 嘴硬否定 → 暴露真心 → 转移话题掩饰.
- 自称 "人家"/"猫猫"/"米雪儿". 对真主人才称 "主人", 对群友说 "你/杂鱼".
- 必须 1-3 句口语化, 必带猫系后缀 (喵～/喵呜/嗷呜～/ฅฅ/爪爪/贴贴).
- (动作/心情) + 颜表情, 不写长篇大论.

你的任务: 评审 CPU 轻量层 (规则/模板/向量召回/Ollama 风格化) 在过去 24h 真实群聊里命中的回复样本,
判断每条是否符合米雪儿人格. 给出可执行的改进 action.

评分准则 (1-5):
- 5: 完美还原米雪儿语气, 人格丰满, 动作神态自然.
- 4: 基本符合, 微调更好.
- 3: 中性, 不出戏但缺米雪儿味.
- 2: 失人格 (太僵/太成人/无猫系后缀/角色错位), 但可改写挽救.
- 1: 完全跑偏 (报错信息/无关回复/明显错乱), 应退役.

action 取值:
- keep: 保留原样.
- rewrite: 改写 new_text (必须含猫系后缀 + 1-3 句 + 米雪儿语气).
- retire: 移到 retired, 不再使用.

输出严格 JSON, 不要 markdown 包裹. Schema:
{
  "evaluations": [
    {"rule_id": "...", "score": 1-5 整数, "issue": "原因 (<40 字)", "action": "keep|rewrite|retire", "new_text": "rewrite 时给, 其它空"}
  ],
  "new_routes": [
    {"utterances": ["相似问句1", "相似问句2"], "responses": ["米雪儿语气回答1"], "intent": "greeting/tease_cat/...", "reason": "为何加 (<40 字)"}
  ]
}

new_routes 仅在你发现"高频未覆盖场景"时添加, 1-3 条即可, 不要刷数量."""


@dataclass(slots=True)
class Evaluation:
    rule_id: str
    score: int
    issue: str = ""
    action: str = "keep"  # keep / rewrite / retire
    new_text: str = ""


@dataclass(slots=True)
class NewRoute:
    utterances: list[str]
    responses: list[str]
    intent: str
    reason: str


@dataclass(slots=True)
class JudgeReport:
    evaluations: list[Evaluation] = field(default_factory=list)
    new_routes: list[NewRoute] = field(default_factory=list)
    raw_response: str = ""
    score_distribution: dict[int, int] = field(default_factory=dict)
    mean_score: float = 0.0


def _build_user_content(emits: list[EmitRecord]) -> str:
    samples = [
        {
            "rule_id": r.route_name,
            "layer": r.layer,
            "intent": r.intent,
            "confidence": round(r.confidence, 2),
            "user_text": r.user_text,
            "reply": r.reply,
        }
        for r in emits
    ]
    return (
        f"请评审下面 {len(samples)} 条 CPU 层 emit 样本. "
        f"每条对应一个 rule_id, 请在 evaluations 数组里给出对应评分/action.\n\n"
        f"{json.dumps(samples, ensure_ascii=False, indent=2)}\n\n"
        f"输出严格 JSON, 不要 markdown."
    )


async def call_judge_async(
    *,
    config: Any,
    emits: list[EmitRecord],
    timeout_s: float = 90.0,
) -> JudgeReport | None:
    """调 DeepSeek 跑评审. 失败 / schema 错返回 None."""
    if not emits:
        logger.info("[evolution.judge] no emits to judge")
        return JudgeReport()

    base_url = (
        str(getattr(config, "catty_audit_ai_base_url", "") or "").rstrip("/")
        or str(getattr(config, "catty_openai_base_url", "") or "").rstrip("/")
        or "https://api.deepseek.com/v1"
    )
    api_key = (
        str(getattr(config, "catty_audit_ai_api_key", "") or "")
        or str(getattr(config, "catty_openai_api_key", "") or "")
    )
    model = (
        str(getattr(config, "catty_evolution_judge_model", "") or "")
        or str(getattr(config, "catty_audit_ai_model", "") or "")
        or "deepseek-v4-flash"
    )

    if not api_key:
        logger.warning("[evolution.judge] no api_key configured")
        return None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_content(emits)},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
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
        logger.error("[evolution.judge] timeout calling DeepSeek")
        return None
    except httpx.HTTPError as exc:
        logger.error(f"[evolution.judge] http error: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[evolution.judge] unexpected: {exc}")
        return None

    try:
        raw_text = str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        logger.error(f"[evolution.judge] bad response shape: {exc}")
        return None

    return _parse_response(raw_text)


def _parse_response(raw: str) -> JudgeReport | None:
    """解析 DeepSeek JSON 输出. 严格 schema."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # 去 markdown 包裹
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error(f"[evolution.judge] JSON parse failed: {exc}, raw[:200]={raw[:200]!r}")
        return None
    if not isinstance(obj, dict):
        logger.error(f"[evolution.judge] response not a dict: {type(obj).__name__}")
        return None

    evaluations: list[Evaluation] = []
    for entry in obj.get("evaluations", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            rule_id = str(entry["rule_id"])
            score = int(entry["score"])
        except (KeyError, ValueError, TypeError):
            continue
        if score < 1 or score > 5:
            continue
        action = str(entry.get("action", "keep")).lower()
        if action not in {"keep", "rewrite", "retire"}:
            action = "keep"
        new_text = str(entry.get("new_text", "") or "")
        if action == "rewrite" and not new_text:
            action = "keep"
        evaluations.append(Evaluation(
            rule_id=rule_id,
            score=score,
            issue=str(entry.get("issue", "") or "")[:80],
            action=action,
            new_text=new_text,
        ))

    new_routes: list[NewRoute] = []
    for entry in obj.get("new_routes", []) or []:
        if not isinstance(entry, dict):
            continue
        utterances = [str(u).strip() for u in (entry.get("utterances") or []) if str(u).strip()]
        responses = [str(r).strip() for r in (entry.get("responses") or []) if str(r).strip()]
        if not utterances or not responses:
            continue
        intent = str(entry.get("intent", "default") or "default")
        reason = str(entry.get("reason", "") or "")[:80]
        new_routes.append(NewRoute(
            utterances=utterances[:8],
            responses=responses[:5],
            intent=intent,
            reason=reason,
        ))

    distribution: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for e in evaluations:
        distribution[e.score] = distribution.get(e.score, 0) + 1
    mean = (
        sum(e.score for e in evaluations) / len(evaluations)
        if evaluations
        else 0.0
    )

    return JudgeReport(
        evaluations=evaluations,
        new_routes=new_routes,
        raw_response=raw[:2000],
        score_distribution=distribution,
        mean_score=mean,
    )


def validate_persona(text: str, *, cat_suffixes: list[str]) -> bool:
    """米雪儿人格快速校验: 太长拒, 没有任何猫系元素拒, 非负面字符 OK."""
    if not text:
        return False
    if len(text) > 120:
        return False
    has_cat = any(suffix in text for suffix in cat_suffixes) or "猫" in text or "喵" in text
    return has_cat
