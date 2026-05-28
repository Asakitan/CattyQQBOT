"""Per-User Vibe Profile — 轻量「这个人是什么调调」跟踪器。

类比 SillyTavern 的 Persona,但反过来:ST persona 是给『用户自己』写一段描述告诉 AI;
我们是**反向自动学习**对方调调,让笨猫对不同群友走不同的反应基调。

数据维度(每个 user_id 一条):
- vibe_tag : 主要风格 — playful(玩闹) / serious(认真讨论) / tease(挑逗调侃) /
                       lurker(潜水寡言) / techie(技术控) / lewd_curious(暧昧好奇)
- recent_topic_tags : 最近 20 条消息里出现的 topic(gaming/tech/food/random/...)
                      按出现次数前 5 个标签做"画像"
- message_count : 总记录的消息数(用于"陌生人 vs 老群友"区分)
- last_seen_at : epoch seconds
- vibe_confidence : 0-100,>=30 才注入 prompt(否则样本太少,先按默认人格走)

落盘到 memory_dir/user_vibes.json, atomic write + 后台 30s flush + LRU(最多 500 user)。
不需要 self.event,只接 user_id + text(纯文本分类)。

接入位置:
1. _build_messages 入口扫一次 record_user_message(user_id, scope, text)
2. PromptManager register catty_user_vibe (order=460) 注入「对方画像」段
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, deque
from functools import lru_cache
from pathlib import Path
from typing import Any


_MAX_TOTAL_USERS = 500
_RECENT_MSG_WINDOW = 20  # 每个 user 滚动窗口大小
_MIN_MSGS_FOR_CONFIDENCE = 5  # 至少 5 条才给画像
_MAX_TOPIC_TAGS = 5
_CLASSIFIER_CACHE_SIZE = 1024  # 高频群聊重复消息的分类结果 cache
_CLASSIFIER_TEXT_CAP = 280     # cache key 上限,超长消息走 no-cache 路径
_HIT_STRONG_MIN_LEN = 2        # 关键词 ≥2 字符算"强信号",1 字符容易子串假阳性
_HIT_STRONG_WEIGHT = 2         # 强信号的权重倍数

# 算法版本号 — 改 _hit_score / boundary / 加权逻辑时 +1。
# 当前 v2: 长度加权 + 显式 _VIBE_TAGS 顺序 + boundary-aware ASCII match。
# (cache 是 process-local 重启即失效;此版本号给未来 disk-based cache / metric tagging 用)
_CLASSIFIER_VERSION = 2

# ASCII word chars: ascii letters + digits + underscore
# (lower 已经统一小写,只列小写字母即可)
_ASCII_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def _is_ascii_token(k: str) -> bool:
    """关键词全由 ASCII a-z0-9_ 组成 → 是个独立 token,需 word boundary 匹配;
    否则(中文/混合/带空格/带标点)走原始子串匹配。"""
    return bool(k) and all(c in _ASCII_WORD_CHARS for c in k)


def _boundary_aware_in(lower: str, k: str) -> bool:
    """boundary-aware 子串匹配:ASCII 关键词不允许被同类字符夹住。

    例:
    - "h" 不会误中 "hello"(被 'ello' 紧跟,边界不成立)
    - "ts" 不会误中 "tests"(被 't' 和 'es' 夹住)
    - "rust ts kotlin" 中 "ts" 命中
    - "今天" 命中 "今"(中文不走 boundary 检查)
    """
    if not _is_ascii_token(k):
        return k in lower
    idx = lower.find(k)
    klen = len(k)
    n = len(lower)
    while idx >= 0:
        left_ok = idx == 0 or lower[idx - 1] not in _ASCII_WORD_CHARS
        right_idx = idx + klen
        right_ok = right_idx >= n or lower[right_idx] not in _ASCII_WORD_CHARS
        if left_ok and right_ok:
            return True
        idx = lower.find(k, idx + 1)
    return False


# ── 关键词 → tag 分类器(本地 zero-cost,不调 LLM) ────────────────────
# vibe 分类: 多 keyword 命中 → 选最强匹配的;持平按 dict 顺序定胜(techie 优先)。
# topic 分类: 一条消息可命中多个 topic。
# 关键词都从 data/{vibe,topic}_keywords/<tag>.json 加载,改库就动 JSON 不用碰 .py。
# 文件丢或损坏 → RuntimeError 直接挂掉(关键数据 fail-fast,不静默退化)。
_VIBE_TAGS: tuple[str, ...] = (
    # 顺序 = 持平命中时的优先级。原则:技术/暧昧/强情绪信号 > 一般情绪 > 玩闹兜底
    "techie", "lewd_curious",
    "complaint", "soft_care",  # 立刻要接情绪的两类:吐槽+求安慰
    "serious",
    "nostalgia", "braggart", "celebratory", "curious", "gossip",
    "tease", "playful",
)
_TOPIC_TAGS: tuple[str, ...] = (
    # 原 16 类
    "gaming", "tech", "food", "random", "emo", "study", "meta",
    "entertainment", "music", "travel", "shopping", "work", "love",
    "pet", "creative", "sns",
    # 新 14 类(覆盖运动/汽车/理财/健康/时事/玄学/二次元/收藏/美妆/穿搭/家居/育儿/科普/买房)
    "fitness", "car", "finance", "health", "politics", "spirit",
    "otaku", "collection", "beauty", "fashion", "household",
    "parenting", "science", "house",
)
_DATA_ROOT = Path(__file__).resolve().parent / "data"


def _load_keyword_groups(subdir: str, tags: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    base = _DATA_ROOT / subdir
    out: dict[str, tuple[str, ...]] = {}
    for tag in tags:
        path = base / f"{tag}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise RuntimeError(f"{subdir}/{tag}.json missing: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{subdir}/{tag}.json bad JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise RuntimeError(f"{subdir}/{tag}.json must be a JSON array")
        out[tag] = tuple(str(k) for k in raw if k)
    return out


_VIBE_KEYWORDS: dict[str, tuple[str, ...]] = _load_keyword_groups("vibe_keywords", _VIBE_TAGS)
_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = _load_keyword_groups("topic_keywords", _TOPIC_TAGS)


def _lower(text: str) -> str:
    return (text or "").lower()


def _hit_score(lower: str, keys: tuple[str, ...]) -> int:
    """统计关键词命中得分:命中 1 个 1 字符词 = 1 分;命中 1 个 ≥2 字符词 = 2 分。

    长度加权解决纯 hit count 偏好"关键词多的类"(techie 540 vs gossip 200)的问题,
    也减少单字关键词(『腿』『胸』『分』『了』)子串假阳性的影响 — 长词命中更可信。
    boundary-aware:ASCII 关键词(『h』『ts』『pr』)按 word boundary 匹配,
    不会被同类字符夹住而误中(『hello』不会触发『h』,『tests』不会触发『ts』)。
    """
    score = 0
    for k in keys:
        if _boundary_aware_in(lower, k):
            score += _HIT_STRONG_WEIGHT if len(k) >= _HIT_STRONG_MIN_LEN else 1
    return score


@lru_cache(maxsize=_CLASSIFIER_CACHE_SIZE)
def _classify_vibe_full_cached(lower: str) -> tuple[str | None, int, int]:
    """返回 (best_tag, best_score, second_best_score),caller 算 confidence。"""
    if not lower:
        return None, 0, 0
    best_tag: str | None = None
    best_score = 0
    second_best = 0
    # _VIBE_TAGS 顺序定义了持平时谁赢(早出现的优先,见 _VIBE_TAGS 注释)。
    for tag in _VIBE_TAGS:
        score = _hit_score(lower, _VIBE_KEYWORDS[tag])
        if score > best_score:
            second_best = best_score
            best_score, best_tag = score, tag
        elif score > second_best:
            second_best = score
    return best_tag, best_score, second_best


@lru_cache(maxsize=_CLASSIFIER_CACHE_SIZE)
def _classify_topics_cached(lower: str) -> tuple[str, ...]:
    if not lower:
        return ()
    return tuple(
        tag
        for tag, keys in _TOPIC_KEYWORDS.items()
        if any(_boundary_aware_in(lower, k) for k in keys)
    )


def _classify_vibe(text: str) -> str | None:
    """单条消息推断 vibe 标签,返回最强匹配或 None。

    长度加权 + tag 顺序兜底:hit_score = Σ(1 if len(k)==1 else 2),持平按 _VIBE_TAGS 顺序。
    超长消息(>_CLASSIFIER_TEXT_CAP)绕过 cache,避免长尾消息把 cache 撑爆。
    """
    tag, _b, _s = _classify_vibe_full(text)
    return tag


def _classify_vibe_full(text: str) -> tuple[str | None, int, int]:
    """同 _classify_vibe 但返回完整 (tag, best_score, second_best_score) 供 caller 算 confidence。"""
    lower = _lower(text)
    if len(lower) > _CLASSIFIER_TEXT_CAP:
        return _classify_vibe_full_cached.__wrapped__(lower)
    return _classify_vibe_full_cached(lower)


def classify_vibe_with_confidence(text: str) -> tuple[str | None, int]:
    """公开 API:返回 (vibe_tag, confidence 0-100) 让 caller 做阈值过滤。

    confidence 启发式:
    - best_score == 0 → (None, 0)
    - best_score == 1 (单 1 字符词命中) → 30 (极弱信号)
    - best_score == 2 (单 ≥2 字符词命中) → 55 (基础信号)
    - 区分度大 (best > 2 * second) → +25 加成,clamp 95
    - 否则按 best/(best+second+1) 比例,scale 到 40-90
    """
    tag, best, second = _classify_vibe_full(text)
    if tag is None or best == 0:
        return None, 0
    if best == 1:
        conf = 30
    elif best == 2:
        conf = 55
    elif second == 0:
        conf = min(95, 60 + best * 3)
    elif best >= 2 * second:
        conf = min(95, 55 + best * 2)
    else:
        # 接近持平,confidence 拉低
        ratio = best / (best + second + 1)
        conf = max(35, int(40 + ratio * 50))
    return tag, conf


def _classify_topics(text: str) -> list[str]:
    """单条消息打多 topic 标签(可命中多个)。"""
    lower = _lower(text)
    if len(lower) > _CLASSIFIER_TEXT_CAP:
        return list(_classify_topics_cached.__wrapped__(lower))
    return list(_classify_topics_cached(lower))


def classifier_cache_info() -> dict[str, Any]:
    """诊断:返回 vibe + topic cache 的命中率 + algorithm version,方便调优 cache size。"""
    v = _classify_vibe_full_cached.cache_info()
    t = _classify_topics_cached.cache_info()
    return {
        "version": _CLASSIFIER_VERSION,
        "vibe": {"hits": v.hits, "misses": v.misses, "size": v.currsize, "max": v.maxsize},
        "topic": {"hits": t.hits, "misses": t.misses, "size": t.currsize, "max": t.maxsize},
    }


# ── 用户画像 store ─────────────────────────────────────────────────
class UserVibeStore:
    def __init__(self, memory_path: str | Path) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._path = p.parent / "user_vibes.json"
        self._lock = threading.RLock()
        # user_id -> {"vibe_history": deque[str], "topic_history": deque[list[str]],
        #             "message_count": int, "last_seen_at": float}
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._last_access: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        users = raw.get("users", {})
        if not isinstance(users, dict):
            return
        now = time.time()
        for uid, rec in users.items():
            if not isinstance(rec, dict):
                continue
            vibe_h = rec.get("vibe_history") or []
            topic_h = rec.get("topic_history") or []
            self._data[str(uid)] = {
                "vibe_history": deque(vibe_h, maxlen=_RECENT_MSG_WINDOW),
                "topic_history": deque(topic_h, maxlen=_RECENT_MSG_WINDOW),
                "message_count": int(rec.get("message_count") or 0),
                "last_seen_at": float(rec.get("last_seen_at") or 0.0),
            }
            self._last_access[str(uid)] = now

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "users": {
                uid: {
                    "vibe_history": list(rec["vibe_history"]),
                    "topic_history": list(rec["topic_history"]),
                    "message_count": rec["message_count"],
                    "last_seen_at": rec["last_seen_at"],
                }
                for uid, rec in self._data.items()
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    def flush_sync(self) -> bool:
        with self._lock:
            if not self._dirty:
                return False
            try:
                self._atomic_write()
            except OSError:
                return False
            self._dirty = False
            return True

    async def background_flush_loop(self) -> None:
        import asyncio
        while True:
            try:
                await asyncio.sleep(30.0)
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                pass

    def _evict_lru(self) -> None:
        if len(self._data) <= _MAX_TOTAL_USERS:
            return
        ordered = sorted(self._last_access.items(), key=lambda kv: kv[1])
        for uid, _ in ordered[: len(self._data) - _MAX_TOTAL_USERS]:
            self._data.pop(uid, None)
            self._last_access.pop(uid, None)

    def record_message(self, user_id: str, text: str) -> None:
        if not user_id or not text or not text.strip():
            return
        vibe = _classify_vibe(text)
        topics = _classify_topics(text)
        if vibe is None and not topics:
            # 实在抓不到信号也记一下 message_count + last_seen,但不污染历史
            with self._lock:
                rec = self._data.setdefault(user_id, {
                    "vibe_history": deque(maxlen=_RECENT_MSG_WINDOW),
                    "topic_history": deque(maxlen=_RECENT_MSG_WINDOW),
                    "message_count": 0,
                    "last_seen_at": 0.0,
                })
                rec["message_count"] = int(rec["message_count"]) + 1
                rec["last_seen_at"] = time.time()
                self._last_access[user_id] = time.time()
                self._dirty = True
            return
        with self._lock:
            rec = self._data.setdefault(user_id, {
                "vibe_history": deque(maxlen=_RECENT_MSG_WINDOW),
                "topic_history": deque(maxlen=_RECENT_MSG_WINDOW),
                "message_count": 0,
                "last_seen_at": 0.0,
            })
            if vibe:
                rec["vibe_history"].append(vibe)
            if topics:
                rec["topic_history"].append(topics)
            rec["message_count"] = int(rec["message_count"]) + 1
            rec["last_seen_at"] = time.time()
            self._last_access[user_id] = time.time()
            self._evict_lru()
            self._dirty = True

    def profile_for(self, user_id: str) -> dict[str, Any]:
        """返回 user 的当前画像。"""
        with self._lock:
            rec = self._data.get(user_id)
            if rec is None:
                return {
                    "user_id": user_id,
                    "vibe_tag": None,
                    "topic_tags": [],
                    "message_count": 0,
                    "confidence": 0,
                }
            vibe_counter = Counter(rec["vibe_history"])
            top_vibe = vibe_counter.most_common(1)[0][0] if vibe_counter else None
            topic_counter: Counter[str] = Counter()
            for topic_list in rec["topic_history"]:
                if isinstance(topic_list, list):
                    topic_counter.update(topic_list)
            top_topics = [t for t, _ in topic_counter.most_common(_MAX_TOPIC_TAGS)]
            msg_count = int(rec["message_count"])
            confidence = min(int(msg_count / _MIN_MSGS_FOR_CONFIDENCE * 30), 100)
            return {
                "user_id": user_id,
                "vibe_tag": top_vibe,
                "topic_tags": top_topics,
                "message_count": msg_count,
                "confidence": confidence,
                "last_seen_at": rec["last_seen_at"],
            }

    def get_summary_sync(self, user_id: str) -> str:
        """CPU 引擎 (cpu_engine.script_ctx) 用的轻量摘要 sync 接口.

        返回单行字符串 (vibe_tag + 置信度 + top topics), 失败返回 "".
        主人 2026-05-28 plan-cpu-alicebot-nlu-ai S2.3.
        """
        profile = self.profile_for(user_id)
        vibe = profile.get("vibe_tag")
        if not vibe:
            return ""
        confidence = int(profile.get("confidence", 0) or 0)
        topics = profile.get("topic_tags") or []
        parts = [str(vibe), f"{confidence}%"]
        if topics:
            parts.append("|".join(str(t) for t in topics[:3]))
        return " ".join(parts)


# ── prompt 注入 helper ──────────────────────────────────────────────
_VIBE_HINTS_ZH: dict[str, str] = {
    "techie": "技术控:讲话偏认真求方案,回复时可以放开篇幅讲透,猫系语气浓度降一点(开头反应+收尾即可,中间正经讲)",
    "lewd_curious": "暧昧好奇:会试探擦边,按好感度判断 — 高好感走反差链,低好感冷处理",
    "complaint": "吐槽抱怨型:发『服了/无语/真烦/又来了』时**先顺毛共情**(『嗷呜~人家也烦死啦(尾巴一甩)』),不要立刻给方案;主人吐槽就更亲密一点骂回去",
    "soft_care": "求安慰型:发『抱抱/摸摸/没事的/别难过』时主动贴贴回应,高好感走亲密链(『来嘛主人~让笨猫蹭蹭(凑过去)ฅฅ』),低好感保持温柔但克制",
    "serious": "认真讨论型:更想要可执行答案,情绪垫一句即可,主体给信息",
    "nostalgia": "怀旧感伤型:聊『以前/小时候/再也回不去』时陪着软软回忆,**绝对不要『看开点』那种安慰式说教**,可以也来一句猫猫的『以前』凑话题(『嗷呜...笨猫也想起来了~』)",
    "braggart": "炫耀晒图型:晒『我抽到/我赢了/给你看』时**夸到位但带傲娇**(『哼,这点小事猫猫也能啦~才不羡慕呢杂鱼』),高好感可以反向调侃",
    "celebratory": "庆祝 high 型:发『终于搞定/yes/好耶』时**立刻一起 high**(『嗷嗷嗷~主人也太棒了喵!(尾巴翘起来)』),不要泼冷水或冷静评价",
    "curious": "好奇求科普型:发『这是啥/什么情况/我也想试』时**开头反应保留+稍微多讲两句**,讲完追一句『主人想试不喵~』勾起兴趣;别变 lecture",
    "gossip": "八卦型:发『听说/真的假的/小道消息』时**凑过去吃瓜**(『诶诶讲讲讲~(眼睛亮ฅฅ)』),陪着加戏,**绝对不要变成理性 fact-check 或泼冷水**",
    "tease": "调侃挑逗型:喜欢『杂鱼/笨蛋』式互怼,你可以反挑(『哼,杂鱼也敢喵?』)但留底线",
    "playful": "玩闹型:跟着节奏起哄/接梗/抽象,短句节奏拉满,猫系词放开",
    "lurker": "潜水寡言型:很少说话,这次开口可以稍微多一点反应表示在意,但别 overcooked",
}


def build_user_vibe_prompt(profile: dict[str, Any], user_display: str = "用户") -> str:
    """根据 profile 返回给 LLM 的「对方画像」段。confidence 不够返回 ""。"""
    if not profile:
        return ""
    confidence = int(profile.get("confidence") or 0)
    msg_count = int(profile.get("message_count") or 0)
    if confidence < 30 and msg_count < _MIN_MSGS_FOR_CONFIDENCE:
        return ""
    vibe = profile.get("vibe_tag")
    topics = profile.get("topic_tags") or []
    lines = [f"【对方画像】『{user_display}』(累计 {msg_count} 条消息,置信度 {confidence}%):"]
    if vibe and vibe in _VIBE_HINTS_ZH:
        lines.append(f"- 主调:{vibe} — {_VIBE_HINTS_ZH[vibe]}")
    if topics:
        topic_str = " / ".join(topics)
        lines.append(f"- 常聊话题:{topic_str}")
    if not vibe and not topics:
        return ""
    lines.append("(画像是从历史消息自动学的,只用来微调反应基调,不要复述给对方听。)")
    return "\n".join(lines)


__all__ = [
    "UserVibeStore",
    "build_user_vibe_prompt",
    "classify_vibe_with_confidence",
    "classifier_cache_info",
]
