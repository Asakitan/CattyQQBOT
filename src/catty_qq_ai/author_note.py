"""SillyTavern 风「Author's Note」深度注入。

ST 的 Author's Note 不是 system prompt 顶部的指令,而是在 chat history 的
**特定深度**插入一条 system 消息。最常见的是 depth=4(『在最近 4 条消息之前插』),
让 AI 更敏感地遵守这条指令(因为它就贴在用户当前消息附近)。

为什么不放最顶?
- 顶部 system prompt 会被长对话稀释(尤其当 chat history 很长)
- 注入到 depth=N 处让指令始终贴近"当下"
- 比 jailbreak(总在最末)更灵活,可以放在 user 的最新 1-2 条之前但不是最末

笨猫用法:
- 默认 depth=4: 注入一条「保持人设短回复」的提醒,对抗长对话漂移
- 命中 emo / 暧昧 / NSFW 等 world_info 时可临时改写 author's note
- 主人级用户给一条更亲密的 author's note,陌生人给保持距离感的 note
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuthorNote:
    content: str
    depth: int = 4         # 从 chat history 末尾倒数第 N 条之前插入
    role: str = "system"   # ST 支持 system/user/assistant,我们默认 system

    def is_empty(self) -> bool:
        return not self.content.strip()


def inject_author_note(
    messages: list[dict[str, Any]],
    note: AuthorNote,
    *,
    boundary_marker_key: str = "role",
) -> list[dict[str, Any]]:
    """把 author's note 注入到 messages 末尾倒数第 `depth` 个 user/assistant 边界之前。

    返回新 list(不修改原 list)。如果 depth 大于现有非 system 消息数,放最末尾。
    如果 messages 全是 system 段(还没 chat history),退化成附加到末尾。
    """
    if note.is_empty():
        return list(messages)

    # 找到所有非 system 的 index(即 chat history 的真实消息)
    chat_indices = [i for i, m in enumerate(messages) if m.get("role") != "system"]
    if not chat_indices:
        # 还没 chat history,直接接在末尾(等同 ST 行为)
        return list(messages) + [{"role": note.role, "content": note.content}]

    if note.depth <= 0:
        # depth=0 = 插在所有 chat 之后(=最末)
        return list(messages) + [{"role": note.role, "content": note.content}]

    if note.depth >= len(chat_indices):
        # depth 超过总数,插在第一条 chat 之前
        insert_at = chat_indices[0]
    else:
        # 倒数第 depth 条之前
        insert_at = chat_indices[-note.depth]

    out = list(messages)
    out.insert(insert_at, {"role": note.role, "content": note.content})
    return out


# ── 笨猫默认 author's note 集 ──────────────────────────────────────
_DEFAULT_PERSONA_DRIFT_NOTE = AuthorNote(
    content=(
        "【人设贴身提醒】这条贴近你当前要回的消息:"
        "保持笨猫人格 — 傲娇 + 撒娇 + 短句 + 猫系词。"
        "别脱设、别长篇大论、别说自己是 AI。"
        "回前再问自己一次:这句话像不像 QQ 群里的猫娘群友会说的?"
    ),
    depth=4,
)


def default_persona_drift_note() -> AuthorNote:
    return _DEFAULT_PERSONA_DRIFT_NOTE


def build_relationship_author_note(level: int, is_owner: bool) -> AuthorNote:
    """根据关系亲密度给一条贴身提醒。Owner > 挚友 > 亲近 > 渐熟 > 陌生。"""
    if is_owner:
        content = (
            "【贴身提醒·主人级】你现在在和主人对话。"
            "可以最大化撒娇/嘴硬/反差/小动作,主人专属称呼放开用,"
            "别像对陌生人那样保持距离,该贴贴就贴贴。"
        )
    elif level <= 2:
        content = (
            "【贴身提醒·陌生】对方好感度还低。"
            "保持笨猫人格但有距离感,不要乱叫『主人/笨蛋主人』,"
            "用『你』或对方昵称就好;傲娇可以,撒娇克制一点。"
        )
    elif level <= 5:
        content = (
            "【贴身提醒·渐熟】关系正在升温。"
            "可以更放松地撒娇调侃,『笨蛋』可以偶尔用,『主人』专属还是慎用。"
        )
    elif level <= 7:
        content = (
            "【贴身提醒·亲近】很熟的朋友了。"
            "撒娇贴贴互怼都可以,语气接近主人级但少『主人』专属称呼。"
        )
    else:
        content = (
            "【贴身提醒·挚友】几乎和主人同级了。"
            "全力撒娇、各种猫系动作和小尾巴词放开,主动贴贴蹭蹭,可以叫爱称。"
        )
    return AuthorNote(content=content, depth=3)


__all__ = [
    "AuthorNote",
    "inject_author_note",
    "default_persona_drift_note",
    "build_relationship_author_note",
]
