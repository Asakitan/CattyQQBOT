"""Script 变量上下文 (BotLibre Script 层等价物).

模板可插入的变量:
    {cat_suffix}        - 随机猫系后缀 (喵～/ฅฅ/嗷呜～/...)
    {user_nickname}     - 用户昵称 (主人/{真实昵称}/杂鱼主人)
    {affection_level}   - 好感度等级 (0-10+)
    {affection_title}   - 好感度称号 (陌生人/朋友/恋人/...)
    {scope_type}        - private / group
    {time_of_day}       - morning / afternoon / evening / night
    {topic}             - 当前意图 (greeting / tease_cat / ...)
    {user_vibe}         - 用户 vibe 摘要 (S2.3 接入)
    {group_lore}        - 群 lore 摘要 (S2.3 接入)

渲染失败 (变量缺失) 时用 default_vars 兜底, 仍渲染失败 (字段名不在 defaults)
则保留 placeholder 字面量 ({xxx}). 这样 CPU 层回复出错不会变成 KeyError 崩.
"""

from __future__ import annotations

import random
import string
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


@dataclass(slots=True)
class ScriptContext:
    user_id: str
    user_nickname: str
    scope_type: str  # "private" / "group"
    intent: str
    affection_level: int = 0
    affection_title: str = "陌生人"
    time_of_day: str = "afternoon"  # morning/afternoon/evening/night
    user_vibe: str = ""
    group_lore: str = ""

    def to_render_dict(self, cat_suffix: str) -> dict[str, str]:
        return {
            "cat_suffix": cat_suffix,
            "user_nickname": self.user_nickname,
            "scope_type": self.scope_type,
            "intent": self.intent,
            "topic": self.intent,
            "affection_level": str(self.affection_level),
            "affection_title": self.affection_title,
            "time_of_day": self.time_of_day,
            "user_vibe": self.user_vibe,
            "group_lore": self.group_lore,
        }


_DEFAULT_CAT_SUFFIXES = ["喵～", "喵呜", "ฅฅ", "嗷呜～", "爪爪", "贴贴"]
_DEFAULT_RENDER_VARS = {
    "cat_suffix": "喵～",
    "user_nickname": "主人",
    "scope_type": "private",
    "intent": "default",
    "topic": "default",
    "affection_level": "0",
    "affection_title": "陌生人",
    "time_of_day": "afternoon",
    "user_vibe": "",
    "group_lore": "",
}

_AFFECTION_TITLES: list[tuple[int, str]] = [
    (0, "陌生人"),
    (3, "认识"),
    (6, "朋友"),
    (10, "好朋友"),
    (15, "亲近"),
    (20, "恋人"),
    (30, "主人"),
]


def _affection_title_for(level: int) -> str:
    title = "陌生人"
    for threshold, name in _AFFECTION_TITLES:
        if level >= threshold:
            title = name
    return title


def _time_of_day_for(ts: float | None = None) -> str:
    now = datetime.fromtimestamp(ts) if ts is not None else datetime.now()
    hour = now.hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 14:
        return "noon"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


class _SafeFormatter(string.Formatter):
    """str.format 子类: 缺失字段不报错, 用 defaults 或保留字面 placeholder."""

    def __init__(self, defaults: dict[str, str]) -> None:
        super().__init__()
        self._defaults = defaults

    def get_value(self, key: int | str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if isinstance(key, str):
            if key in kwargs:
                return kwargs[key]
            if key in self._defaults:
                return self._defaults[key]
            return "{" + key + "}"
        return super().get_value(key, args, kwargs)


_FORMATTER = _SafeFormatter(_DEFAULT_RENDER_VARS)


def build_script_ctx(
    *,
    user_id: str,
    user_nickname: str,
    scope_type: str,
    intent: str,
    affection_get_fn: Callable[[str], tuple[int, int]] | None = None,
    user_vibe_get_fn: Callable[[str], str] | None = None,
    group_lore_get_fn: Callable[[str], str] | None = None,
    group_id: str = "",
) -> ScriptContext:
    """构造 ScriptContext. 各 _get_fn 任一失败仅 swallow, 用默认值兜底.

    不引入 nonebot event 类型 (cpu_engine 不应反向依赖 nonebot).
    """
    level = 0
    if affection_get_fn is not None:
        try:
            level, _exp = affection_get_fn(user_id)
        except Exception:  # noqa: BLE001
            level = 0

    vibe = ""
    if user_vibe_get_fn is not None:
        try:
            vibe = user_vibe_get_fn(user_id)
        except Exception:  # noqa: BLE001
            vibe = ""

    lore = ""
    if group_lore_get_fn is not None and group_id:
        try:
            lore = group_lore_get_fn(group_id)
        except Exception:  # noqa: BLE001
            lore = ""

    return ScriptContext(
        user_id=user_id,
        user_nickname=user_nickname or "主人",
        scope_type=scope_type,
        intent=intent or "default",
        affection_level=int(level),
        affection_title=_affection_title_for(int(level)),
        time_of_day=_time_of_day_for(),
        user_vibe=vibe,
        group_lore=lore,
    )


def render(
    template: str,
    ctx: ScriptContext,
    cat_suffixes: list[str] | None = None,
) -> str:
    """安全模板渲染. 缺变量保留 {placeholder} 字面, 不抛 KeyError."""
    if not template:
        return ""
    suffixes = cat_suffixes if cat_suffixes else _DEFAULT_CAT_SUFFIXES
    cat_suffix = random.choice(suffixes) if suffixes else "喵～"
    render_vars = ctx.to_render_dict(cat_suffix)
    try:
        return _FORMATTER.format(template, **render_vars)
    except Exception:
        return template
