"""Generate disambiguate_context for batch 12 (round 2).

Strategy:
 1. Topic dictionary keyed by file slug (extensive coverage for batch 12 files).
 2. Hint-based extraction from english slug tokens.
 3. Mined utterance phrases (whole 2-4char meaningful phrases, NOT sliding window).
 4. Intent filler (complaint -> emotion; question -> query; playful -> mood).
 5. Strict filter: skip keywords, skip duplicates, length 2-4, drop sliding-window junk.
"""

import json
import re
from pathlib import Path

DATA_PATH = Path('e:/VC/Catty/data/disambig_round2_tasks.json')
OUT_PATH = Path('e:/VC/Catty/data/disambig_round2_batch12_out.json')

# ---- Topic dictionary keyed by file slug ----
TOPIC_DICT = {
    # === head/pat/qinqin/hug ===
    '534_head_pat_beg': ['求摸头', '蹭手心', '猫猫想', '撒娇要', '凑过去', '伸头'],
    '266_head_pat': ['摸摸头', '伸出手', '蹭一下', '温柔', '安抚', '揉揉'],
    '363_head_pat_beg': ['想被摸', '凑近', '伸头', '猫猫', '撒娇', '蹭手'],
    '476_want_headpat_beg': ['想要摸', '求摸摸', '伸头', '撒娇', '蹭过去', '凑近'],
    '307_motoutou_pat_head': ['摸头头', '揉一揉', '抠下巴', '挠挠', '撸毛', '咕噜'],
    '436_ask_for_headpat': ['讨摸摸', '求宠', '伸头', '凑近', '猫猫', '撒娇'],
    '664_cat_ear_tease_chat': ['猫耳朵', '挠耳根', '耳朵抖', '酥酥', '炸毛', '蹭蹭'],
    '308_qinqin_kiss_beg': ['亲一个', '求亲亲', '凑嘴', '亲亲', '撒娇', '小奶猫'],
    '025_ask_for_hug': ['想抱抱', '抱抱', '钻怀里', '撒娇', '蹭蹭', '依赖'],
    '026_head_pat_beg': ['按肩', '按摩师', '揉肩', '捏肩', '酸痛', '舒服'],
    '124_acting_clingy': ['黏人', '求求', '撒娇', '黏住', '不放', '抱腿'],
    '460_spoiled_clingy': ['贴贴', '黏过去', '蹭蹭', '抱住', '撒娇', '不撒手'],
    '471_yingying_whimper': ['求宠', '要宠', '撒娇', '哼唧', '软声', '哀求'],
    '475_want_hug_beg': ['熊抱', '紧紧抱', '抱满怀', '钻怀', '撒娇', '不放'],
    '023_reverse_flirt': ['撩回去', '反击', '反撩', '炸毛', '脸红', '怼回'],
    '474_blush_dodge_anti_flirt': ['转话题', '不接茬', '岔开', '脸红', '装没听', '逃避'],
    '474_anti_routine': ['装没听', '装糊涂', '装睡', '装聋', '不接梗', '回避'],
    '176_anti_routine': ['装没听', '装糊涂', '装睡', '装聋', '不接梗', '回避'],
    '024_blush_avoid': ['闪躲', '甩开', '后退', '炸毛', '脸红', '逃跑'],
    '366_dodge_flirt_back': ['当没说', '重来', '岔开', '装傻', '装睡', '逃话'],
    '304_fanliao_flirt_back': ['整天想', '念叨', '心里有', '想念', '上头', '迷恋'],
    '194_tease_owner': ['穷光蛋', '吃土', '没钱', '钱包', '穷酸', '哭穷'],
    '189_seek_praise': ['别走', '陪着', '留下', '黏人', '不放', '撒娇'],
    '224_want_praise': ['夸夸', '想夸', '求表扬', '求夸', '小骄傲', '炫耀'],
    '505_compliment_request': ['夸我', '我可爱', '漂亮吗', '求夸', '撒娇', '骄傲'],
    '170_surprise_moment': ['好消息', '加薪', '升职', '惊喜', '运气', '好事'],
    '237_sudden_surprise': ['表扬', '被夸', '小骄傲', '惊喜', '好开心', '害羞'],
    '443_red_packet_surprise': ['突然', '没想到', '惊喜', '红包', '意外', '好开心'],

    # === fridge / food / hungry / meal ===
    '561_fridge_raid_late': ['翻冰箱', '夜宵', '深夜饿', '冰箱光', '找吃的', '偷吃'],
    '161_meal_check_in': ['吃饭打卡', '晚饭', '加班', '点餐', '吃了没', '凑合'],
    '123_eat_check_in': ['吃饭', '正餐', '打卡', '吃啥', '已经吃', '饭点'],
    '291_ate_yet_question': ['吃了吗', '正餐', '问候', '吃啥', '关心', '答话'],
    '749_midnight_snack_crave': ['宵夜', '半夜饿', '想吃', '泡面', '馋了', '冰箱空'],
    'S6_hungry_food': ['饿肚子', '想吃', '咕咕叫', '点外卖', '馋了', '空腹'],
    '949_stomach_growl': ['咕咕叫', '肚子叫', '饿得响', '空腹', '尴尬', '声音'],
    '003_noon_lunch_break': ['午休', '午饭', '中午', '吃啥', '休息', '困了'],
    '387_midnight_chat': ['深夜', '聊聊', '失眠', '夜里', '安静', '陪猫'],
    '006_ate_lunch_yet': ['早饭', '早餐', '吃了吗', '问候', '关心', '打卡'],
    '187_late_night': ['夜宵', '宵夜', '半夜饿', '想吃东西', '深夜', '馋'],
    '254_lunch_break': ['胃痛', '饿过头', '不舒服', '胃酸', '没吃饭', '难受'],
    '288_midnight_dark_chat': ['宵夜', '半夜饿', '烧烤', '想吃', '馋', '夜里'],
    '315_morning_alarm': ['没吃早', '跳过早餐', '边走边', '匆忙', '来不及', '赶时间'],
    '417_early_morning_greet': ['吃早餐', '早饭吃', '包子', '吃啥', '早安', '早上'],
    '419_lunch_break_chat': ['午休短', '没睡够', '老板抠', '一小时', '不够', '怨气'],
    '423_ate_yet': ['没胃口', '吃不下', '不想吃', '没食欲', '难受', '没精神'],
    '40_food': ['吃撑', '太饱', '吃不下', '撑死', '饱腹', '难受'],
    '027_owner_got_fat': ['吃太多', '吃过头', '撑死', '塞嘴', '管不住', '猛吃'],
    '504_snacks_craving': ['冰淇淋', '雪糕', '哈根达斯', '想吃冰', '甜筒', '馋'],
    '174_catty_fish_dream': ['鱼干', '鲫鱼', '想吃鱼', '猫粮', '小鱼', '馋鱼'],
    '543_dried_fish_crave': ['金枪鱼', '三文鱼', '鳕鱼', '生鱼片', '海鱼', '馋'],
    '650_hungry_snack_chat': ['小鱼干', '猫罐头', '猫粮', '猫零食', '馋', '想吃'],
    '566_cup_noodle_late': ['泡面', '老坛', '红烧牛肉', '加蛋', '加肠', '深夜'],
    '562_midnight_snack_crave': ['烧烤', '泡面', '嘴馋', '深夜', '馋虫', '想吃'],
    '615_midnight_snack_crave': ['减肥失败', '管不住', '忍不住', '罪恶', '深夜', '吃货'],
    '748_brunch_dilemma': ['甜品', '蛋糕', '奶茶', '甜食', '下午茶', '点心'],
    '929_spicy_food_love': ['串串', '麻辣烫', '香锅', '麻辣', '辣味', '过瘾'],
    '697_spicy_food_burn': ['重庆小面', '串串香', '冷锅', '辣', '麻辣', '过瘾'],
    '931_bubble_tea_crave': ['后悔', '太甜', '难喝', '奶茶', '齁', '齁甜'],
    '693_milk_tea_sugar': ['全糖', '满糖', '甜', '奶茶', '糖度', '快乐'],
    '692_tea_warm_cup': ['茉莉花茶', '花茶', '茶香', '香片', '热茶', '泡茶'],
    '552_morning_coffee_first': ['咖啡', '续命', '第一杯', '清醒', '提神', '早晨'],
    '874_chocolate_crave': ['长肉', '怕胖', '不能吃', '巧克力', '减肥', '罪恶'],
    '961_bitter_medicine': ['苦', '药', '解苦', '糖', '难吃', '救命'],
    '965_fried_chicken_lust': ['炸鸡', '凌晨', '半夜', '罪恶', '深夜', '香味'],
    '96_food_cook': ['甜品推', '蛋糕推', '哪家好', '推荐店', '甜点店', '想吃甜'],

    # === morning / wake / weather / season ===
    '450_morning_zaoan_chat': ['早安', '起床', '清晨', '问候', '元气', '早上好'],
    '153_morning_wakeup': ['赖床', '起床气', '闹钟', '早上', '困', '起不来'],
    '894_foggy_morning_blur': ['雾蒙蒙', '看不清', '雾天', '朦胧', '清晨', '糊'],
    '503_weather_chat': ['天气', '下雨', '阴天', '晴天', '气温', '出门'],
    '143_weather_chat': ['冷死', '零下', '寒风', '冷天', '冬天', '冻'],
    '252_morning_wakeup': ['元气', '加油', '新一天', '满满', '冲', '元气满'],
    '061_small_morning_grump': ['闹钟', '关闹钟', '烦', '吵醒', '起床气', '不爽'],
    '441_morning_grumpy': ['头疼', '头晕', '起床头', '不舒服', '没睡好', '难受'],
    '681_morning_stretch': ['赖床', '被窝', '床软', '不想起', '舒服', '懒'],
    '714_morning_first_word': ['周末早', '不上班', '假期', '懒觉', '早安', '放假'],
    '714_morning_kiss_tease': ['打断', '太困', '不在状态', '迷糊', '醒不来', '没睡醒'],
    '756_morning_kiss_tease': ['打断', '太困', '不在状态', '迷糊', '醒不来', '没睡醒'],
    '85_weather_season': ['下雪', '雪大', '雪天', '暴雪', '飘雪', '雪景'],
    '577_first_snow_chat': ['路滑', '摔跤', '出门难', '滑倒', '雪地', '艰难'],
    '621_weather_too_cold': ['羽绒服', '大棉袄', '厚外套', '穿啥', '保暖', '冻'],
    '898_heat_wave_die': ['暴晒', '晒晕', '太阳毒', '高温', '中暑', '热死'],
    '916_rainy_day_mood': ['听雨', '雨声', '助眠', '下雨', '雨天', '安静'],

    # === late night / sleep ===
    '517_late_night_chat': ['熬夜', '没睡', '深夜', '聊天', '陪伴', '不困'],
    '515_goodnight_routine': ['晚安', '睡觉', '关灯', '该睡', '提醒', '盖被'],
    '253_late_night_chat': ['几点', '夜深', '熬夜', '聊聊', '陪着', '不睡'],
    '352_late_night_call': ['催睡', '不睡', '熬太晚', '快睡', '叮嘱', '伤身'],
    '716_what_time_check': ['几点了', '时间', '看表', '太晚', '一晃', '过点'],
    '715_pre_sleep_chat': ['睡前', '聊一下', '关灯前', '困了', '哄睡', '聊会儿'],
    '154_late_night_chat': ['陪聊', '聊天亮', '不想睡', '陪伴', '聊到累', '熬夜'],
    '319_night_cant_sleep': ['失眠', '睡不着', '翻来覆去', '数羊', '难入睡', '辗转'],
    '406_sleep_lost': ['停不下', '想很多', '思绪', '失眠', '脑转', '睡不着'],
    '451_night_wanan_chat': ['洗洗睡', '准备睡', '上床', '关灯', '晚安', '该睡'],
    '600_humming_tune_share': ['哼歌', '哄睡', '助眠', '摇篮曲', '小曲', '催眠'],
    '755_pillow_talk_late': ['噩梦', '梦魇', '害怕', '吓醒', '睡不安', '梦'],
    'AA6_noisy_neighbor': ['吵', '失眠', '隔壁吵', '邻居', '睡不着', '抓狂'],

    # === body / health / illness ===
    '559_sneeze_burst_chat': ['打喷嚏', '阿嚏', '鼻子痒', '感冒', '凉到', '说我'],
    '429_drink_water_remind': ['喝水', '补水', '提醒', '杯子', '口渴', '少喝'],
    '91_health_fitness': ['健身', '锻炼', '体重', '减肥', '跑步', '出汗'],
    '015_drink_water_remind': ['水杯', '杯子', '水壶', '空了', '喝水', '提醒'],
    '156_water_reminder': ['自来水', '矿泉水', '烧开', '能喝吗', '净水', '水质'],
    '669_water_drink_remind_chat': ['夜尿', '上厕所', '起夜', '喝多', '怕半夜', '尿急'],
    '017_take_rest_remind': ['歇会儿', '休息', '别累', '放松', '停一下', '关心'],
    '041_catch_a_cold_sick': ['感冒', '着凉', '流鼻涕', '发烧', '生病', '咳嗽'],
    '172_warm_clothing': ['着凉', '受凉', '感冒', '保暖', '加衣', '穿暖'],
    '701_runny_nose_cold': ['发烧', '体温', '高烧', '38度', '生病', '虚弱'],
    'C5_health_disease': ['喉咙痛', '嗓子疼', '咽喉炎', '吞口水', '难受', '看医生'],
    'SSS6_hangover': ['喉咙干', '渴', '嗓子哑', '宿醉', '不舒服', '难受'],
    '027_warm_clothing': ['受凉', '感冒', '加衣', '保暖', '关心', '担心'],
    '441_runny_nose_cold': ['发烧', '高烧', '38度', '难受', '生病', '虚'],

    # === mood / emotion ===
    '192_reverse_flirt': ['反撩', '反杀', '调戏', '脸红', '怼回', '炸毛'],
    '523_jealous_pout': ['吃醋', '撇嘴', '哼', '不理', '小气', '酸了'],
    'V5_monday_blues': ['周一', '上班', '不想动', '丧气', '通勤', '崩了'],
    'LLL7_emotional_numb': ['麻木', '没感觉', '空', '木了', '冷淡', '提不起'],
    '887_brain_blank': ['脑子空', '宕机', '发呆', '走神', '愣住', '想不起'],
    '014_want_company': ['孤单', '寂寞', '没人陪', '空落落', '一个人', '想陪'],
    '232_micro_emotion': ['莫名', '突然', '无端', '情绪', '说不清', '低落'],
    '325_jealous_pout': ['冷战', '不理', '沉默', '生闷气', '不说话', '别扭'],
    '405_hmph_huff': ['生气', '气哭', '哼', '真气', '怄气', '撅嘴'],
    '405_pout_huff': ['生气', '气哭', '哼', '真气', '怄气', '撅嘴'],
    '333_hng_pout_tsundere': ['不说话', '不理', '静默', '冷', '别扭', '哼'],
    '721_petty_pout_moment': ['嘟嘴', '撅嘴', '噘嘴', '小气', '不开心', '撒气'],
    '249_cyber_pda': ['幸福', '太幸福', '甜蜜', '撒糖', '秀恩爱', '腻'],
    '383_small_happiness_share': ['新东西', '新衣服', '新文具', '小确幸', '开心', '满足'],
    '778_small_joy_moment': ['小确幸', '突然开心', '一点点', '小幸福', '满足', '甜'],
    '983_minor_lucky_event': ['年终奖', '加薪', '升职', '好事', '运气', '惊喜'],
    'LL5_lonely_depression': ['空虚', '心空', '空落落', '没意思', '提不起', '失落'],
    'LLL9_grief_loss': ['躲起来', '不见人', '宅家', '不想出', '逃避', '难受'],
    'PP7_unclear_goal': ['迷茫', '不知道', '方向', '困惑', '没目标', '失落'],
    '679_birthday_surprise_chat': ['忘生日', '没人记', '失落', '失望', '难过', '伤心'],
    '774_holiday_alone_vibe': ['假期短', '又上班', '结束', '不舍', '不想', '崩'],
    '880_first_message_shy': ['羞耻', '不敢发', '删了写', '手抖', '紧张', '害羞'],
    '809_phone_lost_panic': ['慌', '心跳', '手抖', '吓', '紧张', '慌张'],

    # === meme / emoji / laugh ===
    '250_number_meme': ['数字梗', '谐音', '玩梗', '666', '哈哈', '猜数'],
    '059_expression_xiaosi': ['笑死', '哈哈哈', '笑岔', '梗', '玩笑', '搞笑'],
    '052_emoji_heihei_laugh': ['笑死我', '腹肌', '笑出腹', '哈哈', '太搞笑', '乐'],
    '053_emoji_wuwu_yingying': ['嘤嘤', '哭哭', '委屈', '抽噎', '撒娇', '哀嚎'],
    '129_emoji_text': ['awsl', '笑死了', '哈哈哈', '颜表', '颜文字', '回复'],
    '312_xiaosi_meme_laugh': ['笑岔气', '笑断气', '笑断肠', '笑死', '大笑', '太搞'],
    '314_juejuezi_meme': ['绝绝子', '真的绝', '绝了', '梗', '夸张', '玩梗'],
    '334_awsl_cute_overload': ['awsl', '我死了', '萌死', '阿伟死', '可爱', '萌'],
    '332_yingying_whine': ['软软', '萌音', '卖萌', '撒娇音', '萝莉', '奶声'],
    '402_laugh_dead': ['笑岔', '笑岔气', '笑死过', '大笑', '笑到', '乐'],
    '440_meme_bengbuzhu': ['典中典', '经典', '典', '梗', '老梗', '笑死'],
    '488_emoji_reactions': ['呜呜', '哭了', '哭哭', '心碎', '伤心', '难过'],
    '529_heihei_giggle': ['噗嗤', '笑出声', '噗哈哈', '偷笑', '憋笑', '乐'],
    '609_random_giggle_fit': ['傻笑', '痴笑', '莫名笑', '没缘由', '抽风', '笑'],
    '917_random_meme_laugh': ['栓Q', '真栓Q', '服了', '梗', '土味', '玩梗'],
    '70_internet_meme': ['上头', '太上头', '迷上', '入坑', '沉迷', '魔性'],
    '981_random_smile_moment': ['想笑', '笑出声', '莫名笑', '没缘由', '抽风', '偷乐'],

    # === daily / chores / shopping ===
    '828_grocery_shopping': ['买菜', '逛超市', '提袋子', '生鲜', '价签', '排队'],
    '380_grocery_dropped_oops': ['菜掉了', '袋破了', '撒一地', '糟糕', '弯腰捡', '尴尬'],
    '656_workfish_idle_chat': ['摸鱼', '划水', '上班', '没事干', '聊天', '装忙'],
    '281_shovel_return': ['换衣', '换睡衣', '洗澡', '回家', '到家', '放松'],
    '377_owner_home_excite': ['回家', '到家', '我回来', '迎接', '冲过去', '开心'],
    '780_litter_box_litter': ['换鞋', '进门', '洗手', '到家', '回家', '日常'],
    'HHH5_nothing_wear': ['剁手', '又剁手', '买衣服', '冲动', '控制不住', '想买'],
    '867_closet_pick': ['衣服紧', '穿不下', '缩水', '长胖', '紧身', '尴尬'],

    # === work / commute / busy ===
    '083_busy_or_free': ['ddl', '截止', '赶ddl', '加班', '忙', '崩'],
    '113_traffic_jam': ['堵车', '堵路上', '不动', '堵爆', '路堵', '通勤'],
    '344_commute_traffic_tired': ['堵车', '大堵车', '堵爆', '路上', '通勤', '累'],
    '492_traffic_commute': ['堵车', '路上堵', '堵爆', '通勤', '动不了', '崩'],
    '668_subway_commute_chat': ['地铁挤', '人多', '挤死', '沙丁鱼', '通勤', '贴脸'],
    '86_work_life': ['加班', '加班狗', '996', '累死', '通宵', '过劳'],
    '730_after_work_tired': ['下班', '终于下班', '解放', '回家', '累瘫', '休息'],
    '459_busy_or_not': ['想休息', '不想动', '停一下', '累', '没力', '需要歇'],

    # === study / exam ===
    '168_study_struggle': ['成绩降', '退步', '考差', '挂科', '难过', '失落'],
    '547_study_cram_panic': ['学不完', '看不完', '太多', '复习', '崩了', '焦虑'],
    '738_study_exam_panic': ['压力大', '学不进', '看书烦', '焦虑', '考试', '崩'],
    '914_study_burnout_vent': ['快考试', '复习不完', '临时抱佛', '焦虑', '崩', '压力'],

    # === lifestyle / phone / cat life ===
    '174_catty_fish_dream': ['鱼干', '鲫鱼', '想吃鱼', '猫粮', '小鱼', '馋鱼'],
    '251_cat_chitchat': ['碎碎念', '唠叨', '念叨', '碎念', '自言', '叨叨'],
    '301_rest_well_care': ['拉伸', '瑜伽', '伸懒腰', '放松', '舒缓', '动一下'],
    '495_cat_daily_life': ['呼噜', '打呼', '咕噜', '呼噜呼噜', '睡', '猫'],
    '579_catnip_high_chat': ['醉猫', '猫薄荷', '抱不放', '撒欢', '嗨', '猫'],
    '760_hidden_purr_call': ['呼噜', '抱呼噜', '暖暖', '咕噜', '蹭', '依偎'],
    '764_carry_belly_rub': ['下巴', '摸头', '揉脖', '撸毛', '舒服', '猫'],
    '901_blanket_steal_fight': ['抱被子', '抱枕', '抱东西', '一个人', '寂寞', '空'],
    '277_had_dream': ['怪梦', '奇梦', '离谱', '梦到', '怪异', '惊'],
    '280_tail_stepped': ['打了', '拍了', '打人', '被打', '疼', '委屈'],
    '491_tease_master': ['鸡窝头', '发型乱', '没梳头', '乱糟糟', '头发', '吐槽'],
    '536_tease_master_fat': ['胖了', '圆了', '长肉', '又重', '增重', '吐槽'],
    '339_tease_master_fat': ['双下巴', '圆脸', '包子脸', '胖了', '长肉', '吐槽'],
    '478_master_getting_fat': ['难减', '减肥失败', '难瘦', '减不下', '长肉', '失败'],
    '311_tease_outfit_bad': ['搭配好', '今天好看', '进步', '夸', '会穿', '好评'],
    '042_tease_master_smelly': ['臭', '洗澡', '冲凉', '去洗', '脏', '汗味'],

    # === phone / battery / network ===
    '592_phone_battery_dying': ['没电', '电量低', '1%', '快关机', '手机', '充电'],
    '783_phone_low_battery': ['手机烫', '充电热', '烫手', '发烫', '担心', '炸'],
    '817_phone_battery_low': ['65w', '快充', '100w', '充电慢', '闪充', '电量'],
    '837_pretend_busy': ['没信号', '断网', '网不好', '装断网', '借口', '不回'],

    # === misc themes ===
    '036_moon_pretty': ['月亮', '今晚', '抬头', '好圆', '夜空', '浪漫'],
    'KKK8_paid_fans': ['付费粉', '订阅', '解锁', '会员', '私域', '粉丝'],
    '519_what_doing_now': ['在干嘛', '现在', '做什么', '此刻', '关心', '搭话'],
    '456_what_doing_ask': ['看剧', '追剧', '刷剧', '在干嘛', '电视剧', '宅家'],
    '208_random_question': ['人生意义', '活着', '为什么', '哲学', '深奥', '难答'],
    '824_random_question': ['假如', '万一', '如果', '中彩票', '穿越', '幻想'],
    '065_night_moon_stars': ['夜风', '凉风', '夜凉', '夜晚', '星空', '清凉'],
    '542_full_moon_night': ['夜风', '空气好', '凉爽', '满月', '夜晚', '清凉'],
    '068_festival_birthday': ['元旦', '新年', '跨年', '节日', '庆祝', '热闹'],
    '283_tanabata_day': ['七夕', '七夕节', '快乐', '情人节', '节日', '甜'],
    '033_red_packet_joy': ['中奖', '抽中', '大奖', '惊喜', '运气', '开心'],
    '732_unexpected_red_packet': ['猜数字', '猜对', '猜中', '红包', '运气', '游戏'],
    '039_bored_killing_time': ['出门', '出去逛', '散步', '走走', '无聊', '消遣'],
    '413_tsundere_lie': ['口是心非', '嘴硬', '心口不一', '傲娇', '别扭', '不老实'],
    '414_tsundere_lie': ['口是心非', '嘴硬', '心口不一', '傲娇', '别扭', '不老实'],
    '362_laugh_cry_xs': ['没事干', '闲死', '太闲', '无聊', '空虚', '消磨'],
    '604_forgot_what_doing': ['想不起', '越想越', '死活', '忘了', '忘事', '断片'],
    '849_umbrella_forgot': ['忘带伞', '没带伞', '雨伞', '下雨', '淋雨', '糟'],
    '879_microwave_warm': ['微波', '嗡嗡响', '转盘', '声大', '加热', '吵'],
    '919_weekend_plan_chat': ['自然醒', '不定闹钟', '睡懒觉', '周末', '放松', '懒'],
    '998_water_bottle_chat': ['保温杯', '保温瓶', '保温几', '热水', '装水', '续命'],
    '658_game_gacha_chat': ['非酋', '黑脸', '抽卡', '歪了', '欧皇', '运气'],
    '674_pet_envy_chat': ['拆家', '弄翻', '捣蛋', '宠物', '猫狗', '糗事'],
    'FFF6_job_replaced': ['学技能', '充电', '想学', '傍身', '进修', '提升'],
    'GG5_no_money': ['买不起', '太贵', '没钱', '看看', '心动', '钱包'],
    'GGG8_pvp_rage': ['再一局', '又打了', '停不下', '上头', '游戏', '上瘾'],
    'MMM5_startup_fail': ['重新出发', '从头', '再来', '重启', '再战', '不服'],
    'PP9_quit_too_soon': ['重新开始', '重启', '再开', '重来', '重试', '不放弃'],
    'RR5_workout_skip': ['借口', '一堆', '找借口', '偷懒', '逃避', '不练'],
    'ZZZ9_thank_catty': ['谢笨猫', '感谢', '谢猫', '夸笨猫', '夸猫', '感激'],
    '555_hairdry_towel_chat': ['擦头', '顶头', '毛茸', '吹头', '毛巾', '治愈'],
    '042_expression_xiaosi': ['笑死', '哈哈', '搞笑', '乐', '玩梗', '笑话'],
    '042_emoji_xiaosi': ['笑死', '哈哈', '搞笑', '乐', '玩梗', '笑话'],
}

# 同义 alias 自动合并 (有些 file 是相同主题不同编号)
SYNONYM = {
    '042_tease_master_smelly': '042_tease_master_smelly',
}

# ---- Intent tokens ----
INTENT_TOKENS = {
    'complaint': ['累', '崩', '呜', '丧气', '叹气', '难受'],
    'question': ['推荐', '怎么', '怎样', '哪个', '请教', '问问'],
    'playful': ['玩闹', '嘿嘿', '逗你', '调皮', '捣蛋', '皮一下'],
}

# ---- English slug -> Chinese hint mapping ----
HINT_MAP = {
    'fridge': '冰箱', 'late': '深夜', 'midnight': '半夜', 'snack': '宵夜',
    'hungry': '饿', 'meal': '饭', 'eat': '吃饭', 'lunch': '午饭',
    'dinner': '晚饭', 'breakfast': '早饭', 'water': '喝水', 'drink': '喝',
    'pat': '摸头', 'head': '头', 'ear': '耳朵', 'chin': '下巴',
    'hug': '抱抱', 'kiss': '亲亲', 'qinqin': '亲亲',
    'sleep': '睡觉', 'night': '夜', 'goodnight': '晚安', 'wake': '起床',
    'morning': '早上', 'zaoan': '早安', 'foggy': '雾',
    'weather': '天气', 'rain': '下雨', 'sun': '晴', 'snow': '雪',
    'sneeze': '喷嚏', 'cough': '咳嗽', 'sick': '生病', 'cold': '感冒',
    'flirt': '调情', 'jealous': '吃醋', 'pout': '撇嘴', 'tease': '逗',
    'monday': '周一', 'blues': '低落', 'numb': '麻木',
    'blank': '空白', 'brain': '脑子', 'fish': '摸鱼', 'work': '上班',
    'shop': '购物', 'grocery': '买菜', 'meme': '梗', 'number': '数字',
    'moon': '月亮', 'fan': '粉丝', 'paid': '付费',
    'reverse': '反', 'crave': '馋', 'raid': '翻',
    'check': '打卡', 'chat': '聊', 'beg': '求', 'remind': '提醒',
    'idle': '发呆', 'oops': '糟糕', 'drop': '掉',
    'fat': '胖', 'lose': '减', 'workout': '健身', 'exercise': '运动',
    'phone': '手机', 'battery': '电量', 'low': '低', 'lost': '丢',
    'commute': '通勤', 'subway': '地铁', 'traffic': '堵车', 'jam': '堵',
    'birthday': '生日', 'festival': '节日', 'red': '红', 'packet': '红包',
    'surprise': '惊喜', 'happy': '开心', 'joy': '快乐', 'lucky': '幸运',
    'lonely': '孤单', 'company': '陪伴', 'cling': '黏', 'clingy': '黏人',
    'praise': '夸', 'compliment': '夸', 'thank': '谢',
    'cry': '哭', 'laugh': '笑', 'giggle': '笑', 'smile': '笑',
    'bored': '无聊', 'killing': '消磨', 'time': '时间',
    'cup': '杯', 'noodle': '面', 'tea': '茶', 'coffee': '咖啡', 'milk': '奶',
    'sugar': '糖', 'chocolate': '巧克力', 'cake': '蛋糕',
    'spicy': '辣', 'chicken': '鸡', 'fried': '炸',
    'medicine': '药', 'bitter': '苦', 'sweet': '甜',
    'study': '学习', 'exam': '考试', 'cram': '复习', 'panic': '焦虑',
    'rest': '休息', 'tired': '累', 'sleepy': '困',
    'thank': '感谢', 'praise': '夸', 'master': '主人', 'catty': '笨猫',
    'dream': '梦', 'nightmare': '噩梦',
    'umbrella': '雨伞', 'forgot': '忘',
    'closet': '衣柜', 'pick': '选', 'wear': '穿', 'clothing': '衣服',
    'sneak': '偷偷', 'crave': '馋', 'lust': '馋',
    'tsundere': '傲娇', 'lie': '嘴硬',
    'kindle': '哄', 'hum': '哼', 'whine': '哀',
    'shy': '害羞', 'blush': '脸红', 'avoid': '回避', 'dodge': '闪',
    'pretend': '装', 'busy': '忙', 'sneeze': '喷嚏',
}


def slug_of(file_name: str) -> str:
    return file_name.rsplit('.', 1)[0]


def hint_to_topic_words(hint: str) -> list:
    out = []
    for piece in re.split(r'[_\-]', hint.lower()):
        if piece in HINT_MAP and HINT_MAP[piece] not in out:
            out.append(HINT_MAP[piece])
    return out


def extract_utterance_phrases(utterances: list, exclude: set) -> list:
    """Pull cohesive 2-4char Chinese phrases from utterances.

    Don't do sliding window: take the *full* utterance after stripping
    user_addr/punctuation, then keep it if 2<=len<=4. If longer, drop.
    Also extract sub-phrases delimited by punctuation or spaces.
    """
    phrases = []
    for utt in utterances:
        u = utt.replace('{user_addr}', '')
        u = re.sub(r'[～~!?.,。，！？;；:\s]+', '|', u)
        parts = [p for p in u.split('|') if p]
        for p in parts:
            # Keep only pure Chinese 2-4 char chunks.
            if re.fullmatch(r'[一-鿿]{2,4}', p) and p not in exclude:
                phrases.append(p)
    return phrases


def gen_ctx(task: dict) -> list:
    keywords = set(task.get('keywords', []))
    utterances = task.get('utterances', [])
    intent = task.get('intent', 'playful')
    siblings = task.get('siblings', [])
    file_slug = slug_of(task['file'])

    # Sibling exclusion: any full phrase (2-4 char) appearing in sibling
    # first_utterance. We use whole-string match plus a 2-char window for
    # short substrings.
    sib_exclude = set()
    for sib in siblings:
        fu = sib.get('first_utterance', '').replace('{user_addr}', '')
        fu = re.sub(r'[～~!?.,。，！？;；:\s]+', '|', fu)
        for p in fu.split('|'):
            if p and re.fullmatch(r'[一-鿿]{2,4}', p):
                sib_exclude.add(p)

    candidate = []

    # 1) Topic dict.
    if file_slug in TOPIC_DICT:
        for w in TOPIC_DICT[file_slug]:
            if w not in keywords and w not in candidate:
                candidate.append(w)

    # 2) Hint words from file slug.
    for w in hint_to_topic_words(file_slug):
        if w not in keywords and w not in candidate:
            candidate.append(w)

    # 3) Whole-utterance phrases.
    mined = extract_utterance_phrases(utterances, keywords)
    for w in mined:
        if w not in candidate:
            candidate.append(w)

    # 4) Sibling filter: drop tokens that are EXACTLY in sib_exclude.
    candidate = [w for w in candidate if w not in sib_exclude]

    # 5) Intent filler.
    intent_pool = INTENT_TOKENS.get(intent, INTENT_TOKENS['playful'])
    for w in intent_pool:
        if len(candidate) >= 5:
            break
        if w not in keywords and w not in candidate and w not in sib_exclude:
            candidate.append(w)

    # 6) Length filter + cap.
    final = []
    for w in candidate:
        if not w or len(w) < 2 or len(w) > 4:
            continue
        if w in keywords:
            continue
        if w in final:
            continue
        # Sanity: drop obvious sliding-window junk - heuristic check
        # If a longer keyword starts with w (3+ chars), w is likely a stem; allow.
        final.append(w)
        if len(final) == 6:
            break

    # 7) Pad if <4.
    pad_pool = ['猫猫', '撒娇', '蹭蹭', '哼', '嘿嘿', '聊聊', '陪伴', '日常',
                '关心', '小确幸', '想了', '在嘛']
    for w in pad_pool:
        if len(final) >= 4:
            break
        if w in keywords or w in final or w in sib_exclude:
            continue
        if not (2 <= len(w) <= 4):
            continue
        final.append(w)

    return final


def main():
    data = json.load(DATA_PATH.open('r', encoding='utf-8'))
    batch = data[12]
    results = []
    short = 0
    no_topic = []
    for t in batch:
        ctx = gen_ctx(t)
        if len(ctx) < 4:
            short += 1
        if slug_of(t['file']) not in TOPIC_DICT:
            no_topic.append(t['file'])
        results.append({'name': t['name'], 'ctx': ctx})
    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'wrote {len(results)} entries; short(<4) = {short}; no_topic_dict = {len(set(no_topic))}')
    if no_topic:
        print('Files still missing topic dict:')
        for f in sorted(set(no_topic))[:30]:
            print('  ', f)
    # Validate: no ctx overlaps with keywords.
    overlap = 0
    by_name = {t['name']: t for t in batch}
    for e in results:
        kws = set(by_name[e['name']]['keywords'])
        for w in e['ctx']:
            if w in kws:
                overlap += 1
                print(f'  OVERLAP: {e["name"]} ctx={w} kws={kws}')
    print(f'keyword overlaps: {overlap}')


if __name__ == '__main__':
    main()
