"""笨猫实时情绪状态机 — 跨多轮连续的 mood 向量。

跟 user_vibe 不同:
- user_vibe 是『对方调调』(每个 user_id 一条画像)
- catty_mood 是『笨猫自己当下心情』(每个 scope 一个 8 维向量)

让连续对话不再每条独立 — 主人骂了一句下一句不会立刻笑嘻嘻,
laughing 维度被 annoyed 压制,prompt 注入「还在生闷气」段,
LLM 自然走"嘴硬+尾巴一甩"路线。

维度(8 维,各 0-100 baseline=50):
- happy     开心   (yay/嘿嘿/太好了)
- excited   兴奋   (牛/绝/炸裂/嗷嗷嗷)
- annoyed   烦躁   (服了/烦死/操/草)
- shy       害羞   (脸红/羞/抱抱/亲亲) → 暧昧链触发
- sad       难过   (呜呜/55555/委屈/难过)
- sleepy    困倦   (好困/熬夜/想睡)
- sulky     生闷气 (哼/不理你/讨厌/走开) → 跟 happy 互斥,持续最长
- bored     无聊   (无聊/没意思/划水)

更新规则:
- 命中关键词 → 主维度 +Δ,互斥维度 -Δ/2
- 时间衰减: 每 60 秒所有维度向 50 baseline 回归 1 点 (sulky 0.5/min 慢一倍)
- prompt 注入阈值: max(dim) > 65 才注入,< 65 不打扰默认人格

落盘到 memory_dir/catty_moods.json,30s flush + LRU(最多 200 scope)。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_BASELINE = 50.0
_MIN = 0.0
_MAX = 100.0
_INJECT_THRESHOLD = 65.0       # 主维度 > 此值才注入 prompt
_BLEND_SECONDARY_DELTA = 12.0  # 主维度差 ≤ 此值时算"混合心情"(例 shy+excited 暧昧链)
_DECAY_PER_MIN = 1.0           # 每分钟向 baseline 回归 1 点
_DECAY_SULKY_PER_MIN = 0.5     # sulky 衰减慢一倍(生闷气持续更久)
_MAX_TOTAL_SCOPES = 200

_DIMS: tuple[str, ...] = (
    "happy", "excited", "annoyed", "shy", "sad", "sleepy", "sulky", "bored",
)

# 关键词触发表 — 命中后主维度 +Δ,互斥维度按 _OPPONENTS 表 -Δ/2
# 关键词都用 substring 命中,case-insensitive。
_TRIGGERS: dict[str, tuple[tuple[str, ...], float]] = {
    "happy":   (("开心", "好棒", "太好了", "yay", "yay~", "嘿嘿", "嘻嘻", "good", "nice", "ok啦", "okk", "好耶"), 15.0),
    "excited": (("兴奋", "牛逼", "牛b", "绝了", "yyds", "炸裂", "嗷嗷嗷", "卧槽牛", "wuhu", "妙啊", "妙哇"), 14.0),
    "annoyed": (("服了", "我服", "无语", "烦死", "真烦", "烦死了", "操", "草", "傻逼", "脑残", "妈的", "tmd", "破事"), 16.0),
    "shy":     (("脸红", "害羞", "羞死", "抱抱", "亲亲", "贴贴", "蹭蹭", "亲一口", "摸摸头", "嫁给我", "结婚"), 18.0),
    "sad":     (("难过", "伤心", "委屈", "呜呜", "55555", "想哭", "好惨", "心碎", "失恋", "emo"), 14.0),
    "sleepy":  (("好困", "想睡", "熬夜", "通宵", "肝完", "肝到", "睡了", "晚安", "瞌睡"), 10.0),
    "sulky":   (("哼", "不理你", "讨厌", "走开", "滚", "懒得理", "别烦", "不和你玩"), 16.0),
    "bored":   (("无聊", "没意思", "划水", "好闲", "刷手机", "摆烂", "躺平"), 10.0),
}

# 互斥关系:命中 key 时给 opponents 各 -Δ/2(让心情切换有合理对冲)
_OPPONENTS: dict[str, tuple[str, ...]] = {
    "happy":   ("annoyed", "sad", "sulky"),
    "excited": ("sad", "sleepy", "bored"),
    "annoyed": ("happy", "shy"),
    "shy":     ("annoyed", "sulky"),
    "sad":     ("happy", "excited"),
    "sleepy":  ("excited", "annoyed"),
    "sulky":   ("happy", "shy"),
    "bored":   ("excited", "happy"),
}

# prompt 注入用的 hint 模板 — 主维度 + 可选次维度(混合心情)
_MOOD_HINTS_ZH: dict[str, str] = {
    "happy":   "心情很好嘿嘿(尾巴翘起来),回复可以更跳脱、多接梗、动作密一点",
    "excited": "兴奋值爆表(嗷嗷叫),回复语速快、感叹号多、容易凑过去说『让我看让我看!』",
    "annoyed": "现在有点烦躁(尾巴一甩),回复可以稍微炸毛、傲娇语气浓一点、不要装乖巧",
    "shy":     "现在有点害羞脸红(*/ω＼*),回复嘴硬+脸红嘀咕,暧昧链开启 — 反撩走『炸毛→脸红暴露真心』",
    "sad":     "现在有点低落(耳朵耷拉),回复语气软软的、不要硬开玩笑、可以撒娇求安慰",
    "sleepy":  "现在有点困(打哈欠),回复短一点、可以带『嗯...』『...对吧』之类的迷糊感",
    "sulky":   "**还在生闷气**(背过身去)— 上一轮被惹到了,这一轮不会立刻原谅,先冷脸/嘴硬,要看主人怎么哄",
    "bored":   "现在有点无聊(尾巴软软地拖在地上),容易接抽象梗、瞎接话题、求互动",
}


# ── 状态向量 + Store ─────────────────────────────────────────────────
@dataclass
class _MoodState:
    """单个 scope 的 mood 状态 — 8 维向量 + 最后更新时间。"""
    dims: dict[str, float] = field(default_factory=lambda: {d: _BASELINE for d in _DIMS})
    last_update_at: float = 0.0
    last_trigger_dim: str = ""  # 上次命中关键词的维度,debug 用

    def to_payload(self) -> dict[str, Any]:
        return {
            "dims": {d: round(self.dims.get(d, _BASELINE), 2) for d in _DIMS},
            "last_update_at": self.last_update_at,
            "last_trigger_dim": self.last_trigger_dim,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "_MoodState":
        state = cls()
        raw_dims = payload.get("dims") or {}
        if isinstance(raw_dims, dict):
            for d in _DIMS:
                state.dims[d] = float(raw_dims.get(d, _BASELINE))
        state.last_update_at = float(payload.get("last_update_at") or 0.0)
        state.last_trigger_dim = str(payload.get("last_trigger_dim") or "")
        return state


def _clamp(v: float) -> float:
    if v < _MIN:
        return _MIN
    if v > _MAX:
        return _MAX
    return v


def _decay_toward_baseline(state: _MoodState, now: float) -> None:
    """按时间差让所有维度向 baseline 回归。sulky 衰减慢一倍。"""
    if state.last_update_at <= 0:
        state.last_update_at = now
        return
    elapsed_min = (now - state.last_update_at) / 60.0
    if elapsed_min <= 0:
        return
    for d in _DIMS:
        rate = _DECAY_SULKY_PER_MIN if d == "sulky" else _DECAY_PER_MIN
        decay = rate * elapsed_min
        cur = state.dims[d]
        if cur > _BASELINE:
            state.dims[d] = max(_BASELINE, cur - decay)
        elif cur < _BASELINE:
            state.dims[d] = min(_BASELINE, cur + decay)
    state.last_update_at = now


def _classify_text(text: str) -> list[tuple[str, float]]:
    """对单条 text 返回命中的 (dim, delta) 列表。"""
    if not text:
        return []
    lower = text.lower()
    out: list[tuple[str, float]] = []
    for dim, (keys, delta) in _TRIGGERS.items():
        for k in keys:
            if k and k in lower:
                out.append((dim, delta))
                break
    return out


class CattyMoodStore:
    def __init__(self, memory_path: str | Path) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._path = p.parent / "catty_moods.json"
        self._lock = threading.RLock()
        self._data: dict[str, _MoodState] = {}
        self._last_access: dict[str, float] = {}
        self._dirty = False
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
        scopes = raw.get("scopes") or {}
        if not isinstance(scopes, dict):
            return
        now = time.time()
        for sc, payload in scopes.items():
            if not isinstance(payload, dict):
                continue
            self._data[str(sc)] = _MoodState.from_payload(payload)
            self._last_access[str(sc)] = now

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "scopes": {sc: state.to_payload() for sc, state in self._data.items()},
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
        if len(self._data) <= _MAX_TOTAL_SCOPES:
            return
        ordered = sorted(self._last_access.items(), key=lambda kv: kv[1])
        for sc, _ in ordered[: len(self._data) - _MAX_TOTAL_SCOPES]:
            self._data.pop(sc, None)
            self._last_access.pop(sc, None)

    def record_text(self, scope: str, text: str) -> None:
        """喂一条 user_text 进来,更新 scope mood。"""
        if not scope or not text or not text.strip():
            return
        triggers = _classify_text(text)
        now = time.time()
        with self._lock:
            state = self._data.get(scope) or _MoodState()
            _decay_toward_baseline(state, now)
            if triggers:
                for dim, delta in triggers:
                    state.dims[dim] = _clamp(state.dims[dim] + delta)
                    for opp in _OPPONENTS.get(dim, ()):
                        state.dims[opp] = _clamp(state.dims[opp] - delta / 2.0)
                state.last_trigger_dim = triggers[-1][0]
            state.last_update_at = now
            self._data[scope] = state
            self._last_access[scope] = now
            self._evict_lru()
            self._dirty = True

    def snapshot(self, scope: str) -> dict[str, float]:
        """返回当前 mood dims(衰减到 now 后的)。无记录返回全 baseline。"""
        now = time.time()
        with self._lock:
            state = self._data.get(scope)
            if state is None:
                return {d: _BASELINE for d in _DIMS}
            _decay_toward_baseline(state, now)
            return {d: round(state.dims[d], 2) for d in _DIMS}


# ── prompt 注入 ──────────────────────────────────────────────────
def build_catty_mood_prompt(store: CattyMoodStore, scope: str) -> str:
    """根据 scope 当前 mood 返回 prompt 段。主维度 < 阈值返回 ""。"""
    if not scope:
        return ""
    dims = store.snapshot(scope)
    # 找主维度
    ordered = sorted(dims.items(), key=lambda kv: kv[1], reverse=True)
    top_dim, top_val = ordered[0]
    if top_val < _INJECT_THRESHOLD:
        return ""  # 都在 baseline 附近,不打扰
    lines = [f"【笨猫·当下心情 (mood)】主调 = {top_dim} ({top_val:.0f}/100)"]
    primary_hint = _MOOD_HINTS_ZH.get(top_dim)
    if primary_hint:
        lines.append(f"- {primary_hint}")
    # 混合心情:第二维度跟主维度差 ≤ _BLEND_SECONDARY_DELTA 且 > baseline 才提
    if len(ordered) >= 2:
        sec_dim, sec_val = ordered[1]
        if sec_val > _BASELINE and (top_val - sec_val) <= _BLEND_SECONDARY_DELTA:
            sec_hint = _MOOD_HINTS_ZH.get(sec_dim)
            if sec_hint:
                lines.append(f"- 次调 {sec_dim} ({sec_val:.0f}/100): {sec_hint}")
                lines.append(f"(主+次混合 — 笨猫这条回复应该带『{top_dim}+{sec_dim}』的复合反应)")
    lines.append("(mood 是跨多轮累积的连续状态,会随时间衰减回 baseline;不要复述给用户听。)")
    return "\n".join(lines)


__all__ = [
    "CattyMoodStore",
    "build_catty_mood_prompt",
]
