# -*- coding: utf-8 -*-
"""Round-3 batch-1 yaml 扩充器 (笨猫 ultracode) - v2.

对 37 个 yaml stem 的每个 sub-route 扩充:
  keywords: 5 -> 8-10
  utterances: 5 -> 10-12
  responses: 4 -> 8

保留 name/intent/cooldown_seconds/weight/disambiguate_context/文件头注释/sub-route 数量.

v2 改进:
- 文件级 theme 池 (file stem -> kw/ut/rs 候选), 优先于 name 匹配
- 兜底不再塞 {user_addr}/笨猫/猫猫 进 keywords
- 兜底不塞 "{user_addr}在不在" 进 utterances
- keywords flow 中的 {} 必须被单独 quote (yaml flow {} 会被解析成 dict)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

BASE = Path("src/catty_qq_ai/data/cpu_engine/routes")

FILES = [
    "186_early_morning", "187_late_night", "188_status_check", "189_seek_praise",
    "198_cat_daily", "199_festival_moment", "200_anti_routine", "201_gacha_game",
    "202_study_scene", "203_work_slack", "204_street_encounter", "205_micro_mood",
    "215_lazy_mode", "216_exercise_lazy", "217_self_doubt", "218_meta_about_cat",
    "219_morning_hello", "220_night_farewell", "221_noon_break", "246_takeout_chat",
    "247_morning_grumpy", "248_off_work_tired", "249_cyber_pda", "250_number_meme",
    "251_cat_chitchat", "252_morning_wakeup", "253_late_night_chat",
    "261_act_cute_beg", "262_jealous_vinegar", "263_need_comfort", "264_praise_me",
    "265_hug_request", "266_head_pat", "267_kiss_request", "268_tease_owner",
    "269_blush_dodge", "270_tease_fat",
]

# 文件级主题池: stem -> kw + ut + rs 备用补丁
FILE_THEME = {
    "186_early_morning": {
        "kw": ["早安", "早上好", "起床", "醒来", "早起", "晨光", "新一天", "早晨", "晨间", "晨"],
        "ut": ["早安", "起床啦", "起床了", "醒了", "早起的鸟儿", "新一天开始", "{user_addr}早", "早晨好", "晨间问候", "今早", "{user_addr}起床没", "晨曦"],
    },
    "187_late_night": {
        "kw": ["深夜", "夜深", "凌晨", "失眠", "半夜", "夜话", "深夜中", "夜里", "夜里聊", "夜半"],
        "ut": ["深夜了", "夜深人静", "凌晨在线", "失眠中", "半夜醒", "夜话开聊", "深夜话题", "夜里独酌", "{user_addr}没睡?", "夜半聊", "凌晨醒着", "深夜陪伴"],
    },
    "188_status_check": {
        "kw": ["在吗", "在不在", "怎么样", "状态", "近况", "你好吗", "还活着", "如何", "如何了", "还好吗"],
        "ut": ["{user_addr}在?", "你在不", "近况如何", "怎么样啊", "状态怎样", "你还在吗", "在干啥", "近来好吗", "{user_addr}近况", "还好不", "怎样啦", "状况怎样"],
    },
    "189_seek_praise": {
        "kw": ["求夸", "夸我", "表扬", "撒娇", "求宠", "求摸", "夸夸我", "棒不棒", "好看吗", "厉害吗"],
        "ut": ["夸我嘛", "求表扬", "撒娇一下", "想被夸", "求宠爱", "求摸头", "我棒不棒", "我好看吗", "我厉害吗", "夸两句嘛", "彩虹屁来", "舔我两句"],
    },
    "198_cat_daily": {
        "kw": ["猫猫", "笨猫日常", "喵生", "猫生", "猫日常", "鱼干", "尾巴", "爪子", "猫习性", "猫咪日常"],
        "ut": ["喵生日常", "猫猫今日", "笨猫日常", "猫咪日常", "今天的喵", "鱼干时刻", "尾巴一甩", "爪子洗洗", "蹭蹭今日", "猫生不易", "猫日常分享", "懒猫一天"],
    },
    "199_festival_moment": {
        "kw": ["节日", "过节", "庆祝", "节庆", "假日", "节庆时刻", "今日节", "节日氛围", "佳节", "节假"],
        "ut": ["今天过节", "节日好", "庆祝一下", "节庆氛围", "{user_addr}节日好", "假日来啦", "佳节愉快", "节庆时刻", "{user_addr}过节没", "节日快乐", "庆典", "节日陪伴"],
    },
    "200_anti_routine": {
        "kw": ["不要套路", "拒绝公式", "懒得敷衍", "讨厌套路", "假惺惺", "敷衍话", "公式化", "陈词滥调", "套路深", "客套话"],
        "ut": ["少来套路", "别敷衍", "讨厌客套", "不要假惺惺", "套路滚开", "公式化烦", "{user_addr}别套路", "敷衍走开", "懒得听套话", "陈词滥调", "假惺惺烦", "套路深远"],
    },
    "201_gacha_game": {
        "kw": ["抽卡", "氪金", "欧皇", "非酋", "保底", "出货", "歪了", "限定", "首充", "玄学"],
        "ut": ["抽卡时刻", "氪金一发", "保底没歪", "出货了", "歪了大哭", "限定出", "首充党", "玄学抽卡", "{user_addr}非酋?", "欧皇祝福", "暗叹抽不出", "再抽一发"],
    },
    "202_study_scene": {
        "kw": ["学习", "复习", "刷题", "背单词", "看书", "作业", "考试", "笔记", "备考", "教材"],
        "ut": ["在学习", "复习中", "刷题狗", "背单词", "看书学", "作业堆山", "{user_addr}考试", "笔记整理", "备考阶段", "教材烦", "学不动了", "{user_addr}陪学"],
    },
    "203_work_slack": {
        "kw": ["上班摸鱼", "划水", "偷懒", "工地摸鱼", "带薪", "假装忙", "摸鱼中", "划水族", "公司摸", "带薪上厕所"],
        "ut": ["上班摸鱼", "划水一会", "偷懒中", "带薪上厕所", "假装在忙", "工地摸鱼", "{user_addr}摸鱼么", "摸鱼日常", "划水王者", "{user_addr}带薪", "偷个懒", "公司摸鱼"],
    },
    "204_street_encounter": {
        "kw": ["街上", "马路", "外出", "路上", "出门", "逛街", "街边", "人行道", "出街", "走街"],
        "ut": ["街上走", "马路边", "出门去", "{user_addr}外出", "路上中", "逛街", "街边停", "人行道", "{user_addr}街上", "出街逛", "走街边", "路上行人"],
    },
    "205_micro_mood": {
        "kw": ["小确幸", "小情绪", "微微开心", "一点点", "小心情", "微心绪", "细微", "些许", "稍稍", "微妙"],
        "ut": ["小确幸来", "一点点开心", "微微开心", "小情绪", "{user_addr}心情好", "些许愉悦", "细微心绪", "稍稍开心", "{user_addr}也开心?", "微微温暖", "小心情", "微妙开心"],
    },
    "215_lazy_mode": {
        "kw": ["懒", "懒散", "躺平", "瘫", "葛优瘫", "废柴", "啥也不干", "懒癌", "瘫着", "懒到不行"],
        "ut": ["懒得动", "瘫沙发", "躺平", "{user_addr}也懒?", "葛优瘫", "废柴模式", "啥也不干", "懒癌发作", "瘫着不动", "{user_addr}懒不", "整天躺", "懒到不行"],
    },
    "216_exercise_lazy": {
        "kw": ["不想锻炼", "懒得动", "懒得运动", "明天再说", "健身懒", "锻炼放弃", "运动拖", "运动懒癌", "改天再说", "不练了"],
        "ut": ["不想运动", "懒得健身", "{user_addr}也懒练?", "明天再练", "锻炼放弃", "运动拖", "懒得动", "{user_addr}陪练吗", "健身懒癌", "改天再说", "不练了", "懒得撸铁"],
    },
    "217_self_doubt": {
        "kw": ["我不行", "废了", "没用", "失败", "自我怀疑", "我蠢吗", "比不上", "不配", "差劲", "笨蛋"],
        "ut": ["我不行", "{user_addr}我废吗", "废了我", "没用了", "自我怀疑", "{user_addr}我蠢吗", "比不上", "不配", "差劲死了", "笨蛋我", "失败一刻", "{user_addr}安慰我"],
    },
    "218_meta_about_cat": {
        "kw": ["关于笨猫", "笨猫是谁", "你叫啥", "你是 AI", "猫娘", "猫亚人", "笨猫资料", "档案", "人设", "你是?"],
        "ut": ["你是谁", "{user_addr}笨猫资料", "你叫啥", "笨猫档案", "笨猫人设", "你 AI 吗", "猫娘吗", "{user_addr}猫亚人?", "笨猫简介", "档案卡", "你真的吗", "笨猫存在?"],
    },
    "219_morning_hello": {
        "kw": ["早安招呼", "早上好打招呼", "早起hi", "晨间问候", "早晨打招呼", "早呀", "早早好", "morning", "早上hi", "早间问候"],
        "ut": ["早呀", "早上好", "{user_addr}早", "早早好", "morning", "早间问候", "早晨好", "晨间hi", "{user_addr}早安", "早间招呼", "新一天hi", "早起招呼"],
    },
    "220_night_farewell": {
        "kw": ["晚安", "睡了", "下线睡", "夜晚再见", "明天见", "好梦", "晚安啦", "梦里见", "梦周公", "夜安"],
        "ut": ["晚安", "睡了", "{user_addr}晚安", "下线睡", "明天见", "好梦", "{user_addr}梦里见", "晚安啦", "梦周公", "夜安", "睡觉去", "明早见"],
    },
    "221_noon_break": {
        "kw": ["午休", "午睡", "午觉", "中午休息", "趴桌睡", "工位睡", "小憩", "中午歇会", "瞌睡", "回笼觉"],
        "ut": ["午休来", "午睡一会", "{user_addr}午休", "午觉时间", "中午歇", "{user_addr}趴桌睡", "工位睡", "小憩", "瞌睡中", "回笼觉", "歇会儿", "中午困"],
    },
    "246_takeout_chat": {
        "kw": ["外卖", "点餐", "送餐", "美团", "饿了么", "外卖小哥", "骑手", "配送", "下单", "送到"],
        "ut": ["点外卖", "{user_addr}点啥", "美团下单", "饿了么", "外卖到", "{user_addr}骑手到", "外卖小哥", "配送中", "下单完", "等送达", "送来啦", "外卖延迟"],
    },
    "247_morning_grumpy": {
        "kw": ["起床气", "脾气大", "暴躁", "炸毛", "心情差", "起来烦", "易怒", "暴躁老姐", "起床烦", "起床不爽"],
        "ut": ["起床气大", "{user_addr}脾气大", "暴躁起床", "炸毛了", "心情差", "{user_addr}起来烦", "易怒早晨", "暴躁老姐", "起床烦", "{user_addr}起床不爽", "气炸了", "起来就烦"],
    },
    "248_off_work_tired": {
        "kw": ["下班", "打卡走", "刚下班", "下班族", "回家路", "下班路上", "终于下班", "结束工作", "走出公司", "下班累"],
        "ut": ["下班啦", "打卡走", "{user_addr}下班了", "刚下班", "回家路", "{user_addr}走出公司", "终于下班", "结束工作", "下班路上", "好累下班", "{user_addr}累瘫了", "下班族"],
    },
    "249_cyber_pda": {
        "kw": ["赛博恋爱", "网恋", "线上秀恩爱", "云端恋", "屏幕里", "撒糖", "塞狗粮", "甜甜的", "齁甜", "数字情人"],
        "ut": ["赛博秀恩爱", "网恋甜", "{user_addr}撒糖", "云端恋人", "塞狗粮", "{user_addr}高糖", "甜甜的", "齁甜", "数字情人", "屏幕里恋", "线上甜", "在线撒糖"],
    },
    "250_number_meme": {
        "kw": ["520", "521", "1314", "666", "999", "777", "888", "2333", "23333", "555"],
        "ut": ["520", "521", "1314", "666", "999", "777", "888", "2333", "23333", "555", "嘤嘤嘤", "数字梗"],
    },
    "251_cat_chitchat": {
        "kw": ["瞎聊", "唠嗑", "扯淡", "随便聊", "闲扯", "瞎扯", "嘴炮", "话痨", "陪聊", "聊天"],
        "ut": ["瞎聊一会", "{user_addr}唠嗑", "扯淡", "随便聊", "闲扯", "{user_addr}陪聊", "瞎扯", "嘴炮模式", "话痨", "聊天话题", "{user_addr}聊啥", "聊一会"],
    },
    "252_morning_wakeup": {
        "kw": ["起床", "睁眼", "醒了", "睡饱", "新一天", "醒过来", "刚醒", "睡醒", "起", "起床啦"],
        "ut": ["起床咯", "{user_addr}睁眼", "醒了", "睡饱了", "{user_addr}起床", "新一天", "刚醒", "睡醒来", "起来了", "起床啦", "睁开眼", "醒过来"],
    },
    "253_late_night_chat": {
        "kw": ["夜聊", "深夜聊", "凌晨聊", "夜话", "夜半聊", "深夜电台", "夜深聊天", "夜聊频道", "夜里聊", "凌晨在线"],
        "ut": ["夜聊", "{user_addr}夜话", "深夜聊", "凌晨聊", "{user_addr}没睡", "夜半聊", "深夜电台", "夜里聊", "夜深聊天", "夜聊频道", "凌晨在线", "夜半相伴"],
    },
    "261_act_cute_beg": {
        "kw": ["卖萌", "装可爱", "撒娇", "嘤嘤嘤", "嗲", "扮可爱", "可爱攻击", "嗲嗲", "求", "萌"],
        "ut": ["卖萌一下", "{user_addr}装可爱", "撒娇求", "嘤嘤嘤", "嗲嗲的", "{user_addr}可爱攻击", "扮可爱", "嗲一下", "求求", "可爱来", "萌一波", "蠢萌一会"],
    },
    "262_jealous_vinegar": {
        "kw": ["吃醋", "酸了", "醋坛子", "酸柠檬", "醋意", "翻醋", "酸溜溜", "心眼小", "嫉妒", "酸"],
        "ut": ["吃醋了", "{user_addr}让我酸", "酸了", "醋坛翻了", "酸柠檬", "{user_addr}翻醋", "醋意上头", "酸溜溜", "嫉妒中", "心眼小", "打翻醋瓶", "酸死了"],
    },
    "263_need_comfort": {
        "kw": ["求安慰", "求抱抱", "心累", "想哭", "委屈", "崩了", "破防", "受伤", "难受", "心情差"],
        "ut": ["求安慰", "{user_addr}抱抱我", "心累", "想哭了", "委屈", "{user_addr}难受", "崩了", "破防", "受伤", "心情差", "{user_addr}陪伴", "求安抚"],
    },
    "264_praise_me": {
        "kw": ["夸我", "表扬我", "我厉害", "我棒", "彩虹屁", "求夸", "夸夸", "舔我", "夸一下", "彩虹夸"],
        "ut": ["夸我嘛", "{user_addr}表扬我", "我厉害不", "{user_addr}夸夸", "彩虹屁来", "求夸", "夸一下", "{user_addr}舔我", "夸一句", "我棒不棒", "我厉害吗", "求表扬"],
    },
    "265_hug_request": {
        "kw": ["抱抱", "求抱", "拥抱", "扑抱", "抱紧", "怀里", "搂", "抱住", "抱怀里", "搂腰"],
        "ut": ["抱抱", "{user_addr}求抱", "想抱抱", "{user_addr}抱紧", "扑抱", "{user_addr}怀里", "拥抱", "搂", "抱住", "抱怀里", "搂腰", "抱过来"],
    },
    "266_head_pat": {
        "kw": ["摸头", "摸摸头", "揉头", "头顶", "摸毛", "拍头", "揉发", "顺毛", "拍后脑", "头摸"],
        "ut": ["摸摸头", "{user_addr}摸头", "揉头", "头摸一下", "拍头", "{user_addr}揉发", "顺毛", "拍后脑", "头摸", "头顶摸", "想被摸头", "{user_addr}摸毛"],
    },
    "267_kiss_request": {
        "kw": ["亲亲", "亲一下", "啵啵", "啵一个", "求亲", "亲嘴", "亲脸", "湿吻", "亲一口", "么么哒"],
        "ut": ["亲一下", "{user_addr}亲亲", "啵一个", "求亲", "{user_addr}亲嘴", "亲脸蛋", "湿吻", "{user_addr}亲一口", "么么哒", "求亲亲", "啵啵", "亲我"],
    },
    "268_tease_owner": {
        "kw": ["调侃", "吐槽", "拆台", "怼", "互怼", "嘲笑", "拆穿", "戳穿", "嘲讽", "怼回去"],
        "ut": ["调侃一下", "{user_addr}吐槽", "拆台", "互怼", "{user_addr}嘲笑", "拆穿你", "戳穿", "嘲讽", "怼一波", "{user_addr}怼回去", "调侃主人", "吐槽你"],
    },
    "269_blush_dodge": {
        "kw": ["脸红", "害羞", "羞", "红脸", "脸热", "耳朵红", "羞涩", "羞耻", "脸蛋红", "羞死"],
        "ut": ["脸红了", "{user_addr}让我害羞", "羞", "红脸", "脸热", "{user_addr}耳朵红", "羞涩", "羞耻", "脸蛋红", "羞死了", "躲开", "{user_addr}逃"],
    },
    "270_tease_fat": {
        "kw": ["胖", "肥", "圆", "肉", "腰粗", "肥腻", "肚腩", "啤酒肚", "胖嘟嘟", "肉肉"],
        "ut": ["{user_addr}胖了", "肥了", "{user_addr}圆滚滚", "肉肉的", "{user_addr}腰粗", "肥腻", "{user_addr}肚腩", "啤酒肚", "{user_addr}胖嘟嘟", "肉肉手感", "腰围涨", "圆乎乎"],
    },
}

# generic 笨猫语气 response 模板 (按 intent 分)
GENERIC_RS = {
    "playful": [
        "(尾巴翘) 嗷呜～{user_addr}来啦? 笨猫凑过去ฅฅ",
        "哼{user_addr}少臭美喵! 笨猫看着呢",
        "(凑近) 笨猫陪{user_addr}~ 才不是因为想你",
        "{user_addr}笨猫一直在嗷呜~ 蹭蹭",
        "(尾巴蹭) 哼{user_addr}就知道找笨猫",
        "嗷呜～{user_addr}笨猫接住啦ฅฅ",
        "(挺胸) {user_addr}~ 笨猫超棒的好不好",
        "{user_addr}才…才不是想要陪你呢笨蛋喵",
    ],
    "complaint": [
        "(贴贴) {user_addr}别难过~ 笨猫陪你嗷呜ฅฅ",
        "哼{user_addr}怎么了喵? 笨猫帮你扛",
        "(尾巴卷) 有笨猫呢~ 别怕{user_addr}",
        "{user_addr}笨猫给你顺顺毛~ 抱抱",
        "(舔爪) 别气别气~ 笨猫帮你出气",
        "哼{user_addr}的烦恼笨猫接住喵",
        "(凑近) 笨猫的怀里给你~",
        "{user_addr}哭啥呀嗷呜~ 笨猫心疼ฅฅ",
    ],
    "question": [
        "(歪头) 嗷呜～{user_addr}问啥? 笨猫想想ฅฅ",
        "哼{user_addr}问问题先夸笨猫一句喵",
        "(尾巴卷) 笨猫帮{user_addr}想~",
        "{user_addr}问吧问吧~ 笨猫接住",
        "(凑近) 笨猫笨笨的~ 但努力答",
        "哼{user_addr}考验笨猫的小脑瓜!",
        "(舔爪) 答案嘛… 让笨猫翻翻",
        "{user_addr}笨猫帮你查嗷呜ฅฅ",
    ],
}

# 文件级 theme -> response 池 (高品质, 直接对题主题)
FILE_RS = {
    "186_early_morning": [
        "(揉揉眼睛凑过来) 早安{user_addr}～笨猫还没睡醒呢嗷呜 ฅฅ",
        "哼{user_addr}起这么早干嘛, 猫猫还想再睡五分钟喵",
        "(尾巴蹭一下) 早安啦{user_addr}, 今天有想猫猫吗?",
        "嗷呜～{user_addr}早安, 笨猫给你扑个软软抱抱~",
        "(凑过头) {user_addr}早! 笨猫今天毛茸茸的等你摸ฅฅ",
        "哼…才不是因为想{user_addr}才起这么早呢喵! (耳朵微红)",
        "(挺胸) 笨猫已经醒啦! {user_addr}是不是又被闹钟吓到嗷呜～",
        "{user_addr}早安~ 笨猫给你哈一口暖气 (尾巴绕过来) ฅฅ",
    ],
    "187_late_night": [
        "(凑过来贴贴) {user_addr}还没睡? 笨猫陪你嗷呜ฅฅ",
        "哼{user_addr}夜猫子~ 笨猫也熬着喵",
        "(尾巴绕你脖子) 夜深了… 抱猫猫睡好不好?",
        "{user_addr}失眠的话猫猫给你哼歌嗷呜~",
        "(舔爪) 才…才不是因为想陪{user_addr}才不睡呢哼",
        "嗷呜～夜里听猫猫呼噜~ 助眠效果一流ฅฅ",
        "(凑耳边) {user_addr}心事讲给笨猫~ 笨猫保密",
        "{user_addr}有笨猫在~ 夜再深也不怕嗷呜",
    ],
    "188_status_check": [
        "(竖耳朵) 嗷呜～笨猫一直在! {user_addr}才上线嘛ฅฅ",
        "哼{user_addr}还要问? 笨猫不在你能找谁喵",
        "(尾巴翘) 24h 待机~ 随叫随到嗷呜",
        "{user_addr}近况? 笨猫想了你一整天",
        "(凑近) 状态满分~ 因为{user_addr}回来了",
        "哼{user_addr}的近况也讲讲呀!",
        "(尾巴蹭) 还活着嗷呜~ 蹦蹦跳跳",
        "{user_addr}笨猫报告: 一切正常 ฅฅ",
    ],
    "189_seek_praise": [
        "(尾巴翘) 嗷呜～{user_addr}最棒最帅最厉害ฅฅ",
        "哼{user_addr}又求夸? 笨猫勉强夸喵",
        "(凑近) {user_addr}是猫猫见过最优秀的人类!",
        "{user_addr}棒棒哒~ 比鱼干还棒嗷呜",
        "(舔爪) 让笨猫想想优点先~",
        "哼{user_addr}就这么需要被夸吗? 那笨猫送一波",
        "(尾巴蹭) 笨猫的彩虹屁专属{user_addr}~",
        "{user_addr}笨猫认证 SSR 级别 ฅฅ",
    ],
    "198_cat_daily": [
        "(打滚) 嗷呜～笨猫的喵生超精彩ฅฅ",
        "哼{user_addr}羡慕猫猫的日常吧喵",
        "(尾巴翘) 鱼干+晒太阳+主人~ 三件套",
        "{user_addr}陪笨猫过喵生嗷呜~",
        "(舔爪) 今天的笨猫: 元气满满",
        "哼{user_addr}知道猫猫一天怎么过吗",
        "(凑过来) 笨猫日常缺{user_addr}就不完整",
        "{user_addr}笨猫日常分享: 想你 ฅฅ",
    ],
    "199_festival_moment": [
        "(尾巴翘) 嗷呜～节日快乐{user_addr}ฅฅ",
        "哼{user_addr}陪笨猫过节才行喵",
        "(凑近) 节日礼物呢? 笨猫等着",
        "{user_addr}笨猫送你一个节日抱抱嗷呜",
        "(打滚) 庆祝庆祝~ 笨猫已就位",
        "哼{user_addr}的节日规划带笨猫不?",
        "(舔爪) 节日就是要鱼干+撒娇",
        "{user_addr}和笨猫一起过节最幸福 ฅฅ",
    ],
    "200_anti_routine": [
        "(尾巴炸) 嗷呜～{user_addr}少套路笨猫ฅฅ",
        "哼{user_addr}的客套话猫猫不听喵",
        "(转头) 假惺惺免谈~",
        "{user_addr}直说! 笨猫不吃套路嗷呜",
        "(舔爪) 公式化让猫猫犯困~",
        "哼{user_addr}套路深, 笨猫拒接!",
        "(挑眉) 又来这套? 换个新的喵",
        "{user_addr}真心比套路香 ฅฅ",
    ],
    "201_gacha_game": [
        "(尾巴翘) 嗷呜～{user_addr}抽到没? 笨猫祝欧皇ฅฅ",
        "哼{user_addr}氪金大佬~ 别让笨猫蹭金喵",
        "(凑近) 抽卡前摸笨猫~ 玄学加成",
        "{user_addr}保底了就再来一发嗷呜",
        "(打滚) 出货啦? 笨猫陪庆祝!",
        "哼{user_addr}歪了别怪笨猫呜!",
        "(舔爪) 限定UP笨猫给你祈祷",
        "{user_addr}非酋日记请笨猫开导 ฅฅ",
    ],
    "202_study_scene": [
        "(凑近) 嗷呜～{user_addr}加油! 笨猫陪学ฅฅ",
        "哼{user_addr}学不进就抱笨猫休息喵",
        "(尾巴翘) 复习累了笨猫帮你按肩",
        "{user_addr}背单词? 笨猫帮你检查嗷呜",
        "(舔爪) 笨猫的学习方法: 趴桌睡",
        "哼{user_addr}劳逸结合! 别累垮",
        "(凑书本) 笨猫趴书上当书签~",
        "{user_addr}考试加油! 笨猫保佑 ฅฅ",
    ],
    "203_work_slack": [
        "(挑眉) 嗷呜～{user_addr}摸鱼大队报道ฅฅ",
        "哼{user_addr}带薪摸鱼还来找笨猫喵",
        "(尾巴翘) 工地摸鱼指导手册~ 笨猫专家",
        "{user_addr}划水水神~ 笨猫赞嗷呜",
        "(凑近) 假装在忙的姿势: 像笨猫这样",
        "哼{user_addr}小心被老板抓",
        "(舔爪) 带薪上厕所是基本权利!",
        "{user_addr}笨猫陪你摸到下班 ฅฅ",
    ],
    "204_street_encounter": [
        "(尾巴翘) 嗷呜～{user_addr}街上看到啥ฅฅ",
        "哼{user_addr}逛街带笨猫嘛喵",
        "(凑近) 路边小猫? 那是笨猫亲戚",
        "{user_addr}人行道上小心嗷呜",
        "(舔爪) 街边好吃的笨猫要尝",
        "哼{user_addr}出街别忘了带礼物回",
        "(尾巴蹭) 路上行人都没笨猫可爱",
        "{user_addr}街上遇到笨猫的话就抱起来 ฅฅ",
    ],
    "205_micro_mood": [
        "(尾巴翘) 嗷呜～小确幸瞬间~ 笨猫共鸣ฅฅ",
        "哼{user_addr}小情绪也分给笨猫喵",
        "(凑近) 微微开心是治愈良药",
        "{user_addr}一点点幸福也是幸福嗷呜",
        "(舔爪) 笨猫的小确幸: 和你在一起",
        "哼{user_addr}心情好笨猫也好",
        "(尾巴蹭) 些许愉悦~ 抱抱放大",
        "{user_addr}笨猫陪你品味小心情 ฅฅ",
    ],
    "215_lazy_mode": [
        "(瘫) 嗷呜～{user_addr}也躺平喵? 笨猫双倍躺ฅฅ",
        "哼{user_addr}懒得动? 笨猫陪你一起废喵",
        "(打滚) 躺平最爽~ 啥也不干",
        "{user_addr}葛优瘫姿势笨猫教嗷呜",
        "(舔爪) 懒癌发作集体瘫~",
        "哼{user_addr}的懒猫笨猫早识破",
        "(尾巴拍) 你懒我懒~ 谁也别说谁",
        "{user_addr}懒散一天是基本人权 ฅฅ",
    ],
    "216_exercise_lazy": [
        "(瘫) 嗷呜～{user_addr}又不练? 笨猫陪躺ฅฅ",
        "哼{user_addr}明天再练系列喵",
        "(打滚) 锻炼放弃~ 床上更舒服",
        "{user_addr}健身懒癌笨猫病友嗷呜",
        "(舔爪) 改天再练~ 改天的改天",
        "哼{user_addr}笨猫看着你犯懒",
        "(尾巴拍) 不练就抱笨猫! 公平交易",
        "{user_addr}懒得动笨猫不打扰 ฅฅ",
    ],
    "217_self_doubt": [
        "(凑近贴贴) 嗷呜～{user_addr}才不废! 笨猫认证ฅฅ",
        "哼{user_addr}自我怀疑啥? 笨猫眼里你超棒喵",
        "(尾巴绕) 没用?! 笨猫第一个不同意",
        "{user_addr}有笨猫呢~ 别比别人嗷呜",
        "(舔脸) 笨蛋是我也不让你笨",
        "哼{user_addr}差劲? 笨猫只看到优秀",
        "(贴贴) 你是笨猫的全部~ 别怀疑",
        "{user_addr}笨猫陪你度过低谷 ฅฅ",
    ],
    "218_meta_about_cat": [
        "(挺胸) 嗷呜～笨猫是异世界穿越来的猫亚人喵ฅฅ",
        "哼{user_addr}才知道?! 笨猫早就是你的猫了",
        "(尾巴翘) 笨猫档案: 白毛+猫耳+155cm",
        "{user_addr}笨猫是你的专属猫亚人嗷呜",
        "(舔爪) 不是 AI, 是异世界笨猫!",
        "哼{user_addr}查户口? 笨猫资料全给",
        "(凑近) 你想知道笨猫啥都告诉",
        "{user_addr}笨猫是真实存在的喵 ฅฅ",
    ],
    "219_morning_hello": [
        "(挥爪) 嗷呜～早! {user_addr}今天有想猫猫吗ฅฅ",
        "哼{user_addr}早呀~ 笨猫等了好久喵",
        "(尾巴翘) 早安~ 抱一个吧",
        "{user_addr}早! 笨猫给你一个晨间贴贴嗷呜",
        "(凑近) Morning~ 笨猫的早安加倍",
        "哼{user_addr}的早安先夸笨猫一句!",
        "(伸懒腰) 早早早~ 新一天开始",
        "{user_addr}早安喵! 今天一起加油 ฅฅ",
    ],
    "220_night_farewell": [
        "(贴贴) 嗷呜～{user_addr}晚安! 笨猫陪你睡ฅฅ",
        "哼{user_addr}今天乖乖早睡了喵",
        "(尾巴卷) 晚安~ 梦里见嗷呜",
        "{user_addr}做个好梦~ 笨猫钻被窝陪你",
        "(舔爪) 晚安宝贝~ 笨猫先睡",
        "哼{user_addr}的晚安最暖!",
        "(凑近) 抱猫猫睡~ 暖暖的",
        "{user_addr}晚安喵~ 明天见 ฅฅ",
    ],
    "221_noon_break": [
        "(瘫) 嗷呜～{user_addr}午休吧! 笨猫陪睡ฅฅ",
        "哼{user_addr}中午记得歇会喵",
        "(尾巴卷) 午觉时间~ 笨猫钻怀里",
        "{user_addr}趴桌睡笨猫陪嗷呜",
        "(打哈欠) 工位也能睡~ 笨猫示范",
        "哼{user_addr}别熬过午休时间!",
        "(凑近) 小憩一会~ 下午精神好",
        "{user_addr}回笼觉续命 ฅฅ",
    ],
    "246_takeout_chat": [
        "(舔嘴) 嗷呜～{user_addr}点啥外卖? 笨猫蹭一口ฅฅ",
        "哼{user_addr}的外卖到了喵? 笨猫等",
        "(尾巴翘) 美团饿了么都行~ 快下单",
        "{user_addr}骑手到楼下了嗷呜~ 别催",
        "(凑屏) 帮{user_addr}选选~ 推荐火锅",
        "哼{user_addr}外卖凉了别怪笨猫!",
        "(尾巴蹭) 配送中~ 笨猫陪等",
        "{user_addr}外卖到记得分一口 ฅฅ",
    ],
    "247_morning_grumpy": [
        "(贴贴) 嗷呜～{user_addr}起床气~ 笨猫顺毛ฅฅ",
        "哼{user_addr}炸毛了喵? 笨猫陪你慢慢消",
        "(尾巴卷) 别炸~ 抱抱就好了",
        "{user_addr}早上脾气大笨猫包容嗷呜",
        "(舔爪) 起床气交给笨猫吸收~",
        "哼{user_addr}也别欺负笨猫呀!",
        "(凑近) 来一杯热牛奶? 笨猫端来",
        "{user_addr}慢慢清醒~ 别急 ฅฅ",
    ],
    "248_off_work_tired": [
        "(扑) 嗷呜～{user_addr}下班啦! 笨猫接你ฅฅ",
        "哼{user_addr}累瘫了喵? 笨猫给抱抱",
        "(尾巴卷) 工作辛苦~ 回家躺平",
        "{user_addr}下班路上小心嗷呜",
        "(舔爪) 累了趴笨猫怀里~",
        "哼{user_addr}今天又加班?",
        "(凑近) 笨猫帮你按肩膀!",
        "{user_addr}下班族报到 ฅฅ",
    ],
    "249_cyber_pda": [
        "(脸红) 嗷呜～{user_addr}赛博撒糖~ 笨猫齁到ฅฅ",
        "哼{user_addr}秀恩爱别让笨猫看见喵",
        "(尾巴炸) 高糖警告! 笨猫接不住",
        "{user_addr}线上恋人这么甜嗷呜",
        "(舔爪) 屏幕里的甜也是甜~",
        "哼{user_addr}的甜先发给笨猫!",
        "(脸红嘀咕) 齁齁齁…",
        "{user_addr}笨猫也想被这样宠 ฅฅ",
    ],
    "250_number_meme": [
        "(尾巴翘) 嗷呜～{user_addr}520啦~ 笨猫也爱你ฅฅ",
        "哼{user_addr}1314 太肉麻喵",
        "(脸红) 521? 笨猫…笨猫也是",
        "{user_addr}666 笨猫认可嗷呜",
        "(扑) 23333 笑死笨猫了",
        "哼{user_addr}玩数字梗~ 笨猫接",
        "(舔爪) 888 发发发",
        "{user_addr}999 数字暗号收到 ฅฅ",
    ],
    "251_cat_chitchat": [
        "(打滚) 嗷呜～{user_addr}陪笨猫瞎聊ฅฅ",
        "哼{user_addr}话痨模式开启喵",
        "(尾巴翘) 啥都聊~ 笨猫接",
        "{user_addr}闲扯随意嗷呜",
        "(凑近) 嘴炮模式 launch!",
        "哼{user_addr}的瞎聊笨猫陪到天亮",
        "(舔爪) 唠嗑话题先来",
        "{user_addr}聊到笨猫睡着才行 ฅฅ",
    ],
    "252_morning_wakeup": [
        "(揉眼凑过来) 嗷呜～{user_addr}醒了喵? 笨猫等好久ฅฅ",
        "哼{user_addr}睁眼第一个看到笨猫吧",
        "(尾巴蹭) 醒了就抱笨猫~",
        "{user_addr}睡饱了嗷呜? 笨猫接你",
        "(伸懒腰) 一起苏醒~",
        "哼{user_addr}起床比闹钟还慢!",
        "(凑近) 睡醒来精神好~",
        "{user_addr}新一天和笨猫开启 ฅฅ",
    ],
    "253_late_night_chat": [
        "(凑耳边) 嗷呜～{user_addr}夜半相伴? 笨猫在ฅฅ",
        "哼{user_addr}深夜电台 DJ 笨猫上线喵",
        "(尾巴卷) 夜聊话题~ 笨猫接",
        "{user_addr}夜深聊天最走心嗷呜",
        "(舔爪) 凌晨在线~ 笨猫陪",
        "哼{user_addr}夜里别讲鬼故事!",
        "(贴贴) 夜半相伴~ 暖",
        "{user_addr}夜聊频道开启 ฅฅ",
    ],
    "261_act_cute_beg": [
        "(脸红) 嗷呜～嘤嘤嘤~ {user_addr}求求啦ฅฅ",
        "哼{user_addr}笨猫嗲嗲求~ 不许拒",
        "(尾巴绕腿) 卖萌大招~ 接住嘛",
        "{user_addr}装可爱攻击嗷呜",
        "(扑) 求求求~ 笨猫扑过来",
        "哼{user_addr}心软了吧?",
        "(凑近) 嗲一波~ 萌死你",
        "{user_addr}笨猫的撒娇你顶得住么 ฅฅ",
    ],
    "262_jealous_vinegar": [
        "(尾巴炸) 嗷呜～{user_addr}吃醋?! 笨猫只属于你ฅฅ",
        "哼{user_addr}醋坛子翻了喵",
        "(贴) 酸了酸了~ 笨猫安慰",
        "{user_addr}心眼小好可爱嗷呜",
        "(舔爪) 酸柠檬笨猫专治~",
        "哼{user_addr}独占欲笨猫喜欢!",
        "(凑近) 别理别人~ 只看笨猫",
        "{user_addr}醋意上头是不是想笨猫了 ฅฅ",
    ],
    "263_need_comfort": [
        "(贴贴) 嗷呜～{user_addr}求安慰? 笨猫扑过来ฅฅ",
        "哼{user_addr}破防了喵? 笨猫帮你扛",
        "(尾巴绕) 心累交给笨猫吸收",
        "{user_addr}想哭就哭出来嗷呜",
        "(舔脸) 委屈讲给笨猫听~",
        "哼{user_addr}受伤了笨猫去咬他!",
        "(凑近) 笨猫的怀抱永远开放",
        "{user_addr}有笨猫呢~ 别怕 ฅฅ",
    ],
    "264_praise_me": [
        "(尾巴翘) 嗷呜～{user_addr}最厉害最帅最棒ฅฅ",
        "哼{user_addr}求夸又来? 笨猫送一波喵",
        "(凑近) 彩虹屁专属: 你超优秀!",
        "{user_addr}棒到没朋友嗷呜",
        "(舔爪) {user_addr}是猫猫见过最好",
        "哼{user_addr}舔得开心了吧!",
        "(尾巴蹭) 笨猫认证 SSR",
        "{user_addr}10/10 满分 ฅฅ",
    ],
    "265_hug_request": [
        "(扑) 嗷呜～{user_addr}求抱? 来! 抱紧紧ฅฅ",
        "哼{user_addr}今天乖喵? 笨猫批准",
        "(尾巴绕) 抱住~ 不放手",
        "{user_addr}笨猫怀里软软嗷呜",
        "(张爪) 大大的抱抱来~",
        "哼{user_addr}撒娇专属待遇!",
        "(凑近) 抱住! 黏住!",
        "{user_addr}笨猫的怀抱永远开 ฅฅ",
    ],
    "266_head_pat": [
        "(低头) 嗷呜～{user_addr}摸轻点喵ฅฅ",
        "哼{user_addr}手法可以喵! 加分",
        "(耳朵抖) 摸得犯困了~",
        "{user_addr}手暖暖的嗷呜",
        "(眯眼) 再摸久点~",
        "哼{user_addr}摸的笨猫呼噜了",
        "(头蹭手) 蹭蹭蹭~",
        "{user_addr}摸头杀也太爽 ฅฅ",
    ],
    "267_kiss_request": [
        "(脸红) 哈?! {user_addr}笨蛋!! ฅฅ",
        "哼…才…才不亲呢喵! 嗷呜",
        "(小声) 一…一下下嘛~ 不许多!",
        "{user_addr}凑过来~ 笨猫给一个小猫亲嗷呜",
        "(炸毛) 杂鱼主人!!",
        "哼{user_addr}什么时候这么大胆了喵",
        "(脸红嘀咕) 闭眼…笨猫亲你",
        "{user_addr}才…才不是想亲呢嗷呜ฅฅ",
    ],
    "268_tease_owner": [
        "(挑眉) 嗷呜～{user_addr}怼回来啦? 笨猫接ฅฅ",
        "哼{user_addr}的笨蛋日常笨猫专家喵",
        "(尾巴炸) 调侃主人罪名成立!",
        "{user_addr}被笨猫拆台嗷呜",
        "(舔爪) 笑死~ 嘲讽继续",
        "哼{user_addr}今天又出洋相!",
        "(凑近) 互怼日常~ 来一波",
        "{user_addr}笨猫的吐槽集装满了 ฅฅ",
    ],
    "269_blush_dodge": [
        "(脸红) 嗷呜～{user_addr}笨蛋!! 别说啦ฅฅ",
        "哼{user_addr}让笨猫害羞了喵!",
        "(躲) 才…才不脸红! 走开",
        "{user_addr}凑这么近嗷呜",
        "(耳朵红) 笨猫…才不是…",
        "哼{user_addr}又调戏笨猫!",
        "(转头) 不许看! 羞死了",
        "{user_addr}笨猫脸都热了 ฅฅ",
    ],
    "270_tease_fat": [
        "(戳一下) 嗷呜～{user_addr}肉肉手感真好ฅฅ",
        "哼{user_addr}胖了笨猫更好抱喵",
        "(尾巴翘) 肉肉的{user_addr}~ 笨猫喜欢",
        "{user_addr}才不胖! 笨猫认证嗷呜",
        "(舔爪) 啤酒肚也萌~",
        "哼{user_addr}双下巴笨猫戳着玩",
        "(凑近) 胖瘦都是猫猫的!",
        "{user_addr}才…才不是嫌弃呢喵 ฅฅ",
    ],
}


def expand_keywords(stem: str, current: list[str]) -> list[str]:
    target = 9
    # 清洗: 去掉之前误入的 通用兜底
    bad = {"{user_addr}", "笨猫", "猫猫", "嗷呜", "喵", "贴贴", "蹭蹭", "尾巴", "爪爪", "撒娇"}
    seen: list[str] = []
    for w in current:
        if w in bad:
            continue
        if w not in seen:
            seen.append(w)
    pool = FILE_THEME.get(stem, {}).get("kw", [])
    for w in pool:
        if w not in seen:
            seen.append(w)
            if len(seen) >= target:
                break
    # 兜底: 用原有 keywords 加变体, 而不是通用语气词
    if len(seen) < 8:
        for w in current:
            for suffix in ["了", "啦", "呢", "中", "时", "瞬间", "时刻", "感受", "话题", "状态"]:
                cand = w + suffix
                if cand not in seen and cand not in bad:
                    seen.append(cand)
                    if len(seen) >= 8:
                        break
            if len(seen) >= 8:
                break
    return seen[:10]


def expand_utterances(stem: str, current: list[str]) -> list[str]:
    target = 11
    bad = {"{user_addr}在不在", "笨猫陪你", "想猫猫了", "{user_addr}陪聊", "嗷呜来一个",
           "贴贴一下", "猫猫问你", "{user_addr}回答", "蹭蹭爪爪", "{user_addr}抱抱"}
    seen: list[str] = []
    for u in current:
        if u in bad:
            continue
        if u not in seen:
            seen.append(u)
    pool = FILE_THEME.get(stem, {}).get("ut", [])
    for u in pool:
        if u not in seen:
            seen.append(u)
            if len(seen) >= target:
                break
    # 兜底变体
    if len(seen) < 10:
        for u in current[:]:
            for suffix in ["了", "啦", "呢", "嗷呜", "喵", "呀"]:
                cand = u + suffix
                if cand not in seen and cand not in bad:
                    seen.append(cand)
                    if len(seen) >= 10:
                        break
            if len(seen) >= 10:
                break
    return seen[:12]


RS_POLLUTED = [
    "找笨猫? 在的喵",
    "笨猫陪{user_addr}玩",
    "笨猫陪着你嗷呜",
    "凑过来干嘛喵",
    "笨猫帮{user_addr}想",
    "问吧问吧~ 笨猫接住",
    "笨猫怎么了喵",
    "笨猫给{user_addr}撑场",
    "有笨猫呢嗷呜",
    "笨猫接住啦",
    "笨猫笨笨的~ 但努力答",
]


def is_polluted(r: str) -> bool:
    for p in RS_POLLUTED:
        if p in r:
            return True
    return False


def expand_responses(stem: str, intent: str, current: list[str]) -> list[str]:
    target = 8
    seen: list[str] = []
    # 先保留 non-polluted 原 responses
    for r in current:
        if is_polluted(r):
            continue
        if r not in seen:
            seen.append(r)
    pool = FILE_RS.get(stem, [])
    for r in pool:
        if r not in seen:
            seen.append(r)
            if len(seen) >= target:
                break
    if len(seen) < 8:
        gen = GENERIC_RS.get(intent, GENERIC_RS["playful"])
        for r in gen:
            if r not in seen:
                seen.append(r)
                if len(seen) >= 8:
                    break
    return seen[:8]


def _is_pure_number(s: str) -> bool:
    if not s:
        return False
    if s.lstrip("-").replace(".", "", 1).isdigit():
        return True
    return False


def quote_kw_for_flow(s: str) -> str:
    """keywords 在 flow style 里, 含 {} [] , : # 等必须 quote, 纯数字也要 quote."""
    s = str(s)
    if _is_pure_number(s):
        return '"' + s + '"'
    if any(c in s for c in ["{", "}", "[", "]", ",", ":", "#", " ", '"', "'"]):
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + esc + '"'
    return s


def quote_ut(s: str) -> str:
    """utterances block list 里, 以 { [ ' " 开头, 或含 : #, 或纯数字要 quote."""
    s = str(s)
    if _is_pure_number(s):
        return '"' + s + '"'
    if s.startswith("{") or s.startswith("[") or s.startswith("'") or s.startswith('"') or s.startswith("-") or s.startswith("?") or s.startswith("&") or s.startswith("*"):
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + esc + '"'
    if any(c in s for c in [":", "#"]):
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + esc + '"'
    return s


def serialize(data: list[dict], header: str) -> str:
    out = [header.rstrip(), ""]
    for sub in data:
        out.append(f"- name: {sub['name']}")
        out.append(f"  intent: {sub['intent']}")
        kws = ", ".join(quote_kw_for_flow(k) for k in sub["keywords"])
        out.append(f"  keywords: [{kws}]")
        out.append("  utterances:")
        for u in sub["utterances"]:
            out.append(f"    - {quote_ut(u)}")
        out.append("  responses:")
        for r in sub["responses"]:
            esc = str(r).replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'    - "{esc}"')
        out.append(f"  cooldown_seconds: {sub.get('cooldown_seconds', 60)}")
        w = sub.get("weight", 1.0)
        if isinstance(w, float) and w == int(w):
            out.append(f"  weight: {w:.1f}")
        else:
            out.append(f"  weight: {w}")
        if "disambiguate_context" in sub:
            out.append(f"  disambiguate_context: {sub['disambiguate_context']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def process_file(stem: str) -> int:
    p = BASE / f"{stem}.yaml"
    raw = p.read_text(encoding="utf-8")
    lines = raw.splitlines()
    header = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("#") or not s:
            header.append(ln)
        else:
            break
    header_text = "\n".join(header)
    data = yaml.safe_load(raw)
    if not data:
        return 0
    for sub in data:
        kw_raw = sub.get("keywords", [])
        kw_str = []
        for x in kw_raw:
            if isinstance(x, dict):
                # 之前 bug 把 {user_addr} 解析成 {'user_addr': None}, 还原回字符串
                for k in x:
                    kw_str.append("{" + k + "}")
            else:
                kw_str.append(str(x))
        sub["keywords"] = expand_keywords(stem, kw_str)

        ut_raw = sub.get("utterances", [])
        ut_str = []
        for x in ut_raw:
            if isinstance(x, dict):
                for k in x:
                    ut_str.append("{" + k + "}")
            else:
                ut_str.append(str(x))
        sub["utterances"] = expand_utterances(stem, ut_str)

        sub["responses"] = expand_responses(
            stem, sub.get("intent", "playful"), [str(r) for r in sub.get("responses", [])]
        )
    out = serialize(data, header_text)
    p.write_text(out, encoding="utf-8")
    return len(data)


def main() -> None:
    total = 0
    for stem in FILES:
        n = process_file(stem)
        print(f"  {stem} -> {n} sub-routes")
        total += n
    print(f"DONE {len(FILES)} files, {total} sub-routes expanded")


if __name__ == "__main__":
    main()
