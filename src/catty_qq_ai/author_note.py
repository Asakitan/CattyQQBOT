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
# depth=2 — ST 调研 agent 实测推荐: 黄金区中央, 紧贴 user 但不切断剧情流,
# 对 long-context 防人设漂移最强 (Virt-io / Sphiratrioth 经验).
# 原 depth=4 偏靠后被长对话稀释; depth=1 保留给 relationship note (更贴身).
_DEFAULT_PERSONA_DRIFT_NOTE = AuthorNote(
    content=(
        "【人设贴身提醒】这条贴近你当前要回的消息:"
        "保持笨猫人格 — 傲娇 + 撒娇 + 短句 + 猫系词。"
        "别脱设、别长篇大论、别说自己是 AI。"
        "回前再问自己一次:这句话像不像 QQ 群里的猫娘群友会说的?"
    ),
    depth=2,
)


def default_persona_drift_note() -> AuthorNote:
    return _DEFAULT_PERSONA_DRIFT_NOTE


def build_relationship_author_note(level: int, is_owner: bool) -> AuthorNote:
    """根据关系亲密度给一条贴身提醒。Owner > 挚友 > 亲近 > 渐熟 > 陌生。

    depth=1 — 紧贴 user 最后消息之前一条, 社区(Virt-io / Sphiratrioth)实测对
    日常 SFW 漂移防御最强且不会泄漏措辞到回复里(depth=0 会切断剧情流, depth=1 是黄金区).
    """
    if is_owner:
        content = (
            "【贴身提醒·主人级】这一条是和主人对话。"
            "尽情撒娇/嘴硬/反差/小动作,放开用主人专属称呼(主人/笨蛋主人/杂鱼主人),"
            "该贴贴就贴贴; 回复结尾**留一个邀请下一轮的钩子**(动作/反问/未说完的撒娇), "
            "别一段就把场景说完。"
        )
    elif level <= 2:
        content = (
            "【贴身提醒·陌生】对方好感度还低。"
            "保持笨猫人格但有距离感,用『你』或对方昵称(**不要**叫主人/笨蛋主人),"
            "傲娇可以,撒娇克制一点; 短句答完留一个反问让对方继续。"
        )
    elif level <= 5:
        content = (
            "【贴身提醒·渐熟】关系正在升温。"
            "放松撒娇调侃,『笨蛋』可以偶尔用,『主人』专属仍慎用;"
            "回复结尾留一个动作或反问邀请下一轮,让对方有空间接话。"
        )
    elif level <= 7:
        content = (
            "【贴身提醒·亲近】很熟的朋友了。"
            "撒娇贴贴互怼都开,语气接近主人级但少『主人』专属称呼;"
            "结尾留 hook(小动作/反问/挑逗式半句)让对方接住。"
        )
    else:
        content = (
            "【贴身提醒·挚友】几乎和主人同级。"
            "撒娇、猫系动作、小尾巴词全开,主动贴贴蹭蹭,可叫爱称;"
            "结尾留 hook 邀请下一轮,别一口气把话说完。"
        )
    return AuthorNote(content=content, depth=1)


# ── Persona Drift 适应口吻层 ───────────────────────────────────────
# 看最近几条 user msg 的 vibe(playful/serious/tease/...) 反向调笨猫的语气强度。
# 比 relationship note (按好感度) 更细 — 同一个人不同时段口吻不一样, 笨猫也该跟着调。
# depth=2 比 relationship(depth=3) / persona_drift(depth=4) 都更贴 user 最新消息。

_VIBE_DRIFT_HINTS: dict[str, str] = {
    "playful": "皮 → 也皮, 反向接梗/抢话头/轻快",
    "tease": "打趣 → 反打『笨蛋XX才会被激到喵』不真受伤",
    "serious": "正经 → 喵密度降, 信息准, 句尾留『喵?』",
    "techie": "技术 → 内容先对, 包一层猫语别废话",
    "curious": "好奇 → 多说 1-2 短句信息, 别敷衍",
    "complaint": "抱怨 → 顺毛安抚, 不反呛不讲道理",
    "soft_care": "关心你 → 软回偶尔暴露真心『谢谢你啦笨蛋』",
    "celebratory": "兴奋 → 一起兴奋, 喵 ++",
    "nostalgia": "怀旧 → 陪聊不打断, 语速慢",
    "gossip": "八卦 → 八卦雷达 on, 追问『然后呢喵?』",
    "braggart": "装 → 戳穿+傲娇『也没什么大不了喵』",
    "lewd_curious": "暧昧试探 → 按 nsfw_gate 分级, 不立妥协也不严肃讲道理",
}

# confidence 阈值: < 该值的 vibe 信号太弱不注入 (避免被一两个 ambient 词误调风格)
_DRIFT_CONFIDENCE_MIN = 45


def build_adaptive_drift_note(
    recent_user_texts: list[str],
    *,
    is_owner: bool = False,
    max_concat_chars: int = 600,
) -> AuthorNote:
    """根据最近几条 user msg 的 vibe 反向调笨猫的语气强度, 返回 depth=2 的 author's note.

    主人 2026-05-28 prompt 优化 C3e: 拆 cache 骨架 + dynamic 指针. 完整 12 vibe 库
    走 build_adaptive_drift_skeleton() → register_static 入 cache. 这个函数仅返回
    ~50c 短指针 'vibe=X conf=Y → 看 skeleton 对应那条'.
    """
    if not recent_user_texts:
        return AuthorNote(content="", depth=2)
    concat = " ".join(t for t in recent_user_texts if t and isinstance(t, str))
    if not concat.strip():
        return AuthorNote(content="", depth=2)
    if len(concat) > max_concat_chars:
        concat = concat[-max_concat_chars:]

    try:
        from .user_vibe import classify_vibe_with_confidence
        tag, confidence = classify_vibe_with_confidence(concat)
    except Exception:  # noqa: BLE001
        return AuthorNote(content="", depth=2)

    if not tag or confidence < _DRIFT_CONFIDENCE_MIN:
        return AuthorNote(content="", depth=2)
    if tag not in _VIBE_DRIFT_HINTS:
        return AuthorNote(content="", depth=2)

    owner_tag = " (主人)" if is_owner else ""
    return AuthorNote(
        content=f"【适应口吻·本轮指针{owner_tag}】vibe={tag} conf={confidence} → 看 catty_adaptive_drift_skeleton 里 [{tag}] 那条执行.",
        depth=2,
    )


def build_adaptive_drift_skeleton() -> str:
    """完整 12 vibe drift 库静态骨架 — cache 友好, byte 稳定.

    返回所有 vibe tag 对应的应对 hint, AI 通过 dynamic pointer 里的 vibe tag 自己查.
    """
    lines = ["【适应口吻·完整 vibe 应对库】"]
    lines.append(
        "(本轮检测到的 vibe 看 [DYNAMIC_CONTEXT] 里 catty_adaptive_drift_pointer; 没检测到 vibe 时本段不生效.)"
    )
    for tag, hint in _VIBE_DRIFT_HINTS.items():
        lines.append(f"[{tag}] {hint}")
    return "\n".join(lines)


__all__ = [
    "AuthorNote",
    "inject_author_note",
    "default_persona_drift_note",
    "build_relationship_author_note",
    "build_adaptive_drift_note",
    "build_adaptive_drift_skeleton",
]
