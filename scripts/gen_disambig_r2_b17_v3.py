# -*- coding: utf-8 -*-
"""V3: 在 v2 基础上, 过滤掉与 sibling first_utterance 子串重叠的 ctx 词,
   并补充更专属的备用词。
"""
import json, re

PATH = 'e:/VC/Catty/data/disambig_round2_tasks.json'
batch = json.load(open(PATH,'r',encoding='utf-8'))[17]
v2 = json.load(open('e:/VC/Catty/data/disambig_r2_b17_v2.json','r',encoding='utf-8'))

# 备用储备池: 当 ctx 不足时按 file 基础类挑
BACKUP_POOL = {
    # 生理/吃
    'eat':       ['报备','顿顿','零嘴','补充','咀嚼','嚼'],
    'sleep':     ['困意','打盹','迷瞪','犯困','哈欠','合眼'],
    'comfort':   ['哄哄','拍拍','安抚','陪伴','顺顺','别难过'],
    'cold':      ['秋裤','搓搓','冷飕','瑟瑟','哆嗦','加件'],
    'warm':      ['暖暖','搓手','热乎','暖宝','暖身','回暖'],
    'tired':     ['瘫了','歇会','撑不住','喘喘','缓缓','瘫床'],
    'meme':      ['整活','蹭热度','梗图','热搜','弹幕','跟风'],
    'flirt':     ['偷瞄','心动','怦怦','结巴','低头','撩拨'],
    'kiss':      ['嘟嘴','要亲','贴脸','么么','蹭蹭','亲一口'],
    'pet':       ['顺毛','耳朵','头顶','rua','摸摸','揉揉'],
    'happy':     ['嘻嘻','嘿嘿','蹦跶','喜悦','咯咯','开心'],
    'sad':       ['呜呜','哽咽','眼眶','叹气','心闷','闷闷'],
    'angry':     ['撅嘴','板脸','气鼓鼓','别理','哼','怒'],
    'play':      ['打闹','逗','耍','整活','嬉闹','凑趣'],
    'phone':     ['屏幕','信号','刷','插头','%','断电'],
    'rain':      ['雨声','潮','打伞','泥泞','积水','雨衣'],
    'snow':      ['雪花','堆雪人','雪景','雪地','咯吱','冻'],
    'tea':       ['茶香','茶杯','回甘','一杯','茶味','叶子'],
    'coffee':    ['提神','一杯','店员','点单','糖度','奶泡'],
    'song':      ['哼调','旋律','歌词','跑调','节奏','嘴边'],
    'game':      ['上分','队友','版本','开黑','匹配','操作'],
    'work':      ['加班','下班','工位','摸鱼','打卡','工作'],
    'study':     ['复习','刷题','卷子','背诵','早读','下课'],
    'money':     ['余额','吃土','破产','存款','花呗','工资'],
    'shop':      ['下单','满减','优惠','清单','购物车','凑单'],
    'wear':      ['秋裤','围巾','加衣','毛衣','搓手','打底'],
    'commute':   ['通勤','换乘','站票','早高峰','迟到','打卡'],
    'cat':       ['尾巴','耳朵','撸猫','喵','蹦跶','打滚'],
    'beg':       ['求求','拜托','嘛','吧','给嘛','嘤嘤'],
    'jealous':   ['酸了','撅嘴','别理','吃醋','哼','咽气'],
    'clingy':    ['黏黏','贴贴','别走','蹭着','腿上','怀里'],
    'lonely':    ['一个人','没人','空荡','安静','静悄悄','孤单'],
    'bored':     ['发呆','打哈欠','闷','消磨','空虚','无趣'],
    'busy':      ['加急','分身','开会','转个不停','堆积','日程'],
    'beauty':    ['好看','气质','颜值','清新','软萌','倾国'],
    'shy':       ['脸红','低头','扭捏','结巴','躲开','害羞'],
    'birthday':  ['蜡烛','蛋糕','许愿','party','祝寿','烛光'],
    'festival':  ['节俗','灯笼','烟花','气氛','贴春联','糖瓜'],
    'photo':     ['咔嚓','九宫格','美颜','合影','角度','镜头'],
    'flower':    ['花瓣','花香','插花','开花','送花','一束'],
    'fun':       ['有意思','逗','整活','凑趣','逗趣','蹦跶'],
}

# 根据 file basename 给一个 backup 类
def backup_class(file):
    f=file.lower()
    if 'eat' in f or 'meal' in f or 'food' in f or 'snack' in f or 'stomach' in f or 'fridge' in f or 'cup_noodle' in f or 'ramen' in f or 'sweet' in f or 'tea_milk' in f or 'bubble_tea' in f or 'spicy' in f or 'crisp' in f or 'fish' in f:
        return 'eat'
    if 'sleep' in f or 'insomnia' in f or 'wake' in f or 'morning' in f or 'wanan' in f or 'night' in f or 'midnight' in f or 'alarm' in f or 'drowsy' in f or 'groggy' in f:
        return 'sleep'
    if 'comfort' in f or 'anwei' in f:
        return 'comfort'
    if 'cold' in f and 'cold_aloof' not in f:
        return 'cold'
    if 'warm' in f and 'warmth' not in f:
        return 'warm'
    if 'tired' in f or 'fatigue' in f or 'panic' in f:
        return 'tired'
    if 'meme' in f or 'bengbu' in f or 'number_meme' in f or 'internet_meme' in f:
        return 'meme'
    if 'flirt' in f or 'blush' in f:
        return 'flirt'
    if 'kiss' in f:
        return 'kiss'
    if 'pet_caress' in f or 'head_pat' in f or 'caress' in f or 'headpat' in f:
        return 'pet'
    if 'happy' in f or 'praised' in f or 'excited' in f or 'lucky' in f or 'payday' in f:
        return 'happy'
    if 'cry' in f or 'sob' in f or 'wuwu' in f or 'doom' in f:
        return 'sad'
    if 'angry' in f or 'hmph' in f or 'huff' in f or 'sulky' in f or 'pout' in f or 'grumpy' in f:
        return 'angry'
    if 'play' in f or 'tease' in f or 'roll_in_floor' in f or 'rolling' in f or 'random_chat' in f or 'humming' in f or 'singing' in f or 'karaoke' in f or 'song' in f:
        return 'play'
    if 'phone' in f or 'battery' in f or 'old_phone' in f:
        return 'phone'
    if 'rain' in f or 'umbrella' in f:
        return 'rain'
    if 'snow' in f:
        return 'snow'
    if 'tea' in f and 'milk' not in f and 'bubble' not in f:
        return 'tea'
    if 'coffee' in f or 'just_wake_groggy' in f:
        return 'coffee'
    if 'game' in f or 'gacha' in f:
        return 'game'
    if 'work' in f or 'commute' in f or 'traffic' in f or 'subway' in f or 'bus' in f:
        return 'commute'
    if 'study' in f or 'exam' in f or 'memorize' in f:
        return 'study'
    if 'money' in f or 'revenue' in f or 'no_money' in f or 'scam' in f:
        return 'money'
    if 'shop' in f or 'cart' in f or 'grocery' in f:
        return 'shop'
    if 'wear' in f or 'dress_warm' in f:
        return 'wear'
    if 'cat' in f and 'cat_aloof' not in f:
        return 'cat'
    if 'beg' in f or 'praise' in f or 'ask_for' in f:
        return 'beg'
    if 'jealous' in f or 'chicu' in f:
        return 'jealous'
    if 'clingy' in f or 'sajiao' in f or 'companion' in f:
        return 'clingy'
    if 'lonely' in f or 'alone' in f:
        return 'lonely'
    if 'bored' in f or 'killing_time' in f or 'idle' in f or 'brain_blank' in f:
        return 'bored'
    if 'busy' in f:
        return 'busy'
    if 'beauty' in f or 'outfit' in f or 'decoration' in f:
        return 'beauty'
    if 'shy' in f or 'dodge' in f or 'blush' in f or 'misread' in f:
        return 'shy'
    if 'birthday' in f or 'anniversary' in f:
        return 'birthday'
    if 'festival' in f or 'tanabata' in f or 'full_moon' in f:
        return 'festival'
    if 'photo' in f or 'screenshot' in f or 'scenery' in f or 'window' in f:
        return 'photo'
    if 'flower' in f:
        return 'flower'
    return 'fun'

def filter_sib_overlap(name, ctx, siblings, kw_set, file):
    sib_strings=[]
    for s in siblings:
        u = re.sub(r'\{user_addr\}','',s['first_utterance']).strip()
        if u: sib_strings.append(u)
    kept=[]
    removed=[]
    for c in ctx:
        bad=False
        for u in sib_strings:
            if len(c)>=2 and (c in u or u in c):
                bad=True; break
        if bad:
            removed.append(c)
        else:
            kept.append(c)
    # 如果 kept<4, 从 backup 池补
    if len(kept)<4:
        cls = backup_class(file)
        pool = BACKUP_POOL.get(cls, BACKUP_POOL['fun'])
        for p in pool:
            if p in kept or p in kw_set:
                continue
            bad=False
            for u in sib_strings:
                if len(p)>=2 and (p in u or u in p):
                    bad=True; break
            if bad: continue
            kept.append(p)
            if len(kept)>=5: break
    return kept[:6], removed

# Apply
final=[]
total_removed=0
short_after=0
for r, x in zip(batch, v2):
    kw_set=set(r['keywords'])
    new_ctx, removed = filter_sib_overlap(r['name'], x['ctx'], r['siblings'], kw_set, r['file'])
    if len(new_ctx)<4:
        short_after+=1
        print('SHORT after filter:', r['name'], r['file'], 'new=', new_ctx, 'removed=', removed)
    total_removed += len(removed)
    final.append({'name': r['name'], 'ctx': new_ctx})

print('total_removed_overlap_words:', total_removed)
print('short_after:', short_after)
print('total routes:', len(final))

# 最后再做一次 sanity: ctx 跟 keywords 重叠?
overlap=0
for r,x in zip(batch, final):
    kw=set(r['keywords'])
    for c in x['ctx']:
        if c in kw:
            overlap+=1
            print('OVERLAP:', r['name'], c, kw)
print('kw_overlap:', overlap)

# 保证 len 4-6
bad_len=[x for x in final if not (4<=len(x['ctx'])<=6)]
print('bad_len:', len(bad_len))
for x in bad_len:
    print(' ', x)

# 保证 ctx 字符串非空
bad_str=0
for x in final:
    for c in x['ctx']:
        if not isinstance(c,str) or not c.strip():
            bad_str+=1
print('bad_str:', bad_str)

# 保存
json.dump(final, open('e:/VC/Catty/data/disambig_r2_b17_final.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)
print('saved final to disambig_r2_b17_final.json')
