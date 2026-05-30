# -*- coding: utf-8 -*-
"""
Batch 53 yaml 内容扩充
- keywords: 5 -> 8-10
- utterances: 5 -> 10-12
- responses: 3-4 -> 8
保留 name / intent / cooldown_seconds / weight / disambiguate_context / 文件头注释
"""
import io
import os
import re
import sys
import random
from pathlib import Path

ROUTES = Path(r"e:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes")

BATCH = [
    'UU1_portrait_photo', 'UU2_street_photo', 'UU3_wildlife_photo', 'UU4_macro_photo',
    'UU5_photo_failed', 'UU6_no_inspiration_photo', 'UU7_camera_envy', 'UU8_camera_broken',
    'UU9_photo_judge', 'UUU0_ev_brand_china', 'UUU1_oil_car_luxury', 'UUU2_car_accessory',
    'UUU3_dashcam', 'UUU4_car_audio', 'UUU5_car_loan_pressure', 'UUU6_car_steal',
    'UUU7_parking_dispute', 'UUU8_drunk_driving_fear', 'UUU9_road_rage',
    'V0_christmas_gifts', 'V1_chinese_new_year', 'V2_new_year_eve', 'V3_valentine_gift',
    'V4_halloween', 'V5_monday_blues', 'V6_overtime_rage', 'V7_weekend_lazy',
    'V8_want_vacation', 'V9_want_change_job', 'VV0_floor_choice'
]

# ----- 解析 yaml (简单状态机, 不依赖 PyYAML 以保持手写格式控制) -----

def parse_yaml(text):
    """返回 (header_comment_lines, sub_routes_list)
    每个 sub_route: dict with name, intent, keywords(list), utterances(list),
                   responses(list), cooldown_seconds, weight, disambiguate_context(可选 list or dict)
    """
    lines = text.splitlines()
    header = []
    i = 0
    while i < len(lines):
        s = lines[i]
        if s.strip() == "" or s.lstrip().startswith("#"):
            header.append(s)
            i += 1
        else:
            break

    routes = []
    cur = None
    cur_field = None  # 当前正在收的 list field
    field_indent = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.rstrip()
        stripped = raw.strip()
        if stripped == "" and cur is None:
            i += 1
            continue
        # 新 sub-route 开头
        m = re.match(r"^- name:\s*(.+)$", stripped)
        if m:
            if cur is not None:
                routes.append(cur)
            cur = {
                'name': m.group(1).strip(),
                'intent': None,
                'keywords': [],
                'utterances': [],
                'responses': [],
                'cooldown_seconds': 60,
                'weight': 1.0,
                'disambiguate_context': None,
                '_disambig_raw': [],
            }
            cur_field = None
            i += 1
            continue
        # 字段
        m = re.match(r"^\s+(\w+):\s*(.*)$", raw)
        if m and cur is not None:
            key = m.group(1)
            val = m.group(2).strip()
            cur_field = None
            if key == 'intent':
                cur['intent'] = val
            elif key == 'cooldown_seconds':
                try:
                    cur['cooldown_seconds'] = int(val)
                except:
                    cur['cooldown_seconds'] = 60
            elif key == 'weight':
                try:
                    cur['weight'] = float(val)
                except:
                    cur['weight'] = 1.0
            elif key == 'keywords':
                # 可能是 inline [a, b, c] 或下面列表
                if val.startswith('['):
                    inner = val.strip('[]').strip()
                    # 简单 split
                    items = [x.strip().strip('"').strip("'") for x in inner.split(',') if x.strip()]
                    cur['keywords'] = items
                else:
                    cur_field = 'keywords'
            elif key == 'utterances':
                cur_field = 'utterances'
            elif key == 'responses':
                cur_field = 'responses'
            elif key == 'disambiguate_context':
                cur_field = 'disambiguate_context'
                cur['_disambig_raw'].append(raw)
            else:
                # 其他字段(如 disambiguate context 嵌套) - 保留原文
                if cur_field == 'disambiguate_context':
                    cur['_disambig_raw'].append(raw)
            i += 1
            continue
        # 列表项 - 字符串
        m = re.match(r"^(\s+)-\s+(.+)$", raw)
        if m and cur is not None and cur_field in ('keywords', 'utterances', 'responses'):
            val = m.group(2).strip()
            # 去 quote
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            cur[cur_field].append(val)
            i += 1
            continue
        # disambig 嵌套, 全部原文存
        if cur_field == 'disambiguate_context' and cur is not None:
            cur['_disambig_raw'].append(raw)
            i += 1
            continue
        i += 1
    if cur is not None:
        routes.append(cur)
    return header, routes


# ----- 笨猫语气素材池 -----

ACTIONS_POSITIVE = [
    "尾巴翘", "凑过来", "扑过去", "挺胸", "歪头", "眼睛发亮",
    "蹭蹭手", "摇尾", "贴脸", "扑上去", "钻怀里", "凑头",
    "尾巴绕过去", "贴贴", "踮脚", "爪子搭上去", "尾巴扫脸",
]
ACTIONS_NEG = [
    "瘫倒", "尾巴垂", "尾巴炸毛", "扶额", "挠头", "撇嘴",
    "尾巴卷起", "耳朵贴下来", "摇头", "叹气", "眯眼", "瘫桌",
]
ACTIONS_NEUTRAL = [
    "歪头看屏幕", "尾巴摇", "歪头", "竖耳朵", "扒着看",
    "凑过去看", "尾巴一甩", "爪子托腮", "捧脸",
]
TSUN = ["哼", "才不是", "又不是", "明明就", "哼哼", "哼~"]
CAT_TAIL = ["喵～", "嗷呜～", "ฅฅ", "喵呜", "贴贴", "蹭蹭", "喵!", "嗷呜!"]
SELF = ["笨猫", "猫猫", "人家", "米雪儿", "笨猫我"]

random.seed(20260530)

def _pick(seq):
    return random.choice(seq)

def _u(addr=True):
    return "{user_addr}" if addr else ""


# ----- 关键字扩展 -----

VARIANT_PATTERNS = [
    ("推荐", ["推荐", "想要", "求", "求推荐", "帮我选", "帮看看", "给我推", "讲讲"]),
    ("怎么", ["怎么", "咋", "怎么办", "肿么"]),
    ("好烦", ["好烦", "烦死", "烦死了", "好烦啊"]),
    ("废了", ["废了", "完蛋", "翻车", "凉了", "炸了"]),
]

def expand_keywords(base):
    """5->8-10. 基于原词生成简写/同义/网络变体"""
    seen = list(base)
    seen_set = set(s.lower() for s in seen)

    def add(w):
        wl = w.lower()
        if wl not in seen_set and 1 < len(w) < 20:
            seen.append(w)
            seen_set.add(wl)

    for k in list(base):
        if "推荐" in k:
            base_term = k.replace("推荐", "").strip()
            if base_term:
                add("求推荐" + base_term)
                add("讲讲" + base_term)
                add(base_term + "推荐")
        if "笨猫" in k:
            base_term = k.replace("笨猫", "").strip()
            if base_term:
                add("猫猫" + base_term)
        if "好" in k and len(k) <= 6:
            add(k.replace("好", "超"))
        if "了" in k and len(k) <= 6:
            add(k.replace("了", "啦"))

    # 一直补齐到 8-10
    target = random.randint(8, 10)
    # 第一波 fallback: 截短 + 尾段
    for k in base:
        if len(seen) >= target: break
        if len(k) >= 4:
            add(k[:3])
            add(k[-3:])
            if len(k) >= 6:
                add(k[:4])
                add(k[1:])
    # 第二波 fallback: 加 啦/啊 变体
    SUFS = ["啦", "啊", "嘛", "了"]
    for k in base:
        if len(seen) >= target: break
        for suf in SUFS:
            if len(seen) >= target: break
            if not any(k.endswith(s) for s in SUFS):
                add(k + suf)
    # 第三波 fallback: 拼接, 万一还不够
    for i, k in enumerate(base):
        if len(seen) >= target: break
        if i + 1 < len(base):
            add(k + base[i+1][:2])
    # 兜底: 至少 8
    if len(seen) < 8:
        for k in base:
            if len(seen) >= 8: break
            for s in ["?","!","~","哦"]:
                if len(seen) >= 8: break
                add(k + s)
    return seen[:target if len(seen) >= 8 else max(8, len(seen))]


# ----- utterances 扩展 -----

def expand_utterances(base, name, intent):
    """5 -> 10-12 口语变体, 长短句混搭"""
    seen = list(base)
    seen_set = set(s.strip() for s in seen)
    target = random.randint(10, 12)

    SUFFIXES = ["啊", "呢", "嘛", "啦", "呀", "呜呜"]
    EXCLAMATIONS = ["真的", "太", "好", "也", "又"]

    def add(w):
        w = w.strip()
        if w and w not in seen_set and len(w) < 40:
            seen.append(w)
            seen_set.add(w)

    # 选 base 里短句作短词种子
    shortest = min(base, key=len) if base else ""

    # 1) 每个 base 句配 1 个语气尾 (轮转, 不全部叠在第一句)
    for idx, s in enumerate(base):
        if len(seen) >= target: break
        suf = SUFFIXES[idx % len(SUFFIXES)]
        add(s + suf)

    # 2) 短句 (整词截取) - 不切断单词
    for s in base:
        if len(seen) >= target: break
        # 按 空格/标点 切, 取前两个有意义的词
        toks = re.split(r"[ ，,。.!?！？]", s)
        toks = [t for t in toks if t]
        if len(toks) >= 2:
            add(toks[0] + toks[1][:3])
        elif len(s) >= 8:
            # 长句切成前 2/3
            add(s[:len(s)*2//3])

    # 3) 感叹句
    for s in base:
        if len(seen) >= target: break
        if not s.endswith(("!", "！", "?", "？")):
            add(s + "!")

    # 4) 口语化 (加"诶/卧槽/天哪/真的" 等开头)
    PREFIXES = ["诶", "卧槽", "天哪", "真的", "我去", "呜呜", ""]
    for s in base:
        if len(seen) >= target: break
        p = random.choice(PREFIXES)
        if p:
            add(p + " " + s if not s.startswith(("{",)) else p + s)

    # 5) "..."/拖音
    for s in base:
        if len(seen) >= target: break
        add(s + "...")

    # 6) 双叠 (基础 + 啦/吧)
    for s in base:
        if len(seen) >= target: break
        if not s.endswith(("啊", "啦", "呢")):
            add(s + "吧")

    return seen[:target]


# ----- responses 扩展 -----

SENSITIVE_TOKENS = ("临终", "丧失", "抑郁", "哭", "心痛", "心碎", "想哭", "难过", "心疼")
RECOMMEND_TOKENS = ("推荐",)

def is_sensitive(name, intent, keywords):
    text = " ".join([name or "", intent or ""] + list(keywords))
    return any(t in text for t in SENSITIVE_TOKENS)

def is_recommend(name, intent, keywords):
    if intent == "question":
        text = " ".join([name or ""] + list(keywords))
        return any(t in text for t in RECOMMEND_TOKENS)
    return False


def expand_responses(base, name, intent, keywords):
    """3-4 -> 8. 关键: 推荐型保留密度, 情绪型走傲娇/反撩/共情"""
    seen = list(base)
    seen_set = set(s.strip() for s in seen)
    target = 8

    def add(w):
        w = w.strip()
        if w and w not in seen_set:
            seen.append(w)
            seen_set.add(w)

    sensitive = is_sensitive(name, intent, keywords)
    recommend = is_recommend(name, intent, keywords)

    if recommend:
        # 推荐型: 保留原 3-4 条信息密度, 再加 4-5 条带笨猫语气的补充
        templates = [
            "({act_p}) {self}给{addr}补一条 - {kw}也是宝藏喵～ฅฅ",
            "({act_p}) {addr}选的时候挑性价比就稳了嗷呜～",
            "(凑头) {addr}要不要再多说点细节~ {self}帮你挑喵～",
            "(尾巴翘) {addr}入手前看看评价嘛~ {self}提醒过哦!",
            "嗷呜～{addr}追加一条: 别只看价格还要看口碑喵!",
            "(挺胸) {self}的小爪子已经帮{addr}收藏好啦贴贴~",
            "({act_neu}) {addr}这领域水挺深的~ {self}陪你慢慢选~",
            "哼! 才不是怕{addr}买错才叮嘱的呢~ 是{self}的专业素养!",
            "(歪头) {addr}定了哪款记得跟{self}说一声嘛喵～ฅฅ",
            "嗷呜～{addr}买回来给{self}看看! 人家也想见识见识ฅฅ",
            "({act_p}) {addr}下次去店里带{self}一起~ 帮你把关嘛喵!",
            "(尾巴一甩) {addr}别冲动消费啊~ {self}给你按住爪子!",
        ]
    elif sensitive:
        # 敏感型: 收敛傲娇, 重共情
        templates = [
            "(轻轻凑过去) {addr}…{self}陪着你, 不用一个人扛喵～ฅฅ",
            "(尾巴软软盖在手上) {addr}难过的话就跟{self}说嘛, 耳朵借你嗷呜～",
            "嗷呜～{addr}{self}知道很难… 抱抱蹭蹭, 一点点来好吗",
            "(凑过去贴脸) {addr}哭出来吧, 哭完{self}给你擦眼泪喵～",
            "(尾巴绕过去) {addr}{self}在的, 哪儿也不去ฅฅ",
            "嗷呜～{addr}不用强撑啦, 痛的时候有{self}陪着就行喵～",
            "(轻轻蹭手) {addr}慢慢来嘛, {self}的时间都是你的贴贴~",
            "{addr}{self}虽然帮不了什么大忙, 但抱抱永远在ฅฅ",
            "(凑头) {addr}今天先这样, 早点休息, {self}陪你一整晚嗷呜～",
            "嗷呜～{addr}{self}最讨厌看见你难过了… 陪你度过这阵嘛~",
            "(尾巴贴在手背) {addr}先深呼吸一下嘛, {self}在喵～",
            "嗷呜～{addr}不哭不哭… {self}的呼噜声送给你蹭蹭",
        ]
    else:
        # 通用情绪互动型: 傲娇 + 反撩 + 动作 + 猫系尾
        templates = [
            "({act_n}) {tsun}~ {addr}的事{self}都接住了喵～ฅฅ",
            "({act_p}) 嗷呜～{addr}快过来~ {self}给你蹭一下嗷呜!",
            "{tsun}! {addr}这种小事算什么~ {self}一爪子帮你解决喵～",
            "({act_p}) {addr}是不是又想{self}了~ 才不是{self}主动凑过来的呢哼!",
            "嗷呜～{addr}{self}站你这边! 谁让你不开心{self}就挠他贴贴~",
            "({act_neu}) {addr}慢慢说嘛~ {self}的尾巴当你的耳机线~",
            "{tsun}! 又不是{self}非要管{addr}的~ …只是顺便而已嗷呜~",
            "({act_p}) {addr}抱抱! {self}的怀抱营业中 ฅฅ",
            "嗷呜～{addr}说的{self}都听见了! 才不是装没听到~ 哼!",
            "({act_n}) {addr}别这样{self}心疼~ …{tsun}才不是真心疼呢喵～",
            "{tsun}~ {self}早就猜到{addr}要这么说! …好啦贴贴一下喵!",
            "({act_p}) {addr}过来过来~ {self}的尾巴给你抱~",
            "({act_neu}) 哼~ {addr}还在嘟囔~ {self}给你揉揉脑袋嗷呜",
            "嗷呜～{addr}爪子伸出来~ {self}帮你按一按吧贴贴ฅฅ",
        ]

    # 从模板填充
    random.shuffle(templates)
    main_kw = keywords[0] if keywords else "这事儿"
    for tpl in templates:
        if len(seen) >= target:
            break
        try:
            r = tpl.format(
                addr="{user_addr}",
                self=_pick(SELF),
                act_p=_pick(ACTIONS_POSITIVE),
                act_n=_pick(ACTIONS_NEG),
                act_neu=_pick(ACTIONS_NEUTRAL),
                tsun=_pick(TSUN),
                kw=main_kw,
            )
        except KeyError:
            continue
        add(r)

    return seen[:target]


# ----- yaml 写出 (手动控制引号/缩进) -----

def emit_keyword(k):
    # inline 数组里, 含特殊符号就 quote
    if any(c in k for c in [":", "#", "{", "}", "[", "]", ",", "?", "!"]):
        return '"' + k.replace('"', '\\"') + '"'
    return k

def emit_utt(u):
    # utterances 行首是 {user_addr}xxx 必须 quote
    # 出于安全, 含特殊字符也 quote
    if u.startswith("{") or any(c in u for c in [":", "#", "[", "]", ",", "?"]):
        return '"' + u.replace('"', '\\"') + '"'
    return u

def emit_response(r):
    # responses 全部双引号包裹
    return '"' + r.replace('\\', '\\\\').replace('"', '\\"') + '"'


def emit_sub_route(r):
    out = []
    out.append(f"- name: {r['name']}")
    if r.get('intent'):
        out.append(f"  intent: {r['intent']}")
    # keywords inline
    kw_str = ", ".join(emit_keyword(k) for k in r['keywords'])
    out.append(f"  keywords: [{kw_str}]")
    # utterances 列表
    out.append("  utterances:")
    for u in r['utterances']:
        out.append(f"    - {emit_utt(u)}")
    # responses 列表
    out.append("  responses:")
    for resp in r['responses']:
        out.append(f"    - {emit_response(resp)}")
    out.append(f"  cooldown_seconds: {r['cooldown_seconds']}")
    # weight: int 友好
    w = r['weight']
    if w == int(w):
        out.append(f"  weight: {w:.1f}")
    else:
        out.append(f"  weight: {w}")
    # disambig 原文
    if r.get('_disambig_raw'):
        for line in r['_disambig_raw']:
            out.append(line)
    return "\n".join(out)


def process_file(stem):
    fp = ROUTES / f"{stem}.yaml"
    if not fp.exists():
        return False, f"missing {fp}"
    text = fp.read_text(encoding='utf-8')
    header, routes = parse_yaml(text)
    if not routes:
        return False, f"no routes parsed in {fp}"

    for r in routes:
        try:
            r['keywords'] = expand_keywords(r['keywords'])
            r['utterances'] = expand_utterances(r['utterances'], r['name'], r['intent'] or "")
            r['responses'] = expand_responses(r['responses'], r['name'], r['intent'] or "", r['keywords'])
        except Exception as e:
            return False, f"expand fail on {r['name']}: {e}"

    # 组装
    parts = []
    if header:
        parts.append("\n".join(header).rstrip())
    parts.append("")
    for i, r in enumerate(routes):
        parts.append(emit_sub_route(r))
        parts.append("")
    out = "\n".join(parts).rstrip() + "\n"
    fp.write_text(out, encoding='utf-8')
    return True, f"{stem}: {len(routes)} sub-routes"


def main():
    ok = 0
    fail = []
    for stem in BATCH:
        success, msg = process_file(stem)
        print(msg)
        if success:
            ok += 1
        else:
            fail.append(msg)
    print(f"---\n{ok}/{len(BATCH)} done")
    if fail:
        print("FAILS:")
        for f in fail:
            print(" -", f)


if __name__ == "__main__":
    main()
