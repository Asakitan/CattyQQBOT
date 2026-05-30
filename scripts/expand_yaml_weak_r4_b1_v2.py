"""
Round-4 batch-1 v2 expansion (round-4 final boost for the 20 weak yamls):
- keywords: 8 -> 10 (add 2 more synonyms/variants)
- utterances: 10 -> 12 (add 2 more spoken variants)
- responses: 8 -> 10 (add 2 more Catty-tone variants)

Strategy: parse with PyYAML, append new variants WITHOUT duplicating, then
re-render manually to keep stylistic consistency with the existing files
(flow-style keywords list, double-quoted utterances/responses, header preserved).
"""
from __future__ import annotations
import sys
from pathlib import Path

import yaml

ROUTES_DIR = Path(r"E:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes")

STEMS = [
    "363_head_pat_beg", "364_hug_beg_request", "365_kiss_tease_blush",
    "366_dodge_flirt_back", "367_water_drink_remind", "368_wear_warm_remind",
    "369_eat_well_remind", "370_sleep_early_remind", "371_surprise_redpack_joy",
    "372_sudden_compliment", "373_moon_round_night", "374_starry_sky_chat",
    "375_meow_daily_chatter", "376_tail_stepped_yelp", "377_owner_home_excite",
    "378_boss_check_panic", "379_office_gossip_chat", "380_grocery_dropped_oops",
    "381_lost_thing_panic", "382_sudden_wanna_cry",
]

# Bencat-tone response templates by intent (round-4 v2 boost pool)
# Each entry brings: optional action paren, {user_addr} hook, tsundere/cat tail
RESP_TPL_V2 = {
    "playful": [
        "(尾巴轻轻打圈) 嗷呜~ {user_addr}你这招笨猫见过~ 不过还是接住啦喵~",
        "嘿嘿～{user_addr}这一下~ 笨猫差点被逗笑~ 哼才不会笑呢ฅฅ",
        "(凑过去顶脑袋) {user_addr}~ 笨猫陪你疯一下~ 杂鱼也可爱哼喵~",
        "{user_addr}这种小心思~ 笨猫一眼看穿~ 不过还是要嗷呜～ฅฅ",
    ],
    "complaint": [
        "(尾巴绕一圈) 嗷呜~ {user_addr}笨猫蹲旁边~ 你慢慢说喵~",
        "嘿嘿～{user_addr}这点小事别钻牛角~ 笨猫给你顺顺毛ฅฅ",
        "(小爪子盖手背) 哼～{user_addr}别一个人扛~ 笨猫在呢嗷呜~",
        "{user_addr}的烦躁~ 笨猫接走一半喵~ 剩下一起扛ฅฅ",
    ],
    "question": [
        "(歪头叉腰) 嗷呜~ {user_addr}问得好~ 笨猫脑子开转嗷呜~",
        "嘿嘿～{user_addr}的问题笨猫记下了~ 让笨猫想想喵~ ฅฅ",
        "(尾巴指方向) 哼～{user_addr}笨猫给方向~ 别走偏喵~",
        "{user_addr}的疑问~ 笨猫拆成小块讲~ 别急嗷呜~ฅฅ",
    ],
}


def expand_keywords_v2(existing: list) -> list:
    """8 -> 10: append 2 more variants per route.

    Use波浪号(~)变体 + 全角猫尾变体, 避免和 b1 已用的 '啦'/'嗷' 冲突。
    """
    base = list(existing)
    seen = set(base)
    extras: list[str] = []

    # Strategy A: '喵' suffix (cat-tail variant), unique
    for kw in base:
        if len(base) + len(extras) >= 10:
            break
        v = kw + "喵"
        if v not in seen:
            extras.append(v)
            seen.add(v)

    # Strategy B: '~' suffix (tilde variant)
    for kw in base:
        if len(base) + len(extras) >= 10:
            break
        v = kw + "~"
        if v not in seen:
            extras.append(v)
            seen.add(v)

    # Strategy C: '一下' suffix (frequency-friendly variant) as final fallback
    for kw in base:
        if len(base) + len(extras) >= 10:
            break
        v = kw + "一下"
        if v not in seen:
            extras.append(v)
            seen.add(v)

    return base + extras


def expand_utterances_v2(existing: list) -> list:
    """10 -> 12: append 2 more spoken variants per route.

    Use:
    - 嘿{user_addr} + base
    - {user_addr} 主语前缀 + base + 喵尾
    Avoid duplicates with b1 already-injected variants.
    """
    base = list(existing)
    seen = set(base)
    extras: list[str] = []

    # Strategy: pick first 'base' utterance (the simplest one) and produce 2 spoken variants
    # then loop until we reach >=12
    candidates_seed = [u for u in base if not u.startswith("{user_addr}")]
    if not candidates_seed:
        candidates_seed = list(base)

    for u in candidates_seed:
        if len(base) + len(extras) >= 12:
            break
        v = "嘿{user_addr}" + u
        if v not in seen:
            extras.append(v)
            seen.add(v)

    for u in candidates_seed:
        if len(base) + len(extras) >= 12:
            break
        v = "{user_addr}" + u + "喵"
        if v not in seen:
            extras.append(v)
            seen.add(v)

    # Pad with universal filler if needed
    pad_pool = [
        "{user_addr}人家说~",
        "{user_addr}笨猫这边",
        "诶{user_addr}",
        "{user_addr}嗷呜~",
        "{user_addr}笨猫问你",
    ]
    for p in pad_pool:
        if len(base) + len(extras) >= 12:
            break
        if p not in seen:
            extras.append(p)
            seen.add(p)

    return base + extras


def expand_responses_v2(existing: list, intent: str) -> list:
    """8 -> 10: append 2 more Catty-tone responses per route."""
    base = list(existing)
    seen = set(base)
    extras: list[str] = []

    pool = RESP_TPL_V2.get(intent, RESP_TPL_V2["playful"])
    for t in pool:
        if len(base) + len(extras) >= 10:
            break
        if t not in seen:
            extras.append(t)
            seen.add(t)

    # Cross-intent fallback if pool exhausted
    if len(base) + len(extras) < 10:
        for other_intent, other_pool in RESP_TPL_V2.items():
            for t in other_pool:
                if len(base) + len(extras) >= 10:
                    break
                if t not in seen:
                    extras.append(t)
                    seen.add(t)
            if len(base) + len(extras) >= 10:
                break

    return base + extras


def fmt_yaml_string(s: str) -> str:
    """Double-quote a YAML string with proper escaping."""
    s2 = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + s2 + '"'


def render_yaml(routes: list, header_lines: list) -> str:
    out = []
    out.extend(header_lines)
    if header_lines and not header_lines[-1].endswith("\n"):
        out.append("")

    for i, r in enumerate(routes):
        if i > 0:
            out.append("")
        out.append(f"- name: {r['name']}")
        out.append(f"  intent: {r['intent']}")
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
        w = r["weight"]
        if isinstance(w, float) and w == int(w):
            out.append(f"  weight: {w:.1f}")
        else:
            out.append(f"  weight: {w}")
        if r.get("disambiguate_context"):
            out.append(f"  disambiguate_context: {r['disambiguate_context']}")

    return "\n".join(out) + "\n"


def process_file(stem: str) -> tuple[bool, str]:
    fp = ROUTES_DIR / f"{stem}.yaml"
    if not fp.exists():
        return False, f"missing: {fp}"

    raw = fp.read_text(encoding="utf-8")
    lines = raw.splitlines()
    header = []
    for ln in lines:
        if ln.startswith("#") or ln.strip() == "":
            header.append(ln)
        else:
            break

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

        new_kws = expand_keywords_v2(kws)
        new_utts = expand_utterances_v2(utts)
        new_resps = expand_responses_v2(resps, intent)

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

    # trim trailing empty lines in header
    while header and header[-1].strip() == "":
        header.pop()
    rendered = render_yaml(new_routes, header)
    fp.write_text(rendered, encoding="utf-8")
    return True, f"{stem}: {len(new_routes)} sub-routes boosted"


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
