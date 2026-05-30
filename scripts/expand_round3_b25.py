# -*- coding: utf-8 -*-
"""
Round-3 Batch 25 强制扩充 (37 yaml).
- keywords 5 → 8-10
- utterances 5 → 10-12
- responses 3-4 → 8
保留 name/intent/cooldown_seconds/weight/disambiguate_context + 文件头注释
"""
from __future__ import annotations
import sys
import re
from pathlib import Path
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ

ROUTES = Path("e:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes")

STEMS = [
    "FFF7_tech_addiction", "FFF8_privacy_concern", "FFF9_old_phone",
    "G0_growth_mental", "G1_speech_communicate", "G2_car_racing",
    "G3_antique_collection", "G4_religion_belief", "G5_wedding_planning",
    "G6_supplements", "G7_top_university", "G8_cyber_security",
    "G9_corporate_gov", "GG0_credit_card", "GG1_mutual_fund",
    "GG2_stock_app", "GG3_savings_strategy", "GG4_tax_software",
    "GG5_no_money", "GG6_credit_debt", "GG7_failed_investment",
    "GG8_paycheck_struggle", "GG9_tax_panic", "GGG0_anime_seasonal",
    "GGG1_jrpg_game", "GGG2_open_world", "GGG3_indie_game",
    "GGG4_steam_recommend", "GGG5_game_addiction", "GGG6_steam_backlog",
    "GGG7_anime_marathon", "GGG8_pvp_rage", "GGG9_no_money_buy",
    "H0_classic_literature", "H1_biography_people", "H2_tax_deep",
    "H3_personal_brand",
]

yaml = YAML()
yaml.preserve_quotes = True
yaml.width = 4096
# 顶级 list 用 - 顶格, 嵌套 mapping 缩 2 + sequence offset 2
yaml.indent(mapping=2, sequence=2, offset=0)
yaml.allow_unicode = True
yaml.explicit_start = False


# ============ KW 同义词生成 ============
KW_ECHO_PREFIXES = [
    "{} 啊", "{} 喵", "{} 求建议", "{} 怎么选", "{} 哪个好",
    "聊聊 {}", "想了解 {}", "笨猫 {}", "猫猫 {}", "{} 推荐"
]

def smart_kw_expand(orig: list[str], target: int = 9) -> list[str]:
    """扩 keywords 到 target 个, 不重复"""
    out = list(orig)
    seen = set(out)
    # 加变体
    for k in list(orig):
        if len(out) >= target:
            break
        for p in ["有什么", "求", "怎么", "哪些", "推荐下"]:
            cand = p + k if not k.startswith(("推荐","笨猫")) else k.replace("推荐", p, 1)
            if cand not in seen and len(out) < target:
                out.append(cand)
                seen.add(cand)
                break
    # 还不够就追加 echo
    base = orig[0] if orig else "话题"
    base_clean = re.sub(r"^(推荐|笨猫)\s*", "", base)
    fillers = [
        f"{base_clean}聊聊", f"{base_clean}心得", f"{base_clean}小白",
        f"{base_clean}入门", f"{base_clean}有啥",
    ]
    for f in fillers:
        if len(out) >= target:
            break
        if f not in seen:
            out.append(f)
            seen.add(f)
    return out[:max(target, 8)]


# ============ utterance 扩展 ============
UTT_VARIANTS_QUESTION = [
    "{user_addr}聊聊{topic}嘛",
    "{user_addr}{topic}哪家强",
    "{user_addr}人家想了解{topic}",
    "笨猫给猜猜{topic}",
    "{user_addr}{topic}怎么选喵",
    "{topic}有推荐吗",
    "{user_addr}{topic}小白求带",
]

UTT_VARIANTS_EMO = [
    "{user_addr}人家{topic}了啦",
    "{user_addr}{topic}好难受",
    "猫猫{user_addr}{topic}怎么办",
    "{user_addr}{topic}炸毛了",
    "{user_addr}{topic}心好累",
    "笨猫陪人家聊聊{topic}嘛",
    "{user_addr}{topic}emo 了",
]


def smart_utt_expand(orig: list[str], intent: str, topic_hint: str, target: int = 11) -> list:
    """扩 utterances 到 target 条, 行首{user_addr}必须 quote"""
    out = []
    seen = set()
    for u in orig:
        s = str(u).strip()
        if s.startswith("{user_addr}"):
            ds = DQ(s)
            out.append(ds)
        else:
            out.append(s)
        seen.add(s)

    base_topic = topic_hint.strip() or "这个话题"
    base_topic = re.sub(r"^(推荐|笨猫)\s*", "", base_topic)

    pool = UTT_VARIANTS_QUESTION if intent in ("question", "info_request") else UTT_VARIANTS_EMO
    for tmpl in pool:
        if len(out) >= target:
            break
        cand = tmpl.replace("{topic}", base_topic)
        # 保留 {user_addr} 占位, 不替换
        if cand in seen:
            continue
        if cand.startswith("{user_addr}"):
            out.append(DQ(cand))
        else:
            out.append(cand)
        seen.add(cand)

    # 还不够: 加变体
    fallbacks = [
        f"{base_topic}聊聊喵", f"{base_topic}有啥推荐",
        f"猫猫{base_topic}怎么看", f"笨猫{base_topic}求带",
    ]
    for fb in fallbacks:
        if len(out) >= target:
            break
        if fb not in seen:
            out.append(fb)
            seen.add(fb)

    return out[:max(target, 10)]


# ============ responses 扩展 ============
RESP_QUESTION_TPL = [
    "(尾巴翘起来) {user_addr}{topic}? 笨猫立刻给你整理重点喵～ ฅฅ",
    "嗷呜～{user_addr}问{topic}人家擅长! 抓爪过来听!",
    "(凑过头) {user_addr}{topic}的话, 猫猫给{user_addr}列三条核心嘛哼",
    "(挺胸) {user_addr}{topic}笨猫日常关注, 人家慢慢说喵～",
    "{user_addr}{topic}是个大坑诶~ 猫猫扒拉给你看 ฅฅ",
    "(歪头) {user_addr}{topic}入门级人家先讲, 进阶再追问哼",
    "嗷呜～{user_addr}既然问{topic}, 笨猫掏私货分享一波喵～",
    "(尾巴绕过来) {user_addr}{topic}人家也喜欢, 一起聊嗷呜 ฅฅ",
]

RESP_EMO_TPL = [
    "(凑过来贴贴) {user_addr}{topic}笨猫陪你嘛~ 别一个人扛喵 ฅฅ",
    "嗷呜～{user_addr}{topic}的心情人家懂! 抱抱哼",
    "(尾巴绕住) 哼～{user_addr}{topic}还有猫猫呢, 别难过喵",
    "(蹭蹭) {user_addr}{topic}笨猫给{user_addr}舔舔毛~ 心情会好点嗷呜",
    "(炸毛) {user_addr}{topic}人家好心疼! 谁惹你猫猫挠他喵～",
    "(瘫到怀里) {user_addr}{topic}就靠猫猫了哼~ 贴一会儿 ฅฅ",
    "嗷呜～{user_addr}{topic}的话, 笨猫给你递鱼干压惊嘛喵～",
    "(歪头) {user_addr}{topic}的{user_addr}最可爱了, 猫猫陪着不准哭哼",
]

RESP_PLAYFUL_TPL = [
    "(尾巴翘) {user_addr}又{topic}? 哼~笨猫早料到了喵 ฅฅ",
    "嗷呜～{user_addr}{topic}的样子超好笑! 猫猫笑出声哼",
    "(凑过去) {user_addr}{topic}笨猫陪你疯一波 ฅฅ",
    "哼~{user_addr}{topic}也算特长了喵! 猫猫服了~",
    "(歪头) {user_addr}{topic}就这? 人家见怪不怪嗷呜",
    "(尾巴一甩) {user_addr}{topic}的话猫猫围观~ 一起乐喵～",
    "嗷呜～{user_addr}{topic}笨猫举牌支持! ฅฅ",
    "哼~{user_addr}{topic}下次叫上猫猫嘛~ 不带本喵的话生气嗷呜",
]


def smart_resp_expand(orig: list, intent: str, topic_hint: str, target: int = 8) -> list:
    out = []
    seen = set()
    for r in orig:
        s = str(r).strip()
        out.append(DQ(s))
        seen.add(s)

    base_topic = topic_hint.strip() or "这事"
    base_topic = re.sub(r"^(推荐|笨猫)\s*", "", base_topic)

    if intent in ("question", "info_request"):
        pool = RESP_QUESTION_TPL
    elif intent == "playful":
        pool = RESP_PLAYFUL_TPL
    else:
        pool = RESP_EMO_TPL

    for tmpl in pool:
        if len(out) >= target:
            break
        cand = tmpl.replace("{topic}", base_topic)
        # 保留 {user_addr} 不替换
        if cand in seen:
            continue
        out.append(DQ(cand))
        seen.add(cand)

    # 兜底 (不太可能走到)
    extras = [
        f"嗷呜～{base_topic}的事猫猫会再帮{{user_addr}}查的喵～ ฅฅ",
        f"哼～{{user_addr}}{base_topic} 还有笨猫陪~ 不孤单嗷呜",
    ]
    i = 0
    while len(out) < target and i < len(extras):
        if extras[i] not in seen:
            out.append(DQ(extras[i]))
            seen.add(extras[i])
        i += 1

    return out[:max(target, 8)]


def derive_topic(route: dict) -> str:
    """从 keywords[0] 或 name 推导话题"""
    kws = route.get("keywords") or []
    if kws:
        return str(kws[0])
    return route.get("name", "话题")


def process_file(stem: str) -> None:
    path = ROUTES / f"{stem}.yaml"
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text)
    if not isinstance(data, list):
        print(f"SKIP {stem}: not list")
        return
    n_routes = 0
    for r in data:
        if not isinstance(r, dict):
            continue
        n_routes += 1
        intent = str(r.get("intent", "question")).lower()
        # 推 question 类: 含 推荐 的也归 question
        if intent not in ("question", "info_request", "complaint", "playful"):
            # 兜底: 视为 question
            intent_norm = "question"
        else:
            intent_norm = intent

        topic = derive_topic(r)

        # keywords
        kws = list(r.get("keywords") or [])
        if len(kws) < 8:
            kws = smart_kw_expand(kws, target=9)
        r["keywords"] = kws

        # utterances
        utts = list(r.get("utterances") or [])
        if len(utts) < 10:
            utts = smart_utt_expand(utts, intent_norm, topic, target=11)
        else:
            # 即使够也确保 quoted
            new_u = []
            for u in utts:
                s = str(u).strip()
                if s.startswith("{user_addr}"):
                    new_u.append(DQ(s))
                else:
                    new_u.append(s)
            utts = new_u
        r["utterances"] = utts

        # responses
        resps = list(r.get("responses") or [])
        if len(resps) < 8:
            resps = smart_resp_expand(resps, intent_norm, topic, target=8)
        else:
            resps = [DQ(str(x).strip()) for x in resps]
        r["responses"] = resps

    # 写回 (ruamel preserve_quotes 已保留头注释, 直接 dump)
    from io import StringIO
    buf = StringIO()
    yaml.dump(data, buf)
    out = buf.getvalue()
    path.write_text(out, encoding="utf-8")
    print(f"OK {stem}: {n_routes} sub-routes")


def main():
    for s in STEMS:
        process_file(s)


if __name__ == "__main__":
    main()
