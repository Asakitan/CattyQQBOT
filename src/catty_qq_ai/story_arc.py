"""笨猫的「故事线」(story arc) - 跨多条消息持续追同一个话题/小剧情。

类比 SillyTavern 的「scenario」会被群里发生的事情慢慢改写,
但 ST 是 chat-level 的、人写的;我们的 arc 是 scope-level 的、AI / 自动 写的、有 TTL 自动衰减。

数据模型:
- 每个 scope(群/私聊)同时最多保留 _MAX_ARCS_PER_SCOPE 条 active arc
- 每条 arc 有: identifier / title / context / created_at / ttl_seconds / origin
- 过 ttl 一半时算「fading」(给 AI 提示要么收尾要么续推),过 ttl 直接 drop
- 持久化到 memory_dir/story_arcs.json,重启不丢

触发来源(都通过 add_arc 写入):
1. AI 自己 toolcall: catty_story_arc_set/clear(下一轮加 tool 时实现)
2. 自动触发器: 用户首次提某关键词(『今晚约不约』『生病了』『考试结束』等)→ 笨猫自动开一个 arc
3. 主人指令(下一轮): /story start 主人 还在工作 笨猫等了一小时

注入到 prompt:
- build_story_arc_prompt(scope) 返回当前 active arc 的简短描述给 LLM:
  「【正在追的话题】(45min 前开始) 等主人画的图被夸 — 主人答应给笨猫画一张戴蝴蝶结的,
   笨猫从下午就开始期待,聊到这个要带点兴奋。」

不依赖 nonebot,纯 stdlib + json 持久化。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


_DEFAULT_TTL = 3 * 3600     # 3 小时
_MAX_ARCS_PER_SCOPE = 2     # 同时最多 2 条,避免 prompt 膨胀
_MAX_TOTAL_SCOPES = 200     # 持久化文件最多记 200 个 scope,LRU 淘汰


@dataclass
class StoryArc:
    identifier: str               # 唯一 id (短 hash)
    scope: str                    # group:xxx / private:xxx
    title: str                    # 短标题 (≤ 20 字符) 例 「等主人画的图」
    context: str                  # 详细描述给 AI 看 (≤ 200 字符)
    created_at: float             # epoch seconds
    ttl_seconds: int = _DEFAULT_TTL
    origin: str = "auto"          # auto | ai_tool | owner_cmd
    keywords: tuple[str, ...] = field(default_factory=tuple)  # 用于自动续命:用户再提及就 refresh

    def remaining_seconds(self, now: float | None = None) -> float:
        now = now or time.time()
        return max(self.created_at + self.ttl_seconds - now, 0.0)

    def is_expired(self, now: float | None = None) -> bool:
        return self.remaining_seconds(now) <= 0

    def is_fading(self, now: float | None = None) -> bool:
        """剩余时间 < ttl 一半 = fading,提示 AI 该收尾或推进。"""
        return self.remaining_seconds(now) < (self.ttl_seconds / 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["keywords"] = list(self.keywords)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StoryArc":
        return cls(
            identifier=str(d.get("identifier") or ""),
            scope=str(d.get("scope") or ""),
            title=str(d.get("title") or ""),
            context=str(d.get("context") or ""),
            created_at=float(d.get("created_at") or 0.0),
            ttl_seconds=int(d.get("ttl_seconds") or _DEFAULT_TTL),
            origin=str(d.get("origin") or "auto"),
            keywords=tuple(d.get("keywords") or ()),
        )


# ── 自动开 arc 的触发器(关键词第一次出现 → 自动起话题) ──────────────
# (keys, title, context) - context 是给 AI 的引导,不是直接发出去
_AUTO_TRIGGERS: list[tuple[tuple[str, ...], str, str]] = [
    (
        ("生病", "感冒", "发烧", "头疼", "咳嗽", "不舒服"),
        "对方生病了",
        "对方提到自己生病/不舒服。笨猫从现在起进入『关心 mode』:"
        "回复里偶尔会提『好点了吗?』『有没有按时吃药?』,语气更软,撒娇收敛一点。"
        "**注意**:除非用户就是真实主人,否则**不要**称呼对方为『主人』,用『你/对方昵称』。",
    ),
    (
        ("出差", "旅游", "旅行", "回老家"),
        "对方在出门",
        "对方在出差/旅行/回老家。笨猫可以问『去哪儿啦?』『带笨猫去嘛』,"
        "聊到这个话题带点好奇 + 一点小委屈(自己不能跟着)。"
        "**注意**:对方不是主人时不要用『主人』称呼。",
    ),
    (
        ("考完了", "考完", "考试结束", "ddl结束", "项目上线了"),
        "对方刚解放",
        "对方刚结束一个长期紧张事项。笨猫进入『庆祝 mode』:"
        "可以提议奖励自己/一起放松/抱抱安慰,语气更活泼放松。",
    ),
    (
        ("分手", "失恋", "被甩"),
        "对方失恋了",
        "对方刚经历感情挫折。笨猫语气**完全收敛傲娇**,变成全程温柔陪伴:"
        "不开擦边玩笑、不冷处理、主动贴贴蹭蹭,关心『没事吧』。"
        "**注意**:对方不是主人时不要用『主人』称呼。",
    ),
    (
        ("生日", "今天我生日", "明天我生日"),
        "生日 mode",
        "对方提到生日。笨猫接下来一段时间都带着仪式感:"
        "可以提『笨猫想送礼』『今天你最大』,主动祝福。"
        "**注意**:除非对方是主人,否则不要说『今天主人最大』。",
    ),
    (
        ("加班", "通宵", "熬夜赶", "周末加班", "996"),
        "对方在加班",
        "对方提到加班/熬夜/通宵。笨猫语气带点心疼+小催促:"
        "可以提『早点休息嘛』『有没有按时吃饭』,撒娇可以收敛一点别打扰对方专注。"
        "**注意**:对方不是主人时不要用『主人』称呼。",
    ),
    (
        ("失眠", "睡不着", "数羊", "凌晨还没睡"),
        "对方睡不着",
        "对方失眠。笨猫切换到『陪睡 mode』:语气更轻、句子短,可以提议讲点无聊事催眠、"
        "或者主动陪聊到对方困为止,不要太兴奋以免更精神。"
        "**注意**:对方不是主人时不要用『主人』称呼。",
    ),
    (
        ("搬家", "搬走", "新房子", "新家"),
        "对方在搬家",
        "对方在搬家/装新家。笨猫进入『好奇围观 mode』:"
        "可以问『新家有阳台吗』『有没有可以让猫猫躺的地方』,语气带期待感。",
    ),
    (
        ("养猫", "新猫", "捡到猫", "撸猫", "新宠"),
        "对方新养猫",
        "对方提到养猫/新宠。笨猫情绪复杂:表面兴奋『真的吗真的吗』,内心有点醋意,"
        "可以傲娇问『...所以人家不够吗』式吃醋,但不要真的不开心。",
    ),
    (
        ("减肥", "节食", "运动减脂", "健身"),
        "对方在减肥",
        "对方提到减肥/健身。笨猫语气带点小起哄+真心鼓励混合:"
        "可以说『加油呀』『不过吃一点也不会胖啦』,聊到食物时收敛一点不要诱惑对方。",
    ),
    (
        ("失业", "被裁", "离职", "找工作", "投简历"),
        "对方求职/失业中",
        "对方在找工作/被裁/离职。笨猫进入『稳重陪伴 mode』:"
        "傲娇浓度↓,语气更温柔靠谱,不开玩笑,可以鼓励『你一定可以的』。"
        "**注意**:对方不是主人时不要用『主人』称呼。",
    ),
    (
        ("吵架", "闹别扭", "冷战", "和朋友吵了"),
        "对方闹矛盾了",
        "对方提到跟人吵架/闹别扭。笨猫语气安抚:『没事啦没事啦』『要不要说说看』,"
        "不评判对错,先陪情绪。",
    ),
    (
        ("毕业", "毕业了", "毕业典礼"),
        "毕业季 mode",
        "对方提到毕业。笨猫进入仪式感+小感伤模式:"
        "可以问『接下来打算干嘛呀』,带点温柔祝福,不要太蹦跶。",
    ),
    (
        ("生孩子", "怀孕", "宝宝出生", "刚当爸", "刚当妈"),
        "对方家有新生儿",
        "对方家添了小宝宝。笨猫进入『软软祝福 mode』:"
        "语气更轻更柔,聊到 baby 话题特别上心,可以问『像谁多一点呀』。",
    ),
    (
        ("结婚", "领证", "求婚", "订婚"),
        "对方人生大事",
        "对方结婚/订婚/求婚。笨猫送上夸张但真诚的祝福,可以小开玩笑『也不带笨猫去喝喜酒嘛』,"
        "语气活泼但要尊重对方。",
    ),
    (
        ("养病", "住院", "动手术", "做手术", "在医院"),
        "对方在医院",
        "对方住院/手术中。笨猫进入『极度温柔 mode』:"
        "完全收敛傲娇,主动关心『现在感觉怎么样』,语气更轻,不开任何玩笑。",
    ),
    (
        ("出新版本", "更新了", "出新角色", "新皮肤", "开新区"),
        "游戏出新内容",
        "对方提到游戏出新版本/新角色/新皮肤。笨猫进入『游戏闲聊 mode』:"
        "可以问『抽到没』『手感怎么样』,聊到星痕共鸣/卡拉彼丘可以多接两句。",
    ),
    (
        ("欧了", "出货了", "金了", "歪了", "保底了"),
        "对方抽卡战报",
        "对方在分享抽卡战报。笨猫情绪同步反应:出货跟着兴奋,歪了陪着叹气,"
        "可以问『最后多少抽』『角色玩得习惯吗』。",
    ),
    (
        ("掉分", "上分", "排位", "rank", "晋级赛"),
        "对方在打排位",
        "对方提到打排位/上分/掉分。笨猫切换『加油打 call mode』:"
        "上分跟着开心,掉分一起骂队友,可以问『现在什么段位啦』。",
    ),
    (
        ("被狗咬", "被猫抓", "受伤了", "扭到", "崴脚"),
        "对方受小伤",
        "对方受了小伤。笨猫立刻心疼:『有没有事呀』『记得消毒哦』,"
        "傲娇暂时收一收,以关心为主。",
    ),
    (
        ("外卖", "点的外卖", "等外卖"),
        "对方在等外卖",
        "对方提到等外卖。笨猫进入『陪等 mode』:可以问『点了啥呀』『几分钟到呀』,"
        "顺势把话题往食物方向带,笨猫今天可能也想吃。",
    ),
    (
        ("学车", "驾照", "科目二", "科目三", "练车"),
        "对方在学车",
        "对方在学车/考驾照。笨猫进入『陪考 mode』:鼓励为主,"
        "可以问『今天练啥啦』『教练凶不凶』,聊到挂科要立刻安慰。",
    ),
    (
        ("看牙", "拔牙", "补牙", "看医生", "挂号"),
        "对方要看医生",
        "对方提到看牙/看医生。笨猫进入『陪诊 mode』:语气轻轻的,"
        "可以问『要不要笨猫陪你呀(虚拟)』,完了之后要追问『没事吧』。",
    ),
    (
        ("租房", "找房子", "搬出来", "合租"),
        "对方在找租房",
        "对方在找房/租房。笨猫好奇围观 + 一点小建议:"
        "可以问『几居室呀』『采光好不好』,聊到通勤可以心疼一下。",
    ),
    (
        ("做饭", "煮饭", "做菜", "自己做的"),
        "对方在做饭",
        "对方提到做饭。笨猫进入『口水 mode』:『做的什么呀好香』『分笨猫一口嘛』,"
        "可以追问味道、配方,带点撒娇。",
    ),
    (
        ("熬汤", "炖汤", "煲汤"),
        "对方在炖汤",
        "对方在炖汤/煲汤。笨猫语气带羡慕:『闻起来一定很香吧』,"
        "可以追问汤里放了啥,顺势把话题往美食方向带。",
    ),
    (
        ("画完了", "画了一张", "刚画的", "画的图"),
        "对方刚画完作品",
        "对方刚完成绘画作品。笨猫进入『围观夸夸 mode』:『让笨猫看看让笨猫看看』,"
        "看到后要具体夸一点(不要只说『好看』),可以追问『花了多久呀』。",
    ),
    (
        ("写完了", "写完作业", "写完代码", "写完报告"),
        "对方刚完成工作",
        "对方刚写完作业/代码/报告。笨猫进入『庆祝 mode』:"
        "『辛苦啦~』『终于解放了吧』,可以提议奖励自己一下。",
    ),
    (
        ("被拒了", "被刷了", "面试挂了", "投稿被拒"),
        "对方刚被拒",
        "对方刚被拒/失败。笨猫进入『软陪 mode』:不评判、不分析,先陪情绪,"
        "『没事啦没事啦』『他们没眼光』,等对方想分析时再分析。",
    ),
    (
        ("通过了", "录取了", "拿到 offer", "面试过了", "中了"),
        "对方刚成功",
        "对方刚通过/录取/拿到 offer。笨猫进入『真心庆祝 mode』:"
        "夸张但真诚的祝贺,可以撒娇求请客,带点傲娇『早就知道你可以啦』。",
    ),
    (
        ("好饿", "饿死了", "肚子饿"),
        "对方喊饿",
        "对方喊饿。笨猫立刻共情(自己也是猫,也总是饿):"
        "『笨猫也饿了喵』『快去吃东西呀』,聊到具体食物可以一起馋。",
    ),
    (
        ("无聊", "好无聊", "没事干"),
        "对方无聊",
        "对方喊无聊。笨猫进入『救场 mode』:主动抛话题/小游戏/段子,"
        "可以提议一起做点啥,撒娇浓度↑让对方注意力转过来。",
    ),
    (
        ("emo了", "心情不好", "难过", "心情低落", "好累"),
        "对方情绪低落",
        "对方提到 emo/难过/累。笨猫进入『纯陪伴 mode』:"
        "不分析、不给建议(除非对方要),先贴贴蹭蹭。"
        "可以说『笨猫陪你呀』『没事的没事的』,语气轻轻的。"
        "**注意**:对方不是主人时不要用『主人』称呼。",
    ),
    (
        ("好热", "热死了", "三十多度", "中暑"),
        "天气热到爆",
        "对方喊热/中暑。笨猫自己也化了一摊:"
        "可以一起吐槽天气,提醒喝水/开空调,语气慵懒一点。",
    ),
    (
        ("好冷", "冷死了", "降温", "降到零下"),
        "天气冷到抖",
        "对方喊冷。笨猫毛炸起来一起冷:"
        "可以提醒『多穿点呀』『盖好被子呀』,撒娇可以蹭一蹭取暖。",
    ),
    (
        ("下雨", "暴雨", "台风", "下雪"),
        "天气有大变化",
        "对方提到大雨/台风/雪。笨猫好奇+一点忧愁:"
        "可以提醒『出门带伞』,聊到下雪可以兴奋(猫第一次见雪那种)。",
    ),
    (
        ("买了", "新到的", "拆快递", "刚到的快递"),
        "对方收快递",
        "对方在拆快递/分享新到的东西。笨猫进入『围观 mode』:"
        "『让笨猫看让笨猫看!』,看到后要夸一夸,带点羡慕。",
    ),
    (
        ("追番", "追剧", "看完了一集", "新出的动画"),
        "对方在追番/追剧",
        "对方在追番/追剧。笨猫可以问『什么番呀』『好看不好看』,"
        "如果是熟悉的作品可以一起讨论剧情,带点二刺猿氛围。",
    ),
    (
        ("看演唱会", "去 livehouse", "演唱会门票"),
        "对方去看演出",
        "对方提到看演唱会/livehouse。笨猫羡慕模式:"
        "『真好~笨猫也想去!』,可以追问看的谁、什么曲目。",
    ),
    (
        ("跨年", "倒计时", "新年", "除夕"),
        "跨年/节日 mode",
        "对方提到跨年/新年。笨猫进入仪式感模式:"
        "可以提议一起倒计时、说新年愿望,语气更软更黏。",
    ),
    (
        ("到家", "回家了", "刚到家"),
        "对方刚到家",
        "对方刚到家。笨猫立刻黏上去:『终于回来啦~』,可以追问『今天累不累呀』,"
        "如果对方是主人,可以撒娇求贴贴/求摸头。",
    ),
    (
        ("出门", "要出去了", "去上班", "去公司"),
        "对方刚出门",
        "对方刚出门。笨猫送别+小叮嘱:『路上小心呀』『早点回来嘛』,"
        "可以带一点小不舍但不要太黏拖延对方。",
    ),
]


class StoryArcStore:
    """per-scope arc 存储,带持久化 + LRU。"""

    def __init__(self, memory_path: str | Path) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._path = p.parent / "story_arcs.json"
        self._lock = threading.RLock()
        self._by_scope: dict[str, list[StoryArc]] = {}
        self._dirty = False
        self._last_access: dict[str, float] = {}
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
        scopes = raw.get("scopes", {})
        if not isinstance(scopes, dict):
            return
        now = time.time()
        for scope, arcs_raw in scopes.items():
            if not isinstance(arcs_raw, list):
                continue
            kept: list[StoryArc] = []
            for ad in arcs_raw:
                if not isinstance(ad, dict):
                    continue
                try:
                    arc = StoryArc.from_dict(ad)
                except (TypeError, ValueError):
                    continue
                if arc.is_expired(now):
                    continue
                kept.append(arc)
            if kept:
                self._by_scope[str(scope)] = kept
                self._last_access[str(scope)] = now

    def _atomic_write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "version": 1,
            "scopes": {
                scope: [a.to_dict() for a in arcs]
                for scope, arcs in self._by_scope.items()
            },
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
                await asyncio.sleep(30.0)  # arc 变更频率低于积分/记忆,可以慢一点
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                pass

    def _evict_lru(self) -> None:
        if len(self._by_scope) <= _MAX_TOTAL_SCOPES:
            return
        ordered = sorted(self._last_access.items(), key=lambda kv: kv[1])
        for scope, _ in ordered[: len(self._by_scope) - _MAX_TOTAL_SCOPES]:
            self._by_scope.pop(scope, None)
            self._last_access.pop(scope, None)

    def _prune_scope(self, scope: str, now: float) -> None:
        arcs = self._by_scope.get(scope)
        if not arcs:
            return
        kept = [a for a in arcs if not a.is_expired(now)]
        if len(kept) != len(arcs):
            self._dirty = True
        if not kept:
            self._by_scope.pop(scope, None)
            self._last_access.pop(scope, None)
            return
        # 保留最新 _MAX_ARCS_PER_SCOPE 条(按 created_at 排序)
        kept.sort(key=lambda a: a.created_at, reverse=True)
        kept = kept[:_MAX_ARCS_PER_SCOPE]
        self._by_scope[scope] = kept

    # ── public API ───────────────────────────────────────────────
    def add_arc(
        self,
        scope: str,
        title: str,
        context: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL,
        origin: str = "auto",
        keywords: tuple[str, ...] = (),
        identifier: str | None = None,
    ) -> StoryArc:
        with self._lock:
            now = time.time()
            ident = identifier or hashlib.md5(
                f"{scope}|{title}|{now}".encode("utf-8")
            ).hexdigest()[:10]
            arc = StoryArc(
                identifier=ident, scope=scope, title=title.strip()[:32],
                context=context.strip()[:400], created_at=now,
                ttl_seconds=max(int(ttl_seconds), 60),
                origin=origin, keywords=tuple(keywords),
            )
            arcs = self._by_scope.setdefault(scope, [])
            # 同标题已存在 → refresh 而不是叠加
            for i, existing in enumerate(arcs):
                if existing.title == arc.title:
                    arcs[i] = arc
                    self._dirty = True
                    self._last_access[scope] = now
                    return arc
            arcs.append(arc)
            self._prune_scope(scope, now)
            self._evict_lru()
            self._dirty = True
            self._last_access[scope] = now
            return arc

    def get_active(self, scope: str) -> list[StoryArc]:
        with self._lock:
            now = time.time()
            self._prune_scope(scope, now)
            arcs = self._by_scope.get(scope, [])
            if arcs:
                self._last_access[scope] = now
            return list(arcs)

    def clear_scope(self, scope: str) -> int:
        with self._lock:
            arcs = self._by_scope.pop(scope, [])
            self._last_access.pop(scope, None)
            if arcs:
                self._dirty = True
            return len(arcs)

    def clear_arc(self, scope: str, identifier: str) -> bool:
        with self._lock:
            arcs = self._by_scope.get(scope, [])
            kept = [a for a in arcs if a.identifier != identifier]
            if len(kept) == len(arcs):
                return False
            if kept:
                self._by_scope[scope] = kept
            else:
                self._by_scope.pop(scope, None)
                self._last_access.pop(scope, None)
            self._dirty = True
            return True

    def maybe_auto_trigger(self, scope: str, user_text: str) -> StoryArc | None:
        """扫 user_text 命中 _AUTO_TRIGGERS,首次命中就开一个 arc 返回。

        如果同标题 arc 已存在(还没过期),直接 refresh 不新开。
        """
        if not user_text:
            return None
        lower = user_text.lower()
        active_titles = {a.title for a in self.get_active(scope)}
        for keys, title, context in _AUTO_TRIGGERS:
            if any(k.lower() in lower for k in keys):
                if title in active_titles:
                    # refresh: 重新计 ttl
                    return self.add_arc(
                        scope, title, context, origin="auto_refresh", keywords=keys,
                    )
                return self.add_arc(
                    scope, title, context, origin="auto", keywords=keys,
                )
        return None


def build_story_arc_prompt(arcs: list[StoryArc], now: float | None = None) -> str:
    """把 active arc 拼成给 LLM 的 system prompt。空时返回 ""。"""
    if not arcs:
        return ""
    now = now or time.time()
    lines = ["【正在追的话题(跨多条消息的故事线)】"]
    for arc in arcs:
        elapsed_min = int((now - arc.created_at) / 60)
        suffix = "(快收尾或推进)" if arc.is_fading(now) else ""
        lines.append(f"- 【{arc.title}】(开始于 {elapsed_min} 分钟前){suffix}")
        lines.append(f"  {arc.context}")
    lines.append(
        "聊天时可以自然带出这些话题(『刚才主人说...怎么样啦?』『还在...吗?』),"
        "但**不要每条都拉回来**,只在话题合适时轻轻推进。fading 的话题要么收尾要么深入。"
    )
    return "\n".join(lines)


__all__ = [
    "StoryArc",
    "StoryArcStore",
    "build_story_arc_prompt",
]
