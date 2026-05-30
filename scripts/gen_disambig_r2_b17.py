# -*- coding: utf-8 -*-
"""根据 file 名/utterances/intent/siblings 智能生成 disambiguate_context 草稿。
人家会针对每个 route 给出 4-6 个独有语境词,过滤掉 keywords 和 sibling 共有的词。
"""
import json, re, sys

PATH = 'e:/VC/Catty/data/disambig_round2_tasks.json'
data = json.load(open(PATH, 'r', encoding='utf-8'))
batch = data[17]

# ===== file 名 -> 主题词包 (人家手工根据已知 routes 文化构造) =====
# 这些都是基于 route 系统 R1-R141 的语义.
# 命名规则: file basename(无 .yaml/无序号) -> [4-8个独有语境词候选]
TOPIC_BANK = {
    # ===== eat/food/care =====
    'ate_yet_care': ['没吃','撑着','难受','胃疼','反胃','吃太多'],
    'eat_well_check': ['挑食','没胃口','吃太少','营养','饭点','三餐'],
    'eat_proper_care': ['饭点','正经吃','营养','胃口','胖瘦','三餐'],
    'evening_dinner_chat': ['晚餐','晚饭','下班','回家','吃啥','点外卖'],
    'meal_check_in': ['饭点','吃了没','到点','点了','三餐','报备'],
    'stomach_growl_chat': ['饿了','咕咕','早饭','低血糖','空腹','馋'],
    'ate_yet_question': ['吃了吗','点啥','想吃','报备','饭点','馋'],
    'noon_break_chat': ['午饭','午休','中午','午睡','打盹','下午'],
    'late_night_call': ['深夜','凌晨','别睡','陪我','失眠','黑夜'],
    'morning_alarm_anger': ['闹钟','按掉','响了','吵醒','烦躁','起床气'],
    # ===== sleep/wake =====
    'morning_wakeup': ['醒了','早上','起床','睁眼','闹钟','早安'],
    'morning_grumpy': ['起床气','不想起','赖床','烦躁','撅嘴','闹脾气'],
    'late_night': ['熬夜','凌晨','别睡','黑眼圈','失眠','深夜'],
    'insomnia_thinking': ['失眠','睡不着','胡思乱想','翻来覆去','心事','凌晨'],
    'sleep_yet_ask': ['睡了吗','报备','晚安','洗漱','床上','准备睡'],
    'early_morning_greet': ['早安','晨光','清晨','元气','新的一天','日出'],
    # ===== mood/comfort =====
    'need_comfort': ['抱抱','蹭蹭','安慰','撒娇','委屈','想哭'],
    'micro_emotion': ['情绪','小心思','小情绪','闹别扭','憋着','心里'],
    'hmph_huff': ['哼','生气','撅嘴','别理','炸毛','傲娇'],
    'reverse_flirt_blush': ['脸红','害羞','撩','调戏','心动','怦怦'],
    'clingy_companion': ['黏人','陪着','别走','贴贴','蹭蹭','跟着'],
    # ===== body/health =====
    'cold_hands_warm': ['手凉','冰手','取暖','哈气','搓手','暖手'],
    'wear_warm': ['加衣','保暖','穿厚','降温','感冒','围巾'],
    'sneeze_chain_attack': ['打喷嚏','阿嚏','鼻涕','着凉','感冒','纸巾'],
    'hangover': ['宿醉','头疼','喝多了','解酒','吐了','蜂蜜水'],
    'motion_sickness': ['晕车','晕船','头晕','想吐','开窗','晕乎'],
    'bengbuzhu_meme': ['绷不住','笑死','笑场','破防','憋不住','喷了'],
    'crisp_snack_attack': ['零食','薯片','咔嚓','嘎嘣','嘴馋','咸香'],
    # ===== social/relationship =====
    'no_partner': ['单身','没对象','孤独','光棍','谈恋爱','找对象'],
    'pet_caress_request': ['摸头','摸摸','rua','揉揉','头顶','耳朵'],
    'are_you_there_ping': ['在吗','在不在','喂','人呢','出来','别躲'],
    'outfit_judged': ['穿搭','审美','土','好看','土味','配色'],
    'cleansing_mind': ['净化','清醒','洗脑','治愈','冷静','脑子'],
    'no_revenue': ['没收入','穷','没钱','破产','吃土','工资'],
    # ===== fun/random =====
    'festival_moment': ['节日','过节','春节','中秋','节气','气氛'],
    'random_singing_burst': ['唱歌','哼歌','五音','跑调','歌词','旋律'],
    'random_blessing': ['好运','祝福','锦鲤','开运','保佑','顺利'],
    'emoji_react': ['表情','贴纸','斗图','颜文字','meme','表情包'],
    'caring_greeting': ['关心','打气','加油','鼓励','撑住','陪你'],
    'take_rest': ['歇歇','休息','缓口气','放空','别撑','慢点'],
}

# ===== 通用 fallback: 根据 file basename 启发式提取 =====
FILE_HINTS = {
    'eat': ['吃饭','胃口','饿','馋','饭点','晚饭'],
    'food': ['食物','馋','美食','零食','口味','吃货'],
    'sleep': ['睡觉','困','失眠','床上','晚安','闭眼'],
    'wake': ['起床','醒','清醒','睁眼','早上','闹钟'],
    'morning': ['早上','清晨','早安','起床','闹钟','元气'],
    'night': ['晚上','深夜','凌晨','熬夜','黑夜','别睡'],
    'cold': ['冷','降温','保暖','打冷战','哆嗦','加衣'],
    'warm': ['暖','取暖','热乎','晒太阳','哈气','烫手'],
    'tired': ['累','疲惫','瘫','撑不住','缓缓','歇会'],
    'cry': ['哭','眼泪','委屈','哽咽','抽噎','纸巾'],
    'laugh': ['笑','大笑','笑场','哈哈','破防','喷了'],
    'angry': ['生气','炸','烦','气死','撅嘴','哼'],
    'happy': ['开心','高兴','元气','嘿嘿','蹦跶','喜悦'],
    'sad': ['难过','低落','丧','闷','叹气','委屈'],
    'meme': ['梗','表情','段子','蹭热度','整活','内涵'],
    'flirt': ['撩','调戏','暧昧','脸红','心动','贴贴'],
    'kiss': ['亲','贴脸','嘴对嘴','么么','蹭嘴','亲亲'],
    'hug': ['抱','贴贴','搂着','抱抱','怀里','蹭蹭'],
    'touch': ['碰','摸','戳','rua','揉','蹭'],
    'caress': ['摸头','rua','揉揉','顺毛','蹭蹭','耳朵'],
    'pet': ['摸头','摸摸','rua','揉揉','耳朵','顺毛'],
    'play': ['玩','陪玩','打闹','嬉闹','逗','玩耍'],
    'shy': ['害羞','脸红','扭捏','低头','结巴','躲'],
    'jealous': ['吃醋','酸','嫉妒','别理','哼','撒娇'],
    'comfort': ['安慰','陪着','别怕','蹭蹭','抱抱','撑住'],
    'rest': ['休息','歇会','缓缓','放空','喘口气','慢点'],
    'sick': ['生病','发烧','感冒','吃药','难受','体温'],
    'pain': ['疼','痛','酸','抽搐','按按','哎呦'],
    'work': ['上班','工作','加班','打工','下班','摸鱼'],
    'study': ['学习','作业','复习','考试','背书','刷题'],
    'exam': ['考试','复习','刷题','分数','挂科','卷子'],
    'money': ['钱','穷','工资','花呗','存款','吃土'],
    'shopping': ['买','购物','下单','清单','剁手','优惠'],
    'pay': ['付钱','转账','结账','买单','余额','到账'],
    'cook': ['做饭','下厨','炒菜','洗菜','锅铲','菜谱'],
    'drink': ['喝','水','饮料','吨吨','解渴','口渴'],
    'tea': ['茶','奶茶','泡茶','茶香','茶叶','一杯'],
    'coffee': ['咖啡','拿铁','美式','提神','苦','一杯'],
    'cake': ['蛋糕','奶油','甜','切块','烘焙','生日'],
    'sweet': ['甜','糖','甜品','嘴甜','糖分','甜腻'],
    'spicy': ['辣','麻辣','吃辣','喷火','辣条','刺激'],
    'flower': ['花','花香','插花','花瓣','开花','送花'],
    'rain': ['雨','下雨','打伞','淋湿','雨声','潮'],
    'snow': ['雪','下雪','雪花','堆雪人','雪景','冷'],
    'wind': ['风','大风','刮风','凉风','呼呼','吹乱'],
    'sun': ['太阳','阳光','晒','暖洋洋','日出','日落'],
    'cat': ['猫','喵','猫粮','撸猫','尾巴','耳朵'],
    'dog': ['狗','汪','遛狗','摇尾巴','狗子','吠'],
    'song': ['歌','旋律','歌词','哼唱','跑调','听歌'],
    'movie': ['电影','院线','字幕','影评','票','放映'],
    'game': ['游戏','开黑','上分','队友','版本','打机'],
    'book': ['书','读','翻页','小说','作者','章节'],
    'travel': ['旅游','出行','打卡','景点','机票','行李'],
    'birthday': ['生日','蛋糕','蜡烛','许愿','祝寿','party'],
    'gift': ['礼物','包装','送给','心意','拆','惊喜'],
    'reunion': ['好久','想你','重逢','回来','贴贴','久违'],
    'festival': ['节日','过节','节气','气氛','灯笼','烟花'],
    'lonely': ['孤单','空','一个人','没人','安静','寂寞'],
    'bored': ['无聊','没事','发呆','打哈欠','打发','闲'],
    'busy': ['忙','分身乏术','赶','堆积','加急','转个不停'],
    'phone': ['手机','屏幕','刷','低电量','充电','信号'],
    'message': ['消息','回复','已读','秒回','聊天','叮咚'],
    'photo': ['照片','拍照','合影','美颜','九宫格','咔嚓'],
    'selfie': ['自拍','拍自己','美颜','角度','镜头','凹造型'],
    'cute': ['可爱','软','糯','萌','奶','蓬'],
    'beauty': ['好看','美','漂亮','气质','颜值','倾国'],
    'praise': ['夸','表扬','称赞','吹捧','彩虹屁','点赞'],
    'hate': ['讨厌','恶心','拉黑','恨','嫌','烦'],
    'scold': ['骂','训','凶','吼','瞪','发火'],
    'beg': ['求','拜托','跪了','给我','嘛','吧'],
    'tease': ['逗','撩','戳','损','吐槽','调侃'],
    'apologize': ['道歉','对不起','sry','认错','赔礼','哄'],
    'thanks': ['谢','感谢','么么','心意','报答','回礼'],
    'goodnight': ['晚安','睡吧','好梦','闭眼','洗漱','床上'],
    'goodbye': ['再见','拜拜','下次','回头见','溜了','撤了'],
    'hello': ['你好','嗨','早安','打招呼','刚来','上线'],
    'come': ['来','到了','来了','到达','抵达','门口'],
    'leave': ['走','撤','溜','下线','离开','闪'],
    'wait': ['等','等等','慢点','别急','稍等','等会'],
    'fast': ['快','急','赶紧','立刻','秒','速度'],
    'slow': ['慢','慢慢','拖','磨','迟到','拖延'],
}

def slug_to_tokens(slug):
    return re.split(r'[_\-]', slug.lower())

def gen_ctx_for_route(r):
    file = r['file']
    keywords = set(r['keywords'])
    sib_topics = set()
    for s in r['siblings']:
        for tok in slug_to_tokens(s['topic_hint']):
            sib_topics.add(tok)
    # 取 file basename 用于查 TOPIC_BANK
    base = re.sub(r'^[A-Z]{0,3}\d*_?', '', file.replace('.yaml',''))
    # 去掉前导数字
    base = re.sub(r'^\d+_', '', base)
    candidates = []
    if base in TOPIC_BANK:
        candidates = list(TOPIC_BANK[base])
    else:
        toks = slug_to_tokens(base)
        for t in toks:
            if t in FILE_HINTS:
                candidates.extend(FILE_HINTS[t])
        # de-dup
        seen=set(); uniq=[]
        for c in candidates:
            if c not in seen:
                seen.add(c); uniq.append(c)
        candidates = uniq
    # 过滤掉 keywords 重叠
    candidates = [c for c in candidates if c not in keywords]
    # 长度 2-4
    candidates = [c for c in candidates if 2 <= len(c) <= 4]
    # intent 加成
    intent = r.get('intent','')
    extra = []
    if intent == 'complaint':
        extra = ['累','呜','难受','烦']
    elif intent == 'question':
        extra = ['推荐','怎么','哪种','好不好']
    elif intent == 'playful':
        extra = ['嘿嘿','逗','蹭蹭','嘻嘻']
    # 只在不足时补
    out = []
    for c in candidates:
        if c not in out and c not in keywords:
            out.append(c)
        if len(out)>=6: break
    if len(out)<4:
        for e in extra:
            if e not in out and e not in keywords:
                out.append(e)
            if len(out)>=5: break
    # 最终保证 4-6
    out = out[:6]
    return out, base in TOPIC_BANK

results = []
miss = []
for r in batch:
    ctx, hit = gen_ctx_for_route(r)
    results.append({'name': r['name'], 'ctx': ctx, '_file': r['file'], '_hit': hit, '_kw': r['keywords']})
    if not hit:
        miss.append(r['file'])

# 输出统计
from collections import Counter
miss_c = Counter(miss)
print('total:', len(results))
print('miss_topics:', len(miss_c))
print('miss_top20:')
for f,c in miss_c.most_common(40):
    print(' ', f, c)

# 检查不够 4 词的
short = [x for x in results if len(x['ctx'])<4]
print('short:', len(short))
for x in short[:30]:
    print('  ', x['name'], x['_file'], x['ctx'])

# 把结果写到 draft
json.dump(results, open('e:/VC/Catty/data/disambig_r2_b17_draft.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)
print('drafted to disambig_r2_b17_draft.json')
