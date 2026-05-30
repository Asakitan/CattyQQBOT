# -*- coding: utf-8 -*-
"""
给 batch2 (220 routes) 生成 disambiguate_context.
策略:
  1. 从 file (topic_hint) 提取主题词根 -> 主题语境词
  2. 根据 intent 加情绪词/查询词
  3. 收集 siblings 的 topic_hint 词根作为禁词
  4. 避开本 route 自身 keywords (重复词无 disambig 力)
"""
import json
import re

# ---- 通用主题词典 (基于 file 名 token -> 中文 ctx 候选) ----
# 每个 token 给 6-10 个候选, 后面再裁剪并过滤
TOPIC_LEXICON = {
    # 日常生活
    "lucky": ["运气好", "今天走运", "好运", "顺利", "天选"],
    "streak": ["连胜", "一波", "节节胜", "连续", "状态好"],
    "win": ["赢了", "胜利", "拿下", "carry", "MVP"],
    "777": ["开挂", "炸裂", "稳赢", "上分"],
    # 超市/购物
    "supermarket": ["超市", "推车", "货架", "结账"],
    "run": ["逛", "走一圈", "扫货", "购物"],
    "shopping": ["购物", "买买买", "下单", "剁手"],
    "cart": ["购物车", "凑单", "下单", "结算"],
    "haul": ["开箱", "战利品", "晒货", "抢到"],
    "discount": ["优惠", "促销", "折扣", "便宜"],
    # 天气
    "wear": ["穿衣", "搭配", "衣服", "外套"],
    "warm": ["保暖", "添衣", "暖和", "捂"],
    "warmth": ["保暖", "暖和", "热乎"],
    "remind": ["提醒", "记得", "别忘", "叮嘱"],
    "weather": ["天气", "气温", "气候", "预报"],
    "rain": ["下雨", "雨水", "雨天", "雨声"],
    "rainy": ["阴雨", "湿漉漉", "雨天", "潮湿"],
    "sudden": ["突然", "猝不及防", "忽然"],
    "caught": ["被困", "淋到", "撞上"],
    "wet": ["湿透", "淋湿", "湿哒哒"],
    "umbrella": ["伞", "撑伞", "雨伞", "遮雨"],
    "forgot": ["忘带", "忘了", "落下"],
    "windy": ["大风", "风刮", "刮风", "风大"],
    "wind": ["风", "刮风", "风吹"],
    "snow": ["下雪", "雪天", "雪花", "雪景"],
    "cold": ["冷", "降温", "凉", "寒"],
    "hot": ["热", "高温", "暑气"],
    "cloudy": ["阴天", "多云", "灰蒙蒙"],
    "sunny": ["晴天", "大太阳", "阳光"],
    "fog": ["雾", "雾蒙蒙", "起雾"],
    "thunder": ["打雷", "雷声", "雷电"],
    # 能量/饮料
    "energy": ["精力", "能量", "提神", "续命"],
    "drink": ["喝", "饮料", "灌", "喉咙"],
    "thirsty": ["渴", "口渴", "缺水", "嗓子干"],
    "water": ["水", "白开水", "矿泉水", "喝水"],
    "beg": ["求", "讨", "央求", "撒娇要"],
    "coffee": ["咖啡", "拿铁", "美式", "醒神"],
    "tea": ["茶", "奶茶", "泡茶"],
    # 夜晚
    "night": ["夜晚", "晚上", "深夜", "夜里"],
    "late": ["熬夜", "晚归", "深更", "通宵"],
    "moon": ["月亮", "月色", "月光", "弯月"],
    "star": ["星星", "星空", "繁星"],
    "dream": ["梦", "做梦", "梦境", "好梦"],
    "dreaming": ["做梦", "梦里", "梦境"],
    "sleep": ["睡觉", "睡眠", "入睡", "床上"],
    "sleepy": ["困", "犯困", "瞌睡", "迷糊"],
    "yawn": ["打哈欠", "哈欠", "困意"],
    "yet": ["还没", "睡了吗", "睡没"],
    "check": ["问候", "查岗", "看看"],
    "topic": ["话题", "聊点", "闲聊"],
    # 早晨
    "morning": ["早晨", "清晨", "一大早", "早起"],
    "wake": ["醒来", "起床", "睁眼"],
    "grumpy": ["起床气", "起不来", "烦躁"],
    "zaoan": ["早安", "早上好", "问早"],
    "early": ["很早", "天没亮", "凌晨"],
    "greet": ["打招呼", "问候", "招呼"],
    # 周末
    "weekend": ["周末", "休息日", "放假"],
    "lazy": ["懒", "躺平", "宅", "不想动"],
    "sunday": ["周日", "礼拜天", "周末末"],
    "monday": ["周一", "礼拜一", "上班日"],
    "holiday": ["假期", "节假日", "放假"],
    "vibe": ["氛围", "心情", "感觉"],
    "alone": ["独自", "一个人", "孤单"],
    # 看书学习
    "study": ["学习", "复习", "看书", "刷题"],
    "cram": ["突击", "临阵", "通宵复习", "抱佛脚"],
    "exam": ["考试", "测验", "试卷", "答题"],
    "panic": ["慌", "崩了", "心慌", "抓狂"],
    "anxious": ["焦虑", "紧张", "不安", "忐忑"],
    "anxiety": ["焦虑", "紧张", "压力大"],
    "burnout": ["燃尽", "疲惫", "倦怠", "扛不住"],
    "vent": ["吐槽", "发泄", "诉苦"],
    "scene": ["场景", "氛围", "环境"],
    # 工作摸鱼
    "work": ["工作", "上班", "打工"],
    "fish": ["摸鱼", "划水", "偷懒"],
    "idle": ["闲", "无事", "空闲"],
    "chat": ["聊天", "唠嗑", "扯", "唠"],
    # 拥抱安慰
    "hug": ["抱抱", "拥抱", "搂", "抱紧"],
    "comfort": ["安慰", "哄", "抚慰", "暖心"],
    "seek": ["求", "寻求", "找", "想要"],
    "ask": ["问", "求", "讨", "讨要"],
    "need": ["需要", "想要", "缺"],
    # 害羞
    "blush": ["脸红", "害羞", "羞涩", "耳热"],
    "avoid": ["躲", "回避", "不敢看"],
    "shy": ["害羞", "腼腆", "羞答答"],
    # 情绪
    "pouty": ["噘嘴", "鼓脸", "嘟囔"],
    "sulking": ["生闷气", "闹脾气", "甩脸"],
    "sulk": ["生气", "气鼓鼓", "闹"],
    "angry": ["生气", "气炸", "火大"],
    "sad": ["难过", "丧", "低落"],
    "cry": ["哭", "眼泪", "抽泣"],
    # 鼻塞打喷嚏
    "sneeze": ["打喷嚏", "喷嚏", "鼻子痒"],
    "runny": ["流鼻涕", "鼻涕", "鼻塞"],
    "nose": ["鼻子", "擤鼻", "鼻孔"],
    "cough": ["咳嗽", "咳", "呛"],
    "sick": ["生病", "不舒服", "发烧"],
    "fever": ["发烧", "烧起来", "体温高"],
    # 毯子
    "blanket": ["毯子", "被子", "盖被"],
    "burrito": ["卷成卷", "卷一团", "包住"],
    "cozy": ["舒服", "暖呼呼", "窝着"],
    # 街上
    "street": ["街上", "马路", "走在外面"],
    "encounter": ["偶遇", "撞见", "碰到"],
    # 秘密
    "secret": ["秘密", "悄悄", "小秘密"],
    "share": ["分享", "告诉", "说说"],
    "whisper": ["耳语", "悄悄话", "小声"],
    # 节日
    "festival": ["节日", "过节", "庆祝"],
    "birthday": ["生日", "蛋糕", "庆生"],
    "wish": ["祝福", "心愿", "许愿"],
    "moment": ["瞬间", "时刻", "片刻"],
    # 倒计时
    "looking": ["期待", "盼", "心心念念"],
    "forward": ["期待", "盼着", "想要"],
    "U7": ["倒数", "期盼", "等不及"],
    "NUM": ["数字", "数到", "倒数"],
    # 肚子
    "stomach": ["肚子", "胃", "肚肚"],
    "growl": ["咕咕叫", "饿叫", "响"],
    "hungry": ["饿", "饥饿", "肚饿"],
    "lunch": ["午饭", "午餐", "中午吃"],
    "noon": ["中午", "正午", "午休"],
    "break": ["休息", "歇", "暂停"],
    # 表情包
    "emoji": ["表情包", "斗图", "表情"],
    "war": ["大战", "对决", "斗"],
    "battle": ["对战", "PK", "互斗"],
    "tinghao": ["停战", "投降"],
    "winflag": ["胜利旗", "我赢", "举旗"],
    # 撒娇
    "flirt": ["撩", "撒娇", "暧昧"],
    "back": ["反撩", "回击", "还击"],
    "dodge": ["躲开", "闪避", "避开"],
    "giving": ["放弃", "认输", "投降"],
    "up": ["放弃", "撂挑子"],
    "X8": ["认输", "服了"],
    # 红包
    "red": ["红包", "派红包"],
    "packet": ["红包", "封"],
    "joy": ["开心", "高兴", "乐"],
    # 双十一
    "double11": ["双十一", "购物节", "11.11"],
    # 装忙
    "pretend": ["假装", "装作", "故意"],
    "busy": ["忙", "忙不过来", "忙碌"],
    # 终于回复
    "ptb": ["回消息", "已读", "回复慢"],
    # 思考
    "thinking": ["思考", "想", "琢磨"],
    "aloud": ["自言自语", "念叨", "嘴上说"],
    "random": ["随机", "突发", "脑洞"],
    "thought": ["想法", "念头", "脑回路"],
    # 计划/拖延
    "procrastination": ["拖延", "拖", "推迟"],
    "putoff": ["拖延", "押后", "推后"],
    "planning": ["计划", "安排", "规划"],
    # 比赛分数
    "grades": ["成绩", "分数", "排名"],
    "score": ["分", "得分", "分数"],
    "low": ["低", "差", "拉"],
    # 时间
    "time": ["时间", "几点", "时辰"],
    "what": ["什么", "啥"],
    # 月圆
    "round": ["圆", "圆圆", "满"],
    "pretty": ["漂亮", "好看", "美"],
    # 风
    "day": ["一天", "今天", "白天"],
    # 暖
    "dress": ["穿", "穿搭", "穿衣"],
    "care": ["关心", "照顾", "操心"],
    "clothing": ["衣服", "穿着", "服装"],
    # 窗
    "window": ["窗", "窗外", "窗户"],
    # 隐藏
    "hidden": ["隐藏", "藏", "私藏"],
    # 内嵌兜底
    # ---- 新增 (补全 batch2) ----
    "addict": ["上瘾", "戒不掉", "成瘾"],
    "after": ["之后", "事后", "完事"],
    "alarm": ["闹钟", "响铃", "叫醒"],
    "animal": ["小动物", "动物", "兽兽"],
    "anniversary": ["纪念日", "周年", "纪念"],
    "anti": ["反", "抵抗", "对抗"],
    "are": ["在吗", "在不"],
    "awake": ["清醒", "睡不着", "醒着"],
    "away": ["离开", "走开", "不在"],
    "awsl": ["awsl", "可爱炸", "啊我死了"],
    "baobao": ["抱抱", "搂搂", "求抱"],
    "battery": ["电量", "电池", "没电"],
    "bedtime": ["睡前", "上床", "钻被窝"],
    "belly": ["小肚子", "肚皮", "腹部"],
    "bengbu": ["蚌埠", "崩溃", "绷不住"],
    "bored": ["无聊", "无趣", "没意思"],
    "broke": ["没钱", "穷", "破产"],
    "burrow": ["缩", "钻进", "躲进"],
    "caress": ["抚摸", "轻抚", "摸摸"],
    "catty": ["猫猫", "笨猫", "猫"],
    "ccc": ["嘁嘁嘁", "啧啧"],
    "chatter": ["闲扯", "唠叨", "话痨"],
    "clean": ["擦干净", "清洁", "洗"],
    "clingy": ["黏人", "粘", "缠"],
    "cooler": ["凉快", "降温", "冷感"],
    "complain": ["抱怨", "诉苦", "吐槽"],
    "compliment": ["夸", "夸赞", "表扬"],
    "commute": ["通勤", "上下班", "挤车"],
    "company": ["陪", "陪伴", "陪着"],
    "crave": ["馋", "想吃", "好想"],
    "craving": ["馋", "嘴馋", "想要"],
    "cry": ["哭", "抽泣", "掉泪"],
    "crying": ["哭哭", "哭着", "眼泪"],
    "daily": ["日常", "每天", "平日"],
    "daze": ["发呆", "走神", "出神"],
    "dead": ["死了", "凉了", "歇菜"],
    "devil": ["恶魔", "小恶魔", "皮"],
    "die": ["死", "完", "凉"],
    "died": ["死了", "暴毙"],
    "dinner": ["晚饭", "晚餐", "吃晚"],
    "doing": ["在做", "干啥", "在干"],
    "drinking": ["喝着", "猛灌", "灌"],
    "dry": ["干", "干燥", "缺水"],
    "eat": ["吃", "干饭", "吃饭"],
    "emotion": ["情绪", "心情", "心境"],
    "event": ["事件", "活动", "凑热闹"],
    "excuse": ["借口", "理由", "找辞"],
    "exercise": ["运动", "锻炼", "动一动"],
    "face": ["脸", "脸蛋", "面部"],
    "fail": ["失败", "翻车", "GG"],
    "family": ["家人", "家里", "亲人"],
    "fandom": ["追星", "粉圈", "嗑"],
    "fanliao": ["反撩", "反击撩", "回撩"],
    "far": ["远", "好远", "距离"],
    "fat": ["胖", "长肉", "圆"],
    "feel": ["感觉", "觉得", "感受"],
    "first": ["第一", "初次", "首次"],
    "floor": ["地板", "地上", "倒地"],
    "food": ["食物", "吃的", "美食"],
    "for": [],  # 介词跳过
    "free": ["空闲", "自由", "没事"],
    "full": ["饱", "满", "撑"],
    "gain": ["长", "增", "涨"],
    "game": ["游戏", "开黑", "对局"],
    "getting": ["渐渐", "开始", "变得"],
    "giggle": ["咯咯笑", "傻笑", "偷笑"],
    "goodnight": ["晚安", "睡安", "安"],
    "gossip": ["八卦", "瓜", "聊八"],
    "greetings": ["问候", "打招呼", "招呼"],
    "grievance": ["委屈", "受气", "憋"],
    "grumble": ["嘟囔", "嘀咕", "碎碎念"],
    "gym": ["健身房", "撸铁", "举铁"],
    "haha": ["哈哈", "笑死", "笑喷"],
    "hands": ["手", "小手", "双手"],
    "head": ["头", "脑袋", "脑壳"],
    "health": ["健康", "身体", "养生"],
    "heihei": ["嘿嘿", "贼笑", "坏笑"],
    "hiccup": ["打嗝", "嗝", "停不住嗝"],
    "hng": ["哼", "哼哼", "傲娇"],
    "home": ["家", "家里", "回家"],
    "homesick": ["想家", "思乡", "念家"],
    "hurt": ["痛", "受伤", "疼"],
    "idea": ["主意", "点子", "想法"],
    "if": ["如果", "万一", "假如"],
    "ignoring": ["不理", "无视", "冷落"],
    "in": [],
    "insomnia": ["失眠", "睡不着", "翻来覆去"],
    "itchy": ["痒", "发痒", "痒痒"],
    "jealousy": ["吃醋", "嫉妒", "酸"],
    "juejue": ["绝绝子", "绝", "无敌"],
    "juejuezi": ["绝绝子", "绝", "yyds"],
    "killing": ["杀", "灭", "干掉"],
    "kiss": ["亲", "亲亲", "kiss"],
    "kk": ["kk", "凯凯"],
    "kkk": ["哈哈哈", "笑出声"],
    "laugh": ["笑", "笑出来", "大笑"],
    "left": ["剩下", "走了", "离开"],
    "lick": ["舔", "舔毛", "舔爪"],
    "lie": ["躺", "趴", "瘫"],
    "listener": ["倾听者", "听众", "陪听"],
    "lll": ["呜呜", "Lv", "等级"],
    "lost": ["丢", "迷路", "失"],
    "master": ["主人", "主子", "饲主"],
    "meal": ["饭", "一顿", "三餐"],
    "meme": ["梗", "段子", "网络梗"],
    "meow": ["喵", "喵喵", "猫叫"],
    "micro": ["微", "迷你", "小小"],
    "midnight": ["半夜", "凌晨", "深夜"],
    "milk": ["牛奶", "奶", "热牛奶"],
    "minor": ["小", "轻微", "小小"],
    "mood": ["心情", "情绪", "状态"],
    "morn": ["早上", "晨", "清早"],
    "neck": ["脖子", "颈", "落枕"],
    "neko": ["猫娘", "兽耳", "猫"],
    "no": [],
    "now": ["现在", "此刻", "当下"],
    "office": ["办公室", "工位", "公司"],
    "or": [],
    "outage": ["停电", "断电", "停服"],
    "outfit": ["穿搭", "搭配", "装扮"],
    "overdose": ["过量", "嗑多", "上头"],
    "overload": ["超载", "爆表", "过载"],
    "overthink": ["想多", "脑补", "胡思"],
    "owner": ["主人", "主子", "饲主"],
    "packet": ["红包", "封"],
    "pat": ["拍", "摸头", "轻拍"],
    "pester": ["缠", "黏", "腻"],
    "pet": ["撸", "摸", "宠"],
    "petting": ["摸摸", "撸毛", "顺毛"],
    "phone": ["手机", "电话", "屏幕"],
    "pillow": ["枕头", "抱枕", "靠枕"],
    "ping": ["延迟", "卡", "网延"],
    "pout": ["噘嘴", "嘟嘴", "撇嘴"],
    "pouting": ["噘嘴", "鼓嘴", "嘟"],
    "power": ["电", "能量", "电力"],
    "praised": ["被夸", "受表扬", "被赞"],
    "proper": ["规规矩矩", "好好的", "正经"],
    "quit": ["弃", "戒", "退出"],
    "redpacket": ["红包", "派红包"],
    "relation": ["关系", "感情", "羁绊"],
    "request": ["请求", "求", "要"],
    "rest": ["休息", "歇会", "缓"],
    "return": ["回来", "归来", "返回"],
    "road": ["路上", "路", "马路"],
    "roll": ["滚", "翻", "打滚"],
    "routine": ["日常", "常规", "习惯"],
    "runaway": ["逃跑", "跑掉", "溜"],
    "sajiao": ["撒娇", "嗲", "蹭"],
    "salty": ["咸", "酸", "阴阳"],
    "screen": ["屏幕", "屏", "盯屏"],
    "season": ["季节", "换季", "时节"],
    "session": ["局", "回合", "场"],
    "shiver": ["发抖", "抖", "颤"],
    "shop": ["店", "逛店", "购"],
    "shoveler": ["铲屎官", "饲主"],
    "sigh": ["叹气", "唉声", "叹"],
    "silent": ["沉默", "没声", "无言"],
    "silly": ["傻", "呆", "笨笨"],
    "skip": ["翘", "跳过", "缺"],
    "slack": ["摸鱼", "划水", "偷懒"],
    "smile": ["微笑", "笑脸", "笑"],
    "snack": ["零食", "小吃", "嘴"],
    "snacks": ["零食", "小零食", "膨化"],
    "snooze": ["小睡", "再睡", "回笼"],
    "sob": ["啜泣", "抽泣", "呜咽"],
    "something": ["什么", "某事", "啥"],
    "spoil": ["宠", "惯", "溺爱"],
    "starter": ["开端", "破冰", "开场"],
    "status": ["状态", "近况", "现状"],
    "stiff": ["僵", "酸僵", "僵硬"],
    "still": ["还", "依然", "仍"],
    "struggle": ["挣扎", "硬撑", "撑"],
    "sudden": ["突然", "猝不及防", "忽然"],
    "sulky": ["闷闷", "闹脾气", "拗"],
    "surprise": ["惊喜", "惊讶", "意外"],
    "sweater": ["毛衣", "针织", "厚衣"],
    "sweet": ["甜", "甜蜜", "撒糖"],
    "takeout": ["外卖", "点餐", "送餐"],
    "talk": ["聊", "讲", "说话"],
    "tantrum": ["闹脾气", "耍赖", "撒泼"],
    "tease": ["撩拨", "逗", "戏弄"],
    "text": ["文字", "消息", "发字"],
    "there": ["那里", "那边"],
    "throat": ["嗓子", "喉咙", "嗓门"],
    "throwing": ["扔", "甩", "抛"],
    "tickle": ["挠痒", "挠", "酥麻"],
    "til": ["直到", "到"],
    "tired": ["累", "疲惫", "乏"],
    "today": ["今天", "本日"],
    "traffic": ["堵车", "交通", "路况"],
    "treat": ["待遇", "对待", "待"],
    "treats": ["零嘴", "好吃的", "小食"],
    "tsundere": ["傲娇", "嘴硬", "毒舌"],
    "vibes": ["氛围", "感觉", "气场"],
    "visit": ["拜访", "串门", "来访"],
    "wakeup": ["起床", "醒来", "睁眼"],
    "wallet": ["钱包", "瘪了", "钱"],
    "want": ["想要", "渴望", "求"],
    "wash": ["洗", "清洗", "冲"],
    "weight": ["体重", "公斤", "秤"],
    "where": ["哪里", "在哪"],
    "wifi": ["wifi", "网", "信号"],
    "word": ["字", "词", "话"],
    "wuwu": ["呜呜", "委屈", "哭腔"],
    "www": ["www", "草", "笑死"],
    "xxx": ["xxx", "悄悄"],
    "you": ["你", "主人"],
    "zhu": ["主子", "主人"],
    "broke": ["穷", "没钱", "破产"],
    "burnout": ["燃尽", "倦怠", "扛不住"],
    "class": ["上课", "课堂", "课"],
}

# ---- intent 补充词 ----
INTENT_EXTRAS = {
    "complaint": ["累", "烦", "崩", "呜呜", "委屈", "受不了", "扛不住"],
    "playful": ["嘿嘿", "嘻嘻", "玩闹", "皮一下"],
    "question": ["怎么", "咋整", "推荐", "求问"],
    "greeting": ["招呼", "问好", "Hi"],
}

# ---- file 名 -> token list ----
RE_TOKEN = re.compile(r"[A-Za-z][A-Za-z]+")  # 提取英文词

def file_tokens(fname: str):
    """从 file 名提取英文 token (去掉数字前缀和 .yaml)"""
    stem = re.sub(r"\.ya?ml$", "", fname)
    # 也保留开头数字前缀作为 NUM token
    toks = []
    for m in RE_TOKEN.findall(stem):
        toks.append(m.lower())
    return toks

def sibling_forbidden_words(siblings, own_file):
    """从 sibling 的 topic_hint 提取 token. 同 file (同 topic) sibling 不算 -> 不进禁词.

    返回的 tokens 仅来自跨 file sibling (即不同主题路由), 这样同 file 内的
    主题词不会被无谓屏蔽."""
    own_stem = re.sub(r"\.ya?ml$", "", own_file).lower()
    forb_tokens = set()
    forb_words = set()
    for sib in siblings:
        th = sib.get("topic_hint", "")
        if th.lower().rstrip(".yaml") == own_stem:
            # 同 file sibling -> 跳过 (主题词应保留)
            continue
        for t in file_tokens(th):
            forb_tokens.add(t)
        fu = sib.get("first_utterance", "")
        forb_words.add(fu)
    return forb_tokens, forb_words

def expand_topic_words(tokens):
    """根据 token 列表展开候选 ctx 词"""
    out = []
    for t in tokens:
        if t in TOPIC_LEXICON:
            for w in TOPIC_LEXICON[t]:
                if w not in out:
                    out.append(w)
    return out

def gen_ctx(route):
    name = route["name"]
    fname = route["file"]
    kws = set(route.get("keywords", []))
    utters = route.get("utterances", [])
    intent = route.get("intent", "playful")
    siblings = route.get("siblings", [])

    own_tokens = file_tokens(fname)
    sib_tokens, sib_full = sibling_forbidden_words(siblings, fname)

    # 1. own 主题词
    own_topic_words = expand_topic_words(own_tokens)
    # 2. sib 主题词 (用于过滤共享词)
    sib_topic_words = set(expand_topic_words(list(sib_tokens)))

    def is_keyword_overlap(word):
        # 任务定义: "不能跟 keywords 重复" -> 完全相等才算重复
        # 子串关系反而能强化主题命中, 不当禁用
        return word in kws

    candidates = []
    seen = set()

    def try_add(w):
        if not w or w in seen:
            return False
        if not (2 <= len(w) <= 4):
            return False
        if is_keyword_overlap(w):
            return False
        if w in sib_topic_words:
            return False
        # 必须含中文(允许英文 token like awsl/yyds)
        if not (re.search(r"[一-鿿]", w) or re.match(r"^[A-Za-z]{2,4}$", w)):
            return False
        seen.add(w)
        candidates.append(w)
        return True

    # 1) own topic 词典优先
    for w in own_topic_words:
        try_add(w)

    # 2) 从 keywords 提取子词 (但不完全等于 keyword)
    #    例如 "惊喜礼物" -> "惊喜", "考砸" -> 跳过太短
    for kw in route.get("keywords", []):
        if not kw or len(kw) < 3:
            continue
        # 抽 2-3 字子串, 但不含 kw 自己
        for L in (2, 3):
            for i in range(len(kw) - L + 1):
                seg = kw[i:i+L]
                if seg == kw:
                    continue
                try_add(seg)

    # 3) 从 utterances (去掉 {user_addr}) 抽 2-3 字
    for u in utters:
        uu = re.sub(r"\{[^}]+\}", "", u).strip()
        if not uu:
            continue
        # 去掉英文/数字, 取中文片段
        for chunk in re.findall(r"[一-鿿]+", uu):
            for L in (2, 3):
                for i in range(len(chunk) - L + 1):
                    try_add(chunk[i:i+L])

    # 4) intent 补充
    for w in INTENT_EXTRAS.get(intent, []):
        try_add(w)

    # 5) 通用兜底
    GENERIC = ["心情", "状态", "感觉", "今天", "刚刚", "现在", "好像", "嘿",
               "话题", "情景", "气氛", "状况", "唉", "啊这", "笑死", "顶不住"]
    for w in GENERIC:
        if len(candidates) >= 6:
            break
        try_add(w)

    # 截 4-6
    ctx = candidates[:6]
    if len(ctx) < 4:
        # 极端兜底 (理论上不会到)
        for w in ["氛围", "感觉", "事情", "情况"]:
            if w not in ctx:
                ctx.append(w)
            if len(ctx) >= 4:
                break
    return ctx[:6]


def main():
    data = json.load(open(r"e:/VC/Catty/data/disambig_round2_batch2.json", "r", encoding="utf-8"))
    results = []
    for r in data:
        ctx = gen_ctx(r)
        results.append({"name": r["name"], "ctx": ctx})
    out = {"results": results}
    json.dump(out, open(r"e:/VC/Catty/data/disambig_round2_batch2_out.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    # 简短验证
    print("total:", len(results))
    short = [x for x in results if len(x["ctx"]) < 4]
    print("len<4:", len(short))
    long_ = [x for x in results if len(x["ctx"]) > 6]
    print("len>6:", len(long_))
    empty_w = [x for x in results if any(not w for w in x["ctx"])]
    print("empty word:", len(empty_w))
    # 抽样
    for r in results[:5]:
        print(r)


if __name__ == "__main__":
    main()
