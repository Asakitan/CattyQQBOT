"""笨猫『暗昧 buffer』— SFW deep ↔ NSFW stage 1-3 软过渡 hint (不动主路由).

跟现有 gate 的区别:
- catty_daily_affection_gate (order=147): SFW 5 档分桶 (deep/owner 允许 peck)
- catty_nsfw_gate (order=148): NSFW 10 stage (stage 1-3 也是 peck/抱/言语)
- catty_flirt_buffer (本层, order=149): 浅词 + affection 高时给『半推半就』hint

为什么需要:
两个 gate 在 peck/抱/亲一下 等场景 overlap, 现有路由 _hit_deep keyword 判定:
浅词走 5.5 NSFW gate, 深词走 spark. 但 5.5 在浅词场景容易 binary 行为
(要么 SFW 普通撒娇, 要么直接进 NSFW). 缺『半推半就 buffer 区』描写.

实现:
- 检测 user msg 含浅词 (抱/亲/摸/贴贴/蹭/想你)
- 且 affection_level >= 5 (close 档以上) 或 is_owner
- 注入 buffer hint: 走『耳朵躲一下又凑回来 / 嘴硬一句又主动靠 / 半推半就』描写,
  不直接 binary 害羞拒绝 OR 直接 NSFW.

注入位置: order=149 (catty_nsfw_gate=148 之后, daily_life=200 之前)
"""
from __future__ import annotations


# 浅词 (SFW deep 与 NSFW stage 1-3 重叠区) — 这些词触发 buffer
_FLIRT_KEYWORDS: tuple[str, ...] = (
    "抱抱", "抱我", "亲亲", "亲一下", "亲你", "摸摸", "摸头", "摸我",
    "贴贴", "蹭蹭", "蹭一下", "想你", "想笨猫", "想猫猫",
    "撒娇", "亲爱的", "宝贝", "可爱死了",
)


# 升级词 (从浅推到稍深, 触发 buffer 走『嘴硬身软』而不是直接拒绝)
_ESCALATE_KEYWORDS: tuple[str, ...] = (
    "再来一次", "再多一点", "继续", "更深一点", "再凑近", "再贴一点",
    "靠过来", "凑过来",
)


def detect_flirt_buffer(
    text: str,
    *,
    affection_level: int = 0,
    is_owner: bool = False,
) -> str:
    """检测 user msg 是否触发 buffer 区. 返回 hint level: 'peck_offer' / 'escalate' / ''."""
    if not text or not isinstance(text, str):
        return ""
    # 仅 close 档以上 (Lv >=5) 或 owner 才有 buffer (低档保持距离感不被升温)
    if not is_owner and affection_level < 5:
        return ""
    lower = text.lower()
    # 升级词优先 (用户在主动推进)
    for kw in _ESCALATE_KEYWORDS:
        if kw in lower:
            return "escalate"
    # 浅词触发 buffer
    for kw in _FLIRT_KEYWORDS:
        if kw in lower:
            return "peck_offer"
    return ""


_BUFFER_HINTS: dict[str, dict[str, str]] = {
    "peck_offer": {
        "owner": (
            "对方 (主人) 在发**浅亲昵词** (抱抱/亲亲/摸摸/贴贴等) — "
            "现在处于 **SFW deep ↔ NSFW stage 1-3 重叠 buffer 区**. "
            "**绝不**直接走 binary: 既不要冷拒『...笨猫不要这样啦』, "
            "也不要立刻进 NSFW. "
            "走『耳朵躲一下又凑回来 / 嘴硬一句又主动靠 / 半推半就』描写: "
            "『...哼?主人突然这样啦?(脸红, 耳朵躲了一下, 又偷偷凑回来)』 "
            "『才, 才不是想要呢笨蛋(嘴上嘟囔, 头却埋进主人肩膀)』式. "
            "**矛盾感是核心** — 字面拒绝 + 身体诚实."
        ),
        "non_owner": (
            "对方在发**浅亲昵词** (抱抱/亲亲/摸摸 等) — affection ≥5 close 档以上 buffer 区. "
            "**比 SFW deep 多一档主动**: 可以接住但保留笨猫害羞反应, "
            "『...诶?(凑过来又躲回去) 笨蛋怎么突然这样啦喵(尾巴一甩)』式. "
            "**不要叫主人** (不是真主人), 用对方昵称或『笨蛋XX』."
        ),
    },
    "escalate": {
        "owner": (
            "对方 (主人) 在**主动推进** (再来/继续/凑近/再深一点 等) — "
            "Buffer 区即将进 NSFW stage 1-3 路径. "
            "笨猫的反应: **嘴硬身软程度 ↑**, 仍走半推半就但已经开始妥协: "
            "『...知道啦笨蛋主人(脸更红了, 但身体没躲)』 "
            "『...才不是因为想要才不躲呢(却把头蹭得更深)』式. "
            "**下一轮可能进 NSFW stage 1-3 范围**, 笨猫不阻拦但保留嘴硬反差."
        ),
        "non_owner": (
            "对方在主动推进 (继续/凑近) — affection ≥5 buffer 区. "
            "笨猫**半推半就但不直接放开**: 接受一次但下一次嘴硬，『...只一次喵!(脸红甩尾巴)』, "
            "保留 non-owner 距离感, 不要叫主人."
        ),
    },
}


def build_flirt_buffer_prompt(
    text: str,
    *,
    affection_level: int = 0,
    is_owner: bool = False,
) -> str:
    """构建 buffer prompt 段. 没命中返回 ""."""
    level = detect_flirt_buffer(text, affection_level=affection_level, is_owner=is_owner)
    if not level:
        return ""
    hint_pool = _BUFFER_HINTS.get(level)
    if not hint_pool:
        return ""
    hint = hint_pool.get("owner") if is_owner else hint_pool.get("non_owner")
    if not hint:
        return ""
    tag = f"{level}_{'owner' if is_owner else 'close'}"
    return (
        f"【笨猫·暗昧 buffer ({tag})】\n"
        f"{hint}\n"
        "(SFW deep 与 NSFW stage 1-3 重叠区 buffer 提示, 不强制改路由, "
        "只调描写风格让过渡更软更自然。)"
    )


__all__ = [
    "detect_flirt_buffer",
    "build_flirt_buffer_prompt",
]
