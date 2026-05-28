"""Prompt compressor — NLU 驱动的相关性裁剪.

主人 2026-05-28 plan-cattyCacheFixAndPromptSlim Phase 2:
- 私聊 ≤5000 tokens / 群聊 ≤3000 tokens (含 system + history + current user msg)
- NLU (text2vec + jieba + HanLP) 按相关性筛 history / user_details / summary
- Persona 段 (复用 prompt_manager._PROTECTED_IDENTIFIERS) 强制保留
- NLU 失败 / 超 budget 一律降级到现状逻辑, 不阻塞业务

公开 API (都同步 + 容错降级):
- compress_history(messages, query, target_tokens, *, keep_recent=2) -> list[dict]
- select_user_details(details_list, query, top_k=5) -> list
- select_summary_paragraphs(summary_text, query, target_tokens) -> str
- dynamic_tool_picker(all_tools, query, allowed=None) -> list[dict] | None
- detect_anaphora(query) -> bool  (动态扩展 keep_recent 用)
- count_tokens(text) -> int       (复用 prompt_manager.estimate_tokens)
- get_protected_identifiers() -> frozenset[str]

设计要点:
- L2-normalized embedding → cosine = numpy dot
- score = recency_weight * (idx/n) + (1-recency_weight) * cosine(q, c_i)
- score 排序累加 token, 超 budget 停; 同 score 二级排序按时序 (cache stability)
- Persona ids 完全不进 NLU 通道
"""
from __future__ import annotations

import logging
import re
from typing import Any

import numpy as np

logger = logging.getLogger("catty.nlu.compressor")


# ── Persona 强保护 ids: reuse prompt_manager._PROTECTED_IDENTIFIERS ──────
def get_protected_identifiers() -> frozenset[str]:
    """复用 prompt_manager._PROTECTED_IDENTIFIERS, 避免维护两份白名单."""
    try:
        from ..prompt_manager import _PROTECTED_IDENTIFIERS
        return _PROTECTED_IDENTIFIERS
    except Exception:
        return frozenset()


# ── Phase 3 monotonic anchor (per-scope, in-memory) ──────────────────────
# 主人 2026-05-28 P3 设计: 同 scope 多轮间 anchor 不变 → messages 前缀字节稳定
# → Anthropic cache prefix hit. 只在超 budget 时一次大跳 (一次 reset 后稳定多轮).
# bot 重启时丢失 (内存 dict), 重启后第一轮从 0 重建, 之后稳定.
_HISTORY_ANCHORS: dict[str, int] = {}


def reset_scope_anchor(scope_id: str | None = None) -> None:
    """重置 scope anchor (主人 / 测试用). scope_id=None → 清全部."""
    if scope_id is None:
        _HISTORY_ANCHORS.clear()
    else:
        _HISTORY_ANCHORS.pop(scope_id, None)


def get_scope_anchor(scope_id: str) -> int:
    """读 anchor (诊断 / 测试用)."""
    return _HISTORY_ANCHORS.get(scope_id, 0)


# ── 指代/省略检测 (动态扩展 keep_recent) ──────────────────────────
# 主人决策: 配置项 + 动态. NLU 判断 query 含指代 → keep_recent +N.
_ANAPHORA_PATTERN = re.compile(
    r"(?:^|[^a-zA-Z一-鿿])"
    r"(?:他|她|它|这|那|这个|那个|这件|那件|这样|那样|这里|那里|"
    r"上面|刚刚|刚才|刚说|继续|接着|然后呢|后来呢|对吧|是吧)",
    re.IGNORECASE,
)

# 短 query 强制视为指代 (单字回复需要上下文才能理解)
_ANAPHORA_SHORT_TOKENS: frozenset[str] = frozenset({
    "嗯", "嗯嗯", "对", "对的", "是", "是的", "好", "好的",
    "继续", "然后呢", "后来呢", "?", "?", "啊", "哦", "噢",
})


def detect_anaphora(query: str | None) -> bool:
    """检测 query 是否含指代/省略 (需要上下文才能理解).

    True → caller 应在 keep_recent 基础上 +N 扩展 history.
    """
    if not query or not isinstance(query, str):
        return False
    s = query.strip()
    if not s:
        return False
    if s in _ANAPHORA_SHORT_TOKENS:
        return True
    # 极短 query (≤3 字符且非纯指令) 也算
    if len(s) <= 3 and not any(ch.isdigit() for ch in s):
        return True
    return bool(_ANAPHORA_PATTERN.search(s))


# ── token 计数 (复用 prompt_manager) ─────────────────────────
def count_tokens(text: str | None) -> int:
    """复用 prompt_manager.estimate_tokens, 失败 fallback 粗略估算."""
    if not text:
        return 0
    try:
        from ..prompt_manager import estimate_tokens
        return estimate_tokens(text)
    except Exception:
        return max(len(text) // 2, 1)


# ── cosine 相似度 (L2-normalized embed → dot product) ──────────
def _cosine(v1: Any, v2: Any) -> float:
    if v1 is None or v2 is None:
        return 0.0
    try:
        return float(np.dot(v1, v2))
    except Exception:
        return 0.0


# ── helper: 拿 message 的文本表示 ───────────────────────────
def _msg_text(msg: Any) -> str:
    """提取 message 的可读文本表示, 给 token 计数 / NLU 相似度用.

    主人 2026-05-28: 剥掉 inline 注入段 ([DYNAMIC_CONTEXT...] / <<<CATTY_INTERNAL_INSTRUCTION...>>>)
    再算 token. 原因: catty 把每轮动态段 inline 到 user content (~4K chars),
    monotonic_history_trim 按这个体积算 token 会让 anchor 频繁跳, history prefix
    字节漂移 → DeepSeek cache miss. 这里只影响 token 估算 / anchor 决策, messages
    实际发送内容不变 (Anthropic 路径仍 byte-perfect).
    """
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                parts.append(b.get("text", ""))
            elif btype == "tool_use":
                parts.append(f"[tool:{b.get('name', '')}]")
            elif btype == "tool_result":
                parts.append("[tool_result]")
            elif btype == "image":
                parts.append("[image]")
        text = "\n".join(p for p in parts if p)
    else:
        return ""
    # 剥 inline 注入段 (failsafe: import 失败时返回原文, 不影响业务)
    try:
        from ..prompt_cache import strip_inline_dynamic_from_text
        return strip_inline_dynamic_from_text(text)
    except Exception:
        return text


# ── 拿 compressor 相关 config (lazy import 防 circular) ──────────
def _config_get(field: str, default: Any) -> Any:
    try:
        from nonebot import get_plugin_config
        from ..config import Config
        cfg = get_plugin_config(Config)
        return getattr(cfg, field, default)
    except Exception:
        return default


# ───────────────────────────────────────────────────────────────
# compress_history
# ───────────────────────────────────────────────────────────────


def compress_history(
    messages: list[dict],
    query: str | None,
    target_tokens: int,
    *,
    keep_recent: int = 2,
) -> list[dict]:
    """按 cosine + 时间衰减压缩 history.

    Args:
        messages: 原 history (OpenAI 风 [{role, content}, ...], 不含当前 user query)
        query: 当前 user query, 用来算相关性
        target_tokens: history 段 token 预算
        keep_recent: 最近 N 条永远保留 (强制对话连续性)

    Returns:
        裁剪后 messages (按原顺序), token 数 ≤ target_tokens 或 keep_recent 兜底.

    Fallback 链 (任一失败往下降级):
      cosine + recency → 时序倒序累加 → 仅 keep_recent
    """
    if not messages:
        return messages
    if len(messages) <= keep_recent:
        return messages

    # 永远保留最后 keep_recent 条
    reserved = messages[-keep_recent:]
    candidates = messages[:-keep_recent]
    reserved_tokens = sum(count_tokens(_msg_text(m)) for m in reserved)
    remaining_budget = max(target_tokens - reserved_tokens, 0)

    if remaining_budget <= 0 or not candidates:
        return reserved

    # 试 cosine 路径
    q_vec = None
    embeds = None
    if query:
        try:
            from . import text2vec_engine
            q_vec = text2vec_engine.embed_sync(query)
            texts_for_embed = [_msg_text(m) for m in candidates]
            valid_texts = [t for t in texts_for_embed if t]
            if q_vec is not None and valid_texts:
                embeds = text2vec_engine.embed_sync_batch(valid_texts)
        except Exception as exc:
            logger.debug("compress_history embed fallback: %s", exc)
            q_vec = None
            embeds = None

    if q_vec is None or embeds is None:
        return _fallback_recency_only(candidates, reserved, remaining_budget)

    recency_weight = float(_config_get("catty_compressor_recency_weight", 0.3))
    n = len(candidates)
    embed_iter = iter(embeds)
    scored: list[tuple[float, int, dict, int]] = []
    for idx, m in enumerate(candidates):
        t = _msg_text(m)
        if not t:
            continue
        e = next(embed_iter, None)
        if e is None:
            continue
        cos = _cosine(q_vec, e)
        rec = idx / max(n - 1, 1)
        score = recency_weight * rec + (1.0 - recency_weight) * cos
        scored.append((score, idx, m, count_tokens(t)))

    if not scored:
        return _fallback_recency_only(candidates, reserved, remaining_budget)

    # 按 score 倒序, secondary 按 idx 倒序 (cache stable: 同 score 时近时间优先)
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked_idxs: set[int] = set()
    acc = 0
    for _score, idx, _m, tok in scored:
        if acc + tok > remaining_budget:
            continue
        picked_idxs.add(idx)
        acc += tok

    picked_msgs = [m for i, m in enumerate(candidates) if i in picked_idxs]
    return picked_msgs + reserved


def _fallback_recency_only(
    candidates: list[dict],
    reserved: list[dict],
    remaining_budget: int,
) -> list[dict]:
    """NLU 不可用 → 按时序倒序累加候选直到塞满 budget."""
    if not candidates:
        return reserved
    picked: list[dict] = []
    acc = 0
    for m in reversed(candidates):
        t = count_tokens(_msg_text(m))
        if acc + t > remaining_budget:
            break
        picked.insert(0, m)
        acc += t
    return picked + reserved


# ───────────────────────────────────────────────────────────────
# monotonic_history_trim (Phase 3): cache-friendly anchor checkpoint
# ───────────────────────────────────────────────────────────────


def monotonic_history_trim(
    history: list[dict],
    *,
    scope_id: str,
    target_tokens: int,
    keep_recent: int = 2,
    cut_ratio: float = 0.3,
) -> list[dict]:
    """Cache-friendly history trim — anchor checkpoint pattern.

    主人 2026-05-28 P3 设计:
    Anthropic prompt cache 看 messages 序列 prefix 字节哈希, 不只是 cache_control.
    任何 query-driven 重排都会让 prefix 字节漂移 → cache miss.

    方案: per-scope anchor checkpoint
    - 同 scope 多轮间, anchor 不动 → history[anchor:] 前缀字节稳定 → cache hit
    - 当 history[anchor:] 超 budget 时, anchor 大跳一段 (剩 (1-cut_ratio) × budget,
      留余量避免下轮立即再触), 1 次 cache reset, 之后稳定多轮直到下次超 budget

    Args:
        history: 完整 history list (append-only, 时序)
        scope_id: per-scope 持久化标识 (catty session key)
        target_tokens: history 段 token budget
        keep_recent: 末尾必保 N 条 (anchor 不能超过 n - keep_recent)
        cut_ratio: 触 budget 时砍多大比例 (0.3 = 砍后剩 70% budget)

    Returns:
        history[new_anchor:] — anchor 之后的部分.

    Cache 行为:
    - anchor 不变的轮 (大多数): prefix 字节稳定 → cache hit
    - anchor 跳跃的轮 (~每 N 轮一次): 1 次 cache miss, 之后稳定
    """
    n = len(history)
    if n == 0:
        return history

    anchor = _HISTORY_ANCHORS.get(scope_id, 0)
    # 越界保护 (history 被外部修剪过): 重锚到 keep_recent
    if anchor >= n:
        anchor = max(n - keep_recent, 0)
        _HISTORY_ANCHORS[scope_id] = anchor

    window = history[anchor:]
    window_tokens = sum(count_tokens(_msg_text(m)) for m in window)

    if window_tokens <= target_tokens:
        # 没超 budget, anchor 不动 → 字节稳定
        return window

    # 超 budget: anchor 大跳, 砍到 (1 - cut_ratio) × budget 留余量
    cut_to = max(int(target_tokens * (1.0 - cut_ratio)), 100)
    acc = 0
    new_anchor = anchor
    # 从末尾倒数累加, 找新 anchor 满足总 ≤ cut_to 且保 keep_recent 条
    for i in range(n - 1, anchor - 1, -1):
        t = count_tokens(_msg_text(history[i]))
        if acc + t > cut_to and (n - i) > keep_recent:
            new_anchor = i + 1
            break
        acc += t
    # 不超过 n - keep_recent (强保 keep_recent 末尾)
    new_anchor = min(max(new_anchor, 0), max(n - keep_recent, 0))

    if new_anchor != anchor:
        _HISTORY_ANCHORS[scope_id] = new_anchor
        logger.info(
            "monotonic_trim[%s]: anchor %d→%d (n=%d, budget=%d, kept=%d tok)",
            scope_id, anchor, new_anchor, n, target_tokens, acc,
        )
    return history[new_anchor:]


# ───────────────────────────────────────────────────────────────
# select_user_details
# ───────────────────────────────────────────────────────────────


def _detail_text(d: Any) -> str:
    """从 user_details 项里提取可比文本 (兼容多种存储结构)."""
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        parts: list[str] = []
        for k in ("text", "value", "note", "content", "summary", "fact"):
            v = d.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
        if not parts:
            try:
                parts = [str(d)]
            except Exception:
                return ""
        return " ".join(parts)
    return str(d) if d is not None else ""


def select_user_details(
    details_list: list[Any],
    query: str | None,
    top_k: int = 5,
) -> list[Any]:
    """从 user_details 按相关性选 top-K.

    打分: 0.4*cosine + 0.3*jaccard(jieba 词集合) + 0.3*实体匹配(HanLP NER).
    NLU 失败 → 返回前 top_k 条 (legacy 行为).
    """
    if not details_list:
        return details_list
    if len(details_list) <= top_k:
        return details_list
    if not query:
        return details_list[:top_k]

    # 实体集合 (HanLP)
    entities_in_query: set[str] = set()
    try:
        from . import hanlp_engine
        ents = hanlp_engine.extract_entities_sync(query)
        if ents:
            for v in ents.values():
                if isinstance(v, list):
                    for e in v:
                        if isinstance(e, str):
                            entities_in_query.add(e)
                        elif isinstance(e, (list, tuple)) and e:
                            entities_in_query.add(str(e[0]))
    except Exception:
        pass

    # query 分词
    q_tokens: set[str] = set()
    try:
        from . import jieba_helper
        toks = jieba_helper.tokenize(query)
        if toks:
            q_tokens = {t for t in toks if t.strip()}
    except Exception:
        pass

    # query embed
    q_vec = None
    try:
        from . import text2vec_engine
        q_vec = text2vec_engine.embed_sync(query)
    except Exception:
        q_vec = None

    scored: list[tuple[float, int, Any]] = []
    for i, d in enumerate(details_list):
        text = _detail_text(d)
        if not text:
            continue

        ent_score = 0.0
        if entities_in_query:
            ent_hits = sum(1 for e in entities_in_query if e in text)
            ent_score = ent_hits / max(len(entities_in_query), 1)

        jac_score = 0.0
        if q_tokens:
            try:
                from . import jieba_helper
                d_toks = jieba_helper.tokenize(text)
                if d_toks:
                    d_tokens = {t for t in d_toks if t.strip()}
                    union = len(q_tokens | d_tokens)
                    if union > 0:
                        jac_score = len(q_tokens & d_tokens) / union
            except Exception:
                pass

        cos_score = 0.0
        if q_vec is not None:
            try:
                from . import text2vec_engine
                d_vec = text2vec_engine.embed_sync(text)
                if d_vec is not None:
                    cos_score = _cosine(q_vec, d_vec)
            except Exception:
                pass

        s = 0.4 * cos_score + 0.3 * jac_score + 0.3 * ent_score
        scored.append((s, i, d))

    if not scored:
        return details_list[:top_k]

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [d for _s, _i, d in scored[:top_k]]


# ───────────────────────────────────────────────────────────────
# select_summary_paragraphs
# ───────────────────────────────────────────────────────────────


def select_summary_paragraphs(
    summary_text: str | None,
    query: str | None,
    target_tokens: int,
) -> str:
    """summary 按段落切, 按 cosine 排序累加到 target_tokens.

    段落分隔: 双换行 "\\n\\n".
    单段或空 → 原样返回 (截断到 budget).
    """
    if not summary_text:
        return ""
    paragraphs = [p.strip() for p in summary_text.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        # 单段不切, 但若超 budget 则按字符截断 (粗略)
        if count_tokens(summary_text) <= target_tokens:
            return summary_text
        return summary_text[: target_tokens * 2]  # 粗略字符截断

    if not query:
        # 按原序累加到 budget
        acc_parts: list[str] = []
        acc_tokens = 0
        for p in paragraphs:
            t = count_tokens(p)
            if acc_tokens + t > target_tokens:
                break
            acc_parts.append(p)
            acc_tokens += t
        return "\n\n".join(acc_parts)

    # cosine 路径
    q_vec = None
    embeds = None
    try:
        from . import text2vec_engine
        q_vec = text2vec_engine.embed_sync(query)
        if q_vec is not None:
            embeds = text2vec_engine.embed_sync_batch(paragraphs)
    except Exception:
        q_vec = None
        embeds = None

    if q_vec is not None and embeds is not None and len(embeds) == len(paragraphs):
        scored = [
            (_cosine(q_vec, embeds[i]), i, paragraphs[i])
            for i in range(len(paragraphs))
        ]
        scored.sort(key=lambda x: (-x[0], x[1]))
    else:
        # jaccard fallback
        try:
            from . import jieba_helper
            q_toks_list = jieba_helper.tokenize(query) or []
            q_tokens = {t for t in q_toks_list if t.strip()}
        except Exception:
            q_tokens = set()

        scored = []
        for i, p in enumerate(paragraphs):
            j_score = 0.0
            if q_tokens:
                try:
                    from . import jieba_helper
                    p_toks_list = jieba_helper.tokenize(p) or []
                    p_tokens = {t for t in p_toks_list if t.strip()}
                    union = len(q_tokens | p_tokens)
                    if union > 0:
                        j_score = len(q_tokens & p_tokens) / union
                except Exception:
                    pass
            scored.append((j_score, i, p))
        scored.sort(key=lambda x: (-x[0], x[1]))

    picked_idxs: set[int] = set()
    acc_tokens = 0
    for _s, i, p in scored:
        t = count_tokens(p)
        if acc_tokens + t > target_tokens:
            continue
        picked_idxs.add(i)
        acc_tokens += t

    if not picked_idxs:
        # 一段都塞不下 → 退回原 text 截断
        return paragraphs[0][: target_tokens * 2]

    # 按原顺序输出 (cache stable)
    return "\n\n".join(p for i, p in enumerate(paragraphs) if i in picked_idxs)


# ───────────────────────────────────────────────────────────────
# dynamic_tool_picker
# ───────────────────────────────────────────────────────────────

# 主人 plan step 2.2: 用 prototype 判断当前消息是否触发某 tool 类
# Phase 2 起步用关键词规则, 后续可换 text2vec prototype 升级.
_TOOL_TRIGGER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "catty_imagegen": ("画", "画图", "imgen", "img", "imagegen", "出图", "生成图", "做图"),
    "catty_nai_director": ("色图", "涩图", "nai", "二次元图", "色色"),
    "catty_image_search": ("搜图", "找图", "这张图", "画师", "出处", "saucenao"),
    "catty_web_search": ("搜", "查一下", "查查", "search", "百度", "google", "查询"),
    "catty_meme_query": ("梗", "meme", "表情包"),
    "catty_meme_explain": ("什么意思", "啥意思", "什么梗"),
    "catty_recall": ("记得吗", "之前说", "你说过"),
    "catty_remember": ("记住", "记一下", "帮我记"),
    "catty_recall_notes": ("笔记", "记录"),
}


def _tool_name(t: Any) -> str:
    """从 tool dict 里提取 name (兼容 OpenAI/Anthropic 两种格式)."""
    if not isinstance(t, dict):
        return ""
    if "name" in t and isinstance(t["name"], str):
        return t["name"]
    fn = t.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"]
    return ""


def dynamic_tool_picker(
    all_tools: list[dict] | None,
    query: str | None,
    allowed: set[str] | None = None,
) -> list[dict] | None:
    """按 query 命中关键词触发 tool 类, 只挂相关 tool 定义.

    Args:
        all_tools: 完整 tool schema 列表
        query: 当前 user msg, 用来判断触发哪些 tool
        allowed: caller 强制保留集合 (例如 with_tools loop 已发生 tool_calls 的 tool)

    Returns:
        过滤后 tool 列表; query 空 → 原 all_tools; 命中 0 个关键词 → []
    """
    if not all_tools:
        return all_tools
    if not query:
        return all_tools

    q_lower = query.lower()
    hit: set[str] = set()
    for tool_name, kws in _TOOL_TRIGGER_KEYWORDS.items():
        if any(kw in q_lower for kw in kws):
            hit.add(tool_name)

    if not hit and not allowed:
        return []

    keep_set = hit | (allowed or set())
    return [t for t in all_tools if _tool_name(t) in keep_set]


__all__ = [
    "compress_history",            # Phase 2 NLU 排序版 (cache-unsafe, deprecated)
    "monotonic_history_trim",      # Phase 3 cache-safe anchor checkpoint
    "reset_scope_anchor",
    "get_scope_anchor",
    "select_user_details",
    "select_summary_paragraphs",
    "dynamic_tool_picker",
    "detect_anaphora",
    "count_tokens",
    "get_protected_identifiers",
]
