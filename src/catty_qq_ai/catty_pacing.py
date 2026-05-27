"""笨猫『对话节奏』感知 — 检测用户连发 / 笨猫连发 / 长 idle 回来等节奏信号.

跟现有层的区别:
- catty_reunion: idle 时长触发的重逢反差 (focus 在『久违感』)
- catty_pacing (本层): **当下对话节奏**信号 — 用户是否还在说 / 笨猫该不该停 / 是否
  正常轮回

为什么需要:
真朋友会感知到对方"还在打字"或"自己刚说完该闭嘴" — 笨猫不该每个 user msg 都长篇大论
应答, 应该按对话节奏来. 比如用户连发 3 条短句, 笨猫应该等等再一次回完整;
笨猫连发了 2 条后, 当前轮该短一点收尾.

实现:
- 看 messages 末尾的 role 序列 + (可能的话) 时间戳
- 输出 pacing tag: normal / user_burst / catty_just_spoke / silence_invite
- 注入 prompt hint

注入位置: order=218 (length_intent=217 之后, reply_self_check=220 之前)
"""
from __future__ import annotations

from typing import Any


# user_burst: 末尾有 ≥3 条连续 user msg (没有 assistant 介入)
# catty_just_spoke: 末尾 assistant msg 之后 user 才发 1 条 — 正常, 但如果 assistant
#                   连续 ≥2 条 (笨猫刚连发), 笨猫该收着
# normal: 标准 user/assistant/user/assistant 节奏
# silence_invite: 用户最近发的是单字/短情绪 (嗯/哦/好), 给笨猫一个"可以静默"的信号


_SHORT_FILLER_WORDS: frozenset[str] = frozenset({
    "嗯", "哦", "好", "行", "好的", "知道了", "明白", "ok", "嗯嗯",
    "哈哈", "笑死", "草", "绷", "麻了", "6", "666",
})


def detect_pacing(messages: list[Any]) -> str:
    """看 messages 末尾结构判断对话节奏 tag.

    messages: list[dict] 含 role/content. 末尾应该是 user (当前消息).
    返回 'normal' / 'user_burst' / 'catty_burst' / 'silence_invite'
    """
    if not messages:
        return "normal"

    # 倒序找最近的 user/assistant 边界 (忽略 system)
    convo = [m for m in messages if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if not convo:
        return "normal"

    # 1. user_burst: 末尾 ≥3 条都是 user (没 assistant 介入)
    tail_user_count = 0
    for m in reversed(convo):
        if m.get("role") == "user":
            tail_user_count += 1
        else:
            break
    if tail_user_count >= 3:
        return "user_burst"

    # 2. catty_burst: assistant 连续 ≥2 条后才 user 1 条
    # 反向跳过当前 user, 看接下来 assistant 的连续数
    saw_user_now = False
    catty_consecutive = 0
    for m in reversed(convo):
        role = m.get("role")
        if not saw_user_now:
            if role == "user":
                saw_user_now = True
                continue
            else:
                break  # 末尾不是 user, 异常 — fallback normal
        # 跳过当前 user, 累计 assistant 连续段
        if role == "assistant":
            catty_consecutive += 1
        else:
            break
    if catty_consecutive >= 2:
        return "catty_burst"

    # 3. silence_invite: 末尾 user msg 是短填充词 (嗯/哦/好/...)
    last_user_content = ""
    for m in reversed(convo):
        if m.get("role") == "user":
            last_user_content = str(m.get("content") or "").strip()
            break
    if last_user_content and len(last_user_content) <= 3:
        if last_user_content.lower() in _SHORT_FILLER_WORDS:
            return "silence_invite"

    return "normal"


_PACING_HINTS: dict[str, str] = {
    "user_burst": (
        "对方**连发 ≥3 条还没等你回** — 一次性接住 + 整合, 不要每条都答. "
        "可以一句『嗷呜~等等等我看看(被淹没了喵)』+ 然后挑核心回, "
        "或者直接接最新一条但带『刚才那些也看到啦』式 callback."
    ),
    "catty_burst": (
        "**你刚连发了 ≥2 条** — 本轮该**收着**短回, 不要再长篇大论. "
        "1 句 QQ 短答 + 留个 hook 让对方接话; 不要又开始 2-3 段刷屏."
    ),
    "silence_invite": (
        "对方发的是**单字短情绪填充**(嗯/哦/好/6 之类) — **不要硬接长**, "
        "可以 1 句轻量收尾 (『嗯嗯~主人慢慢来喵 ฅ』), 或者直接接受 silence 不强求每条都回."
    ),
}


def build_pacing_prompt(messages: list[Any]) -> str:
    """构建对话节奏 prompt 段. normal 返回 ""(skip register)."""
    tag = detect_pacing(messages)
    if tag == "normal" or tag not in _PACING_HINTS:
        return ""
    return f"【对话节奏 ({tag})】\n{_PACING_HINTS[tag]}\n(这是节奏信号, 不强制改变内容, 只调节本轮长度/收发频率。)"


__all__ = [
    "detect_pacing",
    "build_pacing_prompt",
]
