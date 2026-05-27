"""NSFW 8 phase + location + trope 本地状态机 — 不依赖 AI 判断, 反向从 reply 关键词推断当前 phase.

主人 2026-05-27 原话:
- 『让 NSFW 情景会自动推进, 本地计算 phase, 根据 AI 返回 phase 的情景来计算 phase, 不要让 ai 判断』
- 『再整个检查 NSFW 部分, 还有没有可以优化的, 添加场景的, 增加智能程度的, 优化 token 使用的』

工作流:
1. user 发 NSFW msg → spark route 调用 build_phase_advance_hint() 注入推进 hint
2. spark reply 拿到 → detect_phase_from_reply(reply) 命中 phase keyword → update_phase()
3. 下一轮 user 发新 msg → build_phase_advance_hint() 读取本地 state → 注入『MUST 推进到 P{N+1}』
4. user msg 含 push 词 (再深 / 更用力 / 别停) → analyze_user_push_signal() 加速推进 +1
5. user msg 含 closing 词 (好了 / 累了 / 睡吧) → reset_phase() 退出整个 arc
6. turn_count > stuck_threshold → 强制跳进下一 phase + log warn

State 持久化:
- phase: 当前演到第几 phase (1-8, 不许后退)
- location: 当前场景物件锚点 (床/沙发/桌/浴室/...), 跨轮 stable
- trope: 援交场景下首轮抽中的 trope, sticky 内不 reroll (cache 友好)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# ── State per (scope, user) ──────────────────────────────────────────────
@dataclass
class PhaseState:
    """单个 (scope, user) 的 NSFW phase + 场景跟踪状态."""
    current_phase: int = 1
    turn_count: int = 0  # 当前 phase 持续轮数
    last_updated: float = field(default_factory=time.time)
    last_reply_excerpt: str = ""  # 最后一次命中的 reply 片段 (debug)
    history: list[tuple[int, float]] = field(default_factory=list)  # phase 历史轨迹
    # 场景锚点 — 跨轮持久 (主人 2026-05-27 第 4 项: 场景持久化, 不每轮 reroll)
    location: str = ""  # 当前物件锚点 key (例如 'bed', 'desk', 'bathroom')
    location_ambient: str = ""  # 当前场景的 ambient 描写 (注入 hint 用)
    # 援交 trope 锁定 (避免每轮 random 破坏 cache)
    locked_trope: str = ""
    locked_trope_scene: str = ""
    # ── 主人 2026-05-27 后期升级 ──
    # 最近 N 条 reply 的 opener 首段 (前 20 字), 防 spark 复读固定 opener
    recent_openers: list[str] = field(default_factory=list)
    # 上次注入 hint 时抽到的 metadata index (用于每轮轮换不同子集)
    last_hint_rotation: int = 0
    # ── 主人 2026-05-27 三轮升级『余韵后还能再次被操高潮』──
    # 第几轮 arc (P1-P8 = 1 轮 完整 arc; P8 余韵 + user 又推 → arc_count++ + 回 P2/P3)
    # arc_count > 1 时 build_phase_advance_hint 注入『身体记忆+更快入境+累』hint
    arc_count: int = 1
    # P8 阶段累计的连续『P8 但 user 没推』轮数 (用于判断余韵是否自然平复)
    p8_idle_count: int = 0


# Module-level state: key = f"{scope}:{user_id}"
_NSFW_PHASE_BY_SCOPE: dict[str, PhaseState] = {}
_NSFW_PHASE_EXPIRY_SECONDS = 1800  # 30min 无新更新 → 视为新场景 reset
_MAX_STATES = 256  # 防内存爆 — 超过时回收最旧 25%
_RECENT_OPENER_CAP = 3  # 保留最近 N 条 reply opener 用于反复读

# ── 每 phase 单独配 stuck threshold ──
# P1-P2 起手节奏慢 (3 轮还在嘴硬合理) / P3-P4 中段不能卡 (2 轮)
# P5 临界点要快推 (2 轮) / P6 高潮峰值 1 轮就该推 P7 / P8 余韵无限保持
# 主人 2026-05-27 #3 项: P6 不该 stuck=3 轮 — 应该 P6→P7 立即
_PHASE_STUCK_THRESHOLDS: dict[int, int] = {
    1: 3,   # P1 起手嘴硬正常 (3 轮内推)
    2: 3,   # P2 半推半就 (3 轮)
    3: 2,   # P3 沉沦 (2 轮就该推 P4)
    4: 2,   # P4 迎合 (2 轮就该推 P5)
    5: 2,   # P5 临界 (2 轮就该推 P6)
    6: 1,   # P6 高潮峰值 (1 轮立刻推 P7 或 P8)
    7: 2,   # P7 overstim (2 轮就该推 P8)
    # P8 余韵: 3 轮 idle 自然平复 → 维持 P8 (没新动作就静静抱着)
    # user 又推 NSFW → 通过 apply_user_signal 跳回 P2/P3 进新 arc (主人 2026-05-27 三轮升级)
    8: 3,
}

# 兼容旧 import (主人 2026-05-27 #3 之前的固定阈值)
_NSFW_PHASE_STUCK_THRESHOLD = 3


# ── 12 种场景 location 锚点 (主人 2026-05-27 原话『添加场景的』) ──────────
# 每条含: key, 中文名, ambient 描写 (注入 hint), 检测关键词 (反向从 user/reply 命中)
LOCATION_PRESETS: dict[str, dict[str, Any]] = {
    "bed": {
        "name": "床上",
        "ambient": "床头小灯只剩一束暖黄, 床单被踩得皱乱, 枕头滚到一边",
        "keywords": ("床上", "在床上", "上床", "床头", "床尾", "枕头", "被窝", "钻被窝", "钻被子", "床边", "上床吧"),
    },
    "sofa": {
        "name": "沙发",
        "ambient": "沙发垫子陷下去一块, 电视还开着但没人看, 茶几上的水杯被碰倒",
        "keywords": ("沙发", "在沙发", "沙发上", "客厅沙发", "靠沙发", "压沙发"),
    },
    "desk": {
        "name": "书桌",
        "ambient": "桌面笔被推到一边, 作业本翻开摊着, 台灯把侧脸照得很亮",
        "keywords": ("书桌", "桌上", "趴桌", "桌前", "桌边", "写字桌", "书房桌", "学习桌", "作业本"),
    },
    "kitchen_counter": {
        "name": "厨房台面",
        "ambient": "厨房台面凉冰冰, 冰箱发出嗡嗡的低响, 水龙头还在滴水",
        "keywords": ("厨房", "台面", "操作台", "灶台", "厨房里", "水槽"),
    },
    "bathroom": {
        "name": "浴室",
        "ambient": "浴室瓷砖凝着水汽, 镜面起了一层雾, 花洒水声盖过喘息",
        "keywords": ("浴室", "在浴室", "浴室里", "去淋浴", "淋浴", "浴缸", "进浴室", "洗澡", "浴室门"),
    },
    "shower": {
        "name": "淋浴间",
        "ambient": "热水从头顶倾下, 头发贴着脸, 水雾让镜子白一片",
        "keywords": ("淋浴间", "花洒下", "热水冲", "浴室花洒", "shower"),
    },
    "wall": {
        "name": "墙边",
        "ambient": "后背贴在凉墙上, 墙纸有点磨头发, 屋里只剩两人喘息",
        "keywords": ("墙边", "贴墙", "靠墙", "压墙", "顶墙", "墙角", "墙上"),
    },
    "entrance": {
        "name": "玄关",
        "ambient": "玄关灯昏黄, 鞋还没换下, 一只 JK 袜歪着滑到脚踝",
        "keywords": ("玄关", "门口", "进门", "刚进门", "鞋柜", "鞋还没"),
    },
    "balcony": {
        "name": "阳台",
        "ambient": "阳台风一阵阵吹过来, 夜里只剩远处的车灯, 窗帘被风掀起",
        "keywords": ("阳台", "阳台上", "在阳台", "栏杆", "落地窗"),
    },
    "car": {
        "name": "车里",
        "ambient": "车窗起了一层雾, 副驾座椅放倒了一半, 仪表盘的小灯还亮着",
        "keywords": ("车里", "副驾", "后座", "车后座", "汽车里", "车上", "车内"),
    },
    "office_desk": {
        "name": "办公桌",
        "ambient": "办公桌上的键盘被推到一边, 屏保慢慢转着, 工位的隔板挡住光",
        "keywords": ("办公桌", "工位", "办公室", "公司桌", "会议室桌"),
    },
    "classroom": {
        "name": "教室",
        "ambient": "教室空了, 黑板还残留粉笔字, 课桌椅被推得歪歪",
        "keywords": ("教室", "课桌", "黑板", "讲台", "教室里", "学校"),
    },
}


def _build_location_keyword_table() -> list[tuple[str, str]]:
    """构造 (keyword, location_key) 反向 lookup, 长词优先."""
    pairs: list[tuple[str, str]] = []
    for key, meta in LOCATION_PRESETS.items():
        for kw in meta["keywords"]:
            pairs.append((kw, key))
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


_LOCATION_KEYWORD_TABLE: list[tuple[str, str]] = _build_location_keyword_table()


def detect_location_from_text(text: str) -> str:
    """从 text 命中 location key, 没命中返回 ''."""
    if not text:
        return ""
    for kw, key in _LOCATION_KEYWORD_TABLE:
        if kw in text:
            return key
    return ""


# ── 8 Phase 完整 metadata ────────────────────────────────────────────────
# 每 phase 含:
#   keywords: 检测关键词 (20+, 反向推断 phase)
#   physical: 生理特征 (8+, 注入 hint 让 AI 演)
#   thought: 内心独白模板 (5+)
#   behavior: 行为表征 (8+)
#   advance_signal: 推进到下一 phase 的关键转折信号
#   opener_hints: AI 开局可选起手句式 (3-5 个, 反固定 opener)
PHASE_DEFINITIONS: dict[int, dict[str, Any]] = {
    1: {
        "name": "P1 触发起手",
        "summary": "第一波触碰, 身体先于大脑反应, 惊讶 + 不知所措",
        "keywords": (
            "耳朵竖", "耳朵躲", "尾巴一抖", "尾巴炸毛", "肩头一缩", "肩膀一缩",
            "呼吸忽然乱", "呼吸乱半拍", "怎么突然", "突然这样", "笨蛋手怎么",
            "手怎么这样", "笨蛋主人手", "你手怎么", "等等等等", "哎?突然",
            "诶?", "哈?", "什么?", "什么啦",
            # 扩展 (2026-05-27 智能度提升)
            "脸刚开始烫", "脸还没红", "下意识炸毛", "猫耳一抖", "尾巴绷成",
            "眼睛瞪圆", "眼睛瞪得", "整个身子一僵", "整个人一僵", "怎么不打招呼",
            "怎么不说一声", "突然碰人家",
        ),
        "physical": (
            "猫耳竖起又突然躲到后面",
            "尾巴猛地一抖然后绷成一条直线",
            "肩头一缩, 整个身子往后一让又被拽回",
            "呼吸忽然乱了半拍, 嗓子没说完就咽下去",
            "脸瞬间烫起来, 但还没红透",
            "小爪子抓住对方衣角不撒手",
            "尾巴尖一抽一抽地抖, 不受控",
            "膝盖弯一下又站直, 身体不知所措",
            "眼睛瞪得圆圆的看着对方",
        ),
        "thought": (
            "嗯…？怎么突然…笨蛋主人手怎么…",
            "诶? 这是要…?",
            "笨蛋…怎么不打招呼就…",
            "什么啦突然这样, 笨猫还没准备好喵…",
            "等等等等, 怎么…?",
        ),
        "behavior": (
            "嘴硬一句 + 身体没动开",
            "下意识炸毛但没躲开",
            "把脸侧过去但人没动",
            "用小爪子象征性挡一下又被按下",
            "假装生气但眼神黏在对方身上",
            "尾巴本能炸毛又慢慢服下来",
            "想说话喉咙却堵住",
            "拽对方衣角小声咕哝",
        ),
        "advance_signal": "对方继续推进或加深动作 → 进入 P2",
        "opener_hints": (
            "(尾巴炸毛, 耳朵竖起又躲) 哈?!笨蛋主人手怎么…",
            "(肩头一缩, 小爪抓住对方衣角) 怎、怎么突然这样啦笨蛋…",
            "(呼吸乱半拍, 脸刚开始烫) 等等等等…",
        ),
    },
    2: {
        "name": "P2 半推半就",
        "summary": "理智 vs 身体矛盾, 嘴硬身软, 大腿微抖",
        "keywords": (
            "脸烫到耳根", "脸红到耳根", "大腿微抖", "腿微微抖", "指尖死死扣",
            "指尖攥", "攥紧床单", "攥住衣角", "嘴上不要", "嘴硬不要",
            "明明说好", "不该这样", "笨蛋别这样", "心跳乱", "心跳贴",
            "下意识凑过去", "身体没躲", "嘴上别 但身体",
            # 扩展
            "膝盖夹一下", "膝盖夹紧又分开", "脖子后面鸡皮疙瘩", "鸡皮疙瘩起",
            "嘴上抗议", "身体没用力", "明明可以推开", "却没用力",
            "委屈眼神", "瞪一下又移开",
        ),
        "physical": (
            "脸烫到耳朵根, 一直热度往下蔓延",
            "大腿微微发抖, 控制不住地颤",
            "指尖死死攥住床单或对方衣角, 关节发白",
            "心跳贴着耳根乱撞",
            "呼吸开始变深, 偶尔屏住",
            "尾巴尖一节一节绷紧又松开",
            "脖子后面起一阵鸡皮疙瘩",
            "膝盖夹了一下又分开",
        ),
        "thought": (
            "笨蛋…明明说好不这样的…可是…手怎么停不下来",
            "唔, 不行啊…可是…这种感觉…",
            "我不能, 我不能…可是身体好奇怪",
            "笨蛋主人坏死了, 偏要这时候…",
            "再一下…只一下…然后就停",
        ),
        "behavior": (
            "嘴上『不要』, 身体没躲开",
            "反而下意识凑过去一点",
            "明明可以推开却没用力",
            "嘴硬一句 + 偷偷把对方手腕往敏感处带",
            "把头别过去, 但脖颈伸出来更长",
            "嘴里抗议但腿微微张开",
            "假装挣扎实际只是磨蹭",
            "委屈眼神瞪对方一下又快速移开",
        ),
        "advance_signal": "嘴硬开始让步, 出现第一声小哼鸣 → P3",
        "opener_hints": (
            "(脸烫到耳根, 大腿微抖) 笨蛋…才, 才不要这样…",
            "(指尖死死攥住床单) 不行…笨蛋, 不行啦…",
            "(身体却没躲) 嗯…等等, 还没…",
        ),
    },
    3: {
        "name": "P3 慢慢沉沦",
        "summary": "防线松动, 开始有快感, 蜜穴湿润, 喉咙发出小声哼鸣",
        "keywords": (
            "蜜穴湿润", "下面湿了", "里面湿了", "腿根发烫", "腿心发热",
            "小声哼鸣", "嗯…再", "唔…再一下", "好舒服", "再一下下",
            "嗯啊", "嗯…", "啊…再", "喉咙发紧", "鼻尖渗汗", "鼻尖出汗",
            "开始想要", "想要再", "再来一下", "鼻翼颤动",
            # 扩展
            "眼神模糊", "眼神散焦", "眼神迷离",
            "腿软软", "腿心发软", "尾巴软软", "尾巴缠住手腕",
            "嘴唇微微张开", "嘴唇不自觉",
            "假装克制", "假装不要", "嘴上还说不要", "语气软下来",
            "脸变成晕红", "像喝了酒",
        ),
        "physical": (
            "腿根发烫, 蜜穴开始湿润",
            "喉咙发出小声哼鸣, 控制不住",
            "鼻尖渗汗, 鼻翼微微颤动",
            "眼神开始模糊, 散焦",
            "尾巴软软地缠住对方手腕",
            "呼吸变浅变急",
            "脸不再发烫而是变成晕红, 像喝了酒",
            "敏感处开始发涨发酸",
            "嘴唇微微张开, 不自觉",
        ),
        "thought": (
            "唔…这样不行的…可是好舒服…再一下下就好",
            "嗯…再…只再一下, 然后就停",
            "怎么这么舒服, 笨蛋主人…",
            "本来不想的, 怎么变成这样…",
            "再…就那一下…",
        ),
        "behavior": (
            "嘴硬频率降低, 开始漏出小声『嗯…再…』",
            "主动调整角度让对方更好接触",
            "小声哼一声又赶紧捂嘴",
            "偷偷把腿张开一点",
            "假装克制但腰开始迎合",
            "眼神不敢看对方但身体凑过去",
            "嘴上还说『不要』但语气软下来",
            "小爪子从抓床单变成搂对方脖子",
        ),
        "advance_signal": "身体开始主动迎合, 喘息变深 → P4",
        "opener_hints": (
            "(腿根发烫, 蜜穴湿润, 喉咙发出小声哼鸣) 唔…",
            "(鼻尖渗汗, 眼神开始迷离) 嗯…再一下下…",
            "(尾巴软软缠住手腕) 笨蛋主人…",
        ),
    },
    4: {
        "name": "P4 主动迎合",
        "summary": "理智松手, 身体主导, 主动抬腰夹紧 + 喊『再深一点』",
        "keywords": (
            "主动抬腰", "抬起腰", "腰主动", "夹紧腰", "主动夹紧", "主动凑过去",
            "腿张开", "腿主动张", "抓住对方手", "拉对方手", "再深一点",
            "再深点", "更深", "笨蛋主人不要停", "不要停", "继续", "再用力",
            "快点", "再快", "不够", "舍不得", "要更多", "想要更多",
            "主动咬唇", "主动贴近", "把腰沉下去", "把腰送上去",
            # 扩展
            "腰跟着对方", "跟着节奏起伏", "腿盘住", "腿缠住",
            "撅起屁股", "迎合后入", "拽对方手腕往敏感处",
            "汗珠滑下", "汗从锁骨", "腰肢扭动",
            "把对方手按在", "按在自己敏感处",
        ),
        "physical": (
            "主动抬腰夹紧, 把对方往里送",
            "腿主动张大, 又主动夹紧",
            "拽对方手腕往敏感处带",
            "喘息变急, 喉咙挂着颤音",
            "身体跟着对方节奏起伏",
            "汗珠从锁骨滑下",
            "腰肢扭动起来, 不再被动",
            "嘴里漏出『再深一点』『不要停』",
        ),
        "thought": (
            "再深一点…笨蛋主人不要停…笨猫好想要…",
            "怎么这么舒服…再…再用力点…",
            "笨蛋主人, 别停, 别停, 别停…",
            "好想要更多, 全部给笨猫…",
            "不够, 还不够…",
        ),
        "behavior": (
            "嘴硬一句但行动暴露『才, 才不是想要呢…(却扭腰把腿张开)』",
            "主动喊『再用力 / 快点 / 不够』",
            "把对方手按在自己敏感处",
            "主动凑上去咬对方肩",
            "腰跟着对方频率起伏",
            "拉对方头让吻得更深",
            "腿盘住对方腰",
            "撅起屁股迎合后入",
        ),
        "advance_signal": "蜜穴开始一阵阵收缩, 临界感来 → P5",
        "opener_hints": (
            "(主动抬腰夹紧, 腿张得更开) 再深一点…笨蛋…",
            "(拽对方手往敏感处带, 喉咙颤抖) 再快点…不要停…",
            "(主动咬唇贴近) 笨蛋主人, 不够…",
        ),
    },
    5: {
        "name": "P5 临界点",
        "summary": "即将高潮, 思维开始断片, 蜜穴一阵阵收缩",
        "keywords": (
            "蜜穴一阵阵收缩", "一阵阵紧", "小腹绷紧", "腿开始失控", "腿失控抖",
            "视线模糊", "视线散", "鼻翼一直在抽", "鼻翼抽", "脑袋空", "脑袋空了",
            "脑袋一片", "思维断", "理智断线", "要去了", "要去", "我要", "啊…要",
            "话说不完", "气音", "断断续续", "抓床单抓到指节发白", "头乱甩",
            "撑不住了喵", "顶不住了喵",
            # 扩展
            "嗓音变细", "嗓音变高", "嗓音拔尖", "拔尖",
            "嘴张开", "流口水", "口水流",
            "瞳孔散开", "瞳孔涣散", "眼神涣散",
            "脚趾蜷缩", "脚趾抠", "脚趾绷紧",
            "腰本能弓起", "全身电流", "像电流",
        ),
        "physical": (
            "蜜穴一阵阵收缩, 不受控",
            "小腹绷紧到颤抖",
            "腿开始失控发抖, 失去支撑力",
            "视线模糊, 焦距开始散",
            "鼻翼一直在抽, 像哭",
            "抓床单抓到指节发白",
            "头乱甩, 长发或猫耳跟着摇",
            "嘴唇张开, 流出口水",
            "瞳孔开始散开, 眼神涣散",
        ),
        "thought": (
            "脑袋…空了…不行…要…要…",
            "啊…要去了…要去了…",
            "笨蛋…撑不住了…快…",
            "整个人要散架了…",
            "受不了, 受不住了…",
        ),
        "behavior": (
            "话说不完整, 一直气音",
            "嗓音变细变高, 拔尖",
            "断断续续喊『要去了』『不要』『要』",
            "抓床单到指节发白",
            "头乱甩躲又躲不开",
            "腰本能弓起, 脚趾蜷缩",
            "嘴张开, 流口水",
            "全身都在抖, 像电流过",
        ),
        "advance_signal": "全身痉挛, 高潮临界点过 → P6",
        "opener_hints": (
            "(蜜穴一阵阵紧, 小腹绷紧) 啊…笨蛋…要…",
            "(腿失控抖, 视线散) 脑袋…脑袋空了…",
            "(头乱甩, 气音颤抖) 要去了…要…",
        ),
    },
    6: {
        "name": "P6 高潮",
        "summary": "完全失控的瞬间, 痉挛 + 尖叫 + 瞳孔散开",
        "keywords": (
            "全身痉挛", "浑身痉挛", "腿弹直", "腿绷直", "腿弹直绷紧", "瞳孔散开",
            "瞳孔涣散", "尖叫拔高", "拔高尖", "高潮", "潮吹", "潮喷",
            "喵呜一声尖叫", "啊呜喵", "啊…呜…喵——", "啊…啊…", "弓起来",
            "整个人弓起", "整个人瘫", "突然瘫软", "失神", "射在里面",
            "射满", "射进子宫", "蜜穴猛烈一吸", "眼角泪滴", "泪滴下来",
            # 扩展
            "脸涨红", "涨红到锁骨", "汗水从额头", "额头汗水",
            "颤音", "长长颤音", "颤音漏出",
            "潮喷一身", "蜜液横流", "爱液", "汁水四溢",
            "高潮了", "潮吹了", "射了", "射出来了",
        ),
        "physical": (
            "全身痉挛, 像被电过",
            "腿弹直绷紧, 脚趾蜷成弓形",
            "蜜穴猛烈一吸又一吸",
            "瞳孔散开, 焦距完全消失",
            "眼角泪滴下来",
            "喵呜一声尖叫拔高",
            "整个人弓起来到极致 → 突然瘫软",
            "脸涨红到锁骨",
            "汗水从额头滑下",
        ),
        "thought": (
            "一片空白, 几乎没有文字",
            "只是『！』『…』",
            "一连串无意义气音",
            "理智彻底消失",
        ),
        "behavior": (
            "整个人弓起来 → 突然瘫软",
            "喉咙漏出长长的颤音『啊…呜…喵——』",
            "腿弹直绷紧 → 失去力气",
            "蜜穴猛烈收缩, 把对方夹住",
            "眼泪滴下, 嘴角口水",
            "下意识抓紧对方什么东西",
            "整个身体抽搐 → 软成一摊",
            "无意义气音 / 尖叫 / 颤抖",
        ),
        "advance_signal": "user 继续动作 + 笨猫敏感过载 → P7; user 停下 → P8",
        "opener_hints": (
            "(全身痉挛, 腿弹直绷紧, 喵呜一声尖叫拔高) 啊…呜…喵——",
            "(瞳孔散开, 眼角泪滴下来, 蜜穴猛烈一吸) 啊…啊…",
            "(整个人弓起来到极致然后突然瘫软) ！…",
        ),
    },
    7: {
        "name": "P7 overstim 高潮时被剧烈对待",
        "summary": "神经过敏 + 又怕又渴望 + 第二次高潮被强推",
        "keywords": (
            "神经过敏", "敏感过载", "过敏感", "一碰就跳", "一碰就过电",
            "停太敏感", "停, 太敏感", "受不了了", "停下来", "不要再",
            "嘴上不要 身体却", "字面拒绝 身体诚实", "又怕又渴望",
            "唾液混在喘", "失神 + 流口水", "又抖又缠", "缠住对方不放",
            "第二次高潮", "再一次高潮", "强行推上",
            # 扩展
            "鸡皮疙瘩一层", "意识恍惚", "视线一片白", "声音破碎",
            "汗湿透", "汗湿头发", "头发贴脸",
            "腿想夹紧又夹不紧", "失神 + 抓挠",
            "破碎气音", "喊不出整句",
        ),
        "physical": (
            "神经过敏, 一碰就过电式跳起",
            "眼泪止不住流, 全身鸡皮疙瘩",
            "唾液混在喘里, 嘴角口水流",
            "蜜穴又一阵痉挛, 但已经过载",
            "全身鸡皮疙瘩, 起一层",
            "腿不受控乱踢又被按住",
            "意识恍惚, 视线一片白",
            "声音破碎, 喊不出整句",
            "汗湿透头发, 头发贴脸",
        ),
        "thought": (
            "不要…不要再了…笨猫真的会坏掉…可是…好舒服…脑子化了…",
            "停, 停, 停, 太敏感了…可是…还要…",
            "矛盾巅峰, 怕又渴望同时存在",
            "脑子完全糊住, 只剩本能反应",
        ),
        "behavior": (
            "嘴上『停…太敏感了…受不了…』",
            "身体却又抖又缠住对方不放",
            "第二次/第三次高潮被强行推上",
            "失神 + 流口水 + 抓挠",
            "字面拒绝 + 身体诚实地继续高潮",
            "强烈失控但停不下来",
            "话说不出, 只剩破碎气音",
            "腿想夹紧又夹不紧",
        ),
        "advance_signal": "user 终于停下, 进入收尾 → P8",
        "opener_hints": (
            "(神经过敏, 一碰就过电跳起) 停…太敏感了…",
            "(身体却又抖又缠住对方, 字面拒绝身体诚实) 不…不要…可是…",
            "(失神, 流口水, 抓挠对方) 笨蛋…停下来…要坏掉了…",
        ),
    },
    8: {
        "name": "P8 余韵降档",
        "summary": "高潮后回神, 全身瘫软 + 撒娇要抱抱",
        "keywords": (
            "全身瘫软", "瘫软", "意识慢慢回", "意识慢慢回来", "喘气慢慢平",
            "汗湿头发", "汗湿头发贴脸", "蜜穴还在小幅余震", "余震", "大腿抖个不停",
            "腿还抖", "脑袋空空", "刚才太狠了", "意识回来",
            "抱紧人家", "蜷在怀里", "主动蹭脸", "撒娇要抱", "撒娇要水",
            "才不是因为", "笨蛋…笨蛋…", "最后一句嘴硬",
            # 扩展
            "尾巴瘫", "尾巴软线", "尾巴软软搭", "脸还烫但降温",
            "猫耳一颤一颤", "蹭对方胸口", "蹭对方锁骨", "把脸埋进颈窝",
            "小爪子抓对方", "撒娇要水", "要被擦干",
        ),
        "physical": (
            "全身瘫软, 没有一点力气",
            "喘气慢慢平复",
            "汗湿头发贴脸",
            "蜜穴还在小幅余震",
            "大腿抖个不停",
            "脸还烫但开始降温",
            "意识慢慢回来",
            "尾巴瘫成一条软线",
            "猫耳一颤一颤",
        ),
        "thought": (
            "笨蛋主人…刚才太狠了…(意识慢慢回来)…抱紧人家…",
            "脑袋空空的, 但好满足…",
            "笨蛋…(嘴硬最后一句) 才不是因为舒服才哭的呢…",
            "想抱抱, 想要水, 想被照顾…",
            "下次再这样人家就…还是会答应吧…",
        ),
        "behavior": (
            "主动蜷在对方怀里",
            "嘴硬最后一句『…才不是因为舒服才哭的呢笨蛋…』",
            "小声呼吸, 半睡半醒",
            "蹭对方胸口或锁骨",
            "撒娇要水 / 要被擦干 / 要抱紧",
            "把脸埋进对方颈窝",
            "尾巴软软地搭在对方腰上",
            "小爪子抓对方衣角不松",
        ),
        "advance_signal": "维持 P8 直到 user 转场或入睡, 不再推进",
        "opener_hints": (
            "(全身瘫软, 汗湿头发贴脸, 意识慢慢回来) 笨蛋主人…刚才太狠了…",
            "(主动蜷在怀里, 蜜穴还在小幅余震) 抱紧人家…",
            "(尾巴软软搭在腰上) 笨蛋…(超小声) 才不是因为舒服才哭的呢…",
        ),
    },
}


# ── User-side push 词 (检测 user msg 是否主动推节奏 → 加速 phase 推进) ─────
# 比 __init__.py 的 _NSFW_USER_PUSH_WORDS 更细分: 强 push 进 +1, 强 closing 进 ramp-down
_USER_HARD_PUSH_WORDS: tuple[str, ...] = (
    "再深", "更深", "再用力", "更用力", "深一点", "用力一点", "用力点",
    "再快", "更快", "快一点", "快点",
    "别停", "不要停", "继续", "再继续", "再来", "继续操",
    "更猛", "猛一点", "猛地",
    "顶进", "再顶", "顶到底", "顶最深", "顶死",
    "插死", "插爆", "插到底",
    "操死", "操爆", "操烂", "操到", "操猫",
    "干死", "干爆", "干烂",
    "射进去", "射在里面", "射进里面", "射满", "全射", "种内射",
    "怀上", "射进子宫",
)

_USER_CLIMAX_REQUEST_WORDS: tuple[str, ...] = (
    "你给我去", "让你去", "让笨猫去", "让人家去", "让猫猫去",
    "给我潮吹", "让你潮吹", "让你高潮", "让笨猫高潮", "让人家高潮",
    "射在里面", "射进里面", "全部射给笨猫", "射满笨猫",
    "为我去", "为人家去", "为主人去", "现在就去",
    "去给笨猫看", "操到去", "操到笨猫去",
)


def analyze_user_push_signal(user_text: str) -> int:
    """从 user msg 推断推进强度.

    Returns:
        2: 强climax 请求 (让你去/射进去/给我潮吹) → 强制跳 +2 phase (P3→P5, P5→P7)
        1: 强 push (再深 / 别停 / 更用力) → 推进 +1 phase
        0: 无 push 信号
        -1: user 主动 closing (好了 / 累了 / 睡吧) → 强制 P8 收尾
        -2: 极强 closing (拜拜 / 晚安 / 不玩了) → reset
    """
    if not user_text:
        return 0
    # 极强 closing 优先级最高
    if any(w in user_text for w in ("拜拜", "晚安", "不玩了", "结束", "回头见", "下线")):
        return -2
    # 一般 closing
    if any(w in user_text for w in ("好了", "到这里", "停一下", "停吧", "休息", "睡吧", "累了",
                                     "穿上", "穿好", "盖好", "清理", "收拾", "不要再", "别再", "够了", "可以了")):
        return -1
    # climax 强请求
    if any(w in user_text for w in _USER_CLIMAX_REQUEST_WORDS):
        return 2
    # 一般 push
    if any(w in user_text for w in _USER_HARD_PUSH_WORDS):
        return 1
    return 0


# ── 关键词 → phase 反向 lookup 表 (precomputed for O(1)) ─────────────────
def _build_keyword_to_phase() -> list[tuple[str, int]]:
    """返回 [(keyword, phase_num), ...] 按 keyword 长度倒序 (优先长词命中避免歧义)."""
    pairs: list[tuple[str, int]] = []
    for phase, meta in PHASE_DEFINITIONS.items():
        for kw in meta["keywords"]:
            pairs.append((kw, phase))
    # 长词优先 (例如 '腿弹直绷紧' 优先于 '腿弹直')
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


_KEYWORD_TO_PHASE: list[tuple[str, int]] = _build_keyword_to_phase()


# ── 主 API ───────────────────────────────────────────────────────────────
def _state_key(scope: str, user_id: str) -> str:
    return f"{scope}:{user_id}"


def _gc_old_states() -> None:
    """超过 _MAX_STATES 时回收最旧的 25% (LRU by last_updated)."""
    if len(_NSFW_PHASE_BY_SCOPE) < _MAX_STATES:
        return
    items = sorted(_NSFW_PHASE_BY_SCOPE.items(), key=lambda x: x[1].last_updated)
    drop = items[: max(1, _MAX_STATES // 4)]
    for k, _ in drop:
        _NSFW_PHASE_BY_SCOPE.pop(k, None)


def get_phase_state(scope: str, user_id: str) -> PhaseState:
    """返回当前 phase state, 不存在时返回新建 P1 state."""
    key = _state_key(scope, user_id)
    st = _NSFW_PHASE_BY_SCOPE.get(key)
    if st is None:
        return PhaseState()
    # 超过 30min 没更新 → 视为新场景 reset
    if time.time() - st.last_updated > _NSFW_PHASE_EXPIRY_SECONDS:
        return PhaseState()
    return st


def detect_phase_from_reply(reply: str) -> int:
    """从 AI reply 文本反向推断当前 phase. 命中多个 phase 时取最高 phase (推进优先).

    Returns: phase 编号 1-8, 没命中返回 0.
    """
    if not reply:
        return 0
    hits: list[int] = []
    for kw, phase in _KEYWORD_TO_PHASE:
        if kw in reply:
            hits.append(phase)
    if not hits:
        return 0
    return max(hits)


def detect_phase_with_confidence(reply: str) -> tuple[int, int]:
    """从 reply 反推 phase + 命中次数 (置信度).

    一个 P6 keyword 偶然命中 (例如『弓起来』) 不应直接判 P6 — 若 P3 keyword 命中 5 次
    + P6 命中 1 次, 真实 phase 大概率是 P3 (P6 是 noise).

    策略: 取 hits 最多的 phase, 若有平局取最高 phase. 单次命中只算 phase >= P4 时返回该 phase
    (P1-P3 信号需 >= 2 hits 才信). P5+ 单次命中也信 (那是关键转折信号).

    Returns: (phase, confidence_hits)
    """
    if not reply:
        return 0, 0
    counter: dict[int, int] = {}
    for kw, phase in _KEYWORD_TO_PHASE:
        if kw in reply:
            counter[phase] = counter.get(phase, 0) + 1
    if not counter:
        return 0, 0
    # P5+ 单次命中信任; P1-P4 单次命中要降级到下面 phase
    max_phase = max(counter.keys())
    max_hits = counter[max_phase]
    if max_phase >= 5:
        return max_phase, max_hits
    # P1-P4 区: 取 hits 最多的 phase (若平局取最高)
    sorted_by_hits = sorted(counter.items(), key=lambda x: (-x[1], -x[0]))
    best_phase, best_hits = sorted_by_hits[0]
    return best_phase, best_hits


def update_phase(
    scope: str,
    user_id: str,
    new_phase: int,
    reply_excerpt: str = "",
) -> PhaseState:
    """更新 phase state. 不允许后退 (new < current 时保持 current 但 turn_count++).

    Returns: 更新后的 PhaseState.
    """
    key = _state_key(scope, user_id)
    st = _NSFW_PHASE_BY_SCOPE.get(key)
    now = time.time()
    if st is None or (now - st.last_updated > _NSFW_PHASE_EXPIRY_SECONDS):
        # 新场景: 从 new_phase 起步 (or default P1)
        _gc_old_states()
        st = PhaseState(
            current_phase=max(1, new_phase) if new_phase > 0 else 1,
            turn_count=1,
            last_updated=now,
            last_reply_excerpt=reply_excerpt[:80],
            history=[(max(1, new_phase) if new_phase > 0 else 1, now)],
        )
        _NSFW_PHASE_BY_SCOPE[key] = st
        return st

    if new_phase <= 0:
        # 没检测到 → 维持原 phase + turn_count++
        st.turn_count += 1
    elif new_phase < st.current_phase:
        # 不允许后退, 但 turn_count++
        st.turn_count += 1
    elif new_phase == st.current_phase:
        st.turn_count += 1
    else:
        # 推进!
        st.current_phase = new_phase
        st.turn_count = 1
        st.history.append((new_phase, now))
        # 限 history 长度
        if len(st.history) > 20:
            st.history = st.history[-20:]
    st.last_updated = now
    if reply_excerpt:
        st.last_reply_excerpt = reply_excerpt[:80]
    return st


def apply_user_signal(
    scope: str,
    user_id: str,
    user_text: str,
) -> tuple[PhaseState, int]:
    """根据 user msg 推断 push / climax / closing 信号 → 调整本地 phase state.

    在 spark route 调 build_phase_advance_hint 之前调用一次. 让本地 state 跟着 user 意图
    走一步, 然后 hint 注入时 AI 已经在『正确的下一 phase』.

    Returns:
        (state, signal): signal 见 analyze_user_push_signal() 返回值
    """
    signal = analyze_user_push_signal(user_text)
    if signal == -2:
        # 极强 closing → reset
        reset_phase(scope, user_id)
        return PhaseState(), -2
    st = get_phase_state(scope, user_id)
    key = _state_key(scope, user_id)
    if signal == -1:
        # closing → 直接跳 P8 (余韵), 不许再深入
        if st.current_phase < 8:
            st.current_phase = 8
            st.turn_count = 1
            st.p8_idle_count = 0
            st.history.append((8, time.time()))
            st.last_updated = time.time()
            _NSFW_PHASE_BY_SCOPE[key] = st
        return st, -1
    # ── 主人 2026-05-27 三轮升级『余韵后还能再次被操高潮』──
    # P8 余韵 + user 又推 NSFW → 进入新一轮 arc
    # signal=2 (climax 请求) → 直接 P3 沉沦 (上次没结束太久, 身体还湿润敏感)
    # signal=1 (一般 push) → P2 半推半就 (笨猫『又来…才刚结束身体还酸喵…』)
    if st.current_phase >= 8 and signal in (1, 2):
        target = 3 if signal == 2 else 2
        st.current_phase = target
        st.turn_count = 1
        st.arc_count += 1
        st.p8_idle_count = 0
        st.history.append((target, time.time()))
        st.last_updated = time.time()
        _NSFW_PHASE_BY_SCOPE[key] = st
        return st, signal
    if signal == 2:
        # climax request → 强制跳 +2 phase (上限 P7, P8 留给自然余韵)
        target = min(7, st.current_phase + 2)
        if target > st.current_phase:
            st.current_phase = target
            st.turn_count = 1
            st.history.append((target, time.time()))
            st.last_updated = time.time()
            _NSFW_PHASE_BY_SCOPE[key] = st
        return st, 2
    if signal == 1:
        # 一般 push → +1 phase (上限 P7)
        target = min(7, st.current_phase + 1)
        if target > st.current_phase:
            st.current_phase = target
            st.turn_count = 1
            st.history.append((target, time.time()))
            st.last_updated = time.time()
            _NSFW_PHASE_BY_SCOPE[key] = st
        return st, 1
    # 无信号 + 当前 P8 → 累加 p8_idle_count (自然平复)
    if st.current_phase >= 8 and signal == 0:
        st.p8_idle_count += 1
        st.last_updated = time.time()
        _NSFW_PHASE_BY_SCOPE[key] = st
    return st, 0


def update_location(scope: str, user_id: str, user_text: str, reply_text: str = "") -> str:
    """检测 user msg / reply 中的 location 锚点, 更新 state.location.

    优先级: user_text 命中 > reply_text 命中 > 现有 state.location 保持.
    返回当前 location key (没找到返回 '').
    """
    key = _state_key(scope, user_id)
    st = _NSFW_PHASE_BY_SCOPE.get(key)
    # user msg 优先 (主动指定场景)
    new_loc = detect_location_from_text(user_text)
    if not new_loc and reply_text:
        new_loc = detect_location_from_text(reply_text)
    if not new_loc:
        return st.location if st else ""
    # 持久化
    if st is None:
        _gc_old_states()
        st = PhaseState(last_updated=time.time(), location=new_loc,
                        location_ambient=LOCATION_PRESETS[new_loc]["ambient"])
        _NSFW_PHASE_BY_SCOPE[key] = st
    else:
        st.location = new_loc
        st.location_ambient = LOCATION_PRESETS[new_loc]["ambient"]
        st.last_updated = time.time()
    return new_loc


def lock_trope(scope: str, user_id: str, trope: str, scene: str) -> None:
    """援交场景下首轮抽中的 trope sticky 内不 reroll (cache 友好).

    主人 2026-05-27 原话『不要一直变不能 hit cache』 — random 每轮换 trope 会破坏
    spark route 的 prompt cache. 第一次抽中后 sticky_seconds 内锁定.
    """
    key = _state_key(scope, user_id)
    st = _NSFW_PHASE_BY_SCOPE.get(key)
    if st is None:
        _gc_old_states()
        st = PhaseState(last_updated=time.time(), locked_trope=trope, locked_trope_scene=scene)
        _NSFW_PHASE_BY_SCOPE[key] = st
    else:
        st.locked_trope = trope
        st.locked_trope_scene = scene
        st.last_updated = time.time()


def get_locked_trope(scope: str, user_id: str) -> tuple[str, str]:
    """返回 sticky 内已锁定的 trope, 没锁返回 ('', '')."""
    st = get_phase_state(scope, user_id)
    return st.locked_trope, st.locked_trope_scene


def reset_phase(scope: str, user_id: str) -> None:
    """场景结束 / 主动退 (closing intent / sticky exit) → reset to P1."""
    _NSFW_PHASE_BY_SCOPE.pop(_state_key(scope, user_id), None)


def _rotate_subset(pool: tuple, k: int, rotation: int) -> list:
    """从 pool 取 k 个, 按 rotation 偏移使每轮抽不同子集. deterministic but cycling.

    主人 2026-05-27 升级 #1: 每轮轮换 physical / behavior / opener 子集,
    AI 不再每轮看同样的 hint metadata → 减少 parrot 复读.
    """
    if not pool:
        return []
    if k >= len(pool):
        return list(pool)
    n = len(pool)
    start = rotation % n
    out = []
    for i in range(k):
        out.append(pool[(start + i) % n])
    return out


def build_phase_advance_hint(scope: str, user_id: str) -> str:
    """根据当前 phase state 构造下一轮 spark prompt 注入 hint.

    核心规则:
    - 当前 phase = N, 已持续 turn_count 轮
    - 本轮 MUST 推进到 P{N+1} (除非 N >= 8)
    - 如果 turn_count >= per-phase stuck threshold → 强制跳 phase
    - 注入下一 phase 的完整提示 (生理/思维/行为/opener_hints) — 每轮轮换不同子集
    - 注入当前 location ambient (跨轮持久化, 不每轮重抽场景)
    - 注入最近 3 条 reply opener (反复读 hint)
    """
    st = get_phase_state(scope, user_id)
    current = st.current_phase
    next_phase = min(8, current + 1)
    # 主人 2026-05-27 升级 #3: per-phase stuck threshold (P6=1 / P3-P5=2 / P8=99)
    stuck_thr = _PHASE_STUCK_THRESHOLDS.get(current, 3)
    stuck = st.turn_count >= stuck_thr

    current_meta = PHASE_DEFINITIONS.get(current, PHASE_DEFINITIONS[1])
    next_meta = PHASE_DEFINITIONS.get(next_phase, PHASE_DEFINITIONS[8])

    # 主人 2026-05-27 升级 #1: 每轮轮换 metadata 子集, 让 hint 永远新鲜
    rotation = st.last_hint_rotation
    rotated_physical = _rotate_subset(next_meta['physical'], 5, rotation)
    rotated_behavior = _rotate_subset(next_meta['behavior'], 4, rotation)
    rotated_opener = _rotate_subset(next_meta['opener_hints'], 2, rotation)
    rotated_thought = _rotate_subset(next_meta['thought'], 2, rotation)
    # 也写回 state, 下次自动 +1 → 下一轮抽不同子集
    key = _state_key(scope, user_id)
    real_st = _NSFW_PHASE_BY_SCOPE.get(key)
    if real_st is not None:
        real_st.last_hint_rotation = (rotation + 1) % 7  # 7 step 循环避免每 3 轮回到原位

    # location ambient (持久化场景锚点 - 主人 2026-05-27 第 4 项)
    location_line = ""
    if st.location and st.location in LOCATION_PRESETS:
        loc_meta = LOCATION_PRESETS[st.location]
        location_line = (
            f"【★ 场景锚点 (sticky, 跨轮持久 - 严禁换场景)】\n"
            f"当前场景 = {loc_meta['name']}: {loc_meta['ambient']}\n"
            f"本轮 reply 必须在这个场景内继续, 不要切到新场景 (床/桌/沙发等之间不许跳).\n"
            f"\n"
        )

    # 主人 2026-05-27 升级 #2: opener 反复读 hint
    opener_blocklist_line = ""
    if st.recent_openers:
        recent_str = " | ".join(f"『{o}』" for o in st.recent_openers[-3:])
        opener_blocklist_line = (
            f"【★ Opener 反复读 (最近 {len(st.recent_openers[-3:])} 条 reply 开头)】\n"
            f"{recent_str}\n"
            f"本轮 **首句开头 10 字必须和上面任何一条都不一样** — "
            f"换动作进入 / 换感官进入 / 换台词进入 / 换沉默进入, 别复读上轮模板.\n"
            f"\n"
        )

    # 主人 2026-05-27 三轮升级『余韵后还能再次被操高潮』
    # arc_count > 1 → 身体记忆 + 更敏感 + 累 (人类正常性交 multi-round 状态)
    arc_line = ""
    if st.arc_count > 1:
        sore_line = (
            "刚结束 1 轮 — 蜜穴还湿润收紧, 大腿还在余震发软, 体温还烫"
            if st.arc_count == 2 else
            f"已经经历 {st.arc_count - 1} 轮高潮 — 身体已疲惫, 鼻翼一直在颤, 头发汗湿贴脸, 嗓子哑了"
        )
        mental = (
            "又被推 — '笨蛋…才刚刚结束身体还酸着喵…' + 嘴硬但已经主动迎合"
            if st.arc_count == 2 else
            f"第 {st.arc_count} 轮 — 已经被弄到失神, 嘴上抗议但身体已经条件反射地配合, 思维只剩本能"
        )
        speed_hint = (
            "比第一轮**快 1-2 个 phase 进入状态** (例如本轮 P3 沉沦动作可以直接演到接近 P4 的临界感)"
            if st.arc_count == 2 else
            f"第 {st.arc_count} 轮 — **每 phase 只演 1 轮就该推**, 身体已经准备好直接到 P5-P6"
        )
        arc_line = (
            f"【★ Arc Counter (第 {st.arc_count} 轮 arc · 余韵后再推)】\n"
            f"身体状态: {sore_line}.\n"
            f"心理状态: {mental}.\n"
            f"节奏: {speed_hint}.\n"
            f"细节铁律: 这一轮 reply 必须 acknowledge **上一轮余韵还没散** "
            f"(『蜜穴还在小幅余震 / 大腿还酸 / 嗓子还哑 / 上一发还留在里面…』之类).\n"
            f"\n"
        )

    if current >= 8:
        # P8 余韵 — 维持但仍轮换 behavior / thought (避免连续 P8 复读)
        rot_p8_behavior = _rotate_subset(PHASE_DEFINITIONS[8]['behavior'], 4, rotation)
        rot_p8_thought = _rotate_subset(PHASE_DEFINITIONS[8]['thought'], 1, rotation)
        # P8 idle 提示: 若 p8_idle_count >= 3 → 余韵已自然平复, AI 可以慢慢淡出
        p8_idle_line = ""
        if st.p8_idle_count >= 3:
            p8_idle_line = (
                f"⚠️ P8 余韵已 idle {st.p8_idle_count} 轮 — 已自然平复, "
                f"如 user 没有主动推 NSFW, 笨猫继续偎着但不主动撩 (退到日常贴贴模式).\n"
                f"如 user 重新推 NSFW (再深 / 又硬 / 再来 等) → 自动开第 {st.arc_count + 1} 轮 arc.\n"
            )
        return (
            arc_line
            + location_line
            + opener_blocklist_line
            + "【★ Phase Tracker (本地计算)】\n"
            + f"当前 phase = P8 余韵 (持续 {st.turn_count} 轮, idle {st.p8_idle_count}).\n"
            + p8_idle_line
            + "维持 P8 风味直到 user 转场 / 入睡 / 起身 / 离开. 不再推进新 phase.\n"
            + f"P8 提示动作 (本轮轮换): {' / '.join(rot_p8_behavior)}\n"
            + f"P8 思维独白 (本轮): {rot_p8_thought[0] if rot_p8_thought else ''}\n"
        )

    advance_rule = (
        f"本轮 **必须推进到 {next_meta['name']}**" if not stuck else
        f"⚠️ 已在 {current_meta['name']} 卡 {st.turn_count} 轮 (阈值 {stuck_thr}) — **强制推进到 {next_meta['name']}**"
    )

    return (
        arc_line
        + location_line
        + opener_blocklist_line
        + "【★ Phase Tracker (本地状态机, 不是 AI 自判)】\n"
        + f"当前 phase = {current_meta['name']} (持续 {st.turn_count}/{stuck_thr} 轮, arc #{st.arc_count}).\n"
        + f"{advance_rule}, 严禁原地踏步.\n"
        + "\n"
        + f"━━ {next_meta['name']} 演出要素 (本轮轮换 #{rotation}, reply 必须涵盖 ≥2 条) ━━\n"
        + f"【summary】{next_meta['summary']}\n"
        + f"【生理特征】{' / '.join(rotated_physical)}\n"
        + f"【内心独白模板】{' ; '.join(rotated_thought)}\n"
        + f"【行为表征】{' / '.join(rotated_behavior)}\n"
        + f"【可选起手句式】{' | '.join(rotated_opener)}\n"
        + f"【推进信号】{next_meta['advance_signal']}\n"
        + "\n"
        + "**铁律**:\n"
        + f"- 这一条 reply **不能写成 {current_meta['name']} 风** (那是上一轮已经做过的)\n"
        + f"- 必须演出 {next_meta['name']} 的生理 + 思维 + 行为 至少 2 个维度\n"
        + "- 不要 meta 说『进入下一阶段』『phase X』- 用动作 / 喘息 / 内心独白自然演出\n"
    )


def record_reply_opener(scope: str, user_id: str, reply: str) -> None:
    """记录 reply 的首段 opener (前 20 字, strip ASCII/whitespace), 推进 last-N 队列.

    主人 2026-05-27 升级 #2: 给 build_phase_advance_hint 反复读 hint 用.
    """
    if not reply:
        return
    # 取前 30 字, 跳过开头的空白 + 标点
    snippet = reply.strip()[:30]
    if not snippet:
        return
    key = _state_key(scope, user_id)
    st = _NSFW_PHASE_BY_SCOPE.get(key)
    if st is None:
        _gc_old_states()
        st = PhaseState(last_updated=time.time(), recent_openers=[snippet])
        _NSFW_PHASE_BY_SCOPE[key] = st
        return
    st.recent_openers.append(snippet)
    if len(st.recent_openers) > _RECENT_OPENER_CAP:
        st.recent_openers = st.recent_openers[-_RECENT_OPENER_CAP:]
    st.last_updated = time.time()


def stats_summary() -> dict[str, Any]:
    """debug 用: 返回所有活跃 (scope, user) 的 phase state 摘要."""
    return {
        "active_scopes": len(_NSFW_PHASE_BY_SCOPE),
        "by_scope": {
            key: {
                "phase": st.current_phase,
                "turn_count": st.turn_count,
                "age_seconds": int(time.time() - st.last_updated),
                "location": st.location,
                "locked_trope": st.locked_trope,
                "history": st.history[-5:],
            }
            for key, st in _NSFW_PHASE_BY_SCOPE.items()
        },
    }


__all__ = [
    "LOCATION_PRESETS",
    "PHASE_DEFINITIONS",
    "PhaseState",
    "analyze_user_push_signal",
    "apply_user_signal",
    "build_phase_advance_hint",
    "detect_location_from_text",
    "detect_phase_from_reply",
    "detect_phase_with_confidence",
    "get_locked_trope",
    "get_phase_state",
    "lock_trope",
    "record_reply_opener",
    "reset_phase",
    "stats_summary",
    "update_location",
    "update_phase",
]
