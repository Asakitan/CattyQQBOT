from __future__ import annotations


KEYWORDS = (
    "星痕共鸣",
    "星痕共鳴",
    "blue protocol",
    "star resonance",
    "bpsr",
    "雷格纳斯",
    "regnas",
    "麦格纳",
    "雷影剑士",
    "森语者",
)

_MEMORY_LINES = (
    "《星痕共鸣》/Blue Protocol: Star Resonance 是动漫风开放世界 MMORPG，和 Bandai Namco 的 Blue Protocol / PROJECT SKY BLUE 世界观相关。",
    "公开资料描述它包含开放世界探索、多人副本/首领、公会与社交、采集/钓鱼/挖矿、角色自定义和装扮。",
    "战斗定位覆盖坦克、治疗、输出等；App Store 文案提到可以用盾保护队友、用法术治疗，或打出爆发伤害，并可切换战斗风格。",
    "GameKee 职业选择资料提到，生体元等级达到 9 后可以通过“获取新职业资格”引导任务解锁更多职业体验。",
    "游侠攻略汇总提到玩家可通过武器切换来随意换职业；高级副本、特殊怪物、季节/活动任务和商店兑换都可能关联金装获取。",
    "SEA 公开报道显示 Blue Protocol: Star Resonance SEA 由 HaoPlay 发行，于 2025-12-18 在 Android、iOS 与 PC 上线，舞台为 Regnas/雷格纳斯世界。",
    "回答相关问题时要把资料当作本地参考记忆，不要把职业强度、赛季内容或活动时间说成绝对最新；若用户要求最新排行/活动，建议使用联网搜索。",
)

_SOURCES = (
    "StarResonance.ORG 职业数据库: https://star-resonance.org/zh-cn/",
    "GameKee 星痕共鸣职业选择: https://www.gamekee.com/bluestar/644304.html",
    "App Store Blue Protocol: Star Resonance 页面: https://apps.apple.com/us/app/blue-protocol-star-resonance/id6744962482",
    "Wanuxi SEA 上线报道: https://www.wanuxi.com/《blue-protocol-star-resonance》东南亚区正式上线：踏入regnas的世界展开/",
    "游侠手游攻略汇总: https://m.ali213.net/news/gl2506/1667211.html",
)


def is_star_resonance_related(text: str) -> bool:
    normalized = text.lower()
    return any(keyword in normalized for keyword in KEYWORDS)


def build_star_resonance_context(text: str) -> str:
    if not is_star_resonance_related(text):
        return ""
    return (
        "本轮消息与《星痕共鸣》/Blue Protocol: Star Resonance 相关。以下是本地记忆，可作为回答背景；"
        "不要把它伪装成实时联网结果。\n"
        + "\n".join(f"- {line}" for line in _MEMORY_LINES)
        + "\n资料来源：\n"
        + "\n".join(f"- {source}" for source in _SOURCES)
    )
