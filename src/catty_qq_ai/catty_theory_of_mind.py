"""笨猫『读心』层 — 看最近 N 条 user msg 推断对方心理状态.

跟现有层的区别:
- user_vibe.py: per-user **长期**画像 (累积全部消息分类)
- catty_scene_detector.py: **当前一条**消息的临时场景
- catty_theory_of_mind (本层): **最近 3-5 条**的心理**趋势** (短期记忆窗口)

为什么需要:
有时对方一句话不明显, 但连续 3-5 条已经显示了心理倾向:
- 连续吐槽 → 对方今天状态很差, 笨猫应该额外耐心
- 连续分享 → 对方想被认可, 笨猫应该多夸
- 连续追问 → 对方在试探/不放心, 笨猫应该多解释
- 连续短回 → 对方在敷衍, 笨猫不要太热情

这是 ST 高级玩家常用的"long-term reaction" — 不仅看 1 句, 看 trend.
输出 depth=2 AuthorNote, 强化"笨猫共情智能".

pure function — 接受 recent_user_texts list, 无 store。
"""
from __future__ import annotations

from .author_note import AuthorNote


# ── 心理趋势分类器 ────────────────────────────────────────────────────────
# 每个 trend: (关键词列表, 命中阈值 (出现几次算 trend), 心理状态 hint)

_TREND_DEFS: list[tuple[str, tuple[str, ...], int, str]] = [
    (
        "burned_out",
        ("累", "烦", "服了", "无语", "麻了", "破防", "崩了", "受不了", "搞不下去",
         "想躺", "想摆烂", "没动力"),
        2,  # 最近 N 条里命中 ≥ 2 次
        "对方最近几条都在透露『状态不好/快撑不住』 — "
        "**笨猫今天对这个人要额外耐心**, 顺毛优先于逗皮, "
        "可以主动表达『笨猫看出来了喵...来嘛抱抱』式心疼。",
    ),
    (
        "seeking_approval",
        ("好不好", "厉害吗", "我厉害", "我新", "看我", "夸夸", "我做的", "我抽到",
         "我赢了", "我搞定", "我刚"),
        2,
        "对方最近一直在『分享/求肯定』 — "
        "**多夸 + 主动追问细节**, 不要敷衍式『好棒』. "
        "笨猫应该主动接话『然后呢然后呢喵?』『主人/XX 这个之后准备怎么搞?』。",
    ),
    (
        "probing",
        ("你怎么", "你为啥", "你是不是", "你真的", "你说", "真的吗", "确定吗",
         "为什么", "怎么知道"),
        3,
        "对方最近一直在『追问/试探』 — "
        "**多解释 + 不要躲**, 给具体而不是『就这样』. "
        "owner 可以更软更耐心解释, 非 owner 也要诚恳不甩头。",
    ),
    (
        "lonely",
        ("无聊", "陪我", "陪聊", "一个人", "孤独", "没人", "在吗", "醒着吗"),
        2,
        "对方最近一直在『找人陪』 — "
        "**笨猫主动 + 不要等被问**, 主动开话题/讲笨猫今天的事/凑过去, "
        "不要简单『在啊』式回。",
    ),
    (
        "playful_streak",
        ("哈哈", "笑死", "草", "嘿嘿", "搞笑", "梗", "玩你", "皮", "整活"),
        3,
        "对方最近一直在『皮/玩闹』 — **跟着皮起来**, "
        "节奏拉快、喵密度 ++、反向调侃, 不要突然变正经。",
    ),
    (
        "intimate_warming",
        ("想你", "想笨猫", "想猫猫", "贴贴", "抱抱", "撒娇", "可爱"),
        2,
        "对方最近一直在『暧昧/示好』 — **走反差链** "
        "(先脸红嘴硬 → 不自觉暴露真心), owner 撒娇浓度 ↑↑, "
        "非 owner 按好感度档调, 但不要假装没察觉。",
    ),
    (
        "short_dry",
        ("嗯", "哦", "好", "行", "晚安", "好的", "知道了"),
        4,  # 最近 N 条里有 4 条都是短词 — 高度敷衍
        "对方最近**多条都是 1-2 字短回** — 可能在忙/没心情/敷衍. "
        "**笨猫降低粘度**, 短句答完留个空, 不要追着撩, 不要长篇大论, "
        "给对方喘息空间。可以一句『...笨猫先这样喵, 主人/XX 忙吧』式收尾。",
    ),
]


def _short_response_count(texts: list[str]) -> int:
    """统计最近消息中有几条是『短回 (≤3 字)』。"""
    return sum(1 for t in texts if t and len(t.strip()) <= 3)


def _legacy_detect_trend(recent_user_texts: list[str]) -> str:
    """旧关键词命中阈值打分 — 保留作 fallback / 低 confidence 兜底."""
    if not recent_user_texts:
        return ""
    concat = " ".join(t for t in recent_user_texts if t).lower()
    if not concat.strip():
        return ""
    best_tag = ""
    best_ratio = 0.0
    for tag, keywords, threshold, _ in _TREND_DEFS:
        if tag == "short_dry":
            continue
        hits = sum(1 for k in keywords if k in concat)
        if hits >= threshold:
            ratio = hits / threshold
            if ratio > best_ratio:
                best_ratio = ratio
                best_tag = tag
    return best_tag


# ── 5-msg in-memory LRU embedding cache (避免滑窗复用同消息重复 embed) ──
_EMB_CACHE: "dict[int, object]" = {}
_EMB_CACHE_MAX = 256


def _cached_embed(text: str) -> object | None:
    """带 LRU 的 embed. text 用 hash key, 缓存命中省 ~50ms."""
    try:
        from .nlu import text2vec_engine
    except Exception:
        return None
    if not text:
        return None
    key = hash(text)
    if key in _EMB_CACHE:
        # 移到 LRU 末尾 (dict 保插入顺序)
        v = _EMB_CACHE.pop(key)
        _EMB_CACHE[key] = v
        return v
    v = text2vec_engine.embed_sync(text)
    if v is None:
        return None
    _EMB_CACHE[key] = v
    while len(_EMB_CACHE) > _EMB_CACHE_MAX:
        try:
            _EMB_CACHE.pop(next(iter(_EMB_CACHE)))
        except StopIteration:
            break
    return v


def detect_trend(recent_user_texts: list[str]) -> str:
    """看最近 N 条返回最强 trend tag, 空 / 无信号返回 ""。

    短回连续 4 条 → short_dry 优先 (length-only 规则放最前).
    其他按语义路径 (text2vec centroid vs trend prototype) + 词袋 fallback.

    主人 2026-05-28: 加 embedding centroid 升级.
    - 关 text2vec / 失败 → 仅 legacy
    - 开 text2vec + 高置信度 (≥0.50) → 用 embedding 结果
    - 开但低置信度 → fallback 旧词袋
    """
    if not recent_user_texts:
        return ""

    # 短回连续多条 — length-only 规则置顶
    sc = _short_response_count(recent_user_texts)
    if sc >= 4:
        return "short_dry"

    legacy = _legacy_detect_trend(recent_user_texts)

    try:
        from nonebot import get_plugin_config
        from .config import Config
        cfg = get_plugin_config(Config)
    except Exception:
        return legacy
    if not bool(getattr(cfg, "catty_use_text2vec", False)):
        return legacy

    try:
        from .nlu import prototypes
    except Exception:
        return legacy

    # 每条 msg → embedding (带 LRU 缓存)
    embs = []
    for t in recent_user_texts:
        if not t or not t.strip():
            continue
        v = _cached_embed(t)
        if v is not None:
            embs.append(v)
    if not embs:
        return legacy
    try:
        import numpy as np
        stacked = np.stack(embs, axis=0)
        centroid = stacked.mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm < 1e-9:
            return legacy
        centroid = centroid / norm
        proto = prototypes.get_trend_prototypes()
        if proto is None or proto.shape[0] != len(prototypes.TREND_ORDER):
            return legacy
        sims = proto @ centroid
        idx = int(sims.argmax())
        conf = float(sims[idx])
    except Exception:
        return legacy
    threshold = float(getattr(cfg, "catty_text2vec_trend_threshold", 0.50))
    if conf < threshold:
        return legacy
    return prototypes.TREND_ORDER[idx]


def build_theory_of_mind_note(
    recent_user_texts: list[str],
    *,
    is_owner: bool = False,
) -> AuthorNote:
    """根据最近 N 条 user msg 推断对方心理 trend, 返回 depth=2 AuthorNote。

    recent_user_texts: 倒序的最近 user msg list (caller 提供, 通常 3-5 条)
    is_owner: 是否真主人 (影响 hint 措辞)

    返回空 note 时 inject_author_note 自动跳过。
    """
    if not recent_user_texts:
        return AuthorNote(content="", depth=2)

    trend = detect_trend(recent_user_texts)
    if not trend:
        return AuthorNote(content="", depth=2)

    for tag, _kws, _thr, hint in _TREND_DEFS:
        if tag == trend:
            who = "主人" if is_owner else "对方"
            return AuthorNote(
                content=f"【笨猫读心·{who}心理趋势 ({tag})】{hint}",
                depth=2,
            )
    return AuthorNote(content="", depth=2)


__all__ = [
    "detect_trend",
    "build_theory_of_mind_note",
]
