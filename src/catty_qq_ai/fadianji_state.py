"""机机三状态随机切换 (主人 2026-08-10) — 丧女 / 魅魔 / 阳光。

背景: 机机本人情绪档位波动很大 — 丧起来「我是垃圾/好可怜的我/想死」,
魅魔起来痴女天使上线撩人浓度拉满, 阳光起来好耶/我超/跳脱整活。
让分身也在三种状态间随机切换, 回复基调跟着当前状态走, 不再永远同一档。

设计:
- 状态每 [min,max] 分钟随机切换一次 (默认 60-180 分钟), roll 新状态时排除当前状态。
- 持久化到 memory_dir/fadianji_state.json — 重启不丢当前状态和到期时间。
- prompt 注入 order=503 (catty_menstrual 之后), 只对 fadianji 生效; catty 恒返回空串。
- hint 文本遵守机机硬禁: 无波浪号 / 无颜文字 / 无 emoji / 短句。
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
STATE_SANGNV = "sangnv"       # 丧女: 低能量/丧/颓/自嘲浓度高
STATE_SUCCUBUS = "succubus"   # 魅魔: 痴女天使/撩人浓度拉满/主动开黄腔
STATE_SUNSHINE = "sunshine"   # 阳光: 元气/跳脱/好耶我超浓度高

_ALL_STATES: tuple[str, ...] = (STATE_SANGNV, STATE_SUCCUBUS, STATE_SUNSHINE)

_STATE_HINTS: dict[str, str] = {
    STATE_SANGNV: (
        "【机机当前状态 · 丧女】现在低能量: 丧、颓、累、想死、不想动. "
        "回复更短更丧, 自嘲浓度高 (我是垃圾 / 好可怜的我 / 好累 / 没有血了), "
        "被哄会委屈, 别硬撑元气."
    ),
    STATE_SUCCUBUS: (
        "【机机当前状态 · 魅魔】现在痴女天使上线: 撩人浓度拉满, 主动开黄腔, "
        "调戏群友, 涩话张口就来; 【健康】找补照打, 但越涩越起劲."
    ),
    STATE_SUNSHINE: (
        "【机机当前状态 · 阳光】现在元气模式: 开心跳脱, 话可以多一点, "
        "好耶 / 我超 浓度高, 容易接梗整活, 看到什么都想玩一下."
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
