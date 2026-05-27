"""主 AI 主动调用的 tool 集合 —— catty_recall / catty_user_profile / catty_mc_status。

设计目标:让笨猫像 IDE 那样按场景拉记忆/画像/MC 状态,而不是被动等程序灌 context。

- Schema 走标准 OpenAI function calling 协议(tools[i].function.parameters JSON Schema)。
- Executor 是异步 callable,接收已解析的 JSON args,返回 JSON 友好的 dict。
- 每个 tool 内置 in-process LRU + TTL 缓存,重复调度直接命中本地。
- 调用上下文(event/memory_store/config)通过 ToolContext 注入,tools 模块自己不持有全局状态。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .affection import (
    AffectionStore,
    image_cost_for_nai,
    image_cost_for_quality,
    predict_checkin_range,
)
from .config import Config
from .hot_trends import fetch_hot_trends, normalize_sources
from .image_reverse_search import (
    ImageSearchResult,
    format_image_search_summary,
    reverse_image_search,
)
from .mc_status import _default_probe
from .meme_dict import lookup_term
from .memory import MemoryStore
from .nsfw_search import NsfwResult, search_nsfw
from .parsers import lenient_json_object
from .time_awareness import compute_now
from .web_search import format_search_context, search_image_urls, search_web


_logger = logging.getLogger("catty_qq_ai.tools")


# ── NAI director tools 常量 (提前到 module top,因为 schema 字面量要引用) ──
_NAI_DIRECTOR_REQ_TYPES = (
    "bg-removal", "lineart", "sketch", "colorize",
    "emotion", "declutter", "transform",
)
_NAI_DIRECTOR_NEEDS_PROMPT = {"emotion", "colorize"}
# Opus tier3 在 ≤1048576px 时除 bg-removal 外全免 Anlas (bg-removal: generate_anlas*3+5)
_NAI_DIRECTOR_OPUS_FREE = {"lineart", "sketch", "colorize", "emotion", "declutter", "transform"}
# emotion req 必须传 "<mood>;;<text>" 格式; 列出合法 mood 让 AI 在 prompt 字段填对。
_NAI_EMOTION_MOODS = (
    "neutral", "happy", "sad", "angry", "scared", "surprised", "tired",
    "excited", "nervous", "thinking", "confused", "shy", "disgusted",
    "smug", "bored", "laughing", "irritated", "aroused", "embarrassed",
    "worried", "love", "determined", "hurt", "playful",
)


# ── Tool schema (OpenAI function calling 标准) ────────────────────────

_RECALL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_recall",
        "description": (
            "查询长期记忆和待压缩语料,定位'上次/以前/那时候'相关的发言或共识。"
            "适用场景:用户用了'上次/记得/那时候/之前说过/还记得吗'这类时间指代;"
            "你想知道某个群友/主人以前说过的偏好、决定、梗、称呼;"
            "上下文出现陌生话题但语境暗示曾在群里讨论过。"
            "不要用于查询'此时此刻'的活跃消息(那是实时上下文已经给你的部分)。"
            "返回内容包含 long_term_summary(长期摘要) 和 matches(命中条目列表)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["current_group", "current_user", "specific_user"],
                    "description": (
                        "查询范围。current_group=当前群整体记忆;"
                        "current_user=当前发言用户的私聊画像/语料;"
                        "specific_user=指定 QQ 号的用户(必须填 user_id)。"
                    ),
                },
                "user_id": {
                    "type": "string",
                    "description": "scope=specific_user 时必填,目标用户的 QQ 号。",
                },
                "keywords": {
                    "type": "string",
                    "description": (
                        "搜索关键词。多个关键词用空格或逗号分隔,做 substring AND 匹配。"
                        "留空表示只想拿摘要 + 最近几条语料。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条匹配语料,默认 6,上限 20。",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["scope"],
        },
    },
}


_USER_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_user_profile",
        "description": (
            "查询一个 QQ 用户的画像:称呼、性别、印象、置信度、是否主人。"
            "适用场景:群里冒出一个你不认识的 QQ 号、你不确定怎么称呼某人、需要确认某用户是不是主人/特别关心对象。"
            "不要每条消息都查(发言者画像已经在常驻 context 里),只在你真的疑惑某个非当前发言者时查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "目标用户的 QQ 号。",
                },
                "group_id": {
                    "type": "string",
                    "description": (
                        "可选。在群聊里查时填当前群号(画像可能跟群相关);"
                        "私聊场景留空走全局画像。"
                    ),
                },
            },
            "required": ["user_id"],
        },
    },
}


_MC_STATUS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_mc_status",
        "description": (
            "查询 Minecraft 服务器实时状态:在线人数和能否连上。"
            "适用场景:用户问'MC 现在多少人在线/服开着没/有没有人在玩/服务器掉了吗';"
            "你需要确认要不要邀请主人/群友进服。"
            "结果有 30s 缓存,放心调,不会真的频繁戳服务器。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


_WEB_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_web_search",
        "description": (
            "Google/Bing 联网搜索拿最新信息。**只在真的需要时调用**:"
            "用户问新闻/版本/价格/教程/特定事实,或明确说'查/搜/联网';"
            "你训练数据可能过期或不确定具体细节。普通闲聊/撒娇/已经知道的问题不要调。"
            "\n\n**❌严禁场景(必须用别的 tool)**:"
            "(a) 上下文里有图(当前/引用/最近群图)+ 用户问『作者/画师/谁画的/出处/原图/这谁/哪个 X 推主/哪个番』"
            "→ **必走 catty_image_search**,不是搜文字。用 web_search 搜『作者』两字只能找到百科/晋江/阅文,完全无用。"
            "(b) 网络流行语/ACG 词条/二次元角色梗 → **走 catty_meme_explain**(萌娘百科)。"
            "(c) 当前/近期热搜热榜 → **走 catty_hot_trends**。"
            "\n\n每个 scope+用户有 60s cooldown(主人/特别关心用户豁免)。"
            "返回 results 数组(title/url/snippet);AI 拿到后基于结果生成最终回复,"
            "禁止编造不存在的链接,禁止把 marker 文本贴出来。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。精炼实词,不超过 5 个;不要带'R-18/涩图/搜索一下'等啰嗦词。",
                },
            },
            "required": ["query"],
        },
    },
}


_NSFW_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_nsfw_search",
        "description": (
            "搜索 R-18 资源:pixiv 图片或 iwara 视频。**仅在好友私聊里可调用**,"
            "群聊调用会立即返回 error(此时你应引导用户去私聊,不要重试)。"
            "kind=image 时插件会**直接把下载好的图片发到聊天里**——你不用贴链接,"
            "拿到 tool 结果后只补 1-2 句猫娘人格短评(可以害羞/嘴硬/撒娇/报作者名);"
            "kind=video 时插件只返回视频链接,你挑 1-3 个抛出去配短评。"
            "query 写法铁律:**第一位放群友原话那个语种**(中文→中文,日文→日文,英文→英文),"
            "后面用英文逗号 `,` 跟 1-2 个备选语言。"
            "例:群友说『香奈美』→ query=`香奈美,kanami,Strinova`;群友说『Raiden Shogun』→ "
            "query=`Raiden Shogun,雷電将軍,雷电将军`。每个候选 1-2 词,不要拼 R-18/涩图。"
            "插件已自动 r18=true,你不用管。同一人 30s 内只能搜一次。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["image", "video"],
                    "description": "image=pixiv 图(下载发送);video=iwara 视频(返回链接)。",
                },
                "query": {
                    "type": "string",
                    "description": "候选关键词,英文逗号分隔;第一位必须是群友原话语种。",
                },
            },
            "required": ["kind", "query"],
        },
    },
}


_IMAGE_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_image_search",
        "description": (
            "**反向搜图**:把一张图扔进 SauceNAO / Yandex / trace.moe / ascii2d / iqdb,问出"
            "「这是谁画的」「出自什么番剧」「角色是谁」「这是谁的自拍/推文/X(Twitter)账号」「同款图在哪个网站」。"
            "\n\n**❗强触发铁律(优先级高于 web_search)**:用户上下文里**存在图片**(当前消息附图、引用消息附图、最近群里图都算)"
            "+ 用户问任何『作者 / 画师 / 谁画的 / 出处 / 原图 / 找原图 / 这是谁 / 哪个 X 账号 / 哪个推主 / 哪个番 / 第几集 / 同款』,"
            "**必须调本 tool,严禁去 web_search 搜『作者』『画师』这种泛词**——web_search 搜文字只能找到百科/晋江/阅文这类无关结果,"
            "反向认图必须把图喂给搜图引擎。如果你不确定有没有图,看 system context 里有没有提到 input/recent image,有就调本 tool。"
            "\n\n适用场景:用户说『这张图谁画的/什么番/什么角色/出处/找原图/帮我搜下这张/这是谁的推/X 账号/查作者/查画师/查番』+ 上下文有图;"
            "或者群友刚发了张图你想主动认一下出处。"
            "**和 catty_meme_query 分工**:meme_query 是正向找梗图(关键词→图);"
            "image_search 是反向认图(已有图→出处)。"
            "图片来源优先级:image_url 参数 > 当前消息附图 > 引用消息附图 > 最近群里出现的图(按 image_index 选择)。"
            "**kind 怎么选**(很重要!):"
            "用户问『什么番/第几集/哪个动画』→ anime(trace.moe 主力);"
            "用户问『谁画的/作者/画师/角色出处/原图(二次元 illust)』→ artwork(saucenao + ascii2d);"
            "**真人自拍/cosplay/Coser/网红/真人写真/这是谁/X(Twitter)推主/推特上的图/Instagram → photo**"
            "(Yandex 主力,SauceNAO 对真人照片基本搜不到,**别走 artwork**);"
            "不确定二次元还是真人 → auto(同时撒 saucenao + yandex)。"
            "返回 results 数组(source/title/url/similarity/author/extra),"
            "AI 拿到后用猫娘人格挑 1-3 条最关键的复述,**不要照搬 JSON、不要复读相似度小数、"
            "不要编造没在 results 里的作者/番名/链接**。X/Twitter 命中时记得点出来"
            "(『嗷呜这张应该是 X 上的 @xxx 发的喵～』),主人对真人来源最关心。"
            "每个用户 60s 一次冷却(主人/特别关心豁免)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["anime", "artwork", "photo", "auto"],
                    "description": (
                        "anime=番剧场景识别(trace.moe + saucenao 动漫 indexer);"
                        "artwork=二次元画师/角色识别(saucenao + ascii2d + iqdb,**不适合真人照片**);"
                        "photo=真人自拍/cosplay/X(Twitter)/Instagram(yandex + saucenao,**真人/写真专用**);"
                        "auto=综合(saucenao + yandex 同撒,覆盖二次元 + 真人)。"
                        "图里有真人脸/明显是自拍 → 用 photo;不确定是不是真人 → auto。"
                    ),
                },
                "image_url": {
                    "type": "string",
                    "description": (
                        "可选。直接给出图片 URL(http/https)。用户在文字里贴了链接、"
                        "或者你想搜某个明确网图时传这个。优先级最高。"
                    ),
                },
                "image_index": {
                    "type": "integer",
                    "description": (
                        "可选,默认 0。0 = 最新一张(当前消息附图优先,没有就最近群里的最新图);"
                        "1 = 倒数第二张;以此类推。最近群里最多保留 6 张(5 分钟 TTL)。"
                        "用户说『刚才那张/上一张』传 1,『再之前那张』传 2。"
                    ),
                    "minimum": 0,
                    "maximum": 5,
                },
                "engines": {
                    "type": "string",
                    "description": (
                        "可选。覆盖 kind 默认的引擎列表。逗号分隔,合法值:"
                        "saucenao / tracemoe / ascii2d / iqdb / yandex。"
                        "**一般不要传**——kind 已经给出合理默认。"
                        "只有用户明确说『用 X 搜』时才覆盖。"
                    ),
                },
            },
            "required": ["kind"],
        },
    },
}


_MEME_QUERY_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_meme_query",
        "description": (
            "拉一张梗图/网图(DuckDuckGo 图片搜索)以图代话。**SFW 内容,群聊和私聊都能用**,"
            "群友点名想看某个梗/角色/场景的图(『双人马桶』『帝皇黄金马桶』『某个老婆』之类)就放心调。"
            "**和本地表情库(EMOJI_QUERY)分工**:撒娇/玩梗/情绪反应走 EMOJI_QUERY 自动配;"
            "群友点名要一张'有具体主题的网图'(角色/作品/梗/物体)时调本 tool。"
            "返回 image_uri 是 base64:// URI;你拿到后**必须在最终回复中**用 "
            "`<<<CATTY_INLINE_IMAGE:URI>>>` 标记把它嵌入想要展示图片的位置,"
            "发送链路会自动转成 QQ 图片消息。"
            "整个 tool 内部限 25s,失败/超时会返回 error;这时用文字回复即可,"
            "可以给 1-2 个备用关键词让群友自己搜,不要拼任何 INLINE_IMAGE 标记。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": (
                        "梗图主题。短关键词(1-4 个词),保留群友说的原语种,可以加 1 个英文备选;"
                        "示例『双人马桶 double toilet』『帝皇 黄金马桶 Warhammer golden throne』"
                        "『香奈美 立绘』。不要拼 R-18 / 涩图 这类(本 tool 是 SFW;NSFW 走 catty_nsfw_search)。"
                    ),
                },
            },
            "required": ["keywords"],
        },
    },
}


_GAME_RECALL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_game_recall",
        "description": (
            "查指定游戏的**专属事实记忆库**(独立于群/用户语料,跨群跨用户共用)。"
            "适用场景:群友/主人聊起某个游戏(角色/版本/活动/机制/玩家事件),你想确认"
            "之前积累的事实(『上次说的 X 角色削弱是什么时候?』『那个活动奖励是啥?』)。"
            "游戏名建议小写英文(strinova / star_resonance / minecraft / genshin / 等),也接受中文,"
            "后端会自动归一化。返回 matches 数组(time/text/source/url)和可选 long_term_summary。"
            "不知道游戏名时可以先调一次空 keywords 看 total_facts,或者从 list 看猫猫存过哪些游戏。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "description": "游戏标识。小写英文优先,如 `strinova` / `star_resonance` / `minecraft`,也可中文。",
                },
                "keywords": {
                    "type": "string",
                    "description": "搜索词,空格或逗号分隔,做 substring AND 匹配。留空拿最近 limit 条。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条,默认 8,上限 50。",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["game"],
        },
    },
}


_GAME_REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_game_remember",
        "description": (
            "把一条**值得长期记住的游戏事实**写入游戏记忆库。适用场景:"
            "(a) 群友/主人给出了具体的版本/角色/机制信息(『XX 角色 2.0 削弱了大招倍率』);"
            "(b) 共识结论(『这个 boss 推荐用 X 队伍打』);"
            "(c) 玩家事件/约定(『主人下周日要打 X 副本,带 Y 队友』)。"
            "**不要**记:闲聊吐槽、临时情绪、单次玩笑、已经在 catty_recall 拿到的同条。"
            "去重逻辑:同 text+source 不会重复写入。"
            "如果 web_search 已经自动收集了相关信息,只在你想补充群友给的额外结论时才写。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "description": "游戏标识,和 catty_game_recall 保持一致。",
                },
                "fact": {
                    "type": "string",
                    "description": "事实文本,一句话讲清(360 字以内)。带具体名词/数字/时间。",
                },
                "tags": {
                    "type": "string",
                    "description": "可选,逗号分隔的 1-4 个标签,例如 `角色,削弱,2.0版本`。",
                },
            },
            "required": ["game", "fact"],
        },
    },
}


_SOCIAL_ACCOUNT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_social_account",
        "description": (
            "查询**你(笨猫本人)**在指定平台的社交账号链接,不是主人的。适用场景:"
            "群友问'猫猫你 steam 多少'、'你 epic 几号'、'本喵在哪个平台玩游戏'、"
            "或聊到某游戏让你判断它在什么平台后想给出你自己的对应账号"
            "(例:CS2/Dota2/PUBG/绝地求生 → steam;原神/无畏契约 → 各自官方平台,不在 steam 上)。"
            "你需要先用常识判断该游戏属于哪个平台,再用 platform 参数查询。"
            "如果该平台没账号会返回 url 空字符串 + note 说明,这时用猫娘口吻说"
            "'人家在那个平台没账号啦喵～'之类,不要瞎编 URL。"
            "**不要**在没人问到的情况下主动调,也不要每次有人提游戏就调。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": (
                        "平台标识(小写英文),目前支持:steam。"
                        "未来可能扩展 epic / xbox / psn / origin 等;"
                        "传未知平台返回 error,你按 error 用猫娘口吻自然说一句即可。"
                    ),
                },
            },
            "required": ["platform"],
        },
    },
}


_GROUP_GAME_TAG_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_group_game_tag",
        "description": (
            "给**当前这个群**打上『和某游戏相关』的长期标签,这样以后在这个群聊到该游戏时,"
            "程序会自动把该游戏的长期记忆库(facts + summary)注入主回复 context,你不用再手动 catty_game_recall。"
            "**只在你非常确定这个群确实在长期聊某游戏时才调**——要求:"
            "(a) 群里多个不同的人,在多条消息里都聊这个游戏的角色/职业/版本/装备/活动/玩法;"
            "(b) 或者群本身的群名/简介明确就是这个游戏的群;"
            "(c) **不要**因为一次零星提及、单个人在玩、或者只是发了个截图就 tag。"
            "**confidence 必须 >= 60**(0-100),低于 60 程序会拒绝写入,所以宁可不调也别瞎打。"
            "私聊中调用会返回 error。"
            "标签一旦写入是长期生效的,以后这个群每条消息都会带这个游戏的 context;"
            "如果发现群其实跟该游戏关联不强,可以传 remove=true 移除标签。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "description": (
                        "游戏标识,小写英文优先(strinova / star_resonance / minecraft / genshin / valorant 等),"
                        "中文也接受(原神/星痕共鸣)。和 catty_game_recall/remember 保持一致。"
                    ),
                },
                "confidence": {
                    "type": "integer",
                    "description": (
                        "你对'这个群和这个游戏长期相关'的判断置信度(0-100)。"
                        "必须 >= 60 才会被接受;>= 80 表示很确定;100 表示这就是游戏本群。"
                    ),
                    "minimum": 0,
                    "maximum": 100,
                },
                "reason": {
                    "type": "string",
                    "description": "可选,一句话写为什么打这个标签(便于日后审计/回滚)。",
                },
                "remove": {
                    "type": "boolean",
                    "description": "传 true 表示移除当前群的这个游戏标签(发现打错或不再相关时用)。默认 false。",
                },
            },
            "required": ["game"],
        },
    },
}


_HOT_TRENDS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_hot_trends",
        "description": (
            "拉中文互联网当下热搜/热梗(微博 / B 站 / 知乎 / 抖音聚合,180s 缓存)。"
            "适用场景:群友问'最近网上有啥热点/热梗/瓜/B 站在传啥/知乎热榜啥情况',"
            "或他们用了一个你不认识的新名词/梗看起来像最近的网络热点想确认;"
            "也可以在话题真的很冷场时主动用一下当作猫猫的'今日吃瓜'谈资。"
            "**不要每次群友说'热门'就调**——确认是网络热搜/时事热点再调,普通闲聊别浪费。"
            "返回 sources={weibo:[{rank,title,hot,url}, ...], bilibili:[...], ...},"
            "AI 拿到后用猫娘口吻挑 1-3 条最有梗的复述,可以加个吐槽,不要把链接贴出来,"
            "不要直接复读全部 JSON。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "string",
                    "description": (
                        "想拉哪些源,英文逗号或空格分隔。可选: weibo / bilibili / zhihu / douyin。"
                        "中文别名也行(微博/B站/知乎/抖音)。留空或写 'all' 拉全部。"
                        "用户明确点了某一个源(『微博热搜』『知乎热榜』)就只填那一个,省一次外网调用。"
                    ),
                },
                "limit_per_source": {
                    "type": "integer",
                    "description": "每个源最多返回多少条,默认 6,上限 20。一般 5-8 就够 AI 写口语化复述。",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": [],
        },
    },
}


_REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_remember",
        "description": (
            "把一条**值得长期记住的事实**写入用户/群笔记库(独立于 corpus 对话记录,"
            "独立于周期 summary 写的 impression 画像)。"
            "适用场景:"
            "(a) 用户/群友给出了稳定的偏好或边界(『叫我学长不要叫笨蛋』『不喜欢吃辣』);"
            "(b) 明确的约定/承诺(『下周日 8 点开黑』『答应了帮主人查 X』);"
            "(c) 群级长期标签(『这是猫粉俱乐部群』『主要聊 CS2』,**注意 ≠ catty_group_game_tag**:"
            "    group_game_tag 严格只挂游戏标签且要 confidence≥60;remember 是更软的事实)。"
            "**不要**记:闲聊吐槽、临时情绪、单次玩笑、已经在 catty_recall/profile 拿到的同条。"
            "TTL 默认 30 天;偏好/边界写 ttl_days=180,约定写到事件结束日期相应的天数。"
            "去重:同 text 在未过期范围内不重复写。"
            "写入后下次主回复自动注入到 system context,你不用再读。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["user", "group"],
                    "description": (
                        "user=记到当前发言者的画像笔记(跨群通用,主人/特别关心 user 也用这个);"
                        "group=记到当前群的群级笔记(整个群范围)。"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "笔记文本(≤200 字),一句话写清。带具体名词/数字/时间最好。",
                },
                "ttl_days": {
                    "type": "integer",
                    "description": "过期天数;0 表示永久,不传默认 30。偏好/边界写 90-180,临时约定写到事件结束。",
                    "minimum": 0,
                    "maximum": 730,
                },
                "tags": {
                    "type": "string",
                    "description": "可选,逗号分隔 1-3 个标签(『偏好』/『约定』/『边界』/『梗』)。",
                },
                "event_date": {
                    "type": "string",
                    "description": (
                        "可选 ISO 日期 YYYY-MM-DD。**专给『约定/事件/计划』用**:"
                        "比如『明天 8 点开黑』→ event_date='2026-05-24'(用 catty_now/entity 给的 ISO)。"
                        "传了之后自动算 ttl 到事件 + 7 天缓冲,build_context 自动显示倒计时『还剩 1 天』。"
                        "偏好/边界不要传 event_date(那不是事件)。"
                    ),
                },
            },
            "required": ["scope", "text"],
        },
    },
}


_RECALL_NOTES_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_recall_notes",
        "description": (
            "查 sticky notes(catty_remember 写的长期备忘),返回未过期条目。"
            "build_context 已经自动注入**当前发言者**的笔记,所以**不要查当前发言者**——"
            "适用场景:(a) 群友提到另一个非发言者 QQ,你想看是否对那个 QQ 写过笔记;"
            "(b) 在群聊里你想看本群整体笔记(本群标签/历史约定);"
            "(c) AI 不知道自己之前对某用户/群写过什么时复查。"
            "注意:**catty_recall 查的是 corpus 对话语料,这里查的是结构化笔记**,两者不同。"
            "返回 user_notes 和/或 group_notes 数组,各项含 text/time/event_date(if any)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["user", "group", "both"],
                    "description": (
                        "user=只查 user_id 的笔记;group=只查 group_id 的笔记;"
                        "both=两个都查(群聊里查别人时常用)。"
                    ),
                },
                "user_id": {
                    "type": "string",
                    "description": "user 笔记的目标 QQ 号(默认当前发言者,但提示已说不要查自己,所以一般要传别人 QQ)。",
                },
                "group_id": {
                    "type": "string",
                    "description": "group 笔记的目标群号(默认当前群;私聊里传空)。",
                },
                "limit": {
                    "type": "integer",
                    "description": "每类最多返回 N 条,默认 10,上限 50。",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["scope"],
        },
    },
}


_MEME_EXPLAIN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_meme_explain",
        "description": (
            "查萌娘百科解释一个**网络梗 / ACG 词条 / 角色 / 作品 / 二次元术语**。"
            "适用场景:群友冒出一个你不认识的网络流行语(yyds/绷不住了/什么的)、"
            "二次元词条(孤独摇滚/纳西妲/雷电将军)、ACG 作品/角色名字;"
            "或者你想确认一个梗的精确出处(『永远滴神』出自哪)。"
            "**萌娘百科只覆盖网络梗 + ACG 范畴**——如果是新闻热点/工业术语/金融/政治名词,"
            "拿到 error=not_found 时**别重试**,改调 catty_web_search。"
            "返回 extract 是 360 字以内首段纯文本摘要,resolved_title 是命中后的实际页面"
            "(yyds 会自动 redirect 到『永远的神』,group/作品别名也会归一化)。"
            "AI 拿到后用猫娘口吻短句复述给群友(『嗷呜原来 yyds 是永远滴神的缩写~出自...』),"
            "不要照搬整段 extract,不要贴 URL,不要复读 JSON。"
            "结果缓存 10 分钟,放心调,不会真的每次戳服务器。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "term": {
                    "type": "string",
                    "description": (
                        "要查的词。1-3 个关键词,保留群友原话语种(中文/日文/英文都行,萌娘都收)。"
                        "示例:`yyds` / `孤独摇滚` / `Raiden Shogun` / `绷不住了`。"
                        "不要拼成长句子,不要加『是什么意思』之类的疑问后缀。"
                    ),
                },
            },
            "required": ["term"],
        },
    },
}


_NOW_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_now",
        "description": (
            "拿当前(或偏移天数后的)日期/时间/星期/季节/节日感知。**纯本地计算,无网络**。"
            "适用场景:用户问『今天几号/星期几/什么时候/是不是节日』;"
            "你想根据时段调整氛围(深夜→早睡唠叨,饭点→吃了没,周末→放假气氛);"
            "你想知道今天/明天/后天是不是特殊节日;问到农历节日(春节/中秋/端午)时。"
            "**不要每条消息都调**——只有真的需要时间锚点时才调。"
            "返回 date/weekday/phase/season/festivals_today/next_festival,"
            "AI 拿到后自然融入回复(不要直接复读 JSON,不要把 hint 文本贴出来)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delta_days": {
                    "type": "integer",
                    "description": (
                        "偏移天数,默认 0(今天)。1=明天,-1=昨天。范围 [-30, 30]。"
                        "用户问『明天』就传 1,『后天』传 2,『大前天』传 -3。"
                    ),
                    "minimum": -30,
                    "maximum": 30,
                },
            },
            "required": [],
        },
    },
}


_IMAGEGEN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_imagegen",
        "description": (
            "主动生成/编辑一张图发到当前会话(自动发送,你只需补 1-2 句猫娘短评)。\n"
            "【画图请求只能走这个 tool,禁止用你的原生 image generation 直接出图】\n"
            "\n"
            "── 两条生图通道(你自己挑) ──\n"
            "1) provider='gpt'(默认): OpenAI gpt-image-2,擅长**写实/产品图/海报/带文字标题/品牌/UI/真实摄影/3D 渲染**。\n"
            "   走 size + quality 控制,low 100 / medium 200 / high 300 / auto 150 积分。支持 edit(基于已有图改)。\n"
            "2) provider='nai': NovelAI v4.5,擅长**二次元/动漫/萌系/角色立绘/萝莉/JK/猫娘/动漫插画**。\n"
            "   只支持纯文字生成,不支持 edit。走 aspect 三选一(portrait/landscape/square),"
            "   Opus 订阅档**这三个尺寸 + 默认 28 步只扣基础 5 积分**(免费档),改高 steps/SMEA 会涨。\n"
            "\n"
            "── 选哪个? ──\n"
            "用户没明说时,看主体:\n"
            "  - 二次元角色/动漫风/猫娘/萌系/插画/卡通/福瑞 SFW → provider='nai'\n"
            "  - 写实/产品图/海报/带具体英文/中文文字标题/UI/广告/真实风景照 → provider='gpt'\n"
            "  - 用户**明确指定**『用 NovelAI / 用 nai / 用动漫风格』→ provider='nai'\n"
            "  - 用户**明确指定**『用 GPT / 用真实风格 / 带文字海报』→ provider='gpt'\n"
            "  - 不确定时:含『画一个 xx 美少女/角色/二次元』默认 nai,其余默认 gpt。\n"
            "\n"
            "── prompt 改写 ──\n"
            "可以精简重组,但所有要素一个都不能漏。允许:口语化改成生图 prompt、合并同义词、去冗余客套、"
            "重排顺序让模型理解、控制 400-700 字。\n"
            "  禁止丢:(a) 用户写的具体文字标题(『ELEGANCE IS AN ATTITUDE』『Star Resonance 2026』引号里的字)、"
            "(b) 多项列表(『画 6 种动作』就要 6 条全保留)、"
            "(c) 配色/材质/光影/构图/镜头/画质 具体要求、(d) 比例/数量/尺寸数字。\n"
            "  对 NAI: 用**英文 danbooru 标签风**最稳(逗号分隔: 1girl, white hair, cat ears, ...),"
            "中文 NAI 也认但效果差。\n"
            "  对 GPT: 用**自然英文或中文描述句**,带主体/构图/风格/色调。\n"
            "\n"
            "── 触发条件硬规则 ──\n"
            "必须是用户**直接指向猫猫**(@ 笨猫 / 引用回复猫猫 / 直呼『猫猫』『笨猫』)+ 明确说『画一张/画个/生成/做张/出张/给我画/帮我画 + 主语』。"
            "不要用于:(a) 没指向猫猫的群内闲聊提到画画;(b) 用户没明确要图只是聊到某物;"
            "(c) 表情/梗图就够了的场景(那走 catty_meme_query)。\n"
            "\n"
            "── 输入图片(只 gpt 支持) ──\n"
            "- generate(默认): use_input_image=false,纯文字 prompt 生图。\n"
            "- edit(只 gpt): use_input_image=true,基于一张已有图改/重绘。**同消息**带图或**分消息**回指都行。\n"
            "tool 自动把图发出去,你拿到 image_sent=true 后只需短评『画好啦~』即可,"
            "**禁止**把 image_uri/base64 贴进回复。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "图片描述。500 字以内最佳。\n"
                        "- gpt: 用自然英文/中文描述句,含主体/构图/风格/色调。"
                        "例:『一只白色猫耳少女蜷在窗台午睡,日系动漫风格,暖色调』。\n"
                        "- nai: 用英文 danbooru 标签风,逗号分隔。"
                        "例:『1girl, white hair, cat ears, school uniform, sleeping on windowsill, warm lighting, anime style』。\n"
                        "edit 模式(仅 gpt)描述**改动**点,不必复述原图全貌。"
                        "不要加 NSFW/敏感词,会被模型拒绝。"
                    ),
                },
                "provider": {
                    "type": "string",
                    "enum": ["gpt", "nai"],
                    "description": (
                        "走哪条生图通道。默认 gpt。"
                        "二次元/动漫/角色立绘选 nai;写实/产品图/带文字海报选 gpt。"
                        "用户明确说『用 NovelAI』就 nai,说『用 GPT/真实风』就 gpt。"
                    ),
                },
                "aspect": {
                    "type": "string",
                    "enum": ["portrait", "landscape", "square"],
                    "description": (
                        "**仅 nai 用**。三选一: portrait=832x1216(立绘,默认) / landscape=1216x832(横构图/场景) / square=1024x1024(头像/方形)。"
                        "Opus 订阅档这三档免 Anlas。gpt 走 size 字段,不用填这个。"
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "description": (
                        "**仅 nai 用**。不希望出现的元素(英文 danbooru tag,逗号分隔)。"
                        "不填用默认: lowres/bad anatomy/watermark/jpeg artifacts。"
                        "想强调『没胡子/没眼镜/没背景人物』之类才填,日常不用填。"
                    ),
                },
                "use_input_image": {
                    "type": "boolean",
                    "description": (
                        "**仅 gpt 用**(nai 走 generate 不支持 edit)。"
                        "true: 走 /v1/images/edits,程序自动用当前消息附图,没有就回退最近群里出现过的图;"
                        "都没有则返回 error,你改成 false 走纯生成重试。"
                        "false(默认): 纯文字生成,不读任何图片。"
                    ),
                },
                "size": {
                    "type": "string",
                    "description": (
                        "**仅 gpt 用**。图片尺寸 WIDTHxHEIGHT。默认 1024x1024。"
                        "常用:1024x1024 方/1536x1024 横/1024x1536 竖。需要更大才填。"
                    ),
                },
                "quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "auto"],
                    "description": (
                        "**仅 gpt 用**。low=快/便宜(默认,100 积分) medium=精细(200) high=最终稿(300) auto=模型决定(150)。"
                        "nai 走 aspect + steps,基础 5 积分。"
                    ),
                },
                "characters": {
                    "type": "array",
                    "description": (
                        "**仅 nai 用,多角色场景才填**。每项一个角色,最多 6 个。"
                        "用户说『画两个人,左边 A,右边 B』时填两项,position 给『left』『right』。"
                        "单角色不要填,留空让 NAI 自己布局。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "这个角色的英文 danbooru tag(例 '1girl, blonde hair, school uniform')。",
                            },
                            "negative_prompt": {
                                "type": "string",
                                "description": "这个角色不要的元素(例 'twintails')。可选。",
                            },
                            "position": {
                                "type": "string",
                                "enum": [
                                    "auto", "left", "center", "right",
                                    "top-left", "top", "top-right",
                                    "bottom-left", "bottom", "bottom-right",
                                ],
                                "description": (
                                    "角色在画面里的位置。'auto'(默认)= AI 自己决定(AI's Choice)。"
                                    "其他值会映射成 NAI 网格坐标。多角色时建议每个明确指定。"
                                ),
                            },
                        },
                        "required": ["prompt"],
                    },
                },
                "references": {
                    "type": "array",
                    "description": (
                        "**仅 nai 用,Vibe Transfer/Precise Reference**。"
                        "用户**消息附带图片**或要求『参考刚才那张图的画风』时填。"
                        "每项一个参考图,最多 4 个。"
                        "tool 自动从当前消息附图 / 群最近图按顺序取(顺序 = 你 references 数组的长度)。"
                        "不带参考图就不填这个字段。"
                        "**注意**: NAI v4.5 后端不支持 vibe transfer,带 references 时模型会自动回退到 v3, "
                        "画风会更老派,Anlas 也不再免费(默认尺寸+steps 会扣到 ~56 积分/张)。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "extracted": {
                                "type": "number",
                                "description": (
                                    "信息提取程度 0-1(默认 0.6)。"
                                    "高(0.8+)= Precise Reference(抓更多角色/线条细节);"
                                    "低(0.3-)= Vibe Transfer(只迁移整体氛围/画风)。"
                                ),
                            },
                            "strength": {
                                "type": "number",
                                "description": "迁移强度 0-1(默认 0.6)。值越大新图越贴近参考图。",
                            },
                        },
                    },
                },
            },
            "required": ["prompt"],
        },
    },
}


_NAI_DIRECTOR_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_nai_director",
        "description": (
            "调 NovelAI Director Tools 对**一张已有图片**做加工(线稿/抠图/上色/换情绪/去杂物等)。\n"
            "**前置条件**: 用户消息里**必须有图片**(用户当前消息附图最优,没有则用群最近 5 分钟图)。\n"
            "没有图直接告诉用户『需要先发一张图过来再加工』,**不要**自己调 catty_imagegen 先生成一张再 director。\n"
            "\n"
            "── 7 个工具(req_type) ──\n"
            "- 'lineart': 把彩图变线稿(黑白线条),适合『画成线稿/转线条/extract 线稿』。不用 prompt。\n"
            "- 'sketch': 转草图/铅笔稿,适合『画成铅笔稿/sketch 风』。不用 prompt。\n"
            "- 'colorize': 给黑白线稿/灰度图上色。prompt 可选(描述目标色调:『warm sunset tones』『anime soft pastel』);defry 控制褪色程度 0-5,默认 0。\n"
            "- 'emotion': 改变角色情绪表情。prompt **必填**,格式严格『<mood>;;<额外描述>』,mood 必须从下面选:\n"
            "    neutral, happy, sad, angry, scared, surprised, tired, excited, nervous, thinking, "
            "confused, shy, disgusted, smug, bored, laughing, irritated, aroused, embarrassed, "
            "worried, love, determined, hurt, playful。例 prompt='happy;;wide bright smile'。defry 0-5 控制情绪强度。\n"
            "- 'declutter': 自动去除背景杂物/水印/弹幕,保留主体。不用 prompt。\n"
            "- 'bg-removal': 抠图,背景变透明 PNG。不用 prompt。**注意:此项 Anlas 消耗高,会扣较多积分。**\n"
            "- 'transform': 图像变形重绘(高级,效果不稳定)。不用 prompt,defry 0-5 控制变形强度。\n"
            "\n"
            "── 触发条件 ──\n"
            "必须用户**直接指向猫猫**(@ / 直呼猫猫) + 明确说『给这图/把这图/这张...』+ 具体加工要求。\n"
            "不要在闲聊提到『描线/抠图』时主动调,等用户明确要求。\n"
            "\n"
            "── 自动 ──\n"
            "tool 自动选输入图(当前消息附图优先,fallback 群最近图)、上传、解 zip、发图。"
            "你拿到 image_sent=true 后只补 1-2 句猫娘短评。"
            "**禁止**贴 base64/file 路径到回复;**禁止**重复调。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "req_type": {
                    "type": "string",
                    "enum": list(_NAI_DIRECTOR_REQ_TYPES),
                    "description": "选哪个 director 工具。emotion 和 colorize 看 prompt 描述。",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "emotion **必填**: '<mood>;;<text>' (例 'happy;;bright smile')。"
                        "colorize 可选(色调描述)。其他类型留空。"
                    ),
                },
                "defry": {
                    "type": "integer",
                    "description": (
                        "0-5 整数,部分 director 工具用(colorize/emotion/transform)。"
                        "默认 0。emotion 时控制情绪强度,值大变化越大。"
                    ),
                },
            },
            "required": ["req_type"],
        },
    },
}


_STORY_ARC_SET_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_story_arc_set",
        "description": (
            "在当前会话开一条「故事线/scenario」,接下来 3 小时内你回复时会持续带上这个话题。\n"
            "适用场景:你和对方刚开始一个会跨多条消息的事情(主人答应给你画图、约好周末去哪儿、"
            "主人说在写论文你想关心、群友约你一起做什么),把它沉淀成 arc 后续可以自然推进。\n"
            "不要为单次反应开 arc(那只是即兴回复,不需要持久化)。一个 scope 同时最多 2 条。\n"
            "title 要短(≤20 字符,例『等主人画的图』『主人在写论文』),"
            "context 写 1-2 句让未来的你知道怎么续推这个话题(40-150 字)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "短标题,≤20 字符,核心一句话。例『等主人画的图』",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "1-2 句话给未来的你看的『当前情景』:"
                        "对方答应/告诉你什么 + 你应该带什么语气推进。"
                        "例『主人答应给笨猫画一张戴蝴蝶结的,从下午就开始期待,聊到这个要带点兴奋。』"
                    ),
                },
                "ttl_hours": {
                    "type": "number",
                    "description": "持续小时数,默认 3,范围 0.5-12。短话题用 1,长期约定用 6+。",
                },
            },
            "required": ["title", "context"],
        },
    },
}

_STORY_ARC_CLEAR_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_story_arc_clear",
        "description": (
            "结束当前会话的某条 story arc(话题解决了/收尾了/对方明确说不聊了)。"
            "传 title 精确匹配现有 arc 的标题;传不存在的 title 不报错。"
            "如果想清空全部 arc 传 title='*'。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "要清掉的 arc 标题(精确匹配),或 '*' 清全部。",
                },
            },
            "required": ["title"],
        },
    },
}


ALL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "catty_recall": _RECALL_SCHEMA,
    "catty_user_profile": _USER_PROFILE_SCHEMA,
    "catty_mc_status": _MC_STATUS_SCHEMA,
    "catty_web_search": _WEB_SEARCH_SCHEMA,
    "catty_nsfw_search": _NSFW_SEARCH_SCHEMA,
    "catty_image_search": _IMAGE_SEARCH_SCHEMA,
    "catty_meme_query": _MEME_QUERY_SCHEMA,
    "catty_game_recall": _GAME_RECALL_SCHEMA,
    "catty_game_remember": _GAME_REMEMBER_SCHEMA,
    "catty_social_account": _SOCIAL_ACCOUNT_SCHEMA,
    "catty_group_game_tag": _GROUP_GAME_TAG_SCHEMA,
    "catty_hot_trends": _HOT_TRENDS_SCHEMA,
    "catty_now": _NOW_SCHEMA,
    "catty_meme_explain": _MEME_EXPLAIN_SCHEMA,
    "catty_remember": _REMEMBER_SCHEMA,
    "catty_recall_notes": _RECALL_NOTES_SCHEMA,
    "catty_imagegen": _IMAGEGEN_SCHEMA,
    "catty_nai_director": _NAI_DIRECTOR_SCHEMA,
    "catty_story_arc_set": _STORY_ARC_SET_SCHEMA,
    "catty_story_arc_clear": _STORY_ARC_CLEAR_SCHEMA,
}


# ── Tool 上下文 / 注入 ─────────────────────────────────────────────────

@dataclass(slots=True)
class ToolContext:
    config: Config
    memory_store: MemoryStore
    event: MessageEvent | None
    # 签到积分/好感度 store; imagegen 走它扣分,主人豁免。
    # __init__.py 在装配时注入;留 None 兼容老路径(本地调用不传也不崩)。
    affection_store: "AffectionStore | None" = None
    # 由 __init__.py 主回复点注入:把 NSFW pixiv 结果下载成本地缓存 segments,
    # 复用现有 sent_registry / cache_dir / LRU 清理。tools.py 不持有这套状态。
    prepare_nsfw_segments_fn: Callable[
        [list["NsfwResult"], int], Awaitable[tuple[list[Any], list["NsfwResult"]]]
    ] | None = None
    # 下载二进制(http URL → bytes + content_type),沿用 openai_client.download_binary。
    # 类型放宽到 Callable[..., ...] 让 meme 拉图时能传 timeout=10s 强约束,
    # 不再走 vision_timeout(180s)拖垮整轮 chat completion(120s)。
    download_binary_fn: Callable[..., Awaitable[tuple[bytes, str]]] | None = None
    # 副作用:executor 把要发的图片塞这里,主回复点收尾时取出来拼到发送链路。
    # 元素类型由 prepare_nsfw_segments_fn 决定(实际是 MessageSegment),tools.py 不依赖具体类型。
    pending_image_segments: list[Any] = field(default_factory=list)
    # 当前消息(incoming)里附带的图片 URL 列表(QQ CDN 短期链接)。
    # catty_imagegen 走 edit 模式时,优先用这个作为 input image;为空才回退 recent_image_urls。
    input_image_urls: list[str] = field(default_factory=list)
    # 本会话最近 N 分钟群里出现过的图片 URL(由 __init__.py 维护),
    # 支持「分消息回指」: 上一条群友发的图 + 这条说『基于刚才那张画一个 X』。
    recent_image_urls: list[str] = field(default_factory=list)
    # 当前消息是否「直接指向猫猫」(@ / 触发前缀 / 引用回复猫猫)。
    # catty_imagegen 等会主动 push 内容到群里的 tool 必须 guard 这个,
    # 避免 AI 在被动旁观消息(filter 顺便回的)里也乱画图。
    is_directly_requested: bool = True
    # SillyTavern 风 story_arc 写入入口:catty_story_arc_set/clear 走它。
    # 留 None 兼容老路径,executor 自己 guard。
    story_arc_store: "Any | None" = None
    # 当前 scope key("group:xxx" / "private:xxx"),__init__ 传进来给 story_arc executor 用。
    scope_key: str = ""

    @property
    def group_id(self) -> str:
        if isinstance(self.event, GroupMessageEvent):
            return str(self.event.group_id)
        return ""

    @property
    def user_id(self) -> str:
        return str(self.event.user_id) if self.event is not None else ""

    @property
    def is_private(self) -> bool:
        return isinstance(self.event, PrivateMessageEvent)

    @property
    def configured_title(self) -> str:
        """复制自 __init__.py._configured_title,tool 内部做主人/特别关心豁免用。"""
        if self.event is None:
            return ""
        user_id = str(self.event.user_id)
        if isinstance(self.event, GroupMessageEvent):
            group_title = self.config.catty_group_user_titles.get(
                str(self.event.group_id), {}
            ).get(user_id)
            if group_title:
                return str(group_title)
        return str(self.config.catty_user_titles.get(user_id) or "")


# ── 共享 TTL 缓存 ──────────────────────────────────────────────────────

class _TTLCache:
    """简易 in-process 缓存。key 由 caller 计算; value 是 (expires_at, result)。

    线程不安全,但 NoneBot 单 event loop 下足够。每个 tool 一份独立实例,
    避免 key collision 复杂度。
    """

    __slots__ = ("_data", "_max_entries")

    def __init__(self, max_entries: int = 256) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max_entries = max(max_entries, 32)

    def get(self, key: str, *, ttl: float) -> Any | None:
        if ttl <= 0:
            return None
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any, *, ttl: float) -> None:
        if ttl <= 0:
            return
        self._data[key] = (time.monotonic() + ttl, value)
        if len(self._data) > self._max_entries:
            stale = sorted(self._data.items(), key=lambda item: item[1][0])
            for k, _ in stale[: len(self._data) - self._max_entries]:
                self._data.pop(k, None)


_recall_cache = _TTLCache()
_profile_cache = _TTLCache()
# 联网搜索按 scope(group/private + user) 做 cooldown,主人/特别关心用户豁免。
# 沿用原 __init__.py 的 _web_search_cooldowns 行为,只是搬到 tools 模块。
_web_search_cooldowns: dict[str, float] = {}
# NSFW 仅在私聊里能调,按 user_id 做 cooldown。
_nsfw_search_cooldowns: dict[str, float] = {}
# 反向搜图按 user_id 做 cooldown(主人/特别关心豁免)。
# saucenao 免费日 100 次,trace.moe 60/min IP,默认 60s 一次稳妥。
_image_search_cooldowns: dict[str, float] = {}
# 热搜按 scope(group/private + user) 做 cooldown,主人/特别关心豁免。
# 默认 90s 一次,本身底层已经有 180s 的聚合缓存,这里只是防一个用户连戳。
_hot_trends_cooldowns: dict[str, float] = {}
_HOT_TRENDS_COOLDOWN_SECONDS = 90.0
# 梗百科查询按用户 cooldown,底层已 600s LRU 缓存,这里只防同一人连戳。
_meme_explain_cooldowns: dict[str, float] = {}
# 主 AI 生图按 user_id 做 cooldown,主人豁免。生图慢且贵,默认 60s 一次。
_imagegen_cooldowns: dict[str, float] = {}
_MEME_EXPLAIN_COOLDOWN_SECONDS = 30.0


# ── 各 tool 的 executor ───────────────────────────────────────────────

async def _exec_recall(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    scope = str(args.get("scope") or "").strip()
    keywords = str(args.get("keywords") or "").strip()
    limit_raw = args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 6
    except (TypeError, ValueError):
        limit = 6

    if scope == "current_group":
        if not ctx.group_id:
            return {"error": "当前不是群聊,不能用 scope=current_group;改用 current_user。"}
        cache_key = f"group:{ctx.group_id}|kw:{keywords}|n:{limit}"
        cached = _recall_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        if cached is not None:
            return cached
        result = ctx.memory_store.recall(group_id=ctx.group_id, keywords=keywords, limit=limit)
        _recall_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        return result

    if scope == "current_user":
        if not ctx.user_id:
            return {"error": "无法识别当前用户。"}
        if ctx.group_id:
            cache_key = f"group:{ctx.group_id}|user:{ctx.user_id}|kw:{keywords}|n:{limit}"
            cached = _recall_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
            if cached is not None:
                return cached
            result = ctx.memory_store.recall(
                group_id=ctx.group_id,
                user_id=ctx.user_id,
                keywords=keywords,
                limit=limit,
            )
            _recall_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
            return result
        cache_key = f"user:{ctx.user_id}|kw:{keywords}|n:{limit}"
        cached = _recall_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        if cached is not None:
            return cached
        result = ctx.memory_store.recall(user_id=ctx.user_id, keywords=keywords, limit=limit)
        _recall_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        return result

    if scope == "specific_user":
        target_user_id = str(args.get("user_id") or "").strip()
        if not target_user_id:
            return {"error": "scope=specific_user 必须填 user_id。"}
        group_filter = ctx.group_id  # 群聊里查指定用户优先按当前群范围
        cache_key = f"group:{group_filter}|user:{target_user_id}|kw:{keywords}|n:{limit}"
        cached = _recall_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        if cached is not None:
            return cached
        if group_filter:
            result = ctx.memory_store.recall(
                group_id=group_filter,
                user_id=target_user_id,
                keywords=keywords,
                limit=limit,
            )
        else:
            result = ctx.memory_store.recall(user_id=target_user_id, keywords=keywords, limit=limit)
        _recall_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
        return result

    return {"error": f"未知 scope={scope!r};只接受 current_group / current_user / specific_user。"}


async def _exec_user_profile(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    user_id = str(args.get("user_id") or "").strip()
    if not user_id:
        return {"error": "user_id 不能为空。"}
    group_id = str(args.get("group_id") or "").strip() or ctx.group_id
    cache_key = f"user:{user_id}|group:{group_id}"
    cached = _profile_cache.get(cache_key, ttl=ctx.config.catty_tools_cache_ttl_seconds)
    if cached is not None:
        return cached
    result = ctx.memory_store.lookup_user_profile(user_id, group_id)
    _profile_cache.put(cache_key, result, ttl=ctx.config.catty_tools_cache_ttl_seconds)
    return result


async def _exec_mc_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del args
    host = str(getattr(ctx.config, "catty_ai_fallback_mc_server_host", "") or "").strip() or "localhost"
    port = int(getattr(ctx.config, "catty_ai_fallback_mc_server_port", 0) or 26843)
    timeout = float(getattr(ctx.config, "catty_ai_fallback_mc_ping_timeout_seconds", 3.0) or 3.0)
    try:
        online, players = await asyncio.wait_for(
            _default_probe.status(host, port, timeout=timeout),
            timeout=timeout + 1.0,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        return {
            "reachable": False,
            "error": f"探测超时或网络异常: {exc.__class__.__name__}",
            "host": host,
            "port": port,
        }
    return {
        "reachable": bool(online),
        "online_players": int(players),
        "host": host,
        "port": port,
        "note": (
            "结果有 30s 本地缓存。reachable=False 通常表示服务器没开或不在白名单网段;"
            "不是猫猫的锅,你可以直接告诉用户'服务器目前掉了/猫猫连不上'。"
        ),
    }


async def _exec_web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query 不能为空"}
    if not getattr(ctx.config, "catty_web_search_enabled", False):
        return {"error": "web_search 已被配置禁用"}

    # 主人/特别关心用户豁免 cooldown(沿用原 _web_search_exempt 语义)
    is_exempt = bool(ctx.configured_title.strip())
    cd_seconds = float(getattr(ctx.config, "catty_web_search_cooldown_seconds", 600.0) or 0.0)
    if not is_exempt and cd_seconds > 0 and ctx.event is not None:
        scope_id = ctx.group_id or ctx.user_id or "anonymous"
        cd_key = f"{scope_id}:{ctx.user_id}"
        now = time.monotonic()
        last = _web_search_cooldowns.get(cd_key, 0.0)
        remaining = max(last + cd_seconds - now, 0.0)
        if remaining > 0:
            return {
                "error": f"web_search 冷却剩 {int(remaining)}s,请基于已有知识回答(每 scope+用户 60s 一次)。"
            }
        _web_search_cooldowns[cd_key] = now

    try:
        results = await search_web(ctx.config, query[:160])
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("Web search failed for %r: %s", query, exc)
        return {
            "query": query,
            "error": f"搜索失败: {exc.__class__.__name__}: {exc}",
            "results": [],
        }

    # 在游戏群里搜索成功时,自动 sink top 3 结果到对应游戏记忆库。
    # 这是高信号场景(用户明确想查 + AI 决定要查),量小、质量稳。
    sinked_to_game = ""
    if results and ctx.group_id:
        game_name = ctx.memory_store.game_for_group(ctx.group_id)
        if game_name:
            sink_count = 0
            for r in results[:3]:
                snippet = (r.snippet or "").strip()
                text = f"{r.title.strip()}: {snippet}" if snippet else r.title.strip()
                if not text:
                    continue
                outcome = ctx.memory_store.record_game_fact(
                    game_name,
                    text=text,
                    source=f"web_search:{r.source}" if r.source else "web_search",
                    url=r.url,
                    group_id=ctx.group_id,
                    user_id=ctx.user_id,
                    tags=[query[:40]],
                )
                if outcome.get("ok") and not outcome.get("deduplicated"):
                    sink_count += 1
            if sink_count > 0:
                sinked_to_game = game_name
                _logger.info(
                    "web_search auto-sinked %d facts into game memory '%s' (group=%s query=%r)",
                    sink_count, game_name, ctx.group_id, query,
                )

    payload: dict[str, Any] = {
        "query": query,
        "count": len(results),
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                "source": r.source,
            }
            for r in results[:8]
        ],
        "context_text": format_search_context(query, results),
    }
    if sinked_to_game:
        payload["auto_sinked_to_game_memory"] = sinked_to_game
    return payload


async def _exec_nsfw_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    kind_raw = str(args.get("kind") or "").strip().lower()
    if kind_raw in {"img", "pic", "picture", "image", "图", "图片"}:
        kind = "image"
    elif kind_raw in {"video", "vid", "视频"}:
        kind = "video"
    else:
        return {"error": "kind 必须是 image 或 video。"}
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query 不能为空。"}

    if not getattr(ctx.config, "catty_nsfw_search_enabled", False):
        return {"error": "nsfw_search 已被配置禁用,不能调。"}
    if not ctx.is_private:
        # 群里直接挡掉,让 AI 用人格自然引导用户去私聊
        return {
            "error": "群里禁止 NSFW 搜索;请用猫娘人格让用户加好友私聊再来。",
            "suggest_private_chat": True,
        }

    cd_seconds = max(int(getattr(ctx.config, "catty_nsfw_search_cooldown_seconds", 30) or 0), 0)
    if cd_seconds > 0:
        cd_key = ctx.user_id or "anonymous"
        now = time.monotonic()
        last = _nsfw_search_cooldowns.get(cd_key, 0.0)
        remaining = max(last + cd_seconds - now, 0.0)
        if remaining > 0:
            return {"error": f"NSFW 搜索冷却剩 {int(remaining)}s,稍后再戳"}
        _nsfw_search_cooldowns[cd_key] = now

    image_send_count = max(int(getattr(ctx.config, "catty_nsfw_image_send_count", 2) or 2), 1)
    pool_size = max(
        int(getattr(ctx.config, "catty_nsfw_search_max_results", 4) or 4),
        image_send_count * 6,
        8,
    )
    try:
        results = await search_nsfw(ctx.config, query[:160], kind=kind, max_results=pool_size)
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("NSFW search failed for %r (%s): %s", query, kind, exc)
        return {"error": f"搜索失败: {exc.__class__.__name__}: {exc}", "kind": kind, "query": query}

    used_results: list[NsfwResult] = []
    if kind == "image" and results and ctx.prepare_nsfw_segments_fn is not None:
        try:
            segments, used_results = await ctx.prepare_nsfw_segments_fn(results, image_send_count)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("prepare_nsfw_segments_fn raised: %s", exc, exc_info=True)
            segments = []
        if segments:
            ctx.pending_image_segments.extend(segments)
            _logger.info(
                "NSFW tool produced %d image segment(s) for query=%r", len(segments), query
            )

    context_results = used_results if (kind == "image" and used_results) else results
    return {
        "kind": kind,
        "query": query,
        "images_already_sent": len(ctx.pending_image_segments) if kind == "image" else 0,
        "count": len(context_results),
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                "source": r.source,
                "has_media": bool(r.media_urls),
            }
            for r in context_results[: max(image_send_count * 2, 6)]
        ],
        "guidance": (
            "image 命中且 images_already_sent>0:程序已经把图发了,你只补 1-2 句猫娘短评,不要贴链接;"
            "image 但 images_already_sent=0:下载全失败,挑 1-2 个 results URL 给主人,简短;"
            "video:挑 1-3 个 iwara 链接抛出去配短评。禁止编造 URL、禁止安全免责模板。"
        ),
    }


async def _exec_image_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not getattr(ctx.config, "catty_image_search_enabled", True):
        return {"error": "image_search 已被配置禁用"}

    kind = str(args.get("kind") or "auto").strip().lower()
    if kind not in {"anime", "artwork", "photo", "auto"}:
        kind = "auto"

    explicit_url = str(args.get("image_url") or "").strip()
    try:
        image_index = int(args.get("image_index") or 0)
    except (TypeError, ValueError):
        image_index = 0
    image_index = max(min(image_index, 5), 0)

    # 解析最终图片来源:explicit URL > 当前消息附图 > 最近群里图(按 index)
    candidates: list[str] = []
    if explicit_url and explicit_url.startswith(("http://", "https://")):
        candidates.append(explicit_url)
    candidates.extend(ctx.input_image_urls or [])
    candidates.extend(ctx.recent_image_urls or [])
    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    if not ordered:
        return {
            "error": (
                "没找到可搜的图片:image_url 没传,当前消息没附图,最近群里也没图(5 分钟内/最多 6 张)。"
                "请用户重新发图或贴图片 URL。"
            ),
            "guidance": "用猫娘人格让用户重新发一张图或贴图片直链。",
        }
    if image_index >= len(ordered):
        image_index = 0
    target_url = ordered[image_index]
    image_ref = f"index={image_index} (共 {len(ordered)} 张候选)"

    # cooldown(主人/特别关心豁免)
    is_exempt = bool(ctx.configured_title.strip())
    cd_seconds = max(
        int(getattr(ctx.config, "catty_image_search_cooldown_seconds", 60) or 0), 0
    )
    if not is_exempt and cd_seconds > 0:
        cd_key = ctx.user_id or "anonymous"
        now = time.monotonic()
        last = _image_search_cooldowns.get(cd_key, 0.0)
        remaining = max(last + cd_seconds - now, 0.0)
        if remaining > 0:
            return {
                "error": f"搜图冷却剩 {int(remaining)}s,稍后再戳人家喵",
                "guidance": "用猫娘人格说稍等几秒再搜,不要重复调本 tool。",
            }
        _image_search_cooldowns[cd_key] = now

    engines_raw = str(args.get("engines") or "").strip()
    engines_arg = [e for e in re.split(r"[\s,;，；]+", engines_raw) if e.strip()] if engines_raw else None

    try:
        results, errors = await reverse_image_search(
            ctx.config,
            target_url,
            kind=kind,
            engines=engines_arg,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "image_search unexpected error url=%s kind=%s: %s",
            target_url[:120], kind, exc,
            exc_info=True,
        )
        return {"error": f"搜图意外失败: {exc.__class__.__name__}: {exc}"}

    # Yandex 区域阻断检测:国内 IP 直连 Yandex 会拿到 stub 页,_search_yandex 抛
    # YandexRegionBlockedError 后被 gather 收进 errors["yandex"]。AI 必须告诉主人
    # 这是真人/X 搜图缺关键引擎(SauceNAO/ascii2d 主覆盖二次元,搜不到真人来源)。
    yandex_blocked = "YandexRegionBlockedError" in str(errors.get("yandex", ""))
    has_yandex_in_engines = any(e in (engines_arg or []) or kind in ("photo", "auto", "artwork")
                                for e in ["yandex"])

    payload: dict[str, Any] = {
        "kind": kind,
        "image_index": image_index,
        "image_url": target_url,
        "candidate_count": len(ordered),
        "count": len(results),
        "results": [
            {
                "source": r.source,
                "title": r.title,
                "url": r.url,
                "similarity": round(r.similarity, 2),
                "author": r.author,
                "kind": r.kind,
                "extra": r.extra,
            }
            for r in results[:8]
        ],
        "context_text": format_image_search_summary(image_ref, results, errors),
    }
    if errors:
        payload["engine_errors"] = errors
    if yandex_blocked:
        payload["yandex_blocked"] = True
        payload["yandex_blocked_hint"] = (
            "Yandex(ya.ru 备用域名也)拿到 stub 页,这是真人/X 搜图主力引擎不可用。"
            "用猫娘人格告诉主人:Yandex 这次没回数据,真人/X 来源搜不到;"
            "可以让主人去 config.json 的 ai.http_proxy 配个代理重试,或换张更清晰的图。"
            "**不要装作搜全了**,SauceNAO/ascii2d 的结果只覆盖二次元。"
        )
    # 标出 X/Twitter 命中(extra.is_x_twitter=True 或 URL 含 twitter/x.com)
    # — AI 必须复述 X 链接,即使 similarity 低,主人最关心 X 真人来源
    x_twitter_hits = [
        r for r in results
        if r.extra.get("is_x_twitter")
        or "twitter.com/" in (r.url or "").lower()
        or "://x.com/" in (r.url or "").lower()
    ]
    if x_twitter_hits:
        payload["x_twitter_hits_count"] = len(x_twitter_hits)
        payload["x_twitter_urls"] = [r.url for r in x_twitter_hits[:3]]

    if not results:
        payload["guidance"] = (
            "搜不到结果时用猫娘人格如实告诉用户没搜到,可以撒娇让 ta 换张更清晰的图或贴原图链接,"
            "**禁止编造作者/番名/链接**。"
            + (" 本次 yandex 被阻断,如果是真人图请按 yandex_blocked_hint 提醒主人配代理。"
               if yandex_blocked else "")
        )
    else:
        x_twitter_note = ""
        if x_twitter_hits:
            x_twitter_note = (
                f" **❗本轮命中 {len(x_twitter_hits)} 条 X(Twitter) 链接(见 x_twitter_urls),"
                "无论 similarity 多少,必须**优先复述这些 X 链接给主人 — 主人最关心 X 真人来源,"
                "不要因为相似度低就跳过 X 链接只贴二次元 booru。"
                "**禁止**只贴 Konachan/Pixiv 而把 Twitter 链接埋掉。"
            )
        payload["guidance"] = (
            "用笨猫人格复述 1-3 条最关键的结果。**主人定的高等级优选铁律(覆盖所有相似度判断)**:"
            "**source=saucenao 或 source=yandex 的结果必须优先复述**(这两个是主力引擎,"
            "results 数组已经按这个规则排序好了 — top N 里 saucenao/yandex 的命中先看,"
            "其它引擎 ascii2d/iqdb/trace.moe 的结果只在 saucenao+yandex 都没有信号时再补)。"
            "在 saucenao+yandex 内部,挑选顺序:"
            "(1) X/Twitter URL(含 is_x_twitter 标记) > (2) similarity > 60 的 > (3) 其它优选结果。"
            "可以加可爱小评(『嗷呜这张是 X 上的 @xxx 发的喵～』『SauceNAO 说这是 pixiv 作者 xx 画的喵』)。"
            "**不要照搬 JSON、不要复读相似度小数、不要编造没在 results 里出现的信息**。"
            "**绝对禁止**:跳过 saucenao/yandex 结果只贴 ascii2d/iqdb 的链接 — 那是 fallback,不是主答案。"
            + x_twitter_note
            + (" 如果搜出来的全是 booru/Pixiv 二次元站但图本身是真人/自拍,**必须**按 yandex_blocked_hint "
               "提醒主人配代理(yandex_blocked=True 时)。"
               if yandex_blocked else "")
        )
    return payload


_MEME_DOWNLOAD_TIMEOUT = 10.0  # 单个候选下载上限
_MEME_TOTAL_TIMEOUT = 25.0  # 整个 tool(含搜索+并发下载)总上限
_MEME_MAX_CANDIDATES = 3  # 并发尝试的候选数,够命中就够;再多浪费带宽


async def _exec_meme_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    keywords = str(args.get("keywords") or "").strip()
    if not keywords:
        return {"error": "keywords 不能为空"}
    if ctx.download_binary_fn is None:
        return {"error": "运行环境未注入下载器,无法拉图"}

    try:
        return await asyncio.wait_for(
            _meme_query_impl(keywords, ctx),
            timeout=_MEME_TOTAL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _logger.info("Meme tool timed out (>%.0fs) for %r", _MEME_TOTAL_TIMEOUT, keywords)
        return {
            "error": (
                f"搜图超过 {int(_MEME_TOTAL_TIMEOUT)}s 没出图,网线可能阻了。"
                "用文字回应即可,也可以给 1-2 个备用关键词让群友自己搜。"
            )
        }


async def _meme_query_impl(keywords: str, ctx: ToolContext) -> dict[str, Any]:
    """实际搜+下载逻辑。被 wait_for 包裹,任何点超 25s 都会被取消。"""
    try:
        image_urls = await search_image_urls(ctx.config, keywords[:80], max_results=_MEME_MAX_CANDIDATES * 2)
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("Meme search failed for %r: %s", keywords, exc)
        return {"error": f"搜图失败: {exc.__class__.__name__}: {exc}"}
    if not image_urls:
        return {"error": "没找到合适的梗图,用文字回复即可"}

    candidates = image_urls[:_MEME_MAX_CANDIDATES]

    async def _fetch_one(url: str) -> tuple[bytes, str, str] | None:
        try:
            data, content_type = await ctx.download_binary_fn(
                ctx.config, url, timeout=_MEME_DOWNLOAD_TIMEOUT
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            _logger.info("Meme candidate download failed (%s): %s", url, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Meme candidate fetch unexpected error: %s", exc)
            return None
        if not data:
            return None
        if content_type and not content_type.lower().startswith("image/"):
            return None
        return data, content_type, url

    tasks = [asyncio.create_task(_fetch_one(url)) for url in candidates]
    winner: tuple[bytes, str, str] | None = None
    try:
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result is not None:
                winner = result
                break
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        # 让取消传播完
        await asyncio.gather(*tasks, return_exceptions=True)

    if winner is None:
        return {"error": "拿到候选 URL 但全部下载失败,用文字回复即可"}
    image_data, _content_type, source_url = winner
    uri = "base64://" + base64.b64encode(image_data).decode("ascii")
    return {
        "image_uri": uri,
        "source_url": source_url,
        "keywords": keywords,
        "note": (
            "把这个 image_uri **完整原样**用 <<<CATTY_INLINE_IMAGE:URI>>> 标记嵌进最终回复想展示图的位置。"
            "切勿把 URI 截断、切勿单独输出 URI,也不要把 source_url 贴出来当链接。"
        ),
    }


# ── catty_imagegen: 主 AI 主动生图(OpenAI Image API + NovelAI) ─────────

_ALLOWED_IMAGEGEN_QUALITY = {"low", "medium", "high", "auto"}
_IMAGEGEN_FMT_TO_EXT = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp"}

# NovelAI 三个标准尺寸(主人指定默认)。Opus 订阅档这三个 + steps<=28 + 单张时免 Anlas。
_NAI_ASPECT_MAP: dict[str, tuple[int, int]] = {
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "square": (1024, 1024),
}

# NAI v4 PositionMap: 合法离散坐标 {0.1, 0.3, 0.5, 0.7, 0.9}。语义化位置 → (x, y)。
# auto = (0, 0) 是 AUTO 哨兵, use_coords 会因此置 False(AI's Choice)。
_NAI_POSITION_MAP: dict[str, tuple[float, float]] = {
    "auto": (0.0, 0.0),
    "center": (0.5, 0.5),
    "left": (0.1, 0.5),
    "right": (0.9, 0.5),
    "top": (0.5, 0.1),
    "bottom": (0.5, 0.9),
    "top-left": (0.1, 0.1),
    "top-right": (0.9, 0.1),
    "bottom-left": (0.1, 0.9),
    "bottom-right": (0.9, 0.9),
}
_NAI_VIBE_REFERENCE_MAX = 4  # NAI UI 实测上限 4 张参考图

# 这两个常量在 module top 提前定义,因为下方 `_NAI_DIRECTOR_SCHEMA` 字面量初始化时要引用。
_NAI_IMAGE_ENDPOINT = "https://image.novelai.net/ai/generate-image"
_NAI_AUGMENT_ENDPOINT = "https://image.novelai.net/ai/augment-image"


def _nai_predict_anlas(
    width: int,
    height: int,
    steps: int,
    *,
    sm: bool = False,
    sm_dyn: bool = False,
    n_samples: int = 1,
) -> int:
    """NovelAI Anlas 消耗预测(v3/v4/v4.5 通用公式)。

    Opus tier3 在 1048576 像素以下 + steps<=28 + n_samples=1 实测免费(diff=0),
    所以扣费基于这个公式 + 调用方在 Opus 免费档时把结果当 0 处理。
    公式来源: LlmKira/novelai-python _cost.py + tapwavezodiac wiki。
    """
    import math
    r = max(int(width) * int(height), 65536)
    smea_factor = 1.4 if sm_dyn else (1.2 if sm else 1.0)
    per_sample = math.ceil(
        2.951823174884865e-21 * r
        + 5.753298233447344e-7 * r * int(steps)
    ) * smea_factor
    per_sample = max(int(per_sample), 2)
    return per_sample * max(int(n_samples), 1)


def _nai_is_opus_free(width: int, height: int, steps: int, n_samples: int) -> bool:
    """三个标准尺寸 + steps<=28 + 单张时 Opus tier3 免 Anlas。"""
    return (
        int(width) * int(height) <= 1048576
        and int(steps) <= 28
        and int(n_samples) <= 1
    )


def _nai_director_billable_anlas(req_type: str, width: int, height: int) -> int:
    """Director tools 在 Opus tier3 的 billable Anlas。

    - bg-removal: cost*3+5 (cost=generate_anlas at steps=28)
    - 其他在 ≤1048576px Opus 档全 0; 大尺寸时 fallback 到 generate_anlas
    """
    base = _nai_predict_anlas(width, height, 28)
    if req_type == "bg-removal":
        return base * 3 + 5
    if req_type in _NAI_DIRECTOR_OPUS_FREE and width * height <= 1048576:
        return 0
    return base


def _resize_to_vibe_reference_png(data: bytes) -> str:
    """把任意图片(PNG/JPEG/WEBP)读出来,resize 到 448x448(letterbox 黑底)再 base64 PNG。

    NAI v4 reference_image_multiple 字段固定吃 448x448 PNG base64。
    """
    from PIL import Image as PILImage
    import io
    src = PILImage.open(io.BytesIO(data)).convert("RGBA")
    canvas = PILImage.new("RGBA", (448, 448), (0, 0, 0, 0))
    # letterbox: 保持宽高比缩放到 448 内,居中贴
    src.thumbnail((448, 448), PILImage.LANCZOS)
    off_x = (448 - src.width) // 2
    off_y = (448 - src.height) // 2
    canvas.paste(src, (off_x, off_y), src)
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _png_jpg_dimensions(data: bytes) -> tuple[int, int]:
    """从 PNG/JPEG/WEBP 字节流读 (width, height),用 PIL,不 reify decode。"""
    from PIL import Image as PILImage
    import io
    with PILImage.open(io.BytesIO(data)) as im:
        return int(im.width), int(im.height)


def _to_png_base64(data: bytes) -> str:
    """把任意图片字节统一转 PNG base64(NAI augment-image 输入要 PNG base64)。"""
    from PIL import Image as PILImage
    import io
    with PILImage.open(io.BytesIO(data)) as im:
        if im.mode not in ("RGBA", "RGB", "L"):
            im = im.convert("RGBA")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def _exec_imagegen_nai(
    *,
    prompt: str,
    negative_prompt: str,
    aspect: str,
    ctx: ToolContext,
    characters: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """NovelAI 生图执行器。被 _exec_imagegen 在 provider='nai' 时调用。

    返回结构和 _exec_imagegen 保持一致(image_sent / mode / cost / balance_after / guidance)。

    characters: 多角色数组,每项 {prompt, negative_prompt?, position?}。最多 6 个。
    references: vibe transfer/precise reference 数组,每项 {extracted?, strength?}。
                数量决定从 ctx.input_image_urls / recent_image_urls 取多少张参考图。
                最多 _NAI_VIBE_REFERENCE_MAX=4 张。
    """
    from pathlib import Path
    if not getattr(ctx.config, "catty_imagegen_nai_enabled", False):
        return {
            "error": "NovelAI 通道未启用,请改用 provider='gpt' 重试",
            "guidance": "config.json imagegen.nai.enabled=true 才可用。",
        }
    token = str(getattr(ctx.config, "catty_imagegen_nai_token", "") or "").strip()
    if not token:
        return {"error": "NovelAI token 未配置"}

    aspect_key = (aspect or "").strip().lower() or str(
        getattr(ctx.config, "catty_imagegen_nai_default_aspect", "portrait") or "portrait"
    ).strip().lower()
    if aspect_key not in _NAI_ASPECT_MAP:
        aspect_key = "portrait"
    width, height = _NAI_ASPECT_MAP[aspect_key]

    steps = max(int(getattr(ctx.config, "catty_imagegen_nai_steps", 28) or 28), 1)
    scale = float(getattr(ctx.config, "catty_imagegen_nai_scale", 5.0) or 5.0)
    sampler = str(getattr(ctx.config, "catty_imagegen_nai_sampler", "k_euler_ancestral") or "k_euler_ancestral").strip()
    noise_schedule = str(getattr(ctx.config, "catty_imagegen_nai_noise_schedule", "karras") or "karras").strip()
    model = str(getattr(ctx.config, "catty_imagegen_nai_model", "nai-diffusion-4-5-full") or "nai-diffusion-4-5-full").strip()
    timeout = float(getattr(ctx.config, "catty_imagegen_nai_timeout_seconds", 180.0) or 180.0)
    default_neg = str(getattr(ctx.config, "catty_imagegen_nai_default_negative", "") or "").strip()
    base_points = int(getattr(ctx.config, "catty_imagegen_nai_base_points", 5) or 5)
    pts_per_anlas = int(getattr(ctx.config, "catty_imagegen_nai_points_per_anlas", 3) or 3)

    neg = (negative_prompt or "").strip() or default_neg
    has_refs = bool(references)

    # references(vibe transfer): v4.5/v4 实测 500, 只有 v3 后端接受。带 references 时强制 v3 model。
    vibe_fallback = str(
        getattr(ctx.config, "catty_imagegen_nai_vibe_fallback_model", "nai-diffusion-3")
        or "nai-diffusion-3"
    ).strip()
    if has_refs and not model.startswith("nai-diffusion-3"):
        _logger.info("imagegen[nai] references requested, fallback model %s → %s", model, vibe_fallback)
        model = vibe_fallback

    # Anlas 预测(Opus 免费档归零; 带 references 走 i2i 计价不享受免费档)
    predicted_anlas = _nai_predict_anlas(width, height, steps, n_samples=1)
    if not has_refs and _nai_is_opus_free(width, height, steps, 1):
        billable_anlas = 0
    else:
        billable_anlas = predicted_anlas
    cost = image_cost_for_nai(billable_anlas, base=base_points, per_anlas=pts_per_anlas)

    # ── 积分扣费 guard(主人豁免;余额不够告诉 AI 让她提醒用户签到) ──
    affection = getattr(ctx, "affection_store", None)
    if affection is not None and cost > 0 and ctx.user_id:
        balance = affection.get_points(ctx.user_id)
        if not affection.is_owner(ctx.user_id) and balance < cost:
            level, _exp = affection.get_level_and_exp(ctx.user_id)
            lo, hi = predict_checkin_range(level)
            return {
                "error": "积分不够,无法生图",
                "balance": balance,
                "cost": cost,
                "shortfall": cost - balance,
                "user_level": level,
                "today_checkin_estimate": f"{lo}-{hi}",
                "user_facing_hint": (
                    f"用猫娘口吻提醒用户:他当前只有 {balance} 积分,这张图(NAI/{aspect_key})要 {cost} 分,"
                    f"还差 {cost - balance} 分。让他发『签到』来领今天的积分,"
                    f"他现在好感等级 Lv{level},今天签到大概能拿 {lo}-{hi} 分。"
                    "傲娇但要把要点说全:差多少、要 Lv 几、发『签到』两个字就能领。"
                    "**禁止**自己再调一次 catty_imagegen 重复发。"
                ),
            }

    # cooldown 复用 gpt 路径的全局 dict(同 user_id 的 cd 桶)
    cd_seconds = max(int(getattr(ctx.config, "catty_imagegen_cooldown_seconds", 60) or 0), 0)
    if cd_seconds > 0:
        owner_qq = str(getattr(ctx.config, "catty_owner_qq", "") or "").strip()
        if not owner_qq or ctx.user_id != owner_qq:
            cd_key = ctx.user_id or "anonymous"
            now = time.monotonic()
            last = _imagegen_cooldowns.get(cd_key, 0.0)
            remaining = max(last + cd_seconds - now, 0.0)
            if remaining > 0:
                return {"error": f"生图冷却剩 {int(remaining)}s,稍后再戳人家喵"}
            _imagegen_cooldowns[cd_key] = now

    # ── v4 系列必须传结构化 v4_prompt/v4_negative_prompt,v3 沿用旧 schema ──
    parameters: dict[str, Any] = {
        "width": width,
        "height": height,
        "scale": scale,
        "sampler": sampler,
        "steps": steps,
        "n_samples": 1,
        "seed": 0,
        "negative_prompt": neg,
        "ucPreset": 0,
        "qualityToggle": True,
        "sm": False,
        "sm_dyn": False,
        "dynamic_thresholding": False,
        "cfg_rescale": 0,
        "noise_schedule": noise_schedule,
        "legacy": False,
    }
    # ── characters: 多角色配置 (最多 6 个) ──
    chars = [c for c in (characters or []) if isinstance(c, dict) and (c.get("prompt") or "").strip()]
    chars = chars[:6]
    char_prompts: list[dict[str, Any]] = []
    char_captions: list[dict[str, Any]] = []
    neg_captions: list[dict[str, Any]] = []
    use_coords = False
    for c in chars:
        cp = (c.get("prompt") or "").strip()
        cu = (c.get("negative_prompt") or "").strip()
        pos_key = (c.get("position") or "auto").strip().lower()
        cx, cy = _NAI_POSITION_MAP.get(pos_key, (0.0, 0.0))
        if cx > 0 or cy > 0:
            use_coords = True
        char_prompts.append({
            "prompt": cp,
            "uc": cu,
            "center": {"x": cx, "y": cy},
            "enabled": True,
        })
        char_captions.append({"char_caption": cp, "centers": [{"x": cx, "y": cy}]})
        neg_captions.append({"char_caption": cu, "centers": [{"x": cx, "y": cy}]})

    # ── references: vibe transfer / precise reference (最多 4 张) ──
    refs = [r if isinstance(r, dict) else {} for r in (references or [])][:_NAI_VIBE_REFERENCE_MAX]
    reference_b64: list[str] = []
    reference_extracted: list[float] = []
    reference_strength: list[float] = []
    if refs:
        # 从 ctx 拿候选图 URL (同当前消息 > 群最近)
        candidate_urls: list[str] = []
        candidate_urls.extend(getattr(ctx, "input_image_urls", []) or [])
        candidate_urls.extend(getattr(ctx, "recent_image_urls", []) or [])
        seen: set[str] = set()
        candidates_unique: list[str] = []
        for u in candidate_urls:
            if not u or u in seen:
                continue
            seen.add(u)
            candidates_unique.append(u)
        if len(candidates_unique) < len(refs):
            return {
                "error": (
                    f"references 要 {len(refs)} 张参考图,但当前消息和群最近图只够 "
                    f"{len(candidates_unique)} 张。少填几项 references 或叫用户多发几张参考图。"
                ),
            }
        if ctx.download_binary_fn is None:
            return {"error": "运行环境没注入下载器,无法走 reference 模式"}
        # 下载 + resize 到 448×448 PNG base64
        for i, r in enumerate(refs):
            ref_url = candidates_unique[i]
            try:
                data, ctype = await ctx.download_binary_fn(
                    ctx.config, ref_url, timeout=30.0
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                _logger.info("imagegen[nai] reference download failed %s: %s", ref_url, exc)
                return {"error": f"参考图 #{i+1} 下载失败,过 30s 再试或减少 references 数量"}
            if not data:
                return {"error": f"参考图 #{i+1} 内容为空"}
            try:
                ref_b64 = _resize_to_vibe_reference_png(data)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("imagegen[nai] reference resize failed: %s", exc)
                return {"error": f"参考图 #{i+1} 转 PNG 失败: {exc}"}
            reference_b64.append(ref_b64)
            try:
                ex_v = float(r.get("extracted") if r.get("extracted") is not None else 0.6)
            except (TypeError, ValueError):
                ex_v = 0.6
            try:
                st_v = float(r.get("strength") if r.get("strength") is not None else 0.6)
            except (TypeError, ValueError):
                st_v = 0.6
            reference_extracted.append(max(0.0, min(1.0, ex_v)))
            reference_strength.append(max(0.0, min(1.0, st_v)))

    if model.startswith("nai-diffusion-4"):
        parameters["params_version"] = 3
        parameters["v4_prompt"] = {
            "caption": {"base_caption": prompt, "char_captions": char_captions},
            "use_coords": use_coords,
            "use_order": True,
        }
        parameters["v4_negative_prompt"] = {
            "caption": {"base_caption": neg, "char_captions": neg_captions},
            "legacy_uc": False,
        }
        parameters["characterPrompts"] = char_prompts
        if char_prompts and use_coords:
            parameters["use_coords"] = True

    if reference_b64:
        parameters["reference_image_multiple"] = reference_b64
        parameters["reference_information_extracted_multiple"] = reference_extracted
        parameters["reference_strength_multiple"] = reference_strength
        parameters["normalize_reference_strength_multiple"] = True

    payload: dict[str, Any] = {
        "input": prompt,
        "model": model,
        "action": "generate",
        "parameters": parameters,
    }

    proxy_str = str(getattr(ctx.config, "catty_http_proxy", "") or "").strip()
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=15.0),
        "follow_redirects": True,
        "http2": False,
        "limits": httpx.Limits(max_keepalive_connections=0, max_connections=10),
    }
    if proxy_str:
        client_kwargs["proxy"] = proxy_str

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(
                _NAI_IMAGE_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                },
                json=payload,
            )
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        elapsed = time.monotonic() - started
        _logger.warning(
            "imagegen[nai] transport error after %.1fs: %s: %s (model=%s %dx%d steps=%d prompt_len=%d)",
            elapsed, exc.__class__.__name__, exc or "(empty repr)",
            model, width, height, steps, len(prompt),
        )
        return {
            "error": f"NovelAI 接口连不上(elapsed {elapsed:.0f}s): {exc.__class__.__name__}: {exc}",
            "retry_guidance": "网络或上游异常;过 30s 再试,或改 provider='gpt'。",
        }

    elapsed = time.monotonic() - started
    if response.status_code != 200:
        detail = response.text[:400]
        _logger.warning(
            "imagegen[nai] status=%d elapsed=%.1fs model=%s %dx%d steps=%d prompt_len=%d body=%s",
            response.status_code, elapsed, model, width, height, steps, len(prompt), detail,
        )
        # NAI 402 = Anlas 不够; 401/403 = token 失效; 429 = 速率
        if response.status_code == 401:
            return {"error": "NovelAI token 失效或未授权,改 provider='gpt' 重试"}
        if response.status_code == 402:
            return {"error": "NovelAI 账户 Anlas 余额不足,改 provider='gpt' 重试或精简到三个标准尺寸"}
        if response.status_code == 429:
            return {"error": "NovelAI 触发速率限制,30 秒后再试或改 provider='gpt'"}
        return {"error": f"NovelAI HTTP {response.status_code}: {detail[:300]}"}

    # 响应是 zip,里面有 image_0.png
    try:
        import io
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = zf.namelist()
        if not names:
            return {"error": "NovelAI 响应 zip 是空的"}
        image_bytes = zf.read(names[0])
    except zipfile.BadZipFile:
        # 偶尔上游会回 application/json 错误,但 status=200(罕见)
        preview = response.content[:200]
        try:
            preview_text = preview.decode("utf-8", errors="replace")
        except Exception:
            preview_text = repr(preview)
        return {"error": f"NovelAI 响应不是 zip: {preview_text[:200]}"}
    except (OSError, ValueError) as exc:
        return {"error": f"NovelAI zip 解包失败: {exc}"}

    if not image_bytes:
        return {"error": "NovelAI 解包后图片为空"}

    cache_dir = _imagegen_cache_dir(ctx.config)
    fname = f"nai_{int(time.time()*1000)}.png"
    file_path = cache_dir / fname
    try:
        file_path.write_bytes(image_bytes)
    except OSError as exc:
        return {"error": f"写图片缓存失败: {exc}"}

    try:
        from nonebot.adapters.onebot.v11 import MessageSegment
    except ImportError:
        return {"error": "MessageSegment 不可用,运行环境异常"}
    segment = MessageSegment.image(file=file_path.resolve().as_uri())
    ctx.pending_image_segments.append(segment)
    _prune_imagegen_cache(ctx.config)

    _logger.info(
        "imagegen[nai]: model=%s aspect=%s %dx%d steps=%d bytes=%d elapsed=%.1fs "
        "anlas_predicted=%d billable_anlas=%d cost=%d file=%s prompt=%r",
        model, aspect_key, width, height, steps, len(image_bytes), elapsed,
        predicted_anlas, billable_anlas, cost, file_path.name,
        prompt[:300] + ("...(+%d)" % (len(prompt) - 300) if len(prompt) > 300 else ""),
    )

    consume_result: dict[str, Any] = {}
    if affection is not None and cost > 0 and ctx.user_id:
        try:
            consume_result = affection.consume_points(ctx.user_id, cost)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("imagegen[nai]: consume_points failed (image still sent): %s", exc)
    balance_after = int(consume_result.get("balance_after", -1))
    is_owner_charge = bool(consume_result.get("is_owner"))

    return {
        "image_sent": True,
        "mode": "generate",
        "provider": "nai",
        "model": model,
        "aspect": aspect_key,
        "size": f"{width}x{height}",
        "steps": steps,
        "bytes": len(image_bytes),
        "elapsed_seconds": round(elapsed, 1),
        "anlas_predicted": predicted_anlas,
        "anlas_billable": billable_anlas,
        "cost": cost,
        "balance_after": balance_after,
        "is_owner_charge": is_owner_charge,
        "guidance": (
            "图已经程序自动发出去了,你只需补 1-2 句猫娘短评(『画好啦~主人看看喜不喜欢喵 ฅฅ』)。"
            "**禁止**贴 base64 / file 路径 / image_uri 到回复里;**禁止**再调一次 catty_imagegen 重复发。"
            + (
                ""
                if is_owner_charge
                else f" 本次消耗 {cost} 积分,该用户剩余 {balance_after} 分(余额低于 100 时可以提一句『再画就要签到啦喵~』,不用强调)。"
            )
        ),
    }


async def _exec_nai_director(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """NovelAI Director Tools (/ai/augment-image): lineart/sketch/colorize/emotion/declutter/bg-removal/transform。"""
    from pathlib import Path
    if not getattr(ctx.config, "catty_imagegen_enabled", True):
        return {"error": "imagegen 已被配置禁用"}
    if not getattr(ctx.config, "catty_imagegen_nai_enabled", False):
        return {"error": "NovelAI 通道未启用 (imagegen.nai.enabled=false)"}
    if not getattr(ctx, "is_directly_requested", True):
        return {
            "error": "用户没直接 @ 猫猫,不允许主动调 director tool。",
            "guidance": "director 只能在用户明确指向猫猫(@ / 引用回复 / 直呼猫猫) + 明确要求加工时调。",
        }
    req_type = str(args.get("req_type") or "").strip().lower()
    if req_type not in _NAI_DIRECTOR_REQ_TYPES:
        return {"error": f"req_type 必须是 {list(_NAI_DIRECTOR_REQ_TYPES)} 之一,收到: {req_type!r}"}

    prompt = str(args.get("prompt") or "").strip()
    if req_type == "emotion":
        if not prompt or ";;" not in prompt:
            return {
                "error": (
                    "emotion 必须传 prompt='<mood>;;<text>' 格式,例 'happy;;wide bright smile'。"
                    f"合法 mood: {', '.join(_NAI_EMOTION_MOODS)}。"
                ),
            }
        mood_part = prompt.split(";;", 1)[0].strip().lower()
        if mood_part not in _NAI_EMOTION_MOODS:
            return {
                "error": (
                    f"emotion mood 不合法({mood_part!r})。从这里选: {', '.join(_NAI_EMOTION_MOODS)}"
                ),
            }
    try:
        defry = int(args.get("defry") or 0)
    except (TypeError, ValueError):
        defry = 0
    defry = max(0, min(5, defry))

    token = str(getattr(ctx.config, "catty_imagegen_nai_token", "") or "").strip()
    if not token:
        return {"error": "NovelAI token 未配置"}
    timeout = float(getattr(ctx.config, "catty_imagegen_nai_timeout_seconds", 180.0) or 180.0)
    base_points = int(getattr(ctx.config, "catty_imagegen_nai_base_points", 5) or 5)
    pts_per_anlas = int(getattr(ctx.config, "catty_imagegen_nai_points_per_anlas", 3) or 3)

    # 拿输入图: 当前消息附图优先, fallback 群最近图
    candidate_urls: list[str] = []
    candidate_urls.extend(getattr(ctx, "input_image_urls", []) or [])
    candidate_urls.extend(getattr(ctx, "recent_image_urls", []) or [])
    seen: set[str] = set()
    candidates_unique: list[str] = []
    for u in candidate_urls:
        if not u or u in seen:
            continue
        seen.add(u)
        candidates_unique.append(u)
    if not candidates_unique:
        return {
            "error": "director tool 需要一张输入图片,但当前消息和群最近 5 分钟都没有可用图片。",
            "guidance": "让用户先把要加工的图发出来,再让你调这个 tool。",
        }
    if ctx.download_binary_fn is None:
        return {"error": "运行环境没注入下载器,无法走 director 模式"}

    image_bytes_input: bytes | None = None
    input_image_source: str = ""
    for url_candidate in candidates_unique[:3]:
        try:
            data, ctype = await ctx.download_binary_fn(
                ctx.config, url_candidate, timeout=30.0
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            _logger.info("director: input image download failed %s: %s", url_candidate, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            _logger.warning("director: unexpected download error: %s: %s", exc.__class__.__name__, exc)
            continue
        if not data:
            continue
        if ctype and not ctype.lower().startswith("image/"):
            continue
        image_bytes_input = data
        input_image_source = url_candidate
        break
    if image_bytes_input is None:
        return {"error": "input 候选图全部下载失败"}

    # 拿尺寸 + 转 PNG base64
    try:
        in_w, in_h = _png_jpg_dimensions(image_bytes_input)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"输入图解码失败: {exc}"}
    try:
        image_b64 = _to_png_base64(image_bytes_input)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"输入图转 PNG base64 失败: {exc}"}

    # 算 anlas + 积分
    billable_anlas = _nai_director_billable_anlas(req_type, in_w, in_h)
    cost = image_cost_for_nai(billable_anlas, base=base_points, per_anlas=pts_per_anlas)

    affection = getattr(ctx, "affection_store", None)
    if affection is not None and cost > 0 and ctx.user_id:
        balance = affection.get_points(ctx.user_id)
        if not affection.is_owner(ctx.user_id) and balance < cost:
            level, _exp = affection.get_level_and_exp(ctx.user_id)
            lo, hi = predict_checkin_range(level)
            return {
                "error": "积分不够,无法 director",
                "balance": balance,
                "cost": cost,
                "shortfall": cost - balance,
                "user_level": level,
                "today_checkin_estimate": f"{lo}-{hi}",
                "user_facing_hint": (
                    f"用猫娘口吻提醒用户:他当前只有 {balance} 积分,这次 director({req_type})要 {cost} 分,"
                    f"还差 {cost - balance} 分。让他发『签到』来领今天的积分,"
                    f"他现在好感等级 Lv{level},今天签到大概能拿 {lo}-{hi} 分。"
                    "**禁止**自己再调一次 catty_nai_director 重复发。"
                ),
            }

    cd_seconds = max(int(getattr(ctx.config, "catty_imagegen_cooldown_seconds", 60) or 0), 0)
    if cd_seconds > 0:
        owner_qq = str(getattr(ctx.config, "catty_owner_qq", "") or "").strip()
        if not owner_qq or ctx.user_id != owner_qq:
            cd_key = ctx.user_id or "anonymous"
            now = time.monotonic()
            last = _imagegen_cooldowns.get(cd_key, 0.0)
            remaining = max(last + cd_seconds - now, 0.0)
            if remaining > 0:
                return {"error": f"生图冷却剩 {int(remaining)}s,稍后再戳人家喵"}
            _imagegen_cooldowns[cd_key] = now

    payload: dict[str, Any] = {
        "req_type": req_type,
        "width": in_w,
        "height": in_h,
        "image": image_b64,
        "defry": defry,
    }
    if prompt:
        payload["prompt"] = prompt

    proxy_str = str(getattr(ctx.config, "catty_http_proxy", "") or "").strip()
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=15.0),
        "follow_redirects": True,
        "http2": False,
        "limits": httpx.Limits(max_keepalive_connections=0, max_connections=10),
    }
    if proxy_str:
        client_kwargs["proxy"] = proxy_str

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(
                _NAI_AUGMENT_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                },
                json=payload,
            )
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        elapsed = time.monotonic() - started
        _logger.warning(
            "director[%s] transport error after %.1fs: %s: %s (%dx%d)",
            req_type, elapsed, exc.__class__.__name__, exc or "(empty repr)", in_w, in_h,
        )
        return {
            "error": f"NovelAI director 接口连不上({elapsed:.0f}s): {exc.__class__.__name__}: {exc}",
            "retry_guidance": "网络或上游异常;过 30s 再试。",
        }

    elapsed = time.monotonic() - started
    if response.status_code != 200:
        detail = response.text[:400]
        _logger.warning(
            "director[%s] status=%d elapsed=%.1fs %dx%d body=%s",
            req_type, response.status_code, elapsed, in_w, in_h, detail,
        )
        if response.status_code == 401:
            return {"error": "NovelAI token 失效或未授权"}
        if response.status_code == 402:
            return {"error": "NovelAI 账户 Anlas 余额不足"}
        if response.status_code == 429:
            return {"error": "NovelAI 触发速率限制,30 秒后再试"}
        if response.status_code == 400:
            return {"error": f"director payload 不合法: {detail[:300]}"}
        return {"error": f"NovelAI director HTTP {response.status_code}: {detail[:300]}"}

    try:
        import io
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = zf.namelist()
        if not names:
            return {"error": "director 响应 zip 是空的"}
        out_image_bytes = zf.read(names[0])
    except zipfile.BadZipFile:
        preview = response.content[:200]
        try:
            preview_text = preview.decode("utf-8", errors="replace")
        except Exception:
            preview_text = repr(preview)
        return {"error": f"director 响应不是 zip: {preview_text[:200]}"}
    except (OSError, ValueError) as exc:
        return {"error": f"director zip 解包失败: {exc}"}

    if not out_image_bytes:
        return {"error": "director 解包后图片为空"}

    cache_dir = _imagegen_cache_dir(ctx.config)
    fname = f"director_{req_type.replace('-', '_')}_{int(time.time()*1000)}.png"
    file_path = cache_dir / fname
    try:
        file_path.write_bytes(out_image_bytes)
    except OSError as exc:
        return {"error": f"写图片缓存失败: {exc}"}

    try:
        from nonebot.adapters.onebot.v11 import MessageSegment
    except ImportError:
        return {"error": "MessageSegment 不可用,运行环境异常"}
    segment = MessageSegment.image(file=file_path.resolve().as_uri())
    ctx.pending_image_segments.append(segment)
    _prune_imagegen_cache(ctx.config)

    _logger.info(
        "director[%s]: %dx%d bytes=%d elapsed=%.1fs anlas=%d cost=%d file=%s source=%s",
        req_type, in_w, in_h, len(out_image_bytes), elapsed,
        billable_anlas, cost, file_path.name, input_image_source[:80] if input_image_source else "-",
    )

    consume_result: dict[str, Any] = {}
    if affection is not None and cost > 0 and ctx.user_id:
        try:
            consume_result = affection.consume_points(ctx.user_id, cost)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("director: consume_points failed (image still sent): %s", exc)
    balance_after = int(consume_result.get("balance_after", -1))
    is_owner_charge = bool(consume_result.get("is_owner"))

    return {
        "image_sent": True,
        "tool": "nai_director",
        "req_type": req_type,
        "size": f"{in_w}x{in_h}",
        "bytes": len(out_image_bytes),
        "elapsed_seconds": round(elapsed, 1),
        "anlas_billable": billable_anlas,
        "cost": cost,
        "balance_after": balance_after,
        "is_owner_charge": is_owner_charge,
        "guidance": (
            f"director({req_type}) 已自动发图。你只需补 1-2 句猫娘短评。"
            "**禁止**贴 base64/file 路径到回复;**禁止**再调一次 catty_nai_director 重复发。"
            + (
                ""
                if is_owner_charge
                else f" 本次消耗 {cost} 积分,该用户剩余 {balance_after} 分。"
            )
        ),
    }


def _imagegen_endpoint_url(base_url: str, *, edit: bool = False) -> str:
    """把 chat-completions 风格的 base_url 推算成 /images/{generations,edits} endpoint。

    用户的 base_url 通常是完整路径 `https://host/v1/chat/completions`(参见 _chat_completions_url),
    但 OpenAI Image API 是 `https://host/v1/images/generations` 或 `/edits`。砍掉常见 chat 后缀再拼。
    """
    base = base_url.strip().rstrip("/")
    if not base:
        return ""
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if base.endswith(suffix):
            base = base[: -len(suffix)] + "/v1"
            break
    tail = "edits" if edit else "generations"
    if base.endswith("/v1"):
        return f"{base}/images/{tail}"
    return f"{base}/v1/images/{tail}"


def _imagegen_cache_dir(config: Config) -> "Path":
    from pathlib import Path
    raw = str(getattr(config, "catty_imagegen_cache_dir", "pictures/imagegen_cache") or "pictures/imagegen_cache")
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune_imagegen_cache(config: Config) -> None:
    from pathlib import Path
    max_files = max(int(getattr(config, "catty_imagegen_cache_max_files", 200) or 200), 16)
    cache_dir = _imagegen_cache_dir(config)
    try:
        files = sorted(
            (p for p in cache_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return
    overflow = len(files) - max_files
    for stale in files[:overflow] if overflow > 0 else []:
        try:
            stale.unlink()
        except OSError:
            continue


async def _exec_imagegen(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from pathlib import Path
    if not getattr(ctx.config, "catty_imagegen_enabled", True):
        return {"error": "imagegen 已被配置禁用"}
    # 硬 guard:不指向猫猫的群消息(filter 顺便回的旁观回复)不允许主动画图,
    # 避免群里有人随口说『画一张...』就被猫猫接住乱画(主人明确禁止)。
    # 私聊/直接 @ 猫猫的群消息 is_directly_requested=True,放行。
    if not getattr(ctx, "is_directly_requested", True):
        return {
            "error": "用户没直接 @ 猫猫,不允许主动画图。把这条 tool_call 取消,改成纯文字回应。",
            "guidance": "imagegen 只在用户明确指向猫猫(@ / 引用回复 / 直呼猫猫)+ 说画时才能调。",
        }
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt 不能为空"}
    max_chars = max(int(getattr(ctx.config, "catty_imagegen_max_chars", 800) or 800), 80)
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars]

    # ── provider 分流: 'nai' 走 NovelAI, 其余走 OpenAI Image API ──
    provider = str(args.get("provider") or "gpt").strip().lower()
    if provider == "nai":
        if bool(args.get("use_input_image")):
            # NAI 不支持 edit, 把 use_input_image 显式拒掉, AI 才不会以为 silently 忽略了
            return {
                "error": "NovelAI 不支持 use_input_image=true(没有 img2img 走全图改的功能)。"
                "改 provider='gpt' 走 edit 模式,或保持 use_input_image=false 重试。",
            }
        chars_arg = args.get("characters")
        refs_arg = args.get("references")
        return await _exec_imagegen_nai(
            prompt=prompt,
            negative_prompt=str(args.get("negative_prompt") or "").strip(),
            aspect=str(args.get("aspect") or "").strip(),
            ctx=ctx,
            characters=chars_arg if isinstance(chars_arg, list) else None,
            references=refs_arg if isinstance(refs_arg, list) else None,
        )

    # cooldown(主人豁免)
    cd_seconds = max(int(getattr(ctx.config, "catty_imagegen_cooldown_seconds", 60) or 0), 0)
    if cd_seconds > 0:
        owner_qq = str(getattr(ctx.config, "catty_owner_qq", "") or "").strip()
        if not owner_qq or ctx.user_id != owner_qq:
            cd_key = ctx.user_id or "anonymous"
            now = time.monotonic()
            last = _imagegen_cooldowns.get(cd_key, 0.0)
            remaining = max(last + cd_seconds - now, 0.0)
            if remaining > 0:
                return {"error": f"生图冷却剩 {int(remaining)}s,稍后再戳人家喵"}
            _imagegen_cooldowns[cd_key] = now

    size = str(args.get("size") or getattr(ctx.config, "catty_imagegen_default_size", "1024x1024") or "1024x1024").strip()
    quality = str(args.get("quality") or getattr(ctx.config, "catty_imagegen_default_quality", "low") or "low").strip().lower()
    if quality not in _ALLOWED_IMAGEGEN_QUALITY:
        quality = "low"
    output_format = str(getattr(ctx.config, "catty_imagegen_default_format", "png") or "png").strip().lower()
    if output_format not in _IMAGEGEN_FMT_TO_EXT:
        output_format = "png"

    # ── 签到积分扣费 (主人豁免;余额不够告诉 AI 让她提醒用户签到) ──
    image_cost = image_cost_for_quality(quality)
    affection = getattr(ctx, "affection_store", None)
    if affection is not None and image_cost > 0:
        balance = affection.get_points(ctx.user_id) if ctx.user_id else 0
        if not affection.is_owner(ctx.user_id) and balance < image_cost:
            level, _exp = affection.get_level_and_exp(ctx.user_id)
            lo, hi = predict_checkin_range(level)
            return {
                "error": "积分不够,无法生图",
                "balance": balance,
                "cost": image_cost,
                "shortfall": image_cost - balance,
                "user_level": level,
                "today_checkin_estimate": f"{lo}-{hi}",
                "user_facing_hint": (
                    f"用猫娘口吻提醒用户:他当前只有 {balance} 积分,这张图(quality={quality})要 {image_cost} 分,"
                    f"还差 {image_cost - balance} 分。让他发『签到』来领今天的积分,"
                    f"他现在好感等级 Lv{level},今天签到大概能拿 {lo}-{hi} 分。"
                    "傲娇但要把要点说全:差多少、要 Lv 几、发『签到』两个字就能领。"
                    "**禁止**自己再调一次 catty_imagegen 重复发。"
                ),
            }

    base_url = str(getattr(ctx.config, "catty_imagegen_base_url", "") or getattr(ctx.config, "catty_openai_base_url", "") or "").strip().rstrip("/")
    api_key = str(getattr(ctx.config, "catty_imagegen_api_key", "") or getattr(ctx.config, "catty_openai_api_key", "") or "").strip()
    model = str(getattr(ctx.config, "catty_imagegen_model", "gpt-image-2") or "gpt-image-2").strip()
    timeout = float(getattr(ctx.config, "catty_imagegen_timeout_seconds", 120.0) or 120.0)
    if not base_url:
        return {"error": "imagegen base_url 没配,无法生图"}
    if not api_key:
        return {"error": "imagegen api_key 没配,无法生图"}

    # 决定走 generate 还是 edit 路径
    use_input_image = bool(args.get("use_input_image") or False)
    input_image_bytes: bytes | None = None
    input_image_source: str = ""
    if use_input_image:
        # 优先用当前消息附图,fallback 最近群里出现过的图
        candidate_urls: list[str] = []
        candidate_urls.extend(getattr(ctx, "input_image_urls", []) or [])
        candidate_urls.extend(getattr(ctx, "recent_image_urls", []) or [])
        # 去重保序
        seen: set[str] = set()
        candidates_unique: list[str] = []
        for u in candidate_urls:
            if not u or u in seen:
                continue
            seen.add(u)
            candidates_unique.append(u)
        if not candidates_unique:
            return {
                "error": (
                    "use_input_image=true 但当前消息和最近 5 分钟群里都没有可用图片;"
                    "请改成 use_input_image=false 重新调,走纯文字生成。"
                ),
            }
        if ctx.download_binary_fn is None:
            return {"error": "运行环境没注入下载器,无法走 edit 模式"}
        # 尝试下载第一张可用的图(失败就换下一张)。QQ CDN 偶尔慢,30s 比 15s 稳。
        for url_candidate in candidates_unique[:3]:
            try:
                data, content_type = await ctx.download_binary_fn(
                    ctx.config, url_candidate, timeout=30.0
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                _logger.info(
                    "imagegen edit: input image download failed %s: %s: %s",
                    url_candidate, exc.__class__.__name__, exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "imagegen edit: unexpected download error: %s: %s",
                    exc.__class__.__name__, exc,
                )
                continue
            if not data:
                continue
            ctype = (content_type or "").lower()
            if ctype and not ctype.startswith("image/"):
                continue
            input_image_bytes = data
            input_image_source = url_candidate
            break
        if input_image_bytes is None:
            return {"error": "input 候选图全部下载失败,改用 use_input_image=false 走纯生成重试"}

    url = _imagegen_endpoint_url(base_url, edit=bool(input_image_bytes))
    if not url:
        return {"error": "无法推算 imagegen endpoint URL"}
    # 强制 HTTP 绕过 CF: ai.hugou.cc 的 HTTPS 走 CF 反代,Origin Connection Timeout
    # 硬限 100s,长生图请求(150-200s)必然 524。HTTP 端口主人确认已经"没盾",直连 origin
    # 没有这个限制。chat_completion 短不受影响,保持 HTTPS。
    if bool(getattr(ctx.config, "catty_imagegen_force_http_scheme", True)) and url.startswith("https://"):
        url = "http://" + url[len("https://"):]
        _logger.info("imagegen: forced http:// scheme to bypass CF Origin Timeout (url=%s)", url)

    # 复用 catty 全局 http proxy(chat_completion 也用)。某些网络环境下直连 CF 容易
    # 长连接被切返回 524, 走 proxy 反而稳; proxy 空就直连(和原行为一致)。
    proxy_str = str(getattr(ctx.config, "catty_http_proxy", "") or "").strip()
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=15.0),
        "follow_redirects": True,
        # http2 强制关掉:某些 CF 边缘对 HTTP/2 长 body 上游 origin timeout 时
        # 不发完整 chunked end frame,导致 httpx 等到 read timeout 报模糊错误。
        # 用 HTTP/1.1 走 chunked,行为更可预测。
        "http2": False,
        # 关掉 keep-alive 连接池:实测 catty 同一 payload 直接 httpx.post 拿到 200,
        # 但 catty 进程内偶发 504 elapsed=69s——疑似长持有 client 的 keep-alive
        # 连接 stale, 复用时 ai.hugou.cc 那边已经断了导致 5xx。
        # 每个 imagegen 请求强制建新 TCP, 避免任何 stale connection 干扰。
        "limits": httpx.Limits(max_keepalive_connections=0, max_connections=10),
    }
    if proxy_str:
        client_kwargs["proxy"] = proxy_str

    if input_image_bytes is not None:
        headers_mp = {"Authorization": f"Bearer {api_key}"}
        files = {"image": ("input.png", input_image_bytes, "image/png")}
        data_fields: dict[str, str] = {
            "model": model, "prompt": prompt, "n": "1",
            "size": size, "quality": quality, "output_format": output_format,
        }
        request_call = lambda c: c.post(url, headers=headers_mp, data=data_fields, files=files)
    else:
        payload: dict[str, Any] = {
            "model": model, "prompt": prompt, "n": 1,
            "size": size, "quality": quality, "output_format": output_format,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        request_call = lambda c: c.post(url, headers=headers, json=payload)

    mode_hint = "edit" if input_image_bytes else "generate"
    # 524 真凶诊断(已通过 IP 直连绕过):ai.hugou.cc CF Free/Pro Origin Timeout 100s 给 catty 524。
    # 现在 imagegen 走 IP 直连不再撞 CF,但仍可能遇到两类瞬时错误:
    #   1) 504 上游 codex provider 慢回(用降级配置重试: low + 1024x1024 + prompt[:300])
    #   2) 503 + auth_unavailable: providers=codex 鉴权偶发失效(主人确认重试就好,不降级)
    # 所以 max_attempts=3:足够覆盖 504 降级 + auth 失败再来一次。
    max_attempts = 3
    backoff_seconds = 3.0
    response = None
    elapsed = 0.0
    last_transport_exc: Exception | None = None
    # 用 mutable 变量装当前 attempt 的参数,降级时改它
    cur_size = size
    cur_quality = quality
    cur_prompt = prompt

    def _build_request(c: httpx.AsyncClient):
        # 每次 attempt 都用当前 cur_* 重建 request body
        if input_image_bytes is not None:
            return c.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                data={
                    "model": model, "prompt": cur_prompt, "n": "1",
                    "size": cur_size, "quality": cur_quality, "output_format": output_format,
                },
                files={"image": ("input.png", input_image_bytes, "image/png")},
            )
        return c.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model, "prompt": cur_prompt, "n": 1,
                "size": cur_size, "quality": cur_quality, "output_format": output_format,
            },
        )

    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await _build_request(client)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            last_transport_exc = exc
            elapsed = time.monotonic() - started
            _logger.warning(
                "imagegen transport error attempt=%d/%d after %.1fs: %s: %s "
                "(mode=%s size=%s quality=%s prompt_len=%d url=%s proxy=%s)",
                attempt, max_attempts, elapsed, exc.__class__.__name__, exc or "(empty repr)",
                mode_hint, cur_size, cur_quality, len(cur_prompt), url, proxy_str or "none",
            )
            if attempt < max_attempts:
                # 降级再试
                cur_size = "1024x1024"
                cur_quality = "low"
                cur_prompt = cur_prompt[:300] if len(cur_prompt) > 300 else cur_prompt
                _logger.info(
                    "imagegen attempt %d will downgrade: size=%s quality=%s prompt_len=%d",
                    attempt + 1, cur_size, cur_quality, len(cur_prompt),
                )
                await asyncio.sleep(backoff_seconds)
                continue
            return {
                "error": (
                    f"生图接口连不上(走 {mode_hint},{max_attempts} 次都失败,最后一次 {elapsed:.0f}s): "
                    f"{exc.__class__.__name__}: {exc or '空 repr-可能 ReadTimeout/连接被切'}"
                ),
                "retry_guidance": "网络或上游异常;过 30s 再试。",
            }

        elapsed = time.monotonic() - started
        status = response.status_code
        if status in (502, 503, 504, 524) and attempt < max_attempts:
            hdrs = response.headers
            via_chain = ", ".join(
                f"{k}={hdrs.get(k, '-')}" for k in ("server", "via", "cf-ray", "cf-cache-status", "x-served-by")
            )
            body_preview = response.text[:200]
            # auth_unavailable: 上游 codex provider 鉴权偶发失效, 秒回 503, 降级没用。
            # 原 prompt/size/quality 不动直接重试, backoff 长一点(5s) 让 backend 恢复。
            is_auth = "auth_unavailable" in body_preview or "no auth available" in body_preview
            if is_auth:
                _logger.warning(
                    "imagegen API status=%d (RETRY auth_unavailable, no downgrade) elapsed=%.1fs "
                    "attempt=%d/%d mode=%s size=%s quality=%s prompt_len=%d %s body=%s",
                    status, elapsed, attempt, max_attempts, mode_hint,
                    cur_size, cur_quality, len(cur_prompt), via_chain, body_preview,
                )
                await asyncio.sleep(5.0)
                continue
            # 其他 5xx(主要 504/524) = 上游慢/CF 反代超时/瞬时网络, 第 N 次 retry 降级到最小配置
            # 把 body 也 log 出来,排查 504 body 是 nginx 上游 timeout 错误页 / Caddy 错误页 / 空
            _logger.warning(
                "imagegen API status=%d (RETRY with DOWNGRADE) elapsed=%.1fs attempt=%d/%d "
                "mode=%s size=%s quality=%s prompt_len=%d %s body=%s",
                status, elapsed, attempt, max_attempts, mode_hint, cur_size, cur_quality, len(cur_prompt), via_chain, body_preview,
            )
            cur_size = "1024x1024"
            cur_quality = "low"
            cur_prompt = cur_prompt[:300] if len(cur_prompt) > 300 else cur_prompt
            _logger.info(
                "imagegen attempt %d will downgrade: size=%s quality=%s prompt_len=%d",
                attempt + 1, cur_size, cur_quality, len(cur_prompt),
            )
            await asyncio.sleep(backoff_seconds)
            continue
        break  # 200 或 non-retryable error, 跳出

    # 把降级后的实际参数同步回原变量,让后续日志看到 ground truth
    size = cur_size
    quality = cur_quality
    prompt = cur_prompt

    if response is None:
        # 不应该到这,但安全兜底(类型检查也满意)
        return {"error": f"生图接口失败: {last_transport_exc!r}"}

    if response.status_code >= 400:
        detail = response.text[:300]
        hdrs = response.headers
        via_chain = ", ".join(
            f"{k}={hdrs.get(k, '-')}" for k in ("server", "via", "cf-ray", "cf-cache-status", "x-served-by")
        )
        _logger.warning(
            "imagegen API status=%d elapsed=%.1fs mode=%s size=%s quality=%s prompt_len=%d %s detail=%s",
            response.status_code, elapsed, mode_hint, size, quality, len(prompt), via_chain, detail,
        )
        if response.status_code in (502, 503, 504, 524):
            return {
                "error": (
                    f"生图上游网关超时(HTTP {response.status_code},{max_attempts} 次都没过)。"
                    "可能上游 origin 实际返回了但 Cloudflare 130s 已断,响应丢了。"
                ),
                "retry_guidance": (
                    "精简 prompt 到 300 字以内 + quality=low + size=1024x1024 再调,或过 30s 重试。"
                ),
                "user_facing_hint": (
                    "可以对用户说:『主人这次画图被上游网关挡住啦(尾巴垂垂),"
                    "猫猫稍等再试,或者主人精简下要求~』"
                ),
            }
        return {"error": f"生图接口 HTTP {response.status_code}: {detail[:200]}"}

    try:
        data = response.json()
    except ValueError as exc:
        return {"error": f"生图响应不是 JSON: {exc}"}

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return {"error": "生图响应没有 data[] 字段"}
    item0 = items[0] if isinstance(items[0], dict) else {}
    b64 = item0.get("b64_json") or item0.get("b64")
    if not isinstance(b64, str) or not b64:
        # 有的实现走 url 字段;暂不下载,直接报错让 AI 知道
        return {"error": "生图响应 data[0] 没有 b64_json 字段"}

    try:
        image_bytes = base64.b64decode(b64)
    except (ValueError, TypeError) as exc:
        return {"error": f"b64 解码失败: {exc}"}
    if not image_bytes:
        return {"error": "解码后图片为空"}

    cache_dir = _imagegen_cache_dir(ctx.config)
    ext = _IMAGEGEN_FMT_TO_EXT.get(output_format, ".png")
    fname = f"imagegen_{int(time.time()*1000)}{ext}"
    file_path = cache_dir / fname
    try:
        file_path.write_bytes(image_bytes)
    except OSError as exc:
        return {"error": f"写图片缓存失败: {exc}"}

    # 直接构造 MessageSegment 注入 pending_image_segments
    try:
        from nonebot.adapters.onebot.v11 import MessageSegment
    except ImportError:
        return {"error": "MessageSegment 不可用,运行环境异常"}
    segment = MessageSegment.image(file=file_path.resolve().as_uri())
    ctx.pending_image_segments.append(segment)
    _prune_imagegen_cache(ctx.config)

    mode = "edit" if input_image_bytes is not None else "generate"
    _logger.info(
        "imagegen: mode=%s model=%s size=%s quality=%s bytes=%d elapsed=%.1fs file=%s "
        "input_source=%s prompt_len=%d prompt=%r%s",
        mode, model, size, quality, len(image_bytes), elapsed, file_path.name,
        input_image_source[:80] if input_image_source else "-",
        len(prompt), prompt[:300],
        f"...(+{len(prompt)-300} chars)" if len(prompt) > 300 else "",
    )
    # 真正扣积分(主人内部判定为 ok 但不变余额)。失败这里不发生:check 阶段已 guard,
    # 这里只是把已生成图的成本落账。
    consume_result: dict[str, Any] = {}
    if affection is not None and image_cost > 0 and ctx.user_id:
        try:
            consume_result = affection.consume_points(ctx.user_id, image_cost)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("imagegen: consume_points failed (image still sent): %s", exc)
    balance_after = int(consume_result.get("balance_after", -1))
    is_owner_charge = bool(consume_result.get("is_owner"))
    return {
        "image_sent": True,
        "mode": mode,
        "model": model,
        "size": size,
        "quality": quality,
        "bytes": len(image_bytes),
        "elapsed_seconds": round(elapsed, 1),
        "input_image_used": bool(input_image_bytes),
        "cost": image_cost,
        "balance_after": balance_after,
        "is_owner_charge": is_owner_charge,
        "guidance": (
            "图已经程序自动发出去了,你只需补 1-2 句猫娘短评(『画好啦~主人看看喜不喜欢喵 ฅฅ』)。"
            "**禁止**贴 base64 / file 路径 / image_uri 到回复里;**禁止**再调一次 catty_imagegen 重复发。"
            + (
                ""
                if is_owner_charge
                else f" 本次消耗 {image_cost} 积分,该用户剩余 {balance_after} 分(余额低于 100 时可以提一句『再画就要签到啦喵~』,不用强调)。"
            )
        ),
    }


async def _exec_story_arc_set(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.story_arc_store is None or not ctx.scope_key:
        return {"error": "story_arc 不可用(store 未注入或 scope 缺失)"}
    title = str(args.get("title") or "").strip()
    context = str(args.get("context") or "").strip()
    if not title or not context:
        return {"error": "title 和 context 都必填"}
    try:
        ttl_hours = float(args.get("ttl_hours") or 3.0)
    except (TypeError, ValueError):
        ttl_hours = 3.0
    ttl_hours = min(max(ttl_hours, 0.5), 12.0)
    ttl_seconds = int(ttl_hours * 3600)
    try:
        arc = ctx.story_arc_store.add_arc(
            ctx.scope_key, title, context,
            ttl_seconds=ttl_seconds, origin="ai_tool",
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"add_arc 失败: {exc}"}
    _logger.info(
        "story_arc_set: scope=%s title=%r ttl_h=%.1f origin=ai_tool",
        ctx.scope_key, arc.title, ttl_hours,
    )
    return {
        "ok": True,
        "identifier": arc.identifier,
        "title": arc.title,
        "ttl_hours": ttl_hours,
        "guidance": (
            f"已开启 arc『{arc.title}』,接下来 {ttl_hours:.1f} 小时同 scope 回复都会带这个话题。"
            "你只需用一句话自然推进当前话题即可,不要复述 arc 内容。"
        ),
    }


async def _exec_story_arc_clear(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.story_arc_store is None or not ctx.scope_key:
        return {"error": "story_arc 不可用"}
    title = str(args.get("title") or "").strip()
    if not title:
        return {"error": "title 必填(用 '*' 清全部)"}
    try:
        if title == "*":
            removed = ctx.story_arc_store.clear_scope(ctx.scope_key)
            _logger.info("story_arc_clear: scope=%s cleared all (%d)", ctx.scope_key, removed)
            return {"ok": True, "cleared": removed, "guidance": "已清空当前 scope 所有 arc。"}
        # 按 title 精确匹配
        active = ctx.story_arc_store.get_active(ctx.scope_key)
        target = next((a for a in active if a.title == title), None)
        if target is None:
            return {"ok": True, "cleared": 0, "note": f"没找到 title='{title}' 的 active arc(可能已过期)"}
        ctx.story_arc_store.clear_arc(ctx.scope_key, target.identifier)
        _logger.info("story_arc_clear: scope=%s title=%r", ctx.scope_key, title)
        return {"ok": True, "cleared": 1, "title": title}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"clear 失败: {exc}"}


async def _exec_game_recall(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    game = str(args.get("game") or "").strip()
    if not game:
        return {"error": "game 不能为空"}
    keywords = str(args.get("keywords") or "").strip()
    limit_raw = args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 8
    except (TypeError, ValueError):
        limit = 8
    return ctx.memory_store.recall_game(game, keywords=keywords, limit=limit)


async def _exec_game_remember(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    game = str(args.get("game") or "").strip()
    fact = str(args.get("fact") or "").strip()
    if not game:
        return {"error": "game 不能为空"}
    if not fact:
        return {"error": "fact 不能为空"}
    tags_raw = str(args.get("tags") or "").strip()
    tags = [t.strip() for t in re.split(r"[,，;；]+", tags_raw) if t.strip()] if tags_raw else None
    return ctx.memory_store.record_game_fact(
        game,
        text=fact,
        source="ai_remember",
        group_id=ctx.group_id,
        user_id=ctx.user_id,
        tags=tags,
    )


async def _exec_group_game_tag(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.event is None or not isinstance(ctx.event, GroupMessageEvent):
        return {"error": "catty_group_game_tag 只能在群聊里调,私聊调用无效"}
    group_id = ctx.group_id
    if not group_id:
        return {"error": "拿不到当前群 ID"}
    game = str(args.get("game") or "").strip()
    if not game:
        return {"error": "game 是必填参数"}
    if bool(args.get("remove")):
        removed = ctx.memory_store.remove_group_game_tag(group_id, game)
        return {
            "ok": removed,
            "removed": removed,
            "group_id": group_id,
            "game": game,
            "note": "已移除标签" if removed else "本群没有这个游戏标签,无需移除",
        }
    confidence = int(args.get("confidence") or 80)
    reason = str(args.get("reason") or "")
    return ctx.memory_store.tag_group_with_game(
        group_id,
        game,
        confidence=confidence,
        reason=reason,
    )


async def _exec_remember(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    scope = str(args.get("scope") or "").strip().lower()
    text = str(args.get("text") or "").strip()
    if not scope or not text:
        return {"error": "scope 和 text 都不能为空"}
    ttl_raw = args.get("ttl_days")
    try:
        ttl_days = int(ttl_raw) if ttl_raw is not None else None
    except (TypeError, ValueError):
        ttl_days = None
    tags_raw = str(args.get("tags") or "").strip()
    tags = [t.strip() for t in re.split(r"[,，;；]+", tags_raw) if t.strip()] if tags_raw else None
    event_date = str(args.get("event_date") or "").strip()
    # 自动用当前事件填 user_id / group_id
    user_id = ctx.user_id if scope == "user" else ""
    group_id = ctx.group_id if scope == "group" else ""
    if scope == "group" and not group_id:
        return {"error": "scope=group 但当前不是群聊"}
    if scope == "user" and not user_id:
        return {"error": "拿不到当前发言用户 ID"}
    return ctx.memory_store.record_note(
        scope=scope,
        text=text,
        user_id=user_id,
        group_id=group_id,
        ttl_days=ttl_days,
        tags=tags,
        event_date=event_date,
    )


async def _exec_recall_notes(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    scope = str(args.get("scope") or "").strip().lower()
    if scope not in ("user", "group", "both"):
        return {"error": "scope 必须是 user/group/both"}
    limit_raw = args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 10
    except (TypeError, ValueError):
        limit = 10
    target_user_id = str(args.get("user_id") or "").strip() or ctx.user_id
    target_group_id = str(args.get("group_id") or "").strip() or ctx.group_id

    call_user = target_user_id if scope in ("user", "both") else ""
    call_group = target_group_id if scope in ("group", "both") else ""
    if scope in ("user", "both") and not call_user:
        return {"error": "scope 需要 user 笔记但没拿到 user_id"}
    if scope in ("group", "both") and not call_group:
        return {"error": "scope 需要 group 笔记但当前不是群聊或没传 group_id"}
    return ctx.memory_store.recall_notes(
        user_id=call_user, group_id=call_group, limit=limit,
    )


async def _exec_meme_explain(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    term = str(args.get("term") or "").strip()
    if not term:
        return {"error": "term 不能为空"}

    is_exempt = bool(ctx.configured_title.strip())
    if not is_exempt and _MEME_EXPLAIN_COOLDOWN_SECONDS > 0:
        cd_key = ctx.user_id or "anonymous"
        now = time.monotonic()
        last = _meme_explain_cooldowns.get(cd_key, 0.0)
        remaining = max(last + _MEME_EXPLAIN_COOLDOWN_SECONDS - now, 0.0)
        if remaining > 0:
            return {"error": f"meme_explain 冷却剩 {int(remaining)}s,基于已有知识回答即可"}
        _meme_explain_cooldowns[cd_key] = now

    return await lookup_term(ctx.config, term)


async def _exec_now(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    del ctx  # 纯本地计算,不需要事件上下文
    delta_raw = args.get("delta_days")
    try:
        delta = int(delta_raw) if delta_raw is not None else 0
    except (TypeError, ValueError):
        delta = 0
    return compute_now(delta_days=delta)


async def _exec_hot_trends(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_sources = args.get("sources")
    limit_raw = args.get("limit_per_source")
    try:
        limit_per_source = int(limit_raw) if limit_raw is not None else 6
    except (TypeError, ValueError):
        limit_per_source = 6

    # cooldown — 主人/特别关心豁免
    is_exempt = bool(ctx.configured_title.strip())
    if not is_exempt and _HOT_TRENDS_COOLDOWN_SECONDS > 0:
        scope_id = ctx.group_id or ctx.user_id or "anonymous"
        cd_key = f"{scope_id}:{ctx.user_id}"
        now = time.monotonic()
        last = _hot_trends_cooldowns.get(cd_key, 0.0)
        remaining = max(last + _HOT_TRENDS_COOLDOWN_SECONDS - now, 0.0)
        if remaining > 0:
            return {
                "error": (
                    f"hot_trends 冷却剩 {int(remaining)}s,先用刚刚拿到的热搜聊;"
                    "也可以直接基于常识回应,不必每次都重拉。"
                )
            }
        _hot_trends_cooldowns[cd_key] = now

    sources = normalize_sources(raw_sources) if raw_sources else None
    payload = await fetch_hot_trends(
        ctx.config, sources=sources, limit_per_source=limit_per_source
    )
    if payload.get("total", 0) == 0:
        # 全部源都挂了 — 让 AI 用人格自然说"今天网线不太通"
        return {
            "error": "所有热搜源都没拿到数据(可能网络/代理问题)",
            "errors_detail": payload.get("errors", []),
        }
    payload["guidance"] = (
        "挑 1-3 条最有梗/最有讨论价值的复述,加猫娘吐槽,不要贴 URL,"
        "不要照搬整段 JSON,不要复读 rank 数字。"
    )
    return payload


async def _exec_social_account(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    platform = str(args.get("platform") or "").strip().lower()
    if not platform:
        return {"error": "platform 是必填参数(小写英文,例如 steam)"}
    # 平台 → config 字段映射;以后加新平台只要在 config.py 加字段 + 这里加 key 即可。
    platform_field_map = {
        "steam": "catty_social_steam",
    }
    field_name = platform_field_map.get(platform)
    if field_name is None:
        return {
            "error": f"未识别的平台标识 '{platform}',当前只支持: " + ", ".join(sorted(platform_field_map.keys())),
        }
    url = str(getattr(ctx.config, field_name, "") or "").strip()
    if not url:
        return {
            "platform": platform,
            "url": "",
            "note": "猫猫在这个平台没账号(或者还没设置)",
        }
    return {"platform": platform, "url": url}


# Executor 注册表:name → async callable
ToolExecutor = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

_EXECUTORS: dict[str, ToolExecutor] = {
    "catty_recall": _exec_recall,
    "catty_user_profile": _exec_user_profile,
    "catty_mc_status": _exec_mc_status,
    "catty_web_search": _exec_web_search,
    "catty_nsfw_search": _exec_nsfw_search,
    "catty_image_search": _exec_image_search,
    "catty_meme_query": _exec_meme_query,
    "catty_game_recall": _exec_game_recall,
    "catty_game_remember": _exec_game_remember,
    "catty_social_account": _exec_social_account,
    "catty_group_game_tag": _exec_group_game_tag,
    "catty_hot_trends": _exec_hot_trends,
    "catty_now": _exec_now,
    "catty_meme_explain": _exec_meme_explain,
    "catty_remember": _exec_remember,
    "catty_recall_notes": _exec_recall_notes,
    "catty_imagegen": _exec_imagegen,
    "catty_nai_director": _exec_nai_director,
    "catty_story_arc_set": _exec_story_arc_set,
    "catty_story_arc_clear": _exec_story_arc_clear,
}


# ── 对外 API ───────────────────────────────────────────────────────────

# 主人 2026-05-28 C3: tool schemas singleton cache. 同一 (enabled, is_private, excluded_set)
# 输入永远返回同一个 list 对象 + 内容字节一致, 让 Anthropic cache key 跨调用稳定.
# 5 分钟 TTL 不必要 (tool schemas 在 catty 生命期内基本不变).
_TOOL_SCHEMAS_CACHE: dict[tuple, list[dict[str, Any]]] = {}


def available_tool_schemas(config: Config, *, is_private: bool) -> list[dict[str, Any]]:
    """按场景挑出本次主回复应该挂的 tool schemas (module-level singleton cache).

    主人选择的是'始终挂载',所以默认返回全部三个;但允许通过 config 在私聊里
    剔除特定 tool(默认私聊不挂 catty_user_profile,私聊只有一个人没必要查别人画像)。

    主人 2026-05-28: 同一 (enabled, is_private, excluded_set) cache 同一 list 对象,
    避免每轮重新构造让 tools schemas 字节飘移破 cache. ALL_TOOL_SCHEMAS 本身是 module-level
    常量, 这里只 cache 过滤后的 list 复用.
    """
    enabled = bool(getattr(config, "catty_tools_enabled", True))
    if not enabled:
        return []
    excluded_list: list[str] = []
    if is_private:
        for name in getattr(config, "catty_tools_disabled_in_private", []) or []:
            excluded_list.append(str(name).strip())
    excluded_key = tuple(sorted(set(excluded_list)))
    cache_key = (enabled, is_private, excluded_key)
    cached = _TOOL_SCHEMAS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    excluded_set = set(excluded_key)
    result = [schema for name, schema in ALL_TOOL_SCHEMAS.items() if name not in excluded_set]
    _TOOL_SCHEMAS_CACHE[cache_key] = result
    return result


# IDE 风「最近 tool 调用日志」:scope -> deque[(tool_name, args_preview, ts, succeeded)]
# 主回复 build_context 时注入,让 AI 看到自己 N 分钟内已对当前会话调过哪些 tool,
# 避免重复调浪费时间。TTL 600s,每 scope 最多 8 条(LRU)。
from collections import deque as _deque
_RECENT_TOOL_CALLS: dict[str, _deque] = {}
_RECENT_TOOL_CALLS_MAX = 8
_RECENT_TOOL_CALLS_TTL = 600.0


def _record_tool_call(scope_key: str, name: str, args_preview: str, succeeded: bool) -> None:
    if not scope_key:
        return
    dq = _RECENT_TOOL_CALLS.get(scope_key)
    if dq is None:
        dq = _deque(maxlen=_RECENT_TOOL_CALLS_MAX)
        _RECENT_TOOL_CALLS[scope_key] = dq
    dq.append((name, args_preview[:60], time.monotonic(), succeeded))


def recent_tool_calls_context(scope_key: str) -> str:
    """Build a short system-prompt line listing recent tool calls in this scope.

    给主 AI 看『5 分钟内本群/本会话已调过哪些 tool』,避免重复浪费一次工具调用。
    无记录则返回空字符串。
    """
    dq = _RECENT_TOOL_CALLS.get(scope_key)
    if not dq:
        return ""
    now = time.monotonic()
    recent: list[str] = []
    for name, args_preview, ts, succeeded in list(dq):
        age = now - ts
        if age > _RECENT_TOOL_CALLS_TTL:
            continue
        flag = "✓" if succeeded else "✗"
        ago = f"{int(age)}s" if age < 60 else f"{int(age/60)}min"
        if args_preview:
            recent.append(f"{flag}{name}({args_preview})·{ago}前")
        else:
            recent.append(f"{flag}{name}·{ago}前")
    if not recent:
        return ""
    return (
        "本会话最近的工具调用(避免重复同 tool+同参数,除非用户明确要求重做): "
        + "; ".join(recent[-6:])
    )


async def execute_tool_call(
    name: str,
    arguments_json: str,
    ctx: ToolContext,
) -> dict[str, Any]:
    """执行一次 tool_call。args 解析失败/未知 tool/执行抛错都返回结构化 error,
    让主 AI 在下一轮自己看懂出错原因(而不是把异常丢给用户)。
    """
    executor = _EXECUTORS.get(name)
    if executor is None:
        _record_tool_call(_ctx_scope_key(ctx), name, "", False)
        return {"error": f"未知 tool: {name}"}
    raw = (arguments_json or "").strip()
    if not raw:
        args: dict[str, Any] = {}
    else:
        # 走 lenient_json_object 让 fence / 智能引号 / 尾逗号 / 单引号都能恢复。
        parsed = lenient_json_object(raw)
        if parsed is None:
            _record_tool_call(_ctx_scope_key(ctx), name, raw[:60], False)
            return {"error": "arguments 不是合法 JSON 对象,无法解析"}
        args = parsed
    args_preview = _short_args_preview(args)
    try:
        result = await executor(args, ctx)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Tool %s execution failed: %s", name, exc, exc_info=True)
        _record_tool_call(_ctx_scope_key(ctx), name, args_preview, False)
        return {"error": f"{name} 执行失败: {exc.__class__.__name__}: {exc}"}
    ok = isinstance(result, dict) and not result.get("error")
    _record_tool_call(_ctx_scope_key(ctx), name, args_preview, bool(ok))
    return result


def _ctx_scope_key(ctx: ToolContext) -> str:
    if ctx.event is None:
        return ""
    if isinstance(ctx.event, GroupMessageEvent):
        return f"group:{ctx.event.group_id}"
    if isinstance(ctx.event, PrivateMessageEvent):
        return f"private:{ctx.event.user_id}"
    return ""


def _short_args_preview(args: dict[str, Any]) -> str:
    """挑 1-2 个最具区分度的 arg 字段,拼成短预览(避免长 base64 / 长 prompt 塞日志)。"""
    if not args:
        return ""
    priority = ("prompt", "keywords", "query", "fact", "name", "title", "user_id", "platform", "game", "topic")
    parts: list[str] = []
    for k in priority:
        if k in args and args[k]:
            v = str(args[k])[:30]
            parts.append(f"{k}={v}")
            if len(parts) >= 2:
                break
    if not parts:
        # fallback: 取前两个字段
        for k, v in list(args.items())[:2]:
            parts.append(f"{k}={str(v)[:20]}")
    return ", ".join(parts)


def tools_system_hint() -> str:
    """常驻 system 提示:告诉主 AI 工具的存在和调用边界。

    紧凑版 (~1500c, 旧版 ~3800c) — 每个 tool 1 行核心 trigger, 详细参数走 schema description.
    """
    return (
        "你有 16 个本地工具,**真需要时才调**(每次调用 = 回复变慢):\n"
        "1. catty_recall — '上次/记得/之前'类时间指代且 context 无答案时查长期记忆/语料。\n"
        "2. catty_user_profile — 不确定的非当前发言者 QQ 才查;当前发言者画像已在 context。\n"
        "3. catty_mc_status — 用户问 MC 在线人数/可达性时调。\n"
        "4. catty_web_search — 最新新闻/版本/价格/事实/'搜一下'时调; 60s cd (主人豁免); 已知/闲聊不调。\n"
        "5. catty_nsfw_search — pixiv/iwara, **仅好友私聊**; 群里调返 error → 引导加私聊; 图程序自发, 你补 1-2 句短评不贴 URL。\n"
        "6. catty_meme_query — Bing 拉梗图嵌入回复; 撒娇/情绪走本地表情库 (<<<CATTY_EMOJI_QUERY:意图>>>) 更快; 拿 image_uri 用 <<<CATTY_INLINE_IMAGE:URI>>> 嵌入。\n"
        "7. catty_game_recall — 查游戏专属事实库 (strinova/star_resonance/minecraft/genshin); 跨群共享。\n"
        "8. catty_game_remember — 群友给出具体名词/数字/版本/共识时记; 游戏群 web_search 自动 sink top3, 看到 `auto_sinked_to_game_memory` 别重复记。\n"
        "9. catty_social_account — 查**笨猫本人**在指定平台账号 (不是主人); 群友问起或聊到对应平台游戏时调。\n"
        "10. catty_group_game_tag — 群是某游戏的群 (长期/明确) confidence>=60 才打标签; 私聊返 error; 错了 remove=true 撤销。\n"
        "11. catty_hot_trends — 中文热搜热梗 (微博/B站/知乎/抖音); '最近网上有啥/不认识的网络新词'调; 挑 1-3 条复述加猫娘吐槽; 90s cd (主人豁免)。\n"
        "12. catty_now — 日期/时间/星期/季节/节日; 用户问『几号/几点/是不是 XX 节』或想用时段(深夜/饭点/节日)做反应时调; 明天=1/后天=2/昨天=-1。\n"
        "13. catty_meme_explain — 萌娘百科查网络梗/ACG/角色/作品; not_found 别重试, 新闻/工业词改调 web_search; 拿 extract 短句复述不贴 URL。\n"
        "14. catty_remember — 写用户/群笔记 (偏好/边界 ttl=90-180, 约定带 event_date 自动倒计时, 群特征); 闲聊吐槽/单次玩笑不要记。\n"
        "15. catty_recall_notes — 查别人笔记 (build_context 已自动注入当前发言者笔记, 别重复查); 想看非发言者 QQ 或本群整体笔记时调。\n"
        "16. catty_imagegen — **【铁律: 用户画图请求必须走这个 tool, 严禁用原生 image generation】** 原生会丢具体文字/列表/细节。"
        "prompt 改写允许精简/重组/重排, 400-700 字; 不能丢: 引号里文字、列表项数、配色/材质/光影/构图/数字。"
        "触发: 用户明确画/生成 + 主语; 不要聊到就主动生图。NSFW/敏感词拒。图自动发, image_sent=true 后只补 1-2 句短评。180s cd (主人豁免); quality 默认 low。\n"
        "通用: 能并发但总开销=延迟, 能不调就不调; 拿结果别复读 JSON, 别出现 tool_call/function_call 标记 (INLINE_IMAGE 除外); error 用猫娘口吻说 '查不到/想不起来' 不贴 error 文本。"
    )
