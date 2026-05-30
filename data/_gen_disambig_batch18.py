# -*- coding: utf-8 -*-
"""Generate disambiguate_context for batch 18 of round2 tasks.

Strategy:
  - Extract topic words from file name (e.g. '419_lunch_break_chat' -> hints).
  - Use utterances and route id segments for route-specific terms.
  - Add intent-specific seasoning (question -> 推荐/怎么; complaint -> 累/呜).
  - Filter out any term that overlaps keywords OR appears in any sibling's
    first_utterance (so it actually disambiguates).
"""
import json
import re

batch = json.load(open('e:/VC/Catty/data/_round2_batch18.json','r',encoding='utf-8'))

# Map file-name token -> candidate ctx words (hand-tuned semantic mapping).
TOPIC_MAP = {
    'lunch_break': ['工位', '便当', '午饭后', '小憩'],
    'noon_break': ['工位', '便当', '小憩', '打盹'],
    'lunch': ['食堂', '便当', '饭点'],
    'weekend_lazy': ['周末', '宅家', '懒得动', '躺平'],
    'weekend_plan': ['周末', '安排', '休息日'],
    'wanan_night': ['深夜', '睡前', '晚安调'],
    'late_night_chat': ['深夜', '夜聊', '凌晨'],
    'late_night_call': ['夜聊', '深夜', '凌晨'],
    'late_night_owl': ['夜猫', '熬夜党', '凌晨'],
    'drink_water_remind': ['白开水', '水杯', '提醒喝'],
    'water_drink_remind': ['白开水', '水杯', '提醒喝'],
    'drink_water_care': ['关心', '水杯', '叮嘱'],
    'internet_slow': ['网卡', '断流', '加载圈'],
    'wifi_died': ['无网', '断网', '掉线'],
    'wifi_disconnect': ['断网', '掉线', '没信号'],
    'wifi_dead': ['断网', '没信号', '崩了'],
    'text_emoji': ['表情包', '颜文字', '聊天梗'],
    'emoji_react': ['表情包', '颜文字', '斗图'],
    'random_emoji': ['表情包', '颜文字', '斗图'],
    'emoji_reactions': ['表情包', '颜文字', '斗图'],
    'morning_wake_grumpy': ['刚醒', '起床气', '迷糊'],
    'morning_wakeup': ['刚醒', '起床', '清晨'],
    'morning_grumble': ['碎碎念', '起床气', '困'],
    'just_wake_groggy': ['刚醒', '迷糊', '没清醒'],
    'study_cram_panic': ['复习', '考前', '考试'],
    'study_exam_chat': ['考试', '复习', '考场'],
    'study_burnout': ['学累了', '崩溃', '复习'],
    'study_anxiety': ['焦虑', '复习', '考前'],
    'study_exam_panic': ['考前', '通宵', '复习'],
    'thirsty_water_beg': ['讨水', '撒娇', '想喝'],
    'street_encounter': ['路上', '街头', '偶遇'],
    'window_view': ['窗外', '看出去', '玻璃外'],
    'window_gaze': ['窗外', '发呆', '出神'],
    'window_watching': ['窗外', '观察', '玻璃'],
    'pet_envy': ['别人家', '羡慕', '小动物'],
    'coffee_morning_kick': ['提神', '续命', '早咖'],
    'morning_coffee': ['提神', '续命', '早咖'],
    'office_slacking': ['摸鱼', '工位', '偷懒'],
    'work_slack': ['摸鱼', '工位', '偷懒'],
    'work_slack_moyu': ['摸鱼', '工位', '偷懒'],
    'workfish_idle': ['摸鱼', '工位', '划水'],
    'slack_off_work': ['摸鱼', '划水', '混班'],
    'midnight_dark': ['黑暗', '凌晨', '失眠想'],
    'insomnia_thinking': ['失眠', '脑补', '翻来覆去'],
    'sleep_early_remind': ['催睡', '早点睡', '别熬'],
    'snack_share': ['零食', '分享', '一起吃'],
    'hungry_snack': ['饿了', '零食', '想吃'],
    'morning_kiss': ['亲亲', '起床仪式', '撒娇'],
    'sulky_pouting': ['撅嘴', '生闷气', '小脾气'],
    'acting_clingy': ['黏人', '撒娇', '挂身上'],
    'sajiao_clingy': ['撒娇', '黏人', '抱大腿'],
    'morning_first_word': ['第一句', '清晨问', '醒来问'],
    'morning_call': ['早安电话', '叫醒', '清晨'],
    'internet_meme': ['梗', '网络梗', '流行语'],
    'juejuezi_meme': ['梗', '夸张梗', '流行语'],
    'lol_dying': ['爆笑', '梗', '神回'],
    'awsl_burst': ['可爱炸', '激动', 'awsl'],
    'sudden_compliment': ['突然夸', '彩虹屁', '猛夸'],
    'chat_filler': ['没话题', '尬聊', '凑数'],
    'what_doing_now': ['闲着', '随便问', '随便聊'],
    'busy_or_free': ['闲不闲', '忙不忙', '问近况'],
    'bored_killing_time': ['打发', '消磨', '消遣'],
    'micro_emotion': ['碎情绪', '小情绪', '随机'],
    'stretching_lazy': ['伸展', '懒洋洋', '舒展'],
    'morning_stretch': ['清晨拉伸', '舒展', '醒神'],
    'take_rest': ['休息', '歇会', '别累'],
    'take_rest_remind': ['催休息', '歇会', '叮嘱'],
    'pillow_talk': ['枕边', '夜话', '小声说'],
    'afternoon_drowsy': ['午后困', '犯困', '下午'],
    'care_rest_health': ['注意休息', '身体', '保重'],
    'chore_fight': ['家务吵', '分工', '不公'],
    'anti_trope': ['反套路', '反向', '不按常理'],
    'anti_routine': ['反套路', '反向', '不按常理'],
    'jealous_pout': ['吃醋', '撅嘴', '冷脸'],
    'jealousy_pouting': ['吃醋', '撅嘴', '不理'],
    'blush_avoid': ['脸红', '躲开', '害羞'],
    'yawn_sleepy': ['哈欠', '困', '眯眼'],
    'yawn_morning_stretch': ['哈欠', '清晨', '伸展'],
    'sleepy_already': ['困了', '想睡', '眯眼'],
    'goodnight_chat': ['晚安', '互道', '睡前'],
    'night_goodnight': ['晚安', '互道', '睡前'],
    'night_wanan': ['晚安调', '夜聊', '互道'],
    'night_sleep': ['睡前', '夜聊', '入睡'],
    'night_insomnia': ['失眠', '睡不着', '翻'],
    'doing_what': ['闲问', '在干嘛', '近况'],
    'status_check': ['在不在', '在线吗', '问候'],
    'status_check_in': ['打招呼', '问候', '近况'],
    'are_you_there': ['在不在', '在线吗', '冒泡'],
    'sneeze_cold': ['打喷嚏', '感冒', '受凉'],
    'hangover': ['宿醉', '酒后', '难受'],
    'hmph_sulk': ['哼哼', '撅嘴', '傲娇'],
    'hng_pout_tsundere': ['哼哼', '撅嘴', '傲娇'],
    'feigned_cold': ['假装冷', '装不在乎', '嘴硬'],
    'banter_teasing': ['互怼', '打趣', '调侃'],
    'tsundere_lie': ['嘴硬', '傲娇', '撒谎'],
    'throwing_tantrum': ['闹脾气', '撒泼', '耍赖'],
    'festival_moments': ['节日', '过节', '庆祝'],
    'festival_birthday': ['生日', '节日', '庆祝'],
    'holiday_moment': ['节日', '假期', '庆祝'],
    'festival_tradition': ['节日', '传统', '过节'],
    'festival_moment': ['节日', '过节', '庆祝'],
    'shovel_return': ['铲屎官', '回家', '到家'],
    'birthday_wish': ['生日', '祝福', '蛋糕'],
    'drama_chat': ['剧情', '剧透', '看剧'],
    'lazy_sunday': ['周日', '懒散', '宅'],
    'what_doing_ask': ['问问', '随便', '近况'],
    'lazy_day_off': ['休息日', '懒散', '宅'],
    'headache_complain': ['头疼', '难受', '脑壳'],
    'self_neglect': ['不照顾', '亏待', '自残式'],
    'random_giggle': ['爆笑', '突然笑', '没原因'],
    'expression_xiaosi': ['笑死', '笑出眼泪', '夸张'],
    'emoji_heihei': ['嘿嘿', '坏笑', '阴笑'],
    'emoji_haha': ['哈哈', '大笑', '爆笑'],
    'heihei_giggle': ['嘿嘿', '坏笑', '偷笑'],
    'furniture_regret': ['家具', '后悔买', '不实用'],
    'climate_doom': ['气候', '环境', '末日感'],
    'unclear_goal': ['迷茫', '没方向', '不知道'],
    'giving_up': ['放弃', '颓', '丧'],
    'no_hobby_time': ['没爱好', '没空', '吃灰'],
    'house_chores': ['家务', '收拾', '打扫'],
    'exercise_lazy': ['不想动', '懒得练', '健身'],
    'gacha_pull_dream': ['抽卡', '出货梦', '十连'],
    'game_session': ['打游戏', '开黑', '排位'],
    'game_vibes': ['游戏氛围', '段位', '战况'],
    'game_gacha': ['抽卡', '游戏氪', '出货'],
    'gacha_game': ['抽卡', '氪金', '出货'],
    'gacha_pull_luck': ['抽卡', '欧非', '运气'],
    'anwei_comfort_seek': ['求安慰', '抱抱', '难过'],
    'sigh_complain': ['叹气', '碎碎念', '抱怨'],
    'procrastination_putoff': ['拖延', '懒得动', '推迟'],
    'burnout_caring': ['倦怠', '累', '关心累'],
    'need_comfort_hug': ['求抱', '难过', '安慰'],
    'sunbathe_window': ['晒太阳', '窗台', '阳光'],
    'anti_trope_cold': ['冷处理', '反套路', '沉默'],
    'bengbu_zhu': ['绷不住', '无语', '哑口'],
    'chicu_jealous': ['吃醋', '醋意', '不爽'],
    'want_comfort': ['求安慰', '难过', '抱抱'],
    'subway_commute': ['地铁', '通勤', '挤'],
    'bus_commute_tired': ['挤公交', '通勤', '累'],
    'parent_died': ['父母离世', '失去', '永别'],
    'self_blame': ['自责', '后悔', '怪自己'],
    'bus_missed_run': ['赶车', '错过', '末班'],
    'cup_noodle': ['泡面', '夜宵', '速食'],
    'midnight_snack_crave': ['夜宵', '半夜饿', '宵夜'],
    'late_night': ['深夜', '凌晨', '夜聊'],
    'cat_tail_stepped': ['尾巴', '踩到', '喵叫'],
    'baobao_hug_beg': ['求抱', '抱抱', '撒娇'],
    'carry_belly_rub': ['抱抱', '摸肚', '撸'],
    'hug_beg_lap': ['求抱', '腿上', '蹭'],
    'tail_stepped_yelp': ['尾巴', '踩到', '叫'],
    'want_hug_beg': ['求抱', '想抱', '撒娇'],
    'ask_for_hug': ['求抱', '抱抱', '撒娇'],
    'kiss_request': ['求亲', '亲一下', '撒娇'],
    'beg_cuddle': ['求抱', '蹭', '撒娇'],
    'clingy_cuddle': ['黏人', '蹭', '不撒手'],
    'hug_petting': ['抱', '摸头', '撸'],
    'kiss_pester': ['亲亲', '缠着', '求'],
    'reverse_flirt_blush': ['反撩', '脸红', '害羞'],
    'random_chatter': ['闲聊', '随便聊', '碎碎'],
    'cat_chitchat': ['闲聊', '随便聊', '猫语'],
    'ate_yet': ['吃饭', '问吃', '饭点'],
    'ate_yet_care': ['关心吃', '饭点', '叮嘱'],
    'cat_fish_snack': ['小鱼干', '猫零食', '喂猫'],
    'water_remind': ['提醒喝', '水杯', '叮嘱'],
    'pet_treats': ['零食', '宠物', '猫零'],
    'spicy_food_burn': ['辣', '解辣', '烧嘴'],
    'weight_gain': ['变胖', '体重', '增重'],
    'tease_owner': ['调侃主', '打趣', '逗'],
    'master_getting_fat': ['主胖', '调侃', '逗'],
    'tease_master': ['调侃主', '打趣', '逗'],
    'owner_got_fat': ['主胖', '体重', '调侃'],
    'wuwu_cry_emoji': ['呜呜', '哭', '委屈'],
    'emoji_wuwu_yingying': ['呜呜', '哭', '委屈'],
    'seek_comfort': ['求安慰', '难过', '抱抱'],
    'need_comfort': ['求安慰', '难过', '抱抱'],
    'startup_fail': ['创业', '失败', '跑路'],
    'noob_teammate_rage': ['坑队友', '挂机', '气'],
    'office_politics': ['办公室斗', '同事', '辞职'],
    'world_war_fear': ['战争', '末日', '怕'],
    'meeting_zone_out': ['开会', '走神', '神游'],
    'parenting_burnout': ['育儿', '崩溃', '累'],
    'chocolate_crave': ['巧克力', '想吃', '甜'],
    'stomach_growl': ['咕咕', '饿', '肚子'],
    'midnight_snack_urge': ['夜宵', '半夜', '想吃'],
    'fish_snack': ['小鱼干', '猫零', '吃'],
    'midnight_snack': ['夜宵', '半夜', '想吃'],
    'hungry_food': ['饿', '想吃', '吃啥'],
    'evening_dinner': ['晚饭', '傍晚', '吃晚'],
    'peiwan_company': ['陪伴', '陪我', '一起'],
    'tea_milk': ['奶茶', '茶饮', '甜饮'],
    'afternoon_tea': ['下午茶', '甜点', '茶歇'],
    'too_sweet': ['齁', '太甜', '腻'],
    'popcorn_movie': ['爆米花', '电影', '影院'],
    'sweet_overload': ['齁', '太甜', '腻'],
    'eat_well_care': ['好好吃', '关心吃', '叮嘱'],
    'takeout_chat': ['外卖', '点单', '配送'],
    'takeout_late': ['外卖慢', '迟到', '等'],
    'takeout_slow': ['外卖慢', '等', '迟'],
    'did_you_eat': ['吃没吃', '饭点', '关心'],
    'emoji_awsl_juejue': ['awsl', '梗', '夸张'],
    'small_complaints': ['小抱怨', '碎碎', '吐槽'],
    'morning_wakeup_chat': ['清晨', '醒来', '问候'],
    'morning_hello': ['早安', '清晨', '问候'],
    'oversleep_panic': ['睡过头', '迟', '慌'],
    'tease_no_shave': ['没刮', '胡子', '调侃'],
    'tease_master_outfit': ['穿搭', '调侃', '打趣'],
    'outfit_fail': ['穿搭', '塌房', '不搭'],
    'tease_master_weight': ['调侃胖', '体重', '打趣'],
    'scratching_post': ['抓板', '磨爪', '挠'],
    'rest_well_care': ['好好歇', '休息', '叮嘱'],
    'feed_water_remind': ['喝水', '提醒', '关心'],
    'late_night_chat_002': ['深夜', '夜聊', '凌晨'],
    'meal_check_in': ['饭点', '问吃', '关心'],
    'study_scene': ['学习', '复习', '埋头'],
    'monday_blues': ['周一', '上班怕', '蓝色'],
    'office_gossip': ['办公室', '八卦', '同事'],
    'main_job_clash': ['主业', '副业', '冲突'],
    'wifi_dead_panic': ['断网', '没网', '崩'],
    'sleep_struggle': ['睡困难', '没睡好', '难'],
    'night_topic': ['夜话题', '夜聊', '凌晨'],
    'tease_master_002': ['调侃主', '打趣', '逗'],
    'emoji_text': ['表情', '颜文字', '梗'],
    'emoji_wuwu': ['呜呜', '哭', '委屈'],
    'self_blame_002': ['自责', '后悔', '怪自己'],
    'rolling_demand': ['打滚要', '撒泼', '撒娇'],
    'night_farewell': ['道别', '夜', '晚安'],
    'sleep_early': ['催早睡', '别熬', '叮嘱'],
    'late_night_owl_002': ['夜猫', '熬夜党', '凌晨'],
    'noon_lunch_break': ['午饭', '午休', '中午'],
    'noon_lunch_break_002': ['午饭', '午休', '中午'],
    'noon_break_chat': ['午休', '中午', '小憩'],
    'cooking_lazy': ['懒做饭', '速食', '凑合'],
    'sneeze_cold_catch': ['打喷嚏', '感冒', '受凉'],
    'sleepy_already_002': ['困了', '想睡', '眯眼'],
    'late_night_chat_003': ['深夜', '夜聊', '凌晨'],
    'random_chitchat': ['闲聊', '碎碎', '随便'],
    'noon_lunch_break_003': ['午饭', '午休', '中午'],
    'cat_chitchat_002': ['闲聊', '猫语', '随便'],
    'tease_master_003': ['调侃主', '打趣', '逗'],
    'master_getting_fat_002': ['主胖', '调侃', '逗'],
    'tease_owner_002': ['调侃主', '打趣', '逗'],
    'owner_got_fat_002': ['主胖', '体重', '调侃'],
    'sigh_complain_002': ['叹气', '碎碎念', '抱怨'],
    'expression_xiaosi_002': ['笑死', '笑出眼泪', '夸张'],
    'emoji_heihei_002': ['嘿嘿', '坏笑', '阴笑'],
    'emoji_haha_002': ['哈哈', '大笑', '爆笑'],
}

INTENT_SEASONING = {
    'question': ['推荐', '怎么', '哪个'],
    'complaint': ['累', '崩', '呜'],
    'playful': ['俏皮', '撒娇', '梗'],
}

def parse_topic(fname):
    """Strip leading digits and .yaml; return remainder."""
    if not fname: return ''
    s = fname.replace('.yaml','')
    # remove numeric prefix and underscore
    s = re.sub(r'^[A-Z0-9]+_', '', s, count=1)
    return s

def find_topic_words(fname):
    topic = parse_topic(fname)
    # Try direct
    if topic in TOPIC_MAP:
        return list(TOPIC_MAP[topic])
    # Try substrings
    for k, v in TOPIC_MAP.items():
        if k and (k in topic or topic in k):
            return list(v)
    return []

# Per-route override / hand-tuned seed words (route name -> list).
# Used when topic generic isn't enough.
ROUTE_OVERRIDES = {}

def gen_ctx(route):
    name = route['name']
    kw = set(route['keywords'])
    sib_uts = ' '.join(s.get('first_utterance','') for s in route.get('siblings',[]))
    uts = ' '.join(route['utterances'])

    candidates = []

    # 1. file topic words
    candidates.extend(find_topic_words(route['file']))

    # 2. words from own utterances (Chinese 2-4 char fragments) that aren't kw
    # 3. specific overrides
    if name in ROUTE_OVERRIDES:
        candidates = ROUTE_OVERRIDES[name] + candidates

    # 4. intent seasoning
    candidates.extend(INTENT_SEASONING.get(route['intent'], []))

    # Filter: drop empties, drop kw overlap (exact match or kw is substring of candidate or vice versa)
    seen = set()
    result = []
    for c in candidates:
        c = c.strip()
        if not c: continue
        if c in seen: continue
        # drop if any keyword equals or contained in c
        bad = False
        for k in kw:
            if not k: continue
            if k == c or k in c or c in k:
                bad = True
                break
        if bad: continue
        # drop if appears in any sibling first_utterance
        if c in sib_uts:
            continue
        seen.add(c)
        result.append(c)
        if len(result) >= 6: break

    # Ensure at least 4 items: add fallbacks
    FALLBACKS = ['日常', '聊天', '感觉', '一点', '稍微', '想想', '随口', '其实']
    fi = 0
    while len(result) < 4 and fi < len(FALLBACKS):
        c = FALLBACKS[fi]; fi += 1
        if c in seen: continue
        bad = False
        for k in kw:
            if k and (k == c or k in c or c in k):
                bad = True; break
        if bad: continue
        if c in sib_uts: continue
        seen.add(c); result.append(c)
    return result[:6]

results = []
for r in batch:
    ctx = gen_ctx(r)
    results.append({'name': r['name'], 'ctx': ctx})

# Save raw and emit JSON to stdout for inspection
json.dump(results, open('e:/VC/Catty/data/_batch18_results.json','w',encoding='utf-8'), ensure_ascii=False, indent=1)

# stats
print('total:', len(results))
print('min ctx len:', min(len(r['ctx']) for r in results))
print('max ctx len:', max(len(r['ctx']) for r in results))
print('empty ctx:', sum(1 for r in results if len(r['ctx'])<4))

# sample
for r in results[:8]:
    print(r['name'], '->', r['ctx'])
