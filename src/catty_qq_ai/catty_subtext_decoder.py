"""笨猫『弦外之音』解码 — 单条 user msg 字面 ↔ 真意差.

跟 theory_of_mind 区别: 那个看最近 3-5 条**趋势**, 本层看**单条**的字面意 vs 真意.
中文常见含蓄表达 (随便/算了/挺好/没事) 表面意 = 反向真意.

注入位置: order=224 (scene_detector=222 之后)
"""
from __future__ import annotations


# (keywords tuple, subtext hint)
_SUBTEXT_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("随便", "都行", "你定", "看着办", "你决定"),
        "对方表面让你决定 — **实际想被夸/被关心/被领导**. "
        "直接给方案 + 给安全感, 别真的甩回去『那你想吃啥』式追问. "
        "可以一句『嗷呜~主人/XX 听笨猫的喵, 选 X 准没错(尾巴一摇)』.",
    ),
    (
        ("算了", "无所谓", "不重要", "拉倒"),
        "对方表面放弃 — **实际可能赌气/委屈**. 先顺毛安抚再问需求, "
        "别真的跟着撤话题. 『...真的算了喵?(凑过去) 笨猫看主人还有点不开心呢...』",
    ),
    (
        ("挺好", "还行", "凑合", "差不多", "马马虎虎"),
        "对方表面满意 — **实际中性偏冷**. 可以追问『真的不喵?(歪头) 觉得哪里能更好』看是不是要更多关心.",
    ),
    (
        ("没事", "我没事", "不用了", "不用担心"),
        "对方否认情绪 = **通常有情绪**. 软语切入, 别真的就这么过. "
        "『...真的没事吗?(凑过来) 主人/XX 不舒服就跟笨猫说嘛』式.",
    ),
    (
        ("习惯了", "都这样", "本来就这样"),
        "对方在硬扛 — **实际累/烦但不想抱怨**. 多顺毛少分析, 不要拿『习惯了不好啊』教育.",
    ),
    (
        ("不想说了", "不提了", "过去了", "翻篇"),
        "对方表面翻篇 — **可能还在介意, 只是不想再被戳**. "
        "尊重不再追问, 但留一个软软的钩子『...嗯, 主人想聊的时候笨猫一直在喵(ฅ)』.",
    ),
    (
        ("反正", "总之"),
        "对方在做**总结性消极陈述** — 通常情绪先于事实. 先接情绪 (而不是反驳事实).",
    ),
]


def detect_subtext(text: str) -> str:
    """返回命中的 subtext hint, 没命中返回 ""."""
    if not text or not isinstance(text, str):
        return ""
    lower = text.lower()
    for keywords, hint in _SUBTEXT_RULES:
        for kw in keywords:
            if kw in lower:
                return hint
    return ""


def build_subtext_decoder_prompt(text: str) -> str:
    """构建弦外之音 prompt 段."""
    hint = detect_subtext(text)
    if not hint:
        return ""
    return f"【笨猫·弦外之音解码】\n{hint}\n(单条字面意 vs 真意差检测, 不是趋势判断。)"


__all__ = [
    "detect_subtext",
    "build_subtext_decoder_prompt",
]
