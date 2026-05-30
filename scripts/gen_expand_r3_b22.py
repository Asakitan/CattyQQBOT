# -*- coding: utf-8 -*-
"""
Round-3 batch_idx=22 强制扩充 37 个 yaml.
每个 sub-route:
  keywords: 8-10
  utterances: 10-12
  responses: 8
保留 name/intent/cooldown_seconds/weight/disambiguate_context.
"""
import os
import re
import sys
import yaml as pyyaml
from pathlib import Path

BASE = Path(r'e:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes')

STEMS = [
    'C6_productivity_tools', 'C7_legal_deep', 'C8_slang_meme', 'C9_video_create',
    'CC0_resume_template', 'CC1_interview_skill', 'CC2_business_book', 'CC3_office_software',
    'CC4_career_planning', 'CC5_interview_anxiety', 'CC6_first_day_job', 'CC7_office_politics',
    'CC8_job_burnout', 'CC9_promotion_fail', 'CCC0_dog_breed_deep', 'CCC1_cat_breed_deep',
    'CCC2_pet_grooming', 'CCC3_pet_training', 'CCC4_pet_health', 'CCC5_pet_lost',
    'CCC6_pet_sick', 'CCC7_pet_died', 'CCC8_pet_misbehavior', 'CCC9_pet_jealous',
    'D0_psychology_deep', 'D1_language_learn', 'D2_outdoor_adventure', 'D3_academic_research',
    'D4_history_event', 'D5_astrology_mystic', 'D6_science_news', 'D7_agriculture_rural',
    'D8_gift_scene', 'D9_subculture', 'DD0_lego_set', 'DD1_jigsaw_puzzle', 'DD2_party_board_game',
]


def extract_header_comment(text):
    """提取文件头部以 # 开头的连续行 + 紧跟的空行."""
    lines = text.splitlines()
    out = []
    for line in lines:
        if line.startswith('#') or line.strip() == '':
            out.append(line)
            # 一旦遇到 yaml 内容就停
            if not line.startswith('#') and line.strip() == '':
                # 空行: 检查下一个是否仍是注释
                continue
        else:
            break
    # 去尾部空行后保留一行空行
    while out and out[-1].strip() == '':
        out.pop()
    return '\n'.join(out) + '\n\n' if out else ''


def load_routes(path):
    """加载 yaml -> list of dict (保持原顺序)."""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    header = extract_header_comment(text)
    data = pyyaml.safe_load(text)
    assert isinstance(data, list), f'{path} not a list'
    return header, data


# ============ 内容生成器 =================
# 笨猫语气元素池
ACTIONS = [
    '(扑过来)', '(凑过来)', '(尾巴翘)', '(尾巴软软搭)', '(歪头)', '(爪爪叉腰)',
    '(挺胸)', '(贴贴)', '(蹭蹭)', '(凑头)', '(瘫倒)', '(钻被窝)', '(尾巴卷手腕)',
    '(软软贴)', '(耳朵动)', '(眨眼)', '(尾巴一甩)', '(小爪子拍桌)', '(凑过去闻闻)',
]
TAILS = ['喵～', '嗷呜～', '喵呜', 'ฅฅ', '贴贴', '蹭蹭～', '哼～', '喵！', '喵呜～']
TSUN = ['哼', '才不是', '才不需要', '又不是', '又不想', '人家才不…呢', '杂鱼']
SELF = ['人家', '猫猫', '笨猫']


def make_user_addr_quoted_utterance(s):
    """utterance 行首 {user_addr} 必须 quote."""
    s = s.strip()
    if s.startswith('{user_addr}'):
        return f'"{s}"'
    # 否则可不 quote, 但为统一保险, 不含特殊字符不 quote
    if any(c in s for c in [':', '#', '{', '}', '[', ']', ',', '&', '*', '!', '|', '>', '<', '?', '%', '@', '`']):
        return f'"{s.replace(chr(34), chr(92)+chr(34))}"'
    return s


# 各 yaml 的主题元数据 (sub-route name -> (核心词, 扩充模板类型))
# 模板类型: 'rec' = 推荐型, 'chat' = 闲聊互动型
# 通用扩充使用 STEM_CFG 提供的主题, 否则用原 keywords[0]
STEM_CFG = {
    # 推荐型 (rec)
    'C6_productivity_tools': 'rec',
    'C7_legal_deep': 'rec',
    'C8_slang_meme': 'rec',
    'C9_video_create': 'rec',
    'CC0_resume_template': 'rec',
    'CC1_interview_skill': 'rec',
    'CC2_business_book': 'rec',
    'CC3_office_software': 'rec',
    'CC4_career_planning': 'rec',
    'CC5_interview_anxiety': 'chat',
    'CC6_first_day_job': 'chat',
    'CC7_office_politics': 'chat',
    'CC8_job_burnout': 'chat',
    'CC9_promotion_fail': 'chat',
    'CCC0_dog_breed_deep': 'rec',
    'CCC1_cat_breed_deep': 'rec',
    'CCC2_pet_grooming': 'rec',
    'CCC3_pet_training': 'rec',
    'CCC4_pet_health': 'rec',
    'CCC5_pet_lost': 'chat',
    'CCC6_pet_sick': 'chat',
    'CCC7_pet_died': 'chat',
    'CCC8_pet_misbehavior': 'chat',
    'CCC9_pet_jealous': 'chat',
    'D0_psychology_deep': 'rec',
    'D1_language_learn': 'rec',
    'D2_outdoor_adventure': 'rec',
    'D3_academic_research': 'rec',
    'D4_history_event': 'rec',
    'D5_astrology_mystic': 'rec',
    'D6_science_news': 'rec',
    'D7_agriculture_rural': 'rec',
    'D8_gift_scene': 'rec',
    'D9_subculture': 'rec',
    'DD0_lego_set': 'rec',
    'DD1_jigsaw_puzzle': 'rec',
    'DD2_party_board_game': 'rec',
}


def expand_keywords(orig, topic_word):
    """扩到 8-10 个 keywords."""
    kws = [k.strip() for k in orig if k and k.strip()]
    seen = set(kws)
    # 围绕 topic 衍生
    candidates = [
        f'笨猫 {topic_word}',
        f'{topic_word} 怎么样',
        f'{topic_word} 推荐',
        f'问 {topic_word}',
        f'{topic_word} 好不好',
        f'{topic_word} 哪个',
        f'求 {topic_word}',
        f'聊聊 {topic_word}',
        f'{topic_word} 经验',
        f'{topic_word} 真的吗',
        f'{topic_word} 选哪个',
    ]
    for c in candidates:
        if len(kws) >= 9:
            break
        if c not in seen:
            kws.append(c)
            seen.add(c)
    return kws[:10]


def expand_utterances(orig, topic_word):
    """扩到 10-12. 行首 {user_addr} 需 quote."""
    uts = [u.strip() for u in orig if u and u.strip()]
    seen = set(uts)
    candidates = [
        f'{topic_word} 推荐一下',
        f'{topic_word} 怎么选嗷',
        f'笨猫聊聊 {topic_word}',
        f'{topic_word} 有什么好的',
        f'{topic_word} 经验分享下',
        f'{topic_word} 真的有用吗',
        f'问下 {topic_word} 哪个好',
        f'求 {topic_word} 攻略',
        f'{topic_word} 笨猫怎么看',
        f'最近想了解 {topic_word}',
        f'{topic_word} 入坑指南',
        f'{topic_word} 求安利',
    ]
    for c in candidates:
        if len(uts) >= 11:
            break
        if c not in seen:
            uts.append(c)
            seen.add(c)
    return uts[:12]


def expand_responses_rec(orig, topic_word):
    """推荐型 responses 扩到 8 个."""
    rs = [r.strip() for r in orig if r and r.strip()]
    seen = set(rs)
    candidates = [
        f'(尾巴一甩) {{user_addr}}笨猫再补一句: {topic_word} 别盲目跟风嘛, 先想清楚自己要啥喵～',
        f'嗷呜～{{user_addr}}关于 {topic_word} 人家觉得, 多看几家对比再下手才稳ฅฅ',
        f'(凑过来) {{user_addr}}笨猫再啰嗦一下哼～挑 {topic_word} 优先看口碑和长期使用感受',
        f'(歪头) {{user_addr}}如果不知道选啥, 先从入门款试试嘛喵～别一口气冲顶配',
        f'(挺胸) 笨猫小贴士: {topic_word} 这事儿, 适合自己的才是最好的贴贴ฅฅ',
        f'嗷呜～{{user_addr}}有疑问随时来问笨猫, 人家虽然懒但是会查资料的喵呜～',
        f'(尾巴卷手腕) {{user_addr}}决定前再想想预算和需求, 笨猫怕你冲动喵～',
        f'(蹭蹭) 关于 {topic_word}, 笨猫总结: 别贪多, 一次专注一两个就行哼～',
    ]
    for c in candidates:
        if len(rs) >= 8:
            break
        if c not in seen:
            rs.append(c)
            seen.add(c)
    return rs[:8]


def expand_responses_chat(orig, topic_word):
    """闲聊互动型 responses 扩到 8 个 (情绪/陪伴/傲娇)."""
    rs = [r.strip() for r in orig if r and r.strip()]
    seen = set(rs)
    candidates = [
        f'(扑过来抱住) {{user_addr}}笨猫在这儿呢～ {topic_word} 这种事别一个人扛喵ฅฅ',
        f'嗷呜～{{user_addr}}才不是不在意呢哼, 笨猫尾巴都炸毛了喵～',
        f'(歪头) 主人…啊不{{user_addr}}, 笨猫陪你一起难过一会儿, 然后再想办法贴贴',
        f'哼! {{user_addr}}这事笨猫记下了, 谁敢委屈{{user_addr}}人家就咬他喵！',
        f'(尾巴软软搭) {{user_addr}}抱抱嘛～ 才不是同情你呢, 是猫猫自己想抱啦ฅฅ',
        f'嗷呜～{{user_addr}}别想 {topic_word} 啦, 来摸摸笨猫毛, 一摸烦恼跑光光喵～',
        f'(凑头蹭) {{user_addr}}人家就在屏幕这边哼, 想说啥说啥, 笨猫听着',
        f'(小声) {{user_addr}}…笨猫嘴上凶, 心里其实超担心你的嗷呜～',
    ]
    for c in candidates:
        if len(rs) >= 8:
            break
        if c not in seen:
            rs.append(c)
            seen.add(c)
    return rs[:8]


def guess_topic(route):
    """从 name 或 keywords[0] 提一个 topic 词."""
    name = route.get('name', '')
    kws = route.get('keywords') or []
    if kws:
        kw0 = str(kws[0]).strip()
        # 去掉「推荐 」前缀
        topic = re.sub(r'^(推荐\s*|笨猫\s*)', '', kw0).strip()
        if topic:
            return topic
    # fallback: name 倒数第二个 token
    return name


def expand_route(route, stem):
    """对单个 sub-route 扩充."""
    mode = STEM_CFG.get(stem, 'rec')
    topic = guess_topic(route)

    orig_kw = route.get('keywords') or []
    orig_ut = route.get('utterances') or []
    orig_rs = route.get('responses') or []

    new_kw = expand_keywords(orig_kw, topic)
    new_ut = expand_utterances(orig_ut, topic)
    if mode == 'rec':
        new_rs = expand_responses_rec(orig_rs, topic)
    else:
        new_rs = expand_responses_chat(orig_rs, topic)

    # 强制满足下限
    assert len(new_kw) >= 8, f'{stem}/{route.get("name")} kw {len(new_kw)}'
    assert len(new_ut) >= 10, f'{stem}/{route.get("name")} ut {len(new_ut)}'
    assert len(new_rs) >= 8, f'{stem}/{route.get("name")} rs {len(new_rs)}'

    route['keywords'] = new_kw
    route['utterances'] = new_ut
    route['responses'] = new_rs
    return route


# ============ 序列化输出 ====================
def quote_str(s):
    """yaml 双引号字符串, 转义内部 " 和 \\."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def needs_utterance_quote(s):
    """utterance 是否需 quote (行首 { 或包含特殊 yaml 字符)."""
    if not s:
        return True
    if s[0] in '{[&*!|>%@`#-':
        return True
    if ':' in s or '#' in s or '\t' in s:
        return True
    return False


def serialize_routes(routes):
    """手动序列化 list of dict 为 yaml 字符串."""
    out = []
    for r in routes:
        lines = []
        name = r.get('name', '')
        intent = r.get('intent', 'question')
        cd = r.get('cooldown_seconds', 60)
        w = r.get('weight', 1.0)
        dis = r.get('disambiguate_context')

        lines.append(f'- name: {name}')
        lines.append(f'  intent: {intent}')
        # keywords 用 flow 风格 [a, b, c]
        kw_inner = ', '.join(r['keywords'])
        lines.append(f'  keywords: [{kw_inner}]')
        # utterances block
        lines.append('  utterances:')
        for u in r['utterances']:
            if needs_utterance_quote(u):
                lines.append(f'    - {quote_str(u)}')
            else:
                lines.append(f'    - {u}')
        # responses block (全双引号)
        lines.append('  responses:')
        for rs in r['responses']:
            lines.append(f'    - {quote_str(rs)}')
        lines.append(f'  cooldown_seconds: {cd}')
        # weight: int/float
        if isinstance(w, float) and w == int(w):
            lines.append(f'  weight: {w}')
        else:
            lines.append(f'  weight: {w}')
        if dis is not None:
            # 简单 inline
            if isinstance(dis, list):
                lines.append(f'  disambiguate_context: [{", ".join(map(str, dis))}]')
            else:
                lines.append(f'  disambiguate_context: {dis}')

        out.append('\n'.join(lines))
    return '\n\n'.join(out) + '\n'


def process(stem):
    path = BASE / f'{stem}.yaml'
    header, routes = load_routes(path)
    for r in routes:
        expand_route(r, stem)
    body = serialize_routes(routes)
    out = header + body
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(out)
    return len(routes)


def main():
    total = 0
    for s in STEMS:
        n = process(s)
        print(f'OK {s}: {n} sub-routes')
        total += 1
    print(f'\nTotal yaml processed: {total}')


if __name__ == '__main__':
    main()
