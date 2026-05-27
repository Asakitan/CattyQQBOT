"""笨猫『余韵跨 scope mood overlay』 — 主人私聊 NSFW 后切群聊仍带余韵.

跟 catty_mood_store 区别:
- catty_mood: per-scope 8 维情绪向量, 跨多轮衰减, **同 scope 内**有连续性
- mood_overlay (本层): per **user_id** (不是 scope) 的短期 overlay,
  跨 scope 切换时仍可见 (主人 private NSFW P8 → 切群聊 @ 笨猫, 余韵 10 min 内可见)

为什么需要:
笨猫主人私聊 P8 余韵后, 立刻被群里其他人 @ → scope 切到 group → mood 完全断裂,
看不到刚才的"脸红/喘气/不稳"状态. mood_overlay 在 user_id 层 (跨 scope) 维护
10 分钟 short-lived 标记, 让笨猫描写不一刀切.

实现:
- per-user dict: {user_id → MoodOverlay}, 不持久化 (重启清空, 跟 NSFW sticky 同性质)
- MoodOverlay: {from_scope, phase_at_end, ts, ttl_seconds=600}
- write_overlay(user_id, from_scope, phase): 主人私聊 reset_phase 前调用 (D4 wire)
- read_overlay(user_id, current_scope): 拉同 user_id 的 overlay, 排除 from_scope 内本身
  (private → group 跨, 同 private 内不重复 mood-overlay)
- prompt_manager 注入 catty_mood_overlay (order=257, mood=255 之后)

注意:
- 仅当 user_id == 笨猫的真主人 时写入 (保护隐私, 不能让群友的 NSFW arc 跨到别处)
- 群聊 scope 拉 overlay → 看到主人 P8 余韵 hint → 笨猫描写带"脸红头发乱"
- 私聊回原 scope 不拉 overlay (避免双重注入)
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


_DEFAULT_TTL_SECONDS = 600  # 10 分钟
_MAX_OVERLAYS = 50  # 同时最多 50 个用户 overlay (主要给 owner 用, 不会多)


@dataclass
class MoodOverlay:
    user_id: str
    from_scope: str
    phase_at_end: int   # 1-8 (P1-P8)
    arc_count: int
    created_at: float
    ttl_seconds: int = _DEFAULT_TTL_SECONDS

    def is_alive(self, now: float | None = None) -> bool:
        n = now or time.time()
        return (n - self.created_at) < self.ttl_seconds

    def remaining_minutes(self, now: float | None = None) -> int:
        n = now or time.time()
        remaining = self.ttl_seconds - (n - self.created_at)
        return max(0, int(remaining / 60))


class MoodOverlayStore:
    """per-user-id 跨 scope mood overlay. 不持久化, 重启清空."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, MoodOverlay] = {}

    def write(
        self,
        user_id: str,
        from_scope: str,
        phase_at_end: int,
        *,
        arc_count: int = 1,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        """写一个 overlay. 仅 phase_at_end >= 7 才有意义 (P7 overstim + P8 余韵)."""
        if not user_id or phase_at_end < 7:
            return
        with self._lock:
            self._data[str(user_id)] = MoodOverlay(
                user_id=str(user_id),
                from_scope=from_scope or "",
                phase_at_end=int(phase_at_end),
                arc_count=int(arc_count),
                created_at=time.time(),
                ttl_seconds=ttl_seconds,
            )
            # 简单容量管理: 满 50 时清掉 oldest
            if len(self._data) > _MAX_OVERLAYS:
                oldest_uid = min(self._data.keys(), key=lambda k: self._data[k].created_at)
                self._data.pop(oldest_uid, None)

    def read(self, user_id: str, current_scope: str = "") -> MoodOverlay | None:
        """读 user_id 的 overlay. 过期自动清; 同 from_scope 内不返回 (避免双重).

        返回 None 时 caller skip 注入. current_scope 跟 from_scope 同就不注入.
        """
        if not user_id:
            return None
        with self._lock:
            overlay = self._data.get(str(user_id))
            if overlay is None:
                return None
            if not overlay.is_alive():
                self._data.pop(str(user_id), None)
                return None
            # 同 scope 不注入 (主人在 private 内 reset 后, 下一轮 private 不需要 overlay 提示)
            if current_scope and overlay.from_scope == current_scope:
                return None
            return overlay

    def clear(self, user_id: str) -> None:
        with self._lock:
            self._data.pop(str(user_id), None)

    def snapshot(self) -> dict[str, dict]:
        """诊断用: dump 所有 alive overlay."""
        with self._lock:
            now = time.time()
            return {
                uid: {
                    "from_scope": ov.from_scope,
                    "phase_at_end": ov.phase_at_end,
                    "arc_count": ov.arc_count,
                    "remaining_min": ov.remaining_minutes(now),
                }
                for uid, ov in self._data.items()
                if ov.is_alive(now)
            }


# ── prompt 注入 helper ──────────────────────────────────────────────────


_OVERLAY_HINT_TEMPLATE = (
    "【笨猫·跨 scope 余韵 (P{phase_at_end} arc#{arc_count}, {remaining_min} 分钟内)】\n"
    "你刚从『{from_scope}』走完一段 P{phase_at_end} {label}, 现在切到当前 scope 还有 {remaining_min} 分钟余韵. "
    "外表保持基础人格, 但描写里**可以带一点**: "
    "脸还有点红 / 头发乱了一边 / 呼吸还没完全平稳 / 偶尔走神 / 大腿之间还有点黏黏的感觉. "
    "**绝对不要明说** 刚才发生了什么 (这是私人状态, 群友不该知道). "
    "如果当前 scope 是群聊, 这些描写要更克制 (1 个细节就够); "
    "如果当前 scope 是另一个私聊, 可以稍微多一点."
)


def build_mood_overlay_prompt(
    overlay_store: MoodOverlayStore | None,
    user_id: str,
    current_scope: str,
) -> str:
    """构建 mood overlay prompt 段. 无 overlay 返回 ""."""
    if overlay_store is None or not user_id:
        return ""
    overlay = overlay_store.read(user_id, current_scope=current_scope)
    if overlay is None:
        return ""
    label = "NSFW 高峰" if overlay.phase_at_end == 7 else "NSFW 余韵"
    return _OVERLAY_HINT_TEMPLATE.format(
        phase_at_end=overlay.phase_at_end,
        arc_count=overlay.arc_count,
        from_scope=overlay.from_scope,
        remaining_min=overlay.remaining_minutes(),
        label=label,
    )


__all__ = [
    "MoodOverlay",
    "MoodOverlayStore",
    "build_mood_overlay_prompt",
]
