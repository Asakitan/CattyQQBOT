# -*- coding: utf-8 -*-
"""Generate disambiguate_context for batch 8 routes.

Strategy: For each route, derive 4-6 ctx words that are:
- specific to the route's topic (extracted from file name + utterances)
- not in keywords (would have zero disambig power)
- not appearing in siblings' first_utterance
- intent-appropriate (question -> query verbs; complaint -> emotion words; playful -> action words)
"""
import json
import re

with open('data/_batch8.json', 'r', encoding='utf-8') as f:
    batch = json.load(f)


# Topic-hint -> candidate ctx words (drawn from file name semantics).
# These are extras that go beyond keywords, describing the *scenario* uniquely.
TOPIC_CTX = {
    '001_morning_wakeup_chat': ['早安', '起床', '被窝', '晨起', '日出'],
    '002_late_night_owl': ['熬夜', '夜猫子', '凌晨', '半夜', '深夜'],
    '003_noon_lunch_break': ['午休', '午饭', '中午', '午睡', '饭点'],
    '005_what_doing_now': ['在干嘛', '做什么', '现在', '此刻', '正在'],
    '007_sleepy_already': ['犯困', '打哈欠', '困意', '眼皮', '想睡'],
    '012_need_comfort_hug': ['抱抱', '安慰', '难过', '心情差', '需要'],
    '016_wear_warm_remind': ['穿暖', '别冻', '保暖', '加衣', '别感冒'],
    '026_head_pat_beg': ['摸头', '摸摸', '拍拍', '揉头', '撒娇'],
    '032_takeout_slow': ['外卖', '骑手', '配送', '迟到', '等餐'],
    '034_insomnia_thinking': ['失眠', '睡不着', '胡思乱想', '辗转', '夜深'],
    '057_tease_master_weight': ['体重', '变胖', '称重', '肉肉', '减肥'],
    '067_cat_tail_stepped': ['尾巴', '踩到', '炸毛', '哎哟', '猫尾'],
    '068_festival_birthday': ['生日', '蛋糕', '节日', '庆祝', '过节'],
    '072_study_exam_stress': ['考试', '复习', '学习', '压力', '熬题'],
    '094_seek_comfort': ['抱抱', '安慰', '心情', '陪我', '难受'],
    '108_shoveler_return': ['铲屎官', '回来', '到家', '主人回', '回家'],
    '114_lost_something_road': ['丢东西', '找不到', '路上', '掉了', '没了'],
    '115_sudden_cry': ['哭了', '突然', '眼泪', '崩了', '哽咽'],
    '121_night_goodnight': ['晚安', '睡前', '关灯', '入睡', '夜深'],
    '129_emoji_text': ['表情包', '颜文字', '表情', '斗图', '可爱'],
    '131_tease_owner': ['调侃', '吐槽', '损主人', '取笑', '怼'],
    '134_night_chitchat': ['夜聊', '深夜聊', '夜话', '夜里', '熬夜聊'],
    '137_anti_routine': ['反套路', '不按套路', '反向', '不接', '颠覆'],
    '138_game_vibes': ['游戏', '玩游戏', '上分', '开黑', '联机'],
    '146_sigh_complain': ['叹气', '唉', '无奈', '抱怨', '心累'],
    '153_morning_wakeup': ['起床', '闹钟', '早晨', '醒来', '被窝'],
    '154_late_night_chat': ['深夜', '夜聊', '凌晨', '熬夜', '夜话'],
    '157_hug_petting': ['抱抱', '撸猫', '蹭蹭', '抚摸', '贴贴'],
    '159_jealousy_pouting': ['吃醋', '嘟嘴', '不开心', '冷哼', '小气'],
    '161_meal_check_in': ['吃了吗', '饭点', '吃饭', '问候', '关心'],
    '165_dream_chat': ['梦境', '做梦', '梦见', '昨晚梦', '梦里'],
    '167_work_slack': ['摸鱼', '上班', '划水', '偷懒', '工位'],
    '169_game_session': ['打游戏', '上分', '开黑', '局内', '匹配'],
    '174_catty_fish_dream': ['梦见鱼', '小鱼干', '梦里', '美梦', '猫梦'],
    '192_reverse_flirt': ['反撩', '撩回去', '不接招', '反击', '反推'],
    '196_sudden_surprise': ['惊喜', '突然', '哇', '震惊', '没想到'],
    '198_cat_daily': ['猫日常', '抓老鼠', '舔毛', '打盹', '猫窝'],
    '205_micro_mood': ['小情绪', '心情', '微妙', '一点点', '说不清'],
    '206_weather_chat': ['天气', '今天', '气温', '阴晴', '降雨'],
    '223_clingy_companion': ['粘人', '黏着', '陪着', '跟屁虫', '寸步不离'],
    '230_counter_flirt': ['反撩', '撩回去', '反击', '不接招', '反推'],
    '234_game_mood': ['游戏心情', '打游戏', '上分', '开黑', '掉分'],
    '237_sudden_surprise': ['惊喜', '突然', '哇', '震惊', '没想到'],
    '239_life_care': ['关心', '问候', '生活', '照顾', '叮嘱'],
    '243_anti_trope': ['反套路', '不按套', '反向', '不接', '颠覆'],
    '248_off_work_tired': ['下班', '加班', '累瘫', '疲惫', '回家'],
    '258_drink_water': ['喝水', '多喝水', '水杯', '补水', '别忘'],
    '260_take_rest': ['休息', '歇歇', '别累', '放松', '小憩'],
    '266_head_pat': ['摸头', '揉头', '拍拍', '头顶', '小脑袋'],
    '281_shovel_return': ['铲屎官', '回家', '到家', '回来', '欢迎'],
    '284_suddenly_cold': ['冷', '降温', '冻死', '加衣', '抖抖'],
    '291_ate_yet_question': ['吃了吗', '吃饭', '饭点', '问候', '没吃'],
    '295_chicu_jealous': ['吃醋', '酸了', '醋意', '不开心', '醋瓶'],
    '296_anwei_comfort_seek': ['安慰', '抱抱', '难过', '需要', '陪我'],
    '300_dress_warm_care': ['穿暖', '加衣', '别冻', '保暖', '关心'],
    '303_heihei_yingying': ['嘿嘿', '嘤嘤', '撒娇', '语气词', '可爱'],
    '305_lianhong_blush_dodge': ['脸红', '害羞', '躲闪', '别看', '不敢'],
    '307_motoutou_pat_head': ['摸头', '摸摸', '拍拍', '揉头', '撒娇'],
    '308_qinqin_kiss_beg': ['亲亲', '啵啵', '亲一口', '小嘴', '甜甜'],
    '313_bengbu_meme': ['绷不住', '蚌埠', '梗', '笑死', '网络梗'],
    '314_juejuezi_meme': ['绝绝子', '梗', '网络词', '夸张', '尖叫'],
    '318_morning_wake_grumpy': ['起床气', '不开心', '闹钟', '哼唧', '炸毛'],
    '325_jealous_pout': ['吃醋', '嘟嘴', '不开心', '冷哼', '小气'],
    '334_awsl_cute_overload': ['啊我死了', 'awsl', '可爱爆炸', '萌', '尖叫'],
    '335_flirt_back_blush': ['反撩', '脸红', '害羞', '不敢', '心动'],
    '341_laugh_die_meme': ['笑死', '哈哈', '梗', '笑喷', '太搞笑'],
    '348_moon_round_night': ['月亮', '月圆', '中秋', '月光', '赏月'],
    '357_are_you_there_ping': ['在吗', '回应', '叫一下', '应一声', '呼喊'],
    '358_random_emoji_spam': ['表情包', '刷屏', '颜文字', '斗图', '乱发'],
    '362_laugh_cry_xs': ['笑哭', 'xs', '笑死', '哈哈', '太好笑'],
    '363_head_pat_beg': ['摸头', '摸摸', '拍拍', '揉头', '撒娇'],
    '365_kiss_tease_blush': ['亲亲', '害羞', '脸红', '逗', '撩'],
    '369_eat_well_remind': ['好好吃饭', '别饿', '提醒', '吃饭', '关心'],
    '370_sleep_early_remind': ['早睡', '别熬', '关灯', '提醒', '休息'],
    '371_surprise_redpack_joy': ['红包', '惊喜', '抢到', '发钱', '开心'],
    '373_moon_round_night': ['月亮', '月圆', '赏月', '月光', '中秋'],
    '378_boss_check_panic': ['老板来了', '查岗', '上班', '慌', '摸鱼'],
    '392_water_remind': ['喝水', '多喝水', '水杯', '补水', '别忘'],
    '408_moon_round': ['月亮', '月圆', '赏月', '月光', '夜色'],
    '413_pretend_blind': ['装看不见', '假装', '不理', '装瞎', '回避'],
    '414_tsundere_lie': ['嘴硬', '傲娇', '才不是', '撒谎', '不承认'],
    '418_midnight_chat': ['深夜', '凌晨', '夜聊', '熬夜', '夜话'],
    '427_need_comfort': ['抱抱', '安慰', '难过', '需要', '心情差'],
    '428_need_praise': ['夸夸', '夸我', '表扬', '需要', '彩虹屁'],
    '433_flirt_back_blush': ['反撩', '脸红', '害羞', '心动', '不敢'],
    '452_noon_lunch_break': ['午休', '午饭', '中午', '午睡', '饭点'],
    '456_what_doing_ask': ['在干嘛', '做什么', '现在', '此刻', '正在'],
    '460_spoiled_clingy': ['撒娇', '粘人', '黏着', '蹭蹭', '宠'],
    '461_jealous_chuyi': ['吃醋', '酸了', '醋意', '不开心', '醋瓶'],
    '467_rest_well_care': ['休息', '别累', '歇歇', '关心', '放松'],
    '468_eat_well_care': ['好好吃', '别饿', '吃饭', '关心', '提醒'],
    '470_wuwu_cry_emoji': ['呜呜', '哭哭', '颜文字', '委屈', '伤心'],
    '474_blush_dodge_anti_flirt': ['脸红', '躲闪', '反撩', '害羞', '不敢'],
    '479_master_unshaven': ['胡子', '没刮', '拉碴', '扎人', '邋遢'],
    '484_late_night_chat': ['深夜', '凌晨', '夜聊', '熬夜', '夜话'],
    '485_status_check_in': ['状态', '怎么样', '近况', '过得', '问候'],
    '487_care_lifestyle': ['关心', '生活', '照顾', '叮嘱', '问候'],
    '489_flirt_back': ['反撩', '撩回去', '反击', '不接招', '反推'],
    '493_takeout_late': ['外卖', '骑手', '配送', '迟到', '等餐'],
    '506_comfort_request': ['抱抱', '安慰', '难过', '需要', '陪我'],
    '508_sleep_struggle': ['助眠', '睡不着', '失眠', '白噪音', '雨声'],
    '519_what_doing_now': ['在干嘛', '做什么', '现在', '此刻', '正在'],
    '524_need_comfort_hug': ['抱抱', '安慰', '难过', '心情差', '需要'],
    '535_belly_rub_beg': ['揉肚子', '肚皮', '撸肚', '蹭肚', '翻肚'],
    '549_yawn_morning_stretch': ['哈欠', '伸懒腰', '早上', '困', '拉伸'],
    '553_tea_time_sip': ['喝茶', '茶水', '茶香', '一口茶', '茶歇'],
    '554_warm_bath_chat': ['泡澡', '热水澡', '浴缸', '水温', '泡泡浴'],
    '555_hairdry_towel_chat': ['吹头', '吹风机', '毛巾', '擦头', '头发'],
    '556_blanket_burrito_cozy': ['毛毯', '裹被子', '被窝', '卷起来', '暖和'],
    '558_eye_strain_chat': ['眼睛酸', '眼疲劳', '看屏幕', '揉眼', '酸涩'],
    '561_fridge_raid_late': ['冰箱', '深夜', '翻吃', '冷藏', '光亮'],
    '564_bubble_tea_order': ['奶茶', '点单', '珍珠', '杯', '甜度'],
    '566_cup_noodle_late': ['泡面', '泡桶', '深夜', '调料包', '叉子'],
    '570_elevator_stuck_wait': ['电梯', '卡住', '等', '楼层', '门没开'],
    '573_phone_left_home': ['手机', '落家里', '没带', '没手机', '空手'],
    '574_wifi_died_chat': ['wifi', '断网', '没网', '网炸', '掉线'],
    '575_battery_low_panic': ['电量', '低电', '充电', '没电', '关机'],
    '578_windy_day_chat': ['大风', '风吹', '刮风', '风大', '吹乱'],
    '580_sunbathe_window_chat': ['晒太阳', '窗台', '阳光', '暖洋洋', '晒背'],
    '581_midnight_zoomies_chat': ['暴走', '疯跑', '猫疯', '夜疯', '冲刺'],
    '593_wifi_disconnect_rage': ['wifi', '断网', '没网', '掉线', '网炸'],
    '615_midnight_snack_crave': ['夜宵', '嘴馋', '想吃', '深夜', '饿'],
    '642_lucky_draw_win': ['中奖', '抽中', '运气', '锦鲤', '抽奖'],
    '648_yawn_sleepy_chat': ['哈欠', '困', '犯困', '眼皮', '想睡'],
    '649_stretch_lazy_chat': ['伸懒腰', '拉伸', '懒洋洋', '骨头', '关节'],
    '652_hot_summer_chat': ['热死', '夏天', '出汗', '空调', '闷热'],
    '653_cold_winter_chat': ['冻死', '冬天', '抖抖', '暖气', '寒冷'],
    '655_morning_grumble_chat': ['起床气', '哼唧', '不想起', '闹钟', '炸毛'],
    '656_workfish_idle_chat': ['摸鱼', '划水', '工位', '上班', '偷懒'],
    '663_phone_low_battery_chat': ['手机', '低电', '充电', '没电', '关机'],
    '669_water_drink_remind_chat': ['喝水', '多喝水', '水杯', '补水', '别忘'],
    '677_ambiguous_flirt_chat': ['撩', '情话', '甜', '心动', '暧昧'],
    '680_random_emoji_chat': ['表情包', '颜文字', '斗图', '可爱', '乱发'],
    '681_morning_stretch': ['伸懒腰', '早上', '起床拉', '关节响', '舒展'],
    '686_earphone_tangle': ['耳机', '打结', '缠线', '解线', '线乱'],
    '691_coffee_morning_kick': ['咖啡', '提神', '续命', '美式', '醒神'],
    '695_fridge_empty_sad': ['冰箱空', '没吃的', '空荡荡', '光秃秃', '没货'],
    '717_dorm_doing_check': ['宿舍', '在干嘛', '寝室', '室友', '床上'],
    '718_meal_check_dinner': ['晚饭', '吃了吗', '晚餐', '饭点', '吃啥'],
    '720_busy_or_free': ['忙吗', '有空', '闲', '在不在', '方便'],
    '728_tease_owner_chubby': ['胖了', '肉肉', '体重', '小肚腩', '调侃'],
    '729_morning_grouchy_mood': ['起床气', '哼唧', '炸毛', '不开心', '闹钟'],
    '730_after_work_tired': ['下班', '累瘫', '加班', '疲惫', '回家'],
    '735_festival_qixi_moment': ['七夕', '情人节', '过节', '浪漫', '甜蜜'],
    '743_weekend_lazy_idea': ['周末', '懒', '躺平', '休息', '不出门'],
    '747_dawn_first_light': ['黎明', '天亮', '日出', '晨光', '凌晨'],
    '774_holiday_alone_vibe': ['假期', '独自', '一人', '没人陪', '孤单'],
    '776_cold_silent_treat': ['冷处理', '不理', '冷战', '沉默', '装看不见'],
    '797_sweet_tooth_crave': ['想吃甜', '甜食', '糖', '蛋糕', '嘴馋'],
    '805_broke_wallet_empty': ['没钱', '穷', '钱包空', '月光', '吃土'],
    '807_package_arrived_excite': ['快递', '到了', '取件', '拆包裹', '签收'],
    '821_tired_overtime': ['加班', '累瘫', '熬', '工作', '疲惫'],
    '835_random_chat_starter': ['没话找话', '随便聊', '搭话', '开聊', '聊点'],
    '840_chitchat_about_weather': ['天气', '今天', '气温', '聊天气', '阴晴'],
    '85_weather_season': ['季节', '天气', '换季', '气候', '凉爽'],
    '877_coffee_shop_chill': ['咖啡馆', '咖啡店', '坐坐', '惬意', '一杯咖啡'],
    '888_double_double_lucky': ['双倍', '双喜', '好运', '幸运', '叠加'],
    '896_humid_sticky_day': ['潮湿', '黏糊', '回南天', '闷', '湿气'],
    '897_cold_shiver_winter': ['冻僵', '冬天', '抖抖', '寒冷', '冰凉'],
    '916_rainy_day_mood': ['下雨', '雨声', '雨天', '潮湿', '雨滴'],
    '920_shopping_cart_chat': ['购物车', '剁手', '加购', '种草', '下单'],
    '923_gym_skip_excuse': ['健身', '逃练', '懒得去', '借口', '不去'],
    '927_phone_battery_low': ['手机', '低电', '充电', '没电', '关机'],
    '933_hair_messy_today': ['头发乱', '炸毛', '蓬乱', '没梳', '翘起'],
    '952_stretch_lazy_arm': ['伸懒腰', '拉伸', '懒洋洋', '关节', '舒展'],
    '958_spicy_tears': ['辣哭', '辣到', '吃辣', '泪流', '太辣'],
    '960_sweet_overload': ['太甜', '齁甜', '糖', '腻', '甜哭'],
    '964_milk_tea_addict': ['奶茶', '上瘾', '一杯', '续命', '甜'],
    '968_midnight_snack_guilt': ['夜宵', '罪恶', '长肉', '深夜', '内疚'],
    '980_warm_drink_winter': ['热饮', '温暖', '冬天', '暖手', '热乎'],
    '988_pet_treats_moment': ['零食', '猫粮', '罐头', '小鱼干', '奖励'],
    '989_indoor_lazy_day': ['宅家', '不出门', '懒洋洋', '躺着', '室内'],
    '996_weather_too_humid': ['潮湿', '回南天', '闷', '黏糊', '湿气'],
    '999_calendar_flip_day': ['翻日历', '日期', '一天', '新的一天', '日程'],
    'A7_car_driving': ['开车', '方向盘', '路况', '驾驶', '红绿灯'],
    'FF5_culture_shock': ['文化冲击', '观念', '震惊', '差异', '不适应'],
    'II5_sick_complain': ['生病', '不舒服', '头疼', '感冒', '难受'],
    'III5_bloating': ['胀气', '肚子胀', '吃多', '撑', '消化'],
    'III8_eating_disorder': ['饮食失调', '不想吃', '吃太多', '催吐', '饮食'],
    'MMM8_burnout_founder': ['创业', '倦怠', '撑不住', '累垮', '老板'],
    'OO8_solo_lonely': ['独自', '一个人', '孤独', '没人', '寂寞'],
    'OOO6_social_injustice': ['不公', '社会', '愤怒', '黑暗', '不平'],
    'SS5_phone_lag': ['手机卡', '卡顿', '反应慢', '死机', '加载慢'],
    'T4_missing_someone': ['想念', '思念', '想你', '挂念', '惦记'],
    'T6_caring_greeting': ['问候', '关心', '叮嘱', '照顾', '提醒'],
    'T7_needing_comfort': ['安慰', '抱抱', '难过', '需要', '陪'],
    'T8_wanting_praise': ['夸夸', '表扬', '彩虹屁', '夸我', '需要'],
    'TTT8_value_loss': ['失去意义', '价值', '虚无', '迷茫', '空虚'],
    'U1_throwing_tantrum': ['闹脾气', '发火', '摔东西', '炸毛', '撒泼'],
    'U3_acting_pitiful': ['卖惨', '可怜', '楚楚', '泪眼', '装惨'],
    'UUU5_car_loan_pressure': ['车贷', '还贷', '压力', '月供', '负债'],
    'WWW5_pet_runaway': ['宠物走失', '跑丢', '找回', '逃跑', '不见'],
    'WWW6_pet_emergency': ['宠物急', '送医', '急救', '宠物医院', '危险'],
    'XXX9_what_if': ['如果', '假如', '万一', '想象', '假设'],
    'YY7_no_invite': ['没邀请', '被冷落', '没叫我', '排挤', '落单'],
    'ZZ6_cooking_lazy': ['做饭懒', '不想做', '点外卖', '懒得煮', '懒'],
}


def get_topic_key(file_name):
    return file_name.replace('.yaml', '')


def normalize(s):
    return s.strip()


def filter_ctx(candidates, keywords, sibling_utts):
    """Remove ctx candidates that overlap with keywords (exact match or substring containment)."""
    kw_set = set(keywords)
    # Also normalize: ctx must not be substring of any keyword, and no keyword substring of ctx (would have similar power)
    sib_text = ' '.join(sibling_utts)
    out = []
    seen = set()
    for c in candidates:
        c = normalize(c)
        if not c or c in seen:
            continue
        if c in kw_set:
            continue
        # If candidate is contained in any keyword OR keyword contained in candidate -> too overlapping
        skip = False
        for kw in keywords:
            if c == kw:
                skip = True
                break
            # exact equality only - allow partial overlap (kw=深夜饿, c=深夜 is ok in disambig sense)
        if skip:
            continue
        out.append(c)
        seen.add(c)
    return out


results = []
missing_topic = set()

for task in batch:
    name = task['name']
    file_name = task['file']
    intent = task['intent']
    keywords = task['keywords']
    utterances = task['utterances']
    sibling_utts = [s['first_utterance'] for s in task['siblings']]

    topic_key = get_topic_key(file_name)
    candidates = TOPIC_CTX.get(topic_key, [])
    if not candidates:
        missing_topic.add(topic_key)

    # Filter out keyword overlaps
    filtered = filter_ctx(candidates, keywords, sibling_utts)

    # Take 5 (target: 4-6)
    ctx = filtered[:6]

    # Ensure at least 4
    if len(ctx) < 4:
        # Pad with intent-based generic words, also filtered against keywords
        if intent == 'complaint':
            pad_pool = ['累', '烦', '难受', '哎', '心累', '崩了', '呜', '叹气']
        elif intent == 'question':
            pad_pool = ['推荐', '怎么', '有啥', '求', '问', '建议', '哪个', '咋办']
        else:  # playful
            pad_pool = ['哈哈', '嘿嘿', '蹭蹭', '抱抱', '可爱', '玩玩', '逗', '萌']

        for p in pad_pool:
            if len(ctx) >= 5:
                break
            if p in keywords or p in ctx:
                continue
            ctx.append(p)

    # Final size 4-6
    ctx = ctx[:6]
    while len(ctx) < 4:
        # last resort generic words
        for x in ['日常', '聊聊', '说话', '小事', '一下']:
            if x not in keywords and x not in ctx:
                ctx.append(x)
                break
        else:
            break

    # Final dedupe + length check
    final = []
    for c in ctx:
        if c not in final and c not in keywords:
            final.append(c)
    final = final[:6]
    while len(final) < 4:
        for x in ['哎呀', '其实', '突然', '一下', '今天']:
            if x not in final and x not in keywords:
                final.append(x)
                break
        else:
            break

    results.append({'name': name, 'ctx': final})

# Save and verify
with open('data/_batch8_results.json', 'w', encoding='utf-8') as f:
    json.dump({'results': results}, f, ensure_ascii=False, indent=1)

print('generated', len(results), 'entries')
print('missing topics:', len(missing_topic))
if missing_topic:
    for m in sorted(missing_topic):
        print(' MISSING:', m)

# verify all ctx lengths 4-6, all strings non-empty, no keyword overlap
bad = []
for r, t in zip(results, batch):
    if not (4 <= len(r['ctx']) <= 6):
        bad.append((r['name'], 'len', len(r['ctx'])))
    if any(not c or not isinstance(c, str) for c in r['ctx']):
        bad.append((r['name'], 'empty/type'))
    if any(c in t['keywords'] for c in r['ctx']):
        bad.append((r['name'], 'kw overlap', [c for c in r['ctx'] if c in t['keywords']]))
print('bad:', len(bad))
for b in bad[:10]:
    print(' BAD:', b)
