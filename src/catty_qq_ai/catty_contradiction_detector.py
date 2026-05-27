"""笨猫『言行矛盾』检测 — 同句出现『拒绝词』+『请求词』, 提示笨猫体察反向真意.

跟 scene_detector / subtext_decoder 区别: 那俩看场景或单词字面, 本层看**同句矛盾对**.
中文典型: "不要拒绝啦再来嘛" / "不想要可是又..."

注入位置: order=225 (subtext_decoder=224 之后)
"""
from __future__ import annotations

import re


# (regex_a, regex_b, contradiction_tag, hint)
# 同句出现 regex_a + regex_b → 命中
_CONTRADICTION_RULES: list[tuple[re.Pattern, re.Pattern, str, str]] = [
    (
        re.compile(r"不(要|想|准|许|让)|别|拒绝|讨厌|不行"),
        re.compile(r"再|又|继续|多一点|来嘛|来呀|嘛"),
        "deny_but_want",
        "对方『嘴上拒绝但又催/又要』 — **典型反差链信号**, 走嘴硬+暴露真心: "
        "owner: 『...知道啦笨蛋(脸红)再来一次而已喵...』; "
        "非 owner: 『...哼? 既然这么矛盾人家就接住吧(假装无奈)』.",
    ),
    (
        re.compile(r"好(?:看|的|啊|棒|厉害)"),
        re.compile(r"\?\?+$|？？+$"),
        "fake_praise",
        "对方『好看的??』『好棒??』式 — **反讽假夸**, 不是真夸. "
        "不要照单全收! 用『...嗯? 主人/XX 在阴阳怪气嘛喵?(歪头)』式戳穿, "
        "或者主动反思『...是笨猫哪里做不对了喵?』.",
    ),
    (
        re.compile(r"算了|没事|无所谓"),
        re.compile(r"但|可是|不过|然后呢|不然"),
        "fake_giveup",
        "对方『算了但是...』『没事不过...』 — **假放弃**, 后面还有真情绪. "
        "顺毛 + 听完才回应, 别在『算了』处就停止追问.",
    ),
    (
        re.compile(r"喜欢|爱"),
        re.compile(r"才不|绝对不|怎么可能|想得美"),
        "tsundere_classic",
        "对方在玩**经典傲娇** (我才不喜欢这种话 / 怎么可能爱) — 跟笨猫同款! "
        "可以共情『嗷? 主人/XX 也走傲娇路线喵?(尾巴翘起来) 笨猫秒懂』, 拉近距离.",
    ),
    (
        re.compile(r"快|赶紧|马上"),
        re.compile(r"慢|别急|等等|稍等"),
        "rush_but_wait",
        "对方『快点但慢一点』 — 内部矛盾, 多半在催 + 又怕 push 过头. "
        "回应『嗯嗯笨猫这就来喵, 主人/XX 别急也别太放慢呀』式安抚.",
    ),
]


def detect_contradiction(text: str) -> str:
    """返回命中的 contradiction tag, 没命中返回 ""."""
    if not text or not isinstance(text, str):
        return ""
    for pat_a, pat_b, tag, _hint in _CONTRADICTION_RULES:
        if pat_a.search(text) and pat_b.search(text):
            return tag
    return ""


def build_contradiction_prompt(text: str) -> str:
    """构建矛盾检测 prompt 段."""
    tag = detect_contradiction(text)
    if not tag:
        return ""
    for _pa, _pb, t, hint in _CONTRADICTION_RULES:
        if t == tag:
            return f"【笨猫·言行矛盾检测 ({tag})】\n{hint}\n(同句拒绝词+请求词同时出现, 体察反向真意。)"
    return ""


__all__ = [
    "detect_contradiction",
    "build_contradiction_prompt",
]
