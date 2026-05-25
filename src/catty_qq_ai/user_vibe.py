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
from pathlib import Path
from typing import Any


_MAX_TOTAL_USERS = 500
_RECENT_MSG_WINDOW = 20  # 每个 user 滚动窗口大小
_MIN_MSGS_FOR_CONFIDENCE = 5  # 至少 5 条才给画像
_MAX_TOPIC_TAGS = 5


# ── 关键词 → tag 分类器(本地 zero-cost,不调 LLM) ────────────────────
# vibe 分类: 多 keyword 命中 → 选最强匹配的;持平按 dict 顺序定胜(techie 优先)。
# topic 分类: 一条消息可命中多个 topic。
# 关键词都从 data/{vibe,topic}_keywords/<tag>.json 加载,改库就动 JSON 不用碰 .py。
# 文件丢或损坏 → RuntimeError 直接挂掉(关键数据 fail-fast,不静默退化)。
_VIBE_TAGS: tuple[str, ...] = ("techie", "lewd_curious", "tease", "serious", "playful")
_TOPIC_TAGS: tuple[str, ...] = (
    "gaming", "tech", "food", "random", "emo", "study", "meta",
    "entertainment", "music", "travel", "shopping", "work", "love",
    "pet", "creative", "sns",
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


def _classify_vibe(text: str) -> str | None:
    """单条消息推断 vibe 标签,返回最强匹配或 None。"""
    lower = _lower(text)
    if not lower:
        return None
    best_tag: str | None = None
    best_hits = 0
    for tag, keys in _VIBE_KEYWORDS.items():
        hits = sum(1 for k in keys if k in lower)
        if hits > best_hits:
            best_hits, best_tag = hits, tag
    return best_tag


def _classify_topics(text: str) -> list[str]:
    """单条消息打多 topic 标签。"""
    lower = _lower(text)
    if not lower:
        return []
    out = []
    for tag, keys in _TOPIC_KEYWORDS.items():
        if any(k in lower for k in keys):
            out.append(tag)
    return out


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


# ── prompt 注入 helper ──────────────────────────────────────────────
_VIBE_HINTS_ZH: dict[str, str] = {
    "techie": "技术控:讲话偏认真求方案,回复时可以放开篇幅讲透,猫系语气浓度降一点(开头反应+收尾即可,中间正经讲)",
    "lewd_curious": "暧昧好奇:会试探擦边,按好感度判断 — 高好感走反差链,低好感冷处理",
    "tease": "调侃挑逗型:喜欢『杂鱼/笨蛋』式互怼,你可以反挑(『哼,杂鱼也敢喵?』)但留底线",
    "serious": "认真讨论型:更想要可执行答案,情绪垫一句即可,主体给信息",
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
]
