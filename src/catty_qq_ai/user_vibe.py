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
# vibe 分类: 多 keyword 命中 → 选最强匹配的;持平默认 playful
_VIBE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "techie": (
        "代码", "python", "java", "javascript", "ts", "react", "vue", "linux",
        "docker", "kubectl", "k8s", "git", "github", "bug", "报错", "stack",
        "算法", "数据结构", "接口", "数据库", "sql", "正则", "shell", "命令行",
        "服务器", "deploy", "测试", "调试", "merge", "pr ", "ci/", "regex",
    ),
    "lewd_curious": (
        "色色", "涩涩", "h", "艹", "操", "床", "胸", "腿", "腹黑", "黄",
        "色图", "擦边", "性感", "JK", "白丝", "黑丝", "猫娘的尾巴",
        "羞羞", "想xx", "腿玩年",
    ),
    "tease": (
        "杂鱼", "笨蛋", "笨猫", "傻猫", "蠢", "弱", "孩子",
        "哼哼", "嘿嘿嘿", "嘻嘻", "嗷", "你这个", "你居然", "哈?",
    ),
    "serious": (
        "为什么", "如何", "怎么做", "讨论", "请教", "分析", "原因", "依据",
        "建议", "方案", "原理", "区别", "对比", "评价", "总结", "看法",
    ),
    "playful": (
        "笑死", "蚌埠", "绝", "牛", "666", "草", "yyds", "awsl",
        "贴贴", "蹭蹭", "可爱", "好玩", "xs", "嘿嘿", "嘻嘻",
        "玩玩", "整活", "嗨翻",
    ),
}

# topic 分类: 一条消息可命中多个 topic
_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gaming": (
        "游戏", "原神", "崩铁", "卡拉彼丘", "strinova", "明日方舟",
        "minecraft", "我的世界", "mc", "csgo", "valorant", "联机", "组队",
        "副本", "本子", "刷", "技能", "boss", "段位", "打游戏",
    ),
    "tech": (
        "代码", "python", "java", "前端", "后端", "ai", "openai", "claude",
        "github", "linux", "docker", "服务器", "算法", "数据库", "网络",
        "调试", "deploy", "运维",
    ),
    "food": (
        "吃", "喝", "饭", "好吃", "饿", "馋", "外卖", "烤", "煮",
        "螺蛳粉", "火锅", "烧烤", "小鱼干", "罐头", "牛奶",
    ),
    "random": (
        "今天", "天气", "下雨", "好热", "好冷", "上班", "下班", "通勤",
        "出门", "回家", "睡觉", "起床",
    ),
    "emo": (
        "难过", "emo", "崩溃", "心累", "委屈", "失恋", "孤独", "一个人",
        "想家", "压力大",
    ),
    "study": (
        "考试", "复习", "ddl", "deadline", "作业", "论文", "毕设",
        "答辩", "上课", "学校", "老师", "刷题",
    ),
    "meta": (
        "你是", "ai", "bot", "助手", "程序", "扮演", "测试", "笨猫",
        "猫猫", "猫娘",
    ),
}


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
