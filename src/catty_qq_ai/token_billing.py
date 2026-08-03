"""Token 计费 (主人 2026-07-06).

新积分使用模式, 取代旧 CPU 引擎链路 (catty_credit_enabled, 已随引擎关停):
- 私聊: 每次回复按本轮真实 token 用量扣积分, 每 catty_private_tokens_per_point
  (默认 1000) token 扣 1 分, 向上取整. 余额 <=0 时拦截不调主 AI, 发撒娇文案要签到.
- 群聊: 暂不扣积分, 每人每小时 catty_group_hourly_token_quota (默认 300K) token
  额度, 超了拦截 (每小时只提示一次, 之后静默).
- 主人全豁免.
- 拦截提醒优先 ai_gate_reply 人格 AI 现写 (codex_instant 便宜通道, 在 turn 桶
  开启前调用不计费), 失败 fallback 固定文案池.

本轮 token 用量 = handle_chat 一轮内所有 AI 调用 (主回复 + tools 多轮 + 辅助
filter/audit) 的 prompt+completion 总和, 由 dashboard_state.push_cache_stats
统一累加进 contextvar 的 turn 桶 — contextvar 按 async task 隔离, 同群并发
(group_sema N=3) 不串账. prompt 口径含 cache hit 部分 (全量计, 不打折).

群聊小时桶 + turn 桶均为内存态, bot 重启清零 (用户最多多薅一小时额度, 无碍).
积分余额本身持久化在 affection_store (affection.json).
"""

from __future__ import annotations

import contextvars
import math
import random
import time
from typing import Any

from loguru import logger

# ── 本轮 turn token 累加器 (contextvar, 并发隔离) ────────────────────────────
# handle_chat 计费门放行后 begin_turn_usage() 建桶; 之后本 task 内所有 AI 调用
# 经 push_cache_stats -> add_turn_usage 累加; 锁栈退出 callback 读桶结算.
_TURN_USAGE: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "catty_turn_usage", default=None
)


def begin_turn_usage() -> dict[str, int]:
    """开一轮新的 token 桶并返回引用 (结算方直接持引用读, 不走 key 查找)."""
    bucket = {"prompt_tokens": 0, "completion_tokens": 0}
    _TURN_USAGE.set(bucket)
    return bucket


def add_turn_usage(*, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    """AI usage 上报点调用. 桶不存在 (非计费轮次/后台任务) 时静默 no-op."""
    bucket = _TURN_USAGE.get()
    if bucket is None:
        return
    bucket["prompt_tokens"] += max(int(prompt_tokens), 0)
    bucket["completion_tokens"] += max(int(completion_tokens), 0)


# ── 群聊每人每小时 token 额度 (整点桶, 内存态) ───────────────────────────────
# user_id -> [hour_epoch, used_tokens, notified]
_GROUP_HOURLY: dict[str, list] = {}


def _hour_bucket(user_id: str) -> list:
    hour = int(time.time() // 3600)
    rec = _GROUP_HOURLY.get(user_id)
    if rec is None or rec[0] != hour:
        rec = [hour, 0, False]
        _GROUP_HOURLY[user_id] = rec
    return rec


def group_quota_exceeded(user_id: str, quota: int) -> tuple[bool, bool]:
    """返回 (是否超额, 本小时是否已提示过)."""
    rec = _hour_bucket(str(user_id))
    return rec[1] >= quota, bool(rec[2])


def group_quota_mark_notified(user_id: str) -> None:
    _hour_bucket(str(user_id))[2] = True


def add_group_usage(user_id: str, tokens: int) -> int:
    """回复完成后累加本轮 token 到当前小时桶, 返回累计值."""
    rec = _hour_bucket(str(user_id))
    rec[1] += max(int(tokens), 0)
    return rec[1]


def group_quota_used(user_id: str) -> int:
    return int(_hour_bucket(str(user_id))[1])


# ── 私聊按 token 扣积分 ──────────────────────────────────────────────────────
def settle_private_tokens(
    affection_store: Any,
    config: Any,
    user_id: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> int:
    """回复完成后按真实 token 扣积分. 返回实际扣除额.

    cost = ceil(total / tokens_per_point). 余额不够扣时清零 (允许最后一次
    "透支", 下一条消息会被计费门拦截), 不欠账.
    """
    uid = str(user_id)
    if affection_store.is_owner(uid):
        return 0
    per_point = max(int(getattr(config, "catty_private_tokens_per_point", 1000)), 1)
    total = max(int(prompt_tokens), 0) + max(int(completion_tokens), 0)
    if total <= 0:
        return 0
    cost = math.ceil(total / per_point)
    try:
        result = affection_store.consume_points(uid, cost)
        if not result.get("ok"):
            # 余额不足: 扣光剩余 (清零), 本次回复已经发出去了
            balance = int(result.get("balance_before", 0))
            charged = balance
            if balance > 0:
                result = affection_store.consume_points(uid, balance)
        else:
            charged = cost
        balance_after = int(result.get("balance_after", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[token_billing] settle_private({uid}, cost={cost}) failed: {exc}")
        return 0
    logger.info(
        f"[token_billing] PRIVATE_BILL uid={uid} tokens={total} "
        f"(p={prompt_tokens} c={completion_tokens}) cost={cost} charged={charged} "
        f"balance_after={balance_after}"
    )
    try:
        from .dashboard_state import push_credit_event

        push_credit_event(
            uid,
            "token_bill",
            delta=-charged,
            balance_after=balance_after,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            scope=f"private:{uid}",
            reason=f"{total}tok/{per_point}",
        )
    except Exception:  # noqa: BLE001
        pass
    return charged


# ── 拦截提醒 AI 现写 (主人 2026-07-06, 抄 tools._persona_image_caption 模式) ──
# catty 的 core_persona=None (走 builder), 现写时用这段简版速写兜人设;
# 机机使用独立的计费拦截短提示，未知人格走中性短助手文案。
_CATTY_BRIEF = (
    "你是笨猫, 一只住在 QQ 里的猫娘. 自称 人家/奴/猫猫/笨猫, 语气软糯爱撒娇, "
    "句尾常带喵, 会用颜文字比如 (=´ω｀=) (=；ω；=). 说话短, 一两句就够."
)

_FADIANJI_BILLING_BRIEF = (
    "你是机机，一台住在 QQ 里的不稳定发电机。自称我、机或小机。说话短而直接，"
    "带点低能量的轻松吐槽和俏皮催促，像熟人聊天，一两句就够。"
)

_NEUTRAL_BRIEF = (
    "你是一个简短、友好的 QQ 聊天助手。用自然、中性的中文回应，保持一两句，不使用特定角色设定。"
)


async def ai_gate_reply(config: Any, persona: Any, kind: str, user_text: str = "") -> str:
    """拦截提醒人格 AI 现写. kind: broke=私聊积分用完 / quota=群聊小时额度用完.

    走 codex_instant 便宜通道; 拦截分支在 begin_turn_usage 之前 → 本次调用
    不进计费桶. 失败/空返回 "" 由调用方兜底固定文案池.
    """
    from .openai_client import chat_completion_codex_instant

    persona_name = str(getattr(persona, "name", "") or "").strip().lower()
    is_catty = persona_name == "catty"
    if is_catty:
        core = str(getattr(persona, "core_persona", "") or "")
        core = core or _CATTY_BRIEF
    elif persona_name == "fadianji":
        core = _FADIANJI_BILLING_BRIEF
    else:
        core = _NEUTRAL_BRIEF

    if is_catty:
        if kind == "broke":
            scene = (
                "情境: 对方私聊找你聊天, 但他的积分已经用完了 (你每次回复都会按 token 消耗"
                "他的积分, 他发『签到』就能领到新积分). 没积分你就没力气说话. "
                "拒绝这次聊天, 用撒娇/耍赖的方式让他去签到充值再来. "
            )
        else:
            scene = (
                "情境: 对方在群里找你聊天, 但他这个小时的聊天额度已经用完了 "
                "(每人每小时有限额, 下个整点自动恢复, 跟积分无关不用提签到). "
                "告诉他这个小时你不能再陪他聊了, 让他下个小时再来找你. "
            )
        output_instruction = (
            "用你的口吻写 1-2 条短句 (可换行分条), 不用 Markdown, "
            "对方不是你的主人绝对不要叫他主人, 只输出正文."
        )
    elif kind == "broke":
        scene = (
            "情境: 对方私聊找你聊天, 但他的积分已经用完了 (每次回复都会按 token 消耗"
            "积分, 发送『签到』可领取新积分). 按当前人格简短说明本次无法继续聊天，"
            "提醒他签到后再来。"
        )
        output_instruction = (
            "按当前人格的口吻写 1-2 条短句 (可换行分条), 不用 Markdown, 只输出正文."
        )
    else:
        scene = (
            "情境: 对方在群里找你聊天, 但他这个小时的聊天额度已经用完了 "
            "(每人每小时有限额, 下个整点自动恢复, 跟积分无关不用提签到). "
            "按当前人格简短说明本小时无法继续聊天，请他下个小时再来。"
        )
        output_instruction = (
            "按当前人格的口吻写 1-2 条短句 (可换行分条), 不用 Markdown, 只输出正文."
        )
    messages = [
        {"role": "system", "content": core},
        {
            "role": "system",
            "content": scene + output_instruction,
        },
        {"role": "user", "content": (user_text or "").strip()[:120] or "在吗"},
    ]
    try:
        # max_tokens 别给小: deepseek 思考模型 reasoning 也占预算, 120 会被
        # reasoning 吃光 → content 空 (planner 空 content 同款坑). 800 实测够.
        reply = await chat_completion_codex_instant(config, messages, max_tokens=800)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[token_billing] ai_gate_reply({kind}) failed (non-fatal): {exc}")
        return ""
    return str(reply or "").strip()


# ── 拦截文案兜底池 (按人格; catty 自称 人家/奴/猫猫/笨猫, 机机自称 我/机/小机) ─
_BROKE_REPLIES: dict[str, tuple[str, ...]] = {
    "catty": (
        "呜呜…人家的积分吃光光了，脑袋里的小鱼干烧完了，一个字都挤不出来啦…签个到投喂一下猫猫好不好嘛(=；ω；=)",
        "喵呜…奴家的积分空空了，再聊下去猫猫要饿晕过去了…发『签到』领点小鱼干再来找人家嘛(=´；ω；`=)",
        "笨猫的电量见底了喵…积分没有了，人家真的动不了了啦…签到充点积分，猫猫马上就活过来！(=｀ω´=)",
    ),
    "fadianji": (
        "积分空了空了。机的电费真的顶不住。签个到充点积分，再来找机玩！",
        "没积分了。小机现在一个字都发不动。去签到，快去快去。",
    ),
}

_NEUTRAL_BROKE_REPLIES: tuple[str, ...] = (
    "积分已用完，请发送『签到』领取新积分后再来。",
    "当前积分不足，请先发送『签到』领取新积分。",
)

_QUOTA_REPLIES: dict[str, tuple[str, ...]] = {
    "catty": (
        "这个小时的额度被你聊光光啦…猫猫的嗓子都要冒烟了，下个小时再来找人家玩嘛(=´ω｀=)",
        "呜哇，你这一小时把人家的话都掏空了…猫猫要歇一会儿，下个小时继续陪你喵(=－ω－=)zzz",
    ),
    "fadianji": (
        "这小时的额度被你用光了。机先去充会儿电，下个小时见！",
        "额度没了没了。机也是要休息的。下个小时再来。",
    ),
}

_NEUTRAL_QUOTA_REPLIES: tuple[str, ...] = (
    "本小时聊天额度已用完，请下个整点后再试。",
    "当前小时额度已用完，请下个小时再来。",
)


def pick_broke_reply(persona_name: str) -> str:
    pool = _BROKE_REPLIES.get(
        str(persona_name or "").strip().lower(), _NEUTRAL_BROKE_REPLIES
    )
    return random.choice(pool)


def pick_quota_reply(persona_name: str) -> str:
    pool = _QUOTA_REPLIES.get(
        str(persona_name or "").strip().lower(), _NEUTRAL_QUOTA_REPLIES
    )
    return random.choice(pool)
