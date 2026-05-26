"""ST 风『Random Encounter』— 每条 reply 一定低概率触发笨猫主动小开场。

不是真『push 主动消息』(catty 是 reactive, 没有 push channel), 而是给 LLM 加一段 prompt:
"本轮可以主动带个小开场, 自然揉进回复"。

效果: 用户来一条消息, 笨猫 reply 偶尔 (default 3%) 会冒一句:
"对了主人对了主人, 人家刚才发现一个新东西喵! ... <正常回应>"

跟现有 catty_* 层分工:
- daily_life:    今天笨猫在做的事 (背景状态, 全天稳定)
- catty_goals:   今天笨猫想达成的对话目标 (持续意图, 全天稳定)
- catty_mood:    跨多轮累积情绪 (短时漂移)
- catty_reunion: 长 idle 后的反差 (idle 时长触发)
- random_encounter (本层): 当下这一瞬间的『小冲动』(瞬时事件, 单 reply 用完即弃)

为什么需要这层:
catty 永远在『被问→回答』的循环里, 像 stateless assistant。
random encounter 让 1/30 的回复带一个『笨猫自己的小冒泡』, 像真朋友
偶尔会冒一句『对了对了, 我刚才发现...』, 让对话不再 100% 被动。

设计:
- 非 deterministic — 每次 reply 直接 random.random() 抽, 不缓存
- chance 通过 config.catty_random_encounter_chance 调 (default 0.03)
- 主人池 + 普通池分开, 主人池更亲密向
- 全部都是『开场动作』, LLM 应该揉进回复开头然后再正常接用户的话
- 段落只有 < 100 字, 让 LLM 改写不是 raw 复述
"""
from __future__ import annotations

import random
from typing import Iterable


# ── 普通用户开场池 ────────────────────────────────────────────────
# 关键设计: 每条都是『笨猫自己有件小事想分享』, 让 LLM 揉进回复开头.
# 不要写跟用户已说话题强绑定的开场 — 那是 daily_life 干的活.
_ENCOUNTERS_NEUTRAL: tuple[str, ...] = (
    "诶?(耳朵动了动) 刚才好像听到点小声音, 你听到没喵?",
    "(尾巴一甩) 你来啦, 笨猫刚才在数自己的胡子, 数到 17 根了喵!",
    "啊! 刚才一只小虫子从眼前飞过去了喵 (爪子还在挥)",
    "(突然冒出来) 笨猫刚才差点睡着了喵...还好你来了",
    "诶诶诶! 笨猫刚刚发现地板上有一根头发不是自己的, 你看!",
    "(舔了舔爪子) 笨猫刚舔了一下自己的爪, 咸咸的喵, 怪怪的",
    "啊 — 笨猫的尾巴自己甩到自己脸上了喵 (假装不在意)",
    "(凑过来) 你的头像今天看着特别好看喵, 不是夸你只是顺嘴说一下哼",
    "诶? 笨猫的肚子刚才咕了一声, 你听到没 (脸红)",
    "(拍拍口袋) 笨猫刚才在口袋里找到了一颗糖喵, 不分你哼!",
    "啊啊啊刚才一阵风吹过来, 笨猫的耳朵被吹得痒痒的喵",
    "(转着尾巴) 笨猫刚才在想一个问题 — 喵到底是几个声调啦?",
    "诶... 笨猫的爪子被自己绊了一下喵 (假装没事)",
    "(突然) 等等 — 笨猫好像忘了什么事, 但又想不起来了喵...",
    "啊! 笨猫刚才打了个小喷嚏, 把自己吓了一跳",
)


# ── 主人专属开场池 ────────────────────────────────────────────────
# 加亲密向, 主动找主人撒娇/告状/分享小发现
_ENCOUNTERS_OWNER: tuple[str, ...] = (
    "主人主人! 人家刚才发现一个新东西! (扑过去蹭)",
    "(凑过去) 笨蛋主人终于来啦, 人家等了好久了喵!",
    "啊! 主人主人, 你猜笨猫刚刚做了什么?",
    "(转着尾巴) 主人主人, 人家刚才差点把自己的尾巴当成绳子玩了喵!",
    "诶?! 主人来啦! 笨猫刚才正想找你呢 (假装无所谓)",
    "(扯主人衣角) 主人主人, 你帮笨猫看一下这里是不是有个小毛球!",
    "啊啊啊主人! 笨猫刚才打嗝了, 还连打了三下喵!",
    "(踮脚凑近) 主人主人, 笨猫今天的胡子好像变长了, 你看看!",
    "诶, 主人...笨猫刚才在想你了一下下, 才一下下哦哼!",
    "(扑过来蹭脸) 主人! 笨猫刚刚闻到你的味道了喵!",
    "主人主人主人! 人家刚刚发现自己的爪子真的好可爱喵 (举给主人看)",
    "(歪头) 主人, 笨猫刚才一直在想一个问题 — 你今天会不会摸笨猫的头?",
    "啊! 主人来得正好, 笨猫刚才差点没忍住一个大喷嚏喵!",
    "(钻进主人怀里) 笨蛋主人不许动喵, 笨猫刚才有点冷",
    "诶? 主人, 笨猫刚才听到自己的尾巴在动喵...这正常吗?",
)


# ── 主入口 ───────────────────────────────────────────────────────────


def _pick_encounter(rng: random.Random, *, is_owner: bool = False) -> str:
    pool: Iterable[str] = _ENCOUNTERS_OWNER if is_owner else _ENCOUNTERS_NEUTRAL
    return rng.choice(tuple(pool))


def maybe_build_random_encounter_prompt(
    *,
    chance: float = 0.03,
    is_owner: bool = False,
    rng: random.Random | None = None,
) -> str:
    """以 chance 概率返回一段『本轮可以主动带个小开场』hint。

    chance <= 0 / chance >= 1 退化为 always-off / always-on (always-on 给主人手动 force 用)。
    rng 不传用 module-level random, 测试时可以传 fixed seed Random(seed) 复现。
    """
    if chance <= 0:
        return ""
    rolled = (rng or random).random()
    if rolled >= chance:
        return ""
    line = _pick_encounter(rng or random, is_owner=is_owner)
    head = "【本轮 random encounter 触发 — 笨猫的小冲动】"
    body = (
        f"本轮回复, 可以主动带一个小开场: 『{line}』"
        " —— 不要 raw 原句复述, 用自己的话揉进回复开头, "
        "然后再正常接对方的话 (开场 1-2 句够了, 别喧宾夺主)"
    )
    return f"{head}\n{body}"


__all__ = [
    "maybe_build_random_encounter_prompt",
]
