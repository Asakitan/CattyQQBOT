"""笨猫『story_arc 主动推进』 — 看 active arc 跟 current user msg 关联度, 给推进/callback hint.

跟现有层的区别:
- story_arc / build_story_arc_prompt (旧): 把 active arc 列出来当 prompt 段, 让 AI 知道
  "有这些追的话题, 自然带出"
- catty_arc_pusher (本层): **关联度检测** — 看当前 msg 跟哪个 arc 相关, 给具体
  "推进这个 arc" 或 "callback 这个 arc" 的精准 hint

为什么需要:
现有 story_arc prompt 是 passive — AI 经常看到但不主动推. 这一层用 arc.keywords
匹配 current msg, 命中时给"现在该推进这个 arc"; 没命中且 arc 老化时给"该回头提"
让笨猫更像活人 (有自己的剧情线, 会主动续).

实施:
- 输入: list[StoryArc], current user_text
- 命中: arc.keywords ∩ user_text 非空 → push intent
- 老化未推: arc.created_at > 30min ago + 没命中 → callback intent
- 输出 prompt hint, 最多 1 个 arc (避免 prompt 过载)

注入位置: order=352 (story_arc=350 之后, 紧贴)
"""
from __future__ import annotations

import time
from typing import Any


_ARC_OLD_THRESHOLD_SECONDS = 30 * 60  # 30 分钟未推进算"老"
_MAX_ARC_HINT = 1                       # prompt 段最多展示 1 个 arc


def _arc_keyword_hits(arc: Any, text: str) -> int:
    """返回 arc.keywords 在 text 里命中的关键词数量."""
    if not text:
        return 0
    keywords = getattr(arc, "keywords", None) or ()
    if not keywords:
        return 0
    lower = text.lower()
    return sum(1 for k in keywords if k and k.lower() in lower)


def detect_arc_action(
    arcs: list[Any],
    current_text: str,
    *,
    now: float | None = None,
) -> tuple[str, Any] | None:
    """返回 (action_tag, arc) 或 None.

    action_tag:
    - 'push': current msg 跟 arc 关联高, 该推进
    - 'callback': 老 arc 未推进且 current msg 跟它不相关, 该主动 callback
    """
    if not arcs:
        return None
    now = now or time.time()

    # 1. 优先找有 keyword 命中的 arc → push
    hit_candidates: list[tuple[int, Any]] = []
    for arc in arcs:
        hits = _arc_keyword_hits(arc, current_text)
        if hits > 0:
            hit_candidates.append((hits, arc))
    if hit_candidates:
        hit_candidates.sort(key=lambda h: -h[0])  # 命中多的优先
        return ("push", hit_candidates[0][1])

    # 2. 没 push 候选, 找最老的未推进 arc → callback
    callback_candidates: list[tuple[float, Any]] = []
    for arc in arcs:
        created = getattr(arc, "created_at", 0.0) or 0.0
        age = now - created
        if age >= _ARC_OLD_THRESHOLD_SECONDS:
            callback_candidates.append((age, arc))
    if callback_candidates:
        callback_candidates.sort(key=lambda h: -h[0])  # 最老的优先
        return ("callback", callback_candidates[0][1])

    return None


def build_arc_pusher_prompt(
    arcs: list[Any],
    current_text: str,
    *,
    now: float | None = None,
) -> str:
    """构建 arc pusher prompt 段. 无 arc 或无 actionable 返回 "."""
    result = detect_arc_action(arcs, current_text, now=now)
    if result is None:
        return ""
    action, arc = result
    title = getattr(arc, "title", "") or "未命名 arc"
    context = (getattr(arc, "context", "") or "")[:120]

    if action == "push":
        return (
            f"【笨猫·arc 主动推进 (push)】\n"
            f"当前 user msg 跟 active arc 【{title}】关联度高 — "
            f"**这一轮推进 arc 进度**, 不是只 callback 提一下. "
            f"在回复里加新细节/进展/转折, 让 arc 往前走一步. "
            f"arc 背景: {context}"
        )
    elif action == "callback":
        now_ts = now or time.time()
        age_min = int((now_ts - getattr(arc, "created_at", now_ts)) / 60)
        return (
            f"【笨猫·arc 主动 callback】\n"
            f"老 arc 【{title}】({age_min} 分钟前的) 一直没人推 — "
            f"**可以在合适时机自然回头一句**(『...对了, 之前那个 X 后来呢喵?』式), "
            f"**不要硬塞**, 当前话题完全不相关时也可以下一轮再提. "
            f"arc 背景: {context}"
        )
    return ""


__all__ = [
    "detect_arc_action",
    "build_arc_pusher_prompt",
]
