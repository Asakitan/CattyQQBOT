"""机机语料全量挖掘流水线 (2026-08-25, 升级计划 Wave ①)。

从 tmp/fdj_group_texts.jsonl (46,654 条机机群聊实录) 与
tmp/fdj_private_texts.jsonl (2,864 条私聊实录) 挖掘三样东西:

1. scene_pairs.jsonl  — 近全量 trigger→reply 场景对 (质量打分+去重+限流),
   比 fadianji_scenes.py 的 2,026 对配额抽样大一个量级, 供磁盘检索库使用。
2. style_stats.json   — 机机风格全量统计 (长度分布/标点率/口头禅词表/发癫率),
   供统计 style critic 与长度门控使用。
3. user_profiles.jsonl — per-trigger_name 互动画像 (称呼/语气档位/共同词/样例),
   供 corpus lore 注入使用。

确定性: 全量扫描 + 固定排序, 无随机, 幂等可重跑。
中间文件重新生成: python tmp/fdj_extract.py (原始 chunked 语料 → tmp/fdj_*_texts.jsonl)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUP_SRC = ROOT / "tmp" / "fdj_group_texts.jsonl"
PRIVATE_SRC = ROOT / "tmp" / "fdj_private_texts.jsonl"
OUT_DIR_DEFAULT = ROOT / "data" / "fadianji_corpus"

SCHEMA_VERSION = 1
GROUP_SCOPE = "group"
PRIVATE_SCOPE = "private:3670608232"

URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
LONG_NUM = re.compile(r"(?<!\d)\d{6,}(?!\d)")
CRED = re.compile(r"密码|口令|密钥|验证码|token|cookie|api[_ -]?key|secret|身份证|银行卡", re.I)
PH = re.compile(r"^\s*\[(?:text|system|image|audio|video|file|json|reply|forward|表情|图片|语音|视频|文件)\s*\]\s*$", re.I)
PLACEHOLDER_BRACKET = re.compile(r"^\[[^\[\]\n]{1,20}\]$")
BANNED = re.compile(r"喵|笨猫|猫娘|米雪儿|卡拉彼丘|猫耳|猫尾|主人")
GROUP_BANNED = re.compile(r"无名老师|好兄弟互损|一米九两百斤地雷男|一米九201超绝病娇地雷男|3670608232")
AUTOREPLY = re.compile(r"^\[自动回复\]")
TRAIL_PUNCT = "。！？!?，,;；：:~～…."
FADIAN_RE = re.compile(r"(?:啊{3,}|呜{3,}|好{3,}|哈{3,}|哦{3,}|嘿{3,}|嘻{3,}|妈妈{2,}|谢谢{2,}|笑死|我超|救命|好好好|呜呜)")
EMOJI_RE = re.compile(r"[\u2600-\u27BF\U0001F300-\U0001FAFF]")
ADDRESS_RE = re.compile(r"(?:谢谢|辛苦|爱你|想你|抱抱|摸摸)?(宝宝|老师|老板|大人|兄弟|哥们|哥|姐|宝贝|桃桃|乖乖)")
STOP_TERMS = frozenset("的了是我你他她它在不有和就都也还要这那很太真的吗呢啊哦嗯哈呀嘛去来上下个一")

GI = re.I
G = re.compile

# 场景分类表 (移植自 tmp/gen_fadianji_scenes_big.py, 去掉配额只留判定)。
# (场景名, trigger正则|None, reply正则|None, 特殊条件)
GROUP_SCENES: list[tuple[str, re.Pattern | None, re.Pattern | None, str | None]] = [
    ("分身口径相关", G(r"是不是本人|本人吗|真机机|机器人|\bai\b|人工智能|分身|本体|备用机|小机|你是机机|是不是机机|豆包|调教", GI), None, None),
    ("不懂/不知道", G(r"什么意思|啥意思|是什么|是谁|为啥|为什么|怎么办|怎么弄|怎么看"), G(r"不知道|不懂|没看懂|不清楚|不明白|没听说|不知道啊"), None),
    ("感谢/收礼", G(r"红包|礼物|打赏|送了|送你|上舰|舰长|年舰|提督|总督|谢谢|辛苦|帮我|帮忙|无料|生贺|画的|剪的|做的"), G(r"^(谢谢|辛苦|感谢|多谢|谢了)"), None),
    ("安慰/接脆弱", G(r"哭|呜呜|难受|难过|委屈|害怕|睡不着|生病|不舒服|崩溃|焦虑|想死|不想活|好点了吗|怎么样|没事吧"), G(r"摸摸|抱抱|没事|怎么了|好好休息|别哭|不哭|会好|吃药|休息"), None),
    ("熟人跳脸", None, G(r"^(滚啊?|爬|有病吧?|傻逼啊?|神经病啊?|讨厌|弱智啊?|你谁|你他妈|我草你妈|闭嘴|去死|捅死|不太行|不行|不要|是想被讨厌吗)[！!？?。…]*$"), None),
    ("NSFW 接梗与回避", G(r"涩图|涩|本子|开黄|黄腔|男娘|女同|网袜|黑丝|白丝|袜子|脚|腋下|喘|音声|黄油|里番|胸|奶|几把|牛子|扣|磨|\*|发情|骚|卖|嫖", GI), None, None),
    ("技术/求助", G(r"代码|报错|配置|服务器|脚本|部署|打不开|bug|网站|程序|安装|运行|接口|电脑|软件|模型|文件|解压|压缩", GI), None, None),
    ("被催播/催更", G(r"播吗|还播|几点播|什么时候播|啥时候播|催更|发视频|更新|什么时候发|剪完|做好了吗|画完|什么时候播"), None, None),
    ("直播相关", G(r"直播|下播|开播|播播|录播|电台|时长|本月的播|播了"), None, None),
    ("生日/节日", G(r"生日|快乐|新年|中秋|端午|圣诞|周年|节日|生贺|跨年"), None, None),
    ("早安晚安问安", G(r"早安|晚安|早上好|睡了|起床|醒了|睡觉|去睡|熬夜|睡了吗|睡着了"), None, None),
    ("深夜 emo", None, G(r"累|困|睡不着|烦|焦虑|难受|绝望|呜呜|想死|害怕|想哭|痛|疼|emo|哭"), "hour_0_5"),
    ("身体状态播报", G(r"疼|痛|难受|好点|身体|吃药|发烧|生病|肚子|腰|肩膀|魔法期|例假|生理期|胃|头显|药"), G(r"疼|痛|药|发烧|肚子|腰|肩膀|魔法期|胃|发炎|迷糊|恶心|晕"), None),
    ("要钱/充电/上舰", G(r"红包|钱|v我|充电|上舰|舰长|提督|总督|工资|饭钱|转账|v50|五十|打赏"), G(r"谢谢|好耶|要|给我|溜|钱|老板|大人|富|饿"), None),
    ("游戏日常", G(r"鸣潮|apex|打派|原神|王者|英雄联盟|lol|游戏|抽卡|排位|钻排|大师|怪猎|打瓦|守岸人|椿|弗洛洛|波仔|跳劈|五星|十连|保底|副本|boss", GI), None, None),
    ("吃瓜/点评第三方", G(r"主播|v圈|vtb|虚拟主播|抖音|b站|视频|评论区|运营|联动|节奏|风波|塌房|乐府|千袅|库莉姆|冰糖|粉丝|同行|隔壁", GI), G(r"感觉|其实|我觉得|是的|不是|确实|可能|应该|真的|我去|我靠|不知道|难绷"), None),
    ("黑评/恶意应对", G(r"黑评|黑子|骂|喷|傻逼|有病|神经病|对线|冲塔|塌房|举报|被封|黑我|恨|讨厌机"), G(r"傻逼|有病|其实|不知道|唉|我去|神经病|滚|畏惧|难绷|习惯|还好"), None),
    ("复读玩梗", None, None, "echo"),
    ("发癫", None, G(r"(?:啊{3,}|呜{3,}|好{3,}|哈{3,}|哦{3,}|嘿{3,}|嘻{3,}|妈妈{2,}|谢谢{2,}|哈哈|笑死|我超|救命|好好好)"), None),
    ("低能量自述", None, G(r"呜呜|好累|累死|好疼|疼死|痛|睡不着|好困|困死|害怕|想死|不想活|难受|好烦|烦死|崩溃|绝望|焦虑|想哭|不舒服|不想动|不想播"), None),
    ("认真观点", None, G(r"^(感觉|真的|我觉得|其实|说实话|怎么说|但是|因为|所以|可能|反而|首先|确实|应该|然后|主要|虽然)"), "min12"),
    ("夸赞/惊讶", G(r"可爱|好看|漂亮|厉害|牛逼|nb|无敌|真的吗|离谱|太强|好帅|好萌|萌|美|老婆|喜欢"), G(r"我去|我靠|我超|牛逼|真的假的|好厉害|好牛|太强|好萌|无敌了|好可爱|好耶"), None),
    ("即时反应", None, G(r"^(我去|我靠|我超|嘶|唉|啊+|呜+|哦+|哈+|好|好耶|好的|是|是的|不是|没有|对|牛逼|草|艹|真的假的|救命|笑死|好好好|哦哦哦|在|来了|晚安|早)[！!？?。…~～]*$"), None),
    ("高频短回", None, None, "freq_reply"),
    ("生活日常", G(r"早上|早安|晚安|睡|起床|醒|洗澡|出门|回家|回来了|在家|上班|下班|上课|放假|假期|周末|今天|明天|昨天|最近|日常|忙|闲"), None, None),
    ("工作学习", G(r"工作|上班|下班|老板|同事|公司|项目|客户|稿|接单|工资|赚钱|学校|上课|考试|作业|学习|论文|毕业|摸鱼"), None, None),
    ("吃饭饮食", G(r"吃饭|吃啥|吃什么|饭|外卖|奶茶|咖啡|可乐|饮料|好吃|饿|饱|火锅|烧烤|蛋糕|水果|零食|菜"), None, None),
    ("睡眠作息", G(r"睡|起床|醒|困|熬夜|睡觉|睡着|睡不着|早安|晚安"), None, None),
    ("家庭关系", G(r"爸爸|妈妈|父母|家人|家里|哥哥|姐姐|弟弟|妹妹|爷爷|奶奶|孩子|女儿|儿子|老婆|老公"), None, None),
    ("创作制作", G(r"画|画画|绘|稿|剪辑|剪|视频|动画|模型|建模|设计|海报|封面|稿子|制作|做图|写歌|小说|创作"), None, None),
    ("社交互动", G(r"朋友|好友|群|群里|群友|认识|聊天|联系|见面|约|聚会|同事|网友|粉丝|关注|转发|评论"), None, None),
]

PRIVATE_SCENES: list[tuple[str, re.Pattern | None, re.Pattern | None, str | None]] = [
    ("互损反顶", None, G(r"妈的|神经病|傻逼|弱智|滚|爬|捅死|杀了你|你妈的|tm|TM"), None),
    ("边界划线", None, G(r"^(我不要|不行|不想|别|不发|不理|不陪|算了|停|好了别说了|不想给|你自己)"), None),
    ("对方道歉/认真时的接法", G(r"对不起|抱歉|道歉|我的错|我错了|不好意思|说重了"), None, None),
    ("关怀对方", None, G(r"没事|摸摸|抱抱|注意|休息|别死|别哭|晚安|小心|吃饭|歇|好好|加油"), None),
    ("脆弱自述", None, G(r"好累|害怕|崩溃|疼|痛|睡不着|不舒服|想哭|难受|哭|焦虑|胃|鼻血|有点怕|好累"), None),
    ("称呼实证", None, G(r"(兄弟|哥们|老师|无名|大人|宝宝|哥)$"), None),
    ("设计/文件/图反馈", G(r"图|颜色|字体|剪影|排版|图层|海报|封面|logo|视频|剪|模型|建模|文件|压缩包|psd|png|jpg|透明|吸色", GI), None, None),
    ("直播事务", G(r"播|续舰|舰长|剪|视频|直播|时长|开播|下播|录"), None, None),
    ("任务确认/收到", None, G(r"^(好|好的|好的！|好！|ok|OK|可以|嗯|嗯嗯|收到|看到|看到了|明白|行|来了|在|弄完了|做完了)(兄弟|大人|老师|宝宝|哥们)?[！!。]?$"), None),
    ("生活日常", G(r"今天|明天|昨天|最近|早上|早安|晚安|起床|醒|睡|洗澡|出门|回家|在家|忙|闲"), None, None),
    ("工作学习", G(r"工作|上班|下班|老板|公司|项目|客户|稿|接单|工资|赚钱|学校|上课|考试|作业|学习|论文|毕业|摸鱼"), None, None),
    ("吃饭饮食", G(r"吃饭|吃啥|吃什么|饭|外卖|奶茶|咖啡|饮料|好吃|饿|饱|火锅|烧烤|蛋糕|水果|零食|菜"), None, None),
    ("睡眠作息", G(r"睡|起床|醒|困|熬夜|睡觉|睡着|睡不着|早安|晚安"), None, None),
    ("家庭关系", G(r"爸爸|妈妈|父母|家人|家里|哥哥|姐姐|弟弟|妹妹|爷爷|奶奶|孩子|女儿|儿子|老婆|老公"), None, None),
    ("创作制作", G(r"画|画画|绘|稿|剪辑|剪|视频|动画|模型|建模|设计|海报|封面|稿子|制作|做图|写歌|小说|创作"), None, None),
    ("社交互动", G(r"朋友|好友|群|群里|群友|认识|聊天|联系|见面|约|聚会|同事|网友|粉丝|关注|转发|评论"), None, None),
    ("日常闲聊", None, G(r"^.{1,25}$"), None),
]

ROUGH_TERMS = ("滚", "爬", "傻逼", "神经病", "有病", "弱智", "捅死", "妈的", "他妈", "闭嘴", "去死")
WARM_TERMS = ("谢谢", "辛苦", "宝宝", "摸摸", "抱抱", "没事", "休息", "老师", "喜欢", "爱你", "想你", "心疼", "乖")


# ── 基础工具 ────────────────────────────────────────────────────────────

def clean_text(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text.replace("\n", " / ")


def usable_text(text: str) -> bool:
    return bool(text) and not URL.search(text) and not LONG_NUM.search(text) and not CRED.search(text) and not BANNED.search(text)


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def file_sha16(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def beijing_hour(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(timezone(timedelta(hours=8))).hour


def content_words(text: str, limit: int = 30) -> list[str]:
    """粗粒度内容词: 连续中文 2-4 字滑窗取词频用, 过滤虚词字。"""
    words: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        cleaned = "".join(ch for ch in chunk if ch not in STOP_TERMS)
        if len(cleaned) >= 2:
            words.append(cleaned[:limit])
    return words


def load_rows(src: Path, scope: str) -> tuple[list[dict], dict[str, list[dict]]]:
    rows: list[dict] = []
    bursts: dict[str, list[dict]] = defaultdict(list)
    with src.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            row["reply"] = clean_text(row.get("typed_text"))
            trigger = clean_text(row.get("trigger_text"))
            row["trigger"] = "" if (PH.fullmatch(trigger) or PLACEHOLDER_BRACKET.fullmatch(trigger)) else trigger
            row["scope"] = scope
            if row["reply"] and not AUTOREPLY.match(row["reply"]):
                rows.append(row)
            bursts[str(row.get("burst_id") or "")].append(row)
    return rows, bursts


def burst_reply(row: dict, bursts: dict[str, list[dict]], max_member: int = 80, max_total: int = 160) -> str:
    members = [
        clean_text(m.get("typed_text"))
        for m in bursts[str(row.get("burst_id") or "")]
        if usable_text(clean_text(m.get("typed_text")))
    ]
    if not members:
        return ""
    if any(len(m) > max_member for m in members):
        return ""
    joined = " / ".join(members)
    return joined if len(joined) <= max_total else ""


# ── 分类与打分 ──────────────────────────────────────────────────────────

def classify(row: dict, trigger: str, reply: str, scenes: list[tuple[str, re.Pattern | None, re.Pattern | None, str | None]], freq_reply_counts: dict[str, int]) -> str:
    for name, tre, rre, cond in scenes:
        if tre is not None and not tre.search(trigger):
            continue
        if rre is not None and not rre.search(reply):
            continue
        if cond == "hour_0_5" and beijing_hour(int(row.get("timestamp") or 0)) not in range(0, 6):
            continue
        if cond == "min12" and len(reply) < 12:
            continue
        if cond == "echo":
            t_core, r_core = trigger.strip(), reply.strip()
            if len(t_core) < 3 or (t_core not in r_core and r_core not in t_core):
                continue
        if cond == "freq_reply" and (len(reply) > 20 or freq_reply_counts.get(reply, 0) < 8):
            continue
        return name
    return "未分类"


def quality_score(trigger: str, reply: str, count: int, gap_sec: float, burst_size: int, category: str) -> float:
    score = 0.5
    n = len(reply)
    if 1 <= n <= 6:
        score += 0.15
    elif n <= 13:
        score += 0.10
    elif n <= 30:
        score += 0.05
    elif n > 60:
        score -= 0.10
    score += min(0.2, 0.03 * count)  # 语料里反复出现的回复 = 招牌
    if 0 < gap_sec <= 30:
        score += 0.05
    if 2 <= len(trigger) <= 40:
        score += 0.05
    elif len(trigger) > 60:
        score -= 0.05
    if burst_size > 1:
        score += 0.05
    if category != "未分类":
        score += 0.05
    return round(max(0.0, min(1.0, score)), 4)


def mine_pairs(rows: list[dict], bursts: dict[str, list[dict]], scope: str, scenes: list, args) -> tuple[list[dict], Counter]:
    freq_reply_counts: Counter = Counter()
    for row in rows:
        if row["reply"] and len(row["reply"]) <= 20:
            freq_reply_counts[row["reply"]] += 1

    banned = GROUP_BANNED if scope == GROUP_SCOPE else None
    ordered = sorted(
        rows,
        key=lambda r: (0 if int(r.get("burst_size") or 1) == 1 else 1, len(r["trigger"]), int(r.get("timestamp") or 0)),
    )
    merged: dict[tuple[str, str], dict] = {}
    seen_bursts: set[str] = set()
    for row in ordered:
        trigger, reply = row["trigger"], row["reply"]
        if not trigger or not usable_text(trigger) or not usable_text(reply):
            continue
        if banned is not None and (banned.search(trigger) or banned.search(reply)):
            continue
        if len(trigger) > 80:
            continue
        bid = str(row.get("burst_id") or "")
        if bid in seen_bursts:
            continue
        full_reply = burst_reply(row, bursts)
        if not full_reply:
            continue
        seen_bursts.add(bid)
        category = classify(row, trigger, full_reply, scenes, freq_reply_counts)
        key = (trigger[:60], full_reply)
        entry = merged.get(key)
        if entry is None:
            merged[key] = {
                "trigger": trigger[:60],
                "reply": full_reply,
                "scope": scope,
                "category": category,
                "count": 1,
                "ts": int(row.get("timestamp") or 0),
                "gap_sec": float(row.get("reply_gap_sec") or 0),
                "burst_size": int(row.get("burst_size") or 1),
                "trigger_name": str(row.get("trigger_name") or ""),
            }
        else:
            entry["count"] += 1

    records: list[dict] = []
    for entry in merged.values():
        score = quality_score(entry["trigger"], entry["reply"], entry["count"], entry["gap_sec"], entry["burst_size"], entry["category"])
        entry["quality"] = score
        reply_len = len(entry["reply"])
        entry["reply_len"] = reply_len
        entry["echo"] = bool(reply_len <= 8 and entry["count"] >= 2 and len(entry["trigger"]) <= 30)
        records.append(entry)

    # 限流: per-reply 冒尖截断 + per-category 多样性上限 + 总配额
    records.sort(key=lambda r: (-r["quality"], -r["count"], r["ts"], r["trigger"], r["reply"]))
    per_reply: Counter = Counter()
    per_category: Counter = Counter()
    kept: list[dict] = []
    max_pairs = args.max_group_pairs if scope == GROUP_SCOPE else args.max_private_pairs
    for record in records:
        if len(kept) >= max_pairs:
            break
        if per_reply[record["reply"]] >= args.max_per_reply:
            continue
        if per_category[record["category"]] >= args.max_per_category:
            continue
        per_reply[record["reply"]] += 1
        per_category[record["category"]] += 1
        kept.append(record)

    kept.sort(key=lambda r: (-r["quality"], -r["count"], r["ts"], r["trigger"], r["reply"]))
    for record in kept:
        raw = "\x1f".join((record["scope"], record["category"], record["trigger"], record["reply"]))
        record["uid"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        # echo 放宽: 同 trigger 复现, 或 reply 本身是口头禅级高频短回
        if record["reply_len"] <= 8 and len(record["trigger"]) <= 30:
            if record["count"] >= 2 or freq_reply_counts.get(record["reply"], 0) >= 6:
                record["echo"] = True
    return kept, freq_reply_counts


# ── 风格统计 ────────────────────────────────────────────────────────────

def build_style_stats(rows: list[dict], scope_stats: dict, freq_reply_counts: Counter) -> dict:
    lengths: list[int] = []
    no_punct_anywhere = 0
    trailing_punct = 0
    face_msgs = 0
    emoji_msgs = 0
    fadian = 0
    burst_msgs = 0
    for row in rows:
        reply = row.get("reply") or ""
        if not reply:
            continue
        lengths.append(len(reply))
        if not any(ch in reply for ch in TRAIL_PUNCT):
            no_punct_anywhere += 1
        if reply[-1] in TRAIL_PUNCT:
            trailing_punct += 1
        if row.get("has_face"):
            face_msgs += 1
        if EMOJI_RE.search(reply):
            emoji_msgs += 1
        if FADIAN_RE.search(reply):
            fadian += 1
        if int(row.get("burst_size") or 1) > 1:
            burst_msgs += 1
    lengths.sort()
    n = max(1, len(lengths))

    def pct(p: float) -> int:
        return lengths[min(len(lengths) - 1, int(len(lengths) * p))] if lengths else 0

    buckets = {"1": 0, "2-3": 0, "4-6": 0, "7-13": 0, "14-30": 0, "31-60": 0, "60+": 0}
    for value in lengths:
        if value <= 1:
            buckets["1"] += 1
        elif value <= 3:
            buckets["2-3"] += 1
        elif value <= 6:
            buckets["4-6"] += 1
        elif value <= 13:
            buckets["7-13"] += 1
        elif value <= 30:
            buckets["14-30"] += 1
        elif value <= 60:
            buckets["31-60"] += 1
        else:
            buckets["60+"] += 1

    top_replies = [
        {"t": text, "c": int(count)}
        for text, count in sorted(freq_reply_counts.items(), key=lambda item: (-item[1], item[0]))[:400]
        if count >= 3
    ]
    return {
        "text_messages": len(lengths),
        "reply_len": {
            "median": pct(0.5), "p25": pct(0.25), "p75": pct(0.75),
            "p90": pct(0.90), "p95": pct(0.95), "p99": pct(0.99),
            "mean": round(sum(lengths) / n, 2) if lengths else 0,
        },
        "len_buckets": buckets,
        "len_bucket_ratios": {key: round(value / n, 4) for key, value in buckets.items()},
        "no_punct_anywhere_ratio": round(no_punct_anywhere / n, 4),
        "trailing_punct_ratio": round(trailing_punct / n, 4),
        "face_ratio": round(face_msgs / n, 4),
        "emoji_ratio": round(emoji_msgs / n, 4),
        "fadian_ratio": round(fadian / n, 4),
        "burst_ratio": round(burst_msgs / n, 4),
        "top_replies": top_replies,
        "scope_note": scope_stats,
    }


# ── per-user 画像 ───────────────────────────────────────────────────────

def build_user_profiles(rows: list[dict], scope: str, min_triggers: int) -> list[dict]:
    by_user: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        name = str(row.get("trigger_name") or "").strip()
        # 排除机机自己的账号名 (续聊/自回复时 trigger 会记成自己)
        if name and row.get("reply") and not GROUP_BANNED.search(name):
            by_user[name].append(row)
    profiles: list[dict] = []
    for name in sorted(by_user):
        user_rows = by_user[name]
        if len(user_rows) < min_triggers:
            continue
        replies = [r["reply"] for r in user_rows]
        rough = sum(any(t in reply for t in ROUGH_TERMS) for reply in replies)
        warm = sum(any(t in reply for t in WARM_TERMS) for reply in replies)
        fadian = sum(bool(FADIAN_RE.search(reply)) for reply in replies)
        addresses = Counter(
            m.group(1)
            for reply in replies
            for m in (ADDRESS_RE.search(reply),)
            if m
        )
        terms: Counter = Counter()
        for row in user_rows:
            terms.update(content_words(row["trigger"], 12))
        samples = sorted(
            ({ "trigger": r["trigger"][:60], "reply": r["reply"][:120], "ts": int(r.get("timestamp") or 0)}
             for r in user_rows if usable_text(r["reply"])),
            key=lambda item: (-len(item["reply"]), item["ts"]),
        )
        seen_replies: set[str] = set()
        diverse_samples: list[dict] = []
        for sample in samples:
            if sample["reply"] in seen_replies:
                continue
            seen_replies.add(sample["reply"])
            diverse_samples.append(sample)
            if len(diverse_samples) >= 4:
                break
        n = len(user_rows)
        rough_ratio = round(rough / n, 4)
        warm_ratio = round(warm / n, 4)
        if rough_ratio >= 0.18:
            register = "互损"
        elif warm_ratio >= 0.25:
            register = "软"
        else:
            register = "日常"
        timestamps = [int(r.get("timestamp") or 0) for r in user_rows if int(r.get("timestamp") or 0) > 0]
        profiles.append({
            "name": name,
            "scope": scope,
            "triggers": n,
            "first_ts": min(timestamps) if timestamps else 0,
            "last_ts": max(timestamps) if timestamps else 0,
            "avg_reply_len": round(sum(len(reply) for reply in replies) / n, 1),
            "rough_ratio": rough_ratio,
            "warm_ratio": warm_ratio,
            "fadian_ratio": round(fadian / n, 4),
            "register": register,
            "addresses": [a for a, _ in addresses.most_common(6)],
            "top_terms": [t for t, _ in terms.most_common(8)],
            "samples": diverse_samples,
        })
    profiles.sort(key=lambda p: (-p["triggers"], p["name"]))
    return profiles


def build_private_profile(rows: list[dict]) -> list[dict]:
    """私聊只有一个对方: 用机机自己的发言做画像 (trigger_name 不可靠)。"""
    replies = [row["reply"] for row in rows if row.get("reply")]
    if not replies:
        return []
    n = len(replies)
    rough = sum(any(t in reply for t in ROUGH_TERMS) for reply in replies)
    warm = sum(any(t in reply for t in WARM_TERMS) for reply in replies)
    fadian = sum(bool(FADIAN_RE.search(reply)) for reply in replies)
    addresses = Counter(m.group(1) for reply in replies for m in (ADDRESS_RE.search(reply),) if m)
    timestamps = [int(row.get("timestamp") or 0) for row in rows if int(row.get("timestamp") or 0) > 0]
    register = "互损" if rough / n >= 0.18 else ("软" if warm / n >= 0.25 else "日常")
    return [{
        "name": "私聊对方",
        "scope": PRIVATE_SCOPE,
        "triggers": n,
        "first_ts": min(timestamps) if timestamps else 0,
        "last_ts": max(timestamps) if timestamps else 0,
        "avg_reply_len": round(sum(len(reply) for reply in replies) / n, 1),
        "rough_ratio": round(rough / n, 4),
        "warm_ratio": round(warm / n, 4),
        "fadian_ratio": round(fadian / n, 4),
        "register": register,
        "addresses": [a for a, _ in addresses.most_common(6)],
        "top_terms": [],
        "samples": [],
    }]


# ── 主流程 ──────────────────────────────────────────────────────────────

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="机机语料全量挖掘流水线")
    parser.add_argument("--out-dir", default=str(OUT_DIR_DEFAULT))
    parser.add_argument("--group-src", default=str(GROUP_SRC))
    parser.add_argument("--private-src", default=str(PRIVATE_SRC))
    parser.add_argument("--max-group-pairs", type=int, default=20000)
    parser.add_argument("--max-private-pairs", type=int, default=4000)
    parser.add_argument("--max-per-reply", type=int, default=30)
    parser.add_argument("--max-per-category", type=int, default=1500)
    parser.add_argument("--min-user-triggers", type=int, default=5)
    args = parser.parse_args(argv)

    group_src = Path(args.group_src)
    private_src = Path(args.private_src)
    if not group_src.exists() or not private_src.exists():
        print(f"缺少中间语料文件: {group_src} / {private_src}\n先跑 python tmp/fdj_extract.py 生成", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    group_rows, group_bursts = load_rows(group_src, GROUP_SCOPE)
    private_rows, private_bursts = load_rows(private_src, PRIVATE_SCOPE)

    group_pairs, group_freq = mine_pairs(group_rows, group_bursts, GROUP_SCOPE, GROUP_SCENES, args)
    private_pairs, private_freq = mine_pairs(private_rows, private_bursts, PRIVATE_SCOPE, PRIVATE_SCENES, args)

    all_pairs = group_pairs + private_pairs
    pairs_text = "\n".join(
        json.dumps(
            {
                "uid": p["uid"], "scope": p["scope"], "category": p["category"],
                "trigger": p["trigger"], "reply": p["reply"], "count": p["count"],
                "quality": p["quality"], "reply_len": p["reply_len"], "echo": p["echo"],
                "ts": p["ts"], "trigger_name": p["trigger_name"],
            },
            ensure_ascii=False, separators=(",", ":"),
        )
        for p in all_pairs
    ) + "\n"

    style_stats = {
        "group": build_style_stats(group_rows, {"rows": len(group_rows)}, group_freq),
        "private": build_style_stats(private_rows, {"rows": len(private_rows)}, private_freq),
    }
    user_profiles = build_user_profiles(group_rows, GROUP_SCOPE, args.min_user_triggers)
    user_profiles.extend(build_private_profile(private_rows))
    profiles_text = "\n".join(json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in user_profiles) + "\n"

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "group": {"path": str(group_src), "sha256_16": file_sha16(group_src), "rows": len(group_rows)},
            "private": {"path": str(private_src), "sha256_16": file_sha16(private_src), "rows": len(private_rows)},
        },
        "outputs": {
            "scene_pairs": {"total": len(all_pairs), "group": len(group_pairs), "private": len(private_pairs),
                            "echo_eligible": sum(1 for p in all_pairs if p["echo"])},
            "style_stats": {"group_top_replies": len(style_stats["group"]["top_replies"]),
                            "private_top_replies": len(style_stats["private"]["top_replies"])},
            "user_profiles": len(user_profiles),
        },
        "limits": vars(args),
    }

    atomic_write(out_dir / "scene_pairs.jsonl", pairs_text)
    atomic_write(out_dir / "style_stats.json", json.dumps(style_stats, ensure_ascii=False, indent=1) + "\n")
    atomic_write(out_dir / "user_profiles.jsonl", profiles_text)
    atomic_write(out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")

    print(f"[mine_fadianji_corpus] scene_pairs: 群 {len(group_pairs)} + 私 {len(private_pairs)} = {len(all_pairs)} "
          f"(echo 候选 {manifest['outputs']['scene_pairs']['echo_eligible']})")
    print(f"[mine_fadianji_corpus] user_profiles: {len(user_profiles)} (群 top: "
          + ", ".join(f"{p['name']}({p['triggers']})" for p in user_profiles[:5]) + ")")
    print(f"[mine_fadianji_corpus] 群风格: 中位 {style_stats['group']['reply_len']['median']} 字 / "
          f"无标点 {style_stats['group']['no_punct_anywhere_ratio']:.1%} / 发癫 {style_stats['group']['fadian_ratio']:.1%}")
    print(f"[mine_fadianji_corpus] 输出目录: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
