"""机机三状态随机切换 (主人 2026-08-10) — 丧女 / 魅魔 / 阳光。

背景: 机机本人情绪档位会波动, 但 2026-08-10 完整 QQ 实录表明自嘲、成人话题、
「我超」都只是条件用法, 不能因为随机状态被放大成全天常驻。三状态只做轻量调制,
不覆盖 core persona 的单条短回、事实边界和会话隔离。

设计:
- 状态每 [min,max] 分钟随机切换一次 (默认 60-180 分钟), roll 新状态时排除当前状态。
- 持久化到 memory_dir/fadianji_state.json — 重启不丢当前状态和到期时间。
- prompt 注入 order=503 (catty_menstrual 之后), 只对 fadianji 生效; catty 恒返回空串。
- hint 文本遵守机机底线: 无波浪号 / 无颜文字 / 短句, 不强迫输出 emoji。
- config 驱动 enabled + 切换间隔, 热重载生效。
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

from nonebot import logger


# ── 状态定义 ─────────────────────────────────────────────────────────
STATE_SANGNV = "sangnv"       # 丧女: 低能量/累/短回, 偶发自嘲
STATE_SUCCUBUS = "succubus"   # 魅魔: 成人群梗敏感度略升, 不强行性化
STATE_SUNSHINE = "sunshine"   # 阳光: 反馈更快, 真兴奋才偶发我超

_ALL_STATES: tuple[str, ...] = (STATE_SANGNV, STATE_SUCCUBUS, STATE_SUNSHINE)

_STATE_HINTS: dict[str, str] = {
    STATE_SANGNV: (
        "【机机当前状态 · 丧女】现在低能量: 累、困、不想动, 回复更短更平. "
        "可以直接说好累/没力气/有点难受; 真被戳中才偶尔自嘲或呜呜, 一轮一次够了. "
        "被哄就简短接住, 不硬撑元气, 也不无缘无故刷想死或绝望."
    ),
    STATE_SUCCUBUS: (
        "【机机当前状态 · 魅魔】现在更容易听懂并接成人群梗, 可以一句直白吐槽或反撩. "
        "只在当前话题已经相关时升一点浓度, 不把问候/技术/倾诉强行黄化, "
        "不机械打【健康】标签, 不展开成长篇色情角色扮演."
    ),
    STATE_SUNSHINE: (
        "【机机当前状态 · 阳光】现在更有精神, 接梗和反馈会快一点. "
        "可用好耶/我去, 真惊讶时偶尔我超; 仍以一条短回为默认, 不强制三连发或每句感叹号."
    ),
}

_DEFAULT_MIN_MINUTES = 60
_DEFAULT_MAX_MINUTES = 180


class FadianjiStateStore:
    """机机三状态随机切换 store, 落盘 memory_dir/fadianji_state.json。"""

    def __init__(self, memory_path: str | Path):
        mem_path = Path(memory_path).expanduser()
        if not mem_path.is_absolute():
            mem_path = mem_path.resolve()
        self._path = mem_path.parent / "fadianji_state.json"
        self._lock = threading.RLock()
        self._state: str = STATE_SUNSHINE
        self._expires_at: float = 0.0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"fadianji_state: load failed, starting fresh: {exc}")
            return
        if not isinstance(raw, dict):
            return
        state = str(raw.get("state", ""))
        if state in _ALL_STATES:
            self._state = state
        try:
            self._expires_at = float(raw.get("expires_at", 0.0))
        except (TypeError, ValueError):
            self._expires_at = 0.0

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(
                    {"state": self._state, "expires_at": self._expires_at},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # noqa: BLE001
            logger.debug(f"fadianji_state: save failed (non-fatal): {exc}")

    def current_state(
        self,
        *,
        min_minutes: int = _DEFAULT_MIN_MINUTES,
        max_minutes: int = _DEFAULT_MAX_MINUTES,
        now: float | None = None,
    ) -> str:
        """拿当前状态; 到期则 roll 新状态 (排除当前) 并落盘。"""
        n = now if now is not None else time.time()
        with self._lock:
            if n < self._expires_at:
                return self._state
            lo = max(int(min_minutes), 1)
            hi = max(int(max_minutes), lo)
            candidates = [s for s in _ALL_STATES if s != self._state]
            self._state = random.choice(candidates)
            self._expires_at = n + random.randint(lo, hi) * 60.0
            self._save()
            logger.info(
                f"fadianji_state: switched to {self._state} "
                f"(next switch in {int((self._expires_at - n) / 60)}min)"
            )
            return self._state


def build_state_prompt(
    store: FadianjiStateStore | None,
    config: Any,
    persona_name: str,
) -> str:
    """构建当前状态 hint。非 fadianji / 未启用 / store 缺失 → 空串。"""
    if store is None or not config:
        return ""
    if not bool(getattr(config, "catty_fadianji_state_enabled", False)):
        return ""
    if str(persona_name or "").strip().lower() != "fadianji":
        return ""
    state = store.current_state(
        min_minutes=int(getattr(config, "catty_fadianji_state_min_minutes", _DEFAULT_MIN_MINUTES) or _DEFAULT_MIN_MINUTES),
        max_minutes=int(getattr(config, "catty_fadianji_state_max_minutes", _DEFAULT_MAX_MINUTES) or _DEFAULT_MAX_MINUTES),
    )
    return _STATE_HINTS.get(state, "")
