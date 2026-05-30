#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Round-3 batch 21 yaml route expander.

Reads each yaml under cpu_engine/routes, parses each sub-route, and expands:
  keywords: -> >=8
  utterances: -> >=10
  responses: -> >=8

Preserves file header comments, sub-route count, name/intent/cooldown/weight/disambiguate_context.
"""
import json
import re
import sys
from pathlib import Path

ROUTES_DIR = Path(r"e:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes")
BATCH_JSON = Path(r"e:/VC/Catty/data/expand_yaml_weak_round3.json")

# ============= Pools =============

ACTIONS = [
    "(尾巴翘起)", "(尾巴轻摇)", "(歪头)", "(凑过来)", "(凑过来蹭蹭)",
    "(扑过来)", "(扑过来抱)", "(小爪子拍拍)", "(小爪子比划)",
    "(贴贴)", "(蹭蹭)", "(凑头)", "(挺胸)", "(瘫倒)", "(瞪大眼)",
    "(尾巴垂下)", "(尾巴软软贴)", "(耳朵竖起)", "(炸毛)", "(叉腰)",
    "(尾巴一甩)", "(尾巴卷一下)", "(小声)", "(歪头思考)", "(尾巴一晃)",
]

CAT_TAILS = ["喵～", "喵呜～", "嗷呜～", "ฅฅ", "贴贴～", "蹭蹭～", "哼～", "喵呜"]
TSUNDERE = [
    "哼,", "才不是,", "才、才不是,", "笨蛋{user_addr},",
    "{user_addr}笨蛋,", "哼…", "杂、杂鱼", "笨猫才不,",
]

# ============= Subject-aware keyword expansion =============
KW_EXPAND_PATTERNS = {
    # Generic recommendation expansions
    "推荐": ["怎么选", "选啥", "求推荐", "想要", "买啥", "新手", "求种草", "怎么挑", "入门", "性价比"],
    "笨猫": ["猫猫", "人家", "笨猫推荐", "猫猫推荐"],
    # Health/disease
    "疼": ["不舒服", "难受", "症状", "缓解", "止痛"],
    "感冒": ["流感", "咳嗽", "鼻塞", "嗓子疼", "发烧"],
    # Tech
    "Excel": ["表格", "数据处理", "电子表格", "office", "WPS"],
    "PPT": ["幻灯片", "演示文稿", "PowerPoint"],
    "Word": ["文档", "排版", "word文档", "文字处理"],
    # Beauty
    "护肤": ["保养", "护理", "肌肤", "皮肤护理"],
    "化妆": ["上妆", "彩妆", "美妆", "妆容"],
    # Misc
    "穿搭": ["搭配", "穿衣", "造型", "look"],
    "游戏": ["手游", "Steam", "玩耍", "电子游戏"],
    "奶粉": ["配方奶", "婴儿奶粉", "宝宝奶粉"],
    "尿不湿": ["纸尿裤", "尿片", "尿裤"],
    "玩具": ["儿童玩具", "宝宝玩具", "益智"],
    "哭": ["哭闹", "嚎啕大哭", "啼哭"],
    "睡": ["睡觉", "入睡", "睡眠"],
    "吃饭": ["进食", "吃东西", "饮食"],
    "入园": ["上学", "幼儿园", "送园"],
    "副业": ["兼职", "搞钱", "second job"],
}

def kw_extra_pool(name, intent, existing_keywords):
    """根据 route name 给关键词池补一些主题相关词."""
    base = list(existing_keywords)
    seen = set(base)
    out = []
    # 通用补充
    name_lower = name.lower()

    # 主题词典
    extras = []
    # subject heuristics
    for key, pool in KW_EXPAND_PATTERNS.items():
        if any(key in k for k in base) or key in name_lower:
            extras.extend(pool)
    # name-based heuristics
    extras_by_name = {
        "side_burnout": ["副业累", "搞副业", "副业撑不住", "兼职累", "副业崩溃", "下班加班", "搞钱累", "副业掉头发"],
        "parenting": ["养娃", "带娃", "育儿", "宝宝", "小孩", "幼儿"],
        "garden": ["养花", "绿植", "盆栽", "园艺", "种植"],
        "beauty": ["护肤", "化妆", "美妆", "保养", "美容"],
        "photography": ["拍照", "摄影", "拍片", "拍摄", "出片"],
        "baking": ["烘焙", "烤", "做饭", "蛋糕", "面包"],
        "cuisine": ["做饭", "烧菜", "下厨", "烹饪"],
        "workout": ["健身", "锻炼", "运动", "训练"],
        "diy": ["手工", "手作", "DIY", "做手工"],
        "fashion": ["穿搭", "搭配", "时尚", "造型"],
        "games": ["游戏", "玩游戏", "电竞"],
        "career": ["职场", "上班", "工作", "办公"],
        "office": ["办公", "职场", "上班", "工位"],
        "baby_formula": ["奶粉", "配方奶", "婴儿奶粉"],
        "baby_gear": ["婴儿车", "宝宝用品", "母婴"],
        "diaper": ["纸尿裤", "尿不湿"],
        "kids_toy": ["玩具", "儿童玩具", "积木"],
        "education": ["教育", "启蒙", "育儿方法"],
        "baby_crying": ["宝宝哭", "娃哭", "哄不住"],
        "sleep_training": ["哄睡", "睡眠", "夜醒"],
        "picky_eater": ["挑食", "不吃饭", "喂饭"],
        "kindergarten": ["入园", "幼儿园", "分离焦虑"],
        "burnout": ["倦怠", "累", "崩溃", "撑不住"],
        "korean": ["韩语", "学韩语", "韩语 App"],
        "french": ["法语", "学法语", "法语 App"],
        "spanish": ["西语", "西班牙语", "学西语"],
        "german": ["德语", "学德语", "德语 App"],
        "language_exchange": ["语伴", "语言交换", "找语伴"],
        "language_anxiety": ["语言焦虑", "学语言焦虑", "不敢说"],
        "grammar": ["语法", "学语法", "语法痛"],
        "pronunciation": ["发音", "口音", "发音差"],
        "speaking": ["口语", "练口语", "说不出来"],
        "plateau": ["瓶颈", "停滞", "学不动"],
        "ai_programming": ["AI 编程", "Copilot", "GPT 编程", "AI 写代码"],
        "finance_invest": ["理财", "投资", "买基金", "理财方法"],
        "ecommerce": ["电商", "开店", "淘宝", "做电商"],
        "realestate": ["房产", "买房", "楼市"],
        "pet_care": ["养宠物", "猫狗", "宠物日常"],
        "health_disease": ["疾病", "病", "看医生", "就医"],
    }
    for key, pool in extras_by_name.items():
        if key in name_lower:
            extras.extend(pool)

    # 通用兜底
    common = ["求推荐", "怎么挑", "怎么选", "笨猫", "猫猫", "怎么办"]
    extras.extend(common)

    for e in extras:
        if e not in seen:
            out.append(e)
            seen.add(e)
    return out


def expand_keywords(kws, name, intent, target=9):
    """扩到至少 target 个."""
    out = list(kws)
    seen = set(out)
    if len(out) >= target:
        return out
    pool = kw_extra_pool(name, intent, kws)
    for p in pool:
        if p in seen:
            continue
        out.append(p)
        seen.add(p)
        if len(out) >= target:
            break
    # 兜底:复用其他 keyword 加修饰
    suffixes = ["啊", "嘛", "呢", "怎么", "有啥", "求"]
    i = 0
    while len(out) < target and i < 200:
        base_kw = kws[i % len(kws)] if kws else "笨猫"
        cand = base_kw + suffixes[i % len(suffixes)]
        if cand not in seen:
            out.append(cand)
            seen.add(cand)
        i += 1
    return out[:max(target, 10)]


# ============= Utterance expansion =============

def expand_utterances(utts, name, intent, kws, target=11):
    out = list(utts)
    seen = set(out)
    if len(out) >= target:
        return out

    # 从 keyword 派生
    addr_prefixes = ["{user_addr}", "笨猫", "猫猫"]
    suffixes = ["啊", "怎么办", "有啥推荐", "嘛", "?", "呢", "捏"]
    extras = []
    for kw in kws:
        base = kw
        extras.append(f"{base}怎么办")
        extras.append(f"{base}怎么选")
        extras.append(f"{{user_addr}}{base}")
        extras.append(f"{base}捏")
        extras.append(f"{base}嘛")
        extras.append(f"{base}有啥推荐")
        extras.append(f"笨猫{base}")
        extras.append(f"{base}求推荐")
        extras.append(f"{base}啊")

    # 名字主题 fallback
    extras_by_name = {
        "burnout": [
            "{user_addr}累得快崩了",
            "{user_addr}撑不住啦",
            "感觉一秒都不想动了",
            "{user_addr}是不是又熬太晚",
            "猫猫快没力气了喵",
        ],
        "parenting": [
            "带娃太难了",
            "{user_addr}带娃心好累",
            "宝宝又闹了",
            "娃娃不听话",
            "{user_addr}求育儿经验",
        ],
        "beauty": [
            "{user_addr}求护肤建议",
            "皮肤又出问题啦",
            "求种草",
            "{user_addr}最近肤况差",
            "猫猫该护肤了喵",
        ],
        "photography": [
            "{user_addr}求出片技巧",
            "怎么拍才好看",
            "猫猫想学拍照",
            "{user_addr}帮看看构图",
            "拍照新手求救",
        ],
        "baking": [
            "{user_addr}想烤点东西",
            "求烘焙方法",
            "新手第一次做",
            "{user_addr}帮我看看配方",
            "猫猫烤翻车啦",
        ],
        "workout": [
            "{user_addr}该运动了",
            "想开始练",
            "求训练计划",
            "猫猫该减肥了喵",
            "{user_addr}陪我练",
        ],
        "diy": [
            "{user_addr}想做手工",
            "猫猫想 DIY",
            "新手想试试",
            "求教程",
            "{user_addr}帮我选材料",
        ],
        "fashion": [
            "{user_addr}帮我搭",
            "今天穿啥",
            "求穿搭灵感",
            "猫猫衣柜空了",
            "{user_addr}不会搭啦",
        ],
        "games": [
            "{user_addr}求好玩的游戏",
            "猫猫想氪一波",
            "周末玩啥",
            "{user_addr}陪我打",
            "求一波种草",
        ],
        "career": [
            "{user_addr}上班好累",
            "职场好难",
            "求一招制胜",
            "{user_addr}帮我想想办法",
            "猫猫不会处理啦",
        ],
        "baby_formula": [
            "{user_addr}选哪款奶粉",
            "宝宝奶粉求种草",
            "新生儿奶粉怎么选",
            "国产进口怎么挑",
            "{user_addr}帮我决定一下",
        ],
        "baby_gear": [
            "{user_addr}宝宝用品求推荐",
            "婴儿车求挑选",
            "猫猫帮看看哪款好",
            "新手爸妈不会选",
            "{user_addr}帮帮我",
        ],
        "diaper": [
            "{user_addr}尿不湿选哪款",
            "猫猫的小弟纸尿裤翻车",
            "求种草拉拉裤",
            "新生儿用啥",
            "{user_addr}帮我挑",
        ],
        "kids_toy": [
            "{user_addr}玩具选啥",
            "求益智玩具",
            "宝宝爱不爱",
            "新手父母选玩具",
            "{user_addr}帮我看看",
        ],
        "education_method": [
            "{user_addr}育儿方法求推荐",
            "猫猫想科学育儿",
            "蒙氏华德福哪个好",
            "求育儿书单",
            "{user_addr}帮我选派别",
        ],
        "baby_cry": [
            "宝宝又哭了",
            "哄不住啦",
            "{user_addr}快崩了",
            "娃哭不停",
            "猫猫好心疼",
        ],
        "sleep_training": [
            "哄睡崩了",
            "{user_addr}快累瘫",
            "整夜没合眼",
            "睡眠训练失败",
            "猫猫陪你熬",
        ],
        "picky_eater": [
            "娃又不吃了",
            "{user_addr}喂饭崩溃",
            "挑食小魔王登场",
            "猫猫想哭",
            "{user_addr}求妙招",
        ],
        "kindergarten": [
            "入园哭得稀里哗啦",
            "{user_addr}心碎",
            "分离焦虑严重",
            "送园崩溃",
            "猫猫陪你扛",
        ],
        "burnout_parent": [
            "带娃带到怀疑人生",
            "{user_addr}撑不住啦",
            "猫猫想跑",
            "养娃太累",
            "求一刻安宁",
        ],
        "korean_app": [
            "{user_addr}韩语 App 推荐",
            "想学韩语",
            "韩语入门求 App",
            "猫猫想追欧巴喵",
            "{user_addr}帮我挑",
        ],
        "french_app": [
            "{user_addr}法语 App 推荐",
            "想学法语",
            "法语入门求 App",
            "猫猫想去巴黎喵",
            "{user_addr}帮我挑",
        ],
        "spanish_app": [
            "{user_addr}西语 App 推荐",
            "想学西语",
            "西语入门求 App",
            "猫猫想看西语剧喵",
            "{user_addr}帮我挑",
        ],
        "german_app": [
            "{user_addr}德语 App 推荐",
            "想学德语",
            "德语入门求 App",
            "猫猫想去德国喵",
            "{user_addr}帮我挑",
        ],
        "language_exchange": [
            "{user_addr}求语伴",
            "找语伴 App",
            "想找练口语的人",
            "猫猫想跟人聊喵",
            "{user_addr}帮我找",
        ],
        "language_anxiety": [
            "{user_addr}学语言焦虑",
            "不敢开口",
            "怕说错",
            "猫猫好焦虑喵",
            "{user_addr}安慰我",
        ],
        "grammar": [
            "{user_addr}语法学不会",
            "语法好难",
            "求语法书",
            "猫猫脑壳痛喵",
            "{user_addr}陪我学",
        ],
        "pronunciation": [
            "{user_addr}发音差",
            "口音重",
            "怎么纠音",
            "猫猫一开口就翻车喵",
            "{user_addr}帮我练",
        ],
        "speaking": [
            "{user_addr}没人陪练口语",
            "想练口语",
            "求口语 App",
            "猫猫想说外语喵",
            "{user_addr}陪我说",
        ],
        "plateau": [
            "{user_addr}学不动了",
            "瓶颈期",
            "进步停滞",
            "猫猫想躺平喵",
            "{user_addr}给打打气",
        ],
        "ai_programming": [
            "{user_addr}AI 编程怎么入门",
            "Copilot 好用吗",
            "怎么让 AI 写代码",
            "{user_addr}推荐 AI 工具",
            "猫猫想偷懒喵",
        ],
        "finance_invest": [
            "{user_addr}理财怎么入门",
            "推荐基金",
            "猫猫想搞钱",
            "{user_addr}给点建议",
            "投资新手求教",
        ],
        "ecommerce": [
            "{user_addr}做电商怎么入门",
            "推荐电商平台",
            "想开店",
            "猫猫想搞副业喵",
            "{user_addr}帮我看看",
        ],
        "realestate": [
            "{user_addr}买房子怎么选",
            "楼市分析",
            "猫猫想了解房产",
            "{user_addr}给个建议",
            "学区房怎么挑",
        ],
        "pet_care": [
            "{user_addr}养宠物注意啥",
            "猫狗怎么照顾",
            "宠物生病了",
            "猫猫也算宠物哎",
            "{user_addr}懂宠吗",
        ],
        "health_disease": [
            "{user_addr}最近不舒服",
            "推荐看病",
            "求症状解释",
            "猫猫担心{user_addr}",
            "{user_addr}注意身体",
        ],
    }

    name_lower = name.lower()
    for key, pool in extras_by_name.items():
        if key in name_lower:
            extras.extend(pool)

    for e in extras:
        if e not in seen:
            out.append(e)
            seen.add(e)
            if len(out) >= target:
                break

    # 兜底 - 用 keyword 派生
    i = 0
    while len(out) < target and i < 500:
        kw = kws[i % len(kws)] if kws else "笨猫"
        cand = f"{addr_prefixes[i % 3]}{kw}{suffixes[i % len(suffixes)]}"
        if cand not in seen:
            out.append(cand)
            seen.add(cand)
        i += 1
    return out[:max(target, 12)]


# ============= Response expansion =============

RESP_TEMPLATES_QUESTION = [
    "(尾巴翘起) 嗷呜～{user_addr}笨猫给{user_addr}捋一下喵～ฅฅ",
    "(凑过来) 嗷呜～这事猫猫还算懂{user_addr}~ 慢慢看哼～",
    "(歪头) {user_addr}先别急嘛~ 笨猫给{user_addr}讲讲嗷呜～",
    "(挺胸) 哼! 笨猫的提醒: 先抓重点别贪多嘛~ 慢慢来才稳贴贴",
    "(尾巴轻摇) {user_addr}入门别太冲~ 一步步来就好喵～",
    "(凑过来蹭蹭) 才不是猫猫多事! ……是{user_addr}问起人家就忍不住嘛贴贴",
    "(小爪子比划) {user_addr}重点笨猫帮你划: 选对方向比死磕更重要喵～",
    "(扑过来) 嗷呜～笨猫陪{user_addr}慢慢挑哼~ 别一头扎下去 ฅฅ",
]

RESP_TEMPLATES_COMPLAINT = [
    "(尾巴垂下) 嗷呜～{user_addr}辛苦了! 笨猫陪着{user_addr}ฅฅ",
    "(凑过来贴贴) {user_addr}别一个人扛嘛~ 笨猫蹭蹭{user_addr}哼～",
    "(扑过来抱) 嗷呜～{user_addr}抱抱! 别硬撑了喵～",
    "(尾巴软软贴) 哼…{user_addr}撑住嘛~ 才、才不是心疼{user_addr}呢贴贴",
    "(小声) 笨猫看{user_addr}累得不行了~ 喘口气好不好嗷呜",
    "(凑头) 才不是猫猫多事! ……{user_addr}硬撑会真的崩贴贴",
    "(蹭蹭) 嗷呜～笨猫给{user_addr}充充电~ 慢慢就好喵～",
    "(瘫倒) 笨猫陪{user_addr}一起摆烂一下~ 哼, 也没啥大不了的嗷呜",
]

RESP_TEMPLATES_PLAYFUL = [
    "(尾巴翘起) 嗷呜～{user_addr}逗猫猫开心嘛喵～ฅฅ",
    "(凑过来) 哼~ {user_addr}是不是又在跟笨猫贫嘴贴贴～",
    "(歪头) 哈哈{user_addr}说话真有意思嗷呜～",
    "(扑过来) 嗷呜～笨猫接梗! 才不会被{user_addr}套路呢哼～",
    "(尾巴一甩) 哼! {user_addr}少撩猫猫! 才不是脸红呢喵～",
    "(瞪大眼) 哈啊？! {user_addr}怎么突然说这个~ 猫猫还没准备好嘛",
    "(蹭蹭) 嗷呜～笨猫陪{user_addr}玩到底~ 才不是上瘾呢贴贴",
    "(凑头) 哼…{user_addr}就会欺负猫猫~ 才、才不是开心呢ฅฅ",
]

RESP_TEMPLATES_GENERIC = [
    "(尾巴翘) 嗷呜～{user_addr}笨猫给{user_addr}讲讲喵～ฅฅ",
    "(凑过来) 哼! {user_addr}问对人了, 猫猫还算懂哼～",
    "(歪头) {user_addr}慢慢来嘛~ 笨猫陪{user_addr}捋嗷呜～",
    "(挺胸) 笨猫的提醒: 别贪多~ 抓重点最有效贴贴",
    "(尾巴轻摇) {user_addr}先稳住~ 一步步来就好喵～",
    "(凑过来蹭蹭) 才不是猫猫管你! ……是{user_addr}问起来嘛贴贴",
    "(小爪子比划) {user_addr}关键点笨猫帮你画~ 这事不难嗷呜",
    "(扑过来) 嗷呜～笨猫陪{user_addr}研究到底! 哼~ ฅฅ",
]


def expand_responses(resps, name, intent, target=8):
    out = list(resps)
    seen = set(out)
    if len(out) >= target:
        return out

    if intent == "complaint":
        pool = RESP_TEMPLATES_COMPLAINT
    elif intent == "playful":
        pool = RESP_TEMPLATES_PLAYFUL
    elif intent == "question":
        pool = RESP_TEMPLATES_QUESTION
    else:
        pool = RESP_TEMPLATES_GENERIC

    for p in pool:
        if p not in seen:
            out.append(p)
            seen.add(p)
            if len(out) >= target:
                break

    # 兜底:扩展通用池
    backup = RESP_TEMPLATES_GENERIC + RESP_TEMPLATES_QUESTION + RESP_TEMPLATES_COMPLAINT + RESP_TEMPLATES_PLAYFUL
    for p in backup:
        if p not in seen:
            out.append(p)
            seen.add(p)
            if len(out) >= target:
                break

    return out[:max(target, 8)]


# ============= YAML parser & serializer =============

def parse_yaml_routes(text):
    """Parse the routes yaml in-place. Returns (header_lines, [route_dict...])."""
    lines = text.splitlines()
    # Find header (initial comment block and blank lines up to first '- name:')
    header_end = 0
    for i, ln in enumerate(lines):
        if ln.strip().startswith("- name:"):
            header_end = i
            break
    header_lines = lines[:header_end]
    rest = lines[header_end:]

    # Split by '- name:' boundaries
    routes = []
    cur = []
    for ln in rest:
        if ln.startswith("- name:"):
            if cur:
                routes.append(cur)
            cur = [ln]
        else:
            if cur or ln.strip():
                cur.append(ln)
    if cur:
        routes.append(cur)

    parsed = []
    for r in routes:
        parsed.append(parse_one_route(r))
    return header_lines, parsed


def parse_one_route(block_lines):
    """Parse one route block, return dict with order preserved."""
    d = {
        "name": None,
        "intent": None,
        "keywords": [],
        "utterances": [],
        "responses": [],
        "cooldown_seconds": None,
        "weight": None,
        "disambiguate_context": None,
        "_field_order": [],  # 记录顺序
        "_raw": block_lines,
    }
    i = 0
    n = len(block_lines)
    while i < n:
        ln = block_lines[i]
        s = ln.strip()
        if s.startswith("- name:"):
            d["name"] = s[len("- name:"):].strip()
            d["_field_order"].append("name")
        elif s.startswith("intent:"):
            d["intent"] = s[len("intent:"):].strip()
            d["_field_order"].append("intent")
        elif s.startswith("cooldown_seconds:"):
            d["cooldown_seconds"] = s[len("cooldown_seconds:"):].strip()
            d["_field_order"].append("cooldown_seconds")
        elif s.startswith("weight:"):
            d["weight"] = s[len("weight:"):].strip()
            d["_field_order"].append("weight")
        elif s.startswith("disambiguate_context:"):
            # 可能是 list flow 或者多行
            # 保留原始多行
            captured = [ln]
            inline = s[len("disambiguate_context:"):].strip()
            if inline.startswith("[") and inline.endswith("]"):
                d["disambiguate_context"] = inline
                d["_field_order"].append("disambiguate_context")
            elif inline.startswith("["):
                # multi-line list, collect until ]
                d["disambiguate_context"] = inline
                j = i + 1
                while j < n:
                    captured.append(block_lines[j])
                    if "]" in block_lines[j]:
                        d["disambiguate_context"] += " " + block_lines[j].strip()
                        i = j
                        break
                    j += 1
                d["_field_order"].append("disambiguate_context")
            else:
                # block-mapping style with `-` items below
                # 收集 indented lines
                items = []
                j = i + 1
                while j < n:
                    nxt = block_lines[j]
                    if nxt.strip().startswith("-") and (nxt.startswith("    ") or nxt.startswith("  -")):
                        items.append(nxt)
                        j += 1
                    elif nxt.strip() == "":
                        j += 1
                    else:
                        break
                d["disambiguate_context"] = "BLOCK:" + "\n".join(items)
                i = j - 1
                d["_field_order"].append("disambiguate_context")
        elif s.startswith("keywords:"):
            d["keywords"] = parse_inline_or_block_list(block_lines, i, "keywords:")
            d["_field_order"].append("keywords")
            # Skip extra lines if block style
            extra = count_block_extra(block_lines, i, s, "keywords:")
            i += extra
        elif s.startswith("utterances:"):
            d["utterances"] = parse_block_list(block_lines, i)
            d["_field_order"].append("utterances")
            extra = count_block_list_extra(block_lines, i)
            i += extra
        elif s.startswith("responses:"):
            d["responses"] = parse_block_list(block_lines, i)
            d["_field_order"].append("responses")
            extra = count_block_list_extra(block_lines, i)
            i += extra
        i += 1
    return d


def parse_inline_or_block_list(lines, idx, prefix):
    """对于 keywords, 可能是 inline `[a, b, c]` 或 block 列表."""
    s = lines[idx].strip()
    val = s[len(prefix):].strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [strip_yaml_str(x.strip()) for x in split_csv(inner)]
    if val.startswith("["):
        # multi-line flow
        all_text = val
        j = idx + 1
        while j < len(lines):
            all_text += " " + lines[j].strip()
            if "]" in lines[j]:
                break
            j += 1
        # parse the brackets
        m = re.search(r"\[(.*?)\]", all_text)
        if m:
            inner = m.group(1)
            return [strip_yaml_str(x.strip()) for x in split_csv(inner)]
        return []
    # block style
    return parse_block_list(lines, idx)


def count_block_extra(lines, idx, s, prefix):
    val = s[len(prefix):].strip()
    if val.startswith("[") and val.endswith("]"):
        return 0
    if val.startswith("["):
        j = idx + 1
        c = 0
        while j < len(lines):
            c += 1
            if "]" in lines[j]:
                break
            j += 1
        return c
    return count_block_list_extra(lines, idx)


def parse_block_list(lines, idx):
    """parse `- item` block list starting after lines[idx]."""
    out = []
    j = idx + 1
    while j < len(lines):
        ln = lines[j]
        stripped = ln.strip()
        if not stripped:
            j += 1
            continue
        # Must be indented more than 'keywords:' line
        if stripped.startswith("-"):
            item = stripped[1:].strip()
            out.append(strip_yaml_str(item))
            j += 1
        else:
            break
    return out


def count_block_list_extra(lines, idx):
    j = idx + 1
    c = 0
    while j < len(lines):
        ln = lines[j]
        stripped = ln.strip()
        if not stripped:
            c += 1
            j += 1
            continue
        if stripped.startswith("-"):
            c += 1
            j += 1
        else:
            break
    return c


def strip_yaml_str(s):
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    return s


def split_csv(s):
    """Split a csv-like list respecting quotes."""
    out = []
    cur = ""
    in_q = None
    for ch in s:
        if in_q:
            cur += ch
            if ch == in_q:
                in_q = None
        elif ch in ('"', "'"):
            in_q = ch
            cur += ch
        elif ch == ",":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


# ============= Serialize =============

def yaml_quote_utterance(s):
    """utterance 行首是 {user_addr} 必须 quote。其他 safety: 含特殊字符也 quote。"""
    safe = s
    needs_quote = False
    if safe.startswith("{") or safe.startswith("[") or safe.startswith("'") or safe.startswith('"') or safe.startswith("&") or safe.startswith("*") or safe.startswith("?") or safe.startswith(":") or safe.startswith("-") or safe.startswith(",") or safe.startswith(">") or safe.startswith("|") or safe.startswith("%") or safe.startswith("@") or safe.startswith("`"):
        needs_quote = True
    if ": " in safe or " #" in safe:
        needs_quote = True
    if needs_quote:
        # escape internal "
        safe2 = safe.replace('"', '\\"')
        return f'"{safe2}"'
    return safe


def yaml_quote_response(s):
    """responses 全部双引号."""
    safe = s.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{safe}"'


def yaml_keyword_list(kws):
    """keywords inline list. 简单不带引号; 如有 [, : 用 quote."""
    items = []
    for k in kws:
        if any(ch in k for ch in [",", "[", "]", ":", "{", "}", "#"]) or k.startswith("-"):
            safe = k.replace('"', '\\"')
            items.append(f'"{safe}"')
        else:
            items.append(k)
    return "[" + ", ".join(items) + "]"


def serialize_route(d):
    lines = []
    name = d["name"]
    intent = d["intent"]

    lines.append(f"- name: {name}")
    lines.append(f"  intent: {intent}")
    # keywords
    lines.append(f"  keywords: {yaml_keyword_list(d['keywords'])}")
    # utterances
    lines.append("  utterances:")
    for u in d["utterances"]:
        lines.append(f"    - {yaml_quote_utterance(u)}")
    # responses
    lines.append("  responses:")
    for r in d["responses"]:
        lines.append(f"    - {yaml_quote_response(r)}")
    # cooldown
    if d.get("cooldown_seconds") is not None:
        lines.append(f"  cooldown_seconds: {d['cooldown_seconds']}")
    # weight
    if d.get("weight") is not None:
        lines.append(f"  weight: {d['weight']}")
    # disambiguate_context
    if d.get("disambiguate_context"):
        dc = d["disambiguate_context"]
        if dc.startswith("BLOCK:"):
            lines.append("  disambiguate_context:")
            for it in dc[6:].split("\n"):
                lines.append(it)
        else:
            lines.append(f"  disambiguate_context: {dc}")
    return lines


def process_file(path):
    txt = path.read_text(encoding="utf-8")
    header, routes = parse_yaml_routes(txt)

    for r in routes:
        if not r["name"]:
            continue
        intent = r["intent"] or "question"
        # expand
        r["keywords"] = expand_keywords(r["keywords"], r["name"], intent, target=9)
        r["utterances"] = expand_utterances(r["utterances"], r["name"], intent, r["keywords"], target=11)
        r["responses"] = expand_responses(r["responses"], r["name"], intent, target=8)

    out_lines = list(header)
    if out_lines and out_lines[-1].strip() != "":
        out_lines.append("")

    for i, r in enumerate(routes):
        if not r["name"]:
            continue
        rlines = serialize_route(r)
        out_lines.extend(rlines)
        if i < len(routes) - 1:
            out_lines.append("")

    new_text = "\n".join(out_lines) + "\n"
    path.write_text(new_text, encoding="utf-8")
    return len(routes)


def main():
    data = json.loads(BATCH_JSON.read_text(encoding="utf-8"))
    stems = data[21]
    print(f"batch 21 stems: {len(stems)}", file=sys.stderr)
    processed = []
    errors = []
    for stem in stems:
        path = ROUTES_DIR / f"{stem}.yaml"
        if not path.exists():
            errors.append((stem, "missing"))
            continue
        try:
            n = process_file(path)
            processed.append((stem, n))
            print(f"OK {stem}: {n} routes", file=sys.stderr)
        except Exception as e:
            errors.append((stem, repr(e)))
            print(f"ERR {stem}: {e}", file=sys.stderr)
    print(json.dumps({"processed": processed, "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
