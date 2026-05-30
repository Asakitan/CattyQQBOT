# -*- coding: utf-8 -*-
"""Generate disambiguate_context for batch 9 of round 2 tasks."""
import json
import re

BATCH_INDEX = 9
SRC = 'e:/VC/Catty/data/disambig_round2_tasks.json'
OUT = 'e:/VC/Catty/data/disambig_round2_batch9_out.json'

# Theme -> ctx pool. ctx must be 2-4 chars, theme-specific.
# Avoid words that appear in keywords/utterances of the route.
THEME_POOL = {
    '002_late_night_owl': ['熬夜', '夜猫', '不睡', '凌晨', '失眠', '夜深'],
    '007_sleepy_already': ['困意', '眼皮', '打瞌睡', '哈欠', '迷糊', '想睡'],
    '011_jealous_vinegar': ['吃醋', '醋坛', '酸了', '嫉妒', '吃飞醋', '小醋'],
    '017_take_rest_remind': ['休息', '歇会', '躺平', '别累', '放松', '休一下'],
    '018_eat_properly': ['好好吃', '吃饭', '别饿', '三餐', '按时吃', '吃点'],
    '023_reverse_flirt': ['反撩', '撩回', '害羞', '脸红', '心动', '小鹿'],
    '036_moon_pretty': ['月亮', '月色', '夜空', '皎洁', '赏月', '月光'],
    '041_catch_a_cold_sick': ['感冒', '发烧', '生病', '难受', '咳嗽', '吃药'],
    '043_procrastination_putoff': ['拖延', '不想干', '摸鱼', '推后', '懒得', '拖着'],
    '045_window_view_outside': ['窗外', '风景', '看窗', '街景', '路人', '窗台'],
    '049_care_dress_warm': ['穿暖', '加衣', '保暖', '别冻', '羽绒', '围巾'],
    '051_care_rest_health': ['注意身体', '别累坏', '健康', '保重', '养身', '歇歇'],
    '056_head_pat_request': ['摸头', '头顶', '拍拍', '摸摸', '揉脑袋', '碰头'],
    '068_festival_birthday': ['生日', '蛋糕', '蜡烛', '寿星', '庆生', '许愿'],
    '074_morning_wake_up': ['起床', '早安', '清晨', '叫醒', '醒了', '懒床'],
    '078_afternoon_tea_time': ['下午茶', '点心', '茶歇', '甜点', '小蛋糕', '红茶'],
    '082_did_you_eat': ['吃饭没', '饿不', '用餐', '开饭', '吃啥', '伙食'],
    '084_drink_water_check': ['喝水', '补水', '水杯', '别脱水', '多喝水', '杯子'],
    '101_seek_company': ['陪我', '别走', '陪伴', '孤单', '陪聊', '寂寞'],
    '108_shoveler_return': ['铲屎官', '回家', '主子', '猫主', '主人回', '到家'],
    '10_status': ['近况', '状态', '怎样', '咋样', '过得', '最近'],
    '111_bus_commute_tired': ['通勤', '挤公交', '上班路', '高峰期', '挤地铁', '挤爆'],
    '121_night_goodnight': ['晚安', '安睡', '好梦', '睡了', '休息了', '入梦'],
    '126_jealous_pout': ['吃醋', '撅嘴', '生闷气', '小气', '哼哼', '醋意'],
    '130_flirt_back': ['撩回去', '反撩', '挑逗', '调情', '回撩', '撩拨'],
    '133_sudden_surprise': ['惊喜', '突然', '吓一跳', '没想到', '突如', '冷不防'],
    '135_cat_daily': ['猫日常', '猫生', '舔毛', '打盹', '抓挠', '尾巴'],
    '153_morning_wakeup': ['早起', '起床气', '懒被窝', '醒来', '清早', '早安'],
    '156_water_reminder': ['提醒喝水', '水杯', '补水', '保湿', '多喝', '咕咚'],
    '161_meal_check_in': ['吃了吗', '用餐了', '伙食', '开饭了', '饭点', '饱了'],
    '162_tease_master': ['调侃', '逗主人', '挑逗', '小坏', '皮一下', '欠揍'],
    '163_flirting_back': ['反撩', '撩回', '害羞', '脸红', '心慌', '小心思'],
    '164_mood_swings': ['情绪起伏', '心情', '波动', '阴晴', '忽喜', '忽悲'],
    '173_rest_concern': ['关心休息', '别累', '歇歇', '注意身体', '放松点', '健康'],
    '175_festival_moment': ['节日', '过节', '团圆', '气氛', '热闹', '佳节'],
    '180_weekend_plan': ['周末', '休假', '出门玩', '放假', '安排', '计划'],
    '186_early_morning': ['清早', '黎明', '天刚亮', '早班', '晨光', '一大早'],
    '187_late_night': ['深夜', '半夜', '凌晨', '夜里', '夜半', '夜深'],
    '195_small_things': ['小事', '日常', '琐事', '细节', '小确幸', '点滴'],
    '196_sudden_surprise': ['惊喜', '突然', '冷不防', '吓到', '猝不及防', '意外'],
    '197_night_chat': ['夜聊', '聊夜话', '夜话', '夜里聊', '深夜谈', '夜谈'],
    '201_gacha_game': ['抽卡', '出货', '欧皇', '非酋', '十连', '保底'],
    '203_work_slack': ['摸鱼', '划水', '偷懒', '上班摸', '工位', '老板'],
    '205_micro_mood': ['小心情', '微妙', '心思', '小情绪', '细腻', '心动'],
    '206_weather_chat': ['天气', '气温', '阳光', '阴天', '气候', '天气预报'],
    '213_friendship_chat': ['朋友', '友情', '闺蜜', '兄弟', '友谊', '陪伴'],
    '217_self_doubt': ['自我怀疑', '不行', '没用', '自卑', '焦虑', '我不配'],
    '221_noon_break': ['午休', '午睡', '午间', '中午', '小憩', '午饭后'],
    '228_insomnia_chat': ['失眠', '睡不着', '辗转', '翻来覆去', '数羊', '清醒'],
    '231_festival_moments': ['佳节', '过节', '团圆夜', '节日', '热闹', '气氛'],
    '236_work_slack': ['摸鱼', '划水', '上班摸', '老板没看', '偷闲', '工作日'],
    '243_anti_trope': ['反套路', '不按套路', '出其不意', '反向', '套路', '反转'],
    '248_off_work_tired': ['下班', '加班累', '收工', '回家路', '疲惫', '打卡下'],
    '252_morning_wakeup': ['早起', '起床', '醒来', '叫早', '早安', '清晨'],
    '257_ate_yet': ['吃了吗', '用饭', '饿了没', '伙食', '吃啥了', '开饭'],
    '272_laugh_die': ['笑死', '太搞笑', '哈哈', '笑喷', '绝了', '笑岔'],
    '275_praised_today': ['被夸', '表扬', '夸奖', '点赞', '认可', '小确幸'],
    '290_what_doing_now': ['在干嘛', '忙啥', '搞什么', '做什么', '正在', '现在干'],
    '295_chicu_jealous': ['吃醋', '吃飞醋', '小心眼', '醋意', '酸了', '吃干醋'],
    '300_dress_warm_care': ['穿暖', '保暖', '加衣', '别冻', '羽绒', '冷风'],
    '313_bengbu_meme': ['蚌埠住', '绷不住', '梗图', '住了', '绷住', '蚌住'],
    '315_morning_alarm': ['闹钟', '响了', '再睡五分', '关掉', '吵醒', '滴滴'],
    '351_morning_first_word': ['早安', '第一句', '清晨', '起床第一', '问早', '早呀'],
    '355_still_awake_check': ['还醒着', '没睡', '醒着吗', '不睡吗', '夜猫', '熬'],
    '358_random_emoji_spam': ['表情包', '斗图', '刷屏', '颜文字', '滚动', '一堆'],
    '360_sulky_hmph_pout': ['生闷气', '哼哼', '撅嘴', '不理你', '撒娇气', '小脾气'],
    '367_water_drink_remind': ['喝水', '补水', '多喝', '水杯', '咕咚', '别忘喝'],
    '382_sudden_wanna_cry': ['想哭', '鼻酸', '崩了', '泪奔', '委屈', '眼眶'],
    '384_morning_call': ['叫早', '早安call', '清晨呼', '起床喊', '催起床', '早唤'],
    '403_awsl_burst': ['啊我死了', '可爱炸', '爆萌', '萌死', '心动', '冲击'],
    '407_dream_share': ['做梦', '梦到', '昨晚梦', '梦境', '说梦', '梦里'],
    '408_moon_round': ['圆月', '月圆', '满月', '月亮圆', '团圆', '中秋'],
    '40_food': ['吃的', '美食', '菜', '饿了', '食物', '想吃'],
    '411_shovel_back': ['铲屎官回', '主人到家', '猫主回', '回来了', '到家', '主子归'],
    '413_pretend_blind': ['装看不见', '装瞎', '假装没看', '故意忽视', '视而不见', '不理'],
    '415_sudden_cry': ['突然哭', '泪崩', '哭出来', '鼻头酸', '眼泪掉', '委屈哭'],
    '417_early_morning_greet': ['早问候', '清早打招呼', '早呀', '起得早', '晨问', '早安'],
    '430_dress_warm_remind': ['提醒穿暖', '别冻着', '加件衣', '记得保暖', '保暖提醒', '披一件'],
    '442_commute_tired': ['通勤累', '上班路累', '挤累了', '路上累', '高峰', '挤一身'],
    '457_ate_yet_care': ['关心吃饭', '吃饭没', '别饿肚', '伙食', '饱不饱', '记得吃'],
    '462_want_comfort': ['想被安慰', '抱抱', '求安慰', '心累求抱', '安慰一下', '抚摸'],
    '473_awsl_meme_chat': ['awsl', '啊我死了', '萌爆', '可爱炸', '冲击', '萌死'],
    '476_want_headpat_beg': ['求摸头', '摸头', '揉脑袋', '拍拍', '想被摸', '撒娇求'],
    '481_lol_dying_laugh': ['笑死', '哈哈哈', '笑喷', '笑岔', '绝绝子', '笑爆'],
    '483_morning_wakeup': ['早起', '醒来', '起床', '清晨', '早安', '叫早'],
    '484_late_night_chat': ['深夜聊', '夜话', '夜聊', '半夜聊', '凌晨谈', '夜里说'],
    '494_redpacket_surprise': ['红包', '抢到', '手气王', '发红包', '运气', '惊喜红包'],
    '495_cat_daily_life': ['猫日常', '舔毛', '打滚', '挠柱子', '猫生', '尾巴摇'],
    '497_cold_tsundere': ['冷淡', '高冷', '傲娇', '装冷', '冰山', '不理人'],
    '504_snacks_craving': ['零食', '嘴馋', '想吃零食', '小吃', '解馋', '零嘴'],
    '506_comfort_request': ['求安慰', '抱抱我', '哄哄', '心累', '需要安慰', '抚慰'],
    '509_phone_screen': ['手机屏', '盯着手机', '屏幕', '刷手机', '看屏幕', '低头族'],
    '50_complaint': ['抱怨', '吐槽', '不爽', '糟心', '烦死', '不满'],
    '515_goodnight_routine': ['晚安例行', '睡前流程', '入睡仪式', '睡觉了', '关灯', '睡前'],
    '521_sleep_yet_check': ['睡了吗', '醒着吗', '还没睡', '入睡', '睡没', '熬夜呢'],
    '528_take_rest_care': ['歇歇', '休息一下', '别累着', '放松', '关心累', '歇会儿'],
    '533_flirt_back_blush': ['脸红反撩', '害羞回撩', '脸通红', '脸热', '心跳', '心动反'],
    '534_head_pat_beg': ['求摸头', '想被摸', '蹭手', '蹭过去', '撒娇摸', '揉头'],
    '536_tease_master_fat': ['嘲胖', '调侃肉', '小肚腩', '肉肉', '吃太多', '微胖'],
    '537_laugh_die_meme': ['笑死梗', '笑死我', '梗图笑', '哈哈梗', '笑岔', '太逗'],
    '543_dried_fish_crave': ['小鱼干', '咸鱼', '鱼干', '猫粮', '解馋', '想吃鱼'],
    '546_gacha_pull_luck': ['抽卡运', '欧皇', '非酋', '十连', '出货', '保底'],
    '550_five_more_minutes': ['再睡五分', '再眯会', '不想起', '懒床', '赖床', '关闹钟'],
    '551_alarm_snooze_chat': ['闹钟响', '贪睡', '关掉闹钟', '再响一次', '懒起', '叫醒'],
    '554_warm_bath_chat': ['泡澡', '热水澡', '浴缸', '泡泡浴', '洗澡', '热水'],
    '558_eye_strain_chat': ['眼睛累', '酸涩', '看屏久', '揉眼睛', '视疲劳', '眼涩'],
    '560_hiccup_chat': ['打嗝', '嗝嗝', '止不住', '喝水止', '隔气', '一直打'],
    '561_fridge_raid_late': ['夜翻冰箱', '半夜冰箱', '冰箱里', '翻吃的', '冷藏', '冰箱'],
    '562_midnight_snack_crave': ['宵夜', '半夜想吃', '深夜馋', '夜宵', '嘴馋夜', '夜餐'],
    '563_ice_cream_craving': ['冰淇淋', '雪糕', '甜筒', '冰激凌', '冰棒', '冷饮'],
    '568_popcorn_movie_night': ['爆米花', '电影夜', '看电影', '影院', '观影', '吃米花'],
    '569_rewatch_old_show': ['老剧', '二刷', '重温', '经典剧', '回看', '怀旧剧'],
    '578_windy_day_chat': ['刮风', '大风', '风很大', '风吹', '阵风', '吹乱头发'],
    '579_catnip_high_chat': ['猫薄荷', '嗨翻', '迷醉', '舔薄荷', '醉了', '猫嗨'],
    '589_rainy_window_mood': ['下雨', '雨天', '雨声', '窗外雨', '潮湿', '滴答'],
    '598_late_reply_apology': ['回晚了', '迟回', '抱歉晚', '没及时', '回复迟', '晚回'],
    '616_pillow_hug_sleep': ['抱枕睡', '抱着抱枕', '搂枕头', '枕头', '抱睡', '搂着'],
    '626_sweet_dessert_crave': ['想吃甜', '甜点', '甜食', '蛋糕馋', '甜品', '巧克力'],
    '630_phone_low_battery': ['没电', '电量低', '快关机', '充电', '电池', '没电了'],
    '642_lucky_draw_win': ['抽奖中', '中奖', '幸运', '运气好', '抽中', '中了'],
    '644_ranked_climb_grind': ['打排位', '上分', '排位赛', '段位', '爬分', '掉分'],
    '649_stretch_lazy_chat': ['伸懒腰', '懒洋洋', '舒展', '懒散', '伸展', '懒劲'],
    '653_cold_winter_chat': ['冬天冷', '寒冬', '冷飕飕', '凛冬', '冬日', '冻人'],
    '657_study_exam_chat': ['考试', '复习', '学习', '刷题', '备考', '挂科'],
    '665_jeer_master_chat': ['挖苦主人', '调侃主人', '嘲一下', '损主人', '吐槽主', '嘲笑'],
    '670_dream_talk_chat': ['说梦话', '梦里说', '梦中讲', '梦呓', '梦语', '说胡话'],
    '671_anniversary_chat': ['纪念日', '周年', '相识日', '认识纪念', '念日', '一周年'],
    '672_voice_call_chat': ['打电话', '语音通话', '通话', '电话聊', '打过来', '听声音'],
    '699_yawn_contagious': ['打哈欠', '哈欠传染', '看着也困', '困倦', '跟着打', '传染'],
    '714_morning_first_word': ['早问', '清晨问', '醒后问', '早安问', '问早', '清早问'],
    '720_busy_or_free': ['忙不忙', '有空吗', '闲着吗', '在忙', '空闲', '有时间'],
    '722_seek_compliment': ['求夸', '夸夸我', '想被夸', '吹我', '夸一下', '彩虹屁'],
    '725_emoji_awsl_juejue': ['绝绝子', 'awsl', '萌爆', '可爱炸', '绝了', '冲击'],
    '726_reverse_flirt_back': ['反撩回去', '撩回主人', '回怼撩', '反向撩', '撩回', '挑逗回'],
    '733_late_night_dreaming': ['深夜做梦', '夜里梦', '半夜梦', '梦境', '夜梦', '梦里'],
    '746_winning_streak_brag': ['连胜', '连赢', '炫胜', '吹胜', '一波连胜', '连吃鸡'],
    '748_brunch_dilemma': ['早午餐', '吃啥纠结', 'brunch', '中早餐', '不知吃啥', '两餐合'],
    '749_midnight_snack_crave': ['宵夜馋', '深夜想吃', '半夜饿', '夜里馋', '夜宵想', '夜里吃'],
    '759_fish_dried_treat': ['小鱼干', '鱼干奖励', '鱼干吃', '咸鱼', '鱼干馋', '猫零食'],
    '762_ear_twitch_listen': ['耳朵抖', '耳朵动', '听到啥', '动耳朵', '耳尖', '耳朵竖'],
    '763_paw_clean_lick': ['舔爪', '爪子洗', '舔爪子', '猫洗脸', '舔毛', '爪洁'],
    '773_anniversary_memory': ['纪念回忆', '周年回想', '念日回忆', '认识纪念', '回忆纪念', '相识周年'],
    '774_holiday_alone_vibe': ['节日独自', '节日一人', '一个人过节', '独过节', '孤过节', '没人陪节'],
    '776_cold_silent_treat': ['冷战', '不理你', '冷处理', '沉默对待', '不说话', '默默'],
    '779_what_if': ['假如', '如果', '万一', '要是', '假设', '若是'],
    '781_yawn_sleepy_chat': ['哈欠困', '困到打哈欠', '哈欠连连', '困倦', '哈欠不停', '想睡哈欠'],
    '789_sneeze_cold_catch': ['打喷嚏', '感冒了', '冷到了', '喷嚏连连', '着凉', '阿嚏'],
    '794_thirsty_water_beg': ['渴死', '想喝水', '口渴', '要水喝', '喉咙干', '咽干'],
    '802_monday_blues_dread': ['周一恐惧', '周一上班', '不想上班', '周一蓝', '又周一', '上班怕'],
    '806_shopping_cart_full': ['购物车满', '加购物车', '购物车', '加车', '已加购', '清空购物车'],
    '814_morning_first_yawn': ['清晨哈欠', '早起哈欠', '醒来打哈欠', '清早哈欠', '一睁眼哈欠', '早安哈欠'],
    '815_midnight_snack_chat': ['宵夜聊', '深夜吃', '半夜吃东西', '夜宵聊', '夜里嘴馋', '夜餐聊'],
    '816_screenshot_share': ['截图分享', '截屏', '甩截图', '丢截图', '发截图', '看截图'],
    '821_tired_overtime': ['加班累', '熬夜加班', '加班到', '通宵加班', '加班疲', '工时长'],
    '824_random_question': ['随机问', '突然问', '冷问题', '怪问题', '突如其来', '冷不丁问'],
    '832_dream_recall': ['梦回忆', '记起梦', '梦境回想', '梦里记得', '回想昨夜梦', '梦的内容'],
    '838_sneeze_runny_nose': ['打喷嚏', '流鼻涕', '鼻塞', '阿嚏', '鼻水', '喷嚏不停'],
    '839_coffee_shop_visit': ['咖啡店', '咖啡馆', '喝咖啡', '拿铁', '美式', '咖啡香'],
    '841_unboxing_excitement': ['开箱', '拆包裹', '快递到', '收快递', '惊喜开箱', '拆开'],
    '85_weather_season': ['季节', '换季', '天气季', '气候转', '入秋', '入冬'],
    '872_ice_cream_melt': ['冰淇淋融', '雪糕化', '化掉了', '快化了', '融化', '滴下来'],
    '880_first_message_shy': ['第一句害羞', '初次发言', '第一条信息', '初消息', '第一句', '起头害羞'],
    '882_typo_funny': ['打错字', '错字', '手滑', '输错', '错别字', '打错了'],
    '917_random_meme_laugh': ['梗图笑', '突发梗', '随机梗', '迷之梗', '搞怪梗', '猝不及防梗'],
    '920_shopping_cart_chat': ['购物车聊', '加购物', '购物车里', '购物车装', '剁手车', '加车聊'],
    '922_broke_wallet_cry': ['钱包瘪', '剁手破产', '穷哭', '没钱了', '钱包空', '月光'],
    '924_diet_failure_chat': ['减肥失败', '减肥破功', '减肥垮', '又胖了', '没忍住吃', '减肥崩'],
    '925_late_for_work': ['上班迟到', '快迟到', '赶不上', '迟了', '路上来不及', '迟到怕'],
    '932_hotpot_dream': ['火锅', '想吃火锅', '麻辣火锅', '涮锅', '红汤', '锅底'],
    '934_cold_caught_sick': ['感冒了', '着凉病', '生病感冒', '冷出病', '感冒难受', '受凉'],
    '937_yawn_sleepy_chat': ['哈欠犯困', '打哈欠困', '困得哈欠', '困倦哈', '哈欠+困', '想睡哈欠'],
    '944_send_wrong_chat': ['发错', '发错人', '发错对象', '错发', '群发错', '私聊错'],
    '951_ear_itchy': ['耳朵痒', '挠耳朵', '抠耳朵', '耳痒', '耳尖痒', '挠耳'],
    '954_cracking_knuckles': ['掰指关节', '响指', '咔咔响', '关节响', '掰手指', '指节'],
    '962_salty_overdose': ['吃太咸', '齁咸', '太咸了', '咸过头', '齁了', '咸死'],
    '964_milk_tea_addict': ['奶茶', '一杯奶茶', '点奶茶', '波霸', '珍珠奶茶', '续命奶茶'],
    '969_diet_struggle': ['减肥挣扎', '想瘦想吃', '减肥纠结', '减重难', '瘦不下来', '又破戒'],
    '96_food_cook': ['做饭', '下厨', '炒菜', '烹饪', '掌勺', '料理'],
    '980_warm_drink_winter': ['热饮', '暖饮', '热可可', '热奶茶', '热咖啡', '冬日暖饮'],
    '994_stomach_growl_chat': ['肚子叫', '咕咕叫', '饿到响', '胃叫', '饿声', '咕噜'],
    'A1_safety_emergency': ['安全', '紧急', '出事了', '救命', '危险', '报警'],
    'AAA7_no_customer': ['没顾客', '生意冷', '门可罗雀', '没人来', '冷清', '没生意'],
    'CCC7_pet_died': ['宠物走了', '猫狗去世', '失去宠', '宠物离世', '永别', '走丢离世'],
    'HH6_neighbor_dispute': ['邻居吵', '邻居矛盾', '隔壁吵', '邻里纠纷', '住户矛盾', '邻居烦'],
    'HHH5_nothing_wear': ['没衣服穿', '衣柜空', '不知穿啥', '没合适', '没衣可穿', '穿搭难'],
    'MMM9_pivot_pain': ['转型痛苦', '转行难', '改方向', '换赛道', '转型', '艰难转'],
    'OO5_travel_burnout': ['旅游累', '旅行倦', '玩累', '行程累', '出游累', '玩到累'],
    'OO8_solo_lonely': ['独自孤独', '一个人寂寞', '独居寂', '单身孤', '没人陪孤', '孤单一人'],
    'QQQ9_fear_high': ['恐高', '怕高', '高空怕', '高处怕', '腿软高', '不敢往下'],
    'RRR8_variety_skip': ['综艺跳', '快进综艺', '综艺无聊', '跳综艺', '弃综艺', '综艺水'],
    'S9_bored_chitchat': ['无聊闲聊', '没事聊', '闲扯', '瞎聊', '随便聊', '聊点啥'],
    'T4_missing_someone': ['想念', '思念', '想他', '想念某人', '挂念', '念叨'],
    'TTT7_storage_full': ['存储满', '内存满', '空间不足', '相册满', '磁盘满', '存满'],
    'WWW6_pet_emergency': ['宠物急', '猫狗急救', '宠物受伤', '宠急诊', '猫急救', '宠物送医'],
    'YY8_birthday_disappointment': ['生日失望', '生日没人', '生日冷清', '失望生日', '生日难过', '冷生日'],
    'Z6_missed_train': ['错过火车', '没赶上车', '误车', '没赶上', '车开走', '赶不上'],
    'ZZ9_late_dinner': ['晚餐迟', '晚饭晚', '迟吃晚饭', '晚餐拖', '迟晚饭', '晚饭点迟'],
}

# Intent-specific extra pool
INTENT_EXTRA = {
    'complaint': ['累', '崩', '呜', '糟', '惨', '烦'],
    'question': ['推荐', '怎么', '哪个', '吗', '呢', '咋办'],
    'playful': ['哈哈', '调皮', '皮一下', '逗', '嘿嘿', '玩闹'],
}


def get_theme_key(filename):
    """Strip .yaml extension."""
    return filename.replace('.yaml', '')


def generate_ctx(route):
    """Generate 4-6 ctx words for a route, avoiding its keywords."""
    keywords = set(route['keywords'])
    # Also avoid words contained in route name pattern
    theme = get_theme_key(route['file'])
    pool = list(THEME_POOL.get(theme, []))

    # Filter pool: skip if word equals any keyword OR keyword equals/contains the word exactly
    filtered = []
    for w in pool:
        if w in keywords:
            continue
        # Check if word is substring of any keyword (would still overlap)
        skip = False
        for kw in keywords:
            if w == kw:
                skip = True
                break
        if not skip:
            filtered.append(w)

    # Pick 5 best (or up to 6)
    chosen = filtered[:6]
    if len(chosen) < 4:
        # Pad with intent extras
        for w in INTENT_EXTRA.get(route['intent'], []):
            if w not in chosen and w not in keywords:
                chosen.append(w)
            if len(chosen) >= 5:
                break

    # Ensure at least 4
    if len(chosen) < 4:
        # generic safe pads (last resort)
        pads = ['日常', '小事', '生活', '心情', '今天', '现在']
        for w in pads:
            if w not in chosen and w not in keywords:
                chosen.append(w)
            if len(chosen) >= 4:
                break

    return chosen[:6]


def main():
    data = json.load(open(SRC, 'r', encoding='utf-8'))
    batch = data[BATCH_INDEX]
    results = []
    missing_themes = set()
    for r in batch:
        theme = get_theme_key(r['file'])
        if theme not in THEME_POOL:
            missing_themes.add(theme)
        ctx = generate_ctx(r)
        # Final sanity check: ensure all are non-empty and 2-4 chars
        ctx_clean = []
        for w in ctx:
            if not w:
                continue
            if 2 <= len(w) <= 4:
                ctx_clean.append(w)
            elif len(w) > 4:
                ctx_clean.append(w[:4])
            # too short skipped
        # ensure 4-6
        if len(ctx_clean) < 4:
            for w in ['日常', '心情', '今天', '现在', '小事', '生活']:
                if w not in ctx_clean and w not in r['keywords']:
                    ctx_clean.append(w)
                if len(ctx_clean) >= 4:
                    break
        ctx_clean = ctx_clean[:6]
        results.append({'name': r['name'], 'ctx': ctx_clean})

    if missing_themes:
        print('MISSING THEMES:', missing_themes)
    print(f'Generated {len(results)} routes, batch has {len(batch)}')
    # Validate
    for i, (rt, res) in enumerate(zip(batch, results)):
        kws = set(rt['keywords'])
        for w in res['ctx']:
            if w in kws:
                print(f'OVERLAP: route {rt["name"]} ctx word "{w}" is keyword')
        if not (4 <= len(res['ctx']) <= 6):
            print(f'BAD LEN: route {rt["name"]} ctx len={len(res["ctx"])}')

    json.dump({'results': results}, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f'Wrote {OUT}')


if __name__ == '__main__':
    main()
