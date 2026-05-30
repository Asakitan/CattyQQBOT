import json, re, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

d = json.load(open('e:/VC/Catty/data/_b0.json','r',encoding='utf-8'))

THEME_MAP = {
    # food / drink
    'bubble': ['奶茶', '芋圆', '珍珠', '波霸'],
    'tea': ['奶茶', '茶饮'],
    'coffee': ['咖啡', '美式', '拿铁', '续杯'],
    'milk': ['奶茶'],
    'lunch': ['午饭', '饭点', '吃饭'],
    'dinner': ['晚饭', '饭点'],
    'breakfast': ['早饭', '早餐'],
    'eat': ['吃饭', '饭点'],
    'food': ['吃饭'],
    'hot': ['热的'],
    'pot': ['锅'],
    'hotpot': ['火锅', '锅底', '涮'],
    'fish': ['摸鱼', '鱼'],
    'snack': ['零食', '小吃'],
    'cake': ['蛋糕', '甜品'],
    'spicy': ['辣的', '辣'],
    'sweet': ['甜的'],
    'meat': ['肉'],
    'noodle': ['面条', '面'],
    'rice': ['米饭'],
    'takeout': ['外卖', '点单'],
    'order': ['点单', '点餐'],
    'canteen': ['食堂'],
    'cook': ['做饭'],
    'kitchen': ['厨房'],
    'craving': ['想吃'],
    'cazhuwu': ['外卖'],

    # weather / season
    'cold': ['冷', '冻僵', '保暖'],
    'warm': ['保暖', '暖'],
    'snow': ['下雪', '雪天'],
    'rain': ['下雨', '雨天'],
    'wind': ['风大', '刮风'],
    'sun': ['太阳', '晴天'],
    'weather': ['天气'],
    'first': ['第一场', '初次'],
    'autumn': ['秋天'],
    'winter': ['冬天'],
    'summer': ['夏天'],
    'spring': ['春天'],
    'window': ['窗外'],
    'diqiwen': ['零下', '低温'],
    'lingxia': ['零下'],
    'leng': ['冷'],
    'lengsi': ['冷死'],
    'mengjing': ['梦境'],

    # sleep / night
    'sleep': ['睡觉', '入睡'],
    'sleepy': ['困', '想睡'],
    'tired': ['累', '疲惫'],
    'insomnia': ['失眠', '睡不着'],
    'dream': ['做梦', '梦到'],
    'night': ['夜里', '晚上'],
    'goodnight': ['晚安'],
    'wanan': ['晚安'],
    'morning': ['早安', '早上'],
    'bedtime': ['睡前'],
    'late': ['熬夜', '半夜'],
    'owl': ['熬夜'],
    'early': ['早睡'],
    'lazy': ['懒'],
    'zao': ['早上'],
    'meng': ['做梦'],
    'zuomeng': ['做梦', '梦'],
    'had': ['昨晚'],
    'share': ['分享'],

    # work / study
    'work': ['上班', '工作'],
    'job': ['工作'],
    'office': ['办公室'],
    'meeting': ['开会'],
    'overtime': ['加班'],
    'salary': ['工资'],
    'boss': ['老板'],
    'colleague': ['同事'],
    'slack': ['摸鱼'],
    'moyu': ['摸鱼'],
    'study': ['学习'],
    'exam': ['考试'],
    'homework': ['作业'],
    'class': ['上课'],
    'school': ['学校'],
    'interview': ['面试'],
    'resume': ['简历'],
    'project': ['项目'],
    'deadline': ['ddl', '截止'],
    'workfish': ['上班', '摸鱼'],
    'idle': ['发呆'],
    'noon': ['中午'],
    'break': ['休息'],

    # transport
    'traffic': ['堵车', '路上'],
    'subway': ['地铁'],
    'bus': ['公交'],
    'train': ['火车'],
    'flight': ['航班', '飞机'],
    'airport': ['机场'],
    'delay': ['延误', '晚点'],
    'stuck': ['困住'],
    'stranded': ['滞留'],
    'commute': ['通勤'],
    'walk': ['走路'],
    'taxi': ['打车'],
    'missed': ['错过'],

    # emotion
    'sad': ['难过', '伤心'],
    'cry': ['想哭'],
    'wuwu': ['呜呜'],
    'yingying': ['嘤嘤'],
    'happy': ['开心', '高兴'],
    'angry': ['生气'],
    'jealous': ['吃醋', '醋'],
    'pout': ['撅嘴'],
    'lonely': ['孤独'],
    'alone': ['一个人'],
    'bored': ['无聊'],
    'comfort': ['安慰', '抱抱'],
    'hug': ['抱抱'],
    'pat': ['摸头'],
    'kiss': ['亲亲'],
    'flirt': ['撩'],
    'love': ['喜欢'],
    'miss': ['想你'],
    'beg': ['撒娇'],
    'pester': ['黏'],
    'sulk': ['闹别扭'],
    'cute': ['可爱'],
    'shy': ['害羞'],
    'blush': ['脸红'],
    'surprise': ['惊喜'],
    'redpacket': ['红包'],
    'gift': ['礼物'],
    'praise': ['夸'],
    'dote': ['宠'],
    'hehe': ['嘿嘿'],
    'giggle': ['嘿嘿'],
    'xiaosi': ['笑死'],
    'expression': ['表情'],
    'emoji': ['表情'],
    'sticker': ['表情包'],
    'reverse': ['反撩'],
    'sudden': ['突然'],
    'lament': ['哀叹'],
    'homesick': ['想家'],
    'hs': ['想家'],
    'yigerenchifan': ['独食', '一人'],
    'curiosity': ['好奇'],
    'question': ['问'],
    'killing': ['打发'],

    # life
    'bath': ['洗澡'],
    'shower': ['洗澡'],
    'skincare': ['护肤'],
    'routine': ['流程'],
    'makeup': ['化妆'],
    'hair': ['头发'],
    'haircut': ['理发'],
    'shopping': ['购物', '买'],
    'clothes': ['衣服'],
    'wear': ['穿'],
    'dress': ['穿', '裙子'],
    'cleaning': ['打扫'],
    'chore': ['家务'],
    'declutter': ['断舍离'],
    'storage': ['收纳', '储物'],
    'sf': ['清仓'],
    'clear': ['清理'],
    'room': ['房间'],
    'home': ['家'],
    'house': ['房子'],
    'rent': ['房租'],
    'small': ['小'],
    'too': ['太'],
    'tsr': ['断舍离'],
    'jiawu': ['家务'],
    'bk': ['整理'],
    'wkp': ['周末'],
    'hc': ['家务'],
    'wkd': ['周末'],
    'lwm': ['懒散'],

    # health
    'sick': ['生病'],
    'fever': ['发烧'],
    'cough': ['咳嗽'],
    'cold_sick': ['感冒'],
    'headache': ['头痛'],
    'stomach': ['胃'],
    'period': ['大姨妈'],
    'rest': ['休息'],
    'health': ['健康'],
    'medicine': ['吃药'],
    'hospital': ['医院'],

    # entertainment
    'game': ['游戏', '打游戏'],
    'movie': ['电影'],
    'music': ['音乐'],
    'song': ['歌'],
    'concert': ['演唱会'],
    'book': ['书'],
    'read': ['看书'],
    'novel': ['小说'],
    'anime': ['动漫'],
    'drama': ['追剧'],
    'tv': ['电视'],

    # social / family
    'family': ['家人'],
    'mom': ['妈妈'],
    'dad': ['爸爸'],
    'sister': ['学姐'],
    'brother': ['兄弟'],
    'friend': ['朋友'],
    'lover': ['对象'],
    'date': ['约会'],
    'crush': ['暗恋'],
    'breakup': ['分手'],
    'single': ['单身'],
    'sl': ['单身'],
    'nf': ['没朋友'],
    'group': ['群里'],
    'chat': ['聊天'],
    'dc': ['闲聊'],
    'dye': ['吃了吗'],
    'did': ['吃了吗'],
    'eaten': ['吃过'],
    'you': ['你'],

    # internet / meme
    'meme': ['梗'],
    'cat': ['猫', '猫咪'],
    'pet': ['宠物'],
    'dog': ['狗'],
    'daily': ['日常', '碎碎念'],
    'meow': ['喵叫', '猫叫'],
    'chatter': ['碎碎念', '闲谈'],

    # money
    'money': ['钱'],
    'broke': ['没钱', '穷'],
    'poor': ['穷'],
    'rich': ['有钱'],

    # misc
    'check': ['关心'],
    'remind': ['提醒'],
    'care': ['关心'],
    'warmth': ['暖'],
    'weekend': ['周末'],
    'plan': ['计划'],
    'evening': ['傍晚'],
    'ed': ['晚饭'],
    'edc': ['晚饭'],
    'nbc': ['中午'],
    'nlb': ['午休'],
    'noon_break': ['午休'],
    'noon_lunch_break': ['午休'],
    'hot_pot_craving': ['火锅'],
    'hpc': ['火锅'],
    'storage_full': ['爆满'],
    'mh': ['早安'],
    'ng': ['晚安'],
    'wa': ['晚安'],
    'ser': ['提醒'],
    'fs': ['初雪'],
    'fsc': ['初雪'],
    'ch': ['冷'],
    'ww': ['天气'],
    'wwr': ['保暖'],
    'dr': ['保暖'],
    'ww_lengsi': ['冷死'],
    'status': ['状态'],
    'first_snow': ['初雪'],
    'snow_excited': ['初雪'],
    'cold_hands': ['冰手'],
    'hands': ['手'],
    'wear_warm': ['加衣'],
    'dress_warm': ['加衣'],
    'fd': ['航班'],
    'mt': ['滞留'],
    'flight_delay': ['航班'],
    'bto': ['奶茶'],
    'bubble_tea': ['奶茶'],
    'bubble_tea_order': ['奶茶'],
    'tos': ['外卖'],
    'takeout_slow': ['外卖'],
    'evening_dinner_chat': ['晚饭'],
    'yike': ['一刻'],
    'wfi': ['摸鱼'],
    'wfi_fish': ['摸鱼'],
    'workfish_idle_chat': ['摸鱼'],
    'yixihuan': ['喜欢吃'],
    'edc_yixihuan': ['喜欢吃'],
    'edc_yike': ['一刻'],
    'air': ['空气'],
    'low': ['低'],
    'high': ['高'],
}

# 反向: 给定 file basename, 输出该 file 的主题词集
def extract_tokens(basename):
    name = basename.replace('.yaml','')
    m = re.match(r'^[A-Z]*\d+[A-Z]*\d*_(.+)$', name)
    if m:
        tokens = m.group(1).split('_')
    else:
        tokens = name.split('_')
    return tokens

def get_words(tokens):
    out = []
    for t in tokens:
        if t in THEME_MAP:
            out.extend(THEME_MAP[t])
    return out

INTENT_FILL = {
    'question': ['推荐', '怎么', '什么'],
    'complaint': ['好累', '崩了', '烦死'],
    'playful': ['哎呀', '嘿嘿'],
}

# 常见无意义碎片黑名单 (滑窗副产品)
BAD_FRAGMENTS = {
    '了', '的', '吗', '呢', '啊', '吧', '哦', '呀', '嘛',
    '是啥', '什么', '怎样', '一个', '我', '你', '他',
    '梦了', '家了', '到了', '完了', '过了', '了吗',
    '不是', '是不', '不要', '不会', '不能', '不知',
    '主人', '猫猫', '笨猫', '人家',  # 在大多数 catty 场景泛用, 区分力弱
}

def extract_utt_words(utts):
    """从 utterance 提取 2-4 字的中文片段. 优先完整段, 滑窗作补充."""
    words = []
    seen = set()
    # 第一轮: 完整中文段 (最大粒度, 2-5 字直接保留)
    full_segs = []
    for u in utts:
        u = re.sub(r'\{[^}]+\}', '', u)
        for m in re.finditer(r'[一-鿿]+', u):
            seg = m.group(0)
            if 2 <= len(seg) <= 5 and seg not in seen and seg not in BAD_FRAGMENTS:
                seen.add(seg)
                words.append(seg)
            full_segs.append(seg)
    # 第二轮: 长段切 3 字、2 字滑窗, 但跳过 BAD
    for seg in full_segs:
        if len(seg) <= 5:
            continue
        for size in (3, 2):
            for i in range(len(seg)-size+1):
                sub = seg[i:i+size]
                if sub in seen or sub in BAD_FRAGMENTS:
                    continue
                seen.add(sub)
                words.append(sub)
    return words

def extract_utt_full_segs(utts):
    """只取 utterance 中长度 2-5 的完整中文段."""
    words = []
    seen = set()
    for u in utts:
        u = re.sub(r'\{[^}]+\}', '', u)
        for m in re.finditer(r'[一-鿿]+', u):
            seg = m.group(0)
            if 2 <= len(seg) <= 5 and seg not in seen and seg not in BAD_FRAGMENTS:
                seen.add(seg)
                words.append(seg)
    return words

# 额外: 从 route name 中也提取一些线索词 (拼音/英文 token)
NAME_HINT = {
    'huluhulu': ['呼噜声', '猫呼噜'],
    'haoxiao': ['真好笑'],
    'huijia': ['进门', '到家'],
    'yueliang': ['月色', '月光'],
    'xiaodemiao': ['笑死'],
    'lol': ['笑哭'],
    'busy': ['有空', '空闲'],
    'meme': ['梗图'],
    'pic': ['图片'],
    'zuiying': ['嘴硬'],
    'huaijin': ['躲闪'],
    'tuna': ['金枪鱼', '鱼罐头'],
    'juewei': ['绝绝子'],
    'lazy_weekend': ['摆烂', '躺平'],
    'crv': ['想吃'],
    'cling': ['黏人'],
    'xiongshou': ['误伤'],
    'yaoshi': ['钥匙'],
    'pang': ['长胖'],
    'lat': ['晚回'],
    'ganmao': ['受寒', '感冒'],
    'xiaosi': ['笑死'],
    'lost': ['丢了'],
    'compliment': ['夸奖'],
    'promotion': ['升职'],
    'outfit': ['穿搭'],
    'what_doing': ['在干嘛'],
    'xinteng': ['心疼'],
    'yiwei': ['胡思乱想'],
    'xizao': ['洗澡'],
    'cazhuwu': ['催单'],
    'xuezhang': ['学姐'],
    'chc': ['巧克力'],
    'key': ['钥匙'],
    'sn': ['打喷嚏'],
    'mem': ['梗图'],
    'rps': ['升职红包'],
    'mof': ['穿搭'],
    'tn': ['吐槽'],
    'ldl': ['笑哭'],
    'ldm': ['表情包'],
    'mdc': ['日常'],
    'spc': ['黏人'],
    'tsy': ['踩到尾巴'],
    'ltp': ['丢东西'],
    'og': ['主人'],
    'lr': ['晚回'],
    'atc': ['嘴硬'],
    'dfb': ['躲'],
    'cfs': ['鱼'],
    'bb': ['绝了'],
    'wp': ['周末'],
    'bon': ['忙不忙'],
    'sc': ['在干嘛'],
    'ts': ['踩尾巴'],
    'it': ['胡思乱想'],
}

def get_name_hints(name):
    out = []
    parts = name.split('_')
    for p in parts:
        if p in NAME_HINT:
            out.extend(NAME_HINT[p])
    return out

# 手工兜底: 这些 route 的 utterance/keyword 高度重合, 给出合理补充
MANUAL_OVERRIDE = {
    'rps_promotion_001': ['职场喜事', '工资条', '老板宣布', '惊喜'],
    'nt_nt_yueguang_010': ['夜晚散步', '安静', '路边', '看月'],
    'gm_gm_daye_004': ['游戏分路', '英雄联盟', '王者', '位置'],
    'bda_blush_006': ['脸红反应', '害羞', '主人撩', '小声'],
    'cc_cc_paipai_005': ['安抚动作', '陪伴', '蹭蹭', '安慰'],
    'wwr_weijin_001': ['穿戴提醒', '出门叮嘱', '别冻', '加件衣', '保暖', '提醒主人'],
    'bda_blush_003': ['躲闪反应', '脸红', '害羞', '不敢看', '躲开主人', '反撩'],
    'to_lazy_004': ['吐槽主人', '宅家', '不动', '葛优'],
    'hpb_back_pat_001': ['求拍拍', '安抚', '蹭蹭', '撒娇'],
    'ex_kuxiao_001': ['尴尬场景', '无奈', '强颜', '表情'],
    'lcx_haoxiao_001': ['笑哭混合', '边笑边哭', '想哭', '笑岔'],
    'se_se_huijia_010': ['街头偶遇', '路上', '街边', '路过', '巷子', '碰见'],
    'nt_yueliang_001': ['夜晚话题', '抬头看', '夜空', '看月'],
    'xs_xiaodemiao_006': ['笑梗表情', '笑死meme', '太魔性', '梗图'],
    'ldl_lol_005': ['笑出眼泪', '表情包', '边笑边哭', '太搞笑'],
    'atc_zuiying_001': ['反套路冷', '装冷淡', '别扭', '吐槽'],
    'cfs_tuna_001': ['猫想吃鱼', '撒娇要', '主人喂', '猫粮', '小鱼干', '罐头'],
    'bb_juewei_004': ['蚌埠住', '梗回应', '太牛', '夸张'],
    'wp_lazy_weekend_011': ['周末计划', '躺尸', '宅家', '葛优瘫'],
    'chc_crv_001': ['想吃巧克', '甜品瘾', '馋嘴', '零食'],
    'spc_cling_005': ['撒娇黏人', '腻歪', '不撒手', '黏'],
    'tsy_xiongshou_001': ['踩到尾巴', '失手', '呜呜', '心疼'],
    'mem_xiaosi_001': ['随机笑梗', '哈哈梗', '梗图', '搞笑'],
    'mof_outfit_001': ['吐槽穿搭', '审美差', '丑装', '土味'],
    'sc_what_doing_002': ['查岗', '问候', '在吗', '有空'],
    'it_yiwei_001': ['睡前胡思', '失眠思绪', '辗转', '夜深'],
    'toc_ai_zenma_001': ['吐槽主人', '幼稚行为', '坏习惯', '嫌弃'],
    'vml_voicetotext_002': ['语音文字', '听不懂', '懒得听', '请打字', '微信语音', '看不懂'],
    'rcs_goodbye_chat_012': ['结束聊天', '下线', '告别', '回头聊'],
    'sp_im_good_002': ['求夸奖', '我厉害', '求表扬', '夸夸'],
    'ev_dianti_001': ['等电梯久', '楼下', '上下楼', '电梯口'],
    'bus_wait_006': ['等公交', '站台', '挤车', '通勤'],
    'oc_tudi_001': ['吐槽土味', '油腻像', '糟糕穿搭', '吐槽'],
    'ldl_lol_006': ['离谱场景', '太奇葩', '震惊', '魔幻'],
    'brb_belly_001': ['求摸肚子', '撒娇', '翻肚', '猫式'],
    'hhg_huoxiao_009': ['失控笑', '笑岔', '笑炸', '止不住', '咯咯', '太魔性'],
    'wtc_humid_001': ['回南天潮', '阴雨闷', '黏腻', '南方'],
    'wwr_yufu_001': ['雨服提醒', '没带伞', '湿身', '保暖'],
    'pa_battery_low_002': ['手机电量', '关机', '充电器', '电量'],
    'hpb_cheek_004': ['捏脸蛋', '揉腮帮', '撒娇', '可爱'],
    'ew_kebei_001': ['可怜表情', '卖惨', '装可怜', '呜呜'],
    'cbr_rb_002': ['拒摸肚子', '不让碰', '炸毛', '反抗'],
    'rg_gig_005': ['笑点高低', '冷笑话', '梗', '不好笑', 'get 到', '怪笑点'],
    'te_xswl_010': ['xswl 表情', '笑死缩写', '网络梗', '弹幕'],
    'wm_huizhi_001': ['吐槽会议', '无聊会', '摸鱼', '加班'],
    'eh_haha_004': ['搞笑段子', '抖音梗', '哈哈哈表情', '弹幕笑', '段子手', '回复神'],
    'rps_confess_001': ['表白惊喜', '突如其来', '心动', '害羞'],
    'srn_cold_arriving_006': ['打喷嚏', '流鼻涕', '生病', '吃药', '多喝水', '头晕'],
    'dw_zaiganma_008': ['躺平状态', '床上', '懒得动', '葛优瘫'],
    'lc_lc_chuannuan_003': ['关心保暖', '加件', '别着凉', '叮嘱'],
    'ms_depressed_010': ['情绪起伏', '没动力', '丧', '抑郁感', '心情差', '消沉'],
    'to_to_guhuzi_002': ['吐槽胡渣', '没刮胡', '邋遢', '主人形象'],
    'hmt_midautumn_001': ['团圆', '过节', '赏月夜', '月饼节', '节日祝福', '吃月饼'],
    'cff_chl_001': ['续杯', '坐着歇', '咖啡香', '提神', '美式', '拿铁'],
    'whb_hug_005': ['高难度抱', '想被抱', '撒娇', '抱起来', '主人来', '小公主'],
    'hhg_xiaopen_003': ['笑喷场景', '笑岔气', '失控', '太搞笑'],
    'hpr_qinqin_001': ['求亲', '撒娇', '亲一口', '主人来', '甜甜', '猫亲'],
    'er_dotdotdot_011': ['无语表情', '三个点', '欲言又止', '沉默', '点点点', '无奈'],
    'tmf_tired_face_001': ['萎靡状态', '丧脸', '疲惫', '空洞眼神', '没力气', '熬'],
    'rw_rai_001': ['雨天心情', '听雨声', '窗外', '安静'],
    'hps_hng_010': ['哼声拉脸', '生气表情', '傲娇', '鼓腮帮', '嘟嘴', '气'],
    'cd_brush_fur_001': ['梳毛日常', '撸猫', '抚摸', '理毛', '梳子', '掉毛'],
    'nc_loss_001': ['心情低落', '需要安慰', '想被抱', '丢失感', '空落', '心空'],
    'wf_yugan_008': ['钓鱼想吃', '海鲜', '渔市', '想吃鱼'],
    'dln_dreamed_001': ['做梦聊', '梦境', '昨晚', '醒来'],
    'ld_lazy_meal_003': ['不想做饭', '凑合', '随便', '外卖', '泡面', '懒人餐'],
    'msg_cat_treat_001': ['猫零食', '夜宵时间', '想吃喵', '小鱼干', '猫罐头', '半夜馋'],
    'tf_zhuren_001': ['吐槽长胖', '增重', '体重', '减肥'],
    'cst_tr_002': ['冷处理', '装高冷', '不在乎', '不理你', '冷战中', '沉默'],
    'smf_tuxxiang_001': ['微妙情绪', '没缘由', '心头一紧', '突然', '微感受', '需要陪'],
    'anc_anni_003': ['节日纪念', '浪漫', '甜蜜日', '纪念日', '陪伴日', '送花'],
    'fl_qinqin_004': ['反撩亲亲', '主动亲', '主人羞', '反过来撩', '甜', '害羞'],
    'kps_basic_001': ['亲亲基础', '撒娇要', '主人来一个', '黏人', '甜', '蹭'],
    'uf_jiwoshi_001': ['鞋袜湿', '没带伞', '雨天惨', '走路不便', '换鞋', '吹脚'],
    'og_pang_005': ['长胖了', '主人胖', '吐槽主人', '减重失败', '又长肉', '增重'],
    'wwr_jacket_007': ['穿羽绒服吗', '冬装', '保暖外套', '加厚衣', '冬天穿', '叮嘱'],
    'qc_meanof_001': ['好奇问', '不懂', '什么梗', '解释一下', '术语', '听不懂'],
    'sp_chuan_001': ['辣到流泪', '辣得哭', '吃辣场景', '川菜馆', '麻辣味', '辣椒'],
    'wc_sock_001': ['加厚袜', '保暖脚', '关心脚冷', '叮嘱穿', '冬日袜', '脚凉'],
    'dye_share_food_001': ['抢食', '蹭一口', '撒娇要', '分饭', '猫式讨食', '凑过去'],
    'cr_yundong_001': ['关心运动', '健身房', '出汗', '别懒', '叮嘱锻炼', '动起来'],
    'ayt_check_006': ['查岗夜', '半夜叫醒', '关心睡眠', '深夜', '查睡没', '关心入睡'],
}

stats = {'auto': 0, 'fallback': 0}
results = []
for t in d:
    name = t['name']
    keywords = set(t['keywords'])
    intent = t.get('intent','')

    own_tokens = extract_tokens(t['file'])
    own_words = get_words(own_tokens)

    # sib_words: 从 sib file token 翻译的主题词 (用于第 0 轮过滤)
    sib_words = set()
    for s in t['siblings']:
        for tk in extract_tokens(s['topic_hint']+'.yaml'):
            if tk in THEME_MAP:
                for w in THEME_MAP[tk]:
                    sib_words.add(w)

    # sib_utt_set: sibling first_utterance 完整 2-5 字段 (用于第二轮严格过滤)
    sib_utt_set = set()
    for s in t['siblings']:
        fu = re.sub(r'\{[^}]+\}', '', s.get('first_utterance',''))
        for m in re.finditer(r'[一-鿿]+', fu):
            seg = m.group(0)
            if 2 <= len(seg) <= 5:
                sib_utt_set.add(seg)

    def _in_kw_strict(w):
        # 严格: 完全相等才视为重复
        return w in keywords

    def _in_kw_loose(w):
        # 宽松: 双向子串视为重复
        for k in keywords:
            if w == k or (len(w)>=2 and w in k) or (len(k)>=2 and k in w):
                return True
        return False

    _in_kw = _in_kw_strict

    seen = set()
    ctx = []
    # 第 0 轮: own_words 严格过滤(对完整段宽松,允许 keyword 子串)
    for w in own_words:
        if _in_kw_strict(w) or w in sib_words or w in seen:
            continue
        seen.add(w)
        ctx.append(w)

    # name hint
    for w in get_name_hints(name):
        if _in_kw_strict(w) or w in sib_words or w in seen:
            continue
        seen.add(w)
        ctx.append(w)

    # 第一轮 utt: 完整段, 严格过滤 (跟 keyword 完全相等 或 跟 sib_utt 完全相等才 skip)
    if len(ctx) < 4:
        for w in extract_utt_full_segs(t['utterances']):
            if _in_kw_strict(w) or w in sib_utt_set or w in seen:
                continue
            if len(w) < 2:
                continue
            seen.add(w)
            ctx.append(w)
            if len(ctx) >= 6:
                break

    # 第三轮: 滑窗 fallback, 严格过滤
    if len(ctx) < 4:
        for w in extract_utt_words(t['utterances']):
            if _in_kw_strict(w) or w in sib_utt_set or w in seen:
                continue
            if len(w) < 2:
                continue
            seen.add(w)
            ctx.append(w)
            if len(ctx) >= 5:
                break

    # 还不够再上 intent 填充 (最后兜底)
    if len(ctx) < 4 and intent in INTENT_FILL:
        for w in INTENT_FILL[intent]:
            if _in_kw_strict(w) or w in sib_words or w in seen:
                continue
            seen.add(w)
            ctx.append(w)
            if len(ctx) >= 5:
                break

    # 清理 ctx: 去掉 BAD_FRAGMENTS (常见无意义字尾词)
    def _is_garbage(w):
        return w in BAD_FRAGMENTS
    ctx = [w for w in ctx if not _is_garbage(w)]
    ctx = ctx[:6]

    # 手工兜底: manual ctx 只过滤 == keyword 完全相等
    if name in MANUAL_OVERRIDE:
        manual = MANUAL_OVERRIDE[name]
        merged = []
        mseen = set()
        for w in ctx:
            if w in mseen: continue
            mseen.add(w); merged.append(w)
        # 去掉填充词
        merged = [w for w in merged if w not in ('哎呀', '嘿嘿', '推荐', '怎么', '什么', '好累', '崩了', '烦死')]
        mseen = set(merged)
        for w in manual:
            if w in mseen: continue
            if w in keywords: continue  # 完全相等才 skip
            mseen.add(w); merged.append(w)
        ctx = merged[:6]

    if len(ctx) >= 4:
        stats['auto'] += 1
    else:
        stats['fallback'] += 1

    results.append({'name': name, 'ctx': ctx, '_len': len(ctx), '_file': t['file'], '_intent': intent, '_kw': list(keywords), '_utts': t['utterances'], '_sibs': [s['topic_hint'] for s in t['siblings']]})

print('auto enough:', stats['auto'])
print('fallback (too short):', stats['fallback'])
short = [r for r in results if r['_len'] < 4]
print('short routes:', len(short))
for r in short[:30]:
    print(r['name'], '|', r['_file'], '|', 'kw=', r['_kw'], '|', 'utt=', r['_utts'], '|', 'sibs=', r['_sibs'], '|', 'ctx=', r['ctx'])

json.dump(results, open('e:/VC/Catty/data/_b0_results.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)
