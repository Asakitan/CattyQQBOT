# -*- coding: utf-8 -*-
"""v4: strict overlap, no single-char ctx, route-level overrides."""
import json, re

data = json.load(open('e:/VC/Catty/data/_batch23.json', 'r', encoding='utf-8'))

# Load SLUG_VOCAB / NAME_HINT_VOCAB / INTENT_FLAVOR from gen3 (head portion).
g3_src = open('e:/VC/Catty/data/_batch23_gen3.py', encoding='utf-8').read()
src_head = g3_src.split('def slug_of')[0]
ns = {}
exec(src_head, ns)
SLUG_VOCAB = ns['SLUG_VOCAB']
NAME_HINT_VOCAB = ns['NAME_HINT_VOCAB']
INTENT_FLAVOR = ns['INTENT_FLAVOR']

ROUTE_OVERRIDES = {
    'jvg_xianqi_008': ['受冷落', '被忽略', '没人爱', '心凉', '蜷起来', '呜'],
    'jv_bushuanghua_001': ['黑脸', '炸毛', '撇嘴', '甩袖', '不理人', '哼'],
    'emr_666_001': ['斗图回', '快速回', '梗回', '冒泡', '表情图', '嘿嘿'],
    'se_streetcat_001': ['流浪猫', '撸街猫', '蹲喂', '猫粮塞', '路边猫', '嘿嘿'],
    'pd_no_one_appreciate_001': ['老物件', '传家宝', '无人识', '审美差', '低估', '呜'],
    'dwr_taoxueshen_010': ['同步喝', '陪喝水', '一起干杯', '小约', '水友', '嘿嘿'],
    'mu_karaoke_009': ['麦霸', '点歌', '包房', 'K歌', '飙高音', '嘿嘿'],
    'hs_hot_003': ['防晒霜', '紫外线', '晒后', '冰镇', '伞', '呜'],
    'pb_bat_004': ['满电出门', '安全感', '一格回满', '电量足', '充满美', '嘿嘿'],
    'ofs_leavework_001': ['关电脑', '回家路', '解脱', '一身轻', '完工', '嘿嘿'],
    'rc_what_if_006': ['脑洞', '假想', '设想', '幻想', '推演', '怎么'],
    'se_lostsomething_001': ['翻包', '找半天', '不见了', '找回', '丢三落四', '呜'],
    'np_neck_pain_001': ['颈椎酸', '揉肩', '工伤', '低头多', '热敷', '呜'],
    'st_off_work_tired_002': ['下班后', '回家瘫', '解脱', '坐瘫', '一身疲', '呜'],
    'tf_body_exhausted_001': ['浑身酸', '骨头痛', '腰酸', '提不起劲', '瘫床', '呜'],
    'lb_bangong_001': ['工位', '会议', '社畜', '加班', '工作多', '呜'],
    'wtc_coldcold_001': ['哆嗦', '搓手', '暖宝', '裹紧', '冷飕飕', '呜'],
    'tot_tired_eyes_002': ['眼酸', '看屏久', '揉眼', '干涩', '闭眼歇', '呜'],
    'tf_too_tired_001': ['瘫床', '走不动', '一身疲', '提不起劲', '回血', '呜'],
    # supplement short ones (post-strict-filter)
    'meme_baolan_001': ['佛系', '咸鱼模式', '不想卷', '随便吧', '葛优瘫', '呜'],
    'nch_lost_001': ['空虚', '没目标', '迷路', '人生方向', '不知去向', '呜'],
    'bct_smelly_001': ['湿背心', '换衣服', '黏腻', '挤地铁', '体味', '呜'],
    'mfw_zaoanleng_001': ['被窝暖', '掀被子', '床贴床', '蹭枕', '懒洋洋', '呜'],
    'sla_shuibuzhao_005': ['眼皮重', '撑不住', '困但醒', '反复醒', '心累', '呜'],
    'ss_concentrate_fail_007': ['书桌前', '坐不住', '页码不动', '溜号', '走神发呆', '呜'],
    'zpr_stk_007': ['拉链坏', '修拉链', '蹲下', '尴尬走', '低头瞥', '呜'],
    'it_shimian_009': ['睡前胡思', '辗转反', '关灯还想', '脑回路', '心事多', '呜'],
    'hs_sum_001': ['蒸笼天', '湿黏', '出汗多', '中暑感', '想吹空调', '呜'],
    'hs_hot_003': ['防晒霜', '紫外线', '晒后', '冰镇', '遮阳伞', '呜'],
    'wcm_peiwo_001': ['想陪', '别走', '一起腻', '腻歪', '独处不行', '呜'],
    'ww_cold_010': ['哆嗦', '搓手', '暖宝宝', '裹紧', '玻璃冷', '呜'],
    'rn_runny_001': ['擤鼻涕', '纸巾团', '鼻头红', '吸鼻子', '抽纸', '呜'],
}

def slug_of(file_name):
    name = file_name.replace('.yaml', '')
    parts = name.split('_', 1)
    if len(parts) == 2 and re.match(r'^[A-Z]*\d+$', parts[0]):
        return parts[1]
    return name

def get_slug_vocab(slug):
    if slug in SLUG_VOCAB:
        return SLUG_VOCAB[slug][:]
    matches = []
    for k, v in SLUG_VOCAB.items():
        if k in slug or slug in k:
            matches.extend(v)
    seen = set(); out = []
    for w in matches:
        if w not in seen:
            seen.add(w); out.append(w)
    return out

def name_hints(route_name):
    out = []
    rn = route_name.lower()
    for k, v in NAME_HINT_VOCAB.items():
        if k in rn:
            out.extend(v)
    return out

def build_block(r):
    block = set()
    for kw in r['keywords']:
        block.add(kw.strip())
    for sib in r.get('siblings', []):
        block.add(sib.get('first_utterance', ''))
    return block

def is_blocked(w, block):
    if len(w) < 2:
        return True
    for b in block:
        if not b: continue
        if w == b: return True
        if w in b: return True
        if b in w: return True
    return False

results = []
short = []
for r in data:
    if r['name'] in ROUTE_OVERRIDES:
        ctx = ROUTE_OVERRIDES[r['name']]
        block = build_block(r)
        # still apply filter
        ctx = [w for w in ctx if not is_blocked(w, block)]
        if len(ctx) < 4:
            short.append((r['name'], 'OVERRIDE_SHORT', ctx, list(block)))
        results.append({'name': r['name'], 'ctx': ctx[:6]})
        continue
    slug = slug_of(r['file'])
    cand = get_slug_vocab(slug)
    cand.extend(name_hints(r['name']))
    cand.extend(INTENT_FLAVOR.get(r.get('intent', ''), []))
    block = build_block(r)
    cleaned = []
    seen = set()
    for w in cand:
        if w in seen: continue
        if is_blocked(w, block): continue
        seen.add(w)
        cleaned.append(w)
    if len(cleaned) < 4:
        short.append((r['name'], slug, cleaned, list(r['keywords'])))
    ctx = cleaned[:6]
    results.append({'name': r['name'], 'ctx': ctx})

print(f'total={len(results)} short={len(short)}')
for s in short:
    print(s)

# Re-check overlap
batch_kw = {b['name']: set(b['keywords']) for b in data}
bad = []
for r in results:
    kws = batch_kw[r['name']]
    for w in r['ctx']:
        if len(w) < 2:
            bad.append((r['name'], w, 'TOO_SHORT'))
        for kw in kws:
            if w == kw or w in kw or kw in w:
                bad.append((r['name'], w, 'OVERLAP', kw))
                break
print('bad:', len(bad))
for b in bad[:20]: print(b)

json.dump(results, open('e:/VC/Catty/data/_batch23_final.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('written final')
