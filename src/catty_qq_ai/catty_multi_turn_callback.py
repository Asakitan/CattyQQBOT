"""笨猫『多轮 callback』检测 — 找最近 N 条 user msg 里 unfinished intents, 主动回头提.

跟现有层的区别:
- catty_initiative (signal-driven): 当前一条 user msg 触发的主动反应
- catty_theory_of_mind (短期趋势): 累计心理状态
- multi_turn_callback (本层): **跨多条**的『悬而未决话题』 — 对方提到的事 后续没下文,
  给笨猫一个『主动回头提』的机会

为什么需要:
真实朋友会记得『你上次说要 X 来着, 后来怎么样了?』 — 笨猫现有层不会这样回头.
catty_rag 是语义召回历史, 但不分『未完成 vs 已完成』; user_vibe 是风格画像.
本层用关键词模式抓『意图声明』(明天 X / 等会 X / 准备 X / 想 X / 打算 X /
刚 X / 一会儿 X), 当后续 msg 没继续这个话题时, 注入 callback hint.

pure function — 不存档, 看 in-conversation recent 即可.

注入位置: 跟 catty_initiative 并列 (order=213), 都是 signal-driven 主动行为.
"""
from __future__ import annotations

import re


# ── Intent patterns ─────────────────────────────────────────────────────
# 每个 (regex, intent_tag, hint_template) — 提取后形成『未完成话题』候选

_INTENT_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # 未来意图 (即将做)
    (re.compile(r"(明天|后天|下周|下个月|周末|今晚|今天晚上)([^,。!?\n]{1,30})"),
     "future_plan", "未来计划"),
    (re.compile(r"(等会儿?|等一下|一会儿|马上|待会|过会)([^,。!?\n]{1,30})"),
     "near_intent", "短期内要做"),
    (re.compile(r"(准备|打算|想要?|要去|要)([去做去吃去玩去看试买找写][^,。!?\n]{1,30})"),
     "plan_to_do", "打算要做"),

    # 过去事件 (刚做完, 可能还有续集)
    (re.compile(r"(刚才?|刚刚|前几|上次)([去吃买玩看做][^,。!?\n]{1,30})"),
     "recent_done", "刚做过的事"),

    # 进行中
    (re.compile(r"(在做|在玩|在写|在搞|在看|在弄|正在)([^,。!?\n]{1,30})"),
     "in_progress", "正在做"),

    # 问题/求助 — 但**只**抓没被回答的 (上下文判断, 这里只标记)
    (re.compile(r"([怎么如何为什么]+)[^,。!?\n]{1,30}([?？])"),
     "open_question", "未回答的问题"),
]


# 抓不到 callback 的最大候选数 (避免 prompt 过载)
_MAX_CALLBACKS = 2

# 候选话题字符数上限 (避免长截断不清)
_SNIPPET_CHAR_MAX = 30


def _trim(s: str, max_chars: int = _SNIPPET_CHAR_MAX) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


def detect_callback_targets(
    recent_user_texts: list[str],
    *,
    look_back: int = 5,
) -> list[tuple[str, str]]:
    """从最近 N 条 user msg 抓 unfinished intents, 返回 [(intent_tag, snippet)].

    recent_user_texts: 倒序 list (最新在前)
    look_back: 看最近几条 (不包括最新一条 — 最新一条是当前消息, 不算未完成)

    判定 unfinished 的简化逻辑:
    - 在 history 里出现的 intent, 但**当前消息**没有相关 keyword → 还在悬着
    - 取 oldest unfinished N 条 (优先回头提最久的)
    """
    if not recent_user_texts or len(recent_user_texts) < 2:
        return []

    current = (recent_user_texts[0] or "").lower()
    history = recent_user_texts[1:1 + look_back]  # 跳过最新一条

    candidates: list[tuple[int, str, str]] = []  # (age_index, tag, snippet)
    for age, text in enumerate(history):
        if not text:
            continue
        lower = text.lower()
        for pat, tag, _label in _INTENT_PATTERNS:
            for m in pat.finditer(text):
                # 整段 raw match 作为 snippet (含 prefix + content)
                snippet = m.group(0)
                snippet = _trim(snippet)
                # 简化判定 unfinished: snippet 的核心词没在 current 里出现
                # (粗糙启发式, 准确率 ~60-70%, 错也不致命 — 笨猫多提一句话不算坏)
                core = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                core_lower = core.lower().strip()
                if core_lower and len(core_lower) >= 2:
                    # 取 core 头 2-3 字 看是否在 current 里
                    core_head = core_lower[:3]
                    if core_head in current:
                        continue  # 当前消息还在这个话题, 不算 unfinished
                candidates.append((age, tag, snippet))

    if not candidates:
        return []

    # 按 age 倒序排 (age 大 = 久 = 优先 callback)
    candidates.sort(key=lambda c: -c[0])
    # 去重 — 同一 intent_tag 只保留 oldest 一个
    seen_tags: set[str] = set()
    out: list[tuple[str, str]] = []
    for _age, tag, snippet in candidates:
        if tag in seen_tags:
            continue
        seen_tags.add(tag)
        out.append((tag, snippet))
        if len(out) >= _MAX_CALLBACKS:
            break
    return out


def build_multi_turn_callback_prompt(recent_user_texts: list[str]) -> str:
    """构建 multi-turn callback prompt 段. 没命中返回 ""(skip register)."""
    targets = detect_callback_targets(recent_user_texts)
    if not targets:
        return ""
    header = "【笨猫·多轮 callback 机会】最近这几轮里对方提过这些没继续:"
    lines = [f"- [{tag}] {snippet}" for tag, snippet in targets]
    footer = (
        "可以在合适时机**主动回头一句**(『...对了, 刚才你说 X 后来怎么样了?』式), "
        "**不要硬塞**, 当前 user msg 跟这个话题无关时也可以等下一轮再提. "
        "callback 频率别太密 — 一条 reply 最多 1 次。"
    )
    return header + "\n" + "\n".join(lines) + "\n" + footer


__all__ = [
    "detect_callback_targets",
    "build_multi_turn_callback_prompt",
]
