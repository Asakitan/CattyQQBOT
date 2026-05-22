"""群消息节奏 / 氛围本地感知 —— 让笨猫根据群当前节奏自动调整发言风格。

设计目标:
- 看最近 N 条群消息(deque 已经在 __init__.py 维护),纯本地 O(n) 统计。
- 给出一个粗粒度的 phase 标签(冷场/正常/热闹/刷屏/复读) + 1-2 条人话观察。
- 输出是给主回复 system prompt 的短提示,让 AI 自然调整语气长度,而不是硬切风格。
- **不替 AI 决策**:phase 只是建议,AI 可以基于其它信号自己拍板。

phase 判定阈值(基于最近窗口):
- 冷场: 5 分钟内 < 2 条消息(非 bot)
- 刷屏: 同一非 bot 用户在 90 秒内连发 ≥ 4 条
- 复读: 90 秒内 ≥ 3 条非 bot 消息文本相似度极高(完全重复或仅差 1-3 字)
- 热闹: 最近 2 分钟内 ≥ 10 条非 bot 消息且 ≥ 3 个不同发言者
- 正常: 其它

输入用 monotonic timestamp(来自 time.monotonic()),不依赖 wall clock,
和 RecentConversationMessage.created_at 保持一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol


# ── 阈值常量(秒/条) ──────────────────────────────────────────────────

_COLD_WINDOW_SEC = 5 * 60
_COLD_MAX_MSGS = 1  # 5 分钟内 <=1 条非 bot 即冷场

_BURST_WINDOW_SEC = 90
_BURST_MIN_MSGS = 4  # 同用户 90 秒 ≥ 4 条 = 刷屏

_ECHO_WINDOW_SEC = 90
_ECHO_MIN_REPEATS = 3  # 90 秒内 ≥3 条相似文本 = 复读

_BUSY_WINDOW_SEC = 2 * 60
_BUSY_MIN_MSGS = 10
_BUSY_MIN_PARTICIPANTS = 3


# ── 输入协议 ─────────────────────────────────────────────────────────

class _MsgLike(Protocol):
    """复用 __init__.py 的 RecentConversationMessage 字段,不强依赖具体类。"""
    user_id: str
    display_name: str
    text: str
    created_at: float
    is_bot: bool


@dataclass(slots=True)
class PulseResult:
    phase: str               # cold / busy / burst / echo / normal
    msg_count_2min: int      # 过去 2 分钟非 bot 消息条数
    participants_2min: int   # 过去 2 分钟非 bot 不同用户数
    seconds_since_last: float  # 距最近一条非 bot 消息多少秒(无消息 → inf)
    observations: list[str]  # 给 AI 的简短人话观察(1-3 条)
    burst_user: str = ""     # 刷屏的用户 display_name(仅 phase=burst 时)
    echo_text: str = ""      # 复读的代表文本(仅 phase=echo 时,截前 30 字)
    reply_style_hint: str = ""  # 该 phase 下建议的回复风格/段数/字数 cap,空串表示无特殊建议

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "phase": self.phase,
            "msg_count_2min": self.msg_count_2min,
            "participants_2min": self.participants_2min,
            "seconds_since_last": (
                round(self.seconds_since_last, 1)
                if self.seconds_since_last != float("inf")
                else None
            ),
            "observations": list(self.observations),
        }
        if self.burst_user:
            d["burst_user"] = self.burst_user
        if self.echo_text:
            d["echo_text"] = self.echo_text
        return d


# ── 正规化:文本相似度判定(complete equal or near-equal) ──────────

def _normalize_for_echo(text: str) -> str:
    """去标点/空白后小写,用于复读判定。允许『xs』和『xs!』算同一条。"""
    if not text:
        return ""
    keep = "".join(c for c in text if c.isalnum() or "一" <= c <= "鿿")
    return keep.lower()


# ── 主分析函数 ────────────────────────────────────────────────────────

def analyze_pulse(messages: Iterable[_MsgLike], *, now: float) -> PulseResult:
    """对给定 deque(或任意可迭代 RecentConversationMessage)做节奏分析。

    Args:
        messages: 任意可迭代,元素需有 user_id/text/created_at/is_bot 字段。
        now: 当前 monotonic 时间戳(从 caller 传入,便于测试注入)。
    """
    # 拷贝成 list 一次,后面多次扫描;按 created_at 升序排,确保 user_msgs[-1] 是最新
    # (生产用 deque 已经按插入序自然有序,排序是 idempotent 的双保险)
    items = sorted(messages, key=lambda m: m.created_at)
    # 只看非 bot 消息(bot 自己的发言不算群氛围 — 否则猫猫发了 4 条会把自己判定成刷屏)
    user_msgs = [m for m in items if not getattr(m, "is_bot", False)]

    # ── 基本统计:过去 2 分钟 ──
    cutoff_2min = now - _BUSY_WINDOW_SEC
    recent_2min = [m for m in user_msgs if m.created_at >= cutoff_2min]
    msg_count_2min = len(recent_2min)
    participants_2min = len({m.user_id for m in recent_2min})
    seconds_since_last = (
        (now - user_msgs[-1].created_at) if user_msgs else float("inf")
    )

    observations: list[str] = []
    phase = "normal"
    burst_user = ""
    echo_text = ""
    reply_style_hint = ""

    # ── 冷场检测 ──
    cutoff_cold = now - _COLD_WINDOW_SEC
    cold_window_msgs = [m for m in user_msgs if m.created_at >= cutoff_cold]
    if len(cold_window_msgs) <= _COLD_MAX_MSGS:
        phase = "cold"
        if seconds_since_last == float("inf"):
            observations.append("群里这段时间一条消息都没有,你是开场第一位")
        else:
            minutes = int(seconds_since_last // 60)
            observations.append(
                f"群里冷了大约 {minutes} 分钟了,你可以轻一点回,或者起个新话题"
            )
        # 冷场:可以稍长(1-2 段, ≤50 字/段)带一个开放话题,而不是只回一个『嗯』
        reply_style_hint = "回复段数 1-2,每段 ≤50 字;可以带一个开放话题或提问,引人接话"
        return PulseResult(
            phase=phase,
            msg_count_2min=msg_count_2min,
            participants_2min=participants_2min,
            seconds_since_last=seconds_since_last,
            observations=observations,
            reply_style_hint=reply_style_hint,
        )

    # ── 刷屏检测:同一用户**连续**(无他人插话) >= _BURST_MIN_MSGS 条且在 _BURST_WINDOW_SEC 内 ──
    # 用"连续"而不是"频次"判定:8 个人轮流发言每人 1 条不是刷屏;一个人独自连发 4 条没人接话才是。
    burst_user_id = ""
    burst_count = 0
    if user_msgs:
        # 从最新一条往前数,统计末尾连续同一用户的消息数
        last_msg = user_msgs[-1]
        candidate_user = last_msg.user_id
        for m in reversed(user_msgs):
            if m.user_id != candidate_user:
                break
            if m.created_at < now - _BURST_WINDOW_SEC:
                break
            burst_count += 1
        if burst_count >= _BURST_MIN_MSGS:
            burst_user_id = candidate_user
    if burst_user_id:
        phase = "burst"
        # display_name 取最新那条
        burst_user = next(
            (m.display_name for m in reversed(user_msgs) if m.user_id == burst_user_id and m.display_name),
            burst_user_id,
        )
        observations.append(
            f"{burst_user} 刚刚连发了 {burst_count} 条没人接话,可能在抖话痨或玩梗,"
            f"你可以揶揄一下/接梗,也可以短回应,别陪着刷屏"
        )
        # 刷屏:1 段 ≤ 20 字,精准戳一下,不陪着刷碎
        reply_style_hint = "回复 1 段, ≤20 字;精准吐槽/接梗,不要拆多条陪着刷屏"

    # ── 复读检测:90 秒内 >= 3 条相似文本 ──
    cutoff_echo = now - _ECHO_WINDOW_SEC
    echo_window_msgs = [m for m in user_msgs if m.created_at >= cutoff_echo and m.text]
    norm_count: dict[str, int] = {}
    norm_to_orig: dict[str, str] = {}
    for m in echo_window_msgs:
        norm = _normalize_for_echo(m.text)
        if not norm or len(norm) > 40:  # 过长的不算复读梗
            continue
        norm_count[norm] = norm_count.get(norm, 0) + 1
        norm_to_orig.setdefault(norm, m.text)
    if phase == "normal" and norm_count:
        echo_cand = max(norm_count.items(), key=lambda kv: kv[1])
        if echo_cand[1] >= _ECHO_MIN_REPEATS:
            phase = "echo"
            echo_text = norm_to_orig[echo_cand[0]][:30]
            observations.append(
                f"群里在复读『{echo_text}』({echo_cand[1]} 次),"
                "你可以跟一下但加猫娘风味,别原样照抄"
            )
            # 复读:1 段 ≤ 25 字, 接梗但要带猫娘味, 不要复读原文
            reply_style_hint = "回复 1 段, ≤25 字;在复读基础上加猫娘味/小动作,不要原样照抄"

    # ── 热闹检测 ──(冷场/刷屏/复读 优先,只在 phase 还是 normal 时考虑)
    if phase == "normal" and msg_count_2min >= _BUSY_MIN_MSGS and participants_2min >= _BUSY_MIN_PARTICIPANTS:
        phase = "busy"
        observations.append(
            f"最近 2 分钟 {participants_2min} 个人发了 {msg_count_2min} 条,群里很热闹,"
            "回复尽量短句快接,别长篇大论"
        )
        # 热闹:1-2 段, ≤30 字/段, 跟上节奏
        reply_style_hint = "回复段数 1-2,每段 ≤30 字;短句快接跟上节奏,别长篇大论"

    # 默认 normal 不强行加 observation(避免占 prompt 空间)
    return PulseResult(
        phase=phase,
        msg_count_2min=msg_count_2min,
        participants_2min=participants_2min,
        seconds_since_last=seconds_since_last,
        observations=observations,
        burst_user=burst_user,
        echo_text=echo_text,
        reply_style_hint=reply_style_hint,
    )


# ── 主回复 system prompt 生成 ─────────────────────────────────────────

def build_pulse_context(messages: Iterable[_MsgLike], *, now: float) -> str:
    """主回复链路用:给定 deque 返回一段 system prompt 文本。

    phase=normal 不打扰(返回空),其它 phase 输出一行简短上下文 + observations
    + 该 phase 的回复风格 cap(段数/字数)。
    """
    pulse = analyze_pulse(messages, now=now)
    if pulse.phase == "normal":
        return ""
    if not pulse.observations:
        return ""
    label_cn = {
        "cold": "冷场",
        "burst": "刷屏",
        "echo": "复读",
        "busy": "热闹",
    }.get(pulse.phase, pulse.phase)
    body = "; ".join(pulse.observations)
    main = f"群节奏感知[{label_cn}]: {body}。"
    if pulse.reply_style_hint:
        main += f" 回复风格建议: {pulse.reply_style_hint}。"
    return main
