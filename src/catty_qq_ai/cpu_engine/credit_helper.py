"""积分门控辅助函数 (S3.1).

不重造 CreditStore - 复用 catty_qq_ai.affection.AffectionStore 已有的:
- get_points / consume_points / admin_add_points
- daily_checkin
- is_owner (主人豁免无限)

本模块加: 时间被动恢复 + DeepSeek token-based 结算 + 占位扣分.

被动恢复 last_ts 仅内存 (bot 重启丢失, 用户最多多薅一次 +5 积分, 无重大影响).
持久化的部分都走 affection_store 既有的原子写 + 5s debounce flush.
"""

from __future__ import annotations

import math
import time
from typing import Any

from loguru import logger


def _push_dashboard(
    user_id: str,
    kind: str,
    *,
    delta: int = 0,
    balance_after: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    scope: str = "",
    reason: str = "",
) -> None:
    """惰性 import 避免循环, dashboard 不可用时静默."""
    try:
        from ..dashboard_state import push_credit_event  # type: ignore

        push_credit_event(
            user_id,
            kind,
            delta=delta,
            balance_after=balance_after,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            scope=scope,
            reason=reason,
        )
    except Exception:  # noqa: BLE001
        pass


_LAST_PASSIVE_RECOVER_TS: dict[str, float] = {}


def estimate_deepseek_cost(
    config: Any,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> int:
    """计算 DeepSeek 调用应扣积分 = base + ceil(prompt/1k)*per_p + ceil(completion/1k)*per_c."""
    base = int(getattr(config, "catty_credit_deepseek_base_cost", 3))
    per_p = int(getattr(config, "catty_credit_deepseek_per_1k_prompt", 2))
    per_c = int(getattr(config, "catty_credit_deepseek_per_1k_completion", 5))
    p_cost = math.ceil(max(prompt_tokens, 0) / 1000) * per_p
    c_cost = math.ceil(max(completion_tokens, 0) / 1000) * per_c
    return base + p_cost + c_cost


def get_balance(affection_store: Any, user_id: str) -> int:
    """复用 affection_store.get_points (主人返回 OWNER_INFINITY_POINTS)."""
    try:
        return int(affection_store.get_points(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[credit] get_balance({user_id}) failed: {exc}")
        return 0


def can_afford_base(affection_store: Any, config: Any, user_id: str) -> bool:
    """够不够 base_cost (主人永远够)."""
    if affection_store.is_owner(user_id):
        return True
    base = int(getattr(config, "catty_credit_deepseek_base_cost", 3))
    return get_balance(affection_store, user_id) >= base


def charge_base(
    affection_store: Any,
    config: Any,
    user_id: str,
    *,
    scope: str = "",
) -> tuple[bool, int, int]:
    """调用 DeepSeek 前占位扣 base_cost (主人豁免不扣).

    返回 (success, base_charged, balance_after).
    success=False 时调用方应转 L0_beg 撒娇求充值.
    """
    base = int(getattr(config, "catty_credit_deepseek_base_cost", 3))
    if affection_store.is_owner(user_id):
        return True, 0, get_balance(affection_store, user_id)
    try:
        result = affection_store.consume_points(user_id, base)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[credit] charge_base({user_id}, {base}) raised: {exc}")
        return False, 0, get_balance(affection_store, user_id)
    success = bool(result.get("success", False))
    balance = int(result.get("balance", 0))
    if success:
        logger.info(
            f"[credit] CHARGE_BASE uid={user_id} scope={scope} base={base} balance_after={balance}"
        )
        _push_dashboard(
            user_id,
            "charge_base",
            delta=-base,
            balance_after=balance,
            scope=scope,
        )
    return success, base if success else 0, balance


def settle_after_response(
    affection_store: Any,
    config: Any,
    user_id: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    base_charged: int,
    scope: str = "",
) -> int:
    """DeepSeek 返回后按真实 token 结算: 多扣或退还. 主人豁免不动余额.

    返回最终扣除总额 (>=0). diff < 0 时退还 base 的一部分.
    """
    if affection_store.is_owner(user_id):
        return 0
    total = estimate_deepseek_cost(
        config,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    diff = total - base_charged
    try:
        if diff > 0:
            r = affection_store.consume_points(user_id, diff)
            new_balance = int(r.get("balance", 0))
            logger.info(
                f"[credit] SETTLE_EXTRA uid={user_id} scope={scope} "
                f"total={total} base={base_charged} extra={diff} "
                f"p={prompt_tokens} c={completion_tokens} balance={new_balance}"
            )
            _push_dashboard(
                user_id, "settle_extra", delta=-diff, balance_after=new_balance,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, scope=scope,
            )
        elif diff < 0:
            refund = -diff
            r = affection_store.admin_add_points(user_id, refund)
            new_balance = int(r.get("balance", 0))
            logger.info(
                f"[credit] SETTLE_REFUND uid={user_id} scope={scope} "
                f"total={total} base={base_charged} refund={refund} "
                f"p={prompt_tokens} c={completion_tokens} balance={new_balance}"
            )
            _push_dashboard(
                user_id, "settle_refund", delta=refund, balance_after=new_balance,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, scope=scope,
            )
        else:
            logger.info(
                f"[credit] SETTLE_EVEN uid={user_id} scope={scope} total={total} "
                f"p={prompt_tokens} c={completion_tokens}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[credit] settle({user_id}, diff={diff}) failed: {exc}")
    return total


def passive_recover_step(
    affection_store: Any,
    config: Any,
    user_id: str,
) -> int:
    """按需被动恢复, 距上次 >=1h + balance < cap 时加 per_hour 积分. 主人跳过."""
    if not getattr(config, "catty_credit_enabled", False):
        return 0
    if affection_store.is_owner(user_id):
        return 0
    cap = int(getattr(config, "catty_credit_passive_recover_cap", 100))
    if get_balance(affection_store, user_id) >= cap:
        return 0
    per_hour = int(getattr(config, "catty_credit_passive_recover_per_hour", 5))
    if per_hour <= 0:
        return 0
    now = time.time()
    last = _LAST_PASSIVE_RECOVER_TS.get(user_id, 0.0)
    if now - last < 3600.0:
        return 0
    _LAST_PASSIVE_RECOVER_TS[user_id] = now
    try:
        r = affection_store.admin_add_points(user_id, per_hour)
        new_balance = int(r.get("balance", 0))
        logger.info(
            f"[credit] PASSIVE_RECOVER uid={user_id} +{per_hour} balance={new_balance} (cap={cap})"
        )
        _push_dashboard(
            user_id, "passive_recover", delta=per_hour, balance_after=new_balance,
        )
        return per_hour
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[credit] passive_recover_step({user_id}) failed: {exc}")
        return 0


def try_daily_signin(affection_store: Any, user_id: str) -> dict[str, Any]:
    """触发签到 (薄包装, 让 cpu_engine 不直接依赖 affection 命名)."""
    try:
        return affection_store.daily_checkin(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[credit] try_daily_signin({user_id}) failed: {exc}")
        return {"success": False, "already": False, "balance": 0, "error": str(exc)}
