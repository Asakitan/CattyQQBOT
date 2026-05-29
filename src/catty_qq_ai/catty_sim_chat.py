"""笨猫 sim chat helper — 让开发者从外部 (rpwsh / HTTP) 触发一条 chat 模拟.

用途:
- 在不真正发 QQ 消息的情况下, 通过 mock 一个 OneBot MessageEvent 走完整
  _build_messages 路径, 然后真调 AI 拿 reply.
- 用来验证 prompt 改动效果: 同一条 input 不同 commit 的 reply 对比.
- 通过 FastAPI dev endpoint 暴露, rpwsh `Invoke-RestMethod` 调用.

非生产用途 — 不发到 QQ, 只拿回完整 messages + AI reply (+ 可选 logger.info)。

接入流程:
1. `from .catty_sim_chat import sim_chat`
2. `await sim_chat(text="好累喵", user_id=993255714, group_id=None, live=True)`
3. 返回 {"messages": [...], "reply": "...", "stats": {...}}

设计:
- mock event 用 pydantic.model_construct 跳过 validation
- 复用 __init__._build_messages 和 openai_client.chat_completion
- live=False 时只拼 prompt 不调 AI (dry-run)
- 不污染 history (mock event 的 message_id 用一个固定大数标记 sim)
"""
from __future__ import annotations

import time
from typing import Any


_SIM_MESSAGE_ID_BASE = 10**12  # 大数 + time 让 mock event message_id 不撞真消息


def _make_mock_private_event(user_id: int, text: str, *, self_id: int = 0):
    """构造 mock PrivateMessageEvent. user_id 是真用户 QQ 号 (主人会用 993255714)."""
    from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
    from nonebot.adapters.onebot.v11.event import Sender
    now = int(time.time())
    return PrivateMessageEvent.model_construct(
        time=now,
        self_id=self_id,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=_SIM_MESSAGE_ID_BASE + now,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender.model_construct(
            user_id=user_id, nickname=f"sim_{user_id}", sex="unknown", age=0,
        ),
        to_me=True,
    )


def _make_mock_group_event(user_id: int, group_id: int, text: str, *, self_id: int = 0):
    """构造 mock GroupMessageEvent. 默认 to_me=True 让 @ 笨猫流程走通."""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    from nonebot.adapters.onebot.v11.event import Sender
    now = int(time.time())
    return GroupMessageEvent.model_construct(
        time=now,
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=_SIM_MESSAGE_ID_BASE + now,
        group_id=group_id,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender.model_construct(
            user_id=user_id, nickname=f"sim_{user_id}", card=f"sim_{user_id}",
            sex="unknown", age=0, role="member",
        ),
        to_me=True,
    )


async def sim_chat(
    *,
    text: str,
    user_id: int | str,
    group_id: int | str | None = None,
    live: bool = True,
    history_replace: bool = False,
    with_tools: bool = True,
    force_spark: bool = False,
    persist: bool = False,
) -> dict[str, Any]:
    """模拟一条 incoming message, 走 _build_messages 拼完整 prompt, 可选调 AI 拿 reply.

    Args:
        text: 用户消息文本
        user_id: 用户 QQ 号 (int or str)
        group_id: 群号. None 则为私聊
        live: True → 真调 AI 拿 reply; False → 只拼 prompt, reply = "[dry-run]"
        history_replace: True → 用空 history 模拟冷会话; False → 用 scope 当前真 history
        with_tools: True → 调 chat_completion_with_tools (含 catty tool schemas, 真实主对话
            路径, cache hit_rate 对齐生产); False → 调 chat_completion (无 tools, 更纯净
            的 prompt cache 测试, 主人 2026-05-29 Round 6 加 with_tools 让 sim 命中率 ≈ 真实).

    Returns:
        {
            "messages": list[ChatMessage],     # 完整发给 AI 的 messages
            "system_blocks": int,              # system 段数量
            "history_count": int,              # history 条数
            "reply": str,                      # AI reply (或 dry-run 提示)
            "stats": {"total_chars": int, "system_chars": int, ...},
        }
    """
    from . import _build_messages, config as _module_config  # circular-safe deferred
    from .message_utils import build_history_key, extract_incoming_message
    from .openai_client import chat_completion, chat_completion_with_tools

    # 主人 2026-05-28: dev/sim_chat 支持非数字 user_id (e.g. 'owner_test') 用作 mock —
    # 数字直接用, 非数字 hash 出稳定正整数作 fake QQ. group_id 同理.
    def _to_qq_id(val: int | str, fallback_label: str) -> int:
        if isinstance(val, int):
            return val
        s = str(val).strip()
        if not s:
            raise ValueError(f"{fallback_label} 为空")
        try:
            return int(s)
        except ValueError:
            import hashlib
            digest = hashlib.md5(s.encode()).digest()
            return int.from_bytes(digest[:6], "big")  # 6 字节 → 0..2^48, 不溢 QQ 号位数感

    uid = _to_qq_id(user_id, "user_id")
    gid = _to_qq_id(group_id, "group_id") if group_id is not None else None
    if gid:
        event = _make_mock_group_event(uid, gid, text)
    else:
        event = _make_mock_private_event(uid, text)

    cfg = _module_config

    incoming = extract_incoming_message(str(getattr(cfg, "qq_account", "0") or "0"), event, cfg)
    if incoming is None:
        return {
            "messages": [], "system_blocks": 0, "history_count": 0,
            "reply": "[sim_chat: extract_incoming_message returned None — 可能被 access 拦了]",
            "stats": {},
        }

    key = build_history_key(event, cfg)

    # build_messages 内部读 session_cache 拿历史, history_replace=True 时清空一下
    if history_replace:
        try:
            from . import _get_session_cache
            sc = _get_session_cache()
            sc.set(key, [])  # 清空 history 模拟冷会话
        except Exception:
            pass

    _bm_ret = await _build_messages(event, key, incoming)
    # _build_messages 返回 (messages, _prefer_spark) tuple
    # 主人 2026-05-29 Round 15: 拿 prefer_spark 标记 — spark 真实路径走
    # chat_completion_codex_instant 不是 chat_completion_with_tools.
    # 之前 sim_chat 只取 messages, prefer_spark=True 时仍走 with_tools → sim 99% 真实 68%.
    _prefer_spark = False
    if isinstance(_bm_ret, tuple) and len(_bm_ret) >= 1:
        messages = _bm_ret[0]
        if len(_bm_ret) >= 2:
            _prefer_spark = bool(_bm_ret[1])
    else:
        messages = _bm_ret  # 兼容老形态
    # 主人 2026-05-29 Round 15: force_spark=True 强制走 spark 路径 (诊断用)
    if force_spark:
        _prefer_spark = True

    # 统计 — 防御性: messages 偶尔可能混入非 dict 项, 用 isinstance 守卫
    dict_msgs = [m for m in messages if isinstance(m, dict)]
    system_msgs = [m for m in dict_msgs if m.get("role") == "system"]
    history_msgs = [m for m in dict_msgs if m.get("role") in ("user", "assistant")]
    sys_chars = sum(len(str(m.get("content") or "")) for m in system_msgs)
    total_chars = sum(len(str(m.get("content") or "")) for m in dict_msgs)

    reply = "[dry-run: live=False, 未调 AI]"
    if live:
        try:
            # 主人 2026-05-28: sim_chat 也要 set_current_scope_key, 让 anthropic metadata.user_id
            # 注入 (cache routing). 主 handle_chat 入口已经 set, 但 sim_chat 是 dev endpoint
            # 旁路, 不经过 handle_chat → contextvar 是 None → metadata 不发. 这里补上.
            try:
                from .openai_client import set_current_scope_key
                set_current_scope_key(key)
            except Exception:  # noqa: BLE001
                pass
            # 主人 2026-05-29 Round 15: prefer_spark=True 时走真实 NSFW spark 路径
            # (chat_completion_codex_instant), 跟生产完全一致 (sys_md5=7ebd7276 不是 6a6b6972).
            if _prefer_spark:
                try:
                    from .openai_client import chat_completion_codex_instant
                    # 真实 spark 用 _pick_nsfw_model_for + max_tokens=800
                    reply_obj = await chat_completion_codex_instant(
                        cfg, messages, max_tokens=800,
                    )
                    reply = str(reply_obj or "[AI returned empty]")
                except Exception as _spark_exc:
                    reply = f"[sim spark failed — {type(_spark_exc).__name__}: {_spark_exc}]"
            # 主人 2026-05-29 Round 6: with_tools=True (默认) 走真实主对话路径
            # (chat_completion_with_tools + available_tool_schemas), 让 sim 命中率对齐生产.
            # 之前 chat_completion 无 tools, sim 测出 99% 但生产 65% 差距大.
            elif with_tools:
                try:
                    from . import memory_store, affection_store
                    from .tools import (
                        available_tool_schemas, execute_tool_call, ToolContext,
                    )
                    from nonebot.adapters.onebot.v11 import PrivateMessageEvent
                    _is_private = isinstance(event, PrivateMessageEvent)
                    _user_text = text
                    _tools = available_tool_schemas(
                        cfg,
                        is_private=_is_private,
                        user_text=_user_text,
                        has_image=bool(getattr(incoming, "has_image", False)),
                    )
                    # 构 ToolContext (sim 模式只需基础 3 字段, 其它走 default)
                    _ctx = ToolContext(
                        config=cfg,
                        memory_store=memory_store,
                        event=event,
                        affection_store=affection_store,
                    )
                    async def _executor(name: str, args_json: str) -> dict[str, object]:
                        return await execute_tool_call(name, args_json, _ctx)
                    reply_obj = await chat_completion_with_tools(
                        cfg, messages,
                        tools=_tools,
                        tool_executor=_executor,
                        max_rounds=int(getattr(cfg, "catty_tools_max_rounds", 3) or 3),
                        max_calls_per_round=int(getattr(cfg, "catty_tools_max_calls_per_round", 3) or 3),
                    )
                except Exception as _wt_exc:
                    # tool-path 失败 fallback 到 plain chat_completion (兼容老 sim 测试)
                    reply_obj = await chat_completion(cfg, messages)
                    reply = f"[with_tools fallback: {type(_wt_exc).__name__}: {_wt_exc}] " + str(reply_obj or "")
                else:
                    reply = str(reply_obj or "[AI returned empty]")
            else:
                reply_obj = await chat_completion(cfg, messages)
                reply = str(reply_obj or "[AI returned empty]")
        except Exception as exc:  # noqa: BLE001
            reply = f"[sim_chat: chat_completion failed — {type(exc).__name__}: {exc}]"

    # 主人 2026-05-29: persist 模式 — 复刻 handle_chat 的 _append_history (主链路同一函数),
    # 让多轮 sim 的 history 真实增长 + 到阈值 trim, 测真实 cache 命中(不再用固定 history).
    # 默认 False 不污染 history; 测真实命中率时用 isolated 测试 scope + persist=True.
    if live and persist and reply and not str(reply).startswith("["):
        try:
            from . import _append_history
            # 取 messages 里最后一条 user 的实际 content 作存档版本 (跟 handle_chat 一致)
            _enriched = incoming.history_content
            for _m in reversed(messages):
                if isinstance(_m, dict) and _m.get("role") == "user":
                    _c = _m.get("content", "")
                    if isinstance(_c, str) and _c.strip():
                        _enriched = _c
                    elif isinstance(_c, list) and _c:
                        _texts = [
                            str(_b.get("text", "")) for _b in _c
                            if isinstance(_b, dict) and _b.get("type") == "text"
                        ]
                        if _texts:
                            _enriched = "\n".join(_texts)
                    break
            _append_history(key, _enriched, str(reply))
        except Exception:  # noqa: BLE001
            pass

    return {
        "messages": messages,
        "system_blocks": len(system_msgs),
        "history_count": len(history_msgs),
        "reply": reply,
        "stats": {
            "total_chars": total_chars,
            "system_chars": sys_chars,
            "history_chars": total_chars - sys_chars,
            "raw_len": len(messages),
        },
    }


__all__ = ["sim_chat"]
