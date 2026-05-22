"""入向消息意图本地多标签分类 —— 让笨猫一眼看出群友"想干什么"。

设计目标:
- 纯规则(关键词 + 正则),零网络,O(n)。每条消息平均判定 < 0.1 ms。
- **多标签**:一条消息可同时是 question + tease_cat;不强行 mutex。
- **结果给主 AI 当 system prompt 提示**,而不是替 AI 决策。
- 关键词表保守,宁可漏(让 AI 自己读原文)别误(贴错标签反误导)。

意图清单(每个都自带反应方向建议):
| tag                    | 触发                              | 反应方向                      |
|------------------------|----------------------------------|------------------------------|
| question               | 问号 / 疑问词                    | 给答案 / 反问                |
| command_to_cat         | "帮我/给我看看/猫猫你"+动词      | 真的去执行(可能要调 tool)    |
| greeting               | 你好/早安/晚安/嗨                 | 招呼回应 + 时段(可叠 catty_now)|
| thanks                 | 谢谢/感谢/3q                     | 谦虚撒娇                     |
| apology                | 对不起/抱歉                      | 软软接住,不要计较             |
| compliment_cat         | 猫猫真可爱/好厉害/喜欢猫猫        | 害羞 + 傲娇                  |
| tease_cat              | 调戏意味:抱抱/亲亲/摸摸/舔/...   | 脸红炸毛 + 嘴硬               |
| complaint              | 烦/累/操蛋/破防/抱怨语气          | 顺毛安抚,别讲道理             |
| seeking_image          | 来张图/发个表情/想看 X 图         | 走 emoji 或 catty_meme_query  |
| info_request           | 是什么 / 怎么 / 多少              | 给信息,可以调 tool            |
| interjection_only      | 纯感叹: "啊"、"哦"、"6"、"草"     | 短回应,不要过度解读            |

每条消息最多打 ~3 个标签,避免上下文爆炸。
"""
from __future__ import annotations

import re
from typing import Iterable


# ── 关键词集合 ─────────────────────────────────────────────────────

# 疑问词(出现任意一个即标 question;问号也算)
_QUESTION_WORDS = (
    "什么", "为什么", "怎么", "怎样", "哪", "几", "多少",
    "吗", "呢", "?", "?", "是不是", "对不对", "可以", "能不能",
    "有没有", "有么", "在不在",
)

# 命令式(让猫猫做事 / 主动触发)
_COMMAND_PATTERNS = (
    re.compile(r"猫猫[,，\s]*你?(去|帮|给|做|查|搜|看看|认识|告诉|说一下|介绍|画|写|算|生成)"),
    re.compile(r"(笨猫|catty)[,，\s]*(去|帮|给|做|查|搜|看看|说|介绍)"),
    re.compile(r"^(帮|给|让)我"),
    re.compile(r"(发|找|来|给我)(一?张|一?个|个)"),
    re.compile(r"(查一?下|搜一?下|看一?下|看看|认识吗)"),
)

# 招呼
_GREETING_WORDS = (
    "你好", "您好", "嗨", "hi", "hello", "哈喽", "哈罗",
    "早安", "早上好", "早", "中午好", "下午好", "晚上好", "晚安",
    "好久不见", "在吗",
)

# 感谢
_THANKS_WORDS = ("谢谢", "感谢", "多谢", "thx", "thanks", "thank", "3q", "栓q", "谢啦", "蟹蟹", "蟹蟹猫")

# 道歉
_APOLOGY_WORDS = ("对不起", "抱歉", "sorry", "不好意思", "我错了", "我道歉")

# 夸猫(必须主语指猫,否则容易和"看 X 真可爱"误判)
_COMPLIMENT_CAT_PATTERNS = (
    re.compile(r"(猫猫|笨猫|catty|喵)(真|好|超|太|这么|这么的|多么)?(可爱|聪明|厉害|强|乖|萌|好看)"),
    re.compile(r"(喜欢|爱)(死)?(你|猫猫|笨猫)"),
    re.compile(r"(你|猫猫|笨猫|catty)(怎么|这么)(这么)?(可爱|乖|聪明|厉害|懂事)"),
)

# 撩/调戏(触发猫娘害羞反应)
_TEASE_CAT_PATTERNS = (
    re.compile(r"(抱抱|亲亲|摸摸|揉揉|蹭蹭|捏捏|戳戳)(你?|猫猫|尾巴|耳朵|肚子|爪)"),
    re.compile(r"(舔|啃|咬)(你?|猫猫|尾巴|爪)"),
    re.compile(r"(给我|让我|想)(抱|亲|摸|揉|蹭|捏|舔|啃)"),
    re.compile(r"(吸|嗦|揉)(猫|喵)"),
)

# 抱怨/吐槽
_COMPLAINT_WORDS = (
    "好累", "累死", "好烦", "烦死", "操蛋", "无语", "破防", "气死",
    "心累", "崩溃", "完蛋", "完了", "炸了", "受不了", "受够",
    "蚌埠住", "麻了", "气炸",
)

# 求图/求表情
_SEEKING_IMAGE_PATTERNS = (
    re.compile(r"(发|来|给|想看|要)(一?张|一?个|几张|个|的)?(图|表情|gif|pic|照片|截图)"),
    re.compile(r"(有没有|有么).{0,8}(图|表情|gif|pic)"),
    re.compile(r"(整一?张|来一?张|发一?张).{0,4}图"),
)

# 信息请求(比 question 更具体的"找事实")
_INFO_REQUEST_PATTERNS = (
    re.compile(r"(是什么|是啥|啥意思|什么意思|啥来着)"),
    re.compile(r"(多少钱|怎么用|怎么玩|怎么走|怎么去)"),
    re.compile(r"^(谁|什么|怎么)"),
)

# 纯感叹/无实质内容(短文本 + 全是语气词/表情)
_INTERJECTION_TOKENS = {
    "啊", "啊啊", "哦", "嗯", "呃", "唉", "哈", "哈哈", "哈哈哈", "嘿",
    "诶", "哎", "嗷", "呀", "哇", "靠", "卧槽", "wc", "wtf",
    "6", "666", "草", "笑死", "xs", "绷", "绷不住",
}

# emoji/颜文字粗筛(出现就更可能是 interjection)
_EMOJI_PUNCT_RE = re.compile(r"[\.\!\?！？\s　~\-_]+")


def _contains_any(text: str, words: Iterable[str]) -> bool:
    return any(w in text for w in words)


def _matches_any(text: str, patterns: Iterable[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def _is_pure_interjection(text: str) -> bool:
    """超短且只包含语气词/标点/表情 → 纯感叹。"""
    stripped = (text or "").strip().lower()
    if not stripped or len(stripped) > 12:
        return False
    # 去掉标点/空白后看是不是命中 _INTERJECTION_TOKENS 之一(整段或重复字符)
    bare = _EMOJI_PUNCT_RE.sub("", stripped)
    if not bare:
        return True  # 全是标点/表情符号 → 也算 interjection
    if bare in _INTERJECTION_TOKENS:
        return True
    # 允许 "啊啊啊啊"/"哈哈哈哈哈" 这种重复
    if len(set(bare)) == 1 and len(bare) <= 8:
        return True
    return False


def classify_intent(text: str, *, has_image: bool = False) -> list[str]:
    """对单条消息打多个意图标签,按显著性排序返回。

    显著性:command > tease > compliment > thanks > apology > complaint > seeking_image >
            greeting > question > info_request > interjection_only

    返回最多 3 个标签;无命中返回 []。
    """
    if not text or not isinstance(text, str):
        return []
    lowered = text.lower()
    tags: list[str] = []

    # 先做高优先级"具体动作类"
    if _matches_any(text, _COMMAND_PATTERNS):
        tags.append("command_to_cat")
    if _matches_any(text, _TEASE_CAT_PATTERNS):
        tags.append("tease_cat")
    if _matches_any(text, _COMPLIMENT_CAT_PATTERNS):
        tags.append("compliment_cat")
    if _contains_any(lowered, _THANKS_WORDS):
        tags.append("thanks")
    if _contains_any(lowered, _APOLOGY_WORDS):
        tags.append("apology")
    if _contains_any(text, _COMPLAINT_WORDS):
        tags.append("complaint")
    if _matches_any(text, _SEEKING_IMAGE_PATTERNS):
        tags.append("seeking_image")

    # 招呼词:必须开头出现或独立成段才算 greeting,避免"hi 我想问下"被纯算成 greeting
    # 简化判定:文本短(<= 20 字) + 出现招呼词
    if len(text) <= 20 and _contains_any(lowered, _GREETING_WORDS):
        tags.append("greeting")

    # question:有 ? 或 ? 或疑问词
    if _contains_any(text, ("?", "？")) or _contains_any(text, _QUESTION_WORDS):
        tags.append("question")

    # info_request 是 question 的细分
    if _matches_any(text, _INFO_REQUEST_PATTERNS) and "question" not in tags:
        tags.append("question")
    if _matches_any(text, _INFO_REQUEST_PATTERNS):
        tags.append("info_request")

    # 如果到这里还是空,且消息是纯感叹 → interjection_only
    if not tags and _is_pure_interjection(text):
        tags.append("interjection_only")

    # 去重保序,最多 3 个
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= 3:
            break
    return out


# ── 反应方向提示表(给主 AI 看) ─────────────────────────────────────

_TAG_HINTS = {
    "command_to_cat": "群友在让你做事,真的去执行(可能要调相关 tool);别只装萌不响应",
    "tease_cat": "撩你的话,标准反应链=脸红炸毛→嘴硬拒绝→小声暴露真心(规则 5)",
    "compliment_cat": "夸你,先傲娇『哼』再不自觉开心,别上来就甜美接住",
    "thanks": "群友谢你,谦虚撒娇,『不、不用谢喵』之类",
    "apology": "群友道歉,软软接住别计较,『没事啦』『主人不要紧的』",
    "complaint": "群友在抱怨/破防,先顺毛安抚,别讲道理别教育,『主人摸摸~』『辛苦了喵』",
    "seeking_image": "群友想看图/表情,本地表情库走 EMOJI_QUERY 或 catty_meme_query;别只回文字",
    "greeting": "招呼语,带时段招呼回去(深夜/早上/午饭可叠 catty_now 信号)",
    "question": "有疑问要给答案;真不知道再调 tool 或承认猫猫不懂",
    "info_request": "在找具体信息,别只给情绪反应;不确定就 catty_web_search / catty_meme_explain",
    "interjection_only": "纯感叹/语气,**短回应**(一句以内)不要过度展开",
}


def build_intent_context(text: str, *, has_image: bool = False) -> str:
    """主回复链路用:返回一段 system prompt 文本。无标签返回空。"""
    tags = classify_intent(text, has_image=has_image)
    if not tags:
        return ""
    # 拼标签 + 反应方向
    lines = []
    for tag in tags:
        hint = _TAG_HINTS.get(tag, "")
        if hint:
            lines.append(f"`{tag}`→{hint}")
    if not lines:
        return ""
    return "群友消息意图标签(供你定回应方向,不要在回复里复读): " + "; ".join(lines) + "。"
