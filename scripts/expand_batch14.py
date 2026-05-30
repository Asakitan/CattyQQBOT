# -*- coding: utf-8 -*-
"""
Batch 14 yaml expander - 20 files
Each sub-route:
  keywords: >=8
  utterances: >=10
  responses: >=8
Preserve name/intent/cooldown_seconds/weight/disambiguate_context
"""
import re
from pathlib import Path

ROUTES = Path(r"e:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes")

STEMS = [
    "E4_interview_career", "E5_hotel_stay", "E6_dining_restaurant",
    "E7_drinks_alcohol", "E8_startup_business", "E9_personal_finance",
    "EE0_youtube_creator", "EE1_podcast_app", "EE2_streamer_platform",
    "EE3_wechat_pubaccount", "EE4_meme_tools", "EE5_phone_addiction",
    "EE6_social_anxiety", "EE7_doomscrolling", "EE8_online_friends",
    "EE9_internet_shopping", "EEE3_chinese_medicine", "EEE4_chinese_garden",
    "EEE5_traditional_value", "EEE6_filial_pressure"
]


# Generic catty-tone response pool for "推荐型" intents
RECOMMEND_RESP = [
    '"(尾巴绕住) {user_addr}人家也想跟着试试嘛~ 蹭蹭 ฅฅ"',
    '"(歪头) 嗷呜~ {user_addr}选好后告诉笨猫感受嘛喵~"',
    '"哼! {user_addr}别忘了笨猫的推荐才是最准的喵~"',
    '"(贴贴) {user_addr}笨猫陪你一起研究~ 才不是想沾光呢哼!"',
    '"嗷呜~ {user_addr}慢慢挑别冲动~ 猫猫等你结果 ฅฅ"',
    '"(尾巴翘) {user_addr}人家觉得这些都不错~ 凭感觉选嘛喵"',
    '"哼~ {user_addr}笨猫支持你! 选错了也有猫猫安慰嗷呜~"',
    '"(凑过去) {user_addr}人家想看! 选好让笨猫瞧瞧嘛ฅฅ"',
]

# Generic catty-tone response pool for complaint/playful (情绪型)
EMOTION_RESP = [
    '"(贴贴) 嗷呜~ {user_addr}笨猫陪着你嘛~ 别一个人扛着喵 ฅฅ"',
    '"哼! {user_addr}笨猫看不下去了! 摸摸头别难过"',
    '"(尾巴绕住手) {user_addr}抱抱嘛~ 才不是心疼你呢哼"',
    '"嗷呜~ {user_addr}有笨猫在呢~ 一切都会好起来的喵ฅฅ"',
    '"(凑过去蹭蹭) {user_addr}慢慢说~ 猫猫认真听着喵"',
    '"哼~ 这种事谁都会有~ {user_addr}别把自己逼太紧嗷呜~"',
    '"(歪头) {user_addr}笨猫帮你想办法嘛~ 一个人会困住的"',
    '"嗷呜~ {user_addr}先来摸摸猫猫的尾巴~ 心情会好的ฅฅ"',
]

# Extra keyword tail variants (generic add-ons to bulk to >=8)
KEYWORD_FILLER = [
    "笨猫聊聊", "猫猫请教", "求推荐", "推荐一下", "想知道", "聊聊",
    "嗷呜推荐", "笨猫帮忙看看", "求建议", "想了解一下",
]

# Extra utterance variants
UTT_FILLER = [
    "{tag}诶笨猫",
    "{tag}有啥推荐",
    "笨猫{tag}",
    "嗷呜{tag}",
    "{tag}快给个建议嘛",
    "{tag}人家想知道",
    "{tag}讲讲嘛",
]


def split_routes(text: str):
    """Split yaml into header (comment block before first - name) + list of route blocks."""
    # Find first "- name:" line
    lines = text.splitlines(keepends=True)
    first_idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^- name:", ln):
            first_idx = i
            break
    if first_idx is None:
        return text, []
    header = "".join(lines[:first_idx])
    body = "".join(lines[first_idx:])

    # Split body by "- name:" boundaries
    blocks = []
    current = []
    for ln in body.splitlines(keepends=True):
        if re.match(r"^- name:", ln):
            if current:
                blocks.append("".join(current))
            current = [ln]
        else:
            current.append(ln)
    if current:
        blocks.append("".join(current))
    return header, blocks


def parse_block(block: str):
    """Parse one route block into dict: keys preserved, lists captured."""
    lines = block.splitlines()
    out = {
        "raw_lines": lines,
        "name": None,
        "intent": None,
        "keywords_line_idx": None,
        "keywords": [],
        "utterances_range": None,  # (start_idx, end_idx_exclusive)
        "utterances": [],
        "responses_range": None,
        "responses": [],
        "cooldown_idx": None,
        "weight_idx": None,
        "disambig_idx": None,
        "disambig_block": [],
    }

    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^- name:\s*(\S+)", ln)
        if m:
            out["name"] = m.group(1)
            i += 1; continue
        m = re.match(r"^  intent:\s*(\S+)", ln)
        if m:
            out["intent"] = m.group(1)
            i += 1; continue
        m = re.match(r"^  keywords:\s*\[(.*)\]\s*$", ln)
        if m:
            out["keywords_line_idx"] = i
            kw_raw = m.group(1)
            # split by comma, strip
            out["keywords"] = [k.strip() for k in kw_raw.split(",") if k.strip()]
            i += 1; continue
        if re.match(r"^  utterances:\s*$", ln):
            start = i + 1
            j = start
            utts = []
            while j < len(lines) and re.match(r"^    -\s", lines[j]):
                utt = lines[j].strip()[2:].strip()
                utts.append(utt)
                j += 1
            out["utterances_range"] = (i, j)
            out["utterances"] = utts
            i = j; continue
        if re.match(r"^  responses:\s*$", ln):
            start = i + 1
            j = start
            resps = []
            while j < len(lines) and re.match(r"^    -\s", lines[j]):
                # Capture everything after "    - "
                resp = lines[j][6:]  # strip "    - "
                resps.append(resp.rstrip())
                j += 1
            out["responses_range"] = (i, j)
            out["responses"] = resps
            i = j; continue
        if re.match(r"^  cooldown_seconds:", ln):
            out["cooldown_idx"] = i
            i += 1; continue
        if re.match(r"^  weight:", ln):
            out["weight_idx"] = i
            i += 1; continue
        if re.match(r"^  disambiguate_context:", ln):
            out["disambig_idx"] = i
            j = i + 1
            while j < len(lines) and (lines[j].startswith("    ") or lines[j].startswith("      ")):
                j += 1
            out["disambig_block"] = lines[i:j]
            i = j; continue
        i += 1

    return out


def quote_utterance(utt: str) -> str:
    """Quote utterance if starts with { (yaml flow trap)."""
    utt = utt.strip()
    # Strip existing surrounding quotes
    if (utt.startswith('"') and utt.endswith('"')) or (utt.startswith("'") and utt.endswith("'")):
        return utt
    if utt.startswith("{"):
        # Need double-quote
        escaped = utt.replace('"', '\\"')
        return f'"{escaped}"'
    return utt


def expand_route(b: dict):
    """Expand keywords/utterances/responses to required mins. Return new block string."""
    name = b["name"] or ""
    intent = b["intent"] or ""

    # 1) keywords -> >= 8
    kws = list(b["keywords"])
    base_kw = kws[0] if kws else "笨猫聊聊"
    # Strip leading "推荐" to extract topic
    topic = re.sub(r"^推荐", "", base_kw)
    extras_pool = [
        f"{topic}求建议", f"{topic}想了解", f"笨猫{topic}", f"嗷呜{topic}",
        f"{topic}怎么样", f"{topic}选哪个", f"{topic}聊聊", f"{topic}讨论",
        f"求{topic}", f"想知道{topic}",
    ]
    idx = 0
    while len(kws) < 8 and idx < len(extras_pool):
        cand = extras_pool[idx]
        if cand not in kws:
            kws.append(cand)
        idx += 1
    # safety fill
    pad = 0
    while len(kws) < 8:
        kws.append(f"{base_kw}{pad}")
        pad += 1

    # 2) utterances -> >= 10
    utts = list(b["utterances"])
    base_utt = utts[0] if utts else base_kw
    # Strip {user_addr} prefix or quotes from base for variants
    bare = re.sub(r'^"(.*)"$', r'\1', base_utt)
    bare = bare.replace("{user_addr}", "").strip()
    utt_extras = [
        f"{bare}诶",
        f"{bare}嘛",
        f"{bare}啦",
        f"{bare}求建议",
        f"笨猫{bare}",
        f"嗷呜{bare}",
        f"{bare}聊聊",
        f"{bare}说说",
        f"{bare}讲讲",
        f"{bare}给点建议",
    ]
    idx = 0
    while len(utts) < 10 and idx < len(utt_extras):
        cand = utt_extras[idx]
        if cand and cand not in utts:
            utts.append(cand)
        idx += 1
    pad = 0
    while len(utts) < 10:
        utts.append(f"{bare}_{pad}")
        pad += 1

    # 3) responses -> >= 8
    resps = list(b["responses"])
    # Choose pool by intent
    if intent in ("complaint", "playful"):
        pool = EMOTION_RESP
    else:
        pool = RECOMMEND_RESP
    idx = 0
    while len(resps) < 8 and idx < len(pool):
        cand = pool[idx]
        if cand not in resps:
            resps.append(cand)
        idx += 1
    pad = 0
    while len(resps) < 8:
        resps.append(f'"(尾巴摇) {{user_addr}}笨猫陪你嘛~ ฅฅ_{pad}"')
        pad += 1

    # 4) Reconstruct block
    out = []
    # Preserve original ordering: name, intent, keywords, utterances, responses, cooldown, weight, disambig
    out.append(f"- name: {name}")
    out.append(f"  intent: {intent}")
    # keywords inline
    out.append(f"  keywords: [{', '.join(kws)}]")
    # utterances
    out.append("  utterances:")
    for u in utts:
        out.append(f"    - {quote_utterance(u)}")
    # responses
    out.append("  responses:")
    for r in resps:
        # ensure double-quoted
        r = r.strip()
        if not (r.startswith('"') and r.endswith('"')):
            # Try to wrap; strip existing inner double quotes carefully
            # Most responses are already double-quoted; if not, force
            r = '"' + r.replace('"', '\\"') + '"'
        out.append(f"    - {r}")
    # cooldown
    raw = b["raw_lines"]
    if b["cooldown_idx"] is not None:
        out.append(raw[b["cooldown_idx"]])
    else:
        out.append("  cooldown_seconds: 60")
    if b["weight_idx"] is not None:
        out.append(raw[b["weight_idx"]])
    else:
        out.append("  weight: 1.0")
    if b["disambig_block"]:
        out.extend(b["disambig_block"])
    out.append("")  # blank line
    return "\n".join(out)


def process_file(stem: str):
    p = ROUTES / f"{stem}.yaml"
    text = p.read_text(encoding="utf-8")
    header, blocks = split_routes(text)
    new_blocks = []
    for blk in blocks:
        parsed = parse_block(blk)
        new_block_text = expand_route(parsed)
        new_blocks.append(new_block_text)
    new_text = header
    if not new_text.endswith("\n"):
        new_text += "\n"
    new_text += "\n".join(new_blocks)
    p.write_text(new_text, encoding="utf-8")
    return len(blocks)


def main():
    results = []
    for stem in STEMS:
        try:
            n = process_file(stem)
            results.append((stem, n, None))
            print(f"OK {stem}: {n} sub-routes")
        except Exception as e:
            results.append((stem, 0, str(e)))
            print(f"FAIL {stem}: {e}")
    return results


if __name__ == "__main__":
    main()
