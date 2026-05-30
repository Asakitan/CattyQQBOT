"""
Generate disambiguate_context for batch 21 routes.
"""
import json
import re

batch = json.load(open('e:/VC/Catty/data/_batch21.json','r',encoding='utf-8'))

# topic_stem (no .yaml) => list of unique ctx words (2-4 chars, Chinese)
# These should be distinctive of the route's topic and unlikely to appear in sibling files.
TOPIC_CTX = {
    # ----- weather / seasonal -----
    "799_hot_drink_winter": ["热饮", "暖手", "捧着", "冬日", "热茶", "暖暖"],
    "786_sunny_day_warm": ["晒暖", "出门走", "暖洋洋", "好天气", "晒一晒"],
    "143_weather_chat": ["聊天气", "随口提", "今天看", "突然说"],
    "503_weather_chat": ["天气如", "外面如", "聊一下", "随便聊"],
    "206_weather_chat": ["闲聊天", "说天气", "天气聊", "顺嘴"],
    "85_weather_season": ["季节聊", "换季", "时节", "应季"],
    "578_windy_day_chat": ["大风天", "刮脸", "迎风走", "挡风", "顶着风"],
    "893_windy_hair_mess": ["头发吹", "刘海", "披头乱", "梳头", "扎头"],
    "049_care_dress_warm": ["降温", "天冷", "添衣", "保重身", "添件衣"],
    "172_warm_clothing": ["穿厚点", "添件", "厚衣", "外套", "毛衣"],
    "527_dress_warm_care": ["叮嘱穿", "提醒穿", "记得穿", "别冻着"],
    "897_cold_shiver_winter": ["冬天冷", "冻手", "刺骨", "瑟瑟", "哈气"],
    "587_cold_hands_warm": ["手冰", "握手", "捂手", "搓手", "焐"],
    "900_pocket_warm_hands": ["插口袋", "兜里", "袋暖", "藏口袋"],
    "896_humid_sticky_day": ["回南天", "潮湿", "黏糊", "湿哒", "返潮"],
    "898_heat_wave_die": ["热死", "高温", "中暑", "晒化", "热浪"],
    "408_moon_round": ["满月", "月圆", "看月亮", "月色", "皓月"],
    "348_moon_round_night": ["赏月", "中秋", "月光", "月夜", "圆月"],
    # ----- food / drink -----
    "565_energy_drink_chat": ["熬夜撑", "提神", "心跳快", "上头", "续命"],
    "794_thirsty_water_beg": ["补水", "运动后", "出汗", "脱水", "解渴"],
    "693_milk_tea_sugar": ["奶茶罪", "全糖", "肥宅", "杯子", "甜腻"],
    "626_sweet_dessert_crave": ["甜点", "蛋糕", "甜食", "甜口", "想吃甜"],
    "183_food_craving": ["馋了", "想吃东", "嘴馋", "胃口"],
    "350_fish_dry_crave": ["鱼干", "小鱼干", "猫零食", "咬鱼"],
    "066_cat_fish_snack": ["猫零食", "鱼干嚼", "啃鱼", "小鱼"],
    "965_fried_chicken_lust": ["炸鸡", "脆皮", "外酥", "鸡腿", "啃鸡"],
    "932_hotpot_dream": ["火锅", "麻辣锅", "涮一涮", "毛肚", "锅底"],
    "566_cup_noodle_late": ["泡面", "桶面", "泡碗面", "重口", "深夜面"],
    "032_takeout_slow": ["外卖慢", "等外卖", "送餐久", "骑手"],
    "968_midnight_snack_guilt": ["夜宵罪", "深夜饿", "怕长肉", "嘴馋夜"],
    "615_midnight_snack_crave": ["夜宵馋", "想吃宵", "馋夜宵", "宵夜"],
    "815_midnight_snack_chat": ["夜宵聊", "宵夜话", "聊夜宵"],
    "930_midnight_snack_urge": ["夜宵冲", "嘴馋夜", "想点夜", "夜里饿"],
    "U9_food_complaint": ["菜冷掉", "饭菜凉", "等太久", "饭撒", "倒翻"],
    "302_eat_proper_care": ["外卖久", "天天外", "吃伤", "油腻吃"],
    "123_eat_check_in": ["吃饭了", "吃没吃", "饭点问", "正吃"],
    "520_ate_yet_meal": ["吃饭吗", "吃了么", "饭吃了", "顿饭"],
    "367_water_drink_remind": ["八杯水", "记得喝", "提醒水", "多喝水"],
    "392_water_remind": ["补水提", "喝口水", "去倒水", "喝两口"],
    "084_drink_water_check": ["喝水没", "杯子水", "喝完", "喝水问"],
    "526_drink_water_remind": ["提水", "杯子选", "捧杯", "新杯"],
    "618_hot_water_remind": ["热水提", "喝热水", "保温水", "暖水"],
    # ----- sleep / night -----
    "075_night_sleep_chat": ["夜聊", "陪夜聊", "夜深谈"],
    "584_cant_fall_asleep": ["翻来覆", "辗转", "数羊", "胡思乱", "睡不着"],
    "286_wanan_night_greeting": ["道晚安", "睡觉了", "下线了", "晚安啦"],
    "515_goodnight_routine": ["睡前流", "刷完牙", "关灯了", "钻被窝"],
    "228_insomnia_chat": ["失眠夜", "陪我说", "聊几句", "夜聊久"],
    "581_midnight_zoomies_chat": ["猫疯跑", "夜疯跑", "蹦跶", "暴走", "冲刺"],
    "550_five_more_minutes": ["再赖会", "闹钟响", "翻身", "起床难", "懒床"],
    "450_morning_zaoan_chat": ["早起累", "刚醒", "迷糊", "起不来"],
    "814_morning_first_yawn": ["首哈欠", "刚睁眼", "醒后哈", "刚醒哈"],
    "729_morning_grouchy_mood": ["起床气", "晨起怒", "脾气大", "炸毛"],
    "285_zaoan_morning_greeting": ["早饭啥", "早餐吃", "起床后", "早晨吃"],
    "074_morning_wake_up": ["醒过来", "刚起床", "起来了"],
    "219_morning_hello": ["道早安", "早安啦", "早上好"],
    "521_sleep_yet_check": ["睡了没", "睡了么", "去睡没"],
    "424_sleep_yet": ["睡没睡", "睡觉了", "睡没"],
    "458_sleep_yet_ask": ["睡没问", "几点睡", "睡没呀"],
    "387_midnight_chat": ["深夜聊", "凌晨聊", "夜深谈"],
    "733_late_night_dreaming": ["做梦", "梦里", "梦见", "睡梦"],
    "253_late_night_chat": ["夜里聊", "深夜话", "夜话"],
    "064_night_insomnia_chat": ["失眠话", "睡不着话"],
    "007_sleepy_already": ["困了要", "犯困", "想睡了"],
    # ----- affection / cute -----
    "827_pet_caress_request": ["撒娇要", "讨摸", "粘人", "讨亲"],
    "308_qinqin_kiss_beg": ["隔屏幕", "网络亲", "远距亲", "想亲"],
    "192_reverse_flirt": ["主动亲", "凑过", "嘟嘴等", "撩你"],
    "103_hug_request": ["紧抱", "搂紧", "不撒手", "缠住", "用力抱"],
    "306_baobao_hug_beg": ["求抱抱", "讨抱", "要抱", "想抱"],
    "364_hug_beg_request": ["抱抱呢", "想抱我", "讨拥抱"],
    "435_ask_for_hug": ["要个抱", "求个抱", "讨抱呀"],
    "056_head_pat_request": ["摸头", "讨摸头", "求摸头"],
    "363_head_pat_beg": ["头来摸", "摸摸头", "要摸"],
    "324_clingy_attention_beg": ["关注我", "看我呀", "理我", "粘"],
    "490_beg_cuddle": ["蹭一下", "粘着", "求抱蹭", "贴贴"],
    "535_belly_rub_beg": ["挠肚子", "肚皮摸", "翻肚", "肚肚"],
    "411_shovel_back": ["铲屎反", "回铲", "翻盆"],
    "411_shovel_back.yaml": ["铲屎反", "回铲", "翻盆"],
    # ----- mood -----
    "127_need_comfort": ["求安慰", "想哭呜", "委屈鬼", "心疼"],
    "263_need_comfort": ["求疼", "需安慰", "顺顺", "哄一下"],
    "012_need_comfort_hug": ["要疼抱", "求抱安", "讨抱安"],
    "LL5_lonely_depression": ["低落", "提不起", "颓", "空荡", "没劲"],
    "471_yingying_whimper": ["小脾气", "鼓嘴", "撒娇闹", "嘤嘤"],
    "021_wuwu_crying": ["呜呜", "想哭", "嘤嘤哭", "泪眼"],
    "331_wuwu_cry_emoji": ["哭表情", "呜呜呜", "哭表"],
    "470_wuwu_cry_emoji": ["哭脸表", "哭哭表"],
    "432_emoji_wuwu": ["哭符号", "哭脸", "泪表情"],
    "226_jealous_pout": ["吃醋", "酸了", "羡慕别"],
    "330_hehe_laugh_emoji": ["呵呵笑", "嘿嘿表"],
    "529_heihei_giggle": ["偷笑", "傻笑", "嘿嘿笑", "笑出"],
    "019_hehe_giggle": ["嘿嘿笑", "暗笑", "傻笑"],
    "341_laugh_die_meme": ["笑死梗", "笑梗", "笑哭梗"],
    "725_emoji_awsl_juejue": ["awsl", "绝绝子", "啊死了"],
    "205_micro_mood": ["小情绪", "无端委", "莫名"],
    # ----- gifts / luck -----
    "033_red_packet_joy": ["表白", "告白了", "心动", "突然好"],
    "540_red_packet_surprise": ["手气王", "抢到", "运气好", "中签"],
    "063_surprise_redpacket": ["红包惊", "突然红"],
    "274_red_packet": ["红包来", "红包到", "派红包"],
    "777_lucky_streak_win": ["连胜", "运气爆", "手气热", "连中"],
    "643_gacha_pull_dream": ["抽卡梦", "歪了", "出货", "保底"],
    "071_game_gacha_pull": ["抽卡", "氪金", "出货呀", "黑屏"],
    "282_birthday_moment": ["生日时", "今天生", "我生日", "蛋糕日"],
    "679_birthday_surprise_chat": ["生日惊", "生日礼", "惊喜生"],
    "231_festival_moments": ["过节", "节日", "节庆", "佳节"],
    "T5_celebration_joy": ["欢庆", "庆祝", "贺一下", "庆贺"],
    # ----- cat behavior -----
    "495_cat_daily_life": ["铲屎", "猫砂盆", "猫便", "清理盆"],
    "067_cat_tail_stepped": ["踩尾巴", "尾巴疼", "踩到尾"],
    "280_tail_stepped": ["尾被踩", "踩尾哭", "尾疼"],
    "410_tail_stepped": ["踩尾巴疼", "踩尾呜", "尾巴", "踩疼"],
    # ----- outfit / appearance -----
    "058_tease_master_outfit": ["搭色", "颜色搭", "配色乱"],
    "271_outfit_collapse": ["搭塌房", "穿崩", "搭难看"],
    "270_tease_fat": ["体重秤", "称重", "上称", "数字"],
    # ----- focus / digital -----
    "573_phone_left_home": ["没带机", "脱机", "戒断屏", "解放眼"],
    "042_daydream_zoning_out": ["发呆", "走神", "出神", "放空"],
    "514_chat_filler": ["闲聊填", "随口", "话填", "凑话"],
    "820_self_talk_mumble": ["自语", "嘀咕", "嘟囔", "碎念"],
    # ----- questions / status -----
    "122_are_you_there": ["有空吗", "陪聊吗", "在不在"],
    "004_are_you_there": ["还在吗", "有人吗", "在线吗"],
    "255_are_you_there": ["在么呢", "陪我", "回应"],
    "421_are_you_there": ["在没在", "还活着", "应一声"],
    "518_are_you_there_ping": ["@一下", "戳一下", "喊话"],
    "289_where_are_you_now": ["在哪呢", "你在哪", "现在哪", "位置"],
    "454_what_time_ask": ["还多久", "倒计时", "几小时", "等多久"],
    "519_what_doing_now": ["在干嘛", "干啥呢", "忙啥", "做啥"],
    "188_status_check": ["状态咋", "在状态", "状态如"],
    "485_status_check_in": ["报状态", "打卡状态", "汇报"],
    "391_busy_check": ["闲不闲", "忙么", "有空否"],
    "008_busy_or_free": ["忙闲问", "空么", "空闲否"],
    # ----- work / commute -----
    "031_stuck_traffic": ["车祸", "事故了", "救护车", "堵成"],
    "771_commute_jam_vent": ["堵爆", "通勤堵", "上下班"],
    "442_commute_tired": ["通勤累", "上下班", "挤累"],
    "V6_overtime_rage": ["又加班", "怒了", "没完没了", "天天加"],
    "821_tired_overtime": ["加班累", "加班久", "晚下班"],
    "570_elevator_stuck_wait": ["电梯门", "等电梯", "急催", "抢梯"],
    "684_phone_low_battery": ["电量低", "电池少", "快没电", "电只剩"],
    "927_phone_battery_low": ["手机电", "电量危", "充会儿"],
    "685_charger_lost_panic": ["充电器", "线找不", "充不上", "线丢"],
    "381_lost_thing_panic": ["找不到", "找东西", "丢了"],
    "605_lost_keys_panic": ["钥匙丢", "找钥匙", "门锁"],
    "850_keys_lost_hunt": ["钥匙找", "钥匙没", "翻钥匙"],
    "768_lost_item_panic": ["东西丢", "找物", "翻包找"],
    "770_meeting_zone_out": ["开会神游", "会议走神", "会无聊"],
    "926_meeting_boring": ["会无聊", "开会闷", "会议困", "会拖"],
    "140_work_slack": ["摸鱼", "划水", "偷懒"],
    "236_work_slack": ["上班摸", "工时摸", "划摸"],
    "139_study_anxiety": ["考前", "学习焦", "复习", "考虑"],
    "811_bus_missed_run": ["错过车", "追公交", "误车"],
    "812_subway_crowded_squish": ["地铁挤", "高峰挤", "挤地铁"],
    "571_bus_packed_chat": ["公交挤", "车上挤", "塞满"],
    # ----- daily life -----
    "077_noon_lunch_break": ["午休时", "中午歇", "饭后"],
    "221_noon_break": ["午歇", "午饭后", "午间"],
    "254_lunch_break": ["午餐", "午饭去", "午时吃"],
    "287_noon_lunch_break": ["中午饭", "午休歇", "中午躺"],
    "320_noon_lunch_break": ["午间歇", "饭点歇", "午歇会"],
    "260_take_rest": ["歇一下", "歇会儿", "缓一下"],
    "528_take_rest_care": ["歇着点", "别累着", "歇会"],
    "401_rest_well": ["好好歇", "休息好", "睡好"],
    "560_hiccup_chat": ["打嗝", "嗝声", "嗝住"],
    "699_yawn_contagious": ["哈欠传", "传染哈"],
    "628_yawn_contagious": ["哈欠大", "下颌酸", "笑大"],
    "629_stretching_lazy": ["懒洋洋", "起身活", "舒展"],
    "554_warm_bath_chat": ["泡澡", "热水澡", "泡汤"],
    "936_headache_complain": ["头疼", "脑壳痛", "头炸", "偏头"],
    "557_stiff_neck_complain": ["脖子僵", "落枕", "颈痛", "扭头疼", "膏药"],
    "950_nose_tickle": ["流涕", "鼻塞", "纸巾", "擤鼻"],
    "793_eye_strain_screen": ["熬夜脸", "眼圈黑", "浮肿脸", "睡眠差"],
    # ----- shopping / haul -----
    "507_shopping_haul": ["开箱", "拆包", "战利品", "新买"],
    "841_unboxing_excitement": ["货不对", "买家秀", "退货", "失望"],
    "816_screenshot_share": ["截图", "分享图", "贴图给", "晒图"],
    # ----- chat / play -----
    "744_random_fun_chat": ["吹牛", "牛逼", "装比", "夸口"],
    "513_music_humming": ["哼调", "唱歌", "节奏哼"],
    "818_random_humming": ["随便哼", "顺嘴哼", "哼曲", "走调"],
    "661_bored_idle_chat": ["无聊聊", "闲着", "闲得慌"],
    "665_jeer_master_chat": ["嘲笑", "讽你", "调侃主"],
    "677_ambiguous_flirt_chat": ["暧昧聊", "撩话", "暧昧"],
    "775_couple_pda_react": ["秀恩爱", "撒糖", "情侣", "羡慕情"],
    "726_reverse_flirt_back": ["反撩", "撩回", "怼回"],
    "163_flirting_back": ["撩回去", "反撩主", "回怼"],
    "130_flirt_back": ["怼回", "嘴回", "回撩"],
    "399_blush_dodge": ["脸红", "躲开", "羞了", "害羞"],
    "200_anti_routine": ["反套路", "套路怼", "反梗"],
    "229_emoji_react": ["表情包回", "表情回", "贴表情"],
    "725_emoji_awsl_juejue": ["awsl", "绝绝", "啊死了"],
    "229_emoji_react.yaml": ["表情回", "贴表情", "回表"],
    "132_small_complaints": ["小抱怨", "小吐槽", "小事烦"],
    "125_seeking_praise": ["求夸", "讨夸", "想被夸"],
    "109_feigned_cold": ["装高冷", "假冷淡", "故作冷"],
    "461_jealous_chuyi": ["吃醋了", "醋意", "酸味"],
    "014_want_company": ["要陪伴", "陪我吧", "想有人"],
    "225_want_comfort": ["求安慰", "讨安抚", "讨疼"],
    "462_want_comfort": ["想被疼", "求哄", "哄哄"],
    # ----- caring / greetings -----
    "T6_caring_greeting": ["关心问", "嘘寒问", "暖问候"],
    "461_jealous_chuyi.yaml": ["吃醋了", "酸了酸"],
    # ----- sick / fragile -----
    "II5_sick_complain": ["病了哭", "感冒了", "病恹"],
    "II6_fever_anxiety": ["发烧", "高烧", "体温"],
    "CCC6_pet_sick": ["宠物病", "猫病了", "送医"],
    "NNN8_parent_sick": ["父母病", "爸妈生", "家人病"],
    # ----- crisis / fear -----
    "U2_scared_startled": ["吓一跳", "吓死", "受惊", "猛地"],
    "U3_acting_pitiful": ["装可怜", "装弱", "卖惨"],
    "HH9_call_police": ["报警", "110", "找警察"],
    "OO5_travel_burnout": ["旅行累", "行程累", "出游疲"],
    "OOO6_social_injustice": ["不公", "不平", "社会丑"],
    "WW5_team_lost": ["队输了", "输比赛", "输了局"],
    "BB6_sleep_training": ["训练睡", "调作息", "改睡时"],
    "FF6_homesick": ["想家", "思乡", "想家人", "回不去"],
    "GGG5_game_addiction": ["打游戏", "久坐", "屏幕久", "通宵"],
    "DD9_hobby_judged": ["爱好被", "没人懂", "孤独癖"],
    "JJJ7_local_dispute": ["不沟通", "懒理", "躺平", "摆烂"],
    # ----- beauty / lifestyle -----
    "B2_beauty_skincare": ["护肤", "敷面膜", "保养"],
    "B3_photography": ["拍照", "摄影", "构图", "出片"],
    "91_health_fitness": ["健身", "锻炼", "减脂"],
    "877_coffee_shop_chill": ["咖啡店", "咖啡馆", "咖啡香", "拿铁"],
    "NUM_095_101": ["数字梗", "数字串", "号码梗"],
    "756_morning_kiss_tease": ["晨亲", "早安亲", "早起亲"],
    "338_kiss_beg_shy": ["求亲害", "讨亲羞", "亲一下"],
    "901_blanket_steal_fight": ["抢被子", "盖被", "被子争"],
}

INTENT_TAGS = {
    "complaint": ["呜呜", "崩了", "心累", "想哭"],
    "question": ["怎么办", "推荐个", "哪个好", "如何选"],
    "playful": ["蹭蹭", "嘿嘿", "撒娇", "好玩"],
}

GENERIC_PADDING = ["独白", "提及", "插话", "嘀咕", "嘟囔", "聊起", "随口说", "突然说"]

def get_filestem(fname):
    return fname.rsplit('.',1)[0]

results = []
unmapped = set()
for r in batch:
    name = r['name']
    file = r['file']
    keywords = set(r['keywords'])
    intent = r['intent']
    utterances = r.get('utterances', [])

    stem = get_filestem(file)
    ctx = list(TOPIC_CTX.get(stem, []))
    if not ctx:
        unmapped.add(stem)

    # Filter: remove ctx that overlaps keywords or wrong length
    ctx = [c for c in ctx if c not in keywords and 2 <= len(c) <= 4]

    # Dedup
    seen = set()
    final = []
    for c in ctx:
        if c not in seen and c.strip():
            seen.add(c)
            final.append(c)

    # Pad with intent tags
    if len(final) < 4:
        for tag in INTENT_TAGS.get(intent, []):
            if tag not in seen and tag not in keywords and 2 <= len(tag) <= 4:
                seen.add(tag)
                final.append(tag)
                if len(final) >= 4:
                    break

    # Pad with generic
    if len(final) < 4:
        for tag in GENERIC_PADDING:
            if tag not in seen and tag not in keywords and 2 <= len(tag) <= 4:
                seen.add(tag)
                final.append(tag)
                if len(final) >= 4:
                    break

    # Cap at 6
    final = final[:6]
    results.append({"name": name, "ctx": final})

# Verify
problems = 0
for i, item in enumerate(results):
    if len(item['ctx']) < 4 or len(item['ctx']) > 6:
        problems += 1
        print(f"BAD LEN {i} {item['name']}: {item['ctx']}")
    kw = set(batch[i]['keywords'])
    overlap = [c for c in item['ctx'] if c in kw]
    if overlap:
        problems += 1
        print(f"OVERLAP {item['name']}: {overlap}")
    for c in item['ctx']:
        if not (2 <= len(c) <= 4):
            problems += 1
            print(f"BAD CHARLEN {item['name']}: {c}")
        if not c.strip():
            problems += 1
            print(f"EMPTY {item['name']}")
print(f"problems: {problems}, total: {len(results)}")
print(f"unmapped topics ({len(unmapped)}):")
for u in sorted(unmapped):
    print("  ", u)

json.dump(results, open('e:/VC/Catty/data/_b21_results.json','w',encoding='utf-8'), ensure_ascii=False, indent=1)
print(f"saved {len(results)} results")
