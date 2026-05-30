# -*- coding: utf-8 -*-
"""
expand_yaml_r4_b0.py - round-4 batch 0
扩充 20 个 yaml 的 sub-route:
  keywords: >= 8
  utterances: >= 10
  responses: >= 8
笨猫语气模板池补充, 保留原 yaml 结构 (line-by-line, 不动注释/顺序)
"""
import re
import sys
from pathlib import Path

# ============== 笨猫语气模板池 ==============
# 占位 {kw} = sub-route 主题关键词 (取 keywords[0])
# 占位 {user_addr} = 用户地址

# 通用 utterances 模板 (短/中/长口语变体)
UTT_TEMPLATES = [
    "{kw}吗",
    "{kw}诶",
    "{kw}呢",
    "刚刚{kw}",
    "笨猫{kw}",
    "{user_addr}{kw}",
    "我{kw}",
    "突然{kw}",
    "又{kw}",
    "想{kw}",
    "{kw}哦",
    "在{kw}",
]

# 通用 responses 模板 (笨猫语气池, 4 类: 扑/嘿嘿/哼/尾巴)
RSP_TEMPLATES = [
    "(尾巴翘起来) 嗷呜～{user_addr}说{kw}~ 笨猫接住!",
    "嘿嘿～{user_addr}的{kw}笨猫记下了喵~",
    "(凑过去) 哼～{user_addr}这个{kw}~ 才不是笨猫想要的呢!",
    "{user_addr}的{kw}~ 笨猫贴贴蹭蹭ฅฅ",
    "(尾巴一甩) 嗷呜~ {user_addr}{kw}? 笨猫陪着!",
    "嘿嘿~ {user_addr}{kw}的样子~ 笨猫偷看了喵~",
    "(脸偏) 哼~ {user_addr}才不准笨猫{kw}呢!",
    "{user_addr}~ 笨猫也想{kw}啦嗷呜~ ฅฅ",
    "(爪爪抓住) 嗷呜~ {user_addr}的{kw}~ 笨猫不放!",
    "嘿嘿~ {kw}就{kw}~ {user_addr}陪笨猫嘛~",
    "(尾巴绕一圈) 哼~ {user_addr}的{kw}笨猫看在眼里~",
    "{user_addr}~ {kw}的时候记得叫笨猫一声喵~ ฅฅ",
]

# 通用 keywords 扩展模板 (基于主题词的同义/变体)
# 策略: 取每个 sub-route 现有 keywords 第一个, 加常见词尾/前缀变体
KW_SUFFIXES = ["了", "吧", "呢", "啊", "诶", "一下", "啦"]
KW_PREFIXES = ["要", "想", "求", "在", "又", "正在"]


def parse_sub_routes(lines):
    """切分 yaml 文本为 [header_comment_lines, [sub_route_blocks]]"""
    header = []
    blocks = []
    i = 0
    n = len(lines)
    # 头部注释
    while i < n and not lines[i].startswith("- name:"):
        header.append(lines[i])
        i += 1
    # sub-route 块: 从 "- name:" 开始到下一个 "- name:" 或文件尾
    cur = []
    while i < n:
        if lines[i].startswith("- name:") and cur:
            blocks.append(cur)
            cur = []
        cur.append(lines[i])
        i += 1
    if cur:
        blocks.append(cur)
    return header, blocks


def find_field_range(block, field):
    """在 sub-route block 内找到 field (keywords/utterances/responses) 的起止行索引
    返回 (field_line_idx, last_item_idx) 或 (None, None)
    field_line_idx: 包含 'field: [...]' 或 'field:' 的那行
    last_item_idx: 最后一个列表项行 (对 inline 数组就是 field_line_idx)
    """
    for idx, line in enumerate(block):
        m = re.match(r"^(\s*)" + field + r":\s*(.*)$", line)
        if m:
            indent, rest = m.group(1), m.group(2).strip()
            # inline: keywords: [a, b, c]
            if rest.startswith("[") and rest.endswith("]"):
                return idx, idx, "inline", indent
            # multi-line: 后续以 indent + "  - " 开头
            last = idx
            j = idx + 1
            child_indent = indent + "  "
            while j < len(block):
                ln = block[j]
                if ln.strip() == "":
                    j += 1
                    continue
                if ln.startswith(child_indent + "- "):
                    last = j
                    j += 1
                else:
                    break
            return idx, last, "block", indent
    return None, None, None, None


def parse_inline_array(s):
    """解析 [a, b, c, "d"] -> ['a','b','c','d'] (粗糙但够用)"""
    s = s.strip()
    assert s.startswith("[") and s.endswith("]")
    inner = s[1:-1]
    if not inner.strip():
        return []
    # 用简单的 quote-aware split
    items = []
    buf = []
    in_q = False
    q_char = ""
    for ch in inner:
        if in_q:
            if ch == q_char:
                in_q = False
            buf.append(ch)
        else:
            if ch in ('"', "'"):
                in_q = True
                q_char = ch
                buf.append(ch)
            elif ch == ",":
                items.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
    if buf:
        items.append("".join(buf).strip())
    return [strip_quote(x) for x in items if x.strip()]


def strip_quote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def parse_block_list(block, start_idx, end_idx, indent):
    """从 multi-line list 解析项内容"""
    child_indent = indent + "  - "
    items = []
    for j in range(start_idx + 1, end_idx + 1):
        ln = block[j]
        if ln.startswith(child_indent):
            raw = ln[len(child_indent):].rstrip("\n").rstrip()
            items.append(strip_quote(raw))
    return items


def needs_quote(s):
    """yaml inline 值是否需要双引号"""
    s = s.strip()
    if not s:
        return True
    # 含特殊字符
    if s[0] in "{[\"'!&*?|>%@`#,":
        return True
    if ":" in s or "#" in s:
        return True
    return False


def emit_keywords_inline(indent, kws):
    """生成 keywords: [a, b, ...]  全部按需 quote"""
    parts = []
    for k in kws:
        if needs_quote(k):
            esc = k.replace('"', '\\"')
            parts.append(f'"{esc}"')
        else:
            parts.append(k)
    return f"{indent}keywords: [{', '.join(parts)}]\n"


def emit_block_list(indent, field, items, quote_all=False):
    """生成 multi-line list:
    field:
      - item1
      - item2
    """
    lines = [f"{indent}{field}:\n"]
    child = indent + "  - "
    for it in items:
        if quote_all or needs_quote(it):
            esc = it.replace('"', '\\"')
            lines.append(f'{child}"{esc}"\n')
        else:
            lines.append(f"{child}{it}\n")
    return lines


def expand_keywords(existing, target=8):
    """补充 keywords 到 target 个"""
    out = list(existing)
    if not out:
        return out
    seed = out[0]
    candidates = []
    # 加后缀
    for sfx in KW_SUFFIXES:
        candidates.append(seed + sfx)
    # 加前缀
    for pfx in KW_PREFIXES:
        candidates.append(pfx + seed)
    # 短化 (取前 2 字)
    if len(seed) > 2:
        candidates.append(seed[:2])
    # 加 "笨猫" 变体
    candidates.append("笨猫" + seed)
    # 去重 & 不能已存在
    for c in candidates:
        if len(out) >= target:
            break
        if c not in out:
            out.append(c)
    return out


def expand_utterances(existing, kw, target=10):
    out = list(existing)
    if not out:
        return out
    # 用主题词模板补
    for tmpl in UTT_TEMPLATES:
        if len(out) >= target:
            break
        candidate = tmpl.format(kw=kw, user_addr="{user_addr}")
        if candidate not in out:
            out.append(candidate)
    return out


def expand_responses(existing, kw, target=8):
    out = list(existing)
    for tmpl in RSP_TEMPLATES:
        if len(out) >= target:
            break
        candidate = tmpl.format(kw=kw, user_addr="{user_addr}")
        if candidate not in out:
            out.append(candidate)
    return out


def utt_needs_quote(s):
    """utterances 行首是 { 必须 quote (yaml flow mapping 解析错)"""
    s = s.strip()
    if not s:
        return True
    if s.startswith("{"):
        return True
    return needs_quote(s)


def process_sub_route(block):
    """处理一个 sub-route block, 扩充 keywords/utterances/responses"""
    new_block = list(block)

    # keywords (inline)
    k_idx, k_end, k_type, k_indent = find_field_range(new_block, "keywords")
    if k_idx is None:
        return new_block
    # 解析现有 keywords
    line = new_block[k_idx]
    m = re.match(r"^(\s*)keywords:\s*(.*)$", line)
    rest = m.group(2).strip()
    if k_type == "inline":
        kws = parse_inline_array(rest)
    else:
        kws = parse_block_list(new_block, k_idx, k_end, k_indent)
    # 取主题词
    if not kws:
        return new_block
    theme = kws[0]
    # 扩充
    new_kws = expand_keywords(kws, target=8)

    # utterances
    u_idx, u_end, u_type, u_indent = find_field_range(new_block, "utterances")
    if u_idx is None:
        return new_block
    if u_type == "block":
        utts = parse_block_list(new_block, u_idx, u_end, u_indent)
    else:
        utts = parse_inline_array(re.match(r"^(\s*)utterances:\s*(.*)$", new_block[u_idx]).group(2).strip())
    new_utts = expand_utterances(utts, theme, target=10)

    # responses
    r_idx, r_end, r_type, r_indent = find_field_range(new_block, "responses")
    if r_idx is None:
        return new_block
    if r_type == "block":
        rsps = parse_block_list(new_block, r_idx, r_end, r_indent)
    else:
        rsps = parse_inline_array(re.match(r"^(\s*)responses:\s*(.*)$", new_block[r_idx]).group(2).strip())
    new_rsps = expand_responses(rsps, theme, target=8)

    # 重新组装: 按 field 行索引从大到小替换 (这样改后前面的索引不会乱)
    # 1) responses
    rsp_lines = []
    for r in new_rsps:
        esc = r.replace('"', '\\"')
        rsp_lines.append(f'{r_indent}  - "{esc}"\n')
    rsp_block = [f"{r_indent}responses:\n"] + rsp_lines
    new_block = new_block[:r_idx] + rsp_block + new_block[r_end + 1:]

    # 重新查询 u_idx (responses 改了行数, 但 u_idx < r_idx 不受影响)
    # 2) utterances
    utt_lines = []
    for u in new_utts:
        if utt_needs_quote(u):
            esc = u.replace('"', '\\"')
            utt_lines.append(f'{u_indent}  - "{esc}"\n')
        else:
            utt_lines.append(f"{u_indent}  - {u}\n")
    utt_block = [f"{u_indent}utterances:\n"] + utt_lines
    new_block = new_block[:u_idx] + utt_block + new_block[u_end + 1:]

    # 3) keywords (inline 重新生成)
    new_block[k_idx] = emit_keywords_inline(k_indent, new_kws)

    return new_block


def process_file(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    header, blocks = parse_sub_routes(lines)
    new_lines = list(header)
    for b in blocks:
        new_b = process_sub_route(b)
        new_lines.extend(new_b)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


STEMS = [
    "363_head_pat_beg", "364_hug_beg_request", "365_kiss_tease_blush",
    "366_dodge_flirt_back", "367_water_drink_remind", "368_wear_warm_remind",
    "369_eat_well_remind", "370_sleep_early_remind", "371_surprise_redpack_joy",
    "372_sudden_compliment", "373_moon_round_night", "374_starry_sky_chat",
    "375_meow_daily_chatter", "376_tail_stepped_yelp", "377_owner_home_excite",
    "378_boss_check_panic", "379_office_gossip_chat", "380_grocery_dropped_oops",
    "381_lost_thing_panic", "382_sudden_wanna_cry",
]


def main():
    root = Path("e:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes")
    done = []
    skip = []
    for s in STEMS:
        p = root / f"{s}.yaml"
        if not p.exists():
            skip.append((s, "missing"))
            continue
        try:
            process_file(p)
            done.append(s)
        except Exception as e:
            skip.append((s, f"err: {e}"))
    print(f"DONE {len(done)}/{len(STEMS)}")
    print("done:", done)
    if skip:
        print("skip:", skip)


if __name__ == "__main__":
    main()
