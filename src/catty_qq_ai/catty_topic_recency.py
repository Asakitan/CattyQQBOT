"""笨猫『话题新鲜度』检测 — 当前 user msg 提到的话题, 是新还是已经聊过的老话题.

跟 multi_turn_callback / arc_pusher 区别: 那俩抓 unfinished intent / arc progress,
本层判断**话题本身**是否在最近 N 轮聊过, 决定是新展开还是简短继续.

实现: 利用 daily_life.TOPIC_KEYWORDS 字典, 看 user_text 跟 history 末尾 N 条
user msg 的 topic 集合差.

注入位置: order=226 (contradiction_detector=225 之后)
"""
from __future__ import annotations


def detect_topic_recency(
    current_text: str,
    recent_user_texts: list[str],
    *,
    look_back: int = 5,
) -> dict[str, list[str]]:
    """返回 {'new_topics': [...], 'old_topics': [...]} — 当前 vs 历史的话题差.

    current_text: 当前 user msg
    recent_user_texts: 倒序最近 user msg list (含当前, [0]=最新)
    look_back: 看最近 N 条 (不含当前)
    """
    try:
        from .daily_life import detect_topics
    except Exception:  # noqa: BLE001
        return {"new_topics": [], "old_topics": []}

    if not current_text:
        return {"new_topics": [], "old_topics": []}

    current_topics = detect_topics(current_text)
    if not current_topics:
        return {"new_topics": [], "old_topics": []}

    history_slice = recent_user_texts[1:1 + look_back] if recent_user_texts else []
    historical_topics: set[str] = set()
    for t in history_slice:
        historical_topics.update(detect_topics(t))

    new = sorted(current_topics - historical_topics)
    old = sorted(current_topics & historical_topics)
    return {"new_topics": new, "old_topics": old}


def build_topic_recency_prompt(
    current_text: str,
    recent_user_texts: list[str],
) -> str:
    """构建话题新鲜度 prompt 段."""
    diff = detect_topic_recency(current_text, recent_user_texts)
    new = diff.get("new_topics") or []
    old = diff.get("old_topics") or []
    if not new and not old:
        return ""

    lines = ["【笨猫·话题新鲜度】"]
    if old:
        lines.append(
            f"- **老话题 (近 5 轮聊过)**: {', '.join(old)} — "
            "本轮**简短继续 / 推进**, 别重复展开背景, 别再科普第一遍."
        )
    if new:
        lines.append(
            f"- **新话题**: {', '.join(new)} — "
            "本轮**主动展开 1-2 句背景**, 用笨猫语气接 + 留 hook 让对方继续聊."
        )
    lines.append("(跟 multi_turn_callback / arc_pusher 互补, 这层只看话题本身新旧。)")
    return "\n".join(lines)


__all__ = [
    "detect_topic_recency",
    "build_topic_recency_prompt",
]
