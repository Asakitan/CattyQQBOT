"""本体避让 (body presence avoid) — 机机本体在场时, 分身让位。

背景 (主人 2026-08-10): 机机本人 (QQ 3062800942) 也在自己的粉丝群 922298923 里,
跟分身(小机)同群。本体发言时分身应该退到幕后 — 群里所有消息都忽视,
只有本体本人 @ 分身时才回 (本体在场, 备用机让位)。

设计:
- watches 纯 config 驱动 (config.json 的 body_presence 段), 热重载自动生效。
- 本体活跃时间戳存内存, 不持久化 — 重启清空, 避让状态自然解除重新积累。
- _rule 入口逻辑:
  * 本体消息 → touch 记录活跃 + 走正常流程 (她 @ 分身则回, 不 @ 则 mention-only gate 照常 drop)
  * 非本体消息 → 本群在 cooldown 窗内 → drop (无论是否 @ 分身、是否续聊窗口)
  * 无匹配 watch 的群 / enabled=False → 零开销跳过
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

_DEFAULT_COOLDOWN_MINUTES = 30.0


@dataclass(frozen=True, slots=True)
class BodyPresenceWatch:
    """一条本体避让规则: group_id 群里 user_id 是本体, 本体活跃后 cooldown 内分身让位。"""
    group_id: str
    user_id: str
    cooldown_seconds: float = _DEFAULT_COOLDOWN_MINUTES * 60.0


def parse_watches(raw: Any) -> tuple[BodyPresenceWatch, ...]:
    """config catty_body_presence_watches (list[dict]) → 规范化 watches。非法条目跳过。"""
    if not raw:
        return ()
    out: list[BodyPresenceWatch] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        gid = str(item.get("group_id") or "").strip()
        uid = str(item.get("user_id") or "").strip()
        if not gid or not uid:
            continue
        try:
            cd = max(float(item.get("cooldown_minutes", _DEFAULT_COOLDOWN_MINUTES)), 1.0) * 60.0
        except (TypeError, ValueError):
            cd = _DEFAULT_COOLDOWN_MINUTES * 60.0
        out.append(BodyPresenceWatch(group_id=gid, user_id=uid, cooldown_seconds=cd))
    return tuple(out)


class BodyPresenceStore:
    """per-group 本体最近活跃时间。内存态, 不持久化 (重启清空, 避让自然解除)。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_active: dict[str, float] = {}

    def touch(self, group_id: str, now: float | None = None) -> None:
        with self._lock:
            self._last_active[str(group_id)] = now if now is not None else time.time()

    def in_avoid(self, group_id: str, cooldown_seconds: float, now: float | None = None) -> bool:
        """group_id 群是否处于本体避让窗 (本体最近 cooldown_seconds 内发言过)。"""
        with self._lock:
            last = self._last_active.get(str(group_id), 0.0)
        if last <= 0.0:
            return False
        return ((now if now is not None else time.time()) - last) < float(cooldown_seconds)
