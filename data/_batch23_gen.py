# -*- coding: utf-8 -*-
"""
Auto-generate disambiguate_context for batch 23 routes.
Strategy:
1. Extract the route's "topic_hint" from the file name (e.g. 552_morning_coffee_first -> 咖啡/早晨/提神).
2. Pull unique terms from utterances that are NOT in keywords and NOT in any sibling's first_utterance/topic_hint tokens.
3. Add intent-flavored words (question -> 推荐/怎么; complaint -> 累/呜/崩; playful -> 嘿嘿/嘻嘻).
4. Add file_hint topic words manually curated per topic family.
5. Fall back to utterance characters / route-name-derived words.
"""
import json, re, sys

data = json.load(open('e:/VC/Catty/data/_batch23.json','r',encoding='utf-8'))

# ---------- topic word bank, derived from common topic_hint families ----------
# Maps a "topic seed" -> a list of candidate disambig words (Chinese topic-specific terms).
TOPIC_BANK = {
    # head pat / chin
    'jiaxiaba': ['下巴痒', '撸下巴', '挠脖窝', '咕噜', '舒服喵'],
    'shubi': ['梳毛器', '掉毛', '打结', '撸毛器', '理毛'],
    # off work tired
    'off_work_tired': ['下班路', '加班完', '通勤完', '一身疲', '坐瘫'],
    # noon break / oversleep
    'noon_break': ['午觉', '午休', '中午困', '下午茶', '回神'],
    # in-law conflict / grievance
    'in_law_conflict': ['婆媳', '婆家', '亲戚事', '难调和', '吵架'],
    # pre sleep / insomnia
    'pre_sleep_chat': ['睡前聊', '床上', '关灯', '数羊', '困不来'],
    # tease owner lazy pose
    'tease_owner_lazy': ['沙发', '葛优', '躺尸', '懒虫', '化水'],
    # pimple panic - face wash
    'pimple_panic': ['长痘', '冒痘', '皮肤油', '毛孔', '泛红'],
    # jealousy pouting
    'jealousy_pout': ['嘟嘴', '哼唧', '甩尾', '小气', '不理'],
    # tired fatigue body
    'tired_fatigue': ['全身酸', '骨头痛', '瘫床', '走不动', '提不起'],
    # jealous vinegar
    'jealous_vinegar': ['吃醋', '酸了', '抓尾', '小心眼', '醋坛'],
    # phone battery low / heat / anxiety
    'phone_battery_low': ['电量条', '红条', '充满', '强迫感', '插充电'],
    'phone_hot': ['散热差', '降频', '烧手', '机身热', '关后台'],
    # bengbu meme cry
    'bengbu_meme': ['蚌埠住', '笑哭', '梗图', '泪奔', '崩不住'],
    # office slack overtime
    'office_slack': ['打工', '工位', '老板催', 'PPT', '会议'],
    # mirror check self hair
    'mirror_check_self': ['镜子', '照镜', '镜前', '换发型', '看自己'],
    # flirt back love
    'flirt_back': ['表白', '嘴甜', '冲冲冲', '心动', '突袭'],
    # jealous vinegar exclusive
    'jealous_exclusive': ['专属', '独有', '只看我', '锁死', '黏紧'],
    # seek praise progress
    'seek_praise': ['夸夸', '夸笨猫', '抱抱奖', '加油棒', '小红花'],
    # internet meme - bailan
    'internet_meme': ['摆烂图', '梗图', '佛了', '不卷', '咸鱼'],
    # bump lost chat
    'bump_lost': ['碰一下', '撞到', '反应慢', '蒙圈', '找不到'],
    # gacha pulls
    'game_mood_gacha': ['十连', '保底', '欧气', '非酋', '出货'],
    # traffic jam late / motion sickness
    'traffic_jam_late': ['坐车', '后排', '颠簸', '呕感', '风口'],
    # afternoon tea milk tea
    'afternoon_tea': ['续命', '波霸', '芋圆', '半糖', '热饮'],
    # micro emotion sudden happy
    'micro_emotion': ['莫名', '心花', '哼小调', '蹦跶', '冒泡'],
    # stomach growl chibao
    'stomach_growl': ['打饱嗝', '撑肚', '走不动', '解腰带', '消食'],
    # share hungry snack
    'hungry_snack': ['分一口', '尝尝', '喂喂', '递过来', '嘴馋'],
    # cold caught sick - sore throat
    'cold_caught_sick': ['吞咽痛', '红肿', '咽炎', '冰水', '消炎'],
    # ask headpat - purr
    'ask_for_headpat_purr': ['咕噜咕噜', '呼噜响', '蹭脸', '满足脸', '舒服哼'],
    # forgot password
    'password_forgot': ['登录', '验证码', '找回', '邮箱', '安全题'],
    # morning coffee first
    'morning_coffee_first': ['美式', '拿铁', '提神', '续命杯', '现磨'],
    # need comfort lost
    'need_comfort_lost': ['迷路', '没方向', '找不到', '没目标', '空虚'],
    # sneeze runny nose chicken soup
    'sneeze_runny_nose': ['姜汤', '葱白', '保暖汤', '退烧', '驱寒'],
    # midnight snack crave
    'midnight_snack_crave': ['深夜', '罪恶感', '宵夜', '凉菜', '送餐慢'],
    # lazy day off binge watch
    'lazy_day_off': ['周末', '休息日', '宅家', '懒散', '什么都不做'],
    # house chores - pet food
    'house_chores_pet_food': ['猫粮空', '买货', '囤货', '货架', '主子'],
    # owner no shave
    'owner_no_shave': ['胡渣', '邋遢', '油腻', '剃须刀', '修边'],
    # want kiss beg - flying kiss
    'want_kiss_beg': ['啵啵', '隔空', '飞过来', '心心', '隔屏'],
    # bus commute tired - sweat
    'bus_commute_tired': ['挤地铁', '汗渍', '通勤', '臭味', '换衣'],
    # random grievance thanks
    'random_grievance': ['谢谢猫', '有你在', '靠你', '感激', '抱抱猫'],
    # emoji war battle
    'emoji_war_battle': ['斗图', '战绩', '王者', '碾压', '吊打'],
    # ate yet baofu
    'ate_yet': ['吃太饱', '腰带松', '撑死', '吃过头', '走不动'],
    # want fish jerky
    'want_fish_jerky': ['鱼条', '小鱼干', '咸鱼', '酥脆', '鱼香'],
    # pet sick cry
    'pet_sick': ['毛孩子', '生病', '心疼', '看医生', '宠物医院'],
    # rest well care - recharge
    'rest_well_care': ['歇歇', '小憩', '充能', '回神', '缓口气'],
    # bored idle chat - watch
    'bored_idle_chat': ['片单', '推荐剧', '什么好看', '电视剧', '电影'],
    # late night chat quiet
    'late_night_chat_quiet': ['静谧', '夜风', '万籁', '独处', '夜晚'],
    # night sleep chat - too late
    'night_sleep_chat': ['凌晨', '半夜', '深夜', '熬过头', '太迟'],
    # social injustice - want change
    'social_injustice': ['不公', '社会', '想发声', '挺身', '改革'],
    # tease master fat - hair
    'tease_master_fat': ['鸡窝', '炸毛', '没梳', '蓬乱', '起床发'],
    # indoor lazy day - movie
    'indoor_lazy': ['宅家', '电影日', '马拉松', '一刷再刷', '陪看'],
    # morning first word
    'morning_first_word': ['赖床', '不起', '被窝', '掀被', '再睡'],
}

INTENT_FLAVOR = {
    'complaint': ['呜', '累', '崩'],
    'playful': ['嘿', '蹭', '嘻'],
    'question': ['推荐', '怎么', '哪个'],
    '哭': ['呜呜', '泪', '心疼'],
    '食欲不振': ['没胃口', '吃不下', '反胃'],
    '语法学不会': ['卡住', '没懂', '记不住'],
    '旧版熟悉': ['习惯了', '老版本', '改了'],
}

def slugify(file_name):
    # Strip leading prefix '034_' and trailing '.yaml'
    name = file_name.replace('.yaml','')
    # remove leading code/number
    parts = name.split('_', 1)
    if len(parts) == 2 and re.match(r'^[A-Z]*\d+$', parts[0]):
        return parts[1]
    return name

# crude topic seed match
def topic_seed(file_name):
    slug = slugify(file_name)
    # try direct match
    if slug in TOPIC_BANK:
        return slug
    # try suffix match
    for k in TOPIC_BANK:
        if k in slug or slug in k:
            return k
    return None

# print a few examples
for r in data[:5]:
    print(r['name'], '|', r['file'], '|', topic_seed(r['file']))
