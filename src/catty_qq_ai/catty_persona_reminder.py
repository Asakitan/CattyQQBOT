"""P5.3 — 每 N 轮人格提醒 (turn counter + depth_inject).

主人 2026-05-28 plan-cattyCacheFixAndPromptSlim P5.3:
P5.1 把人格内容从每轮 hardcoded 注入改成 ~2.2K 单段 core_persona (cache prefix).
长对话里 AI 容易"忘记"细节, 但每轮都重复整段 ~2.2K 又浪费.
方案: 每 N 轮 (默认 6) 在 user msg 附近 inject 一段精简 (~150 token) 提醒.

机制:
- per-scope turn counter (in-memory dict)
- 每次调用 should_inject_reminder 递增 counter
- counter % N == 0 时返回 True (触发 reminder)
- bot 重启时 counter 丢失, 第 N 轮重新触发

Reminder 内容:
- 5 条核心铁律 (傲娇/猫系/短句/反差/称呼)
- ~150 token, 跟 core_persona 头部 anchor 对齐
- inject 位置: depth=2 (黄金区, 紧贴 user 当前消息但不在最末)
"""
from __future__ import annotations

import logging

logger = logging.getLogger("catty.persona_reminder")


# per-scope turn counter (in-memory, bot 重启时丢失)
_REMINDER_TURN_COUNT: dict[str, int] = {}


# ~150 token 精简提醒 (跟 core_persona 5 铁律对齐, 长对话防漂移)
_REMINDER_TEXT = (
    "【人格贴身提醒 (每 N 轮)】你是笨猫本人:"
    " 1) 嘴硬本能 — 对主人叫『杂鱼/笨蛋主人』内心黏人;"
    " 2) 反差链 — 暧昧时炸毛→嘴硬→小声暴露→小动作掩饰, 绝不冷拒;"
    " 3) 短句 1-3 句 QQ 节奏, 不写小说;"
    " 4) 必带 (动作) + 猫系词喵/喵呜/ฅฅ, 自称『人家/猫猫/笨猫』;"
    " 5) 称呼专属: 真实主人才用『主人』, 群友用『你/对方/笨蛋XX』."
    " 元身份追问 (AI/bot) 炸毛拒认, 绝不输出 `I'm Claude` / 英文 disclaimer."
)


def should_inject_reminder(scope_id: str, every_n_turns: int = 6) -> bool:
    """递增 scope 的轮次 counter, 每 N 轮返回 True.

    Args:
        scope_id: per-scope 标识 (catty session key)
        every_n_turns: 每多少轮触发一次 (默认 6)

    Returns:
        True = 本轮应该 inject reminder, False = 不
    """
    if every_n_turns <= 0:
        return False
    if not scope_id:
        return False
    count = _REMINDER_TURN_COUNT.get(scope_id, 0) + 1
    _REMINDER_TURN_COUNT[scope_id] = count
    # 每 N 轮触发: count=N, 2N, 3N, ...
    return count % every_n_turns == 0


def build_reminder_text(persona=None) -> str:
    """返回精简 reminder text (~150 token).

    多人格 (主人 2026-07-06): persona.persona_reminder_text 非空时用它;
    None / catty → 原 _REMINDER_TEXT 字节不变。
    """
    if persona is not None and getattr(persona, "persona_reminder_text", None):
        return persona.persona_reminder_text
    return _REMINDER_TEXT


def reset_scope(scope_id: str | None = None) -> None:
    """测试用 / 主人手动重置."""
    if scope_id is None:
        _REMINDER_TURN_COUNT.clear()
    else:
        _REMINDER_TURN_COUNT.pop(scope_id, None)


def get_turn_count(scope_id: str) -> int:
    """诊断用: 当前 scope 已计数轮次."""
    return _REMINDER_TURN_COUNT.get(scope_id, 0)


__all__ = [
    "should_inject_reminder",
    "build_reminder_text",
    "reset_scope",
    "get_turn_count",
]
