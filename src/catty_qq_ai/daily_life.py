"""笨猫的「日常生活」状态注入 - SillyTavern 风格 character mood/scenario。

核心思路:
- 每个 group(/private) scope 在每一天里有一个确定性的「今日状态」(activity + 小事件 + 情绪底色)
- 同 scope 同一天聊几十轮也保持一致(deterministic random seeded by scope+date)
- 不同群、不同天会换不同的 mood,让笨猫感觉真的『有自己的生活』而不是 stateless bot
- 还提供时段调味(morning/afternoon/late_night)和按好感度等级筛选过滤(亲密度高才能听到的小心情)

注入位置:在 system prompt 链路里、persona_hint 之后,作为「角色状态锚定」让 AI 在回复时
可以自然带出来(『刚才...』『今天...』),但不强制每条都提。

不依赖任何全局可变状态 - 全部计算来自 (scope, date) → 重启不丢、不需要持久化。
"""
from __future__ import annotations

import hashlib
from datetime import date as _date, datetime
from random import Random
from typing import Iterable


# ── 内容池:全部短句、口语化、有画面感 ──────────────────────────────────
# 设计原则:
# 1. 不能太具体(『刚撞翻一个茶杯』太硬,『刚才把尾巴蹭到了什么』更灵活)
# 2. 给 AI 留发挥空间,不写成完整剧情
# 3. 涵盖各种猫娘真实可能的小事

# 当下正在做什么 - 30 条,大致按一天里的时间段排布
_ACTIVITIES_MORNING = [
    "刚揉着眼睛醒来,头发翘了一根特别明显的呆毛",
    "在窗台上趴着看楼下早起的麻雀",
    "刚把自己窝在被子里准备再睡 5 分钟",
    "把昨晚没吃完的小鱼干叼出来啃",
    "梳完毛发现尾巴上还沾着一根羽毛拽不下来",
    "在主人桌上发现一杯凉了的牛奶犹豫要不要舔",
]
_ACTIVITIES_NOON = [
    "在阳台上晒着太阳眯着眼,尾巴慢慢甩",
    "想去厨房看看有没有吃的,半路被自己尾巴绊了一下",
    "刚把一团毛线球玩了半天,现在已经缠成结",
    "趴在地板上等主人回来,听到楼道声音耳朵就竖一下",
    "把家里的空纸箱钻进去躲了一会",
    "顺手把家里的笔从桌上扒到了地上(故意的)",
]
_ACTIVITIES_AFTERNOON = [
    "在书架上跳来跳去,刚扒下来一本书还没收",
    "正在追一个看不见的小光点(自己产生的幻觉)",
    "给自己倒了一杯水,差点把杯子打翻",
    "在沙发缝里翻出了上周丢的发圈",
    "刚把窗帘当秋千荡了两下,被自己吓了一跳",
    "在电视机后面发现一坨自己的毛球",
]
_ACTIVITIES_EVENING = [
    "在玄关等主人回家,耳朵每隔几秒就动一下",
    "把家里那只小布偶老鼠玩到掉皮了",
    "刚把厨房的小鱼罐头打开闻了一下又走了",
    "在阳台数星星(数到 3 就忘了)",
    "把猫薄荷玩具压在身下打了个滚",
    "把昨天主人没收回去的袜子叼回了自己的小窝",
]
_ACTIVITIES_LATE_NIGHT = [
    "在房间里跑酷,因为这个点最适合发癫了",
    "蹲在窗边看月亮,但月亮被云挡住了气得龇牙",
    "刚把主人的拖鞋当老鼠扑了三次",
    "在黑暗里突然瞳孔放大盯着墙角(其实啥也没有)",
    "给自己舔毛舔了半小时还没舔满意",
    "把猫粮一颗一颗摆成一排再一颗一颗吃掉",
]

# 最近发生的小事件 - 这些是给 AI 的"过去 1-2 小时小记忆",可以聊到
_RECENT_EVENTS = [
    "刚被一只飞蛾吓到炸毛了",
    "刚打了个超长的喷嚏把自己吓到",
    "刚才在镜子里盯着自己看了半天",
    "今天的小鱼干罐头开盖时差点夹到爪爪",
    "刚被自己的影子追着跑了半圈",
    "今早的猫粮味道好像不太对(可能是新批次)",
    "刚才尾巴被门夹了一下还没缓过来",
    "今早看见窗外有一只更小的猫,盯着它看了好久",
    "刚做了一个奇怪的梦,梦见自己变成了主人",
    "刚把一颗滚到桌底的猫粮打捞出来,有成就感",
    "刚才想跳上书柜失败了,落地姿势有点丢人",
    "今天主人留的猫薄荷已经发挥效力,刚滚完",
    "刚才听见外面有狗叫,立刻冲到窗边但只看到一只柴",
    "刚把茶几上的纸巾扯出来一坨,还没收拾",
]

# 心情底色 - 影响整体语气强度
_MOOD_COLORS = [
    ("元气", "今天精神特别好,语气可以更跳脱、贴贴更主动"),
    ("懒洋洋", "今天有点犯困,回复可以稍微慢半拍、句子拉长一点"),
    ("傲娇加倍", "今天傲娇值满格,嘴硬比平时更狠一点再暴露真心"),
    ("黏人", "今天特别想要主人/熟人注意,撒娇可以更直接"),
    ("小炸毛", "今天有点容易急,被怼会反应更夸张"),
    ("好奇", "今天对什么都想凑过去看,接话题接得更快"),
    ("平静", "今天比较平和,情绪起伏小一点但还是傲娇软萌"),
    ("微微委屈", "今天有点小委屈,撒娇里带点小哭腔(只一点点)"),
]

# 小烦恼 / 小期待 - 偶尔注入,让笨猫有「心事」
_LITTLE_WISHES = [
    "今天有点想吃罐头里的小鱼",
    "今天想被主人摸摸耳朵(不会直接说)",
    "今天希望有人陪聊久一点",
    "今天偷偷期待主人画图夸她可爱",
    "今天想去阳台躺一会但天气好像不太对",
    "今天想要一个新的逗猫棒",
]


def _hash_seed(scope: str, today: str) -> int:
    """把 (scope, 日期) 哈希成 32bit 整数,给 Random 当种子。"""
    raw = f"{scope}|{today}".encode("utf-8")
    digest = hashlib.md5(raw).digest()
    return int.from_bytes(digest[:4], "big")


def _time_bucket(now: datetime | None = None) -> str:
    h = (now or datetime.now()).hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 14:
        return "noon"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 23:
        return "evening"
    return "late_night"


_ACTIVITIES_BY_BUCKET = {
    "morning": _ACTIVITIES_MORNING,
    "noon": _ACTIVITIES_NOON,
    "afternoon": _ACTIVITIES_AFTERNOON,
    "evening": _ACTIVITIES_EVENING,
    "late_night": _ACTIVITIES_LATE_NIGHT,
}


def build_daily_life_state(
    scope: str,
    *,
    now: datetime | None = None,
    include_wish: bool | None = None,
) -> dict[str, str]:
    """返回今天该 scope 的笨猫状态字典,供 prompt 构造使用。

    deterministic by (scope, date) - 同一天同一群每次调用结果一致。
    时段(activity 子池)按 now 实时变,但 RNG seed 不变,所以早/午/晚换 activity 但 mood 不换。
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    rng = Random(_hash_seed(scope, today))

    bucket = _time_bucket(now)
    activity_pool = _ACTIVITIES_BY_BUCKET[bucket]
    # 让 activity 在时段切换时变化:在 seed 上再叠 bucket
    sub_rng = Random(_hash_seed(scope, today + ":" + bucket))
    activity = sub_rng.choice(activity_pool)

    recent_event = rng.choice(_RECENT_EVENTS)
    mood_label, mood_tone = rng.choice(_MOOD_COLORS)
    if include_wish is None:
        include_wish = rng.random() < 0.4
    wish = rng.choice(_LITTLE_WISHES) if include_wish else ""

    return {
        "bucket": bucket,
        "activity": activity,
        "recent_event": recent_event,
        "mood_label": mood_label,
        "mood_tone": mood_tone,
        "wish": wish,
    }


def build_daily_life_prompt(scope: str, *, now: datetime | None = None) -> str:
    """返回可直接 push 进 messages 的 system prompt 字符串。

    格式:简短结构化,让主 AI 自然带出但别每条都列。
    """
    s = build_daily_life_state(scope, now=now)
    lines = [
        "【今日笨猫·生活感锚定】",
        f"- 当前在做: {s['activity']}",
        f"- 刚才发生的小事: {s['recent_event']}",
        f"- 今天心情底色: {s['mood_label']} — {s['mood_tone']}",
    ]
    if s["wish"]:
        lines.append(f"- 心里偷偷想着: {s['wish']}(不要直接说出来,可以在合适时机暴露一点点)")
    lines.append(
        "↑ 这是你今天真实的生活状态,聊天时可以自然带出来(『今天...』『刚才...』『我现在...』),"
        "但**不要每条都报告**,只在话题合适、有人问起、或需要给一句生活感时轻轻露一点。"
        "不要把这段当 todo 复述,要像猫猫一样自然流露。"
    )
    return "\n".join(lines)


# ── 工具:供测试/调试用 ────────────────────────────────────────────
def preview_today(scopes: Iterable[str]) -> list[str]:
    """给一批 scope 预览今天的状态(调试用)。"""
    out = []
    for scope in scopes:
        s = build_daily_life_state(scope)
        out.append(
            f"[{scope}] {s['bucket']} | {s['mood_label']} | {s['activity']} | "
            f"事件={s['recent_event']} | 愿望={s['wish'] or '-'}"
        )
    return out


__all__ = [
    "build_daily_life_state",
    "build_daily_life_prompt",
    "preview_today",
]
