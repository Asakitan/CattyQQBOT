"""SillyTavern 风 prompt macro substitution。

参考 ST 官方 macros docs (https://docs.sillytavern.app/usage/core-concepts/macros/),
实现常用的 macro 替换。把 character_card / world_info / story_arc 等模块里写的
`{{char}}` `{{user}}` `{{date}}` 等占位符在 prompt build 阶段替换成运行时实际值。

支持的 macro:
- 名字: {{char}} {{user}} {{group}}
- 时间: {{time}} {{date}} {{weekday}} {{isodate}} {{isotime}} {{datetimeformat::FMT}}
- 历史: {{lastUserMessage}} {{lastCharMessage}}
- 闲置: {{idleDuration}} (上次对话到现在的时长)
- 随机/挑选: {{random::a::b::c}} {{pick::a::b::c}}
- 控制: {{newline}} {{space}} {{noop}}
- 注释: {{//}} -> 删除
- 变量(简化): {{getvar::name}} {{setvar::name::value}}

不支持(暂不实现,加注释提示而已):
- {{summary}}, {{persona}} 等 ST 特定字段
- 完整的 if/else 分支
- 全局变量持久化

使用:
    macros.render(text, ctx={"char": "笨猫", "user": "Alice", "weekday": ...})
"""
from __future__ import annotations

import random as _random
import re
import time
from datetime import datetime
from typing import Any


# ── 顶层入口 ────────────────────────────────────────────────────────────
def render(text: str, ctx: dict[str, Any] | None = None) -> str:
    """对 text 做完整 macro 替换,返回新字符串。

    ctx 可以传:
        char        : 角色名(默认『笨猫』)
        user        : 对方名字字面值(默认『用户』)
        group       : 群昵称(可选)
        last_user_message : 对方最近一条消息文本(可选)
        last_char_message : 笨猫最近一条回复文本(可选)
        last_active_at    : 上次对话 epoch seconds(用于 {{idleDuration}})
        variables   : dict 提供 getvar/setvar
        now         : datetime,默认 datetime.now()
        seed        : int,给 {{random::}} 的可复现种子(默认每次不同)
    """
    if not text:
        return text
    ctx = dict(ctx or {})
    ctx.setdefault("char", "笨猫")
    ctx.setdefault("user", "用户")
    ctx.setdefault("now", datetime.now())

    # 顺序:先简单替换 → 时间/格式 → 历史 → 闲置 → 变量 → 随机/挑选 → 控制
    text = _replace_basic_names(text, ctx)
    text = _replace_time(text, ctx)
    text = _replace_history(text, ctx)
    text = _replace_idle(text, ctx)
    text = _replace_variables(text, ctx)
    text = _replace_random_pick(text, ctx)
    text = _replace_control(text, ctx)
    text = _strip_comments(text)
    return text


# ── 名字 ────────────────────────────────────────────────────────────────
def _replace_basic_names(text: str, ctx: dict[str, Any]) -> str:
    return (
        text.replace("{{char}}", str(ctx.get("char", "")))
            .replace("{{user}}", str(ctx.get("user", "")))
            .replace("{{group}}", str(ctx.get("group", "")))
    )


# ── 时间 ────────────────────────────────────────────────────────────────
_WEEKDAYS_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _replace_time(text: str, ctx: dict[str, Any]) -> str:
    now: datetime = ctx.get("now") or datetime.now()
    text = text.replace("{{time}}", now.strftime("%H:%M"))
    text = text.replace("{{date}}", now.strftime("%Y-%m-%d"))
    text = text.replace("{{weekday}}", _WEEKDAYS_ZH[now.weekday()])
    text = text.replace("{{isodate}}", now.date().isoformat())
    text = text.replace("{{isotime}}", now.time().isoformat(timespec="seconds"))

    # {{datetimeformat::FMT}} 自定义 strftime 格式
    def _fmt_sub(m: re.Match[str]) -> str:
        try:
            return now.strftime(m.group(1))
        except (ValueError, TypeError):
            return m.group(0)
    text = re.sub(r"\{\{datetimeformat::(.+?)\}\}", _fmt_sub, text)
    return text


# ── 历史 ────────────────────────────────────────────────────────────────
def _replace_history(text: str, ctx: dict[str, Any]) -> str:
    text = text.replace("{{lastUserMessage}}", str(ctx.get("last_user_message", "")))
    text = text.replace("{{lastCharMessage}}", str(ctx.get("last_char_message", "")))
    return text


# ── 闲置时长 ────────────────────────────────────────────────────────────
def _format_duration_short(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def _replace_idle(text: str, ctx: dict[str, Any]) -> str:
    if "{{idleDuration}}" not in text:
        return text
    last_active = ctx.get("last_active_at")
    if not last_active:
        return text.replace("{{idleDuration}}", "")
    delta = time.time() - float(last_active)
    if delta < 0:
        delta = 0
    return text.replace("{{idleDuration}}", _format_duration_short(delta))


# ── 变量 ────────────────────────────────────────────────────────────────
def _replace_variables(text: str, ctx: dict[str, Any]) -> str:
    variables = ctx.get("variables")
    if not isinstance(variables, dict):
        return text
    # {{setvar::name::value}} → 设置变量,渲染为空
    def _setvar(m: re.Match[str]) -> str:
        name, value = m.group(1).strip(), m.group(2)
        variables[name] = value
        return ""
    text = re.sub(r"\{\{setvar::([^:}]+)::([^}]*)\}\}", _setvar, text)
    # {{getvar::name}} → 取值;不存在则空
    def _getvar(m: re.Match[str]) -> str:
        return str(variables.get(m.group(1).strip(), ""))
    text = re.sub(r"\{\{getvar::([^}]+)\}\}", _getvar, text)
    return text


# ── 随机/挑选 ───────────────────────────────────────────────────────────
def _replace_random_pick(text: str, ctx: dict[str, Any]) -> str:
    """{{random::a::b::c}} 每次随机抽一个; {{pick::a::b::c}} 同一 seed 下稳定。"""
    seed = ctx.get("seed")
    rng = _random.Random(seed) if seed is not None else _random.Random()

    def _random_sub(m: re.Match[str]) -> str:
        options = [s for s in m.group(1).split("::") if s != ""]
        if not options:
            return ""
        return rng.choice(options)

    def _pick_sub(m: re.Match[str]) -> str:
        options = [s for s in m.group(1).split("::") if s != ""]
        if not options:
            return ""
        # pick 用 hash(text+options) 稳定
        h = abs(hash(m.group(1))) if seed is None else abs(hash((seed, m.group(1))))
        return options[h % len(options)]

    text = re.sub(r"\{\{random::([^}]+)\}\}", _random_sub, text)
    text = re.sub(r"\{\{pick::([^}]+)\}\}", _pick_sub, text)
    return text


# ── 控制 ────────────────────────────────────────────────────────────────
def _replace_control(text: str, ctx: dict[str, Any]) -> str:
    return (
        text.replace("{{newline}}", "\n")
            .replace("{{space}}", " ")
            .replace("{{noop}}", "")
    )


# ── 注释 {{// ...}} → 删除整段 ──────────────────────────────────────
def _strip_comments(text: str) -> str:
    return re.sub(r"\{\{//[^}]*\}\}", "", text)


__all__ = ["render"]
