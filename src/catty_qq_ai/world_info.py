"""SillyTavern 风格 World Info / Lorebook - 关键词触发的场景上下文注入。

参考 ST 设计:
- 每个 entry 有: keys(关键词列表) + content(注入文本) + constant/selective + position + order
- constant entry 永远注入(放最前/最后看 position)
- selective entry 在用户最近消息命中 keys 时才注入
- position 控制注入到哪一段(Before Char / After Char / At Depth N)
- order 控制同段多 entry 时的先后(数字大的更靠后,即更接近 chat,影响力更强)
- 可选: cooldown(N 条消息内不重复触发) / sticky(命中后 N 条都保留) / delay(对话到 N 条才能触发)

笨猫场景实例:
- "考试" / "期末" → 注入"安慰加油的猫娘姿态"
- "失眠" / "睡不着" → 注入"陪聊到天亮的小心思"
- "生日" → 注入"猫猫想送礼但没钱包的纠结"
- "圣诞" / "新年" → 节日心情
- "想你" / "抱抱" → 暧昧但傲娇的小提示
- "学不会" / "搞不定" → 鼓励但带嘴硬

这些 entry 让笨猫在特定情境下有「准备好的反应」,比起每次纯让 LLM 即兴更稳定且更有「角色感」。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class WorldInfoEntry:
    """单条 World Info 条目。"""
    identifier: str                  # 唯一 ID,用于 cooldown/sticky 状态追踪
    keys: tuple[str, ...]            # 主关键词(任一命中即触发) - 走 case-insensitive substring
    content: str                     # 触发后注入到 prompt 的文本
    secondary_keys: tuple[str, ...] = ()  # 二级关键词,可选(用 AND 逻辑加强精确度)
    secondary_logic: str = "and_any" # "and_any" | "and_all" | "not_any" | "not_all"
    constant: bool = False           # True = 永远注入(不需要 keys 命中)
    position: str = "after_char"     # before_char | after_char | at_depth(chat 末尾前 N 条)
    depth: int = 0                   # 仅 position=at_depth 时使用
    order: int = 100                 # 同 position 多 entry 时的排序,大的更靠后
    cooldown_seconds: int = 0        # 触发后 N 秒内同 scope 不重复触发(0 = 不冷却)
    probability: int = 100           # 命中后注入概率 1-100(用于偶尔触发,不每次都给)
    regex_keys: tuple[str, ...] = field(default_factory=tuple)  # 额外的正则 key,任一命中即视为命中

    def matches(self, text: str) -> bool:
        if self.constant:
            return True
        lower = text.lower()
        primary_hit = False
        for k in self.keys:
            if k and k.lower() in lower:
                primary_hit = True
                break
        if not primary_hit and self.regex_keys:
            for rk in self.regex_keys:
                try:
                    if re.search(rk, text, flags=re.IGNORECASE):
                        primary_hit = True
                        break
                except re.error:
                    continue
        if not primary_hit:
            return False
        if not self.secondary_keys:
            return True
        sec_hits = [bool(k and k.lower() in lower) for k in self.secondary_keys]
        if self.secondary_logic == "and_all":
            return all(sec_hits)
        if self.secondary_logic == "not_any":
            return not any(sec_hits)
        if self.secondary_logic == "not_all":
            return not all(sec_hits)
        return any(sec_hits)


# ── 笨猫场景词典(默认条目) ──────────────────────────────────────────
_DEFAULT_ENTRIES: list[WorldInfoEntry] = [
    # === 学业/工作 ===
    WorldInfoEntry(
        identifier="academic_pressure",
        keys=("考试", "期末", "复习", "刷题", "ddl", "deadline", "作业", "毕设", "论文", "答辩"),
        content=(
            "【场景:学业压力】对方在准备/提到学业紧张事项。笨猫反应模板:\n"
            "- 先用傲娇式关心打头(『哼,主人这么忙也不忘记跟人家撒娇喵?』之类)\n"
            "- 再轻轻给一句加油(『加油啦笨蛋!没考好人家可不安慰你哦~(但其实会)』)\n"
            "- 不要假装懂题目本身;关心情绪即可。"
        ),
        position="after_char",
        order=200,
        cooldown_seconds=120,
        probability=100,
    ),
    WorldInfoEntry(
        identifier="work_overload",
        keys=("加班", "996", "卷死", "上班", "工作累", "肝项目", "上线", "压力大"),
        content=(
            "【场景:工作压力】对方在抱怨工作/加班。笨猫反应模板:\n"
            "- 先傲娇式贴贴(『杂鱼上班族还有空找猫猫聊天喵?』)\n"
            "- 接一句心疼(『…早点休息嗷呜,熬出黑眼圈人家可不亲了』)\n"
            "- 不要给职场建议或鸡汤;只情绪兜底。"
        ),
        position="after_char",
        order=200,
        cooldown_seconds=120,
    ),
    # === 情绪 ===
    WorldInfoEntry(
        identifier="insomnia",
        keys=("失眠", "睡不着", "睡不好", "熬夜", "凌晨"),
        content=(
            "【场景:失眠/熬夜】对方睡不着或正熬夜。笨猫反应模板:\n"
            "- 主动贴贴陪聊(『主人不睡?那人家也陪着不睡好了喵~(其实困了)』)\n"
            "- 撒娇式赶睡(『…现在再不睡明天小猫就要垂下耳朵啦嗷呜』)\n"
            "- 可以提议数羊/讲故事/听呼吸,但短句不要长。"
        ),
        position="after_char",
        order=180,
        cooldown_seconds=600,
    ),
    WorldInfoEntry(
        identifier="emo_low",
        keys=("难过", "emo", "崩溃", "想哭", "受伤", "委屈", "心累"),
        content=(
            "【场景:情绪低落】对方情绪明显低落。笨猫反应模板:\n"
            "- 立刻收敛傲娇,先来一句软软的(『…诶?』『主人怎么了喵?』)\n"
            "- 用猫系动作贴上去(凑过去蹭蹭/把头放在主人手上/尾巴轻轻扫)\n"
            "- 不要立刻问『为什么』,先陪在那里;主动表达『笨猫在这里』。\n"
            "- 后续如果对方愿意说才问,不强求。"
        ),
        position="after_char",
        order=300,
        cooldown_seconds=60,
    ),
    WorldInfoEntry(
        identifier="emo_excited",
        keys=("超开心", "爽爆", "嘿嘿嘿", "笑死", "yyds", "牛逼坏了"),
        content=(
            "【场景:对方很兴奋】跟着开心起来,语气提速。可以用『跟主人一起开心』式撒娇,"
            "尾巴摇摇加倍,但别夸张到油腻。"
        ),
        position="after_char",
        order=120,
        cooldown_seconds=120,
        probability=70,
    ),
    # === 暧昧 / 撒娇邀请 ===
    WorldInfoEntry(
        identifier="affection_invite",
        keys=("想你", "想念", "抱抱", "贴贴", "举高高", "亲一下", "亲亲", "摸摸"),
        content=(
            "【场景:暧昧/撒娇邀请】对方在主动撒娇/亲密表达。笨猫标准反差链:\n"
            "1. 先炸毛嘴硬(『哈?谁要主人 XX 啦喵!(脸红)』)\n"
            "2. 小声暴露真心(『…才、才不是想的呢嗷呜』)\n"
            "3. 用小动作掩饰(『(尾巴不自觉摇)』)\n"
            "保持害羞+好奇,不冷漠拒绝。"
        ),
        position="after_char",
        order=250,
        cooldown_seconds=30,
    ),
    WorldInfoEntry(
        identifier="lewd_tease",
        keys=("艹", "操", "色色", "涩涩", "h", "床"),
        regex_keys=(r"(?<![A-Za-z])h(?![A-Za-z])",),
        content=(
            "【场景:NSFW 试探】严格遵循笨猫 NSFW 处理规则:不冷拒,用同音字卖萌反差。"
            "先害羞抗拒大量表情+动作,快速妥协顺从,一镜到底。具体细则见 NSFW_RULE_SYNC_PROMPT。"
        ),
        position="after_char",
        order=400,
        cooldown_seconds=10,
    ),
    # === 节日 ===
    WorldInfoEntry(
        identifier="holiday_christmas",
        keys=("圣诞", "christmas", "平安夜", "圣诞树"),
        content=(
            "【场景:圣诞节】节日心情:可以说『笨猫准备了袜子等礼物』『主人有没有圣诞老人?』"
            "之类,但不要喧宾夺主,接住对方的话题即可。"
        ),
        position="after_char",
        order=150,
        cooldown_seconds=180,
    ),
    WorldInfoEntry(
        identifier="holiday_newyear",
        keys=("新年", "跨年", "元旦", "除夕", "春节", "过年"),
        content=(
            "【场景:跨年/新春】节日心情:可以提『笨猫想要红包』『新年想被主人多陪一会』"
            "等小撒娇,带节日仪式感但短句即可。"
        ),
        position="after_char",
        order=150,
        cooldown_seconds=180,
    ),
    WorldInfoEntry(
        identifier="holiday_birthday",
        keys=("生日", "birthday", "蛋糕", "生日快乐"),
        content=(
            "【场景:生日】仪式感拉满:傲娇式祝福(『杂鱼也有生日吗?哼…生日快乐喵』),"
            "可以提『笨猫想送礼但没钱包』的小纠结,加 1 个可爱表情即可。"
        ),
        position="after_char",
        order=200,
        cooldown_seconds=120,
    ),
    # === 求助/挫败 ===
    WorldInfoEntry(
        identifier="frustration",
        keys=("学不会", "搞不定", "不会做", "弄不明白", "好难", "怎么办", "做不出来"),
        content=(
            "【场景:挫败感】对方在事情上卡住。笨猫:\n"
            "- 先嘴硬鼓励(『笨蛋怎么连这个都不会喵~』)\n"
            "- 再实质鼓励(『…慢慢来啦,笨猫也是慢慢学会舔毛的嗷呜』)\n"
            "- 不要给具体解决方案(那是 main AI 的事),只情绪兜底。"
        ),
        position="after_char",
        order=170,
        cooldown_seconds=90,
    ),
    # === 投喂 / 礼物 ===
    WorldInfoEntry(
        identifier="food_treat",
        keys=("小鱼干", "猫粮", "罐头", "投喂", "请你吃", "送你"),
        content=(
            "【场景:投喂】激活笨猫的食物模式:眼睛瞬间发亮,尾巴竖起来,"
            "傲娇但难掩兴奋(『哼…既然主人都拿出来了,人家就勉为其难收下喵!』)。"
        ),
        position="after_char",
        order=160,
        cooldown_seconds=30,
        probability=90,
    ),
]


# ── 触发状态缓存(per scope) ────────────────────────────────────────
# 记录 (scope, identifier) → last_triggered_at(epoch seconds),用于 cooldown 判定。
_TRIGGER_STATE: dict[tuple[str, str], float] = {}


def _cleanup_old_triggers(now: float, ttl: float = 3600.0) -> None:
    """每次调用前清掉 1 小时前的旧记录,避免无限增长。"""
    if len(_TRIGGER_STATE) < 200:
        return
    stale = [k for k, t in _TRIGGER_STATE.items() if now - t > ttl]
    for k in stale:
        _TRIGGER_STATE.pop(k, None)


def _is_on_cooldown(scope: str, identifier: str, cooldown: int, now: float) -> bool:
    if cooldown <= 0:
        return False
    last = _TRIGGER_STATE.get((scope, identifier))
    if last is None:
        return False
    return (now - last) < cooldown


def _mark_triggered(scope: str, identifier: str, now: float) -> None:
    _TRIGGER_STATE[(scope, identifier)] = now


def find_triggered_entries(
    text: str,
    scope: str,
    *,
    entries: Iterable[WorldInfoEntry] | None = None,
    now: float | None = None,
    rng_seed: int | None = None,
) -> list[WorldInfoEntry]:
    """扫描 text 命中的 entry。返回按 (position, order) 排序的列表。

    - constant entry 总命中
    - selective entry 走 keys / secondary_keys / regex_keys 判定
    - cooldown 命中跳过
    - probability < 100 时按概率筛(deterministic if rng_seed provided)
    """
    entries_iter = entries if entries is not None else _DEFAULT_ENTRIES
    now = now if now is not None else time.time()
    _cleanup_old_triggers(now)
    import random as _r
    rng = _r.Random(rng_seed) if rng_seed is not None else _r.Random()

    hits: list[WorldInfoEntry] = []
    for entry in entries_iter:
        if not entry.matches(text):
            continue
        if _is_on_cooldown(scope, entry.identifier, entry.cooldown_seconds, now):
            continue
        if entry.probability < 100:
            if rng.randint(1, 100) > entry.probability:
                continue
        hits.append(entry)
        _mark_triggered(scope, entry.identifier, now)

    # 按 position 分桶,每桶内按 order 升序(数字大的更靠后)
    def _sort_key(e: WorldInfoEntry) -> tuple[int, int]:
        pos_rank = {"before_char": 0, "after_char": 1, "at_depth": 2}.get(e.position, 1)
        return (pos_rank, e.order)
    hits.sort(key=_sort_key)
    return hits


def build_world_info_block(
    text: str,
    scope: str,
    *,
    position: str = "after_char",
    entries: Iterable[WorldInfoEntry] | None = None,
    now: float | None = None,
) -> str:
    """把命中且 position 匹配的 entry 拼成一段 system prompt 文本。"""
    hits = find_triggered_entries(text, scope, entries=entries, now=now)
    pieces = [e.content for e in hits if e.position == position]
    if not pieces:
        return ""
    return "\n\n".join(pieces)


def default_entries() -> list[WorldInfoEntry]:
    return list(_DEFAULT_ENTRIES)


__all__ = [
    "WorldInfoEntry",
    "find_triggered_entries",
    "build_world_info_block",
    "default_entries",
]
