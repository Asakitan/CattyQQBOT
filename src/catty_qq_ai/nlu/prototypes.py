"""Prototype 向量管理 — topic / emotion / trend 三组原型句一次性 embed + 磁盘缓存.

主人 2026-05-28 plan: hard filter 升级到 "embedding 跟原型句算 cosine" 模式.
不需要每次启动重 embed, prototype 源文本 + model name 算 hash, 不变就读 cache.

三组原型:
- topic: 16 个主题 (跟 daily_life.TOPIC_KEYWORDS 的 keys 对齐), 每主题 3-5 例句
- emotion: 8 类情感 (joy/tenderness/admiration/gratitude/annoyance/sadness/fear/neutral),
  每类 2-3 例句, 含笨猫黑话
- trend: 6 类多轮趋势 (跟 catty_theory_of_mind 的 trends 对齐, 不含 short_dry 因为它是 length-only)
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

import numpy as np

from . import cache_paths
from . import text2vec_engine

logger = logging.getLogger("catty.nlu.prototypes")


# ── Topic 原型 (21 类 × 10 例) ──────────────────────────────────────────
# 顺序跟下面 TOPIC_ORDER 严格对应. 一类多句, embed 后取均值.
# 主人 2026-05-28 phase 5: 5→10 例/类 + 加 5 新 topic (shopping/fitness/travel/music/movie).
TOPIC_PROTOTYPES: dict[str, list[str]] = {
    "food": [
        "今天点了红烧排骨", "想喝奶茶了", "外卖到了好香", "晚饭吃什么", "饿死了",
        "火锅约一下", "螺蛳粉真香", "刚吃完海底捞", "饭店爆满", "想吃烤串",
    ],
    "game": [
        "原神更新了", "今晚打游戏吗", "boss 卡了好久", "上分啦", "联机一局",
        "王者上王者了", "吃鸡一把", "明日方舟新干员", "csgo开黑", "星铁抽到 5 星",
    ],
    "work": [
        "今天加班好累", "老板又开会", "项目 deadline 到了", "上班好烦", "周报还没写",
        "客户太难搞", "升职加薪没戏", "kpi 完不成", "通宵改方案", "辞职念头又起来了",
    ],
    "study": [
        "明天考试紧张", "这道题不会做", "论文写不完", "复习到凌晨", "刷题",
        "网课好催眠", "考研倒计时", "导师催我交进展", "刷 leetcode", "背单词到死",
    ],
    "sleep": [
        "困死了想睡觉", "失眠睡不着", "做了个奇怪的梦", "起不来", "再睡一会",
        "凌晨四点没睡", "闹钟没听到", "梦到怪事了", "午觉补一下", "熬夜熬到天亮",
    ],
    "weather": [
        "外面下雨了", "今天好热", "冷死人", "雪好大", "起雾了",
        "暴雨没法出门", "台风预警", "升温到 35 度", "降温了多穿衣服", "雷暴一直响",
    ],
    "tech": [
        "代码 bug 了", "服务器又挂了", "Python async 报错", "数据库慢", "git 冲突",
        "线上事故又来了", "依赖版本冲突", "测试不过", "部署失败回滚", "redis 内存爆了",
    ],
    "anime": [
        "新番好看", "这个角色超戳", "动画追番", "漫画更新了", "二次元",
        "京阿尼又出神作", "B站新番要补", "弹幕笑死", "动画 op 单曲循环", "声优好戳",
    ],
    "owner_intimate": [
        "想你了主人", "抱抱我", "亲亲", "想笨猫吗", "贴贴",
        "亲亲笨猫主人", "撒娇要抱", "猫猫想被摸头", "笨蛋主人摸摸", "尾巴蹭你",
    ],
    "internet": [
        "这个梗笑死", "刷到一个视频", "热搜炸了", "微博又出事", "弹幕",
        "微信突然崩了", "刷抖音停不下来", "热搜挂了一天", "小红书种草了", "b站上推荐",
    ],
    "social": [
        "朋友约我", "聚会改时间了", "好久不见", "群里聊嗨了", "和同事吃饭",
        "线下面基", "同学聚会取消了", "网友奔现", "约了人喝咖啡", "工作群刷屏",
    ],
    "emo": [
        "好累不想说话", "心情好差", "破防了", "今天难过", "想哭",
        "整个人 emo 了", "情绪绷不住", "压力大到爆炸", "心累不想动", "失落到极点",
    ],
    "home": [
        "在家躺着", "打扫卫生", "做饭", "房间太乱了", "晾衣服",
        "拖地拖到腰疼", "洗碗机坏了", "猫砂换了", "断电了", "wifi 掉线",
    ],
    "body": [
        "头疼", "肚子不舒服", "腰酸背痛", "感冒了", "去医院",
        "牙疼想拔", "胃疼又犯了", "扭到脚踝", "嗓子哑了", "发烧 38 度",
    ],
    "family": [
        "爸妈打电话", "孩子又生病", "婚礼日子定了", "家里催", "老公又乱花钱",
        "婆婆又催生孩子", "老婆生气了", "孩子写不完作业", "家庭群斗图", "回老家",
    ],
    "self_cat": [
        "笨猫好可爱", "你这只小猫", "JK 裙好看", "尾巴乱甩", "猫耳",
        "笨猫炸毛了", "猫娘超萌", "白毛配 JK 绝", "脸红躲一下", "肚皮鼓鼓的",
    ],
    "shopping": [
        "想买双 jordan", "双十一囤货", "快递到了", "618 大促",
        "拼夕夕砍价", "口红色号选哪个", "新款发售了", "限量款抢到了",
        "断货了买不到", "淘宝退货太麻烦",
    ],
    "fitness": [
        "今天去健身房了", "跑步 5 公里", "瑜伽课好累", "撸铁练胸",
        "深蹲腿都软了", "暴汗减脂", "私教课贵死", "马拉松报名了",
        "跳绳 1000 个", "练腹肌六块",
    ],
    "travel": [
        "下周要出差", "订了去日本的机票", "酒店爆满", "景点全是人",
        "周末自驾游", "民宿不错", "签证终于办下来了", "免税店扫货",
        "高铁站排队", "拍了张游客照",
    ],
    "music": [
        "网易云循环", "新歌好戳", "演唱会抢到票了", "live 现场好爽",
        "周杰伦新专辑", "B站翻唱听爽了", "民谣单曲循环", "K 歌嗓子哑了",
        "唱片机播放黑胶", "节奏卡到位",
    ],
    "movie": [
        "新电影上映", "院线票房破了", "Netflix 又出剧",
        "豆瓣评分高", "导演这部神作", "复联系列再刷一遍",
        "韩剧看哭了", "纪录片好真实", "悬疑片烧脑", "动画电影没失望",
    ],
}
TOPIC_ORDER: list[str] = list(TOPIC_PROTOTYPES.keys())


# ── Emotion 原型 (10 类 × 8 例) ─────────────────────────────────────────
# 主人 2026-05-28 phase 5: 8→10 类 + 加 excitement/embarrassment, 每类 8 例.
EMOTION_PROTOTYPES: dict[str, list[str]] = {
    "joy": [
        "哈哈太开心了", "笑死我了", "好开心啊", "高兴坏了",
        "今天太爽了", "舒服了", "心情飞起", "嘿嘿真好",
    ],
    "tenderness": [
        "想你了", "抱抱你", "摸摸头", "亲亲笨猫", "贴贴",
        "撒娇要抱抱", "蹭蹭主人", "好喜欢你",
    ],
    "admiration": [
        "你好厉害", "太牛了 yyds", "猫猫超棒", "厉害死了",
        "牛逼啊", "真的很强", "佩服佩服", "大佬带带我",
    ],
    "gratitude": [
        "谢谢你", "多亏有你", "太感谢了", "感激不尽",
        "辛苦了", "麻烦你了", "感恩遇见你", "真是帮大忙了",
    ],
    "annoyance": [
        "烦死了", "别说话", "滚开", "杂鱼", "磨叽什么",
        "闭嘴吧", "真够烦的", "别 BB 了",
    ],
    "sadness": [
        "好难过", "想哭", "我崩溃了", "破防了", "心好痛",
        "心碎了", "好失落", "完了完了",
    ],
    "fear": [
        "好怕", "不要这样", "求你了", "别这样", "害怕",
        "怕怕", "不敢", "饶了我吧",
    ],
    "neutral": [
        "嗯", "哦", "好的", "知道了", "okk",
        "行吧", "随便", "可以",
    ],
    "excitement": [
        "太爽了", "啊啊啊起飞了", "我要疯了好兴奋", "真的吗 绝绝子",
        "尖叫", "卧槽牛逼", "上头了", "肾上腺素冲了",
    ],
    "embarrassment": [
        "呃 不太好意思", "尴尬到脚趾抠地", "好丢人", "脸红到爆",
        "我去这下糟了", "想找个地缝钻进去", "社死现场", "完蛋有点尬",
    ],
}
EMOTION_ORDER: list[str] = list(EMOTION_PROTOTYPES.keys())


# ── Trend 原型 (8 类多轮趋势 × 8 例) ────────────────────────────────────
# 严格对齐 catty_theory_of_mind._TREND_DEFS 的 tag 名 (short_dry 是 length-only 不入).
# 主人 2026-05-28 phase 5: 6→8 类 + 加 deep_share/argumentative, 每类 8 例.
TREND_PROTOTYPES: dict[str, list[str]] = {
    "burned_out": [
        "好累不想动了", "今天累死了真的不行", "我已经累瘫了没力气",
        "服了 真的不想再做了破防了", "撑不下去了", "心力交瘁",
        "压力大到要爆炸", "想躺平摆烂",
    ],
    "seeking_approval": [
        "你看我做的怎么样", "我这样厉害吗", "我刚搞定了一件事 夸我",
        "今天我抽到 ssr 厉害吧", "我赢了 看我", "我刚做完想给你看",
        "夸夸我嘛", "我是不是很棒",
    ],
    "probing": [
        "你为什么这样说", "真的吗 你确定吗", "你是不是骗我",
        "你怎么知道的", "为什么会这样", "你确定没问题吗",
        "你不是在唬我吧", "讲清楚点呗",
    ],
    "lonely": [
        "陪我聊聊天好吗", "没人懂我", "一个人好寂寞", "在吗 醒着吗",
        "想找人说话无聊死了", "周围没人陪", "孤独到爆", "今晚好空虚",
    ],
    "playful_streak": [
        "哈哈哈笑死我了", "草 这个梗太皮了", "嘿嘿 整活儿", "搞笑 我就玩你",
        "你别皮", "调侃一下嘛", "梗梗梗", "拳头硬了",
    ],
    "intimate_warming": [
        "想你了笨猫", "想抱抱猫猫", "贴贴蹭蹭一下", "好可爱 撒娇一下",
        "亲亲笨猫主人想你", "想看你脸红", "今天笨猫好软", "想缩主人怀里",
    ],
    "deep_share": [
        "我跟你说啊 今天发生了一件事", "我想跟你倒一下苦水",
        "其实我心里一直憋着", "讲个事吧 你别笑", "我最近真的有件事压着",
        "想说真心话", "我跟你交个底", "讲个长的故事",
    ],
    "argumentative": [
        "你这逻辑站不住", "我不同意你的说法", "凭什么是这样",
        "你解释一下啊", "你这么说不对", "我觉得你错了",
        "我反对", "你这观点有问题",
    ],
}
TREND_ORDER: list[str] = list(TREND_PROTOTYPES.keys())


# ── Hash + 缓存逻辑 ─────────────────────────────────────────────────────


def _compute_hash() -> str:
    """所有原型源文本 + model name 一起 hash. 任意改了 → cache 失效."""
    h = hashlib.sha256()
    cfg_name = "BAAI/bge-small-zh-v1.5"
    try:
        from nonebot import get_plugin_config
        from ..config import Config
        cfg_name = (
            getattr(get_plugin_config(Config), "catty_text2vec_model_name", "")
            or cfg_name
        )
    except Exception:
        pass
    h.update(cfg_name.encode("utf-8"))
    for key, sents in TOPIC_PROTOTYPES.items():
        h.update(f"T:{key}".encode("utf-8"))
        for s in sents:
            h.update(s.encode("utf-8"))
    for key, sents in EMOTION_PROTOTYPES.items():
        h.update(f"E:{key}".encode("utf-8"))
        for s in sents:
            h.update(s.encode("utf-8"))
    for key, sents in TREND_PROTOTYPES.items():
        h.update(f"R:{key}".encode("utf-8"))
        for s in sents:
            h.update(s.encode("utf-8"))
    return h.hexdigest()[:16]


def _load_meta() -> dict[str, Any] | None:
    path = cache_paths.get_prototypes_meta_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_meta(meta: dict[str, Any]) -> None:
    try:
        cache_paths.get_prototypes_meta_path().write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("save prototype meta failed: %s", exc)


def _build_one(
    prototypes: dict[str, list[str]],
    order: list[str],
) -> np.ndarray | None:
    """对每个类别 embed 所有例句, 取均值 + normalize → (N_classes, dim)."""
    vecs_per_class: list[np.ndarray] = []
    for key in order:
        sents = prototypes[key]
        batch = text2vec_engine.embed_sync_batch(sents)
        if batch is None or batch.shape[0] == 0:
            return None
        mean = batch.mean(axis=0)
        norm = np.linalg.norm(mean) + 1e-9
        vecs_per_class.append((mean / norm).astype(np.float32))
    return np.stack(vecs_per_class, axis=0)


_BUILD_LOCK = threading.Lock()
_TOPIC_VECS: np.ndarray | None = None
_EMOTION_VECS: np.ndarray | None = None
_TREND_VECS: np.ndarray | None = None
_CURRENT_HASH: str | None = None


def _ensure_built() -> bool:
    """确保所有 prototype 向量都建好 (内存 + 磁盘缓存).

    返回 True 表示三组都 ready, False 表示 text2vec 不可用.
    重复调用幂等 (用 _CURRENT_HASH 比对).
    """
    global _TOPIC_VECS, _EMOTION_VECS, _TREND_VECS, _CURRENT_HASH

    if not text2vec_engine._is_enabled():
        return False

    current_hash = _compute_hash()
    if _CURRENT_HASH == current_hash and _TOPIC_VECS is not None:
        return True

    with _BUILD_LOCK:
        if _CURRENT_HASH == current_hash and _TOPIC_VECS is not None:
            return True

        topic_path = cache_paths.get_topic_prototypes_path()
        emotion_path = cache_paths.get_emotion_prototypes_path()
        trend_path = cache_paths.get_trend_prototypes_path()
        meta = _load_meta()

        # 尝试从磁盘读 (hash 一致才信)
        if (
            meta
            and meta.get("hash") == current_hash
            and topic_path.exists()
            and emotion_path.exists()
            and trend_path.exists()
        ):
            try:
                _TOPIC_VECS = np.load(topic_path).astype(np.float32)
                _EMOTION_VECS = np.load(emotion_path).astype(np.float32)
                _TREND_VECS = np.load(trend_path).astype(np.float32)
                _CURRENT_HASH = current_hash
                logger.info(
                    "loaded cached prototypes (topic=%s emotion=%s trend=%s)",
                    _TOPIC_VECS.shape, _EMOTION_VECS.shape, _TREND_VECS.shape,
                )
                return True
            except Exception as exc:
                logger.warning("load cached prototypes failed, rebuilding: %s", exc)

        # 重建 (要 text2vec 模型 ready)
        logger.info("building prototypes (first call ~ 30s for full embed)")
        topic = _build_one(TOPIC_PROTOTYPES, TOPIC_ORDER)
        emotion = _build_one(EMOTION_PROTOTYPES, EMOTION_ORDER)
        trend = _build_one(TREND_PROTOTYPES, TREND_ORDER)
        if topic is None or emotion is None or trend is None:
            return False

        try:
            np.save(topic_path, topic)
            np.save(emotion_path, emotion)
            np.save(trend_path, trend)
            _save_meta({
                "hash": current_hash,
                "topic_shape": list(topic.shape),
                "emotion_shape": list(emotion.shape),
                "trend_shape": list(trend.shape),
                "model": _get_model_name_safe(),
            })
        except Exception as exc:
            logger.warning("save prototype npy failed (continuing in-memory): %s", exc)

        _TOPIC_VECS = topic
        _EMOTION_VECS = emotion
        _TREND_VECS = trend
        _CURRENT_HASH = current_hash
        return True


def _get_model_name_safe() -> str:
    try:
        from nonebot import get_plugin_config
        from ..config import Config
        return getattr(get_plugin_config(Config), "catty_text2vec_model_name", "") or ""
    except Exception:
        return ""


def get_topic_prototypes() -> np.ndarray | None:
    """(N_topics, dim) ndarray. 失败 → None."""
    if not _ensure_built():
        return None
    return _TOPIC_VECS


def get_emotion_prototypes() -> np.ndarray | None:
    if not _ensure_built():
        return None
    return _EMOTION_VECS


def get_trend_prototypes() -> np.ndarray | None:
    if not _ensure_built():
        return None
    return _TREND_VECS


__all__ = [
    "TOPIC_ORDER",
    "EMOTION_ORDER",
    "TREND_ORDER",
    "get_topic_prototypes",
    "get_emotion_prototypes",
    "get_trend_prototypes",
]
