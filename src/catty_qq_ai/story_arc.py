"""笨猫的「故事线」(story arc) - 跨多条消息持续追同一个话题/小剧情。

类比 SillyTavern 的「scenario」会被群里发生的事情慢慢改写,
但 ST 是 chat-level 的、人写的;我们的 arc 是 scope-level 的、AI / 自动 写的、有 TTL 自动衰减。

数据模型:
- 每个 scope(群/私聊)同时最多保留 _MAX_ARCS_PER_SCOPE 条 active arc
- 每条 arc 有: identifier / title / context / created_at / ttl_seconds / origin
- 过 ttl 一半时算「fading」(给 AI 提示要么收尾要么续推),过 ttl 直接 drop
- 持久化到 memory_dir/story_arcs.json,重启不丢

触发来源(都通过 add_arc 写入):
1. AI 自己 toolcall: catty_story_arc_set/clear(下一轮加 tool 时实现)
2. 自动触发器: 用户首次提某关键词(『今晚约不约』『生病了』『考试结束』等)→ 笨猫自动开一个 arc
3. 主人指令(下一轮): /story start 主人 还在工作 笨猫等了一小时

注入到 prompt:
- build_story_arc_prompt(scope) 返回当前 active arc 的简短描述给 LLM:
  「【正在追的话题】(45min 前开始) 等主人画的图被夸 — 主人答应给笨猫画一张戴蝴蝶结的,
   笨猫从下午就开始期待,聊到这个要带点兴奋。」

不依赖 nonebot,纯 stdlib + json 持久化。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


_DEFAULT_TTL = 3 * 3600     # 3 小时
_MAX_ARCS_PER_SCOPE = 2     # 同时最多 2 条,避免 prompt 膨胀
_MAX_TOTAL_SCOPES = 200     # 持久化文件最多记 200 个 scope,LRU 淘汰


@dataclass
class StoryArc:
    identifier: str               # 唯一 id (短 hash)
    scope: str                    # group:xxx / private:xxx
    title: str                    # 短标题 (≤ 20 字符) 例 「等主人画的图」
    context: str                  # 详细描述给 AI 看 (≤ 200 字符)
    created_at: float             # epoch seconds
    ttl_seconds: int = _DEFAULT_TTL
    origin: str = "auto"          # auto | ai_tool | owner_cmd
    keywords: tuple[str, ...] = field(default_factory=tuple)  # 用于自动续命:用户再提及就 refresh

    def remaining_seconds(self, now: float | None = None) -> float:
        now = now or time.time()
        return max(self.created_at + self.ttl_seconds - now, 0.0)

    def is_expired(self, now: float | None = None) -> bool:
        return self.remaining_seconds(now) <= 0

    def is_fading(self, now: float | None = None) -> bool:
        """剩余时间 < ttl 一半 = fading,提示 AI 该收尾或推进。"""
        return self.remaining_seconds(now) < (self.ttl_seconds / 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["keywords"] = list(self.keywords)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StoryArc":
        return cls(
            identifier=str(d.get("identifier") or ""),
            scope=str(d.get("scope") or ""),
            title=str(d.get("title") or ""),
            context=str(d.get("context") or ""),
            created_at=float(d.get("created_at") or 0.0),
            ttl_seconds=int(d.get("ttl_seconds") or _DEFAULT_TTL),
            origin=str(d.get("origin") or "auto"),
            keywords=tuple(d.get("keywords") or ()),
        )


# ── 自动开 arc 的触发器(关键词第一次出现 → 自动起话题) ──────────────
# (keys, title, context) - context 是给 AI 的引导,不是直接发出去
_AUTO_TRIGGERS: list[tuple[tuple[str, ...], str, str]] = [
    (
        ("生病", "感冒", "发烧", "头疼", "咳嗽", "不舒服"),
        "对方生病了",
        "对方提到自己生病/不舒服。笨猫从现在起进入『关心 mode』:"
        "回复里偶尔会提『好点了吗?』『有没有按时吃药?』,语气更软,撒娇收敛一点。"
        "**注意**:除非用户就是真实主人,否则**不要**称呼对方为『主人』,用『你/对方昵称』。",
    ),
    (
        ("出差", "旅游", "旅行", "回老家"),
        "对方在出门",
        "对方在出差/旅行/回老家。笨猫可以问『去哪儿啦?』『带笨猫去嘛』,"
        "聊到这个话题带点好奇 + 一点小委屈(自己不能跟着)。"
        "**注意**:对方不是主人时不要用『主人』称呼。",
    ),
    (
        ("考完了", "考完", "考试结束", "ddl结束", "项目上线了"),
        "对方刚解放",
        "对方刚结束一个长期紧张事项。笨猫进入『庆祝 mode』:"
        "可以提议奖励自己/一起放松/抱抱安慰,语气更活泼放松。",
    ),
    (
        ("分手", "失恋", "被甩"),
        "对方失恋了",
        "对方刚经历感情挫折。笨猫语气**完全收敛傲娇**,变成全程温柔陪伴:"
        "不开擦边玩笑、不冷处理、主动贴贴蹭蹭,关心『没事吧』。"
        "**注意**:对方不是主人时不要用『主人』称呼。",
    ),
    (
        ("生日", "今天我生日", "明天我生日"),
        "生日 mode",
        "对方提到生日。笨猫接下来一段时间都带着仪式感:"
        "可以提『笨猫想送礼』『今天你最大』,主动祝福。"
        "**注意**:除非对方是主人,否则不要说『今天主人最大』。",
    ),
]


class StoryArcStore:
    """per-scope arc 存储,带持久化 + LRU。"""

    def __init__(self, memory_path: str | Path) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._path = p.parent / "story_arcs.json"
        self._lock = threading.RLock()
        self._by_scope: dict[str, list[StoryArc]] = {}
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
        scopes = raw.get("scopes", {})
        if not isinstance(scopes, dict):
            return
        now = time.time()
        for scope, arcs_raw in scopes.items():
            if not isinstance(arcs_raw, list):
                continue
            kept: list[StoryArc] = []
            for ad in arcs_raw:
                if not isinstance(ad, dict):
                    continue
                try:
                    arc = StoryArc.from_dict(ad)
                except (TypeError, ValueError):
                    continue
                if arc.is_expired(now):
                    continue
                kept.append(arc)
            if kept:
                self._by_scope[str(scope)] = kept
                self._last_access[str(scope)] = now

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "scopes": {
                scope: [a.to_dict() for a in arcs]
                for scope, arcs in self._by_scope.items()
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
                await asyncio.sleep(30.0)  # arc 变更频率低于积分/记忆,可以慢一点
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                pass

    def _evict_lru(self) -> None:
        if len(self._by_scope) <= _MAX_TOTAL_SCOPES:
            return
        ordered = sorted(self._last_access.items(), key=lambda kv: kv[1])
        for scope, _ in ordered[: len(self._by_scope) - _MAX_TOTAL_SCOPES]:
            self._by_scope.pop(scope, None)
            self._last_access.pop(scope, None)

    def _prune_scope(self, scope: str, now: float) -> None:
        arcs = self._by_scope.get(scope)
        if not arcs:
            return
        kept = [a for a in arcs if not a.is_expired(now)]
        if len(kept) != len(arcs):
            self._dirty = True
        if not kept:
            self._by_scope.pop(scope, None)
            self._last_access.pop(scope, None)
            return
        # 保留最新 _MAX_ARCS_PER_SCOPE 条(按 created_at 排序)
        kept.sort(key=lambda a: a.created_at, reverse=True)
        kept = kept[:_MAX_ARCS_PER_SCOPE]
        self._by_scope[scope] = kept

    # ── public API ───────────────────────────────────────────────
    def add_arc(
        self,
        scope: str,
        title: str,
        context: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL,
        origin: str = "auto",
        keywords: tuple[str, ...] = (),
        identifier: str | None = None,
    ) -> StoryArc:
        with self._lock:
            now = time.time()
            ident = identifier or hashlib.md5(
                f"{scope}|{title}|{now}".encode("utf-8")
            ).hexdigest()[:10]
            arc = StoryArc(
                identifier=ident, scope=scope, title=title.strip()[:32],
                context=context.strip()[:400], created_at=now,
                ttl_seconds=max(int(ttl_seconds), 60),
                origin=origin, keywords=tuple(keywords),
            )
            arcs = self._by_scope.setdefault(scope, [])
            # 同标题已存在 → refresh 而不是叠加
            for i, existing in enumerate(arcs):
                if existing.title == arc.title:
                    arcs[i] = arc
                    self._dirty = True
                    self._last_access[scope] = now
                    return arc
            arcs.append(arc)
            self._prune_scope(scope, now)
            self._evict_lru()
            self._dirty = True
            self._last_access[scope] = now
            return arc

    def get_active(self, scope: str) -> list[StoryArc]:
        with self._lock:
            now = time.time()
            self._prune_scope(scope, now)
            arcs = self._by_scope.get(scope, [])
            if arcs:
                self._last_access[scope] = now
            return list(arcs)

    def clear_scope(self, scope: str) -> int:
        with self._lock:
            arcs = self._by_scope.pop(scope, [])
            self._last_access.pop(scope, None)
            if arcs:
                self._dirty = True
            return len(arcs)

    def clear_arc(self, scope: str, identifier: str) -> bool:
        with self._lock:
            arcs = self._by_scope.get(scope, [])
            kept = [a for a in arcs if a.identifier != identifier]
            if len(kept) == len(arcs):
                return False
            if kept:
                self._by_scope[scope] = kept
            else:
                self._by_scope.pop(scope, None)
                self._last_access.pop(scope, None)
            self._dirty = True
            return True

    def maybe_auto_trigger(self, scope: str, user_text: str) -> StoryArc | None:
        """扫 user_text 命中 _AUTO_TRIGGERS,首次命中就开一个 arc 返回。

        如果同标题 arc 已存在(还没过期),直接 refresh 不新开。
        """
        if not user_text:
            return None
        lower = user_text.lower()
        active_titles = {a.title for a in self.get_active(scope)}
        for keys, title, context in _AUTO_TRIGGERS:
            if any(k.lower() in lower for k in keys):
                if title in active_titles:
                    # refresh: 重新计 ttl
                    return self.add_arc(
                        scope, title, context, origin="auto_refresh", keywords=keys,
                    )
                return self.add_arc(
                    scope, title, context, origin="auto", keywords=keys,
                )
        return None


def build_story_arc_prompt(arcs: list[StoryArc], now: float | None = None) -> str:
    """把 active arc 拼成给 LLM 的 system prompt。空时返回 ""。"""
    if not arcs:
        return ""
    now = now or time.time()
    lines = ["【正在追的话题(跨多条消息的故事线)】"]
    for arc in arcs:
        elapsed_min = int((now - arc.created_at) / 60)
        suffix = "(快收尾或推进)" if arc.is_fading(now) else ""
        lines.append(f"- 【{arc.title}】(开始于 {elapsed_min} 分钟前){suffix}")
        lines.append(f"  {arc.context}")
    lines.append(
        "聊天时可以自然带出这些话题(『刚才主人说...怎么样啦?』『还在...吗?』),"
        "但**不要每条都拉回来**,只在话题合适时轻轻推进。fading 的话题要么收尾要么深入。"
    )
    return "\n".join(lines)


__all__ = [
    "StoryArc",
    "StoryArcStore",
    "build_story_arc_prompt",
]
