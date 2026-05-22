from __future__ import annotations

from collections.abc import Iterable


KEYWORDS = (
    "星痕共鸣",
    "星痕共鳴",
    "星痕",
    "blue protocol",
    "蔚蓝法则",
    "蓝色协议",
    "star resonance",
    "bpsr",
    "雷格纳斯",
    "regnas",
    "麦格纳",
    "magna",
    "阿斯特里亚",
    "asteria",
    "生体元",
    "获取新职业资格",
    "武器切换",
    "巨刃守护者",
    "神盾骑士",
    "神射手",
    "冰魔导师",
    "雷影剑士",
    "森语者",
    "极限空间",
    "开拓局委托",
    "虚蚀",
)

_MEMORY_LINES = (
    "《星痕共鸣》/Blue Protocol: Star Resonance 是动漫风开放世界 MMORPG，和 Bandai Namco 的 Blue Protocol / PROJECT SKY BLUE 世界观相关。",
    "公开资料把舞台称为 Regnas/雷格纳斯世界，包含 Magna/麦格纳、Asteria Plains/阿斯特里亚平原等地区称呼；不同地区译名可能略有差异。",
    "基础玩法包含开放世界探索、主线/支线任务、多人副本与首领、公会与社交、采集/钓鱼/挖矿、坐骑/载具、角色自定义和时装装扮。",
    "战斗定位覆盖坦克、治疗、近战输出、远程输出和控场等方向；公开文案提到可以用盾保护队友、用法术治疗，或切换风格打出爆发伤害。",
    "职业/玩法不要按单一固定职业理解：GameKee 资料提到生体元等级达到 9 后可通过“获取新职业资格”引导任务解锁更多职业体验；游侠攻略也提到玩家可通过武器切换来换职业。",
    "群聊里提到巨刃守护者、神盾骑士、神射手、冰魔导师、雷影剑士、森语者、极限空间、开拓局委托、虚蚀、金装等词时，大概率是在聊星痕共鸣的职业、副本、活动或养成。",
    "金装和养成相关说法要谨慎：攻略汇总提到高级副本、特殊怪物、季节/活动任务和商店兑换都可能关联金装获取，但具体掉落、兑换和收益会随版本变化。",
    "SEA 公开报道显示 Blue Protocol: Star Resonance SEA 由 HaoPlay 发行，于 2025-12-18 在 Android、iOS 与 PC 上线；其他地区服、国服、公测和活动时间不要互相混用。",
    "回答相关问题时把这些当作本地参考记忆，不要伪装成实时联网结果；职业强度、赛季内容、活动时间、兑换码、限时奖励和版本改动应建议联网搜索或让用户提供最新截图/公告。",
)

_GROUP_MEMORY_LINES = (
    "当前 QQ 群被配置为《星痕共鸣》主题群；即使本轮没直接点名游戏，也优先把职业、副本、装备、活动、开服、捏脸、坐骑和公会等话题按星痕共鸣上下文理解。",
)

_SOURCES = (
    "StarResonance.ORG 职业数据库: https://star-resonance.org/zh-cn/",
    "GameKee 星痕共鸣职业选择: https://www.gamekee.com/bluestar/644304.html",
    "App Store Blue Protocol: Star Resonance 页面: https://apps.apple.com/us/app/blue-protocol-star-resonance/id6744962482",
    "Wanuxi SEA 上线报道: https://www.wanuxi.com/《blue-protocol-star-resonance》东南亚区正式上线：踏入regnas的世界展开/",
    "游侠手游攻略汇总: https://m.ali213.net/news/gl2506/1667211.html",
)


def _group_matches(group_id: int | str | None, group_ids: Iterable[int | str] | None) -> bool:
    if group_id is None or not group_ids:
        return False
    normalized_group_id = str(group_id)
    return normalized_group_id in {str(item) for item in group_ids}


def is_star_resonance_related(text: str) -> bool:
    normalized = text.lower()
    return any(keyword.lower() in normalized for keyword in KEYWORDS)


def build_star_resonance_context(
    text: str,
    *,
    group_id: int | str | None = None,
    group_ids: Iterable[int | str] | None = None,
    memory_store: object | None = None,
    force_group_related: bool = False,
) -> str:
    """拼《星痕共鸣》context。

    静态部分:_MEMORY_LINES + _SOURCES。
    动态部分:memory_store 不为 None 时,从 game_memory['star_resonance'] 拼 summary + 最近 facts。
    group_related:config.group_ids / memory_store 群标签 / force_group_related 任一为真触发。
    """
    keyword_related = is_star_resonance_related(text)
    group_related = force_group_related or _group_matches(group_id, group_ids)
    if not keyword_related and not group_related:
        return ""
    memory_lines = (*(_GROUP_MEMORY_LINES if group_related and not keyword_related else ()), *_MEMORY_LINES)
    sections: list[str] = [
        "本轮消息与《星痕共鸣》/Blue Protocol: Star Resonance 相关。以下是本地记忆，可作为回答背景；"
        "不要把它伪装成实时联网结果。",
        "\n".join(f"- {line}" for line in memory_lines),
    ]
    if memory_store is not None:
        try:
            dynamic = memory_store.build_dynamic_game_context("star_resonance", recent_facts_limit=6)
        except Exception:
            dynamic = ""
        if dynamic:
            sections.append("猫猫长期积累的星痕共鸣事实记忆(高优先级,版本/职业/活动):\n" + dynamic)
    sections.append("资料来源：\n" + "\n".join(f"- {source}" for source in _SOURCES))
    return "\n".join(sections)
