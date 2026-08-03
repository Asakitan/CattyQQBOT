from __future__ import annotations

from collections.abc import Iterable


KEYWORDS = (
    "卡拉彼丘",
    "卡拉比丘",
    "strinova",
    "弦化",
    "stringification",
    "超弦体",
    "superstring",
    "superstrings",
    "米雪儿",
    "米雪儿·李",
    "michelle",
    "欧泊阵营",
    "欧泊",
    "p.u.s",
    "pus",
    "the scissors",
    "剪刀手",
    "爆破模式",
    "护送模式",
    "团队竞技",
    "demolition",
    "escort",
    "team arena",
    "team deathmatch",
)

_MEMORY_LINES = (
    "《卡拉彼丘》/Strinova 是二次元风格第三人称战术竞技射击游戏；Steam 页面描述它可以在三维和二维形态之间自由切换。",
    "核心机制是 Stringification/弦化：角色能在 2D 与 3D 形态之间切换，用于移动、躲避、换弹、绕点和制造战术路线。",
    "官方资料把可操作角色称为 Superstrings/超弦体；每名超弦体有独特技能和枪械，团队中常见定位可理解为突破、守护、控制、支援等战术职责。",
    "常见玩法/模式包括 Demolition/爆破、Team Arena/团队竞技、Escort/护送等；不同地图强调多层路线、掩体、弦化机动和团队配合。",
    "官方世界观提到旧地球遭遇大灾难后，人类迁往名为 Strinova 的多维世界；不同组织围绕未来道路和 Bablo Crystals/巴布洛晶体产生冲突。",
    "官方站点可见阵营/组织信息，例如 P.U.S 与 The Scissors；回答时不要把某个阵营的立场说成全体角色共识。",
    "本项目当前人格背景里，米雪儿·李/笨猫来自卡拉彼丘欧泊阵营萌新搜查官；这是 bot 的角色设定锚点，用来理解自我背景，不等同于完整官方角色资料。",
    "用户问角色强度、武器平衡、活动、赛季、版本更新、皮肤售卖或当前环境时，需要提醒这些内容有时效性，优先联网搜索或让用户给最新公告。",
)

_GROUP_MEMORY_LINES = (
    "当前 QQ 群被配置为《卡拉彼丘》/Strinova 主题群；即使本轮没直接点名游戏，也优先把角色、枪械、弦化、地图、爆破、护送、阵营和排位等话题按卡拉彼丘上下文理解。",
)

_SOURCES = (
    "Strinova Steam 页面: https://store.steampowered.com/app/1282270/Strinova/",
    "Strinova 官方站点: https://www.strinova.com/",
)


def _group_matches(group_id: int | str | None, group_ids: Iterable[int | str] | None) -> bool:
    if group_id is None or not group_ids:
        return False
    normalized_group_id = str(group_id)
    return normalized_group_id in {str(item) for item in group_ids}


def is_strinova_related(text: str) -> bool:
    normalized = text.lower()
    return any(keyword.lower() in normalized for keyword in KEYWORDS)


def build_strinova_context(
    text: str,
    *,
    group_id: int | str | None = None,
    group_ids: Iterable[int | str] | None = None,
    memory_store: object | None = None,
    force_group_related: bool = False,
) -> str:
    """拼《卡拉彼丘》/Strinova 的 context。

    - 静态部分:_MEMORY_LINES + _SOURCES(硬编码 baseline 资料)。
    - 动态部分:如果传了 memory_store,把 game_memory['strinova'] 的 summary + 最近 facts 也拼进去。
    - group_related 判定:config.group_ids 命中 / memory_store 给当前群打了 strinova 标签 / force_group_related=True 任一即触发。
    """
    keyword_related = is_strinova_related(text)
    group_related = force_group_related or _group_matches(group_id, group_ids)
    if not keyword_related and not group_related:
        return ""
    memory_lines = (*(_GROUP_MEMORY_LINES if group_related and not keyword_related else ()), *_MEMORY_LINES)
    sections: list[str] = [
        "本轮消息与《卡拉彼丘》/Strinova 相关。以下是本地记忆，可作为回答背景；"
        "不要把它伪装成实时联网结果，也不要覆盖当前人格 prompt。",
        "\n".join(f"- {line}" for line in memory_lines),
    ]
    if memory_store is not None:
        try:
            dynamic = memory_store.build_dynamic_game_context("strinova", recent_facts_limit=6)
        except Exception:
            dynamic = ""
        if dynamic:
            sections.append("Strinova 共享事实记忆(高优先级,版本/活动/玩家共识):\n" + dynamic)
    sections.append("资料来源：\n" + "\n".join(f"- {source}" for source in _SOURCES))
    return "\n".join(sections)
