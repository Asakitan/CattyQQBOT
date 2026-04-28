from __future__ import annotations

import random
import re
import time


FEATURE_DIRECT_KEYWORDS = (
    "联网搜索",
    "上网搜索",
    "上网查",
    "网上查",
    "搜一下",
    "搜索一下",
    "帮我搜",
    "搜点",
    "搜",
    "搜一点",
    "搜些",
    "帮我查",
    "查一下",
    "查找一下",
    "百度一下",
    "谷歌一下",
    "海龟汤",
    "星痕共鸣",
    "blue protocol",
    "star resonance",
)

_SEARCH_PATTERNS = (
    r"^(?:(?:猫猫|笨猫|喵)[，,、\s]*)?(?:(?:帮|替|给)我)?(?:联网|上网|网上)?(?:搜索一下|搜索|搜一下|搜一搜|搜搜|搜点|搜一点|搜些|搜|查一下|查下|查查|查找一下|查找|百度一下|谷歌一下)\s*",
    r"(?:联网|上网|网上)(?:搜索|搜|查)\s*",
)

TURTLE_SOUPS = (
    {
        "title": "雨夜的伞",
        "question": "一个人下雨天没有带伞，走了很久却一点都没淋湿。为什么？",
        "answer": "他走在有顶棚的长廊里，雨一直在外面下。",
    },
    {
        "title": "空杯子",
        "question": "桌上有一个空杯子，旁边的人看了一眼就哭了。为什么？",
        "answer": "杯子是逝者生前常用的杯子，今天本该有人继续倒水给他。",
    },
    {
        "title": "迟到的车票",
        "question": "他买到了一张车票，却因为车票太准时而失去了工作。为什么？",
        "answer": "他是车站报时员，车票证明他偷偷离岗坐车，且列车准点到达暴露了时间。",
    },
    {
        "title": "最后一盏灯",
        "question": "房间里最后一盏灯灭了，所有人反而松了一口气。为什么？",
        "answer": "他们在拍恐怖片，最后一盏灯灭代表镜头顺利完成。",
    },
    {
        "title": "不能打开的门",
        "question": "她站在自家门口，却一直不敢开门，直到听见陌生人的脚步声才开了。为什么？",
        "answer": "她是逃生游戏工作人员，要等下一组玩家到门口才开门继续演出。",
    },
)


def extract_web_search_query(text: str) -> str:
    clean = text.strip()
    if not clean:
        return ""
    lowered = clean.lower()
    if not any(keyword in lowered for keyword in ("联网", "上网", "网上", "搜索", "搜", "查一下", "查下", "查找", "百度", "谷歌")):
        return ""
    query = clean
    for pattern in _SEARCH_PATTERNS:
        query = re.sub(pattern, "", query, count=1, flags=re.IGNORECASE).strip()
    query = re.sub(r"^(?:一点|一些|点|些)\s*", "", query, count=1, flags=re.IGNORECASE).strip()
    query = query.strip(" ：:，,。！？!?；;\"'“”‘’")
    return query or clean


def is_turtle_soup_request(text: str) -> bool:
    normalized = text.strip().lower()
    return "海龟汤" in normalized or "海龜湯" in normalized


def format_duration_cn(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds >= 3600:
        hours = seconds // 3600
        minutes = (seconds % 3600 + 59) // 60
        return f"{hours}小时{minutes}分钟"
    if seconds >= 60:
        return f"{(seconds + 59) // 60}分钟"
    return f"{seconds}秒"


def turtle_soup_cooldown_key(group_id: object | None) -> str:
    return f"group:{group_id}" if group_id is not None else "private"


def turtle_soup_remaining(
    cooldowns: dict[str, float],
    key: str,
    *,
    now: float | None = None,
    cooldown_seconds: float,
) -> float:
    now = time.monotonic() if now is None else now
    last = cooldowns.get(key, 0.0)
    return max(last + max(cooldown_seconds, 0.0) - now, 0.0)


def choose_turtle_soup(key: str) -> str:
    soup = random.Random(f"{key}:{int(time.time()) // 300}").choice(TURTLE_SOUPS)
    return (
        f"海龟汤《{soup['title']}》\n"
        f"题面：{soup['question']}\n"
        "规则：只能问能用“是/否/无关”回答的问题，答案人家先藏起来喵。"
    )


def search_cooldown_key(user_id: object) -> str:
    return f"user:{user_id}"
