# -*- coding: utf-8 -*-
"""
Round-3 batch-18 expander (笨猫 cpu_engine routes)
Read each YAML under routes/, for every sub-route ensure:
  keywords  >= 8   (we target 9)
  utterances>= 10  (we target 11)
  responses >= 8   (we target 8)
Preserve: name / intent / cooldown_seconds / weight / disambiguate_context / header comment
"""
from __future__ import annotations
import re
from pathlib import Path
import random

ROUTES_DIR = Path(r"e:/VC/Catty/src/catty_qq_ai/data/cpu_engine/routes")

STEMS = [
    "889_sticker_battle","890_rainy_day_mood","891_first_snow_excite","892_thunder_scared",
    "893_windy_hair_mess","894_foggy_morning_blur","895_sunny_lazy_chat","896_humid_sticky_day",
    "897_cold_shiver_winter","898_heat_wave_die","899_breeze_cozy_feel","900_pocket_warm_hands",
    "901_blanket_steal_fight","902_socks_lost_pair","903_sweater_itchy_feel","904_hat_static_hair",
    "905_glove_finger_lost","906_scarf_choke_wrap","907_slippers_warm_step","908_pajama_cute_set",
    "909_hoodie_hide_inside","910_skirt_swing_breezy","911_button_loose_fall","912_zipper_stuck_jam",
    "913_snack_share_chat","914_study_burnout_vent","915_coffee_addict_chat","916_rainy_day_mood",
    "917_random_meme_laugh","918_pet_envy_chat","919_weekend_plan_chat","920_shopping_cart_chat",
    "921_payday_excited","922_broke_wallet_cry","923_gym_skip_excuse","924_diet_failure_chat",
    "925_late_for_work",
]

# --- 词汇池 (按 intent 选用) -----------------------------------------------
ACTIONS_PLAYFUL = [
    "(扑过来)","(尾巴翘)","(贴贴)","(凑过来)","(蹭蹭)","(歪头)","(尾巴卷)","(尾巴一甩)",
    "(凑头)","(眼睛亮)","(尾巴摇摇)","(耳朵竖)","(蹦跳)","(爪爪举)","(嘴角弯)"
]
ACTIONS_COMPLAINT = [
    "(瘫倒)","(叹气)","(耳朵蔫)","(尾巴拍)","(扁嘴)","(摇头)","(皱眉)","(尾巴搭)",
    "(蜷成团)","(揉眼睛)","(打哈欠)","(撇嘴)","(趴下)"
]
ACTIONS_QUESTION = [
    "(歪头)","(凑头)","(眨眼)","(尾巴问号)","(耳朵转)","(尾巴翘)","(摸下巴)","(凑过来)"
]
ACTIONS_GENERIC = ACTIONS_PLAYFUL + ACTIONS_QUESTION

CAT_TAIL = ["喵～","嗷呜～","ฅฅ","喵呜","喵","ฅ•ﻌ•ฅ"]
PRONOUNS = ["笨猫","猫猫","人家"]
TSUNDERE = ["哼～","才不是","又不是","哼"]
MOODS_OK = ["真不错","也不错","刚刚好","蛮舒服","贴心"]
MOODS_BAD = ["难受","糟糕","烦死","受不了","累瘫"]

# --- 主题词扩展 (每个 yaml 独立配 keywords / utterances 池) -------------------
# 每条 sub-route 都有种子, 我们从种子 keywords + 通用扩展词补齐到 9 个
# 用 sub-route name 关键词 + 已有 keywords 推一些近义/口语化变体

GENERIC_KW_PAD = ["真的","好烦","好喜欢","好爱","好惨","咋办","咋整","你说呢","谁懂","真离谱","笑死","太可爱","太难了","完蛋","救命"]
GENERIC_UTT_PAD_USER = [
    "{user_addr}你说呢","{user_addr}笨猫问你","{user_addr}咋整","{user_addr}帮帮人家",
    "{user_addr}有没有招","{user_addr}快出主意","{user_addr}笨猫不行了","{user_addr}陪陪猫猫"
]
GENERIC_UTT_PAD_PLAIN = [
    "真的好烦","笑死人家了","谁懂啊","完蛋了","救命","咋整啊","太离谱了","裂开","笨猫想哭","笨猫躺平"
]

# --- 子路由关键词 / 口语化 短语词典 ----------------------------------------
# key = sub-route name 前缀 (前 3 段) → 拓展词 list
# 不在词典里的, fallback 用 sub-route name 推
EXTRA_KW = {
    # 889 贴纸大战
    "sb_tiezhi": ["疯狂贴","贴满","贴贴贴贴","贴脸","贴满屏","贴回去","糊一脸"],
    "sb_caitiao": ["撒纸","彩纸雨","撒花花","庆祝特效","烟花特效","欢呼"],
    "sb_dianzan": ["双击666","点爆赞","赞到爆","赞到手酸","点不停","赞翻"],
    "sb_shubiao": ["双击疲劳","点到手抽","按到手指疼","点麻了","点到手肿"],
    "sb_zhuti": ["皮肤贴纸","主题包","贴纸包","新出包","限定皮肤","特别版"],
    "sb_huimou": ["撩人眼神","抛媚眼","暧昧眼神","心动表情包","暧昧符号","桃心眼"],
    "sb_shoubu": ["手指心","双手比心","小心心","手势贴纸","OK贴","V字"],
    "sb_pinkou": ["表情贫困","贴纸库空","存货少","贴纸枯竭","没存图","没素材"],
    "sb_shoucang": ["保存方法","存到本地","加进收藏","怎么存","怎么截","怎么留"],
    "sb_chaotian": ["一直贴","刷屏轰炸","满屏炸","贴到看不见","狂发贴纸","屠版"],
    # 890 下雨
    "rd_xiayu": ["下雨啦","在下雨","雨好猛","暴雨天","狂风暴雨","雨哗哗"],
    "rd_dasan": ["忘记带伞","没拿伞","裸奔回家","狼狈淋雨","雨里跑"],
    "rd_yixiang": ["雨声音","白噪音","催眠雨声","治愈雨","雨打窗","沙沙雨"],
    "rd_yuxie": ["鞋全湿","袜湿透","鞋进水","踩水洼","泡脚","湿哒哒"],
    "rd_chiyu": ["想吃热的","喝热汤","吃火锅","吃面","吃粥","暖胃食物"],
    "rd_xinqing": ["阴天压抑","情绪低","懒得动","想躺平","emo","郁闷"],
    "rd_caihong": ["彩虹出现","双彩虹","美翻","太美了","拍下来","见者好运"],
    "rd_dushui": ["街上积水","内涝","堵水","水没过脚","下水道堵"],
    "rd_yusan": ["伞坏","伞翻","伞架弯","伞骨折","风太大","破伞"],
    "rd_anjiao": ["共撑伞","共撑一把","一起淋雨","雨中漫步","雨景拍照","浪漫雨天"],
    # 891 初雪
    "fs_chuxue": ["第一场雪","下雪了","初雪来了","雪花飘","雪降临"],
    "fs_xueren": ["堆雪人","捏雪人","做雪人","雪人鼻子","雪人配件"],
    "fs_dazhang": ["打雪仗","雪球大战","互扔雪","雪球砸","堵雪球"],
    "fs_xuejing": ["雪景超美","白茫茫","雪世界","银装","雪覆盖"],
    "fs_chufa": ["雪天出门","雪地走","雪路滑","雪天通勤"],
    "fs_lengyan": ["冷得发抖","冷死","手脚冰","冻僵","冷麻"],
    "fs_xuepian": ["雪花好美","雪花飘","捧雪花","雪花六角","看雪花"],
    "fs_tongnian": ["小时候的雪","童年雪","怀念雪","回忆雪"],
    "fs_dongyu": ["冬天的浪漫","冬季氛围","冬日感","冬天美"],
    "fs_xueyong": ["雪后阳光","雪化","雪没了","失望融化"],
    # 892 雷电
    "tn_dalei": ["打雷了","雷声响","轰隆隆","雷阵雨","雷声大"],
    "tn_pa": ["雷怕","怕打雷","吓到了","害怕","吓出魂"],
    "tn_shandian": ["闪电亮","闪电劈","紫光","闪一下","刺眼"],
    "tn_duokai": ["躲被窝","躲沙发","钻被子","抱枕头","捂耳朵"],
    "tn_baowen": ["要抱抱","抱紧","抱我","保护我"],
    "tn_lingjing": ["停电","断电","跳闸","黑屏","没电"],
    "tn_dianlu": ["拔插头","防雷","断电源","保护电器","防雷击"],
    "tn_chuanghu": ["关窗","下雨进屋","风灌进来","关好门窗"],
    "tn_chongdian": ["不充电","别充电","断网线","路由停"],
    "tn_jielei": ["接地","防雷击","避雷针","屋顶避雷"],
    # 893 风大
    "wd_dafeng": ["大风","刮风","风超大","狂风","风呼呼"],
    "wd_toufa": ["头发吹乱","头发飞","头发遮眼","乱发","炸毛"],
    "wd_youfu": ["衣服飞","衣摆乱飞","裙子吹","风灌进衣服"],
    "wd_zhengyan": ["睁不开眼","风吹眼","眯眼","眼睛进沙"],
    "wd_chuiluo": ["东西吹走","帽子飞","纸张飞","小物件飞"],
    "wd_changfa": ["长发飘","马尾乱","刘海乱","碎发乱"],
    "wd_konglian": ["风把妆吹花","脸吹麻","脸蛋红"],
    "wd_yusan": ["伞被吹翻","伞撑不住","伞反转","风太大撑不住"],
    "wd_chenhuang": ["走路被吹","被吹得走","抗风","站不稳"],
    "wd_qingxin": ["风吹很爽","凉风","清爽","风很舒服"],
    # 894 大雾
    "fg_dawu": ["有雾","大雾天","雾蒙蒙","白茫茫","能见度低"],
    "fg_kaiche": ["开车看不清","雾天开车","能见度差","开慢点"],
    "fg_zoulu": ["走路看不见","摸黑走","盲走","看不清路"],
    "fg_xinqing": ["雾天压抑","闷得慌","郁闷","看不远"],
    "fg_zhaopian": ["雾景拍照","雾里美","氛围感","拍出仙境"],
    "fg_jianjin": ["渐变景","若隐若现","迷迷糊糊"],
    "fg_jianyan": ["眼睛酸","看东西累","视线模糊"],
    "fg_kongqi": ["空气湿","湿气重","闷"],
    "fg_chenjian": ["晨雾","清晨雾","早上雾大"],
    "fg_xianjing": ["仙境感","梦幻","云中漫步"],
    # 895 大太阳
    "sl_taiyang": ["大太阳","出太阳","晴天","好天气"],
    "sl_chaoshi": ["晒得脸热","暴晒","刺眼","耀眼"],
    "sl_yangguang": ["阳光好","阳光暖","好晒"],
    "sl_lansan": ["懒洋洋","想瘫","晒着不想动","晒太阳"],
    "sl_baoshai": ["要防晒","涂防晒","打遮阳伞"],
    "sl_zhongshu": ["可能中暑","头晕","太热中暑"],
    "sl_shaijiao": ["晒被子","晒衣服","太阳晒衣"],
    "sl_chuanlin": ["穿凉鞋","穿短袖","清爽穿搭"],
    "sl_yinliao": ["想喝冷饮","想吃冰","冰镇饮料"],
    "sl_xiawu": ["午后困","下午犯困","晒得想睡"],
    # 896 闷热潮湿
    "hu_menre": ["闷热","湿热","闷得慌","透不过气"],
    "hu_chaoshi": ["湿哒哒","黏糊糊","潮湿","到处湿"],
    "hu_zaomei": ["回南天","梅雨","湿气","霉味"],
    "hu_kongtiao": ["开空调","除湿","抽湿","开除湿机"],
    "hu_pifu": ["皮肤黏","出汗","汗黏","身上黏"],
    "hu_yifu": ["衣服不干","晾不干","衣服潮","湿衣"],
    "hu_xinqing": ["心情烦躁","闷得烦","湿气压抑"],
    "hu_tiyongan": ["失眠","睡不着","闷得睡不着"],
    "hu_dixiashi": ["地板湿","地砖出水","墙皮潮"],
    "hu_meiyuji": ["梅雨季","连绵雨","下不停"],
    # 897 寒颤冬天
    "cs_haoleng": ["好冷","冷死","冷到发抖","冻僵"],
    "cs_kaiyuan": ["哈白气","呵气","白雾气"],
    "cs_shoubing": ["手冰","手冻","手指麻"],
    "cs_jiaobing": ["脚冰","脚冻","脚指麻"],
    "cs_dongjiang": ["冻僵","僵硬","动不了"],
    "cs_quanshen": ["浑身冷","发抖","哆嗦"],
    "cs_bijian": ["鼻子冻红","鼻头红","鼻塞"],
    "cs_chiqi": ["牙打颤","上牙磕下牙","咯咯响"],
    "cs_rumian": ["想钻被窝","钻被","裹被子"],
    "cs_kaihuo": ["烤火","暖炉","小太阳"],
    # 898 热浪
    "hw_haore": ["好热","热死","热爆","烫到"],
    "hw_chuhan": ["出汗","汗如雨","汗流浃背"],
    "hw_haire": ["热到死","快化了","热到瘫"],
    "hw_kongtiao": ["开冷气","开26度","空调党"],
    "hw_lengyin": ["要冰水","冰可乐","冰咖啡"],
    "hw_shengbing": ["要中暑","头晕","低血糖"],
    "hw_changshui": ["补水","多喝水","脱水"],
    "hw_yixie": ["不想动","懒散","瘫","瘫一天"],
    "hw_yangtai": ["阳台烫","地板烫","金属烫"],
    "hw_jueyu": ["想钻冰箱","泡冰水","泡澡"],
    # 899 微风
    "bz_weifeng": ["微风","小风","凉风","清风"],
    "bz_shufu": ["舒服","真舒服","凉爽","惬意"],
    "bz_changfa": ["头发飘","刘海拂","风吹长发"],
    "bz_yangtai": ["阳台吹风","阳台坐","开窗吹"],
    "bz_xianyu": ["闲鱼","闲适","懒散","佛系"],
    "bz_huayuan": ["花园","坐花园","花香"],
    "bz_yedu": ["夜读","夜里看书","夜晚惬意"],
    "bz_sanbu": ["散步","走走","遛遛"],
    "bz_pinxie": ["品茶","喝茶","闻香"],
    "bz_xinqing": ["心情好","治愈","岁月静好"],
    # 900 口袋暖手
    "pw_kounuan": ["口袋暖","袖口暖","插口袋"],
    "pw_dianre": ["暖宝宝","电热包","暖手宝"],
    "pw_shoutao": ["戴手套","手套","保暖手套"],
    "pw_baoshou": ["抱手","双手互搓","哈气"],
    "pw_ronghua": ["毛绒手套","毛茸茸","绒里"],
    "pw_chuangkou": ["床上暖","被窝暖","炕头"],
    "pw_chongdian": ["暖宝宝充电","USB暖手"],
    "pw_tieshen": ["贴身暖","贴肚暖","贴腰暖"],
    "pw_zhuayang": ["手痒","冻疮","痒得抓"],
    "pw_jiazhang": ["手指僵","手指动不了","僵硬"],
    # 901 抢被子
    "bs_qiangbei": ["抢被子","拽被","拉被","争被"],
    "bs_juanbei": ["卷被子","裹成蛹","包成粽"],
    "bs_taibei": ["踢被","蹬被","蹬被边"],
    "bs_huibei": ["回被窝","钻被窝","进被"],
    "bs_baowen": ["保暖","暖被","暖窝"],
    "bs_dianbei": ["电热毯","电毯","加热"],
    "bs_zhuangzhe": ["装睡","装乖","偷被"],
    "bs_jixi": ["脚伸过来","冰脚踢","凉脚贴"],
    "bs_shujue": ["睡觉乱","姿势乱","横躺"],
    "bs_xinghuo": ["醒来没被","早上凉","裸睡了"],
    # 902 找袜子
    "sk_zhaowa": ["找袜子","袜子丢","袜子不见"],
    "sk_pidiao": ["袜子掉","只剩一只","另一只呢"],
    "sk_xiyiji": ["洗衣机吃","袜子失踪","洗丢"],
    "sk_yiguai": ["袜子怪","袜子妖怪","袜子精"],
    "sk_pousi": ["袜子破洞","脚趾露","破袜子"],
    "sk_chouchu": ["袜子臭","脚臭","熏死"],
    "sk_xinwa": ["新袜子","换新袜","买袜子"],
    "sk_fenseban": ["分色摆","按色分","袜子分类"],
    "sk_qichao": ["奇潮","潮袜","花袜"],
    "sk_putong": ["普通袜","黑袜","白袜"],
    # 903 毛衣痒
    "sw_yangda": ["毛衣痒","抓痒","痒死"],
    "sw_jingdian": ["静电","静电火","电到"],
    "sw_jiaohuo": ["脖子痒","脖颈痒","领口痒"],
    "sw_xiangling": ["香羊毛","真羊毛","纯毛"],
    "sw_xingdai": ["怀里抱","靠在身上","蹭毛衣"],
    "sw_xiyi": ["洗毛衣","毛衣缩水","变小"],
    "sw_qiumao": ["球毛","毛球","起球"],
    "sw_diaozhu": ["掉珠","掉装饰","掉扣"],
    "sw_naizhu": ["奶住","奶里奶气","奶呼呼"],
    "sw_pinmao": ["拼色毛衣","条纹毛衣","花纹"],
    # 904 帽子静电
    "ht_jingdian": ["静电","头发立","头发翘"],
    "ht_zhanmao": ["粘毛","头发吸","头发贴"],
    "ht_paomao": ["跑头发","头发分叉","炸毛"],
    "ht_maomao": ["毛帽","保暖帽","毛球帽"],
    "ht_yutuo": ["遮丑","遮油头","懒人帽"],
    "ht_shimao": ["帽子失踪","找帽","帽子不见"],
    "ht_chuima": ["吹乱","风刮帽","帽飞了"],
    "ht_shige": ["湿了","帽子湿","下雨湿"],
    "ht_youfan": ["油头","懒得洗","遮油"],
    "ht_zuqu": ["帽兜","连帽","兜帽"],
    # 905 手套丢一只
    "gl_diuyi": ["手套丢一只","只剩一只","少一只"],
    "gl_zhaobu": ["找不到","到处找","翻不到"],
    "gl_lianzhi": ["连指","连指手套","包指"],
    "gl_chumo": ["触屏手套","能触屏","屏不了"],
    "gl_qingji": ["轻击","点屏","点击不灵"],
    "gl_baonuan": ["保暖手套","加绒","暖手"],
    "gl_shidian": ["湿点","沾水","湿了"],
    "gl_fenzi": ["分指","五指手套","分开"],
    "gl_maogu": ["毛茸茸","绒手套","软乎"],
    "gl_paifen": ["拍粉","拍灰","落灰"],
    # 906 围巾
    "sf_weijin": ["围巾","围一圈","围脖"],
    "sf_qiazhuo": ["卡脖","勒脖","紧"],
    "sf_changjin": ["太长","长围巾","拖地"],
    "sf_baonuan": ["保暖","暖脖","暖颈"],
    "sf_huase": ["花色","花纹","色彩"],
    "sf_xixi": ["洗洗","洗围巾","水洗"],
    "sf_diaoluo": ["掉落","掉地","摔了"],
    "sf_chenshan": ["缠衫","缠在衣","缠住"],
    "sf_xiongzhao": ["蒙脸","蒙嘴","遮脸"],
    "sf_kuaige": ["快割","勒得疼","勒红"],
    # 907 拖鞋
    "sl_tuoxie": ["拖鞋","暖拖鞋","毛拖"],
    "sl_nuanjiao": ["暖脚","脚暖","暖脚趾"],
    "sl_maorong": ["毛绒拖","毛茸茸","绒拖"],
    "sl_xianai": ["可爱","萌拖","卡通拖"],
    "sl_paoji": ["跑掉","拖鞋飞","踢飞"],
    "sl_chuangbian": ["床边","下床","起床穿"],
    "sl_zoulu": ["走路啪嗒","啪嗒声","走得响"],
    "sl_houdi": ["厚底","软底","防滑底"],
    "sl_zhuanyi": ["专用","居家","室内"],
    "sl_changchuan": ["常穿","天天穿","离不开"],
    # 908 睡衣
    "pj_shuiyi": ["睡衣","居家服","睡袍"],
    "sxs_lianti": ["连体睡衣","卡通睡衣","动物睡衣"],
    "pj_maotai": ["毛毯款","加绒睡衣","保暖睡衣"],
    "pj_kabu": ["卡通","印花","印图"],
    "pj_chunmian": ["纯棉","棉睡衣","软棉"],
    "pj_zhuajie": ["抓绒","绒里","暖绒"],
    "pj_shoukou": ["袖口长","袖太长","盖手"],
    "pj_kuaiqiu": ["裤脚","裤长","裤短"],
    "pj_xinkuan": ["新款","新出","新到"],
    "pj_lifu": ["礼服","睡衣节","穿一周"],
    # 909 卫衣
    "hd_weiyi": ["卫衣","连帽","帽衫"],
    "hd_dunao": ["躲脑","钻帽","躲帽"],
    "hd_zhibao": ["纸袋","口袋","前口袋"],
    "hd_juanmao": ["卷毛","毛绒","绒里"],
    "hd_kuandu": ["宽大","oversize","巨大码"],
    "hd_shenghuo": ["生活感","日常","百搭"],
    "hd_zhaomao": ["遮发","遮头","遮发型"],
    "hd_shoubu": ["手部","手插袋","插口袋"],
    "hd_pinpai": ["品牌","logo","印字"],
    "hd_yangzi": ["懒人卫衣","sloth","佛系穿搭"],
    # 910 短裙飘
    "sk_qunzi": ["裙子","短裙","百褶裙"],
    "sk_piaofei": ["飘起来","风吹起","裙摆飞"],
    "sk_yali": ["压裙","压住","按住"],
    "sk_dafen": ["搭粉","搭白","配色"],
    "sk_baojie": ["保洁","勤洗","常洗"],
    "sk_chunqiu": ["春秋裙","季节裙","薄裙"],
    "sk_tanqi": ["叹气","裙子又掉","裙子转"],
    "sk_dieban": ["褶板","褶皱","褶痕"],
    "sk_nuduan": ["女短","短裙","mini"],
    "sk_paichuan": ["拍穿搭","穿搭","outfit"],
    # 911 扣子松
    "bn_kouzi": ["扣子","钮扣","纽扣"],
    "bn_songdiao": ["扣子掉","扣子松","扣子脱"],
    "bn_diufen": ["丢分","丢失","掉了"],
    "bn_xiufu": ["缝补","针线","缝起来"],
    "bn_xianxue": ["线穴","线松","线断"],
    "bn_chenshan": ["衬衫","正装","制服"],
    "bn_yifu": ["衣服","外套","西装"],
    "bn_kouwei": ["扣位","扣眼","扣口"],
    "bn_xiezi": ["谢谢","得帮","找你"],
    "bn_xinkou": ["新扣","换扣","买扣"],
    # 912 拉链卡
    "zp_lalian": ["拉链","拉锁","Zipper"],
    "zp_kaqu": ["卡了","卡住","卡死"],
    "zp_buchang": ["拉不上","拉不动","拉不开"],
    "zp_huahua": ["滑动","滑脱","滑下"],
    "zp_yiwu": ["夹布","咬布","卡布"],
    "zp_youla": ["拉合不上","合不拢","闭不上"],
    "zp_xiuxie": ["修拉链","修补","换拉链"],
    "zp_youshe": ["油涂","润滑","蜡涂"],
    "zp_dijia": ["底架","拉链头","拉链脚"],
    "zp_zhe": ["卡纸","夹纸","卡毛"],
    # 913+ 后续兜底
}

# 通用 fallback: 从已有 keywords 推
def fallback_kw_pad(name: str, existing: list) -> list:
    out = []
    for k in existing:
        # 把短语弱变体
        if "好" in k and "好" + k not in existing and len(k) <= 4:
            out.append(k + "啊")
        if k.endswith("了") and k[:-1] not in existing:
            out.append(k[:-1])
    for w in GENERIC_KW_PAD:
        if w not in existing and w not in out:
            out.append(w)
        if len(out) >= 6:
            break
    return out

# --- responses 生成池 (按 intent) ----------------------------------------
def gen_responses(name: str, intent: str, theme_hint: str, existing: list) -> list:
    """构造 8 个 responses, 已有的保留前面位置, 新增补到 8 个"""
    out = list(existing)  # 保留原有
    actions = ACTIONS_PLAYFUL if intent == "playful" else (
        ACTIONS_COMPLAINT if intent == "complaint" else ACTIONS_QUESTION
    )
    pronoun_pool = PRONOUNS
    rng = random.Random(hash(name) & 0xFFFFFFFF)
    # 8 个固定模板
    templates_playful = [
        '{act} 嗷呜～{{user_addr}}{pn}陪你玩 ฅฅ',
        '哼～{theme}才不让{{user_addr}}独享',
        '{act} {pn}也要参一脚喵～',
        '{{user_addr}}笨蛋, {pn}比你还兴奋 ฅฅ',
        '(尾巴翘) 嗷呜～{theme}就该这么玩',
        '{pn}才不输你呢喵～',
        '(凑过来) {{user_addr}}带{pn}一起 ฅฅ',
        '哼～看{pn}怎么接招喵呜',
    ]
    templates_complaint = [
        '{act} 嗷呜～{{user_addr}}{pn}也难受 ฅฅ',
        '哼～{theme}真的烦死人家了',
        '{act} {pn}陪你一起瘫喵呜',
        '{{user_addr}}别撑了, 听{pn}的歇会儿',
        '(尾巴搭) {theme}就是欠惨啊',
        '{pn}也想躺平喵～',
        '(蜷成团) {{user_addr}}抱抱{pn} ฅฅ',
        '哼～{pn}还能再叹一口气',
    ]
    templates_question = [
        '{act} 嗷呜～{{user_addr}}{pn}想想看 ฅฅ',
        '哼～{theme}得问明白',
        '(凑头) {pn}帮你查查喵～',
        '{{user_addr}}给{pn}点提示嘛',
        '(歪头) {pn}也有点好奇',
        '哼～{pn}的答案是这样喵呜',
        '(尾巴问号) {theme}咋整 ฅฅ',
        '{pn}和{{user_addr}}一起琢磨喵',
    ]
    if intent == "playful":
        templates = templates_playful
    elif intent == "complaint":
        templates = templates_complaint
    else:
        templates = templates_question
    # 补到 8
    idx = 0
    while len(out) < 8 and idx < len(templates):
        act = rng.choice(actions)
        pn = rng.choice(pronoun_pool)
        line = templates[idx].format(act=act, pn=pn, theme=theme_hint)
        # 去 {{user_addr}} double brace
        line = line.replace("{{user_addr}}", "{user_addr}")
        if line not in out:
            out.append(line)
        idx += 1
    # 仍不足 → 用尾词追加
    while len(out) < 8:
        act = rng.choice(actions)
        pn = rng.choice(pronoun_pool)
        tail = rng.choice(CAT_TAIL)
        line = f'{act} {pn}补一句{theme_hint}{tail}'
        if line not in out:
            out.append(line)
        else:
            line += " ฅ"
            out.append(line)
    return out[:8]


def gen_keywords(name: str, existing: list) -> list:
    """补到 9 个"""
    out = list(existing)
    # name 前缀 prefix_topic_001 → prefix_topic
    parts = name.rsplit("_", 1)
    key = parts[0] if parts else name
    extras = EXTRA_KW.get(key, [])
    for k in extras:
        if k not in out:
            out.append(k)
        if len(out) >= 9:
            break
    if len(out) < 9:
        for k in fallback_kw_pad(name, existing):
            if k not in out:
                out.append(k)
            if len(out) >= 9:
                break
    while len(out) < 9:
        out.append(existing[0] + str(len(out)))
    return out[:9]


def gen_utterances(name: str, existing: list, theme_hint: str) -> list:
    """补到 11 个, 半数带 {user_addr}, 半数不带"""
    out = list(existing)
    rng = random.Random(hash(name + "u") & 0xFFFFFFFF)
    pad_user = list(GENERIC_UTT_PAD_USER)
    pad_plain = list(GENERIC_UTT_PAD_PLAIN)
    rng.shuffle(pad_user)
    rng.shuffle(pad_plain)
    # 与 theme 强相关的衍生句
    theme_lines = [
        f"{{user_addr}}笨猫陪我聊{theme_hint}",
        f"{{user_addr}}{theme_hint}咋整",
        f"我想说{theme_hint}",
        f"{theme_hint}笑死",
        f"{theme_hint}真的离谱",
        f"{{user_addr}}你也{theme_hint}吗",
    ]
    rng.shuffle(theme_lines)
    pool = theme_lines + pad_user + pad_plain
    for line in pool:
        if line not in out:
            out.append(line)
        if len(out) >= 11:
            break
    while len(out) < 11:
        out.append(existing[0] + str(len(out)))
    return out[:11]


# --- YAML 解析 (轻量, 顺序敏感, 保留头部注释) -------------------------------
SUB_RE = re.compile(
    r"(?P<head>- name: (?P<name>\S+)\n"
    r"  intent: (?P<intent>\S+)\n)"
    r"  keywords: \[(?P<keywords>.*?)\]\n"
    r"  utterances:\n(?P<utt_block>(?:    - .+\n)+)"
    r"  responses:\n(?P<resp_block>(?:    - .+\n)+)"
    r"(?P<tail>(?:  disambiguate_context:.*?\n  weight:.*?\n)|"
    r"(?:  cooldown_seconds: \d+\n  weight: [\d.]+\n))",
    re.DOTALL,
)


def parse_list_line(block: str) -> list:
    out = []
    for line in block.splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        s = s[2:].strip()
        # 去掉左右双引号
        if s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
        out.append(s)
    return out


def render_list(items: list, key_kind: str) -> str:
    """key_kind = 'utterance' / 'response' → 全部双引号
       'keyword' → 不引号, [a, b, c] flow style
    """
    if key_kind == "keyword":
        return "[" + ", ".join(items) + "]"
    lines = []
    for s in items:
        # 转义内部双引号
        es = s.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    - "{es}"')
    return "\n".join(lines)


def expand_yaml(path: Path) -> int:
    raw = path.read_text(encoding="utf-8")
    # 抓 header (开头连续注释 + 空行)
    head_match = re.match(r"((?:#.*\n)+\n?)", raw)
    header = head_match.group(1) if head_match else ""
    body = raw[len(header):]

    # 用 sub-route 分割 (以 "- name:" 起头)
    chunks = re.split(r"(?=^- name: )", body, flags=re.MULTILINE)
    new_chunks = []
    count = 0
    for ch in chunks:
        if not ch.strip().startswith("- name:"):
            new_chunks.append(ch)
            continue
        # 拆 lines
        lines = ch.splitlines(keepends=True)
        name = ""
        intent = ""
        kw_existing: list = []
        utt_existing: list = []
        resp_existing: list = []
        cooldown_line = ""
        weight_line = ""
        disambig_lines: list = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if line.startswith("- name: "):
                name = line.split("- name: ", 1)[1].strip()
                i += 1
            elif stripped.startswith("intent:"):
                intent = stripped.split("intent:", 1)[1].strip()
                i += 1
            elif stripped.startswith("keywords:"):
                # 单行 flow
                m = re.search(r"keywords:\s*\[(.*?)\]", stripped)
                if m:
                    raw_kw = m.group(1)
                    kw_existing = [k.strip() for k in raw_kw.split(",") if k.strip()]
                i += 1
            elif stripped.startswith("utterances:"):
                i += 1
                while i < n and lines[i].startswith("    - "):
                    s = lines[i].strip()[2:].strip()
                    if s.startswith('"') and s.endswith('"'):
                        s = s[1:-1]
                    utt_existing.append(s)
                    i += 1
            elif stripped.startswith("responses:"):
                i += 1
                while i < n and lines[i].startswith("    - "):
                    s = lines[i].strip()[2:].strip()
                    if s.startswith('"') and s.endswith('"'):
                        s = s[1:-1]
                    resp_existing.append(s)
                    i += 1
            elif stripped.startswith("cooldown_seconds:"):
                cooldown_line = line.rstrip("\n")
                i += 1
            elif stripped.startswith("weight:"):
                weight_line = line.rstrip("\n")
                i += 1
            elif stripped.startswith("disambiguate_context:"):
                # 收集到下一个 weight / cooldown / 空行前
                disambig_lines.append(line.rstrip("\n"))
                i += 1
                while i < n:
                    nxt = lines[i]
                    if nxt.startswith("    ") or nxt.startswith("      ") or nxt.startswith("  - ") or nxt.startswith("  ctx_") or nxt.startswith("  topic"):
                        disambig_lines.append(nxt.rstrip("\n"))
                        i += 1
                    else:
                        break
            else:
                i += 1

        if not name:
            new_chunks.append(ch)
            continue

        # 主题 hint = name 中间段
        parts = name.split("_")
        theme_hint = parts[1] if len(parts) >= 3 else (parts[-1] if parts else "聊聊")
        # 用 keywords[0] 替换主题词 (更具体)
        if kw_existing:
            theme_hint = kw_existing[0]

        new_kw = gen_keywords(name, kw_existing)
        new_utt = gen_utterances(name, utt_existing, theme_hint)
        new_resp = gen_responses(name, intent, theme_hint, resp_existing)

        # 重建块
        out_lines = [f"- name: {name}", f"  intent: {intent}"]
        out_lines.append(f"  keywords: {render_list(new_kw, 'keyword')}")
        out_lines.append("  utterances:")
        out_lines.append(render_list(new_utt, "utterance"))
        out_lines.append("  responses:")
        out_lines.append(render_list(new_resp, "response"))
        # disambig (如有) 放 cooldown 前
        for dl in disambig_lines:
            out_lines.append(dl)
        if cooldown_line:
            out_lines.append(cooldown_line)
        else:
            out_lines.append("  cooldown_seconds: 60")
        if weight_line:
            out_lines.append(weight_line)
        else:
            out_lines.append("  weight: 1.0")
        out_lines.append("")  # 空行
        new_chunks.append("\n".join(out_lines) + "\n")
        count += 1

    final_text = header + "".join(new_chunks)
    path.write_text(final_text, encoding="utf-8")
    return count


def main():
    total = 0
    for stem in STEMS:
        p = ROUTES_DIR / f"{stem}.yaml"
        if not p.exists():
            print(f"MISSING: {p}")
            continue
        n = expand_yaml(p)
        total += n
        print(f"{stem}: {n} sub-routes expanded")
    print(f"TOTAL sub-routes: {total}")


if __name__ == "__main__":
    main()
