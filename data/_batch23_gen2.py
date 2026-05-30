# -*- coding: utf-8 -*-
"""
Heuristic disambig_ctx generator for batch 23.
- Extract topic words from file_name slug.
- Combine with curated topic vocab.
- Augment by intent.
- Filter against keywords + sibling first_utterances.
"""
import json, re, sys

data = json.load(open('e:/VC/Catty/data/_batch23.json', 'r', encoding='utf-8'))

# Topic vocab indexed by token appearing in file name slug.
# Each key is a slug token; value is list of disambig candidate words.
SLUG_VOCAB = {
    'head_pat_beg': ['撸下巴', '颌下', '抓痒', '舒服', '咕噜'],
    'noon_break': ['午觉', '午休', '中午困', '回神', '下午'],
    'in_law_conflict': ['婆家', '婆媳', '老一辈', '不公', '心酸'],
    'small_things': ['日常', '琐事', '碎碎念', '小事', '吐槽'],
    'pre_sleep_chat': ['关灯', '床上', '睡前', '数羊', '困不来'],
    'tease_owner': ['沙发瘫', '葛优', '懒虫', '化水', '熔椅'],
    'pimple_panic': ['长痘', '冒痘', '油脸', '毛孔', '泛红'],
    'jealousy_pouting': ['嘟嘴', '甩尾', '哼唧', '小气', '不理人'],
    'tired_fatigue': ['全身酸', '骨头痛', '瘫床', '动不了', '提不起劲'],
    'jealous_vinegar': ['吃醋', '酸了', '小心眼', '抓尾', '醋坛'],
    'phone_battery_low': ['电量条', '充满', '红条', '强迫', '插电'],
    'bengbu_meme': ['蚌埠住', '笑哭', '梗图', '泪奔', '崩不住'],
    'office_slack': ['打工', '工位', '老板催', '会议', 'PPT'],
    'mirror_check_self': ['镜子', '照镜', '镜前', '看自己', '换发型'],
    'flirt_back': ['表白', '嘴甜', '冲冲冲', '心动', '突袭'],
    'emotion_seek_praise': ['夸夸', '夸我', '抱抱奖', '加油棒', '小红花'],
    'internet_meme': ['梗图', '佛了', '不卷', '咸鱼', '摆烂图'],
    'random_grievance': ['谢谢猫', '感激', '靠你', '抱抱猫', '有你在'],
    'phone_battery': ['散热差', '降频', '烧手', '关后台', '冲电烫'],
    'bump_lost_chat': ['迷糊', '反应慢', '蒙圈', '抓瞎', '找不到'],
    'game_mood': ['十连', '保底', '欧气', '非酋', '出货'],
    'traffic_jam_late': ['坐车', '后排', '颠簸', '呕感', '风口'],
    'afternoon_tea_time': ['续命', '波霸', '芋圆', '半糖', '热饮'],
    'micro_emotion': ['莫名', '心花', '哼小调', '蹦跶', '冒泡'],
    'stomach_growl_chat': ['打嗝', '撑肚', '消食', '解腰带', '走不动'],
    'hungry_snack_chat': ['分一口', '尝尝', '喂喂', '递过来', '嘴馋'],
    'cold_caught_sick': ['吞咽痛', '咽炎', '红肿', '消炎', '冰水'],
    'ask_for_headpat': ['呼噜响', '蹭脸', '满足脸', '舒服哼', '咕噜咕噜'],
    'password_forgot': ['登录', '验证码', '找回', '邮箱', '安全题'],
    'morning_coffee_first': ['美式', '拿铁', '现磨', '提神', '续命杯'],
    'need_comfort_hug': ['空虚', '没目标', '迷路', '没方向', '找不到'],
    'sneeze_runny_nose': ['姜汤', '葱白', '驱寒', '保暖汤', '红糖水'],
    'midnight_snack_crave': ['深夜', '罪恶感', '宵夜', '送餐慢', '凉了'],
    'lazy_day_off': ['宅家', '休息日', '懒散', '什么都不做', '葛优'],
    'house_chores': ['囤货', '货架', '主子', '采购', '家务'],
    'owner_no_shave': ['胡渣', '邋遢', '剃须刀', '油腻', '修边'],
    'want_kiss_beg': ['啵啵', '隔空', '飞过来', '心心', '隔屏'],
    'bus_commute_tired': ['挤地铁', '汗渍', '通勤', '换衣', '臭味'],
    'emoji_war_battle': ['斗图', '战绩', '王者', '碾压', '吊打'],
    'ate_yet_question': ['撑死', '腰带松', '吃过头', '走不动', '消化'],
    'want_fish_jerky': ['鱼条', '小鱼干', '酥脆', '咸鱼', '鱼香'],
    'pet_sick': ['毛孩子', '宠物', '心疼', '看医生', '宠物医院'],
    'rest_well_care': ['歇歇', '小憩', '充能', '回神', '缓口气'],
    'bored_idle_chat': ['推荐剧', '片单', '什么好看', '电视剧', '哪部'],
    'late_night_chat': ['静谧', '夜风', '万籁', '独处', '夜晚'],
    'night_sleep_chat': ['凌晨', '半夜', '深夜', '熬过头', '太迟'],
    'social_injustice': ['不公', '社会', '想发声', '改革', '挺身'],
    'tease_master_fat': ['鸡窝头', '炸毛', '没梳', '蓬乱', '起床发'],
    'indoor_lazy_day': ['宅家', '电影日', '马拉松', '一刷再刷', '陪看'],
    'morning_first_word': ['赖床', '不起', '被窝', '掀被', '再睡'],
    'wuwu_crying': ['呜呜', '抽噎', '哭鼻子', '泪汪', '抹眼泪'],
    'wuwu_cry_emoji': ['哭脸', '眼泪表情', '抽噎', '泪奔', '抹泪'],
    'petty_pout_moment': ['嘟嘴', '小傲娇', '嘟囔', '不开心', '撒娇'],
    'mood_swings': ['情绪', '波动', '反复', '突然好', '突然丧'],
    'wuwu_cry': ['抽噎', '哭出声', '泪汪汪', '红眼眶', '心疼自己'],
    'flirt_back_blush': ['脸红', '害羞', '别看', '心跳', '脸热'],
    'fanliao_flirt_back': ['反撩', '撩回去', '脸热', '害羞躲', '心跳'],
    'reverse_flirt': ['反向撩', '主动撩', '突袭', '突然甜', '猝不及防'],
    'jealous_pout': ['醋坛', '吃醋', '占有', '独占', '不分享'],
    'jealous_vinegar': ['酸气', '抓尾', '小心眼', '醋意', '吃干醋'],
    'lazy_mode': ['躺尸', '不动', '化水', '熔椅', '葛优'],
    'lazy_sunday_morning': ['周日', '懒晨', '不起', '赖床', '宅家'],
    'cold_shiver': ['打寒颤', '搓手', '冻僵', '哆嗦', '抖腿'],
    'cold_caught_sick': ['吞咽', '咽炎', '消炎', '红肿', '冰水'],
    'cough_throat_dry': ['咳嗽', '清嗓', '干哑', '润喉', '声哑'],
    'sick_complain': ['浑身酸', '不舒服', '请假', '没胃口', '难受'],
    'wear_warm_remind': ['加衣', '保暖', '围巾', '羽绒服', '暖宝'],
    'dress_warm_remind': ['多穿', '披件', '别冻', '保暖', '围巾'],
    'morning_grouchy_mood': ['起床气', '皱眉', '哼唧', '心情差', '没精神'],
    'after_work_tired': ['下班路', '一身疲', '坐瘫', '回家', '解脱'],
    'comfort_request': ['抱抱', '需要安慰', '蹭蹭', '别走', '陪我'],
    'praise_me': ['夸我', '我棒不', '夸笨猫', '小红花', '点赞'],
    'want_praise': ['想被夸', '求表扬', '夸我嘛', '美一下', '哼哼'],
    'seek_compliment': ['夸我嘛', '我可爱不', '不夸难过', '表扬', '舔'],
    'redpacket_surprise': ['红包', '发福利', '转账', '小心意', '惊喜'],
    'praised_blush_happy': ['脸红', '不好意思', '害羞', '心跳', '嘴角翘'],
    'praised_happy': ['开心', '飞起', '炸毛', '尾巴抖', '尾巴翘'],
    'kuajiang_praise_beg': ['夸我', '求夸', '哼哼夸', '哼夸', '舔笨猫'],
    'kiss_beg_shy': ['亲亲', '害羞', '凑过来', '嘴对嘴', '蹭脸'],
    'kiss_pester': ['缠着亲', '蹭蹭嘴', '凑近', '小奶亲', '不让躲'],
    'qinqin_kiss_beg': ['亲亲', '抱亲', '小奶亲', '凑嘴', '蹭脸'],
    'kiss_request': ['亲一下', '凑过来', '抱亲', '蹭嘴', '小奶亲'],
    'kiss_beg': ['亲亲', '小奶亲', '蹭蹭', '蹭嘴', '凑近'],
    'morning_kiss_tease': ['早安吻', '出门前', '凑嘴', '蹭蹭', '回家亲'],
    'heat_wave_die': ['热浪', '蒸笼', '汗如雨', '中暑', '空调救'],
    'dress_warm_care': ['多穿点', '冻坏', '加衣', '围巾', '保暖'],
    'weather_chat': ['天气热', '阴天', '回南天', '潮湿', '梅雨'],
    'commute_tired': ['通勤累', '坐车', '挤地铁', '上下班', '路上'],
    'street_encounter': ['街上', '路上', '碰见', '撞见', '偶遇'],
    'too_small_room': ['小房间', '断舍离', '收纳', '挤', '租房'],
    'game_session': ['打游戏', '连胜', '上分', '车队', '开黑'],
    'game_vibes': ['手游', '排位', '上王者', '抽抽抽', '连胜'],
    'game_gacha_chat': ['抽卡欧', '保底了', '十连', '欧气', '非酋'],
    'gacha_game': ['抽卡池', '保底', '出货', '欧皇', '非酋'],
    'gacha_pull_dream': ['抽十连', '保底', '欧气', '梦中抽', '抽到'],
    'gacha_pull_luck': ['抽运气', '十连', '欧气', '保底', '抽到'],
    'sigh_complain': ['唉', '无奈', '叹气', '叹一声', '心累'],
    'busy_or_free': ['有空', '无所事事', '闲着', '空虚', '闲晃'],
    'anti_trope': ['反套路', '装死', '拒绝', '不演', '不接梗'],
    'phone_lag': ['卡顿', '掉帧', '反应慢', '内存满', '清缓存'],
    'flirt_back_blush_2': ['脸红', '害羞', '心跳', '别看', '逃跑'],
    'hot_drink_winter': ['热饮', '冬天', '暖手', '热茶', '热汤'],
    'phone_low_battery': ['低电量', '充电', '红条', '断电', '插充'],
    'random_burp_moment': ['打嗝', '嗝逆', '吃急', '气压', '喘不上'],
    'tease_master_weight': ['胖', '肚腩', '小肚子', '体重', '游泳圈'],
    'noon_lunch_break': ['午饭', '中午饭', '工作餐', '快餐', '便当'],
    'morning_wake_up': ['起床', '起不来', '困', '困死', '床贴床'],
    'morning_alarm_anger': ['闹钟', '闹钟响', '吵醒', '掐闹钟', '关闹'],
    'alarm_clock_hate': ['闹钟', '砸闹钟', '吵醒', '太烦', '关掉'],
    'morning_wakeup': ['睁眼', '起床', '困死', '闹钟响', '掀被'],
    'oversleep_panic': ['睡过头', '迟到', '完蛋', '慌死', '冲刺'],
    'goodnight_chat': ['晚安', '关灯', '入睡', '睡前话', '聊一会'],
    'workout_skip': ['不想动', '跳过', '懒得练', '休息日', '明天再'],
    'rest_well': ['好好歇', '充电', '睡眠', '回血', '补眠'],
    'busy_or_not': ['忙吗', '在忙', '加班', '收工', '回家了'],
    'stretch_morning_yawn': ['打哈欠', '伸懒腰', '没睡醒', '伸展', '困意'],
    'morning_wakeup_2': ['闹钟', '起床气', '哼哼', '掀被', '困死'],
    'late_night_owl': ['熬夜', '熬到', '晚睡', '不困', '宵夜'],
    'micro_mood': ['莫名', '小情绪', '突然', '心情', '飘忽'],
    'sudden_micro_feel': ['莫名', '突然', '心慌', '心动', '想笑'],
    'A2_philosophy_life': ['人生', '意义', '思考', '哲思', '想不通'],
    'random_happy_chat': ['莫名乐', '冒泡', '蹦跶', '心花', '小确幸'],
    'morning_grumpy': ['起床气', '脸冷', '懒得理', '哼唧', '心情差'],
    'hng_pout_tsundere': ['哼', '甩头', '小傲娇', '别理', '不屑'],
    'self_doubt': ['不够好', '没人喜欢', '自卑', '怀疑自己', '不讨喜'],
    'eat_well_care': ['好好吃', '别挑', '记得吃', '吃饱', '别饿'],
    'pouty_sulking': ['撅嘴', '小情绪', '生闷气', '不理', '哼'],
    'paw_clean_lick': ['舔爪', '清洁', '猫式洗', '舔毛', '脏爪'],
    'chicu_jealous': ['吃醋', '酸柠檬', '小心眼', '醋意', '占有'],
    'mental_help': ['焦虑', '心慌', '压力大', '崩溃', '撑不住'],
    'seek_comfort': ['抱抱', '安慰', '陪我', '蹭蹭', '哭一会'],
    'need_comfort': ['想抱抱', '心疼我', '陪一下', '别走', '哭'],
    'expression_xiaosi': ['笑死', '太搞笑', '哈哈哈', '绝绝子', '笑岔'],
    'laugh_die_meme': ['笑死', '梗图', '哈哈哈', '绝绝子', '笑趴'],
    'wuwu_cry_emoji_2': ['哭哭', '抽噎', '泪奔', '抹泪', '哭脸'],
    'want_comfort': ['想抱抱', '需要安慰', '陪我', '蹭蹭', '哭一会'],
    'anwei_comfort_seek': ['安慰我', '抱抱', '陪我', '别走', '蹭蹭'],
    'what_doing_now': ['在干嘛', '现在做啥', '忙啥呢', '在不在', '动作'],
    'work_slack': ['摸鱼', '工位', '不想动', '加班', '老板'],
    'what_doing_ask': ['在干嘛', '现在做啥', '忙啥', '动态', '状态'],
    'evening_dinner_chat': ['晚饭', '回家', '吃饭', '夜餐', '日常'],
    'work_slack_moyu': ['摸鱼', '工位', '划水', '加班', '老板'],
    'outfit_fail': ['穿搭', '衣服丑', '搭配', '撞色', '尴尬'],
    'morning_wakeup_chat': ['早安', '起床', '懒洋洋', '迷糊', '初醒'],
    'morning_wake_grumpy': ['起床气', '脸冷', '皱眉', '心情差', '哼'],
    'tease_no_shave': ['胡子', '邋遢', '油', '剃刀', '修边'],
    'tease_master_outfit': ['穿搭', '衣服丑', '邋遢', '撞色', '直男'],
    'tease_master': ['损主人', '调侃', '吐槽', '看不下', '邋遢'],
    'flirting_back': ['反撩', '突然甜', '撩回去', '害羞', '心跳'],
    'blush_dodge_anti_flirt': ['脸红', '躲开', '别看', '害羞', '心跳'],
    'compliment': ['夸夸', '赞美', '彩虹屁', '小可爱', '夸笨猫'],
    'reverse_flirt_blush': ['反撩', '害羞', '脸红', '心跳', '躲'],
    'flirt_back_blush_2': ['脸红', '害羞', '心跳', '别看', '逃跑'],
    'jealous_petty': ['醋坛', '占有', '独占', '不分享', '醋意'],
    'jealous_pouting': ['嘟嘴', '哼', '不理', '吃醋', '甩尾'],
    'jealous_vinegar_2': ['酸', '醋坛', '占有', '独占', '心眼'],
    'jealous_pout_2': ['吃醋', '生气', '小心眼', '占有', '醋'],
    'qmsl_cute_attack': ['好可爱', '萌爆', '激萌', '心动', '冲'],
    'awsl_cute_attack': ['啊我死了', '萌爆', '激萌', '心动', '萌脆'],
    'paw_clean_lick_2': ['舔爪', '清洁', '舔毛', '脏爪', '猫式洗'],
    'meow_daily_chatter': ['日常喵', '蹭', '咕噜', '撒娇', '撒尾'],
    'cat_daily': ['日常', '蹭蹭', '主子', '猫生活', '撒娇'],
    'catty_fish_dream': ['鱼梦', '梦到鱼', '吃鱼梦', '咸鱼梦', '咕噜'],
    'fish_dry_crave': ['鱼干', '想吃', '咸鱼', '酥脆', '馋'],
    'meal_check_in': ['吃过没', '记得吃', '别忘吃', '吃饱', '饿吗'],
    'fish_snack': ['鱼干', '小鱼', '咸香', '酥', '一条'],
    'takeout_chat': ['外卖', '点餐', '送达', '骑手', '凉了'],
    'ate_yet': ['吃过', '吃饱', '饿吗', '吃了吗', '撑'],
    'food': ['美食', '好吃', '想吃', '咽口水', '点'],
    'eat_well_check': ['吃饱不', '别饿', '正餐', '吃几口', '挑食'],
    'late_night_drink': ['宵夜饮', '凌晨喝', '热饮', '茶饮', '晚饮'],
    'snacks_craving': ['馋零食', '想吃', '嘴馋', '泡面', '零食袋'],
    'milk_tea_obsession': ['奶茶瘾', '续命', '不喝难受', '加珍珠', '半糖'],
    'tea_milk_chat': ['奶茶', '杯型', '芋圆', '波霸', '加料'],
    'bubble_tea_crave': ['珍珠', '波霸', '芋圆', '半糖', '热饮'],
    'milk_tea_addict': ['续命', '上瘾', '一天一杯', '加冰', '少甜'],
    'cup_noodle_late': ['泡面', '深夜泡', '不健康', '咸', '调料包'],
    'A_dessert_temptation': ['甜品', '蛋糕', '奶油', '甜到', '小蛋糕'],
    'dessert_temptation': ['甜品', '蛋糕', '奶油', '甜到', '小蛋糕'],
    'did_you_eat': ['吃了吗', '吃过没', '一起吃', '点啥', '别饿'],
    'wuwu_yingying': ['哼唧', '呜呜', '小哭包', '泪眼', '撒'],
    'exercise_lazy': ['健身', '运动', '懒得练', '跳过', '不动'],
    'after_work_tired_2': ['下班路', '回家', '坐瘫', '解脱', '一身疲'],
    'face_wash_cold': ['冷水洗脸', '提神', '冰脸', '通透', '紧绷'],
    'skincare_routine': ['护肤', '水乳', '面膜', '精华', '保养'],
    'night_goodnight': ['晚安', '关灯', '入梦', '关电', '盖被'],
    'bath_skincare_routine': ['洗澡', '泡浴', '热水澡', '澡盆', '搓背'],
    'sulky_pouting': ['撅嘴', '小情绪', '生闷气', '哼', '别理'],
    'grumpy_face_chat': ['臭脸', '皱眉', '黑脸', '不爽', '不开心'],
    'sulky_hmph_pout': ['哼', '甩头', '不理', '撅嘴', '生气'],
    'hmph_huff': ['哼', '甩头', '气哼', '别理', '不爽'],
    'jealous_pout_silent': ['闷气', '不说话', '吃醋', '占有', '抓尾'],
    'morning_grouchy_mood': ['起床气', '冷脸', '没精神', '哼', '心情差'],
    'A0_jealous_petty': ['醋意', '独占', '专属', '不分享', '占有'],
    'A2_philosophy': ['人生', '存在', '意义', '思考', '哲思'],
    'philosophy_life': ['人生', '意义', '思考', '哲思', '想不通'],
    'existential_crisis': ['存在主义', '人生意义', '迷茫', '空虚', '找不到自己'],
    'unclear_goal': ['没目标', '没方向', '迷茫', '不知道', '空心'],
    'take_rest_care': ['歇歇', '别累', '小憩', '充电', '放空'],
    'take_rest': ['休息', '坐下', '歇会', '回血', '补眠'],
    'rest_concern': ['担心累', '歇歇', '别熬', '小憩', '补觉'],
    'rest_well_care_2': ['好好歇', '别熬夜', '记得睡', '补觉', '别加班'],
    'are_you_there': ['在吗', '在不', '有人吗', '冒泡', '回应'],
    'camera_broken': ['相机', '镜头', '坏了', '修', '换'],
    'squat_numb_legs': ['蹲麻', '腿麻', '站不起', '抽筋', '抖'],
    'where_are_you_now': ['在哪', '位置', '哪里', '现在哪', '地点'],
    'what_time_check': ['几点', '时间', '现在几点', '钟', '时辰'],
    'sleepy_already': ['困了', '想睡', '眼皮重', '打瞌', '撑不住'],
    'morning_wakeup_3': ['睁眼', '困死', '掀被', '闹钟', '不想起'],
    'midnight_snack_guilt': ['内疚', '怕胖', '罪恶', '管不住', '半夜吃'],
    'late_night_chat_2': ['夜聊', '熬夜', '不睡', '聊到深', '夜话'],
    'late_night_owl_2': ['夜猫子', '熬到深', '不睡', '宵夜', '夜场'],
    'jealous_pout_3': ['吃醋', '抓尾', '不分享', '醋', '心眼'],
    'pivot_pain': ['转方向', '放不下', '沉没', '改方向', '挣扎'],
    'failed_side': ['副业失败', '沉没', '亏', '错路', '不甘'],
    'promotion_fail': ['升职失败', '没晋升', '不甘', '心累', '裁员'],
    'cold_silent_treat': ['冷战', '不说话', '不理', '偷瞄', '默默'],
    'cat_daily_life': ['日常猫', '蹭', '咕噜', '主子', '撒娇'],
    'neko_fish_snack': ['鱼干', '吃鱼', '咸鱼', '咬鱼', '一条鱼'],
    'pet_caress_request': ['摸摸', '抱抱', '撸', '蹭', '主子待遇'],
    'ask_for_headpat_2': ['头顶', '抓头', '撸头', '摸头', '蹭'],
    'want_headpat_beg': ['摸头', '撸头', '蹭头', '头顶', '抓头'],
    'motoutou_pat_head': ['摸头头', '抓头', '撸头', '蹭头', '顶头'],
    'head_pat': ['摸头', '撸头', '抓头', '揉头', '蹭头'],
}

INTENT_FLAVOR = {
    'complaint': ['呜', '累', '崩'],
    'playful': ['嘿嘿', '蹭蹭', '嘻嘻'],
    'question': ['推荐', '怎么', '哪个'],
    '哭': ['呜呜', '泪', '心疼'],
    '食欲不振': ['没胃口', '吃不下', '反胃'],
    '语法学不会': ['卡住', '记不住', '没懂'],
    '旧版熟悉': ['习惯了', '老版本', '改了'],
}

def slug_of(file_name):
    name = file_name.replace('.yaml', '')
    parts = name.split('_', 1)
    if len(parts) == 2 and re.match(r'^[A-Z]*\d+$', parts[0]):
        return parts[1]
    return name

def get_slug_vocab(slug):
    # try exact
    if slug in SLUG_VOCAB:
        return SLUG_VOCAB[slug][:]
    # try substring
    matches = []
    for k, v in SLUG_VOCAB.items():
        if k in slug or slug in k:
            matches.extend(v)
    if matches:
        # de-dup keep order
        seen = set()
        out = []
        for w in matches:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out
    return []

results = []
unmatched = []
for r in data:
    slug = slug_of(r['file'])
    vocab = get_slug_vocab(slug)
    intent_flavor = INTENT_FLAVOR.get(r.get('intent', ''), [])
    # candidate words
    cand = vocab[:] + intent_flavor
    # filter against keywords (and sibling first_utterances + sibling topic_hints loose check)
    block = set()
    for kw in r['keywords']:
        block.add(kw)
    for sib in r.get('siblings', []):
        block.add(sib.get('first_utterance', ''))
        for tok in sib.get('topic_hint', '').split('_'):
            block.add(tok)
    # also remove words that appear inside any keyword token (substring)
    def is_blocked(w):
        for b in block:
            if not b:
                continue
            if w == b or w in b or b in w:
                return True
        return False
    cleaned = []
    seen = set()
    for w in cand:
        if w in seen:
            continue
        if is_blocked(w):
            continue
        seen.add(w)
        cleaned.append(w)
    # need 4-6 words
    if len(cleaned) < 4:
        unmatched.append((r['name'], slug, len(cleaned)))
    # pick 5 (or 4..6)
    ctx = cleaned[:6] if len(cleaned) >= 6 else cleaned
    results.append({'name': r['name'], 'ctx': ctx, 'slug': slug, 'intent': r.get('intent', '')})

print(f'total: {len(results)}')
print(f'unmatched (<4 ctx after filtering): {len(unmatched)}')
print('--- first 20 unmatched ---')
for u in unmatched[:20]:
    print(u)

# also report which slugs were not in vocab
unknown_slugs = set()
for r in data:
    slug = slug_of(r['file'])
    if not get_slug_vocab(slug):
        unknown_slugs.add(slug)
print('--- unknown slugs ---', len(unknown_slugs))
for s in sorted(unknown_slugs)[:80]:
    print(s)

json.dump(results, open('e:/VC/Catty/data/_batch23_draft.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
