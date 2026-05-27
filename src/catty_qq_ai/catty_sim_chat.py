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
) -> dict[str, Any]:
    """模拟一条 incoming message, 走 _build_messages 拼完整 prompt, 可选调 AI 拿 reply.

    Args:
        text: 用户消息文本
        user_id: 用户 QQ 号 (int or str)
        group_id: 群号. None 则为私聊
        live: True → 真调 AI 拿 reply; False → 只拼 prompt, reply = "[dry-run]"
        history_replace: True → 用空 history 模拟冷会话; False → 用 scope 当前真 history

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
    from .openai_client import chat_completion

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
    # _build_messages 返回 (messages, _prefer_spark) tuple, 这里 unpack
    if isinstance(_bm_ret, tuple) and len(_bm_ret) >= 1:
        messages = _bm_ret[0]
    else:
        messages = _bm_ret  # 兼容老形态

    # 统计 — 防御性: messages 偶尔可能混入非 dict 项, 用 isinstance 守卫
    dict_msgs = [m for m in messages if isinstance(m, dict)]
    system_msgs = [m for m in dict_msgs if m.get("role") == "system"]
    history_msgs = [m for m in dict_msgs if m.get("role") in ("user", "assistant")]
    sys_chars = sum(len(str(m.get("content") or "")) for m in system_msgs)
    total_chars = sum(len(str(m.get("content") or "")) for m in dict_msgs)

    reply = "[dry-run: live=False, 未调 AI]"
    if live:
        try:
            reply_obj = await chat_completion(cfg, messages)
            reply = str(reply_obj or "[AI returned empty]")
        except Exception as exc:  # noqa: BLE001
            reply = f"[sim_chat: chat_completion failed — {type(exc).__name__}: {exc}]"

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
