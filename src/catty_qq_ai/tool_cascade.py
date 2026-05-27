"""Tool dependency cascade — 命中 source tool 后建议下一步该调 target tool.

跟现有 tool routing 的区别:
- chat_completion_with_tools (现有): AI 自己决定每轮调哪个 tool, 多轮 3 rounds 上限
- tool_cascade (本层): 看上一轮 tool 结果, 给 AI 一个『下一步推荐调 X』hint
  让 AI 不用自己猜 tool chain (catty_recall → catty_user_profile → catty_game_recall)

为什么需要:
现有 3 轮循环里, AI 偶尔只调 1 个 tool 就停了, 漏掉自动 chain. 比如用户说『主人之前
提过的 strinova 攻略』:
  - AI 调 catty_recall 拿到 raw 历史片段
  - 但其实还该调 catty_game_recall 拿专属事实库
  - AI 不一定察觉 → 漏读

加 cascade map 后, recall 出 result 时, 系统主动提示『下一轮可以调 catty_game_recall(scope='strinova')』,
AI 容易接住.

实现:
- 每个 source tool 配 list[CascadeRule] — 触发条件 + 建议 target tool
- check_cascade(name, result_dict) 返回 list[hint_str]
- chat_completion_with_tools 每轮 tool 跑完后调 check_cascade, hint 作为
  role=system 注入 history, 下一轮 AI 看到

pure function — 不读 store, 不发起新 tool call (建议交给 AI 决定).
"""
from __future__ import annotations

from typing import Any


# CascadeRule 形态:
# {
#   "on_result_contains": str | list[str],  # result JSON 含某 key 或子串才触发
#   "suggest": str,  # 推荐 target tool 名
#   "hint": str,  # 给 AI 的具体提示 (中文, 含调用建议)
# }


_CASCADE_RULES: dict[str, list[dict[str, Any]]] = {
    # catty_recall 命中: 通常说明用户的"上次/记得"指代被找到, 后续可以追画像 + 游戏库
    "catty_recall": [
        {
            "on_result_contains": ["matches", "long_term_summary"],
            "suggest": "catty_user_profile",
            "hint": (
                "[cascade hint] catty_recall 拿到了历史片段. 如果命中里有提到特定 user_id "
                "(非当前发言者) 且你还不确定怎么称呼对方, 下一轮可以调 "
                "catty_user_profile(user_id=XXX) 补全画像. 否则直接基于 recall 内容回答即可."
            ),
        },
        {
            "on_result_contains": ["game_name", "strinova", "star_resonance", "minecraft", "genshin"],
            "suggest": "catty_game_recall",
            "hint": (
                "[cascade hint] catty_recall 命中里提到了游戏关键词. 下一轮可以调 "
                "catty_game_recall(game='strinova/star_resonance/minecraft/genshin') 拿专属事实库, "
                "事实库跨群共享比 recall 更精准."
            ),
        },
    ],

    # catty_user_profile 命中: 拿到画像后, 如果对方提到游戏, 自动联动 game_recall
    "catty_user_profile": [
        {
            "on_result_contains": ["game_played", "favorite_game", "main_game"],
            "suggest": "catty_game_recall",
            "hint": (
                "[cascade hint] catty_user_profile 显示对方有主玩游戏. 下一轮可以调 "
                "catty_game_recall(game=该游戏名) 拿对应事实库."
            ),
        },
    ],

    # catty_web_search 已经有 auto_sinked_to_game_memory (tools.py 自动 sink top3 到游戏库),
    # 但 AI 拿到结果时该明确告诉它『结果已自动收集了, 别再调 catty_game_remember 重复』
    "catty_web_search": [
        {
            "on_result_contains": "auto_sinked_to_game_memory",
            "suggest": "",  # 不建议下一步 tool, 只 warn
            "hint": (
                "[cascade hint] catty_web_search 已自动 sink top3 结果到游戏库 "
                "(auto_sinked_to_game_memory). **不要再调 catty_game_remember** 重复写, "
                "去重也会拦但浪费一次工具调用."
            ),
        },
    ],

    # catty_game_recall 拿到事实后, 如果用户问『最近版本』, 提示可以调 web_search 拿最新
    "catty_game_recall": [
        {
            "on_result_contains": ["facts", "entries"],
            "suggest": "",  # 不强制下一步
            "hint": (
                "[cascade hint] catty_game_recall 拿到了事实库. 如果用户问的是『最新版本/今天的活动』"
                "等时效性内容, 事实库可能过时, 下一轮可以调 catty_web_search(query='游戏名 最新版本') 补充."
            ),
        },
    ],

    # catty_now 拿到时间, 如果接近特定节日/时段, 提示
    "catty_now": [
        {
            "on_result_contains": "festival",
            "suggest": "",
            "hint": (
                "[cascade hint] catty_now 显示当前接近一个节日. 回复时可以自然带节日氛围, "
                "但不要每条都报时."
            ),
        },
    ],

    # catty_meme_explain 拿到梗解释后, 如果用户在群里, 提示用 catty_meme_query 拉梗图配合
    "catty_meme_explain": [
        {
            "on_result_contains": "extract",
            "suggest": "catty_meme_query",
            "hint": (
                "[cascade hint] catty_meme_explain 拿到了梗词条解释. 如果用户在群里玩这个梗, "
                "下一轮可以调 catty_meme_query 拉一张相关梗图嵌入回复, 但**不要复读完整 extract**."
            ),
        },
    ],
}


def _check_match(payload: Any, needle: str) -> bool:
    """递归在 payload 里找 needle (key 或子串)."""
    if not needle:
        return False
    if isinstance(payload, dict):
        # 先看 key, 再看 value
        for k, v in payload.items():
            if isinstance(k, str) and needle in k:
                return True
            if _check_match(v, needle):
                return True
        return False
    if isinstance(payload, list):
        for item in payload:
            if _check_match(item, needle):
                return True
        return False
    if isinstance(payload, str):
        return needle in payload
    return False


def check_cascade(
    tool_name: str,
    tool_result: Any,
    *,
    max_hints: int = 2,
) -> list[str]:
    """看 tool 结果, 返回 cascade hints (≤max_hints 条).

    tool_name: 上一轮调的 tool 名
    tool_result: tool executor 返回的 dict (或 JSON 字符串)
    max_hints: 每轮 hint 上限, 避免 prompt 过载

    返回 list[hint_str], 空 list 时不注入.
    """
    rules = _CASCADE_RULES.get(tool_name)
    if not rules:
        return []
    out: list[str] = []
    for rule in rules:
        triggers = rule.get("on_result_contains")
        if isinstance(triggers, str):
            triggers = [triggers]
        if not isinstance(triggers, list):
            continue
        hit = False
        for needle in triggers:
            if _check_match(tool_result, str(needle)):
                hit = True
                break
        if hit:
            hint = rule.get("hint", "")
            if hint:
                out.append(hint)
                if len(out) >= max_hints:
                    break
    return out


def build_post_tool_hint(executed_tool_results: list[tuple[str, Any]]) -> str:
    """给 chat_completion_with_tools 用: 把多个 tool 的 cascade hints 合成 1 个 system msg.

    executed_tool_results: list of (tool_name, parsed_result_dict)
    返回 1 个合并后的 hint str (空时返回 ""), caller 决定是否注入 history.
    """
    all_hints: list[str] = []
    for name, result in executed_tool_results:
        hints = check_cascade(name, result)
        all_hints.extend(hints)
    if not all_hints:
        return ""
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for h in all_hints:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return "\n\n".join(unique[:3])  # 总 hint 上限 3 条


__all__ = [
    "check_cascade",
    "build_post_tool_hint",
]
