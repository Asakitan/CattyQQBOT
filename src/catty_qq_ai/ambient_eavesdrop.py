"""笨猫『窃听周边对话』buffer — 让笨猫"听到"群里其他人互聊, 被 @ 时可以主动 mention.

跟现有层的区别:
- memory.py: 全量 corpus 落盘, 慢但永久
- session_cache: 笨猫接过的对话 history, 不含 ambient
- ambient_eavesdrop (本层): **群里所有 msg 的轻量 in-memory buffer** (包括非 @ 笨猫的),
  让笨猫被触发时能拉出最近周边对话 → 表现"我刚才听到你们聊 X"的"在场感"

为什么需要:
QQ 群里大部分时候笨猫不应该回应 (避免话痨), 但偶尔被 @ 时如果只看历史
chat_matcher 收到的 msg, 就会错过群里的 ambient chatter. 真朋友会知道
"啊刚才 A 在跟 B 吵架的事我听到了". 这层让笨猫有这个能力.

实现:
- 单独 ambient_matcher priority=10, block=False (不阻塞其他 matcher) 累积所有
  GroupMessageEvent 到 per-scope deque
- 不持久化, 重启清空 (跟 NSFW sticky 同性质 — ambient 是短期听到, 不是长期记忆)
- per-scope deque maxlen=20, 30 分钟 TTL
- prompt_manager 注入时排除**当前发言者** (那个是 main user, 不算 ambient)

注入位置: order=265 (在 catty_mood=255 之后, 各种 contexts=600+ 之前).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


_MAX_AMBIENT_PER_SCOPE = 20      # per scope deque size — 30 min 内大概 20 条够拉
_AMBIENT_TTL_SECONDS = 30 * 60   # 30 分钟过期, 太久的 ambient 没意义
_PROMPT_MAX_COUNT = 3            # 主人 2026-05-28 C15-6: 6→3, 群聊 ambient 砍半
_TEXT_TRIM_CHARS = 50            # 主人 2026-05-28 C15-6: 80→50, 单条截更短


@dataclass
class AmbientMessage:
    user_id: str
    nickname: str
    text: str
    ts: float


class AmbientStore:
    """per-scope in-memory ambient buffer. 不持久化 — 重启清空."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, deque[AmbientMessage]] = {}

    def record(self, scope: str, user_id: str, nickname: str, text: str) -> None:
        """累积一条 ambient msg. 空 scope/text 跳过."""
        if not scope or not text:
            return
        text = text.strip()
        if not text:
            return
        if len(text) > _TEXT_TRIM_CHARS:
            text = text[:_TEXT_TRIM_CHARS] + "…"
        with self._lock:
            dq = self._data.setdefault(scope, deque(maxlen=_MAX_AMBIENT_PER_SCOPE))
            dq.append(AmbientMessage(
                user_id=str(user_id) if user_id else "",
                nickname=(nickname or "").strip() or f"用户{user_id}",
                text=text,
                ts=time.time(),
            ))

    def get_ambient(
        self,
        scope: str,
        *,
        exclude_user_id: str = "",
        max_count: int = _PROMPT_MAX_COUNT,
        max_age_seconds: int = _AMBIENT_TTL_SECONDS,
    ) -> list[AmbientMessage]:
        """拉最近的 ambient msg, 排除当前发言者 + 排除自己 (笨猫不算 ambient).

        返回按时间顺序 (老→新) list, 方便 prompt 展示。
        """
        if not scope:
            return []
        with self._lock:
            dq = self._data.get(scope)
            if not dq:
                return []
            now = time.time()
            cutoff = now - max_age_seconds
            out: list[AmbientMessage] = []
            # 从新到旧扫, 拿够 max_count 就停
            for m in reversed(dq):
                if m.ts < cutoff:
                    break
                if exclude_user_id and m.user_id == str(exclude_user_id):
                    continue
                out.append(m)
                if len(out) >= max_count:
                    break
            return list(reversed(out))  # 还原时间序

    def clear(self, scope: str) -> None:
        with self._lock:
            self._data.pop(scope, None)

    def snapshot(self) -> dict[str, int]:
        """诊断: 各 scope 当前 buffer 大小."""
        with self._lock:
            return {s: len(d) for s, d in self._data.items()}


# ── prompt 注入 ──────────────────────────────────────────────────────────


def build_ambient_prompt(messages: list[AmbientMessage]) -> str:
    """构建 ambient prompt 段. 空 list 返回 ""(skip register)."""
    if not messages:
        return ""
    now = time.time()
    lines = ["【笨猫·周边对话 (ambient, 听到但没人直接叫你)】"]
    for m in messages[-1:]:
        age_s = max(0.0, now - m.ts)
        if age_s < 120:
            age_label = "刚刚"
        elif age_s < 5 * 60:
            age_label = "几分钟前"
        elif age_s < 15 * 60:
            age_label = "十几分钟前"
        elif age_s < 30 * 60:
            age_label = "半小时内"
        else:
            age_label = "半小时前"
        nick = m.nickname or "?"
        text = " ".join(str(m.text or "").split())
        if len(text) > 64:
            text = text[:63].rstrip() + "…"
        lines.append(f"- [{age_label}] {nick}: {text}")
    lines.append(
        "↑ 旁听补丁: 当前 user 才是真在跟你说话; 只在明显接梗时轻带一句, 别逐条回应。"
    )
    return "\n".join(lines)


__all__ = [
    "AmbientMessage",
    "AmbientStore",
    "build_ambient_prompt",
]
