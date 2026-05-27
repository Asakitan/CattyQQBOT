"""NSFW 8 phase 本地状态机 — 不依赖 AI 判断, 反向从 reply 关键词推断当前 phase.

主人 2026-05-27 原话: 『让 NSFW 情景会自动推进, 本地计算 phase, 根据 AI 返回 phase 的情景
来计算 phase, 不要让 ai 判断. 每个情景添加更多的 ai 提示动作等等, 大扩写』.

工作流:
1. user 发 NSFW msg → spark route 调用 build_phase_advance_hint() 注入推进 hint
2. spark reply 拿到 → detect_phase_from_reply(reply) 命中 phase keyword → update_phase()
3. 下一轮 user 发新 msg → build_phase_advance_hint() 读取本地 state → 注入『MUST 推进到 P{N+1}』
4. turn_count > stuck_threshold → 强制跳进下一 phase + log warn

8 phase 完整 metadata:
- P1 触发起手 (惊讶+耳朵躲) / P2 半推半就 (嘴硬身软) / P3 慢慢沉沦 (蜜穴湿润)
- P4 主动迎合 / P5 临界点 / P6 高潮 / P7 overstim / P8 余韵降档
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# ── State per (scope, user) ──────────────────────────────────────────────
@dataclass
class PhaseState:
    """单个 (scope, user) 的 NSFW phase 跟踪状态."""
    current_phase: int = 1
    turn_count: int = 0  # 当前 phase 持续轮数
    last_updated: float = field(default_factory=time.time)
    last_reply_excerpt: str = ""  # 最后一次命中的 reply 片段 (debug)
    history: list[tuple[int, float]] = field(default_factory=list)  # phase 历史轨迹


# Module-level state: key = f"{scope}:{user_id}"
_NSFW_PHASE_BY_SCOPE: dict[str, PhaseState] = {}
_NSFW_PHASE_EXPIRY_SECONDS = 1800  # 30min 无新更新 → 视为新场景 reset
_NSFW_PHASE_STUCK_THRESHOLD = 3  # 同 phase 持续 N 轮 → 下次强制推进


# ── 8 Phase 完整 metadata ────────────────────────────────────────────────
# 每 phase 含:
#   keywords: 检测关键词 (15+, 反向推断 phase)
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


def reset_phase(scope: str, user_id: str) -> None:
    """场景结束 / 主动退 (closing intent / sticky exit) → reset to P1."""
    _NSFW_PHASE_BY_SCOPE.pop(_state_key(scope, user_id), None)


def build_phase_advance_hint(scope: str, user_id: str) -> str:
    """根据当前 phase state 构造下一轮 spark prompt 注入 hint.

    核心规则:
    - 当前 phase = N, 已持续 turn_count 轮
    - 本轮 MUST 推进到 P{N+1} (除非 N >= 8)
    - 如果 turn_count > stuck_threshold → 强制跳 phase
    - 注入下一 phase 的完整提示 (生理/思维/行为/opener_hints)
    """
    st = get_phase_state(scope, user_id)
    current = st.current_phase
    next_phase = min(8, current + 1)
    stuck = st.turn_count >= _NSFW_PHASE_STUCK_THRESHOLD

    current_meta = PHASE_DEFINITIONS.get(current, PHASE_DEFINITIONS[1])
    next_meta = PHASE_DEFINITIONS.get(next_phase, PHASE_DEFINITIONS[8])

    if current >= 8:
        # 已到 P8 余韵, 维持不再推进
        return (
            "\n【★ Phase Tracker (本地计算)】\n"
            f"当前 phase = P8 余韵 (持续 {st.turn_count} 轮).\n"
            "维持 P8 风味直到 user 转场 / 入睡 / 起身 / 离开. 不再推进新 phase.\n"
            f"P8 提示动作: {' / '.join(next_meta['behavior'][:4])}\n"
            f"P8 思维独白: {next_meta['thought'][0]}\n"
        )

    advance_rule = (
        f"本轮 **必须推进到 {next_meta['name']}**" if not stuck else
        f"⚠️ 已在 {current_meta['name']} 卡 {st.turn_count} 轮 — **强制推进到 {next_meta['name']}**"
    )

    return (
        "\n【★ Phase Tracker (本地状态机, 不是 AI 自判)】\n"
        f"当前 phase = {current_meta['name']} (持续 {st.turn_count} 轮).\n"
        f"{advance_rule}, 严禁原地踏步.\n"
        "\n"
        f"━━ {next_meta['name']} 演出要素 (本轮 reply 必须涵盖 ≥2 条) ━━\n"
        f"【summary】{next_meta['summary']}\n"
        f"【生理特征】{' / '.join(next_meta['physical'][:6])}\n"
        f"【内心独白模板】{next_meta['thought'][0]} ; {next_meta['thought'][1]}\n"
        f"【行为表征】{' / '.join(next_meta['behavior'][:4])}\n"
        f"【可选起手句式】{' | '.join(next_meta['opener_hints'])}\n"
        f"【推进信号】{next_meta['advance_signal']}\n"
        "\n"
        "**铁律**:\n"
        f"- 这一条 reply **不能写成 {current_meta['name']} 风** (那是上一轮已经做过的)\n"
        f"- 必须演出 {next_meta['name']} 的生理 + 思维 + 行为 至少 2 个维度\n"
        "- 不要 meta 说『进入下一阶段』『phase X』- 用动作 / 喘息 / 内心独白自然演出\n"
    )


def stats_summary() -> dict[str, Any]:
    """debug 用: 返回所有活跃 (scope, user) 的 phase state 摘要."""
    return {
        "active_scopes": len(_NSFW_PHASE_BY_SCOPE),
        "by_scope": {
            key: {
                "phase": st.current_phase,
                "turn_count": st.turn_count,
                "age_seconds": int(time.time() - st.last_updated),
                "history": st.history[-5:],
            }
            for key, st in _NSFW_PHASE_BY_SCOPE.items()
        },
    }


__all__ = [
    "PHASE_DEFINITIONS",
    "PhaseState",
    "build_phase_advance_hint",
    "detect_phase_from_reply",
    "get_phase_state",
    "reset_phase",
    "stats_summary",
    "update_phase",
]
