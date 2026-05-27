"""跨对话用户细节记忆 — 抓 keyword pattern 提取『对方喜欢 / 工作 / 宠物 / 近事』.

跟现有层的区别:
- memory.py: 完整对话语料 corpus, AI 摘要后用
- catty_rag: 向量召回相关历史片段
- user_vibe: per-user 调性画像 (techie/playful/...)
- user_details_store (本层): **结构化** key-value 细节 (favorite_foods / job /
  pet / recent_event / mentioned_topics), keyword pattern 自动提取, 持久化到 JSON.

为什么需要:
笨猫现在没有"记得对方爱吃 X / 养了 Y / 工作是 Z"的结构化能力. memory 是模糊摘要,
catty_rag 要 query 才召回. 这个 store 是『可枚举的对方画像细节』 — 注入 prompt
时『对方喜欢: 烤鱼/咖啡; 工作: 程序员; 宠物: 一只白猫 喵球』式直接展示, 让
笨猫主动 callback『主人之前不是说喜欢烤鱼嘛?』.

设计:
- per-user JSON 文件 (跟 user_vibe_store 同 backing)
- LRU 500 用户, 后台 30s flush
- 关键词 pattern 自动提取 (轻量, 不调 LLM)
- 每个细节带 confidence + last_mentioned_ts, 旧的自动淡出
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


_MAX_TOTAL_USERS = 500
_MAX_DETAILS_PER_FIELD = 5  # favorite_foods 最多记 5 个
_DETAIL_TTL_SECONDS = 30 * 24 * 3600  # 30 天过期


# ── Pattern 库 ─────────────────────────────────────────────────────────
# 每个 pattern: (regex, field_name)
# 抓到的 group(1) 是细节内容
_DETAIL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 食物偏好
    (re.compile(r"(?:我|人家|本人)(?:超|很|特别|最)?(?:爱|喜欢|喜爱)吃([^,。!?\n]{2,15})"),
     "favorite_foods"),
    (re.compile(r"(?:我|人家|本人)(?:不|讨厌|超讨厌|最讨厌)吃([^,。!?\n]{2,15})"),
     "disliked_foods"),

    # 工作 / 学习
    (re.compile(r"(?:我|人家)(?:是|做|当)(?:个|名)?(程序员|工程师|设计师|学生|医生|老师|律师|司机|厨师|护士|经理|销售|运营|产品|测试|前端|后端|全栈|架构|UI 设计师|UX|UE)"),
     "job"),
    (re.compile(r"(?:我|人家)(?:在|的)(?:学校|大学|公司|单位)(?:叫|是|名字叫)?([^,。!?\n]{2,20})"),
     "workplace"),

    # 宠物
    (re.compile(r"(?:我|人家|家里|我家)(?:有|养了?)(?:一?(?:只|条|头))?([^,。!?\n]*?(?:猫|狗|鱼|鸟|鹦鹉|龟|兔子|仓鼠))(?:[,。!?\n ]|$)"),
     "pet"),

    # 兴趣爱好
    (re.compile(r"(?:我|人家)(?:超|很|特别|最)?(?:爱|喜欢)(玩|打|看)([^,。!?\n]{2,15})"),
     "hobby"),
    (re.compile(r"(?:我|人家)(?:在|最近在|这阵子在)(玩|打|看|追|学)([^,。!?\n]{2,15})"),
     "recent_activity"),

    # 近事
    (re.compile(r"(?:我|人家)(?:今天|昨天|前天|刚刚?|刚才)(去了?|做了?|吃了?|买了?)([^,。!?\n]{2,20})"),
     "recent_event"),
]


def _extract_details(text: str) -> dict[str, list[str]]:
    """从单条 user msg 抓所有命中的细节. 返回 {field: [snippet, ...]}.

    snippet 是 group 提取后 strip 过的纯内容 (不含主语 '我/人家').
    """
    if not text:
        return {}
    out: dict[str, list[str]] = {}
    for pat, field in _DETAIL_PATTERNS:
        for m in pat.finditer(text):
            # 取最后一个非空 group 作为 detail content
            groups = [g for g in m.groups() if g]
            if not groups:
                continue
            detail = groups[-1].strip()
            if not detail or len(detail) < 2:
                continue
            if len(detail) > 30:
                detail = detail[:30]
            out.setdefault(field, []).append(detail)
    return out


# ── Store ───────────────────────────────────────────────────────────────
class UserDetailsStore:
    """per-user 结构化细节, 文件持久化 + LRU.

    内部数据结构:
    {user_id: {
        field: deque[(detail_str, ts), ...],  # deque maxlen=_MAX_DETAILS_PER_FIELD
        ...
    }}
    """

    def __init__(self, memory_path: str | Path) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._path = p.parent / "user_details.json"
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, deque]] = {}
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
        for uid, fields in users.items():
            if not isinstance(fields, dict):
                continue
            self._data[str(uid)] = {}
            for field, entries in fields.items():
                if not isinstance(entries, list):
                    continue
                dq = deque(maxlen=_MAX_DETAILS_PER_FIELD)
                for e in entries:
                    if isinstance(e, list) and len(e) >= 2:
                        dq.append((str(e[0]), float(e[1])))
                if dq:
                    self._data[str(uid)][str(field)] = dq
            self._last_access[str(uid)] = now

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "users": {
                uid: {
                    field: [[d, ts] for d, ts in dq]
                    for field, dq in fields.items()
                }
                for uid, fields in self._data.items()
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
        """从消息抓细节 → 入库 (去重 + 时间戳更新)."""
        if not user_id or not text:
            return
        details = _extract_details(text)
        if not details:
            return
        now = time.time()
        with self._lock:
            fields = self._data.setdefault(user_id, {})
            for field, snippets in details.items():
                dq = fields.setdefault(
                    field, deque(maxlen=_MAX_DETAILS_PER_FIELD),
                )
                # 去重: 已存在的 snippet 只更新 ts, 不重复 push
                existing = {s for s, _t in dq}
                for s in snippets:
                    if s in existing:
                        # 更新 ts
                        for i, (snip, _t) in enumerate(dq):
                            if snip == s:
                                dq[i] = (snip, now)
                                break
                    else:
                        dq.append((s, now))
            self._last_access[user_id] = now
            self._evict_lru()
            self._dirty = True

    def get_details(
        self,
        user_id: str,
        *,
        max_age_seconds: float = _DETAIL_TTL_SECONDS,
    ) -> dict[str, list[str]]:
        """返回未过期的细节 {field: [snippets]}. 过期的过滤掉."""
        if not user_id:
            return {}
        now = time.time()
        cutoff = now - max_age_seconds
        with self._lock:
            fields = self._data.get(user_id)
            if not fields:
                return {}
            out: dict[str, list[str]] = {}
            for field, dq in fields.items():
                live = [s for s, ts in dq if ts >= cutoff]
                if live:
                    out[field] = live
            return out


# ── Prompt 注入 ─────────────────────────────────────────────────────────
_FIELD_DISPLAY: dict[str, str] = {
    "favorite_foods": "爱吃的",
    "disliked_foods": "讨厌的食物",
    "job": "工作",
    "workplace": "学校/公司",
    "pet": "养的宠物",
    "hobby": "爱好",
    "recent_activity": "最近在做",
    "recent_event": "近事",
}


def build_user_details_prompt(
    details: dict[str, list[str]],
    user_display: str = "对方",
) -> str:
    """构建 user details prompt 段. 空 details 返回 ""(skip register)."""
    if not details:
        return ""
    lines = [f"【已知{user_display}的细节】(从历史对话自动学的, 可以主动 callback『主人之前不是说 X 嘛?』式):"]
    for field, snippets in details.items():
        label = _FIELD_DISPLAY.get(field, field)
        lines.append(f"- {label}: {', '.join(snippets)}")
    lines.append("(不要复述这段给对方; 自然带进对话即可。最多回头提 1 次。)")
    return "\n".join(lines)


__all__ = [
    "UserDetailsStore",
    "build_user_details_prompt",
]
