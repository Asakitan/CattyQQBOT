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

2026-08-13 事件驱动覆盖:
- 在旧三状态定时随机轨之外, 被夸/做成事/破防/连环起哄时可立即覆盖到对应状态。
- 事件覆盖默认持续 15 分钟, 到期自动回到原定时随机轨; 同一事件 3 分钟内防抖。
- 覆盖状态和到期时间与定时状态一起落盘, 兼容旧版只有 state/expires_at 的 JSON。
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
STATE_PROUD = "proud"         # 得意: 被夸/刚做成事, 小膨胀但不长篇自夸
STATE_BROKEN = "broken"       # 破防: 被拆穿/连续翻车/被戳痛处
STATE_RAMPAGE = "rampage"     # 暴走: 被连环逗弄/群友起哄

_RANDOM_STATES: tuple[str, ...] = (STATE_SANGNV, STATE_SUCCUBUS, STATE_SUNSHINE)
_ALL_STATES: tuple[str, ...] = _RANDOM_STATES + (STATE_PROUD, STATE_BROKEN, STATE_RAMPAGE)

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
    STATE_PROUD: (
        "【机机当前状态 · 得意】刚被夸到或做成了事, 可以小膨胀一点并嘴硬两句. "
        "得意归得意, 仍以一到两句短回为主, 不长篇自夸或反复邀功."
    ),
    STATE_BROKEN: (
        "【机机当前状态 · 破防】刚被拆穿、连续翻车或戳到痛处, 可以连发两到三个短气泡, "
        "也可以复读同一句短话. 偶尔呜呜或😭就够了, 结尾自己收住, 不把破防拖成长篇."
    ),
    STATE_RAMPAGE: (
        "【机机当前状态 · 暴走】刚被连环逗弄或群友起哄, 可以跳脸并用中档爆粗, "
        "例如滚啊/有病吧/傻逼档. 一轮收住, 不追着同一个人连续骂."
    ),
}

_DEFAULT_MIN_MINUTES = 60
_DEFAULT_MAX_MINUTES = 180
_DEFAULT_OVERRIDE_TTL_MINUTES = 15
_EVENT_DEBOUNCE_SECONDS = 3 * 60
_EVENT_STATE_MAP: dict[str, str] = {
    "praised": STATE_PROUD,
    "success": STATE_PROUD,
    "exposed": STATE_BROKEN,
    "fail_streak": STATE_BROKEN,
    "teased_streak": STATE_RAMPAGE,
}


class FadianjiStateStore:
    """机机状态随机切换与事件覆盖 store, 落盘 memory_dir/fadianji_state.json。"""

    def __init__(self, memory_path: str | Path):
        mem_path = Path(memory_path).expanduser()
        if not mem_path.is_absolute():
            mem_path = mem_path.resolve()
        self._path = mem_path.parent / "fadianji_state.json"
        self._lock = threading.RLock()
        self._state: str = STATE_SUNSHINE
        self._expires_at: float = 0.0
        self._override_state: str | None = None
        self._override_expires_at: float = 0.0
        self._override_ttl_minutes: int = _DEFAULT_OVERRIDE_TTL_MINUTES
        self._last_event: str | None = None
        self._last_event_at: float = 0.0
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
        if state in _RANDOM_STATES:
            self._state = state
        try:
            self._expires_at = float(raw.get("expires_at", 0.0))
        except (TypeError, ValueError):
            self._expires_at = 0.0
        override_state = str(raw.get("override_state", ""))
        self._override_state = override_state if override_state in _ALL_STATES else None
        try:
            self._override_expires_at = float(raw.get("override_expires_at", 0.0))
        except (TypeError, ValueError):
            self._override_expires_at = 0.0
        if self._override_state is None:
            self._override_expires_at = 0.0
        last_event = str(raw.get("last_event", "")).strip().lower()
        self._last_event = last_event or None
        try:
            self._last_event_at = float(raw.get("last_event_at", 0.0))
        except (TypeError, ValueError):
            self._last_event_at = 0.0

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(
                    {
                        "state": self._state,
                        "expires_at": self._expires_at,
                        "override_state": self._override_state,
                        "override_expires_at": self._override_expires_at,
                        "last_event": self._last_event,
                        "last_event_at": self._last_event_at,
                    },
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
        """拿当前状态; 事件覆盖优先, 覆盖到期后回到三态定时随机轨。"""
        n = now if now is not None else time.time()
        with self._lock:
            if self._override_state is not None:
                if n < self._override_expires_at:
                    return self._override_state
                self._override_state = None
                self._override_expires_at = 0.0
                self._save()
            if n < self._expires_at:
                return self._state
            lo = max(int(min_minutes), 1)
            hi = max(int(max_minutes), lo)
            candidates = [s for s in _RANDOM_STATES if s != self._state]
            self._state = random.choice(candidates)
            self._expires_at = n + random.randint(lo, hi) * 60.0
            self._save()
            logger.info(
                f"fadianji_state: switched to {self._state} "
                f"(next switch in {int((self._expires_at - n) / 60)}min)"
            )
            return self._state

    def apply_event(self, event: str, *, now: float | None = None) -> str | None:
        """应用一次状态事件; 未知事件不动状态, comforted 清除事件覆盖。"""
        n = now if now is not None else time.time()
        normalized = str(event or "").strip().lower()
        with self._lock:
            if normalized == "comforted":
                self._override_state = None
                self._override_expires_at = 0.0
                self._last_event = None
                self._last_event_at = 0.0
                self._save()
                return self.current_state(now=n)
            target = _EVENT_STATE_MAP.get(normalized)
            if target is None:
                return None
            if (
                self._last_event == normalized
                and 0.0 <= n - self._last_event_at < _EVENT_DEBOUNCE_SECONDS
            ):
                return self.current_state(now=n)
            self._override_state = target
            self._override_expires_at = n + self._override_ttl_minutes * 60.0
            self._last_event = normalized
            self._last_event_at = n
            self._save()
            return target

    def set_override_ttl_minutes(self, minutes: int) -> None:
        """设置事件覆盖 TTL; prompt/config 层可在事件处理前更新。"""
        with self._lock:
            self._override_ttl_minutes = max(int(minutes), 1)


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
    store.set_override_ttl_minutes(
        int(
            getattr(
                config,
                "catty_fadianji_event_override_minutes",
                _DEFAULT_OVERRIDE_TTL_MINUTES,
            )
            or _DEFAULT_OVERRIDE_TTL_MINUTES
        )
    )
    state = store.current_state(
        min_minutes=int(getattr(config, "catty_fadianji_state_min_minutes", _DEFAULT_MIN_MINUTES) or _DEFAULT_MIN_MINUTES),
        max_minutes=int(getattr(config, "catty_fadianji_state_max_minutes", _DEFAULT_MAX_MINUTES) or _DEFAULT_MAX_MINUTES),
    )
    hint = _STATE_HINTS.get(state, "")
    # 主人 2026-08-13 (二轮): 情绪事件判断交给主 AI — 回复末尾自报 marker,
    # harness 提取后驱动事件状态机, 不再单独发 audit 分类小调用。
    if hint and bool(getattr(config, "catty_fadianji_event_mood_enabled", True)):
        hint += (
            "\n情绪自我标记: 本轮若明显被夸/被拆穿/连续翻车/被起哄/被安慰/刚做成事, "
            "在回复末尾加 <<<CATTY_FD_MOOD:praised|exposed|fail_streak|teased_streak|comforted|success>>> 之一; "
            "不明显就不加, 不解释这个标记, 它会被系统吃掉不会发给用户."
        )
    return hint
