"""Generate disambiguate_context for batch 10. Heuristic + topic-keyed."""
import json
import re

data = json.load(open('e:/VC/Catty/data/disambig_round2_tasks.json','r',encoding='utf-8'))
batch = data[10]

# Topic-to-context mapping based on file name semantics.
# Key = substring of file name. Value = list of ctx words to apply when topic matches.
TOPIC_CTX = {
    # food/snack
    'takeout_slow': ['外卖', '配送', '骑手', '等餐'],
    'fish_dried_treat': ['鱼干', '小鱼干', '猫零食', '抢吃'],
    'tongue_burn_hot': ['烫到舌', '舌头烫', '太烫了', '解烫'],
    'cup_noodle': ['泡面', '夜宵', '老坛', '酸菜'],
    'instant_noodle': ['泡面', '加蛋', '夜宵', '碗面'],
    'midnight_snack': ['夜宵', '半夜饿', '夜里吃', '宵夜'],
    'food_craving': ['想吃', '馋了', '解馋', '美食'],
    'miss_chinese_food': ['想吃', '中餐', '老家味', '馋'],
    'spicy_food': ['吃辣', '辣味', '辣度', '解辣'],
    'spicy_tears': ['辣哭', '辣到', '辣眼泪', '解辣'],
    'hotpot_dream': ['火锅', '涮锅', '锅底', '想吃'],
    'eat_well': ['吃饭', '一日三餐', '正餐', '关心'],
    'ate_yet': ['吃没吃', '吃了吗', '问吃饭', '关心'],
    'meal_check': ['吃饭', '正餐', '问吃没', '关心'],
    'eat_check_in': ['吃饭', '一顿', '正餐', '关心'],
    'eat_properly': ['好好吃', '正餐', '一日三餐', '关心'],
    'popcorn_movie': ['爆米花', '电影院', '看电影', '观影'],
    'cat_fish_snack': ['鱼干', '小鱼干', '猫零食', '想吃'],
    'neko_fish_snack': ['鱼干', '小鱼干', '猫零食', '想吃'],
    'grocery_dropped': ['丢菜', '撒一地', '采购', '袋子'],
    'street_encounter': ['路上', '街上', '碰见', '在外面'],
    'lost_thing': ['丢东西', '找不到', '掉了', '不见'],
    'small_things': ['日常琐事', '小东西', '小事', '日常'],
    'lost_something_road': ['路上丢', '街上掉', '丢东西', '找不到'],
    'road_pickup_lost': ['路上丢', '掉了', '找不回', '丢东西'],
    'umbrella_forgot_wet': ['忘带伞', '淋雨', '湿透了', '没伞'],
    'rainboots_puddle': ['雨鞋', '水坑', '踩水', '雨天'],
    'weather_rain': ['下雨', '雨天', '雨水', '湿气'],
    'rainy_day_mood': ['下雨', '雨天', '阴天', '心情'],
    'rainy_window': ['雨天', '窗外雨', '听雨', '雨声'],
    'rest_concern': ['休息', '别累', '关心', '注意'],
    'rest_well_care': ['好好休息', '关心', '别累', '注意'],
    'care_lifestyle': ['作息', '生活', '关心', '健康'],
    'care_drinking': ['喝水', '关心', '别喝太多', '健康'],
    'take_rest': ['休息会', '歇歇', '别累', '关心'],
    'rest_well': ['好好休息', '关心', '别累', '注意'],
    'take_rest_care': ['休息会', '关心', '歇歇', '别累'],
    'take_rest_remind': ['歇歇', '动一动', '别久坐', '提醒'],
    'eye_strain': ['眼睛累', '盯屏幕', '用眼', '护眼'],
    'eye_strain_chat': ['眼睛累', '护眼', '盯屏', '用眼'],
    'eye_strain_screen': ['屏幕看久', '眼疲劳', '护眼', '盯屏'],
    'sleep_early_remind': ['早睡', '提醒睡', '别熬', '关心'],
    'yawn_sleepy': ['打哈欠', '困', '想睡', '犯困'],
    'yawn_sleepy_chat': ['哈欠', '困意', '想睡', '迷糊'],
    'yawn_sleepy_drop': ['困倒', '撑不住', '哈欠', '想睡'],
    'night_cant_sleep': ['睡不着', '失眠', '夜里', '助眠'],
    'sleep_struggle': ['睡不醒', '挣扎', '困', '起不来'],
    'sleep_yet_ask': ['睡了吗', '关心', '问候', '夜话'],
    'slept_yet_ask': ['睡了吗', '关心', '问候', '夜话'],
    'sleep_yet': ['睡了吗', '关心', '问候', '夜话'],
    'are_you_there': ['在吗', '醒着吗', '关心', '问候'],
    'are_you_there_check': ['在吗', '在不在', '关心', '问候'],
    'status_check_in': ['报备', '在哪呢', '状态', '关心'],
    'night_sleep_chat': ['夜里', '入睡', '困意', '夜话'],
    'morning_wake_up': ['起床', '醒来', '清晨', '床上'],
    'morning_wakeup': ['起床', '醒来', '清晨', '床上'],
    'morning_wakeup_chat': ['起床', '醒来', '清晨', '床上'],
    'morning_grumpy': ['起床气', '清晨', '脾气', '早上'],
    'morning_grouchy_mood': ['早上低气压', '清晨', '不爽', '早起'],
    'early_morning': ['清晨', '一大早', '早起', '凌晨'],
    'morning_alarm_anger': ['闹钟', '吵醒', '清晨', '烦躁'],
    'alarm_clock_hate': ['闹钟', '讨厌闹钟', '吵醒', '清晨'],
    'oversleep_panic': ['睡过头', '迟到', '慌乱', '赶时间'],
    'morning_call': ['早安', '晨呼', '清晨', '起床'],
    'morning_hello': ['早安', '清晨', '问候', '晨呼'],
    'stretch_morning_yawn': ['伸懒腰', '清晨哈欠', '起床', '困意'],
    'noon_lunch_break': ['午休', '中午', '午饭', '小憩'],
    'lunch_break_chat': ['午休', '中午', '小憩', '午饭'],
    'noon_break': ['午休', '中午', '午饭', '小憩'],
    'afternoon_drowsy': ['下午困', '午后', '犯困', '困意'],
    'late_for_work': ['迟到', '上班', '赶时间', '慌乱'],
    'oversleep_panic_alt': ['睡过头', '迟到', '慌张', '赶时间'],
    'small_morning_grump': ['清晨小事', '不爽', '抱怨', '早上'],
    'small_complaints': ['小抱怨', '日常吐槽', '不顺', '碎碎念'],
    'small_grievance': ['小委屈', '小事', '吐槽', '碎碎念'],
    'random_grievance': ['委屈', '吐槽', '不公平', '碎碎念'],
    'random_chat': ['闲聊', '随便聊', '日常', '碎碎念'],
    'random_emoji_spam': ['表情刷屏', '颜文字', '满屏', '日常'],
    'random_meme_laugh': ['网络梗', '沙雕', '笑死', '玩梗'],
    'cold_outside': ['外面冷', '出门冷', '寒风', '降温'],
    'warm_clothing': ['穿暖', '保暖', '加衣', '别冻'],
    'dress_warm_care': ['穿暖', '保暖', '加件衣', '关心'],
    'dress_warm_care_alt': ['穿暖', '保暖', '加衣', '关心'],
    'wear_warm_remind': ['穿暖', '保暖', '提醒', '加衣'],
    'weather_chat': ['天气', '气温', '冷热', '天聊'],
    'window_gaze_chat': ['窗外', '发呆', '看外面', '凝视'],
    'cold_hands_warm': ['手冷', '取暖', '暖手', '握手'],
    'cold_shoulder_act': ['冷战', '不理你', '冷处理', '装哑'],
    'jealous_pout': ['吃醋', '撅嘴', '小不爽', '气鼓鼓'],
    'jealousy_pouting': ['吃醋', '撅嘴', '小不爽', '气鼓鼓'],
    'chicu_jealous': ['吃醋', '偏心', '气鼓鼓', '小不爽'],
    'jealous_petty': ['吃醋', '小气', '哼', '气鼓鼓'],
    'pouty_sulking': ['撅嘴', '生气', '别扭', '小情绪'],
    'sulky_pouting': ['撅嘴', '闹脾气', '小情绪', '别扭'],
    'sulky_hmph_pout': ['哼', '撅嘴', '生气', '小情绪'],
    'hmph_pout_sound': ['哼', '撅嘴', '别扭', '小情绪'],
    'hmph_pout_face': ['哼', '撅嘴', '小情绪', '别扭'],
    'hng_pout_tsundere': ['哼', '傲娇', '撅嘴', '不要'],
    'hum_pout_face': ['哼', '撅嘴', '小情绪', '别扭'],
    'hmph_sulk': ['哼', '生气', '撅嘴', '别扭'],
    'hmph_huff': ['哼', '撅嘴', '小情绪', '不爽'],
    'petty_pout_moment': ['小气', '撅嘴', '小情绪', '别扭'],
    'rolling_demand': ['打滚要', '撒泼', '撒娇', '耍赖'],
    'sajiao_clingy': ['撒娇', '黏人', '依赖', '抱抱'],
    'tsundere_lie': ['嘴硬', '才不', '傲娇', '别扭'],
    'throwing_tantrum': ['闹脾气', '不要', '撒泼', '耍赖'],
    'anti_routine': ['反套路', '不接梗', '冷淡', '怼'],
    'anti_routine_cold': ['反套路', '冷处理', '装忙', '怼'],
    'anti_trope': ['反套路', '不上钩', '冷淡', '不接'],
    'feigned_cold': ['假冷淡', '装作', '反套路', '冷处理'],
    'pretend_blind': ['假装看不见', '装瞎', '装没看见', '装作'],
    'slack_off_work': ['摸鱼', '装忙', '上班', '摆烂'],
    'workfish_idle_chat': ['摸鱼', '装忙', '工位', '划水'],
    'work_slack': ['摸鱼', '装忙', '上班', '划水'],
    'work_slack_moyu': ['摸鱼', '装忙', '上班', '划水'],
    'boss_check_panic': ['老板来', '装忙', '紧张', '看见了'],
    'office_slack': ['办公室', '摸鱼', '装忙', '划水'],
    'tease_master_weight': ['吐槽体重', '调侃', '胖瘦', '玩梗'],
    'tease_master_fat': ['调侃胖', '吐槽', '逗笑', '玩梗'],
    'pretty_movie_night': ['电影夜', '看片', '观影', '一起看'],
    'fish_dried_treat_alt': ['鱼干', '小鱼干', '抢吃', '猫零食'],
    'jealous_pout_alt': ['吃醋', '撅嘴', '气鼓鼓', '小情绪'],
    'unconvinced': ['不服', '不甘心', '不平', '气'],
    'wuwu_crying': ['呜呜', '哭哭', '委屈', '想哭'],
    'cry_wuwu_sob': ['哭哭', '呜呜', '不想说话', '一个人静'],
    'jealous_petty_alt': ['吃醋', '小气', '哼', '气鼓鼓'],
    'sticker_share': ['表情包', '发图', '沙雕图', '玩梗'],
    'screenshot_share': ['截图', '发图', '分享', '看到'],
    'cute_animal_share': ['动物图', '发图', '可爱', '羊驼'],
    'emoji_war_battle': ['表情包大战', '斗图', '玩梗', '发图'],
    'emoji_war': ['表情包', '斗图', '玩梗', '发图'],
    'laugh_die_meme': ['笑死梗', '沙雕', '玩梗', '发图'],
    'meme_pic_exchange': ['表情包', '发图', '搞笑', '玩梗'],
    'praise_me': ['夸我', '夸夸', '求表扬', '可爱吗'],
    'seeking_praise': ['求夸', '求表扬', '夸我', '棒不棒'],
    'beg_for_praise': ['求夸', '夸夸', '求表扬', '可爱吗'],
    'kuajiang_praise_beg': ['求夸', '夸夸', '夸我', '好看吗'],
    'flirt_back_blush': ['脸红', '害羞', '反撩', '别撩了'],
    'praise_beg': ['求夸', '夸我', '夸夸', '可爱吗'],
    'need_praise': ['求夸', '夸我', '夸夸', '可爱吗'],
    'praised_today': ['被夸了', '今天被夸', '开心', '得意'],
    'want_praise': ['求夸', '夸我', '夸夸', '可爱吗'],
    'want_compliment': ['求夸', '赞我', '夸夸', '可爱吗'],
    'seek_compliment': ['求夸', '夸我', '夸夸', '可爱吗'],
    'emotion_seek_praise': ['考好了', '求夸', '炫耀', '得意'],
    'festival_qixi_moment': ['节日', '过节', '庆祝', '氛围'],
    'festival_birthday': ['节日', '庆祝', '过节', '氛围'],
    'festival_moment': ['节日', '过节', '氛围', '庆祝'],
    'festival_moments': ['节日', '过节', '氛围', '庆祝'],
    'festival_tradition': ['节日传统', '过节', '习俗', '氛围'],
    'holiday_moment': ['节日', '过节', '假期', '氛围'],
    'holiday_moment_alt': ['节日', '假期', '过节', '氛围'],
    'shovel_return': ['铲屎官', '回家', '下班', '辛苦'],
    'shoveler_return': ['铲屎官', '回家', '下班', '辛苦'],
    'off_work_tired': ['下班累', '加班', '回家', '辛苦'],
    'tired_overtime': ['加班累', '熬夜', '辛苦', '心累'],
    'no_hobby_time': ['没爱好', '没时间玩', '累瘫', '生活'],
    'busy_or_free': ['有空吗', '忙不忙', '关心', '问候'],
    'busy_or_not': ['有空吗', '忙不忙', '问候', '关心'],
    'busy_or_not_ask': ['有空吗', '忙不忙', '关心', '问候'],
    'asking_busy_now': ['现在忙吗', '关心', '问候', '在做什么'],
    'what_doing_now': ['在干嘛', '现在做啥', '关心', '问候'],
    'what_doing': ['在干嘛', '做什么', '关心', '问候'],
    'bored_idle_chat': ['好无聊', '没事干', '闲', '找你玩'],
    'bored_killing_time': ['无聊', '打发时间', '闲', '没事干'],
    'rest_concern_alt': ['休息', '别累', '关心', '注意'],
    'water_reminder': ['喝水', '关心', '提醒', '注意'],
    'drink_water_check': ['喝水', '关心', '提醒', '注意'],
    'drink_water_remind': ['喝水', '提醒', '关心', '注意'],
    'water_drink_remind': ['喝水', '提醒', '关心', '注意'],
    'diet_struggle': ['减肥', '节食', '瘦身', '管住嘴'],
    'midnight_snack_urge': ['夜宵', '半夜饿', '宵夜', '深夜'],
    'midnight_snack_chat': ['夜宵', '半夜', '宵夜', '深夜'],
    'midnight_snack_crave': ['夜宵', '想吃', '深夜', '宵夜'],
    'late_night': ['半夜', '深夜', '夜里', '夜话'],
    'late_night_call': ['半夜聊', '深夜', '夜话', '夜里'],
    'night_chitchat': ['夜聊', '深夜', '夜话', '夜里'],
    'night_farewell': ['晚安', '道别', '睡前', '夜话'],
    'night_chat': ['夜话', '深夜', '夜里', '夜聊'],
    'phone_low_battery': ['手机没电', '低电量', '充电', '电量'],
    'phone_low_battery_chat': ['手机没电', '低电量', '关机', '充电'],
    'phone_battery_low': ['手机没电', '关键时刻', '低电量', '关机'],
    'friendship_chat': ['朋友', '友情', '关系', '社交'],
    'anxious_waiting_reply': ['等回复', '焦虑', '在不在', '没回'],
    'missed_message': ['漏消息', '没看到', '翻聊天', '消息'],
    'lost_thing_panic': ['找不到', '慌乱', '丢了', '焦急'],
    'where_are_you_now': ['你在哪', '找不到你', '关心', '位置'],
    'emotion_seek_anwei': ['求安慰', '抱抱', '难过', '心疼'],
    'anwei_comfort_seek': ['求安慰', '抱抱', '难过', '心疼'],
    'low_self_esteem': ['没用', '没价值', '怀疑自己', '自卑'],
    'skill_plateau': ['瓶颈', '没进步', '怀疑天赋', '卡住'],
    'skill_plateau_extreme': ['极度怀疑', '天赋', '瓶颈', '崩'],
    'no_invite': ['没人约', '孤独', '冷落', '怀疑'],
    'need_comfort': ['求安慰', '抱抱', '难过', '心疼'],
    'need_comfort_hug': ['求抱', '抱抱', '难过', '心疼'],
    'seek_comfort': ['求安慰', '抱抱', '难过', '心疼'],
    'want_comfort': ['求安慰', '抱抱', '难过', '心疼'],
    'spoiled_clingy': ['黏人', '撒娇', '抱抱', '不放手'],
    'spoil_clingy': ['黏人', '撒娇', '抱抱', '不放手'],
    'beg_cuddle': ['求抱', '抱抱', '撒娇', '黏'],
    'hug_petting': ['抱抱', '摸摸', '撸', '亲密'],
    'hug_beg_lap': ['求抱', '腿上', '抱抱', '黏'],
    'head_pat': ['摸头', '揉头', '撒娇', '亲密'],
    'head_pat_beg': ['求摸头', '揉头', '撒娇', '亲密'],
    'head_pat_request': ['求摸头', '揉头', '撒娇', '亲密'],
    'ask_for_headpat': ['求摸头', '揉头', '撒娇', '亲密'],
    'reverse_flirt_blush': ['反撩', '脸红', '害羞', '别撩'],
    'reverse_tease': ['反撩', '逗你', '调侃', '撩回'],
    'ambiguous_tease': ['暧昧', '撩', '调侃', '害羞'],
    'heihei_yingying': ['嘿嘿', '颜文字', '笑声', '玩闹'],
    'heihei_giggle': ['嘿嘿', '偷笑', '笑声', '玩闹'],
    'emoji_heihei_laugh': ['嘿嘿', '颜文字', '笑声', '玩梗'],
    'emoji_haha': ['哈哈', '颜文字', '笑声', '玩梗'],
    'emoji_reactions': ['颜文字', '表情', '反应', '玩梗'],
    'emoji_text': ['颜文字', '文字符号', '表情', '玩梗'],
    'text_emoji': ['颜文字', '文字符号', '表情', '玩梗'],
    'emoji_wuwu_yingying': ['呜呜', '颜文字', '哭哭', '委屈'],
    'number_meme': ['数字梗', '666', '玩梗', '弹幕'],
    'emoji_react': ['表情反应', '颜文字', '玩梗', '弹幕'],
    'sticker_battle': ['斗图', '表情包', '玩梗', '发图'],
    'hehe_giggle': ['呵呵', '笑声', '偷笑', '玩闹'],
    'micro_emotion': ['情绪波动', '突然', '感受', '心情'],
    'mood_swings': ['情绪波动', '突然', '心情', '感受'],
    'sudden_surprise': ['惊喜', '突然', '意外', '激动'],
    'surprise_moment': ['惊喜', '意外', '突然', '激动'],
    'zaoan_morning_greeting': ['早安', '清晨', '问候', '心情'],
    'sunny_day_warm': ['阳光好', '晴天', '出门', '暖洋洋'],
    'weekend_lazy_idea': ['周末', '懒散', '安排', '玩'],
    'weekend_plan_chat': ['周末', '安排', '玩', '出门'],
    'lazy_sunday_morning': ['周日懒', '懒散', '周末', '不想动'],
    'sunny_day_warm_alt': ['阳光', '晴天', '暖和', '出门'],
    'busy_idle_chat_alt': ['有空吗', '忙不忙', '关心', '问候'],
    'first_snow_chat': ['初雪', '下雪', '雪天', '路滑'],
    'first_snow_excite': ['初雪', '激动', '下雪', '雪天'],
    'snow_first_sight': ['初雪', '看雪', '下雪', '激动'],
    'heat_wave_die': ['酷暑', '高温', '热到', '解暑'],
    'leg_cramp_pain': ['抽筋', '腿疼', '缺钙', '酸痛'],
    'warm_bath_chat': ['泡澡', '热水', '澡盆', '舒服'],
    'sleep_check_in': ['在哪呢', '状态', '问候', '报备'],
    'traffic_commute': ['通勤', '上班路上', '挤车', '堵车'],
    'commute_jam_vent': ['堵车', '通勤', '上班路上', '心累'],
    'late_night_alt': ['深夜', '半夜', '夜里', '夜话'],
    'morning_alarm_anger_alt': ['闹钟', '清晨', '吵醒', '烦躁'],
    'gacha_pull': ['抽卡', '氪金', '十连', '欧非'],
    'gacha_pull_luck': ['抽卡', '氪金', '欧非', '运气'],
    'gacha_pull_dream': ['抽卡', '氪金', '欧非', '运气'],
    'gacha_chat': ['抽卡', '氪金', '欧非', '运气'],
    'gacha_luck': ['抽卡', '氪金', '欧非', '运气'],
    'no_money_buy': ['没钱', '买不起', '钱包空', '消费'],
    'shopping_haul': ['购物', '买买', '消费', '剁手'],
    'tea_milk_chat': ['奶茶', '茶饮', '糖度', '点单'],
    'game_gacha_pull': ['打野', '上单', '中单', '英雄联盟'],
    'game_vibes': ['打野', '英雄', '游戏', '匹配'],
    'game_session': ['打野', '游戏', '排位', '匹配'],
    'jungle_carry_glory': ['打野', '带飞', '英雄', 'carry'],
    'lucky_streak_win': ['连胜', '运气好', '打野', 'carry'],
    'noob_teammate_rage': ['坑队友', '演员', '挂机', '骂队友'],
    'ranked_climb_grind': ['上分', '排位', '冲段', '演员'],
    'game_mood': ['游戏心情', '氪金', '排位', '匹配'],
    'study_cram': ['考试', '通过', '上岸', '高分'],
    'study_exam_chat': ['考试', '复习', '脑子转不动', '备考'],
    'memorize_textbook': ['背书', '课文', '记忆', '卡壳'],
    'self_talk_mumble': ['自言自语', '念叨', '碎碎念', '绕口令'],
    'brain_blank': ['断片', '想不起', '空白', '忘了'],
    'language_anxiety': ['学语言', '卡壳', '焦虑', '说不出'],
    'cant_focus': ['专注不了', '想睡觉', '走神', '学不进'],
    'praise_beg_alt': ['求夸', '夸我', '夸夸', '可爱吗'],
    'juejuezi_meme': ['绝绝子', '拉满', '梗', '玩梗'],
    'moon_round_night': ['月亮', '月圆', '思念', '夜空'],
    'full_moon_night': ['月圆', '思念', '怀念', '夜空'],
    'insomnia_thinking': ['失眠', '想念', '夜里', '辗转'],
    'anniversary_sweet': ['纪念日', '回忆', '甜蜜', '过往'],
    'couple_pda_react': ['情侣', '秀恩爱', '异地', '羡慕'],
    'random_chat_alt': ['闲聊', '日常', '随便聊', '碎碎念'],
    'are_you_there_alt': ['在吗', '醒着吗', '关心', '问候'],
    'sleep_check': ['睡了吗', '关心', '问候', '夜话'],
    'cat_daily': ['猫日常', '日常', '出门', '关心'],
    'morning_grouchy_mood_alt': ['早起', '低气压', '清晨', '不爽'],
    'rest_concern_x': ['休息', '别累', '关心', '注意'],
    'window_gaze_chat_alt': ['窗外', '发呆', '凝视', '看外面'],
    'snowfall_excite': ['下雪', '雪花', '初雪', '激动'],
    # extra topics
    'act_cute_beg': ['撒娇', '装可爱', '求宠', '黏'],
    'anime_marathon': ['追番', '熬夜看', '黑眼圈', '通宵'],
    'ate_yet_question': ['吃没吃', '关心', '问吃饭', '一日三餐'],
    'bath_skincare_routine': ['洗澡', '护肤', '洗漱', '睡前'],
    'battery_low_panic': ['电量低', '充电', '插座', '咖啡店'],
    'bengbu_zhu': ['绷不住', '无语', '玩梗', '哭笑'],
    'bengbuzhule_meme': ['绷不住', '玩梗', '哭笑', '无语'],
    'blue_light_eyes': ['蓝光', '远眺', '护眼', '盯屏'],
    'bubble_tea_crave': ['奶茶', '糖度', '热量', '想喝'],
    'bus_packed_chat': ['公交', '挤车', '刷卡', '通勤'],
    'camera_envy': ['换相机', '羡慕', '新机', '想买'],
    'cat_daily_life': ['猫日常', '回家', '日常', '生活'],
    'charger_lost_panic': ['充电器', '丢了', '找不到', '焦急'],
    'chat_filler': ['算了', '没事', '没啥', '随便'],
    'clock_check': ['时间', '整点', '看表', '报时'],
    'cold_shiver': ['哆嗦', '冷颤', '保暖', '热水'],
    'cold_winter_chat': ['冬天', '下雪', '冷', '冬日'],
    'commute_traffic': ['通勤', '下班路', '挤车', '堵车'],
    'commute_traffic_tired': ['通勤累', '上班路', '没电', '堵车'],
    'complaint': ['网络卡', '吐槽', '不爽', '抱怨'],
    'counter_flirt': ['反撩', '馋你', '调侃', '撩回'],
    'crisp_snack_attack': ['薯片', '乐事', '零食', '想吃'],
    'cultural_inferiority': ['失望', '文化', '国家', '感叹'],
    'cup_noodle_late': ['泡面', '夜宵', '老坛', '酸菜'],
    'dawn_first_light': ['天亮', '闹钟', '清晨', '黎明'],
    'did_you_eat': ['好吃', '吃饭', '关心', '问候'],
    'dream_chat': ['梦', '噩梦', '惊醒', '做梦'],
    'energy_drink_chat': ['红牛', '提神', '能量饮', '熬夜'],
    'feigned_cold': ['装坚强', '装作没事', '假冷淡', '撑'],
    'fish_dried_treat': ['鱼干', '小鱼干', '抢吃', '猫零食'],
    'flirt_back': ['反撩', '摸摸头', '调侃', '撩回'],
    'food_complaint': ['点错', '难吃', '吐槽', '失望'],
    'food_disappoint': ['太咸', '难吃', '失望', '吐槽'],
    'fried_chicken_lust': ['炸鸡', '鸡腿', '想吃', '解馋'],
    'goodnight_chat': ['晚安吻', '睡前', '晚安', '夜话'],
    'goodnight_routine': ['刷牙', '睡前', '洗漱', '夜里'],
    'grammar_pain': ['虚拟语气', '语法', '看不懂', '学英语'],
    'grocery_dropped_oops': ['摔倒', '撒一地', '买菜', '袋子'],
    'hairdryer_use': ['吹风机', '吹头发', '太响', '洗澡后'],
    'health_fitness': ['眼酸', '健身', '锻炼', '保健'],
    'hiccup_chat': ['打嗝', '嗝', '止嗝', '想喝水'],
    'hobby_boredom': ['热情消退', '腻了', '不想玩', '兴趣淡'],
    'hot_drink_winter': ['热水', '太烫', '烫嘴', '冬日'],
    'hot_summer_complain': ['出汗', '酷热', '夏天', '热到'],
    'hungry_food': ['想喝奶茶', '饿', '馋', '解馋'],
    'hungry_midnight': ['泡面', '半夜饿', '夜宵', '深夜'],
    'insomnia_late': ['熬夜', '失眠', '睡不着', '夜里'],
    'just_wake_groggy': ['刚醒', '脸肿', '迷糊', '清晨'],
    'late_night_call': ['孤独', '深夜', '夜话', '一个人'],
    'lazy_mode': ['躺尸', '懒散', '不想动', '摆烂'],
    'lianhong_blush_dodge': ['脸红', '害羞', '别撩', '躲'],
    'litter_box_litter': ['猫砂', '猫日常', '出门久', '便便'],
    'lol_dying_laugh': ['搞笑', '笑死', '玩梗', '哈哈'],
    'lost_something_road': ['手机丢', '路上掉', '丢东西', '找不到'],
    'lottery_dice_play': ['抽奖', '骰子', '猜数', '运气'],
    'lovesickness': ['相思', 'TA不回', '思念', '等回复'],
    'marriage_crisis': ['冷战', '婚姻', '情感危机', '吵架'],
    'meal_check_in': ['没胃口', '吃饭', '关心', '一日三餐'],
    'meow_daily_chatter': ['猫日常', '挠柱', '猫语', '猫生活'],
    'midnight_chat': ['熬夜', '深夜', '伤肝', '夜话'],
    'midnight_zoomies_chat': ['深夜跑酷', '猫嗨', '夜里疯', '猫日常'],
    'milk_tea_addict': ['奶茶上瘾', '失眠', '咖啡因', '想喝'],
    'mirror_check_self': ['照镜子', '我帅吗', '看脸', '自夸'],
    'monday_blues_dread': ['周一', '不想上班', '抗拒', '蓝色星期一'],
    'music_mood': ['听歌', '音乐', '歌单', '心情'],
    'neck_pain': ['脖子痛', '久坐', '颈椎', '酸痛'],
    'night_moon_stars': ['月亮', '星空', '夜空', '抬头看'],
    'night_topic': ['夜里', '回忆涌上', '深夜', '夜话'],
    'no_friends': ['没朋友', '孤独', '冷落', '感慨'],
    'old_dog_trick': ['脑子不灵', '老了', '迟钝', '感叹'],
    'owner_got_fat': ['减肥', '变胖', '体重', '吐槽'],
    'pet_lost': ['宠物丢', '找不到', '焦急', '走丢'],
    'phone_left_home': ['手机落家', '没带手机', '忘带', '焦急'],
    'phone_lost_panic': ['手机丢', '照片', '慌乱', '焦急'],
    'pocket_warm_hands': ['暖手', '旧大衣', '兜里', '冬天'],
    'random_blessing': ['祝福', '好运', '加油', '日常'],
    'random_fart_burp': ['伸懒腰', '日常', '小动作', '碎碎'],
    'random_sigh_chat': ['加油', '感叹', '日常', '碎碎'],
    'random_thought': ['乱想', '突发奇想', '碎碎', '感慨'],
    'red_packet_surprise': ['红包', '礼物', '惊喜', '收到'],
    'road_encounter': ['路上', '街上', '碰见', '捡到'],
    'seek_company': ['陪伴', '一起', '别走', '在身边'],
    'seek_praise': ['求夸', '夸我', '夸夸', '可爱吗'],
    'sick_complain': ['感冒', '生病', '不舒服', '难受'],
    'slack_off_work': ['摸鱼', '装忙', '老板', '划水'],
    'slang_meme': ['网络词', 'YYDS', '解释', '梗'],
    'sleep_buddy_chat': ['一起睡', '陪睡', '夜话', '窗外雨'],
    'sleep_lost': ['睡眠浅', '失眠', '夜里醒', '睡不沉'],
    'sleep_training': ['日夜颠倒', '作息', '调整', '睡眠'],
    'small_happiness_share': ['小确幸', '感恩', '分享', '开心'],
    'small_happy_moment': ['捡到便宜', '小开心', '幸运', '小事'],
    'smile_til_cry_chat': ['笑死', '666', '玩梗', '哈哈'],
    'spicy_food_lover': ['麻辣烫', '辣味', '吃辣', '解辣'],
    'stairs_climb_tired': ['爬楼', '气喘', '累', '楼梯'],
    'stiff_neck_complain': ['落枕', '转头痛', '颈椎', '酸痛'],
    'stomach_growl': ['饿', '肚子叫', '叫外卖', '想吃'],
    'stretch_lazy_arm': ['瑜伽', '伸懒腰', '拉伸', '懒'],
    'subway_commute': ['地铁', '通勤', '挤车', '一小时'],
    'subway_crowded_squish': ['地铁挤', '人多', '味道', '通勤'],
    'suddenly_cold': ['突然冷淡', '不理人', '不理我', '变冷'],
    'surprise_redpacket': ['红包', '邀请', '惊喜', '收到'],
    'sweet_tooth_crave': ['甜筒', '甜食', '甜品', '想吃'],
    'tea_calm_moment': ['泡茶', '茶具', '茶香', '静心'],
    'takeout_slow_anger': ['外卖慢', '骑手', '配送', '迟'],
    'tease_master': ['吐槽主人', '调侃', '塌房', '吐槽'],
    'tease_owner': ['吐槽主人', '变胖', '调侃', '吐槽'],
    'voice_call_chat': ['语音', '唱歌', '通话', '听声音'],
    'want_company': ['无聊', '陪我', '想找人', '孤单'],
    'weather_rain_chat': ['下雨', '雨天', '湿透', '天聊'],
    'weather_too_cold': ['热茶', '取暖', '冷天', '保暖'],
    'weekend_plan': ['周末', '自然醒', '安排', '玩'],
    'where_are_you_now': ['你在哪', '又跑了', '找不到你', '关心'],
    'wifi_disconnect_rage': ['信号差', '掉线', 'WIFI', '网卡'],
    'window_weather_chat': ['窗外', '风大', '天气', '看外面'],
    'windy_day_chat': ['风大', '头发吹乱', '大风', '出门'],
    'yawn_morning_stretch': ['哈欠', '清晨', '眼泪', '伸懒腰'],
    'phone_battery_low': ['没信号', '手机', '电量', '关机'],
    'morning_coffee_first': ['咖啡', '提神', '清晨', '冲淡'],
    'office_gossip_chat': ['办公室', '八卦', '奖金', '同事'],
    'dark_circles_sigh': ['黑眼圈', '冰敷眼', '熬夜', '没睡好'],
}

# generic intent-based fallback words to mix in
INTENT_EXTRA = {
    'complaint': ['累', '崩', '呜', '烦'],
    'question': ['推荐', '怎么', '哪个', '问'],
    'playful': ['玩', '逗', '调侃', '嘿嘿'],
}

# All other intents fallthrough to playful-ish
DEFAULT_INTENT_EXTRA = ['日常', '聊', '关心', '玩闹']

# Build per-route ctx by topic_hint lookup with fallback.
def file_to_topic(fname):
    # strip leading nums and trailing .yaml
    s = fname.replace('.yaml','')
    # drop leading id like 345_ or U1_ or A6_ or FF8_ or GGG9_
    s = re.sub(r'^[A-Z]*\d+[A-Z]*_', '', s)
    return s

def pick_ctx(route):
    name = route['name']
    fname = route.get('file','')
    keywords = set(route.get('keywords', []))
    utterances = route.get('utterances', [])
    intent = route.get('intent','')
    topic = file_to_topic(fname)
    # Lookup exact topic; if not in dict, try partial substring match
    ctx_pool = []
    if topic in TOPIC_CTX:
        ctx_pool.extend(TOPIC_CTX[topic])
    else:
        # find any topic key that is a substring of `topic`
        for k, v in TOPIC_CTX.items():
            if k in topic or topic in k:
                ctx_pool.extend(v)
                break
    # add intent extras
    if intent in INTENT_EXTRA:
        ctx_pool.extend(INTENT_EXTRA[intent])
    else:
        ctx_pool.extend(DEFAULT_INTENT_EXTRA)
    # dedupe and remove anything that is exactly in keywords
    seen = []
    for w in ctx_pool:
        if w in keywords:
            continue
        # also skip if keyword contains w as substring (too similar) — but only when it would be redundant
        # We keep it permissive: only block exact match.
        if w not in seen:
            seen.append(w)
    # ensure 4-6
    if len(seen) < 4:
        # pad with utterance-derived hints
        for u in utterances:
            cleaned = u.replace('{user_addr}','')
            # take 2-4 char chunk
            cleaned = cleaned[:4]
            if cleaned and cleaned not in keywords and cleaned not in seen:
                seen.append(cleaned)
            if len(seen) >= 4:
                break
    if len(seen) < 4:
        # last-resort generic
        for w in ['关心','日常','互动','聊聊','陪伴']:
            if w not in keywords and w not in seen:
                seen.append(w)
            if len(seen) >= 4:
                break
    # trim to 6
    return seen[:6]

results = []
for r in batch:
    ctx = pick_ctx(r)
    # Final safety: ensure no ctx word is in keywords
    kws = set(r.get('keywords', []))
    ctx = [c for c in ctx if c not in kws]
    # Ensure 4-6
    if len(ctx) < 4:
        # pad with fallback safe words not in keywords
        for w in ['关心','日常','聊聊','陪伴','互动','问候']:
            if w not in kws and w not in ctx:
                ctx.append(w)
            if len(ctx) >= 4:
                break
    ctx = ctx[:6]
    results.append({'name': r['name'], 'ctx': ctx})

# verify
assert len(results) == 220, f"Expected 220 got {len(results)}"
for r in results:
    assert 4 <= len(r['ctx']) <= 6, f"{r['name']} ctx length {len(r['ctx'])}"
    for w in r['ctx']:
        assert isinstance(w,str) and w.strip(), f"{r['name']} empty ctx"

# Coverage: how many used a topic key vs fallback
topic_keys = set(TOPIC_CTX.keys())
hit, miss = 0, 0
miss_topics = {}
for r in batch:
    t = file_to_topic(r['file'])
    if t in topic_keys:
        hit += 1
    else:
        # also try substring
        if any(k in t or t in k for k in topic_keys):
            hit += 1
        else:
            miss += 1
            miss_topics[t] = miss_topics.get(t,0)+1
print(f"Topic hits: {hit}, misses: {miss}")
print("Top miss topics:")
for k,v in sorted(miss_topics.items(), key=lambda x:-x[1])[:30]:
    print(' ', k, v)

# Save output
json.dump(results, open('e:/VC/Catty/data/_batch10_out.json','w',encoding='utf-8'), ensure_ascii=False)
print(f"Wrote {len(results)} results")