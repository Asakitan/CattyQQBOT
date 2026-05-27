"""笨猫『现在的场景』author_note - 把 daily_life + session_spice + mood 凝缩到 depth=2。

为什么需要这一层 (跟现有 daily_life / session_spice 的区别):
- daily_life / session_spice 在 system 块顶部 (order 200/208), 长 prompt 里被 LLM 习以为常忽略
- catty_scene_now 把这些状态信号凝缩成 ≤120 字的『当下一句剧情』, 注入 depth=2 贴近 user
- LLM recency bias 让这一层比 system 块更难被忽略, 强迫 AI 回复时带出现场感

ST 风类比:
- daily_life = scenario (background)
- session_spice = persona micro-traits
- catty_scene_now = author_note (foreground / "here-and-now")

ST 高级玩法里 author_note 不是简单提醒, 而是用来注入"剧情焦点":
我们这里就是把今天的 activity / mood / spice / reunion 信号串成一句"此刻笨猫正在...",
让 AI 输出有具体场景感而不是抽象人格。

pure function — 接受 daily_life_state + spice + reunion 输入, 不读 store。
"""
from __future__ import annotations

from .author_note import AuthorNote


# 把 daily_life mood_label 翻译成短氛围描述 — 主要为了缩字符
# (daily_life 的 mood_tone 字段已经够短, 这里只把 mood_label 转成"色调"用)
_MOOD_LABEL_SHORT: dict[str, str] = {
    "懒洋洋": "懒",
    "元气满满": "元气",
    "蔫蔫的": "蔫",
    "暖洋洋": "暖",
    "闷闷的": "闷",
    "调皮": "皮",
    "黏人": "黏",
    "走神": "飘",
    "好奇": "好奇",
    "软软的": "软",
    "傲娇满分": "傲娇 max",
    "撒娇满分": "撒娇 max",
    "占有欲微涨": "占有欲 ↑",
    "想被摸头": "想被摸头",
    "雷达灵敏": "雷达 ↑",
    "主人专属乖巧": "乖巧 mode",
}


def _short_mood(label: str | None) -> str:
    if not label:
        return ""
    return _MOOD_LABEL_SHORT.get(label, label)


def _trim_clause(s: str | None, *, max_chars: int = 50) -> str:
    """把状态字段裁到 ≤ max_chars, 太长截断 + 省略号。"""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


def build_scene_now_note(
    *,
    daily_state: dict[str, str] | None = None,
    spice: tuple[str, str, str] | None = None,
    reunion_level: str = "warm",
    is_owner: bool = False,
    user_display: str = "对方",
) -> AuthorNote:
    """合成『现在的笨猫』author_note (depth=2 贴近 user 最后消息).

    daily_state: daily_life.build_daily_life_state() 返回的 dict (含 activity/mood_label/mood_tone/wish)
    spice: session_spice.pick_session_spice() 返回的 (mood_micro, body_quirk, speech_quirk)
    reunion_level: catty_reunion.classify_idle_level() 返回的 warm/brief/days/long
    is_owner: 是否真主人 (影响称呼 + 撒娇浓度提示)
    user_display: 对方称呼字面 (主人/笨蛋主人/对方 nickname)

    所有字段空 → 返回空 note (inject 自动跳过)。
    """
    if daily_state is None and spice is None:
        return AuthorNote(content="", depth=2)

    activity = _trim_clause(daily_state.get("activity") if daily_state else "", max_chars=42)
    mood_tone = _trim_clause(daily_state.get("mood_tone") if daily_state else "", max_chars=30)
    mood_label = _short_mood(daily_state.get("mood_label") if daily_state else "")
    wish = _trim_clause(daily_state.get("wish") if daily_state else "", max_chars=36)

    spice_mood, spice_body, spice_speech = (spice or ("", "", ""))
    spice_mood = _trim_clause(spice_mood, max_chars=40)
    spice_body = _trim_clause(spice_body, max_chars=40)

    addr_hint = "对方" if not is_owner else "主人"
    # owner 用 user_display (主人/笨蛋主人), 非 owner 用对方昵称
    addr = user_display if user_display else addr_hint

    lines: list[str] = ["【现在的笨猫·此刻场景】贴近你这一句, 不要复述给对方听:"]
    # 当下场景片段 (activity + mood)
    if activity:
        if mood_label:
            lines.append(f"- 此刻在: {activity} (色调: {mood_label})")
        else:
            lines.append(f"- 此刻在: {activity}")
    elif mood_label:
        lines.append(f"- 此刻色调: {mood_label}")

    if mood_tone:
        lines.append(f"- 情绪底色: {mood_tone}")

    # spice 选一条最有现场感的注入 (body_quirk > mood_micro)
    if spice_body:
        body_hint = spice_body.replace("今天小动作偏好: ", "").replace("今天小动作偏好:", "")
        lines.append(f"- 小动作偏好: {body_hint}")
    elif spice_mood:
        mood_hint = spice_mood.replace("今天微情绪: ", "").replace("今天微情绪:", "")
        lines.append(f"- 微情绪: {mood_hint}")

    if wish:
        lines.append(f"- 心里偷偷想着: {wish}")

    # 久别重逢: 在 days/long 档加一行提示
    if reunion_level in ("days", "long"):
        if is_owner:
            lines.append(
                f"- ⚠️ 距上次和{addr}说话很久了 — 开场带『终于来了喵?』反差感, 别无缝接话"
            )
        else:
            lines.append(
                f"- ⚠️ 这位({addr})很久没出现了 — 礼貌带点惊喜『好久不见』, 不要假装没断过"
            )

    # 主人 vs 非主人的称呼 + 撒娇浓度提示
    if is_owner:
        lines.append(
            f"- 你正在和**主人**({addr})说话: 称呼专属(主人/笨蛋主人/杂鱼主人), "
            "撒娇/嘴硬反差全开, 结尾留贴贴钩子"
        )
    else:
        lines.append(
            f"- 你正在和**{addr}**说话: 不能叫主人, 用对方昵称或『你』, "
            "保持笨猫人格但根据好感度档调撒娇浓度"
        )

    lines.append(
        "↑ 把上面的『此刻』隐隐藏在第一句的语气/小动作里 (例: 慵懒地翻了个身 / 突然耳朵竖一下 / 蹭蹭尾巴), "
        "不要明面上『今天我在X』式自报家门。"
    )
    return AuthorNote(content="\n".join(lines), depth=2)


__all__ = [
    "build_scene_now_note",
]
