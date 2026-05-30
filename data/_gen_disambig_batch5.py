"""Generate disambiguate_context for batch 5 routes."""
import json
import re

data = json.load(open('e:/VC/Catty/data/disambig_round2_tasks.json', 'r', encoding='utf-8'))
batch = data[5]

# English->Chinese topic word mapping
TOPIC_MAP = {
    'morning': ['早安', '早起', '清晨', '起床'],
    'wakeup': ['起床', '醒来', '闹钟', '赖床'],
    'wake': ['起床', '醒来', '闹钟', '赖床'],
    'night': ['夜里', '夜晚', '熬夜', '晚上'],
    'late': ['熬夜', '深夜', '晚睡', '晚归'],
    'owl': ['夜猫', '熬夜', '深夜'],
    'lunch': ['午饭', '中午', '午餐', '饭点'],
    'ate': ['吃过', '饭点', '吃饭', '饭'],
    'sleepy': ['困', '想睡', '犯困', '打哈欠'],
    'jealous': ['吃醋', '酸', '占有', '小气'],
    'vinegar': ['吃醋', '醋意', '酸了', '醋坛'],
    'beg': ['求', '讨', '撒娇', '想要'],
    'praise': ['夸夸', '表扬', '赞美', '求夸'],
    'hehe': ['嘿嘿', '偷笑', '坏笑'],
    'giggle': ['嘿嘿', '偷笑', '咯咯'],
    'hmph': ['哼', '撇嘴', '生气'],
    'sulk': ['闹脾气', '生闷气', '撅嘴'],
    'lol': ['哈哈', '笑死', '爆笑'],
    'dying': ['笑死', '裂开', '崩溃'],
    'stuck': ['堵', '卡', '塞', '挪不动'],
    'traffic': ['车流', '路堵', '马路', '红绿灯'],
    'moon': ['月亮', '月色', '月光', '夜景'],
    'pretty': ['好看', '漂亮', '美'],
    'drink': ['喝', '小酌', '啤酒', '饮料'],
    'alcohol': ['喝酒', '小酌', '醉', '啤酒'],
    'water': ['喝水', '水杯', '解渴', '白开水'],
    'window': ['窗外', '窗户', '窗台', '风景'],
    'view': ['风景', '景色', '看风景'],
    'outside': ['窗外', '外面', '街上'],
    'happy': ['开心', '愉快', '美好', '小确幸'],
    'moment': ['瞬间', '时刻', '小事'],
    'reverse': ['反撩', '反杀', '反击'],
    'flirt': ['撩', '调情', '挑逗'],
    'blush': ['脸红', '害羞', '心跳'],
    'hug': ['抱抱', '抱', '拥抱', '搂'],
    'request': ['想要', '求', '讨'],
    'tease': ['调侃', '逗', '损'],
    'master': ['主人', '老爷'],
    'outfit': ['穿搭', '衣服', '打扮', '穿着'],
    'expression': ['表情', '表情包', '颜文字'],
    'xiaosi': ['笑死', '笑死人'],
    'work': ['上班', '工作', '加班'],
    'slack': ['摸鱼', '划水', '躺平'],
    'moyu': ['摸鱼', '划水', '偷懒'],
    'noon': ['中午', '正午', '午时'],
    'break': ['休息', '空闲', '间歇'],
    'check': ['打卡', '记录'],
    'spoil': ['宠', '宠溺', '惯'],
    'clingy': ['黏人', '黏', '腻', '撒娇'],
    'kiss': ['亲', '亲亲', '嘴对嘴', '吻'],
    'pester': ['缠', '磨', '黏'],
    'cat': ['猫', '猫咪', '小猫'],
    'fish': ['鱼', '小鱼', '鱼干'],
    'snack': ['零食', '小食', '吃的'],
    'tail': ['尾巴', '猫尾', '尾尖'],
    'stepped': ['踩到', '被踩', '踩'],
    'status': ['状态', '心情'],
    'takeout': ['外卖', '送餐', '骑手'],
    'slow': ['慢', '拖', '等'],
    'random': ['随便', '突然', '冷不丁'],
    'grievance': ['委屈', '受委屈', '难过'],
    'acting': ['装', '装作', '故意'],
    'pout': ['撅嘴', '噘嘴', '嘟嘴'],
    'small': ['小', '一点点'],
    'complaints': ['抱怨', '吐槽', '嘀咕'],
    'complaint': ['抱怨', '吐槽', '嘀咕'],
    'anti': ['反', '不要', '反套路'],
    'routine': ['套路', '常规', '老一套'],
    'chatter': ['闲聊', '碎碎念', '叨叨'],
    'emoji': ['表情包', '颜文字'],
    'reactions': ['反应', '回应'],
    'road': ['路上', '马路', '街道'],
    'encounter': ['遇到', '撞见', '碰到'],
    'weekend': ['周末', '休息日', '双休'],
    'plan': ['计划', '安排', '打算'],
    'goodnight': ['晚安', '睡觉', '安睡'],
    'seek': ['求', '讨', '要'],
    'food': ['食物', '吃的', '美食'],
    'craving': ['想吃', '馋', '嘴馋'],
    'chat': ['聊', '聊天', '唠'],
    'commute': ['通勤', '上下班', '挤地铁'],
    'tired': ['累', '疲', '困'],
    'bus': ['公交', '公车', '巴士'],
    'subway': ['地铁', '换乘'],
    'cold': ['冷淡', '冷'],
    'silent': ['沉默', '不说话'],
    'treat': ['冷处理', '冷战'],
    'aloof': ['高冷', '清冷', '冷淡'],
    'tsundere': ['傲娇', '别扭'],
    'lie': ['说谎', '反话'],
    'love': ['爱', '喜欢'],
    'huff': ['哼', '气'],
    'heng': ['哼', '撇嘴'],
    'pet': ['宠物', '小动物'],
    'sick': ['生病', '不舒服'],
    'pain': ['疼', '痛'],
    'cute': ['可爱', '萌'],
    'angry': ['生气', '气'],
    'cry': ['哭', '哭哭'],
    'laugh': ['笑'],
    'spoiled': ['撒娇'],
    'fluff': ['毛茸茸', '蓬松'],
    'meow': ['喵'],
    'purr': ['呼噜'],
    'play': ['玩'],
    'game': ['游戏'],
    'sleep': ['睡觉', '困'],
    'study': ['学习'],
    'exam': ['考试'],
    'rain': ['下雨', '雨'],
    'snow': ['下雪', '雪'],
    'hot': ['热', '热死'],
    'mosquito': ['蚊子'],
    'face': ['脸'],
    'hum': ['哼'],
    'recommend': ['推荐', '介绍'],
    'rec': ['推荐'],
    'movie': ['电影', '影片'],
    'film': ['电影'],
    'book': ['书', '小说'],
    'music': ['音乐', '歌'],
    'song': ['歌曲', '音乐'],
    'tea': ['茶', '泡茶'],
    'coffee': ['咖啡', '拿铁'],
    'milk': ['奶', '牛奶'],
    'sweet': ['甜', '甜品'],
    'cake': ['蛋糕'],
    'bread': ['面包'],
    'noodle': ['面条', '拉面'],
    'rice': ['米饭', '盖饭'],
    'dumpling': ['饺子'],
    'hotpot': ['火锅'],
    'bbq': ['烧烤'],
    'spicy': ['辣', '麻辣'],
    'sour': ['酸'],
    'salty': ['咸'],
    'walk': ['散步', '走路'],
    'run': ['跑步', '跑'],
    'gym': ['健身'],
    'shop': ['购物', '逛街'],
    'buy': ['买'],
    'sale': ['打折', '促销'],
    'home': ['家'],
    'room': ['房间'],
    'bed': ['床', '床上'],
    'pillow': ['枕头'],
    'blanket': ['被子'],
    'phone': ['手机'],
    'laptop': ['电脑'],
    'computer': ['电脑'],
    'tv': ['电视'],
    'movie_night': ['影夜'],
    'date': ['约会'],
    'love_talk': ['情话'],
    'crush': ['心动'],
    'cute_act': ['卖萌'],
    'sigh': ['叹气'],
    'yawn': ['哈欠', '打哈欠'],
    'stretch': ['伸懒腰'],
    'sneeze': ['打喷嚏'],
    'cough': ['咳嗽'],
    'hungry': ['饿'],
    'thirsty': ['渴'],
    'full': ['饱'],
    'eat': ['吃'],
    'tease_master': ['调侃主人'],
    'pun': ['谐音'],
    'joke': ['笑话'],
    'lazy': ['懒', '懒得'],
    'energy': ['活力', '元气'],
    'mood': ['心情'],
    'sad': ['难过', '伤心'],
    'lonely': ['孤单'],
    'miss': ['想'],
    'wait': ['等'],
    'patience': ['耐心'],
    'impatient': ['不耐烦'],
    'gentle': ['温柔'],
    'soft': ['软'],
    'tender': ['温柔'],
    'safe': ['安心'],
    'warm': ['温暖', '暖'],
    'cool': ['凉'],
    'rain_day': ['雨天'],
    'sunny': ['晴天', '太阳'],
    'cloudy': ['阴天'],
    'wind': ['风'],
    'storm': ['暴雨'],
    'season': ['季节'],
    'spring': ['春天'],
    'summer': ['夏天'],
    'autumn': ['秋天'],
    'winter': ['冬天'],
    'fall': ['秋天'],
    'bday': ['生日'],
    'birthday': ['生日'],
    'newyear': ['新年'],
    'holiday': ['假期'],
    'vacation': ['假期'],
    'travel': ['旅行', '旅游'],
    'trip': ['旅行'],
    'sea': ['海'],
    'mountain': ['山'],
    'sky': ['天空'],
    'star': ['星星'],
    'sunset': ['日落', '夕阳'],
    'sunrise': ['日出'],
    'firework': ['烟花'],
    'festival': ['节日'],
    'memorial': ['纪念'],
    'gift': ['礼物'],
    'surprise': ['惊喜'],
    'celebrate': ['庆祝'],
    'party': ['派对'],
    'dance': ['跳舞'],
    'sing': ['唱歌'],
    'concert': ['演唱会'],
    'idol': ['偶像'],
    'fan': ['粉丝'],
    'photo': ['照片', '拍照'],
    'camera': ['相机'],
    'selfie': ['自拍'],
    'social': ['社交'],
    'chat_app': ['聊天软件'],
    'qq': ['QQ'],
    'wx': ['微信'],
    'weibo': ['微博'],
    'douyin': ['抖音'],
    'reading': ['阅读', '看书'],
    'novel': ['小说'],
    'manga': ['漫画'],
    'comic': ['漫画'],
    'anime': ['动漫'],
    'cartoon': ['动画'],
    'idol_chat': ['偶像聊'],
    'apartment': ['公寓'],
    'view_outside': ['看窗外'],
    'building': ['楼'],
    'street': ['街道'],
    'park': ['公园'],
    'cafe': ['咖啡馆'],
    'mall': ['商场'],
    'station': ['车站'],
    'bus_stop': ['公交站'],
    'lazy_day': ['懒散'],
    'productive': ['高效'],
    'gloomy': ['阴沉'],
    'cheer': ['打气'],
    'cheer_up': ['加油'],
    'support': ['支持'],
    'comfort': ['安慰'],
    'cute_moment': ['可爱'],
    'silly': ['傻'],
    'dumb': ['笨'],
    'smart': ['聪明'],
    'shy': ['害羞'],
    'tease_back': ['反撩'],
    'silent_treat': ['冷处理'],
    'aloof_chat': ['高冷'],
    'random_emoji': ['表情包'],
    'random_grievance': ['委屈'],
    'random_chatter': ['碎碎念'],
    'random_topic': ['随便聊'],
    'tail_pull': ['揪尾巴'],
    'ear_rub': ['揉耳'],
    'head_pat': ['摸头'],
    'belly_rub': ['揉肚子'],
    'paw': ['爪子'],
    'whiskers': ['胡须'],
    'fur': ['毛'],
    'tongue': ['舌头'],
    'tooth': ['牙'],
    'baby_talk': ['宝宝话'],
    'tsundere_lie': ['口是心非'],
    'tsundere_love': ['傲娇爱'],
    'pout_face': ['鼓脸'],
    'snack_time': ['零食'],
    'snack_request': ['讨零食'],
    'kiss_pester': ['求亲亲'],
    'hug_request': ['求抱抱'],
    'spoil_clingy': ['撒娇腻'],
    'beg_praise': ['求夸'],
    'jealous_pout': ['吃醋鼓'],
    'jealous_vinegar': ['醋坛'],
    'cold_silent_treat': ['冷战'],
    'cold_aloof_chat': ['高冷聊'],
    'anti_routine': ['反套路'],
    'hum_pout': ['哼撅嘴'],
    'hmph_huff': ['哼气'],
    'hmph_sulk': ['哼撅嘴'],
    'tease_master_outfit': ['调侃穿搭'],
    'reverse_flirt_blush': ['反撩脸红'],
    'tail_stepped': ['尾巴被踩'],
    'cat_fish_snack': ['猫鱼干'],
    'random_emoji_chat': ['表情包聊'],
    'late_night_owl': ['夜猫'],
    'morning_wakeup_chat': ['早安聊'],
    'morning_wake_up': ['早起'],
    'noon_lunch_break': ['午休'],
    'goodnight_chat': ['晚安聊'],
    'sleepy_already': ['困了'],
    'ate_lunch_yet': ['吃午饭'],
    'drink_water_check': ['喝水打卡'],
    'drink_alcohol_chat': ['喝酒聊'],
    'window_view_outside': ['窗外'],
    'work_slack_moyu': ['上班摸鱼'],
    'takeout_slow': ['外卖慢'],
    'commute_tired': ['通勤累'],
    'commute_traffic_tired': ['堵车累'],
    'bus_commute_tired': ['公交挤'],
    'small_things': ['小事'],
    'small_complaints': ['小抱怨'],
    'small_happy_moment': ['小确幸'],
    'food_craving': ['馋'],
    'road_encounter': ['路遇'],
    'weekend_plan': ['周末'],
    'seek_praise': ['求夸'],
    'jealous_pout_chat': ['吃醋'],
    'beg_for_praise': ['求夸'],
    'random_chatter_chat': ['碎碎念'],
    'tease_master_chat': ['调侃主人'],
    'expression_xiaosi': ['笑死表情'],
    'lol_dying': ['笑裂'],
    'hehe_giggle': ['嘿嘿'],
    'reverse_flirt': ['反撩'],
    'cat_meow': ['喵'],
    'cat_purr': ['呼噜'],
    'cat_act': ['撒娇'],
    'self': ['自夸'],
    'show_off': ['炫耀'],
    'brag': ['吹嘘'],
    'humblebrag': ['凡尔赛'],
    'lonely_chat': ['孤单聊'],
    'miss_user': ['想主人'],
    'wait_user': ['等主人'],
    'door': ['门'],
    'curtain': ['窗帘'],
    'mirror': ['镜子'],
    'desk': ['桌子'],
    'chair': ['椅子'],
    'kitchen': ['厨房'],
    'bathroom': ['浴室'],
    'shower': ['洗澡'],
    'bath': ['泡澡'],
    'towel': ['毛巾'],
    'soap': ['香皂'],
    'shampoo': ['洗发水'],
    'comb': ['梳子'],
    'hair': ['头发'],
    'eye': ['眼睛'],
    'mouth': ['嘴'],
    'lips': ['嘴唇'],
    'cheek': ['脸蛋'],
    'forehead': ['额头'],
    'chin': ['下巴'],
    'neck': ['脖子'],
    'shoulder': ['肩'],
    'arm': ['手臂'],
    'hand': ['手'],
    'finger': ['手指'],
    'leg': ['腿'],
    'foot': ['脚'],
    'feet': ['脚'],
    'belly': ['肚子'],
    'back': ['背'],
    'waist': ['腰'],
    # additional common words from batch
    'care': ['关心', '在意'],
    'remind': ['提醒'],
    'warm': ['暖', '温暖'],
    'comfort': ['安慰'],
    'want': ['想要'],
    'meme': ['梗', '表情包'],
    'midnight': ['深夜', '半夜'],
    'doing': ['在干嘛', '做什么'],
    'mood': ['心情'],
    'weather': ['天气'],
    'lost': ['丢', '丢失'],
    'cry': ['哭', '抽泣'],
    'wuwu': ['呜呜'],
    'now': ['现在'],
    'rest': ['休息'],
    'sudden': ['突然'],
    'ask': ['问'],
    'owner': ['主人'],
    'panic': ['慌'],
    'need': ['需要'],
    'lazy': ['懒'],
    'dream': ['梦'],
    'rainy': ['雨天'],
    'day': ['天'],
    'well': ['好'],
    'insomnia': ['失眠'],
    'yawn': ['哈欠'],
    'self_praise': ['自夸'],
    'snow': ['雪'],
    'outfit_chat': ['穿搭'],
    'talk': ['聊'],
    'lucky': ['幸运'],
    'busy': ['忙'],
    'dress': ['穿', '裙子'],
    'you': ['你'],
    'awsl': ['啊我死了'],
    'wear': ['穿'],
    'wifi': ['wifi', '网'],
    'gacha': ['抽卡'],
    'red': ['红'],
    'packet': ['红包'],
    'shopping': ['购物'],
    'micro': ['小'],
    'emotion': ['情绪'],
    'compliment': ['夸'],
    'call': ['打电话'],
    'crave': ['馋'],
    'fat': ['胖'],
    'company': ['陪伴'],
    'daily': ['日常'],
    'meal': ['吃饭'],
    'early': ['早'],
    'fail': ['失败'],
    'envy': ['羡慕'],
    'thinking': ['想'],
    'rage': ['怒'],
    'hiccup': ['打嗝'],
    'attack': ['攻击'],
    'take': ['拿'],
    'homesick': ['想家'],
    'dodge': ['躲'],
    'question': ['问题'],
    'struggle': ['挣扎'],
    'long': ['久'],
    'bored': ['无聊'],
    'pull': ['拉'],
    'luck': ['运气'],
    'chitchat': ['闲聊'],
    'forgot': ['忘'],
    'umbrella': ['伞'],
    'reverse_flirt_chat': ['反撩'],
    'towel': ['毛巾'],
    'life': ['生活'],
    'ambiguous': ['暧昧'],
    'dinner': ['晚饭'],
    'parent': ['父母'],
    'trope': ['套路'],
    'contagious': ['传染'],
    'couple': ['情侣'],
    'pda': ['秀恩爱'],
    'react': ['反应'],
    'die': ['死'],
    'not': ['不'],
    'drowsy': ['困'],
    'there': ['那里'],
    'neck': ['脖子'],
    'complain': ['抱怨'],
    'return': ['退'],
    'drunk': ['醉'],
    'seeking': ['求'],
    'journey': ['路'],
    'subway': ['地铁'],
    'internet': ['网络'],
    'disconnect': ['断网'],
    'free': ['空'],
    'festival_chat': ['过节'],
    'addict': ['上瘾'],
    'word': ['话'],
    'sulky': ['闹脾气'],
    'pouting': ['撅嘴'],
    'season_chat': ['季节'],
    'job': ['工作'],
    'joy': ['开心'],
    'thing': ['东西'],
    'text': ['文字'],
    'where': ['哪里'],
    'old': ['老'],
    'humming': ['哼歌'],
    'drama': ['剧'],
    'win': ['赢'],
    'heihei': ['嘿嘿'],
    'inspiration': ['灵感'],
    'photo_chat': ['拍照'],
    'bad': ['坏'],
    'jerky': ['肉干'],
    'share': ['分享'],
    'hungry': ['饿'],
    'pillow': ['枕头'],
    'buying': ['买'],
    'furniture': ['家具'],
    'car': ['车'],
    'silent_chat': ['沉默'],
    'caught': ['被抓'],
    'office': ['办公室'],
    'anger': ['生气'],
    'yingying': ['呜呜'],
    'cart': ['购物车'],
    'full': ['饱'],
    'excite': ['兴奋'],
    'pet_chat': ['宠物'],
    'caffeine': ['咖啡因'],
    'litter': ['猫砂'],
    'haul': ['购物'],
    'blanket': ['被子'],
    'cozy': ['舒服'],
    'warmth': ['温暖'],
    'shoulder': ['肩膀'],
    'act': ['装'],
    'crying_chat': ['哭'],
    'idle': ['闲'],
    'regret': ['后悔'],
    'addiction': ['上瘾'],
    'workout': ['运动'],
    'fridge': ['冰箱'],
    'sweet_chat': ['甜'],
    'off': ['关'],
    'disappoint': ['失望'],
    'bengbu': ['绷不住'],
    'ice': ['冰'],
    'wakeup_chat': ['起床'],
    'cold_silent_chat': ['冷战'],
    'cold_aloof_chat': ['高冷'],
    'first': ['第一'],
    'yet': ['吗'],
    'lookalike': ['像'],
    'gulu': ['咕噜'],
    'snore': ['打呼'],
    'screen': ['屏幕'],
    'battery': ['电量'],
    'wechat': ['微信'],
    'voice': ['语音'],
    'video': ['视频'],
    'soup': ['汤'],
    'snack_chat': ['零食'],
    'sneeze_chat': ['打喷嚏'],
    'tiredness': ['累'],
}

INTENT_WORDS = {
    'complaint': ['累瘫', '烦躁', '崩溃', '呜呜', '受不了', '难受'],
    'playful': ['撒娇', '调侃', '嘟囔', '小声', '蹭蹭'],
    'question': ['推荐', '怎么', '哪个', '是啥', '介绍'],
}

# Curated whitelist of well-formed 2-3 char Chinese words that can appear in utterances
# and serve as good context discriminators.
KNOWN_WORDS = set([
    # food
    '咖啡', '拿铁', '奶茶', '牛奶', '蛋糕', '面包', '面条', '米饭', '饺子', '火锅', '烧烤',
    '零食', '小食', '甜品', '雪糕', '冰淇淋', '巧克力', '布丁', '果冻', '糖果',
    '水果', '苹果', '橘子', '葡萄', '西瓜', '草莓', '香蕉', '梨子', '柠檬',
    '外卖', '饭点', '吃饭', '吃过', '饭菜', '小吃', '宵夜', '夜宵', '早餐', '午餐', '晚餐',
    '茶水', '泡茶', '泡面', '速食', '快餐',
    # weather
    '雨天', '晴天', '阴天', '下雪', '下雨', '雪花', '雷声', '打雷', '暴雨', '台风', '彩虹', '太阳',
    '月亮', '星星', '云朵', '风',
    '气温', '降温', '升温', '回暖', '湿冷', '闷热',
    # body
    '脸红', '害羞', '心跳', '撒娇', '调情', '吃醋', '醋意', '醋坛',
    '尾巴', '猫尾', '尾尖', '耳朵', '猫耳', '爪子', '小爪', '肉垫', '胡须',
    '抱抱', '蹭蹭', '亲亲', '摸摸', '揉揉', '拍拍', '摸头', '揉耳',
    '伸懒腰', '打哈欠', '打喷嚏', '打嗝', '叹气', '咳嗽', '哽咽',
    # mood
    '开心', '难过', '伤心', '委屈', '害怕', '生气', '难受', '舒服', '安心', '安慰',
    '美好', '小确幸', '幸运', '倒霉', '无聊', '兴奋', '激动', '紧张', '焦虑', '崩溃',
    '失眠', '困了', '想睡', '犯困', '哈欠',
    # actions
    '起床', '醒来', '闹钟', '赖床', '熬夜', '睡觉', '入睡', '深夜', '半夜', '夜里', '夜晚',
    '上班', '下班', '加班', '摸鱼', '划水', '躺平', '休息', '通勤', '挤地铁', '挤公交',
    '堵车', '路堵', '车流', '高峰',
    '逛街', '购物', '打折', '促销', '买买买', '剁手', '退货', '退款', '快递',
    '抽卡', '充值', '氪金', '游戏', '联机', '副本',
    # places
    '家里', '房间', '床上', '客厅', '厨房', '浴室', '阳台', '窗外', '窗户', '楼下', '楼上',
    '街上', '路上', '马路', '街道', '公园', '咖啡馆', '便利店', '超市', '商场', '车站', '地铁站',
    # clothing
    '穿搭', '衣服', '裙子', '裤子', '鞋子', '袜子', '外套', '毛衣', '睡衣', '过膝袜', '制服',
    # cat-related
    '猫粮', '猫砂', '罐头', '鱼干', '小鱼', '逗猫棒', '抓板', '呼噜', '喵叫',
    # times
    '早上', '上午', '中午', '下午', '傍晚', '晚上', '深夜', '凌晨',
    '今天', '昨天', '明天', '周末', '工作日', '假期', '休息日',
    # idol/anime
    '动漫', '番剧', '小说', '漫画', '电影', '电视剧', '综艺', '直播', '弹幕',
    '偶像', '爱豆', '演唱会', '应援', '粉丝', '同人',
    # social
    '聊天', '聊聊', '语音', '视频', '电话', '消息', '红包', '点赞', '评论',
    # complaints
    '受不了', '不行了', '烦死', '累死', '气死', '崩了',
    # praise
    '夸夸', '表扬', '赞美', '求夸', '夸我', '宠我',
    # tsundere
    '嘟囔', '小声', '哼哼', '撅嘴', '噘嘴', '嘟嘴', '撇嘴',
    # other
    '日常', '碎碎念', '小事', '心情', '一下', '蹭蹭', '碎念', '默默', '突然', '忽然',
    '原来', '没想到', '不行', '受不了', '反撩', '反杀', '反击', '反套路',
    '主人', '老爷', '阿姨', '叔叔', '老板',
    '想你', '想家', '想念', '思念', '念你', '挂念', '想主人',
    '陪我', '陪伴', '陪你', '陪着',
    # questions
    '推荐', '介绍', '哪个', '哪家', '是啥', '咋办', '怎么办', '为啥', '哪里', '什么时候',
    # complaints body
    '腰疼', '背疼', '颈椎', '腿酸', '脚酸', '头疼', '肚子疼', '酸痛',
    '头晕', '发烧', '感冒', '过敏', '咳嗽',
    # daily
    '看书', '阅读', '学习', '考试', '复习', '上课', '下课', '作业',
    '健身', '运动', '跑步', '散步', '骑车',
    '拍照', '自拍', '滤镜', '修图',
    # money
    '工资', '发薪', '存钱', '花钱', '透支', '账单', '房租',
    # web
    '断网', 'wifi', '充电', '充电', '没电', '电量', '低电量', '关机',
    # festivals
    '过节', '过年', '生日', '生日蛋糕', '礼物', '惊喜', '庆祝', '派对',
    # travel
    '旅游', '旅行', '出差', '出门', '回家', '到家',
    # sleep
    '困意', '困死', '困了', '睡意', '梦里', '做梦', '梦境',
    # emotions extra
    '吐槽', '抱怨', '嘀咕', '叨叨', '碎碎',
    # cute
    '可爱', '萌萌', '软乎', '糯糯', '甜甜', '元气',
    # negative
    '烦躁', '焦虑', '抑郁', '低落', '低气压', '丧',
    # food cravings
    '想吃', '馋了', '嘴馋', '解馋',
    # specific scenarios
    '路遇', '撞见', '碰到', '偶遇', '巧遇',
    '排队', '等位', '等单', '等车',
    '迟到', '晚点', '准时',
    # internet slang
    '笑死', '裂开', '绷不住', '太顶', '太炸', '芭比Q', '芜湖',
    '冲冲', '冲鸭', '加油', '坚持',
    # specific tropes
    '反话', '口是心非', '别扭', '傲娇', '死傲娇',
    '冷战', '冷处理', '高冷', '清冷', '冷漠',
    '霸总', '套路', '老一套',
])

FALLBACK = ['日常', '聊聊', '碎碎念', '小事', '心情', '一下', '嘀咕', '蹭蹭']


def file_to_topic_words(fname):
    base = fname.replace('.yaml', '')
    parts = base.split('_')
    parts = [p for p in parts if not p.isdigit() and p.lower() not in ('chat',)]
    return parts


# trailing particles - words ending here are likely fragments
BAD_TAIL = set('的了又啦呢吧吗着也很都从向往跟和与就才仅而是去过来要会能可以把被让叫又必经在到')
# leading particles
BAD_HEAD = set('的了又啦呢吧吗着也很都从向往跟和与就才仅而是去过来要会能可以把被让叫又把不没已经太就')
# direct stop fragments often produced by sliding window
STOP_FRAGS = set([
    '车堵', '堵车堵', '车堵死', '交又晚', '等地', '醒了醒', '已经', '经起',
    '上必', '上必喝', '式感', '耍小', '小性', '性子', '绝了绝',
    '我爱', '我爱你', '想你', '腰快', '快断', '腰快断', '小测挂',
    '验失', '失败', '没什', '没什么', '什么', '到底', '不行', '不想',
    '怎么办', '怎么', '为什么', '为啥', '哪里', '哪个', '在哪', '什么时候',
])


def char_segments(s, ns=(2, 3)):
    """Extract chinese char windows, filtered to skip fragments with bad heads/tails."""
    segs = re.findall(r'[一-鿿]+', s)
    out = []
    for seg in segs:
        for n in ns:
            for i in range(len(seg) - n + 1):
                w = seg[i:i + n]
                # skip if last char is a particle
                if w[-1] in BAD_TAIL:
                    continue
                if w[0] in BAD_HEAD:
                    continue
                if w in STOP_FRAGS:
                    continue
                out.append(w)
    return out


def gen_ctx(route):
    file = route['file']
    keywords = set(route['keywords'])
    intent = route['intent']
    utterances = route['utterances']
    siblings = route.get('siblings', [])
    sib_utters = set(s['first_utterance'] for s in siblings)
    sib_files = [s.get('topic_hint', '') for s in siblings]

    my_topics = file_to_topic_words(file)
    sib_topics = set()
    for sf in sib_files:
        sib_topics.update(file_to_topic_words(sf))

    # build sibling chinese word pool (to exclude)
    sib_word_pool = set()
    for w in sib_topics:
        sib_word_pool.update(TOPIC_MAP.get(w, []))
    # also from sibling first_utterances - char windows
    for su in sib_utters:
        cleaned = re.sub(r'\{[^}]*\}', '', su)
        for seg in char_segments(cleaned, (2, 3, 4)):
            sib_word_pool.add(seg)

    unique_eng = [w for w in my_topics if w not in sib_topics]
    shared_eng = [w for w in my_topics if w in sib_topics]

    candidates = []
    seen = set()

    def add(w):
        if not w or len(w) < 2 or len(w) > 4:
            return
        if w in keywords or w in seen:
            return
        # block sibling-pool words to maximize disambiguation
        if w in sib_word_pool:
            return
        # block if it's a strict substring of another seen word (avoid 车堵/堵车堵 noise)
        for ex in seen:
            if (w in ex and len(w) < len(ex)) or (ex in w and len(ex) < len(w)):
                # keep the shorter cleaner topic word usually
                if w in ex:
                    return
                # ex is in w: remove ex and add w (w more specific)
                # but to keep stable order, just skip w
                return
        candidates.append(w)
        seen.add(w)

    # 1. unique topic chinese words (highest quality)
    for w in unique_eng:
        for cw in TOPIC_MAP.get(w, []):
            add(cw)

    # 2. extract well-formed chinese words from utterances (whitelisted)
    if len(candidates) < 6:
        for utter in utterances:
            cleaned = re.sub(r'\{[^}]*\}', '', utter)
            for seg in char_segments(cleaned, (2, 3)):
                if seg in KNOWN_WORDS:
                    add(seg)
                if len(candidates) >= 6:
                    break
            if len(candidates) >= 6:
                break

    # 3. shared topic words (still on-topic)
    if len(candidates) < 5:
        for w in shared_eng:
            for cw in TOPIC_MAP.get(w, []):
                if cw not in sib_word_pool:
                    add(cw)

    # 4. extract specific 2-char head from utterance chunks (looser - no whitelist)
    if len(candidates) < 4:
        for utter in utterances:
            cleaned = re.sub(r'\{[^}]*\}', '', utter)
            chunks = re.split(r'[，。！？、,. !?～~]+', cleaned)
            for chunk in chunks:
                # try first 2 chars as head
                if len(chunk) >= 2:
                    head = chunk[:2]
                    if re.match(r'^[一-鿿]{2}$', head):
                        # extra filter: avoid common bad starts
                        if head not in ('什么', '怎么', '为啥', '哪里', '今天', '昨天', '明天', '我是', '我的', '一个', '一下') \
                           and head[-1] not in '不没已经太就也很要会能可以也都还又把被让叫一二三四五六七八九十几':
                            add(head)
                if len(candidates) >= 6:
                    break
                if len(candidates) >= 5:
                    break
            if len(candidates) >= 5:
                break

    # 5. intent words (last resort)
    if len(candidates) < 4:
        for w in INTENT_WORDS.get(intent, []):
            add(w)

    # 6. fallback
    if len(candidates) < 4:
        for w in FALLBACK:
            if w not in keywords and w not in seen:
                candidates.append(w)
                seen.add(w)
            if len(candidates) >= 4:
                break

    # final pad if still <4
    pad = ['一下', '嘀咕', '蹭蹭', '喵呜', '哼哼', '小小', '碎念', '默默']
    pi = 0
    while len(candidates) < 4 and pi < len(pad):
        w = pad[pi]
        pi += 1
        if w not in keywords and w not in seen:
            candidates.append(w)
            seen.add(w)

    return candidates[:6]


results = []
for r in batch:
    ctx = gen_ctx(r)
    # validate
    assert 4 <= len(ctx) <= 6, f"{r['name']} ctx len {len(ctx)}"
    for w in ctx:
        assert w and w not in r['keywords'], f"{r['name']} word {w} conflicts keywords"
    results.append({'name': r['name'], 'ctx': ctx})

out = {'results': results}
json.dump(out, open('e:/VC/Catty/data/_gen_disambig_batch5_out.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=2)
print('done, count:', len(results))
# print first 10 samples
for r in results[:10]:
    print(r['name'], '->', r['ctx'])
print('---')
print('total len ctx histogram:')
from collections import Counter
print(Counter(len(r['ctx']) for r in results))
