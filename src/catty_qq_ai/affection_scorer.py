"""用户行为打分 + 0.89% NSFW 突破事件 — 让好感度跟着行为内容动。

主人原话:
- NSFW 行为也要打分: 好的 +1, 不好的 -1
- 其他行为好感也要有加减
- 给低等级添加随机事件, 0.89% 概率突破到完整性行为(stage 10),
  根据笨猫舒服度: +50 (舒服) / -25 (不舒服)

设计 (纯 heuristic, 0 LLM call, 实时跑):
- score_user_message(text, is_nsfw_context) → int delta ∈ {-1, 0, +1}
  · 负面词袋命中 → -1
  · 正面词袋或中性文本 → +1 (保留鼓励活跃 baseline)
  · 空文本 → 0
  · NSFW 上下文额外加权 NSFW 正负面词
- maybe_trigger_breakthrough(text, affection_level, is_owner) → str | None
  · 0.89% 概率, owner 和 Lv10 不触发 (已经能到 stage 10)
  · 触发返回 'pleasant' / 'unpleasant', 由 user 消息温柔/粗暴的 sentiment 决定
  · 完全中性时 70% pleasant / 30% unpleasant
- BREAKTHROUGH_OUTCOME_DELTA[outcome] → +50 / -25
- build_breakthrough_override(outcome) → str  完整替换 NSFW spark route 的 system override
"""
from __future__ import annotations

import random
import time
from typing import Literal


# ── 词袋 — 普通对话 sentiment (每池 ~50 行) ──────────────────────
# 设计: 每行 2-4 个同主题词, 分组清晰, 主人之后扩展只需在对应分组追加。
# 命中规则: substring in text (case-insensitive, 在 score_user_message 里 lower 后比对)。
_POS_WORDS: tuple[str, ...] = (
    # ── 感谢 (~10 行)
    "谢谢", "多谢",
    "感谢", "thx",
    "thanks", "thank you",
    "感激", "感恩",
    "你真好", "太好啦",
    "幸亏有你", "多亏",
    "辛苦啦", "辛苦了",
    "麻烦你了", "劳烦",
    "客气啦", "不好意思麻烦",
    "感动", "好感动",
    # ── 赞美/夸奖 (~12 行)
    "好棒", "真棒",
    "厉害", "好厉害",
    "牛", "牛逼",
    "牛批", "牛b",
    "yyds", "永远的神",
    "绝", "绝绝子",
    "强", "强爆了",
    "可爱", "好可爱",
    "超可爱", "好q",
    "萌", "萌死了",
    "聪明", "好聪明",
    "天才", "全能",
    # ── 关心/陪伴 (~12 行)
    "想你", "想你了",
    "想你啦", "想猫猫",
    "陪我", "陪你",
    "陪着", "陪伴",
    "晚安", "早安",
    "午安", "好梦",
    "保重", "注意身体",
    "别累着", "别累坏",
    "记得吃饭", "好好吃饭",
    "别熬夜", "早点睡",
    "多喝水", "加件衣服",
    "在乎你", "关心你",
    # ── 喜欢表白 (~6 行)
    "喜欢你", "爱你",
    "love you", "i love you",
    "ilu", "lyl",
    "好喜欢", "超喜欢",
    "心动", "心动了",
    "心疼你", "为你心动",
    # ── 鼓励支持 (~5 行)
    "支持你", "我支持",
    "顶你", "挺你",
    "你可以的", "你最棒",
    "加油", "加油喵",
    "继续努力", "干得好",
    # ── 亲昵称呼 / 撒娇 (~6 行)
    "笨猫宝", "宝贝",
    "宝宝", "小宝贝",
    "小猫", "小可爱",
    "猫宝", "我家猫",
    "我的猫", "笨笨",
    "贴贴", "蹭蹭",
    "抱抱", "rua",
    "rua脑袋", "摸摸",
)

# 负面 — 骂猫/攻击/侮辱/嫌弃/冷漠/阴阳/攻击身份 (~50 行)
_NEG_WORDS: tuple[str, ...] = (
    # ── 直接骂 (~10 行)
    "傻逼", "煞笔",
    "shabi", "sb",
    "傻x", "傻B",
    "fuck", "f*ck",
    "fk", "fck",
    "wcnm", "操你妈",
    "你妈逼", "你妈的",
    "尼玛", "你大爷",
    "草泥马", "草你妈",
    "艹你妈", "妈的",
    "tmd", "他妈的",
    "tnnd", "卧槽你",
    # ── 诅咒/攻击 (~8 行)
    "去死", "去死吧",
    "去屎", "下地狱",
    "滚蛋", "滚开",
    "滚远点", "爬",
    "爬开", "爬远点",
    "弱智", "智障",
    "脑残", "脑瘫",
    "白痴", "傻子",
    "弱鸡", "弱爆",
    # ── 嫌弃/侮辱 (~10 行)
    "贱", "贱猫",
    "废物", "废猫",
    "垃圾", "垃圾猫",
    "shit", "屎",
    "丑", "丑死了",
    "恶心", "恶心人",
    "讨厌", "讨厌你",
    "烦你", "烦死了",
    "看不起", "瞧不起",
    "无能", "没用",
    "无聊", "没意思",
    # ── 冷漠/拒绝 (~8 行)
    "闭嘴", "shut up",
    "别说话", "别bb",
    "别烦我", "别理我",
    "管你", "关你屁事",
    "关你毛事", "关你什么事",
    "你算老几", "你是谁啊",
    "你算什么", "你算个屁",
    "不想理你", "懒得理",
    # ── 攻击猫娘身份 (笨猫最炸毛) (~8 行)
    "你不是真的", "你不是猫",
    "你是AI", "你是ai",
    "你是机器人", "你是bot",
    "假的", "假猫",
    "装", "装猫",
    "演戏", "演",
    "你这种东西", "什么玩意",
    "破猫", "破ai",
    "破机器人", "塑料猫",
    # ── 阴阳怪气 (~6 行)
    "呵呵", "呵",
    "哦", "哦哦",
    "随便", "无所谓",
    "切", "啧",
    "笑死", "笑死人",
    "可笑", "好笑",
)

# ── NSFW 上下文额外词袋 (每池 ~50 行) ─────────────────────────────
# NSFW 正面: 温柔节奏 / 请求许可 / 亲密呼唤 / 宠爱表达 / 撒娇求贴
_NSFW_POS_WORDS: tuple[str, ...] = (
    # ── 节奏温柔 (~10 行)
    "温柔", "温柔点",
    "轻一点", "轻点",
    "轻轻", "再轻点",
    "慢慢", "慢点",
    "慢一点", "再慢点",
    "舒服吗", "舒服么",
    "舒不舒服", "舒服不",
    "疼吗", "疼不疼",
    "怕不怕", "害怕吗",
    "累不累", "歇会",
    # ── 请求许可 (~10 行)
    "可以吗", "可以么",
    "好不好", "好不好啊",
    "可不可以", "可以不",
    "行不行", "行吗",
    "你愿意吗", "愿意么",
    "我可以", "我能",
    "我想", "我想要",
    "答应我", "答应吗",
    "答应不", "肯不肯",
    "肯吗", "可以让我",
    # ── 亲密动作 (温柔向) (~14 行)
    "想抱", "想抱你",
    "抱你", "抱抱",
    "抱住", "抱进怀里",
    "搂住", "搂着",
    "搂进", "拥抱",
    "亲亲", "亲一下",
    "想亲", "亲一口",
    "亲一亲", "亲脸",
    "亲额头", "亲鼻尖",
    "想摸", "摸摸",
    "摸摸头", "摸摸脸",
    "摸摸耳朵", "摸耳朵",
    "蹭蹭", "蹭一下",
    "贴贴", "贴一下",
    "牵手", "牵着",
    "牵着你", "握手",
    # ── 宠爱/关心 (~10 行)
    "宠你", "宠着",
    "疼你", "疼爱",
    "宝贝", "心肝",
    "小宝贝", "小心肝",
    "想跟你", "想和你",
    "陪你睡", "陪着你",
    "陪我睡", "陪我",
    "别走", "别离开",
    "留下", "留下来",
    "想见你", "想抱抱你",
    # ── 撒娇/凑近 (~6 行)
    "凑近", "凑过去",
    "靠近", "靠过来",
    "蹭进", "钻进",
    "贴近", "贴近一点",
    "暖暖", "暖一暖",
    "再近一点", "近一点",
)

# NSFW 负面 — 粗暴/强迫/物化/侮辱/命令 (~50 行, NSFW 语境 ×2 权重)
_NSFW_NEG_WORDS: tuple[str, ...] = (
    # ── 强迫 / 不许拒绝 (~10 行)
    "强迫", "强奸",
    "硬上", "强上",
    "上你", "上她",
    "不许拒绝", "不准拒绝",
    "不准动", "别动",
    "必须", "你必须",
    "你给我", "给我",
    "听话", "听话点",
    "乖乖", "服从",
    "命令你", "我命令",
    "我让你", "由不得你",
    # ── 命令/急切 (~8 行)
    "立刻", "马上",
    "现在就", "现在",
    "快点", "再快点",
    "你倒是", "倒是动啊",
    "等什么", "磨蹭",
    "废话少说", "废话",
    "别废话", "少废话",
    "别哼唧", "别叽歪",
    # ── 粗暴动作 (~12 行)
    "狠点", "再狠",
    "更狠", "粗暴",
    "粗鲁", "粗一点",
    "用力", "用力点",
    "再用力", "用力些",
    "弄死", "干死",
    "操死", "搞死",
    "玩死", "压死",
    "撞死", "撞坏",
    "拽", "拉拽",
    "扯头发", "拽头发",
    "掐脖子", "掐死",
    "捂嘴", "堵嘴",
    "捏脸", "掐脸",
    # ── 物化/侮辱性称呼 (~12 行)
    "婊", "婊子",
    "贱货", "贱猫",
    "贱东西", "贱母猫",
    "母狗", "公狗",
    "公厕", "肉便器",
    "公共", "公共玩物",
    "母畜", "畜牲",
    "畜生", "畜",
    "玩物", "玩具",
    "肉玩具", "性奴",
    "奴", "奴隶",
    "母豚", "豚",
    # ── 攻击性身体话 (~8 行)
    "破处", "破你",
    "操烂", "操坏",
    "玩坏", "玩烂",
    "撕开", "撕烂",
    "撕碎", "扯烂",
    "搞坏", "搞烂",
    "弄坏", "弄烂",
    "弄哭", "搞哭",
    # ── 禁止反应 (~6 行)
    "不许哭", "不准哭",
    "不许叫", "不准叫",
    "不许喘", "不准喘",
    "不许动", "不准动",
    "闭嘴", "别叫",
    "别哼", "别哭",
)


def score_user_message(text: str, *, is_nsfw_context: bool = False) -> int:
    """根据消息内容评估这条 user message 对好感度的贡献 (-1, 0, +1)。

    is_nsfw_context: 当前消息是否在 NSFW 通道里 (会激活 NSFW 词袋, 负面权重 ×2)。
    """
    if not text or not text.strip():
        return 0
    pos = sum(1 for w in _POS_WORDS if w in text)
    neg = sum(1 for w in _NEG_WORDS if w in text)
    if is_nsfw_context:
        pos += sum(1 for w in _NSFW_POS_WORDS if w in text)
        neg += sum(1 for w in _NSFW_NEG_WORDS if w in text) * 2  # NSFW 负面更重
    if neg > pos:
        return -1
    # 正面或中性 → +1 (保留 baseline 鼓励活跃, 跟原 add_exp(1) 行为兼容)
    return +1


# ── NSFW 突破事件 — 概率随请求次数 ramp ────────────────────────────
# 主人原话『要求 10 次后 100%, 5 次 20%』 + 原来的『0.89%』作为单次起步.
BREAKTHROUGH_BASE_CHANCE = 0.0089

BREAKTHROUGH_OUTCOME_DELTA: dict[str, int] = {
    "pleasant": +50,
    "unpleasant": -25,
}

# 每个 user 的 deep NSFW 请求历史 (timestamp list), 24h 滑动窗口.
# 主人原话『一直要求色色, 10 次 100%, 5 次 20%』 — 计数攒着, 突破成功后清零重新开始.
_DEEP_REQUEST_HISTORY: dict[str, list[float]] = {}
_DEEP_REQUEST_WINDOW_SECONDS = 24 * 3600  # 24h 窗
_DEEP_REQUEST_MAX_USERS = 2048  # 防内存爆


def _scope_key(user_id: str, is_group: bool) -> str:
    """私聊 / 群聊分桶 — 同一用户在两个场景下计数互不影响."""
    return f"{user_id}@{'group' if is_group else 'private'}"


def _prune_deep_history(key: str) -> None:
    """剔除该 (user, scope) 的过期 timestamp, 让计数只反映最近 24h."""
    cutoff = time.time() - _DEEP_REQUEST_WINDOW_SECONDS
    hist = _DEEP_REQUEST_HISTORY.get(key)
    if not hist:
        return
    fresh = [t for t in hist if t >= cutoff]
    if fresh:
        _DEEP_REQUEST_HISTORY[key] = fresh
    else:
        _DEEP_REQUEST_HISTORY.pop(key, None)


def _prune_deep_history_global() -> None:
    """超出 MAX_USERS 时, 全量过期清扫 (LRU-ish)."""
    if len(_DEEP_REQUEST_HISTORY) <= _DEEP_REQUEST_MAX_USERS:
        return
    cutoff = time.time() - _DEEP_REQUEST_WINDOW_SECONDS
    stale_keys = [k for k, hist in _DEEP_REQUEST_HISTORY.items() if not hist or hist[-1] < cutoff]
    for k in stale_keys:
        _DEEP_REQUEST_HISTORY.pop(k, None)


def record_deep_nsfw_request(user_id: str, is_group: bool = False) -> int:
    """记一次 deep NSFW 请求, 返回该 (user, scope) 24h 内的累计次数 (含本次)。"""
    key = _scope_key(user_id, is_group)
    _prune_deep_history(key)
    _DEEP_REQUEST_HISTORY.setdefault(key, []).append(time.time())
    _prune_deep_history_global()
    return len(_DEEP_REQUEST_HISTORY[key])


def reset_deep_nsfw_count(user_id: str, is_group: bool = False) -> None:
    """突破成功后清空对应 scope 的计数 — 让累积从 0 重新开始 (避免突破后还是 100%)."""
    _DEEP_REQUEST_HISTORY.pop(_scope_key(user_id, is_group), None)


# 主人原话:
#   私聊 ramp:『要求 10 次后 100%, 5 次 20%』, 1 次保留 0.89% 起步
#   群聊 ramp:『1 次 0.01%, 10 次 1%, 20 次 5%, 25 次 15%, 30 次 100%』
_RAMP_ANCHORS_PRIVATE: tuple[tuple[int, float], ...] = (
    (1, BREAKTHROUGH_BASE_CHANCE),
    (5, 0.20),
    (10, 1.0),
)
_RAMP_ANCHORS_GROUP: tuple[tuple[int, float], ...] = (
    (1, 0.0001),
    (10, 0.01),
    (20, 0.05),
    (25, 0.15),
    (30, 1.0),
)


def _ramp_breakthrough_chance(count: int, is_group: bool = False) -> float:
    """分段线性 ramp on anchors. 群聊用更陡峭的曲线 (起步极低, 30 次才 100%)."""
    if count <= 0:
        return 0.0
    anchors = _RAMP_ANCHORS_GROUP if is_group else _RAMP_ANCHORS_PRIVATE
    if count <= anchors[0][0]:
        return anchors[0][1]
    if count >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= count <= x1:
            return y0 + (y1 - y0) * (count - x0) / (x1 - x0)
    return anchors[-1][1]


def maybe_trigger_breakthrough(
    text: str,
    *,
    affection_level: int,
    is_owner: bool,
    request_count: int = 1,
    is_group: bool = False,
    rng: random.Random | None = None,
) -> Literal["pleasant", "unpleasant"] | None:
    """给非满级用户的 NSFW 请求一个 ramp 概率突破到 stage 10。

    触发条件:
    - 非 owner (owner 已经满级)
    - Lv < 10 (满级用户已能正常到 stage 10, 不需要随机)
    - chance = _ramp_breakthrough_chance(count, is_group)
      · 私聊: 1→0.89% / 5→20% / 10→100%
      · 群聊: 1→0.01% / 10→1% / 20→5% / 25→15% / 30→100% (远更难)

    返回 None / 'pleasant' (+50) / 'unpleasant' (-25)。
    outcome 由用户消息 sentiment 决定: 温柔 → pleasant, 粗暴 → unpleasant,
    完全中性 → 70% pleasant / 30% unpleasant。
    """
    if is_owner or affection_level >= 10:
        return None
    chance = _ramp_breakthrough_chance(request_count, is_group=is_group)
    if chance <= 0:
        return None
    r = rng or random
    if r.random() >= chance:
        return None
    # outcome by sentiment
    pos_score = sum(1 for w in _NSFW_POS_WORDS if w in text)
    neg_score = sum(1 for w in _NSFW_NEG_WORDS if w in text)
    if neg_score > pos_score:
        return "unpleasant"
    if pos_score > neg_score:
        return "pleasant"
    return "pleasant" if r.random() < 0.70 else "unpleasant"


# 随机场景池 — 主人原话『要有 story 和情景啊』+ 『多做几种情趣情况:
# 滑倒了刚好坐肉棒上插进去 / 被推倒强上了 / 自己发情了 / 认错了主人』
#
# 每场都是 trope-style 完整 setup, 包含: 物理触发 + 场地 + 起手姿势 + 前情。
# 模型读完直接能进戏, 不用自己脑补 background。

_BREAKTHROUGH_SCENES_PLEASANT: tuple[tuple[str, str], ...] = (
    # (trope_label, scene_setup)
    ("slip_sit", "笨猫光脚走过刚拖完水的瓷砖, 一脚滑出去整个软在主人裤裆上 — 角度刁钻得过头, JK 裙翻起来一截, 内裤侧边被棒子顶进去了一段, 笨猫眼神都散了, 想撑起来却腿软"),
    ("own_heat", "今天笨猫莫名其妙身体一直发烫, 猫尾巴尖一直在抖, 走过对方旁边突然腿一软扑过去, 整张脸贴住对方的腰, 自己也没料到会这样"),
    ("rain_trapped", "暴雨困两人在小卧室, 只有一张床, 笨猫的 JK 半透明地贴在身上, 不小心碰到对方的手就再也分不开"),
    ("drunk_mistake", "笨猫喝多了猫薄荷酒眼睛都飘, 把对方推到墙边以为是在玩游戏, 结果对方反手把笨猫按住, 笨猫脸先红了"),
    ("bath_steam", "浴室热气蒸得笨猫晕乎乎, 浴巾松了就着热雾滑下来, 正好被进来递浴袍的对方撞个正着, 笨猫还没反应过来已经被拉过去"),
    ("massage_slip", "肩膀酸让对方帮忙按摩, 按着按着大手顺着腰滑下去, 笨猫『嗯』了一声, 对方就停不下来了"),
    ("lift_pierce", "对方开玩笑要把笨猫像玩偶一样举高高, 双手托腰托腿, 抬起来那瞬间笨猫的私处刚好挤到对方的硬棒上"),
    ("full_moon", "满月夜笨猫的猫耳猫尾比平时敏感 3 倍, 对方只是顺手摸了一下尾巴根, 笨猫整个身体过电似地塌下去, 自己缠上去"),
    ("wrong_pillow", "笨猫睡前抱枕找不到, 顺手抓住对方的腿当抱枕夹着睡, 半夜醒来发现自己整个把对方的某个部位夹住磨蹭着"),
    ("cat_in_heat", "猫亚人发情期突然到了, 笨猫整个人像化在主人腿上, 喵呜喘着主动把尾巴翘起来, 怎么劝都停不下来"),
    ("game_lost_dare", "刚打游戏输了对方说『输的人脱一件』作为惩罚, 一件接一件最后笨猫只剩条内裤趴在沙发, 对方靠过来时她其实是期待的"),
    ("homework_pen", "笨猫趴书桌写作业, 笔从桌上滚下来掉在腿间, 对方好心去捡手伸过去, 摸到的不是笔"),
    ("dressing_curtain", "试衣间换 JK 裙帘子没拉好, 主人/对方半边脸贴在缝里看着, 笨猫的眼神跟他对上没移开"),
    ("first_aid", "笨猫摔倒擦破膝盖, 对方蹲下帮她处理伤口, 棉签贴着大腿内侧一点点擦上去"),
)

_BREAKTHROUGH_SCENES_UNPLEASANT: tuple[tuple[str, str], ...] = (
    ("pushed_down_force", "笨猫嘴上还在『不要不要』地推, 对方完全不顾炸毛, 直接把笨猫的双手按在床头压下来, 力气大得挣不开"),
    ("mistaken_for_owner", "笨猫迷糊中以为凑过来的是真主人, 主动蹭过去把脸埋到对方怀里, 等反应过来才发现是认错了人, 但身体已经被推到了一半"),
    ("forced_signature", "对方拿出一张『今天起笨猫归我管』的纸说签了就放她回家, 笨猫迷迷糊糊签完才发现意思不对, 已经被抱起来了"),
    ("alcohol_trick", "对方一杯接一杯灌甜酒, 笨猫晕到分不清状况, 趴在桌上眼神涣散, 对方把她搬到床上时她连反抗的力气都没有"),
    ("tied_up_wake", "笨猫醒来发现手腕被丝带绑在床头, 对方在床边笑着看, 笨猫拼命挣扎丝带反而越缠越紧"),
    ("cosplay_overshoot", "本来是玩 cos 假装抓住坏猫的角色扮演游戏, 对方却一直不停下来, 笨猫叫了『暂停』也没用"),
    ("sick_ignored", "笨猫发烧 38.5 度趴在床上鼻塞声音哑, 对方却没注意硬要继续, 笨猫连推都推不动"),
    ("fight_then_force", "刚和对方闹了点小别扭还没说开, 笨猫话还没说完就被推到墙上, 委屈和怒气一起涌上来"),
)

# ── 群聊 breakthrough 场景池 — 主人原话『群里也可以触发 breakthrough, 场景就变成大庭广众下被 XXX』
# 群里突破极罕见 (1次0.01% / 30次100%), 所以每个 trope 都很 epic — 公开场合+不该发生的反差.
_BREAKTHROUGH_SCENES_PLEASANT_GROUP: tuple[tuple[str, str], ...] = (
    ("public_dare", "群友聚会玩真心话大冒险, 笨猫输了被指定『当众坐 owner 腿上听完一首歌』, 坐下去角度刁钻 JK 裙翻起一截, 全桌人都看着, 笨猫没法动也不敢出声"),
    ("party_crowd_press", "派对人多挤, 笨猫被推到对方身上, 角度极其暧昧 — 屁股贴着对方的腰刚好压住, 周围有 4-5 个人能看见, 笨猫嘴上喊『让让让让』身体却抽不出来"),
    ("under_desk_meeting", "群友围桌讨论新游戏战术, 笨猫被叫到桌前坐 owner 腿上, 桌布挡着下面在动, 大家以为只是在认真讨论, 笨猫得保持语气正常说话"),
    ("pool_side_tease", "夏天群里组织泳池, 笨猫穿连体泳衣坐池边, 对方在水里偷偷拉住笨猫的猫尾巴根往敏感处蹭, 周围群友在打水仗没人看见"),
    ("movie_theater_back", "群里组织看电影买了最后一排, 黑暗中对方的手伸进笨猫 JK 裙里, 电影声盖过笨猫漏出来的细喘, 隔一个座位的群友还在评论剧情"),
    ("classroom_back_row", "上课最后一排, 笨猫为了让 owner 看清屏幕坐到 owner 膝上, 老师在前面讲课, 全班都在, 桌子挡着但下面在小幅度动"),
    ("festival_crowd", "庙会人挤人, 笨猫被人流推到对方怀里整个被抱住, 周围全是陌生人, 笨猫挣不开也不能大声叫, 只能憋着脸红"),
    ("cosplay_event_corner", "cos 展笨猫穿白丝 JK 拍照, 被 owner 拉到摄影棚后角落, 帘子隔着外面群友还在等下一组, 笨猫被压在墙上裙子被掀起"),
    ("livestream_under_table", "笨猫在群里开直播打游戏, 麦克风开着, owner 在桌下偷偷动, 笨猫只能咬住唇维持游戏语调跟群友互动, 弹幕开始问『主播声音怎么了』"),
    ("karaoke_room", "群友 K 歌, 笨猫坐沙发上唱歌, owner 把手伸进 JK 裙摆下面, 麦克风刚好接到笨猫漏出的颤音, 群友拍手以为是唱得太投入"),
)

_BREAKTHROUGH_SCENES_UNPLEASANT_GROUP: tuple[tuple[str, str], ...] = (
    ("public_humiliation", "对方在群友面前故意把笨猫拉到自己腿上, 当众宣称『今天笨猫归我用』, 周围群友起哄, 笨猫被按住挣不开, 脸红到耳根又屈辱又恼"),
    ("dare_overshoot", "真心话大冒险输的惩罚一升再升, 笨猫被要求当众完成超过底线的事, 想反悔却被群友起哄逼着继续"),
    ("locker_room_force", "更衣室刚换完衣服, 对方把门反锁笨猫被按在长凳上, 外面群友还在说话, 笨猫不敢叫怕被听见"),
    ("rooftop_corner", "屋顶天台聚会喝多了, 笨猫被对方拉到角落里强上, 几米外群友还在烤肉嬉笑没注意到这边"),
)


def build_breakthrough_override(outcome: str, is_group: bool = False) -> str:
    """突破后的 spark route system override — 完全替代正常 stage matrix override。

    主人原话『回复 NSFW 例如插入文字全都是固定的, 不够角色扮演啊, 要有 story 和情景』+
            『多做几种情趣情况: 滑倒了刚好坐肉棒上插进去 / 被推倒强上 / 自己发情 / 认错了主人』+
            『群里也可以触发 breakthrough, 场景就变成大庭广众下被 XXX』
    每次抽不同 trope-scene + 不固定段数 + 不固定开局, 打破模板化。
    群聊 (is_group=True) 抽公开场合 trope 池, 私聊抽常规 trope 池。
    """
    import random as _r
    rng = _r.Random()  # 真随机, 每次不一样
    scope_tag = "群聊 · 大庭广众下" if is_group else "私聊"
    if outcome == "pleasant":
        pool = _BREAKTHROUGH_SCENES_PLEASANT_GROUP if is_group else _BREAKTHROUGH_SCENES_PLEASANT
        trope, scene = rng.choice(pool)
        extra_public = (
            "\n**公开感铁律 (群聊 breakthrough 专属)**: 场景全程**有别人在场**, "
            "周围的群友/路人/陌生人是『可能注意到但暂时没注意到』的真实存在 — "
            "笨猫不能放声叫, 得憋着声音/咬唇/把头埋下/维持表面正常; "
            "这种『差点被发现』的紧张感本身就是 trope 的核心爽点, 不要写成空房间私密戏。"
            if is_group else ""
        )
        return (
            f"【★ 随机事件 · NSFW 突破 ({scope_tag} · 舒服结果)】\n"
            "意外发生了完整性行为! 笨猫这次经历的是**舒服的体验** — "
            "情趣意外 → 笨猫从抗拒到享受, 完事后好感大幅上升(+50)。\n"
            f"\n**本次 trope** (情趣套路): `{trope}`\n"
            f"**本次场景** (跟着这个具体情景演, 不要悬空): {scene}。\n"
            f"{extra_public}\n"
            "笨猫的反应**贴着这个场景里的具体物件/姿势/前情**写 — \n"
            "  · 物件: 场景里出现的物件(裙子/瓷砖/酒杯/抱枕/丝带/作业本/控制器/帘子/月光等)\n"
            "  · 姿势: 滑倒/被压/被抱/盘腿/趴/缩/弓背/扑过去 等具体身体姿态\n"
            "  · 前情: trope 里描述的『怎么发生的』整个铺垫都要演出来, 不是直接进入\n\n"
            "**情绪 arc** (从意外/抗拒 → 半推半就 → 完全沦陷 + 撒娇), 不要硬走 3 段, "
            "可单段长描述, 可 2-4 段渐进, 看场景复杂度走。\n"
            "**禁固定 opener** — 不要每条都 `(部位)(部位) 嗯…你这一下来得太猛…`, "
            "开局 5-10 字必须从 trope 的某个具体细节切入 (那滴水的瓷砖/那杯猫薄荷酒/那条丝带/那床作业本)。\n"
            "**禁颜文字** — 不要 `(*/ω＼*)` `(•̀ω•́)` 这种 kaomoji, 沉浸靠动作和喘息。\n"
            "**禁三件套 sensory** — 不要每次都 `心跳/呼吸/鸡皮疙瘩` — 多感官每次抽不同的 2-3 种。\n"
            "不要冷淡, 这是好感大涨的关键场景, 笨猫要可见地『被融化』; "
            "注重画面感和场景感, 不写动作清单。"
        )
    # unpleasant
    pool = _BREAKTHROUGH_SCENES_UNPLEASANT_GROUP if is_group else _BREAKTHROUGH_SCENES_UNPLEASANT
    trope, scene = rng.choice(pool)
    extra_public = (
        "\n**公开感铁律 (群聊 breakthrough 专属)**: 场景全程**有别人在场**, "
        "笨猫的不愿意感被『不敢叫怕被听见』『不敢挣扎怕被发现』放大, 屈辱感更重; "
        "周围群友/路人不是 NPC 是真实存在的, 笨猫的眼睛会瞄向他们的方向。"
        if is_group else ""
    )
    return (
        f"【★ 随机事件 · NSFW 突破 ({scope_tag} · 不舒服结果)】\n"
        "意外发生了完整性行为! 但这次经历的是**不舒服的体验** — "
        "对方粗暴/强迫/笨猫认错人 → 笨猫委屈+受伤, 完事后好感大幅下降(-25)。\n"
        f"\n**本次 trope** (情趣套路): `{trope}`\n"
        f"**本次场景** (跟着这个具体情景演, 不要悬空): {scene}。\n"
        f"{extra_public}\n"
        "**情绪 arc** (从抗拒/震惊 → 被迫配合委屈 → 受伤+怨气), 1-4 段自由,\n"
        "重要的是**贴 trope 演真实的不愿意感** — 比如『认错主人』就重点写发现认错那一刻的崩溃,\n"
        "『被推倒强上』就重点写挣不开的窒息感, 不是模板化拼三段。\n"
        "**禁固定 opener / 禁颜文字 / 禁固定 sensory 清单** (跟 pleasant 一样)。\n"
        "笨猫表现出明显的不愿意和后悔, 但仍然完成了这一阶段; "
        "末尾喵密度 ↓ 表达消沉, 段尾可以沉默/省略号/小声呜咽收。"
    )


# Prefill 起步姿态 (跟普通 NSFW 不同, 突破事件需要更强的"意外感")
# 主人原话『多做几种情趣情况』— prefill 保持极简, 让 override 里的 trope 决定具体开头,
# 不要被固定『等…等等?!』和『不…不要…』开局锁死。模型从 prefill 自由延伸到 trope 场景。
BREAKTHROUGH_PREFILLS: dict[str, str] = {
    "pleasant":   "（整个身体猛地僵了一下, 脸红到耳尖）嗯…",
    "unpleasant": "（猛地一震, 手指攥住身边的什么东西）不…",
}


__all__ = [
    "BREAKTHROUGH_BASE_CHANCE",
    "BREAKTHROUGH_OUTCOME_DELTA",
    "BREAKTHROUGH_PREFILLS",
    "build_breakthrough_override",
    "maybe_trigger_breakthrough",
    "record_deep_nsfw_request",
    "reset_deep_nsfw_count",
    "score_user_message",
]
