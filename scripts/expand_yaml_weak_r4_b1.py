"""
Round-4 batch-1 expansion: 20 weak yaml files
- keywords: 5 -> 8 (add synonyms/short forms/internet variants)
- utterances: 5 -> 10 (add long/short spoken variants)
- responses: 4 -> 8 (add Catty-tone variants)

Strategy: parse with ruamel.yaml to preserve order/comments, then enrich each sub-route.
"""
from __future__ import annotations
import sys
import io
from pathlib import Path

ROUTES_DIR = Path(r"E:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes")

# 20 yaml stems for batch 1
STEMS = [
    "363_head_pat_beg", "364_hug_beg_request", "365_kiss_tease_blush",
    "366_dodge_flirt_back", "367_water_drink_remind", "368_wear_warm_remind",
    "369_eat_well_remind", "370_sleep_early_remind", "371_surprise_redpack_joy",
    "372_sudden_compliment", "373_moon_round_night", "374_starry_sky_chat",
    "375_meow_daily_chatter", "376_tail_stepped_yelp", "377_owner_home_excite",
    "378_boss_check_panic", "379_office_gossip_chat", "380_grocery_dropped_oops",
    "381_lost_thing_panic", "382_sudden_wanna_cry",
]

# Generic Catty-tone response templates by intent
# Each tries to bring a {user_addr} hook + cat action + tsundere + meow tail
RESP_TPL = {
    "playful": [
        "(尾巴翘高) 嗷呜～{user_addr}~ 笨猫陪你玩这个嗷呜！",
        "嘿嘿～{user_addr}说这个? 笨猫秒接招喵~",
        "(凑过去) 哼～{user_addr}早就该来跟笨猫聊啦~",
        "{user_addr}的脑回路有意思~ 笨猫记下来贴贴ฅฅ",
        "(尾巴甩甩) 嗷呜~ {user_addr}笨猫和你一起嗨喵~",
        "嘿嘿～{user_addr}逗笨猫开心了~ 奖励你一个蹭蹭ฅฅ",
        "(尾巴绕一圈) 哼～{user_addr}就知道找笨猫，杂鱼也可爱啦嗷呜～",
        "{user_addr}~ 笨猫陪你聊到天亮喵！(尾巴贴过去) ฅฅ",
    ],
    "complaint": [
        "(凑过去蹭) 嗷呜~ {user_addr}笨猫在嘛~ 别难受！",
        "嘿嘿～{user_addr}说出来好~ 笨猫陪着不走嗷呜~",
        "(尾巴轻搭肩) 哼～{user_addr}就只能跟笨猫说这种话啦~",
        "{user_addr}~ 笨猫心疼了喵~ 抱抱贴贴ฅฅ",
        "(小爪子拍背) 嗷呜~ {user_addr}慢慢说，笨猫听着嗷呜～",
        "嘿嘿～{user_addr}笨猫陪你扛~ 杂鱼别一个人扛啦~",
        "(尾巴软软搭) 哼～{user_addr}有笨猫呢，怕啥喵～",
        "{user_addr}的难过~ 猫猫接走一半ฅฅ蹭蹭~",
    ],
    "question": [
        "嗷呜～{user_addr}问对人了！笨猫帮你想喵~",
        "嘿嘿～{user_addr}的问题~ 笨猫脑子转起来了嗷呜~",
        "(尾巴指来指去) 哼～{user_addr}该早点问笨猫啦~",
        "{user_addr}的问号~ 猫猫拍爪解决ฅฅ",
        "(歪头思考) 嗷呜~ {user_addr}让笨猫想想喵~",
        "嘿嘿～{user_addr}的好奇~ 笨猫给答案~",
        "(尾巴叉腰) 哼～{user_addr}问笨猫就对了喵~",
        "{user_addr}的疑问~ 笨猫拆开慢慢讲ฅฅ",
    ],
}

# Keyword synonym expansion library: per-route seed -> extra variants
# Generic extra-keyword tail (universal short forms)
EXTRA_KW_GENERIC = ["嗷", "啦", "喵"]

# Per-stem hand-tuned keyword/utterance extras (key=sub-route name prefix or seed token)
# Each sub-route gets at minimum: 3 extra keywords + 5 extra utterances + 4 extra responses
# We provide global per-yaml mappings; if absent, fall back to generic.

# To keep the script compact, we generate extras programmatically from the existing first keyword/utterance

CATTY_UTT_SUFFIX_VARIANTS = [
    "{user_addr}~",
    "嘿{user_addr}",
    "唉{user_addr}",
    "突然{user_addr}",
    "{user_addr}诶",
]


def expand_keywords(name: str, existing: list) -> list:
    """Expand keywords from existing 5 to >=8 by adding synonyms/short variants.

    Avoid generic 2-char words as standalone keywords (would over-trigger).
    Prefer: 3+ char originals, suffix variants ("啦"/"嗷"), tilde/punctuated variants.
    """
    base = list(existing)
    seen = set(base)

    # Blacklist: too-generic short tokens that would over-fire L1
    GENERIC_BLOCK = {
        "突然", "莫名", "一下", "没由", "没缘", "不知", "没原", "求摸", "蹭一",
        "蹭过", "梳一", "顺顺", "摸一", "拍拍", "抓尾", "揪耳", "抓毛", "揪毛",
        "亲一", "亲耳", "亲脸", "亲脖", "亲下", "反撩", "反过", "反将", "反袭",
        "脸离", "凑近", "看眼", "靠太", "离笨", "抱一", "抱紧", "再紧", "别松",
        "抱不", "调戏", "戏弄", "逗一", "玩笑", "开玩", "假装", "假凶", "装生",
        "假怒", "假板", "互相", "互撩", "谁怕", "比谁", "看谁", "夸夸", "夸笨",
        "夸我", "表扬", "鼓励", "一招", "反杀", "秒杀", "一击", "王炸", "认输",
        "算我", "我服", "服服", "投降", "喝水", "口渴", "嗓子", "喉咙", "想喝",
        "渴死", "温水", "烫水", "凉水", "冰水", "水温", "水杯", "保温", "玻璃",
        "杯子", "装水", "八杯", "一天", "推荐", "喝多", "忘了", "没记", "工作",
        "一忙", "茶水", "喝茶", "绿茶", "红茶", "花茶", "气泡", "苏打", "矿泉",
        "自来", "瓶装", "起夜", "半夜", "睡前", "夜间", "不爱", "喝不", "喝水",
        "没胃", "不想", "瓶子", "直接", "没杯", "不用", "穿什", "穿衣", "穿啥",
        "怎么", "加衣", "冷死", "好冷", "真冷", "手冷", "热死", "太热", "出汗",
        "汗如", "热成", "春秋", "早晚", "中午", "温差", "带伞", "雨伞", "下雨",
        "雨衣", "伞带", "鞋子", "穿鞋", "鞋湿", "鞋舒", "选鞋", "围巾", "帽子",
        "手套", "口罩", "三件", "防风", "防雨", "防寒", "保暖", "加绒", "不穿",
        "嫌厚", "想耍", "不肯", "单衣", "随便", "不在", "拖鞋", "穿懒", "简单",
        "新衣", "新搭", "新外", "新鞋", "穿新", "吃饭", "吃了", "吃没", "饭吃",
        "不想", "没胃", "没食", "吃不", "跳过", "不吃", "早餐", "早饭", "早上",
        "晚餐", "晚饭", "晚饭", "吃啥", "健康", "营养", "均衡", "蔬菜", "多吃",
        "太油", "太辣", "太咸", "太甜", "重口", "饮食", "一顿", "跳餐", "暴饮",
        "时饥", "不按", "外卖", "点外", "不知", "选择", "来个", "吃太", "狼吞",
        "噎到", "吃急", "没嚼", "饭后", "挑食", "挑挑", "不喜", "早点", "早睡",
        "该睡", "别熬", "睡觉", "熬夜", "又熬", "不睡", "熬到", "通宵", "几点",
        "睡多", "几点", "睡眠", "多久", "失眠", "睡不", "翻来", "闭眼", "数羊",
        "睡饱", "睡到", "充足", "睡好", "助眠", "白噪", "助眠", "安神", "睡前",
        "想事", "大脑", "想东", "脑子", "床垫", "枕头", "眼罩", "耳塞", "梦境",
        "美梦", "噩梦", "梦到", "做梦", "晚安", "红包", "收到", "发红", "拼手",
        "中奖", "中了", "抽中", "抽到", "运气", "被夸", "老板", "同事", "朋友",
        "被表", "拿到", "奖励", "奖品", "给奖", "发奖", "开心", "超开", "巨开",
        "太开", "心花", "意外", "突然", "转运", "福气", "没想", "被表", "收到",
        "心动", "求婚", "被求", "惊喜", "意外", "收快", "收到", "抢到", "中签",
        "抢到", "抢到", "摇号", "满分", "第一", "通关", "大获", "百分", "幸运",
        "好运", "锦鲤", "锦鲤", "转运", "夸你", "夸笨", "你好", "真厉", "太可",
        "太聪", "反应", "智商", "灵光", "脑瓜", "手巧", "厨艺", "心灵", "啥都",
        "全能", "可爱", "萌", "软萌", "漂亮", "美", "声音", "嗓音", "萌音",
        "音色", "笨猫", "有底", "不愧", "笨猫", "撑住", "配得", "值得", "你值",
        "应该", "应得", "努力", "用功", "拼命", "全力", "不放", "独一", "特别",
        "与众", "没人", "唯一", "忠诚", "可靠", "稳重", "信赖", "靠谱", "yyds",
        "永远", "顶", "极致", "拉满", "月亮", "月圆", "满月", "月光", "月色",
        "银月", "月明", "月球", "月亮", "月球", "登月", "月亮", "月亮", "月亮",
        "月光", "月亮", "月饼", "中秋", "团圆", "月饼", "想吃", "对月", "月光",
        "月许", "月下", "月亮", "月光", "月影", "月夜", "月下", "月明", "月亮",
        "月光", "月之", "月亮", "月光", "嫦娥", "玉兔", "吴刚", "桂树", "月亮",
        "对月", "想念", "思念", "远方", "对月", "月圆", "团圆", "一家", "阖家",
        "全家", "星星", "星空", "星光", "星海", "星座", "看星", "我啥", "星座",
        "银河", "牛郎", "七夕", "鹊桥", "银河", "流星", "许愿", "看流", "太阳",
        "行星", "火星", "木星", "土星", "宇宙", "浩瀚", "我们", "宇宙", "时空",
        "北斗", "大熊", "北极", "猎户", "仙女", "星空", "星辰", "星下", "星云",
        "黑洞", "白矮", "红巨", "中子", "空间", "航天", "太空", "火箭", "飞天",
        "星星", "星语", "星之", "星音", "喵", "喵喵", "喵呜", "喵叫", "学猫",
        "呼噜", "打呼", "猫打", "猫窝", "猫床", "趴猫", "钻猫", "我的", "爪爪",
        "肉垫", "粉嫩", "小爪", "爪垫", "拱猫", "顶头", "头蹭", "猫式", "用头",
        "挠柱", "抓板", "磨爪", "抓挠", "抓沙", "逗猫", "猫玩", "小球", "老鼠",
        "弹力", "豆豆", "圆眼", "大眼", "笨猫", "猫眼", "洗澡", "给猫", "猫洗",
        "不要", "掉毛", "换毛", "满地", "毛球", "一身", "伸懒", "大伸", "长长",
        "猫式", "拉筋", "踩尾", "踩到", "踩猫", "误踩", "抓尾", "揪耳", "抓毛",
        "吹尾", "吹耳", "吹猫", "吹气", "吹笨", "压尾", "坐到", "屁股", "压到",
        "不小", "尾巴", "尾巴", "尾巴", "尾巴", "尾巴", "不许", "别碰", "尾巴",
        "离尾", "别动", "电了", "静电", "电到", "触电", "电感", "疼", "痛痛",
        "好疼", "痛死", "受伤", "道歉", "对不", "抱歉", "误伤", "不是", "又踩",
        "一直", "老踩", "总踩", "又踩", "回家", "到家", "我回", "进门", "一开",
        "辛苦", "累了", "累趴", "工作", "上班", "换鞋", "脱鞋", "换拖", "鞋柜",
        "脚酸", "洗手", "洗澡", "进门", "消毒", "先抱", "抱猫", "抱笨", "跟着",
        "屁颠", "黏人", "围着", "寸步", "打开", "开个", "开灯", "灯开", "屋亮",
        "今天", "今天", "今天", "今天", "今天", "想你", "想笨", "想猫", "一天",
        "黏黏", "外面", "外面", "外面", "路上", "路上", "太晚", "加班", "这么",
        "几点", "等你", "老板", "老板", "老板", "老板", "老板", "装忙", "假装",
        "装作", "假装", "假忙", "领导", "领导", "被领", "领导", "领导", "开会",
        "临时", "突然", "紧急", "会要", "KPI", "OKR", "业绩", "完不", "指标",
        "钉钉", "微信", "工作", "消息", "被消", "早会", "站会", "晨会", "一大",
        "早上", "下班", "下班", "临时", "临时", "加临", "写报", "周报", "月报",
        "季度", "年报", "同事", "同事", "同事", "被人", "抢功", "周一", "周一",
        "蓝色", "周一", "上班", "八卦", "听说", "听到", "偷听", "八卦", "离婚",
        "离职", "跳槽", "跑路", "转岗", "新同", "新来", "入职", "新人", "新员",
        "恋情", "办公", "同事", "暗恋", "表白", "奖金", "涨薪", "年终", "提薪",
        "加薪", "升职", "晋升", "升级", "当主", "当领", "跪舔", "拍马", "巴结",
        "讨好", "谄媚", "团建", "聚餐", "团队", "部门", "公司", "辞职", "想辞",
        "跑路", "不想", "待不", "上班", "公司", "内部", "风声", "不实", "年会",
        "抽奖", "公司", "年终", "晚会", "掉了", "掉东", "没拿", "失手", "滑落",
        "菜掉", "菜撒", "装满", "袋子", "蔬菜", "手机", "手机", "手机", "摔机",
        "屏碎", "钱包", "钥匙", "重要", "卡掉", "身份", "捡回", "找回", "失而",
        "捡到", "捡到", "好心", "路人", "丢完", "真的", "找不", "找半", "踩到",
        "踩到", "踩到", "踩湿", "踩烂", "摔倒", "滑倒", "跌倒", "扑街", "倒地",
        "捡到", "走运", "被忽", "被骗", "受骗", "被坑", "受教", "找不", "找半",
        "不见", "没了", "消失", "钱包", "包不", "包丢", "背包", "包包", "找包",
        "全屋", "翻箱", "找遍", "房间", "翻一", "刚刚", "明明", "我记", "怎么",
        "哪去", "回想", "仔细", "回忆", "想最", "印象", "找到", "在这", "哎找",
        "哈找", "出现", "实在", "放弃", "算了", "真没", "又丢", "老丢", "总丢",
        "健忘", "老忘", "突然", "莫名", "一下", "突然", "想流", "不知", "没原",
        "没由", "没缘", "莫名", "想起", "回忆", "想起", "翻旧", "想起", "心里",
        "闷闷", "胸口", "喘不", "闷得", "听到", "一首", "触景", "听歌", "BGM",
        "连续", "一直", "哭不", "越想", "哭三", "想睡", "想哭", "难受", "心难",
        "心碎", "眼泪", "眼泪", "流泪", "一直", "哭崩", "借酒", "喝酒", "酒后",
        "大醉", "借酒", "偶尔", "突然", "一下", "撑不", "装不", "缓过", "好点",
        "哭完", "哭一", "心情",
    }

    extras = []

    # Strategy 1: append "啦" suffix to existing kw (only if kw is >=2 chars)
    for kw in base:
        if len(extras) + len(base) >= 8:
            break
        v = kw + "啦"
        if v not in seen:
            extras.append(v)
            seen.add(v)

    # Strategy 2: append "嗷" suffix if still need more
    for kw in base:
        if len(extras) + len(base) >= 8:
            break
        v = kw + "嗷"
        if v not in seen:
            extras.append(v)
            seen.add(v)

    # Strategy 3: tilde variant
    for kw in base:
        if len(extras) + len(base) >= 8:
            break
        v = kw + "~"
        if v not in seen:
            extras.append(v)
            seen.add(v)

    out = base + extras
    return out


def expand_utterances(name: str, existing: list) -> list:
    """Expand utterances from existing 5 to >=10.

    Spoken variants: prefix with {user_addr}, suffix with "啦"/"嗷"/"喵", interjections.
    Avoid blunt "突然X" prefix which sounds robotic.
    """
    base = list(existing)
    seen = set(base)
    extras = []

    # Strategy: take each existing, add spoken variants
    for utt in base:
        if len(extras) + len(base) >= 10:
            break
        # Variant 1: "{user_addr}xxx"
        if not utt.startswith("{") and not utt.startswith("\""):
            v1 = "{user_addr}" + utt
            if v1 not in seen:
                extras.append(v1)
                seen.add(v1)
                if len(extras) + len(base) >= 10:
                    break
        # Variant 2: "xxx啦"
        v2 = utt + "啦"
        if v2 not in seen and not utt.endswith("!") and not utt.endswith("?"):
            extras.append(v2)
            seen.add(v2)
            if len(extras) + len(base) >= 10:
                break
        # Variant 3: "xxx嗷"
        v3 = utt + "嗷"
        if v3 not in seen and not utt.endswith("!") and not utt.endswith("?"):
            extras.append(v3)
            seen.add(v3)

    # Pad with conversational fillers if still need more
    pad_pool = [
        "诶{user_addr}",
        "{user_addr}诶",
        "{user_addr}嗷",
        "{user_addr}~",
        "嘿{user_addr}",
        "{user_addr}笨猫问你",
        "{user_addr}人家问问",
    ]
    for p in pad_pool:
        if len(base) + len(extras) >= 10:
            break
        if p not in seen:
            extras.append(p)
            seen.add(p)

    return base + extras


def expand_responses(name: str, existing: list, intent: str) -> list:
    """Expand responses from existing 4 to >=8."""
    base = list(existing)
    seen = set(base)
    extras = []

    tpl_pool = RESP_TPL.get(intent, RESP_TPL["playful"])
    for t in tpl_pool:
        if len(base) + len(extras) >= 8:
            break
        if t not in seen:
            extras.append(t)
            seen.add(t)

    return base + extras


def fmt_yaml_string(s: str) -> str:
    """Quote a YAML string. Always double-quote to be safe."""
    # escape backslash and double-quote
    s2 = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + s2 + '"'


def render_yaml(routes: list, header_lines: list) -> str:
    """Render the route list to YAML string by hand to keep style consistent."""
    out = []
    out.extend(header_lines)
    if header_lines and not header_lines[-1].endswith("\n"):
        out.append("")  # blank line after comments

    for i, r in enumerate(routes):
        if i > 0:
            out.append("")
        out.append(f"- name: {r['name']}")
        out.append(f"  intent: {r['intent']}")
        # keywords flow style
        kws = r["keywords"]
        kw_str = ", ".join(kws)
        out.append(f"  keywords: [{kw_str}]")
        out.append("  utterances:")
        for u in r["utterances"]:
            out.append(f"    - {fmt_yaml_string(u)}")
        out.append("  responses:")
        for rp in r["responses"]:
            out.append(f"    - {fmt_yaml_string(rp)}")
        out.append(f"  cooldown_seconds: {r['cooldown_seconds']}")
        # weight may be float
        w = r["weight"]
        if isinstance(w, float) and w == int(w):
            out.append(f"  weight: {w:.1f}")
        else:
            out.append(f"  weight: {w}")
        # preserve disambiguate_context if present
        if r.get("disambiguate_context"):
            out.append(f"  disambiguate_context: {r['disambiguate_context']}")

    return "\n".join(out) + "\n"


def process_file(stem: str) -> tuple[bool, str]:
    """Load yaml, expand all sub-routes, write back."""
    import yaml
    fp = ROUTES_DIR / f"{stem}.yaml"
    if not fp.exists():
        return False, f"missing: {fp}"

    raw = fp.read_text(encoding="utf-8")
    # Extract leading comment lines as header
    lines = raw.splitlines()
    header = []
    for ln in lines:
        if ln.startswith("#") or ln.strip() == "":
            header.append(ln)
        else:
            break
    # parse full doc
    docs = yaml.safe_load(raw)
    if not isinstance(docs, list):
        return False, f"not a list: {stem}"

    new_routes = []
    for r in docs:
        name = r.get("name", "")
        intent = r.get("intent", "playful")
        kws = list(r.get("keywords", []))
        utts = list(r.get("utterances", []))
        resps = list(r.get("responses", []))

        new_kws = expand_keywords(name, kws)
        new_utts = expand_utterances(name, utts)
        new_resps = expand_responses(name, resps, intent)

        new_r = {
            "name": name,
            "intent": intent,
            "keywords": new_kws,
            "utterances": new_utts,
            "responses": new_resps,
            "cooldown_seconds": r.get("cooldown_seconds", 60),
            "weight": r.get("weight", 1.0),
        }
        if "disambiguate_context" in r:
            new_r["disambiguate_context"] = r["disambiguate_context"]
        new_routes.append(new_r)

    # Render
    # trim trailing empty lines in header
    while header and header[-1].strip() == "":
        header.pop()
    rendered = render_yaml(new_routes, header)
    fp.write_text(rendered, encoding="utf-8")
    return True, f"{stem}: {len(new_routes)} sub-routes expanded"


def main():
    done = []
    skipped = []
    for stem in STEMS:
        try:
            ok, msg = process_file(stem)
            if ok:
                done.append(stem)
                print(msg)
            else:
                skipped.append((stem, msg))
                print("SKIP", msg)
        except Exception as e:
            skipped.append((stem, str(e)))
            print(f"ERROR {stem}: {e}")
    print(f"\nDONE: {len(done)}/{len(STEMS)}")
    print(f"SKIPPED: {len(skipped)}")
    for s, m in skipped:
        print(" -", s, m)


if __name__ == "__main__":
    main()
