# -*- coding: utf-8 -*-
"""
v3: Comprehensive slug vocab. Filter strict (must not appear in keywords/sibling first_utt).
If still <4 after filter, pull from utterances + name tokens.
"""
import json, re

data = json.load(open('e:/VC/Catty/data/_batch23.json', 'r', encoding='utf-8'))

SLUG_VOCAB = {
    # --- head pat / chin / pet ---
    'head_pat_beg': ['撸下巴', '颌下', '舒服', '咕噜', '蹭手'],
    'ask_for_headpat': ['头顶', '抓头', '撸头', '蹭手', '主子待遇'],
    'cat_daily': ['日常喵', '蹭蹭', '主子', '咕噜', '小动作'],
    'cat_daily_life': ['日常猫', '蹭', '咕噜', '主子', '撒娇'],
    'meow_daily_chatter': ['日常喵', '蹭', '撒尾', '撒娇', '叫喵'],
    'pet_animal': ['毛孩子', '宠物', '猫狗', '小动物', '主子'],
    # --- noon / nap / sleep ---
    'noon_break': ['午觉', '午休', '中午困', '回神', '下午茶'],
    'lunch_break': ['午饭后', '午休', '便当', '午觉', '回工位'],
    # --- conflict ---
    'in_law_conflict': ['婆家', '婆媳', '老一辈', '难调和', '心酸'],
    # --- small life ---
    'small_things': ['日常', '琐事', '碎碎念', '小事', '吐槽'],
    'small_complaints': ['吐槽', '碎碎念', '日常烦', '小怨气', '碎事'],
    'small_happiness_share': ['小确幸', '冒泡', '小惊喜', '分享一下', '蹦跶'],
    'small_joy_moment': ['小确幸', '蹦跶', '冒泡', '心花', '甜一下'],
    'small_morning_grump': ['早上小脾气', '哼唧', '脸冷', '心情差', '皱眉'],
    # --- pre sleep / sleep struggle ---
    'pre_sleep_chat': ['关灯', '床上', '睡前', '数羊', '困不来'],
    'sleep_struggle': ['翻身', '辗转', '床硬', '难入', '挣扎'],
    'sleep_check_late': ['几点了', '熬深', '太晚', '半夜', '不睡'],
    'sleep_yet_check': ['睡了吗', '入睡', '关灯了', '床上吗', '困不'],
    'still_awake_check': ['还醒着', '没睡', '不困', '熬夜', '夜猫'],
    'insomnia_thinking': ['失眠', '想事', '脑子停不下', '睡不着', '辗转'],
    # --- tease owner ---
    'tease_owner': ['沙发瘫', '葛优', '懒虫', '化水', '熔椅'],
    'tease_master': ['损主人', '调侃', '吐槽', '看不下', '邋遢'],
    'tease_master_fat': ['鸡窝', '炸毛', '没梳', '蓬乱', '起床头'],
    'tease_fat': ['肚腩', '游泳圈', '体重', '小肚子', '胖一圈'],
    # --- pimple / face ---
    'pimple_panic': ['长痘', '冒痘', '油脸', '毛孔', '泛红'],
    # --- jealousy / pout ---
    'jealousy_pouting': ['嘟嘴', '甩尾', '哼唧', '小气', '不理'],
    'jealous_pout': ['醋坛', '吃醋', '占有', '独占', '不分享'],
    'jealous_vinegar': ['酸气', '抓尾', '小心眼', '醋意', '吃干醋'],
    'hng_pout_tsundere': ['哼', '甩头', '小傲娇', '别理', '不屑'],
    'sulky_pouting': ['撅嘴', '小情绪', '生闷气', '哼', '别理'],
    'sulky_hmph_pout': ['哼', '甩头', '不理', '撅嘴', '生气'],
    'grumpy_face_chat': ['臭脸', '皱眉', '黑脸', '不爽', '不开心'],
    # --- tired body ---
    'tired_fatigue': ['全身酸', '骨头痛', '瘫床', '动不了', '提不起劲'],
    'tired_overtime': ['加班累', '熬班', '熬夜班', '熬深', '回家瘫'],
    # --- jealous vinegar ---
    # --- phone ---
    'phone_battery_low': ['电量条', '充满', '红条', '强迫', '插电'],
    'phone_low_battery': ['低电', '充电', '红条', '断电', '插充'],
    'phone_battery_dying': ['临停机', '一格电', '关机', '低电量', '断电'],
    'phone_addiction': ['手机瘾', '刷手机', '放不下', '上瘾', '熬刷'],
    # --- bengbu meme ---
    'bengbu_meme': ['蚌埠住', '笑哭', '梗图', '泪奔', '崩不住'],
    'bengbuzhu_meme': ['蚌埠住', '崩不住', '泪奔', '笑哭', '梗图'],
    # --- office / work ---
    'office_slack': ['工位', '老板催', '会议', '划水', '打工'],
    'office_slacking_check': ['摸鱼', '划水', '工位', '不想动', '老板'],
    'work_slack': ['摸鱼', '工位', '划水', '加班', '老板'],
    'workout_skip': ['不想练', '跳过', '懒得动', '休息日', '明天再'],
    'work_life': ['打工', '上班族', '社畜', '工资', '老板'],
    # --- mirror ---
    'mirror_check_self': ['镜子', '照镜', '镜前', '看自己', '换发型'],
    'mirror_bedhead': ['起床头', '鸡窝', '炸毛', '蓬乱', '没梳'],
    'mirror_selfie': ['自拍', '镜前', '滤镜', '拍照', '美颜'],
    # --- flirt ---
    'flirt_back': ['表白', '嘴甜', '冲冲冲', '心动', '突袭'],
    'flirting_back': ['反撩', '突然甜', '撩回去', '害羞', '心跳'],
    # --- emotion seek praise ---
    'emotion_seek_praise': ['夸夸', '夸我', '抱抱奖', '加油棒', '小红花'],
    # --- internet meme ---
    'internet_meme': ['梗图', '佛了', '不卷', '咸鱼', '摆烂图'],
    'meme_juejuezi': ['绝绝子', '梗', '冲冲冲', '蚌埠住', '笑死'],
    'wow_awsl_react': ['啊我死了', '萌爆', '激萌', '心动', '萌脆'],
    # --- random ---
    'random_grievance': ['谢谢猫', '感激', '靠你', '抱抱猫', '有你在'],
    'random_happy_chat': ['莫名乐', '冒泡', '蹦跶', '心花', '小确幸'],
    'random_chat': ['闲聊', '随口', '冒泡', '唠嗑', '日常话'],
    'random_compliment_chat': ['彩虹屁', '夸笨猫', '夸你', '甜话', '嘴甜'],
    'random_fart_burp': ['打嗝', '放屁', '生理', '小尴尬', '失礼'],
    # --- bump lost ---
    'bump_lost_chat': ['迷糊', '反应慢', '蒙圈', '抓瞎', '撞东西'],
    # --- game ---
    'game_mood': ['十连', '保底', '欧气', '非酋', '出货'],
    # --- traffic ---
    'traffic_jam_late': ['坐车', '后排', '颠簸', '呕感', '风口'],
    'commute_traffic_tired': ['堵车', '通勤', '车厢挤', '上下班', '路上'],
    # --- tea / drink ---
    'afternoon_tea_time': ['续命', '波霸', '芋圆', '半糖', '热饮'],
    'milk_tea_addict': ['续命', '上瘾', '一天一杯', '加冰', '少甜'],
    'milk_tea_obsession': ['奶茶瘾', '续命', '不喝难受', '加珍珠', '半糖'],
    'tea_time_sip': ['泡茶', '热茶', '清香', '一口茶', '茶香'],
    'hot_drink_winter': ['冬天', '暖手', '热饮', '热茶', '驱寒'],
    'hot_water_remind': ['多喝水', '热水', '保暖', '加湿', '喝点暖的'],
    'drink_water_care': ['喝水', '保湿', '别脱水', '续杯', '小水壶'],
    'drink_water_check': ['喝过水', '杯子', '水壶', '保温杯', '别脱水'],
    'drink_water_remind': ['记得喝水', '续杯', '水壶', '小提醒', '杯子'],
    # --- micro emotion ---
    'micro_emotion': ['莫名', '心花', '哼小调', '蹦跶', '冒泡'],
    # --- stomach ---
    'stomach_growl_chat': ['打嗝', '撑肚', '消食', '解腰带', '肚响'],
    'stomach_growl': ['肚饿响', '咕咕叫', '饿了', '肚子叫', '空腹'],
    # --- hungry ---
    'hungry_snack_chat': ['分一口', '尝尝', '喂喂', '递过来', '嘴馋'],
    # --- cold / sick ---
    'cold_caught_sick': ['吞咽痛', '咽炎', '红肿', '消炎', '冰水'],
    'runny_nose_cold': ['流鼻涕', '擤鼻', '鼻塞', '纸巾', '感冒'],
    'sneeze_runny_nose': ['姜汤', '葱白', '驱寒', '保暖汤', '红糖水'],
    # --- password ---
    'password_forgot': ['登录', '验证码', '找回', '邮箱', '安全题'],
    # --- coffee ---
    'morning_coffee_first': ['美式', '拿铁', '现磨', '提神', '续命杯'],
    'coffee_shop_chill': ['咖啡馆', '坐一下午', '闲坐', '咖啡香', '惬意'],
    # --- comfort ---
    'need_comfort_hug': ['空虚', '没目标', '迷路', '没方向', '找不到'],
    'need_comfort': ['想抱抱', '心疼我', '陪一下', '别走', '安慰'],
    'seek_comfort': ['抱抱', '安慰', '陪我', '蹭蹭', '哭一会'],
    'anwei_comfort_seek': ['安慰我', '抱抱', '陪我', '别走', '蹭蹭'],
    # --- midnight snack ---
    'midnight_snack_crave': ['深夜', '罪恶感', '宵夜', '送餐慢', '凉了'],
    'lazy_day_off': ['宅家', '休息日', '懒散', '什么都不做', '葛优'],
    'lazy_weekend_morning': ['周末晨', '赖床', '懒洋洋', '不起', '宅家'],
    # --- house ---
    'house_chores': ['囤货', '货架', '主子', '采购', '家务'],
    # --- owner ---
    'owner_no_shave': ['胡渣', '邋遢', '剃须刀', '油腻', '修边'],
    # --- kiss ---
    'want_kiss_beg': ['啵啵', '隔空', '飞过来', '心心', '隔屏'],
    'kiss_tease_blush': ['脸红', '害羞', '凑嘴', '别躲', '心跳'],
    # --- commute ---
    'bus_commute_tired': ['挤地铁', '汗渍', '通勤', '换衣', '臭味'],
    # --- emoji ---
    'emoji_war_battle': ['斗图', '战绩', '王者', '碾压', '吊打'],
    'emoji_reactions': ['表情包', '冒泡表情', '回应表情', '斗图', '小图'],
    'emoji_text': ['表情字', '文字图', '符号', '颜文字', '颜表情'],
    # --- eat ---
    'ate_yet_question': ['撑死', '腰带松', '吃过头', '走不动', '消化'],
    'ate_yet_meal': ['吃没吃', '正餐', '吃了没', '点啥', '一起吃'],
    'eat_check_in': ['吃过没', '记得吃', '正餐', '吃饱', '别饿'],
    'eat_well_care': ['好好吃', '别挑', '记得吃', '吃饱', '别饿'],
    'eat_well_check': ['吃饱不', '别饿', '正餐', '吃几口', '挑食'],
    # --- fish ---
    'want_fish_jerky': ['鱼条', '小鱼干', '酥脆', '咸鱼', '鱼香'],
    'fish_snack': ['鱼干', '小鱼', '咸香', '酥', '一条'],
    'dried_fish_crave': ['鱼干', '想吃', '咸鱼', '酥脆', '馋'],
    # --- pet sick / lost ---
    'pet_sick': ['毛孩子', '宠物', '心疼', '看医生', '宠物医院'],
    'pet_lost': ['走丢', '找猫', '寻宠', '丢了宠', '心慌'],
    # --- rest ---
    'rest_well_care': ['歇歇', '小憩', '充能', '回神', '缓口气'],
    'rest_well': ['好好歇', '回血', '补眠', '小憩', '别熬'],
    'take_rest_care': ['歇歇', '别累', '小憩', '充电', '放空'],
    'take_rest_remind': ['歇会', '提醒歇', '别熬', '该睡', '坐下'],
    # --- bored ---
    'bored_idle_chat': ['推荐剧', '片单', '什么好看', '电视剧', '哪部'],
    # --- late night ---
    'late_night_chat': ['静谧', '夜风', '万籁', '独处', '夜话'],
    'late_night_call': ['深夜聊', '夜话', '电话', '熬到聊', '夜聊'],
    # --- night sleep ---
    'night_sleep_chat': ['凌晨', '半夜', '深夜', '熬过头', '太迟'],
    # --- social ---
    'social_injustice': ['不公', '社会', '想发声', '改革', '挺身'],
    # --- indoor ---
    'indoor_lazy_day': ['宅家', '电影日', '马拉松', '一刷再刷', '陪看'],
    # --- morning ---
    'morning_first_word': ['赖床', '不起', '被窝', '掀被', '再睡'],
    'morning_grouchy_mood': ['起床气', '冷脸', '没精神', '哼', '心情差'],
    'morning_wakeup': ['睁眼', '困死', '掀被', '闹钟', '不想起'],
    'morning_zaoan_chat': ['早安', '问早', '早起', '迷糊', '初醒'],
    'zaoan_morning_greeting': ['早安', '问早', '招呼', '迷糊', '初醒'],
    'just_wake_groggy': ['迷糊', '没清醒', '脸肿', '哈欠', '懵'],
    # --- weather ---
    'weather_chat': ['天气', '气温', '阴天', '回南天', '梅雨'],
    'window_weather_chat': ['看窗外', '天气', '阴天', '风', '雨'],
    'window_view_outside': ['看窗外', '风景', '楼下', '街景', '阳光'],
    'rainy_day_mood': ['雨天', '阴雨', '潮湿', '雨声', '湿冷'],
    'hot_summer_chat': ['夏天', '热浪', '空调', '出汗', '湿热'],
    'hot_summer_complain': ['热死', '中暑', '汗', '湿黏', '蒸笼'],
    'wind_too_strong': ['大风', '风口', '吹乱', '冷风', '裹紧'],
    'thunder_scared': ['打雷', '雷声', '害怕', '躲被', '电闪'],
    # --- aging / nostalgic ---
    'aging_anxiety': ['老了', '皱纹', '掉发', '体力差', '衰老'],
    'nostalgic': ['怀旧', '想起', '过去', '老歌', '童年'],
    # --- anti routine ---
    'anti_routine': ['反套路', '不按', '突袭', '不接梗', '出其不意'],
    'anti_trope': ['反套路', '装死', '拒绝', '不演', '不接梗'],
    # --- ask hug / hug ---
    'ask_for_hug': ['抱抱我', '凑过来', '钻怀', '蹭怀', '黏'],
    'hug_request': ['抱我', '蹭怀', '凑过去', '钻被', '黏'],
    'hug_beg_lap': ['钻腿上', '蜷腿上', '坐腿', '蹭腿', '腿窝'],
    # --- blanket ---
    'blanket_burrito_cozy': ['裹被子', '被窝', '蚕茧', '暖呼呼', '蜷'],
    # --- brain freeze ---
    'brain_freeze': ['脑结冰', '冰激凌', '头一凉', '吸冰', '突冷'],
    # --- class ---
    'class_skip_daze': ['翘课', '走神', '发呆', '溜号', '不听'],
    # --- clock ---
    'clock_check': ['看钟', '几点', '时间', '钟', '查时'],
    # --- collection ---
    'collection_obsession': ['收藏癖', '集齐', '入坑', '剁手', '凑齐'],
    # --- drama ---
    'drama_addiction': ['追剧瘾', '上头', '熬夜追', '一集接一', '剧情上头'],
    # --- first date ---
    'first_date_anxiety': ['首次约会', '紧张', '心跳', '手心汗', '约会'],
    # --- friendship ---
    'friendship_chat': ['好友', '老友', '聚一聚', '友情', '约出来'],
    # --- grammar ---
    'grammar_pain': ['语法', '时态', '从句', '英语', '学不会'],
    # --- hair dry ---
    'hairdry_towel_chat': ['吹头发', '毛巾', '吹风机', '湿发', '擦头'],
    # --- heihei ---
    'heihei_yingying': ['嘿嘿', '哼唧', '撒娇', '黏', '蹭'],
    # --- hot pot ---
    'hotpot_dream': ['火锅', '想吃锅', '麻辣', '羊肉', '锅底'],
    # --- ice cream ---
    'ice_cream_craving': ['冰激凌', '雪糕', '冷饮', '巧克力球', '想吃冰'],
    # --- kiss ---
    # --- laugh ---
    'laugh_cry_xs': ['笑死', '眼泪都笑', '太搞笑', '哈哈哈', '笑岔'],
    'laugh_dead': ['笑死', '太搞笑', '岔气', '哈哈哈', '蚌埠住'],
    'expression_xiaosi': ['笑死', '太搞笑', '哈哈哈', '绝绝子', '笑岔'],
    # --- lovesickness ---
    'lovesickness': ['想你', '相思', '夜思', '念你', '挂念'],
    # --- lucky ---
    'lucky_streak_win': ['连胜', '运气好', '欧气', '中奖', '彩虹'],
    'winning_streak_brag': ['连胜', '吊打', '王者', '战绩', '炫耀'],
    # --- missed message ---
    'missed_message': ['没回', '没看见', '消息漏', '半天没回', '回我'],
    # --- moon ---
    'moon_pretty': ['月亮', '月光', '夜空', '赏月', '月色'],
    'moon_round': ['圆月', '中秋', '月圆', '满月', '月光'],
    # --- music ---
    'music_humming': ['哼歌', '小调', '哼起', '哼曲', '哼着走'],
    'music_mood': ['听歌', '播放', '歌单', '耳机', '心情曲'],
    # --- neck ---
    'neck_pain': ['脖子', '落枕', '酸痛', '颈椎', '揉肩'],
    # --- no money ---
    'no_money': ['月光', '没钱', '余额', '吃土', '剁手'],
    # --- nothing wear ---
    'nothing_wear': ['没衣服', '衣柜空', '搭不出', '穿啥', '换装'],
    # --- old phone ---
    'old_phone': ['旧手机', '老机', '换新机', '卡顿', '怀旧'],
    # --- outfit envy ---
    'outfit_envy': ['羡慕穿搭', '同款', '种草', '想穿', '别人衣'],
    # --- package ---
    'package_arrived_excite': ['快递到', '拆包', '剁手', '开箱', '惊喜'],
    # --- passed down ---
    'passed_down': ['祖传', '老物', '传下来', '老古董', '怀旧'],
    # --- praised today ---
    'praised_today': ['被夸', '今天夸', '夸我', '心花', '甜'],
    # --- red packet ---
    'red_packet_joy': ['红包', '抢红包', '微信', '转账', '惊喜'],
    'surprise_redpack_joy': ['红包惊喜', '抢到', '中大', '炫耀', '美滋滋'],
    # --- reverse tease ---
    'reverse_tease': ['反撩', '损回去', '怼回去', '突袭', '反吐'],
    # --- slippers ---
    'slippers_warm_step': ['拖鞋', '暖足', '毛绒', '踩地', '冷地'],
    # --- street ---
    'street_encounter': ['街上', '路上', '碰见', '撞见', '偶遇'],
    # --- stretching ---
    'stretching_lazy': ['伸懒腰', '伸展', '没睡醒', '懒洋洋', '伸手'],
    # --- study ---
    'study_scene': ['学习', '复习', '书桌', '刷题', '专注'],
    'study_struggle': ['学不进', '走神', '看不下', '挣扎', '困倦'],
    # --- typing ---
    'typing_dot_dot': ['正在输入', '点点点', '打字中', '正打', '等回'],
    # --- want company ---
    'want_company': ['陪我', '想陪', '别走', '一起', '蹭'],
    # --- weekend / wake ---
    'sleepy_already': ['困', '想睡', '眼皮重', '打瞌', '撑不住'],
    # --- wuwu cry ---
    'wuwu_cry_emoji': ['哭脸', '抽噎', '泪奔', '抹泪', '哭哭'],
    # --- zipper ---
    'zipper_stuck': ['拉链', '卡住', '坏了', '修', '换'],
    # --- busy or not ---
    'busy_or_not': ['闲着', '收工', '下班了', '在忙吗', '冒泡', '回我'],
    # --- dress warm care ---
    'dress_warm_care': ['御寒', '加件', '多穿', '保暖', '披一件', '冻僵'],
    # --- takeout slow ---
    'takeout_slow': ['配送慢', '送达晚', '凉了', '催餐', '骑手电话', '点餐'],
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

# extra utterance-extracted vocab fallback: if name contains certain tokens, add tag words
NAME_HINT_VOCAB = {
    'huzi': ['胡渣', '胡子拉碴'],
    'oversleep': ['睡过头', '迟到'],
    'overtime': ['加班', '熬夜班'],
    'jianhua': ['尖叫'],
    'choka': ['抽卡', '十连'],
    'gacha': ['抽卡', '保底'],
    'milk': ['奶茶', '波霸'],
    'tea': ['奶茶', '茶饮'],
    'fish': ['鱼干', '小鱼'],
    'cry': ['呜呜', '哭哭'],
    'kiss': ['亲亲', '啵'],
    'kuajiang': ['夸笨猫', '彩虹屁'],
    'praise': ['夸', '彩虹屁'],
    'lazy': ['懒洋洋', '葛优'],
    'lay': ['躺尸', '熔椅'],
    'pose': ['姿势'],
    'hair': ['鸡窝', '蓬乱'],
    'mess': ['乱蓬', '凌乱'],
    'jealous': ['吃醋', '醋坛'],
    'vinegar': ['醋', '酸'],
    'cold': ['冻', '寒'],
    'sick': ['不舒服', '难受'],
    'tired': ['累', '瘫'],
    'rest': ['歇', '补眠'],
    'comfort': ['抱抱', '蹭蹭'],
    'study': ['学习', '复习'],
    'game': ['连胜', '上分'],
    'flirt': ['撩', '甜'],
    'work': ['工位', '打工'],
    'drink': ['饮品', '杯'],
    'phone': ['手机', '屏幕'],
    'meme': ['梗', '梗图'],
    'wuwu': ['呜呜', '泪'],
    'shengqi': ['生气', '黑脸'],
    'pout': ['撅嘴', '嘟嘴'],
    'sigh': ['叹气', '唉'],
    'happy': ['开心', '蹦跶'],
    'morning': ['清晨', '初醒'],
    'late': ['深夜', '熬'],
    'sleep': ['入睡', '床上'],
    'eat': ['咽', '吃'],
    'pet': ['宠物', '毛孩'],
    'street': ['街上', '路边'],
    'outfit': ['穿搭', '搭配'],
    'rain': ['雨天', '潮湿'],
    'hot': ['热浪', '出汗'],
    'wind': ['大风', '冷风'],
    'thunder': ['打雷', '雷声'],
    'moon': ['月亮', '夜空'],
    'music': ['听歌', '哼调'],
    'snack': ['零食', '小吃'],
    'noodle': ['泡面', '面条'],
    'fish_dry': ['鱼干', '咸香'],
    'baby': ['崽崽', '小宝'],
    'tease': ['损', '调侃'],
}

def slug_of(file_name):
    name = file_name.replace('.yaml', '')
    parts = name.split('_', 1)
    if len(parts) == 2 and re.match(r'^[A-Z]*\d+$', parts[0]):
        return parts[1]
    return name

def get_slug_vocab(slug):
    if slug in SLUG_VOCAB:
        return SLUG_VOCAB[slug][:]
    matches = []
    for k, v in SLUG_VOCAB.items():
        if k in slug or slug in k:
            matches.extend(v)
    seen = set(); out = []
    for w in matches:
        if w not in seen:
            seen.add(w); out.append(w)
    return out

def name_hints(route_name):
    out = []
    rn = route_name.lower()
    for k, v in NAME_HINT_VOCAB.items():
        if k in rn:
            out.extend(v)
    return out

def build_block(r):
    block = set()
    for kw in r['keywords']:
        block.add(kw.strip())
    for sib in r.get('siblings', []):
        fu = sib.get('first_utterance', '')
        block.add(fu)
        # also block sibling topic_hint tokens
    # don't filter topic-hint tokens too aggressively; keep relaxed
    return block

def is_blocked(w, block):
    for b in block:
        if not b: continue
        if w == b: return True
        if w in b and len(w) >= 2: return True
        if b in w and len(b) >= 2: return True
    return False

results = []
for r in data:
    slug = slug_of(r['file'])
    cand = get_slug_vocab(slug)
    cand.extend(name_hints(r['name']))
    cand.extend(INTENT_FLAVOR.get(r.get('intent', ''), []))
    block = build_block(r)
    cleaned = []
    seen = set()
    for w in cand:
        if w in seen: continue
        if is_blocked(w, block): continue
        seen.add(w)
        cleaned.append(w)
    ctx = cleaned[:6] if len(cleaned) >= 4 else cleaned
    results.append({'name': r['name'], 'ctx': ctx, 'slug': slug, 'intent': r.get('intent', ''), 'orig_kw': r['keywords'], 'utts': r['utterances']})

# check short
short = [r for r in results if len(r['ctx']) < 4]
print(f'short={len(short)}/{len(results)}')
for r in short[:30]:
    print(r['name'], '|', r['slug'], '|', r['intent'], '|', r['ctx'], '|', r['orig_kw'])

json.dump(results, open('e:/VC/Catty/data/_batch23_draft.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('written draft')
