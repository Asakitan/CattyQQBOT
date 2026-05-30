# -*- coding: utf-8 -*-
"""Round-4 batch 8 强制扩充: 20 个 yaml stem
- keywords 5 -> 8+ (>=8)
- utterances 5 -> 10+ (>=10)
- responses 4 -> 8+ (>=8)
保留 name/intent/cooldown_seconds/weight/disambiguate_context/文件头注释/sub-route 数量
策略: 通用扩展池 + 从 sub 现有 keywords 抽 seed 生成上下文化条目
"""
import json, os, re, sys
import yaml

BATCH_JSON = 'e:/VC/Catty/data/expand_yaml_weak_round4.json'
ROUTES_DIR = 'e:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes'

stems = json.load(open(BATCH_JSON, 'r', encoding='utf-8'))[8]
print(f"[batch 8] {len(stems)} stems: {stems}")

# ===== 通用笨猫风格 response 池 (按 intent 大致分) =====
RESP_GENERIC = {
    'playful': [
        '(尾巴翘到天) 嗷呜～{user_addr}笨猫陪你玩喵 ฅฅ',
        '(凑头蹭) 哼～{user_addr}才不许把笨猫晾着',
        '(扑过来) 嘿嘿～{user_addr}笨猫想到的都跟你说嗷呜',
        '哼~ 才不是因为{user_addr}才开心呢 (但尾巴摇成风扇) 喵~',
        '(瘫怀里) 一起一起~ {user_addr}这种事笨猫最喜欢啦贴贴',
        '(歪头眨眼) 嗷呜～笨猫盯着{user_addr}哦, 别耍赖喵',
        '(尾巴拍拍) {user_addr}笨蛋~ 笨猫给你提个建议嗷ฅฅ',
        '(蹦蹦) wow～笨猫激动到原地转圈, {user_addr}快接住',
    ],
    'complaint': [
        '(尾巴炸毛) 嗷呜～谁敢欺负{user_addr}! 笨猫扑过去咬 ฅฅ',
        '(扑过来安慰) 别气啦{user_addr}~ 笨猫帮你出气喵~',
        '哼~ 这种事最讨厌啦! 笨猫陪你一起骂嗷呜~',
        '(钻怀里) 难过的话钻笨猫这里嘛~ 暖暖的 ฅฅ',
        '(贴贴) 跟笨猫吐槽嘛{user_addr}~ 越说越爽快喵呜~',
        '哼~ 早跟你说了嘛{user_addr}笨蛋~ (但其实心疼)',
        '(尾巴拍床) 气死了气死了! 笨猫跟你同步炸毛~',
        '(凑近) 来~ 笨猫给{user_addr}揉揉~ 别堵心啦',
    ],
    'question': [
        '(歪头) 嗷呜～{user_addr}问笨猫? 让人家想想喵~',
        '(尾巴绕) 哼~ 这种问题笨猫超会的! 听好啦~',
        '(凑近) {user_addr}这事啊~ 笨猫给你分析嗷ฅฅ',
        '(瘫怀里) 笨猫陪你慢慢想嘛~ 别急喵呜~',
        '(尾巴翘) 嘿嘿~ {user_addr}问对人了! 笨猫专业 ฅฅ',
        '哼~ 才不是不知道呢! (但耳朵竖着想了想)',
        '(贴脸) 笨猫给你出主意嘛~ {user_addr}先抱抱',
        '(凑头看{user_addr}) 嗷呜~ 一起想办法! 笨猫不让你一个人 ฅฅ',
    ],
    'praise': [
        '(尾巴卷) 嗷呜～{user_addr}夸我嘛~ 笨猫就棒喵ฅฅ',
        '哼~ 又不是因为{user_addr}夸才开心呢! (但已经凑过去了)',
        '(扑) 夸夸我! 笨猫一整天都开心嗷呜~',
        '(凑头蹭) 嘿嘿~ {user_addr}最懂笨猫了 喵~',
    ],
    'sad': [
        '(扑过来抱住) {user_addr}别难过~ 笨猫在喵呜~',
        '(尾巴轻拍) 心疼死了~ 抱抱{user_addr} ฅฅ',
        '哼~ 谁惹的?! 笨猫去咬他喵~',
        '(钻怀里) 别哭啦~ 笨猫陪你 贴贴~',
    ],
}

# ===== 通用 utterance 后缀模板 =====
UTT_SUFFIX_TEMPLATES = [
    '{seed}',
    '{user_addr}{seed}',
    '{seed}嘛',
    '{seed}喵',
    '哎呀{seed}',
    '突然{seed}',
    '我{seed}',
    '你看{seed}',
    '今天{seed}',
    '又{seed}',
    '人家{seed}',
    '笨猫{seed}',
    '{seed}怎么办',
    '{seed}了',
]

# ===== keyword 扩展模板 =====
KW_SUFFIX_TEMPLATES = [
    '{seed}', '{seed}了', '{seed}啦', '{seed}的', '{seed}了啊',
    '又{seed}', '突然{seed}', '老{seed}', '总{seed}', '一直{seed}',
]

def extract_core(text):
    """从已有 keyword/utt 中抽核心词 (去掉 {user_addr} 占位 + 标点)"""
    s = re.sub(r'\{[^}]+\}', '', text).strip()
    s = re.sub(r'^[啊呀啦了哦呢嗷喵～~,。\s]+', '', s)
    s = re.sub(r'[啊呀啦了哦呢嗷喵～~,。\s]+$', '', s)
    return s

def expand_keywords(sub):
    kws = list(sub.get('keywords', []) or [])
    if len(kws) >= 8:
        return kws
    seen = set(kws)
    # 用现有 kw 作 seed 派生
    seeds = [extract_core(k) for k in kws if len(extract_core(k)) >= 2]
    for seed in seeds:
        for tpl in KW_SUFFIX_TEMPLATES:
            cand = tpl.format(seed=seed)
            if len(cand) >= 2 and cand not in seen:
                kws.append(cand); seen.add(cand)
                if len(kws) >= 9: break
        if len(kws) >= 9: break
    # 补到至少 8
    fallback = ['好烦', '咋办', '帮帮', '主人看看', '猫猫陪']
    for f in fallback:
        if len(kws) >= 8: break
        if f not in seen:
            kws.append(f); seen.add(f)
    return kws[:10]

def expand_utterances(sub):
    utts = list(sub.get('utterances', []) or [])
    if len(utts) >= 10:
        return utts
    seen = set(utts)
    seeds = []
    for u in utts:
        s = extract_core(u)
        if len(s) >= 2:
            seeds.append(s)
    # 也用 keywords 作 seed
    for k in sub.get('keywords', []) or []:
        s = extract_core(k)
        if len(s) >= 2 and s not in seeds:
            seeds.append(s)
    # 派生
    for seed in seeds:
        for tpl in UTT_SUFFIX_TEMPLATES:
            cand = tpl.format(seed=seed, user_addr='{user_addr}')
            if cand and cand not in seen:
                utts.append(cand); seen.add(cand)
                if len(utts) >= 11: break
        if len(utts) >= 11: break
    # 兜底
    fallback = [
        '{user_addr}怎么办喵',
        '猫猫帮忙嘛',
        '主人快来',
        '人家想跟你说',
        '哎呀又这样',
    ]
    for f in fallback:
        if len(utts) >= 10: break
        if f not in seen:
            utts.append(f); seen.add(f)
    return utts[:12]

def expand_responses(sub):
    resps = list(sub.get('responses', []) or [])
    if len(resps) >= 8:
        return resps
    seen = set(resps)
    intent = sub.get('intent', 'playful')
    pool = RESP_GENERIC.get(intent, RESP_GENERIC['playful'])
    # 直接套通用池
    for r in pool:
        if len(resps) >= 8: break
        if r not in seen:
            resps.append(r); seen.add(r)
    # 不够就跨池
    if len(resps) < 8:
        for other in RESP_GENERIC.values():
            for r in other:
                if len(resps) >= 8: break
                if r not in seen:
                    resps.append(r); seen.add(r)
            if len(resps) >= 8: break
    return resps[:10]

def emit_yaml(data, header):
    KEY_ORDER = ['name','intent','keywords','utterances','responses',
                 'cooldown_seconds','weight','disambiguate_context']
    out_lines = [header.rstrip()]
    for sub in data:
        block_lines = []
        name_val = sub.get('name', '')
        block_lines.append(f'- name: {name_val}')
        for k in KEY_ORDER:
            if k == 'name' or k not in sub:
                continue
            v = sub[k]
            if isinstance(v, list):
                block_lines.append(f'  {k}:')
                for item in v:
                    s = str(item)
                    esc = s.replace('\\', '\\\\').replace('"', r'\"')
                    block_lines.append(f'    - "{esc}"')
            elif isinstance(v, dict):
                # disambiguate_context 字典
                block_lines.append(f'  {k}:')
                for kk, vv in v.items():
                    if isinstance(vv, list):
                        block_lines.append(f'    {kk}:')
                        for vi in vv:
                            esc = str(vi).replace('"', r'\"')
                            block_lines.append(f'      - "{esc}"')
                    else:
                        block_lines.append(f'    {kk}: {vv}')
            else:
                block_lines.append(f'  {k}: {v}')
        # 兜底未列出的 key
        for k, v in sub.items():
            if k in KEY_ORDER:
                continue
            if isinstance(v, list):
                block_lines.append(f'  {k}:')
                for item in v:
                    esc = str(item).replace('"', r'\"')
                    block_lines.append(f'    - "{esc}"')
            else:
                block_lines.append(f'  {k}: {v}')
        out_lines.append('\n'.join(block_lines))
    return '\n\n'.join(out_lines).rstrip() + '\n'

# ===== 主循环 =====
processed = []
skipped = []

for stem in stems:
    path = os.path.join(ROUTES_DIR, f'{stem}.yaml')
    if not os.path.exists(path):
        skipped.append((stem, 'not found'))
        continue
    raw = open(path, 'r', encoding='utf-8').read()
    header_lines = []
    for line in raw.splitlines():
        if line.startswith('#') or line.strip() == '':
            header_lines.append(line)
        else:
            break
    header = '\n'.join(header_lines).rstrip() + '\n\n'

    data = yaml.safe_load(raw)
    if not isinstance(data, list):
        skipped.append((stem, 'not list'))
        continue

    sub_count_before = len(data)
    for sub in data:
        sub['keywords'] = expand_keywords(sub)
        sub['utterances'] = expand_utterances(sub)
        sub['responses'] = expand_responses(sub)
    sub_count_after = len(data)
    assert sub_count_before == sub_count_after, f"{stem} sub count changed"

    out = emit_yaml(data, header)
    open(path, 'w', encoding='utf-8').write(out)
    processed.append(stem)
    print(f'[OK] {stem} ({sub_count_after} subs)')

print(f'\n=== summary ===')
print(f'processed: {len(processed)}/{len(stems)}')
if skipped:
    print(f'skipped: {skipped}')
