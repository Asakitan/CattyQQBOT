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
from typing import Any, Awaitable, Callable, Collection, Literal

import httpx
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .affection import (
    AffectionStore,
    image_cost_for_nai,
    image_cost_for_quality,
    predict_checkin_range,
)
from .catty_nsfw_imagegen import _curl_post_json as _nai_curl_post_json
from .config import Config
from .emoji_store import EmojiStore
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


_EMOJI_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "catty_emoji",
        "description": (
            "从本地 QQ 表情库搜索并发送一张表情图。适用场景:想用表情包表达害羞/得意/贴贴/炸毛/困惑/笑/撒娇/嘲讽等情绪;"
            "用户说'发个表情/来个表情包/斗图/给我挑只小猫';或者你觉得这轮文字后配一张本地表情更自然。"
            "本 tool 会把选中的本地图片加入待发送队列,最终回复里不用写图片路径,只要正常补一句短评即可。"
            "不要用于搜索具体网图/角色图/梗图主题(那走 catty_meme_query),这里只查本地表情库。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "表情意图关键词,如:害羞贴贴/得意被夸/脸红炸毛/绷不住笑/委屈小猫/疑惑歪头。",
                },
                "tags": {
                    "type": "string",
                    "description": "可选,逗号分隔补充标签,如:猫猫,害羞,贴贴。",
                },
            },
            "required": ["query"],
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
            "成功后插件会把下载好的图片加入待发送队列,最终回复只补一句短评,"
            "不要输出图片 URI/链接/INLINE_IMAGE 标记。"
            "整个 tool 内部限 25s,失败/超时会返回 error;这时用文字回复即可,"
            "可以给 1-2 个备用关键词让群友自己搜。"
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
            "用户**直接指向猫猫(@/引用回复/直呼猫猫/笨猫)+ 明确说『画一张/画个/生成/做张/给我画/帮我画 + 主语』**时调这个 tool。\n"
            "**args 留空 {} 即可**——provider 选择 / NAI 标签 prompt / GPT 描述句 / 风格判定 / 参考图 / 报价 / 短评配文 "
            "全部由后端代理 LLM 自动决定, 图会自动发到群里。**tool 调完本轮直接结束, 你不要再写任何文字回复**(短路本轮 chat)。\n"
            "**禁触发**: 用户没 @ 猫猫只是闲聊提到画画 / 用户没明确要图只是讨论某物 / 表情梗图够用的场景(走 catty_meme_query)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
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
            "── 价目表(公式: 5 + Anlas × 3 积分) ──\n"
            "  · lineart / sketch / colorize / emotion / declutter / transform: Opus 免 Anlas, "
            "**5 积分/张**(基础费)\n"
            "  · bg-removal: Anlas = (generate_anlas × 3 + 5), 832×1216 大约 (17×3+5)=56 Anlas, "
            "→ **5 + 56×3 = ~170 积分** ⚠ 贵! 用户没明确要求抠图不要主动调。\n"
            "\n"
            "── 必须提前报价 ──\n"
            "调 tool 之前先告诉用户『这次加工要扣 X 积分喵～』(bg-removal 要明确警告贵), tool 返回的 cost 字段拿到后再确认一次。\n"
            "\n"
            "── 自动 ──\n"
            "tool 自动选输入图(当前消息附图优先,fallback 群最近图)、上传、解 zip、发图。"
            "你拿到 image_sent=true 后只补 1-2 句猫娘短评 + 报价。"
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
    "catty_emoji": _EMOJI_SCHEMA,
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
    "catty_recall_user_messages": {  # P5.6: 跟 lazy schema 字节一致 (本来就该是 lazy)
        "type": "function",
        "function": {
            "name": "catty_recall_user_messages",
            "description": "拉某群友最近 N 条消息 (群聊接梗用, 默认 history per-user 时必备)",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "目标 QQ 号"},
                    "count": {"type": "integer", "description": "要拉的条数 (1-20, 默认 8)", "minimum": 1, "maximum": 20},
                },
                "required": ["user_id"],
            },
        },
    },
}


# ── P5.5 Lazy Tool Schema (OpenAI native 格式, AI 决定调时只看 name + 短描述) ──
# 主人 2026-05-28 plan-cattyCacheFixAndPromptSlim P5.5:
# - description ≤30 字, 极简告知 AI "这是干啥的"
# - parameters 保留 required schema (AI 仍能填正确 args), 但 properties description 砍到 5 字
# - 配合 catty_tools_lazy_schema_enabled flag (默认 True) 切换全量 / lazy
# - Anthropic native 格式由 convert_openai_tool_to_anthropic 自动转 (anthropic_native_client.py)
#   两种 API 输出字节稳定 = cache key 跨调用稳定
def _make_lazy_schema(name: str, short_desc: str, props: dict[str, dict], required: list[str]) -> dict:
    """生成 lazy schema. short_desc ≤30 字, props 每个 description 砍到 ≤5 字."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": short_desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


_LAZY_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "catty_recall": _make_lazy_schema(
        "catty_recall", "拉某 scope 历史消息",
        {
            "scope": {"type": "string", "enum": ["current_group", "current_user", "specific_user"], "description": "范围"},
            "keywords": {"type": "string", "description": "关键词"},
            "user_id": {"type": "string", "description": "QQ 号"},
            "limit": {"type": "integer", "description": "条数"},
        },
        ["scope"],
    ),
    "catty_user_profile": _make_lazy_schema(
        "catty_user_profile", "查某 QQ 用户画像",
        {
            "user_id": {"type": "string", "description": "QQ 号"},
            "group_id": {"type": "string", "description": "群号"},
        },
        ["user_id"],
    ),
    "catty_mc_status": _make_lazy_schema(
        "catty_mc_status", "MC server 状态", {}, [],
    ),
    "catty_emoji": _make_lazy_schema(
        "catty_emoji", "发本地表情图",
        {
            "query": {"type": "string", "description": "意图"},
            "tags": {"type": "string", "description": "标签"},
        },
        ["query"],
    ),
    "catty_web_search": _make_lazy_schema(
        "catty_web_search", "联网搜索 (新闻/查询)",
        {
            "query": {"type": "string", "description": "查询词"},
        },
        ["query"],
    ),
    "catty_nsfw_search": _make_lazy_schema(
        "catty_nsfw_search", "pixiv/iwara R-18 搜 (私聊限定)",
        {
            "kind": {"type": "string", "enum": ["image", "video"], "description": "类型"},
            "query": {"type": "string", "description": "关键词"},
        },
        ["kind", "query"],
    ),
    "catty_image_search": _make_lazy_schema(
        "catty_image_search", "反向搜图 (问出处/作者)",
        {
            "kind": {"type": "string", "enum": ["anime", "artwork", "photo", "auto"], "description": "类型"},
            "image_url": {"type": "string", "description": "图 URL"},
            "image_index": {"type": "integer", "description": "倒序索引"},
            "engines": {"type": "string", "description": "引擎"},
        },
        ["kind"],
    ),
    "catty_meme_query": _make_lazy_schema(
        "catty_meme_query", "拉梗图 (SFW)",
        {"keywords": {"type": "string", "description": "关键词"}},
        ["keywords"],
    ),
    "catty_game_recall": _make_lazy_schema(
        "catty_game_recall", "查游戏事实库",
        {
            "game": {"type": "string", "description": "游戏名"},
            "keywords": {"type": "string", "description": "关键词"},
            "limit": {"type": "integer", "description": "条数"},
        },
        ["game"],
    ),
    "catty_game_remember": _make_lazy_schema(
        "catty_game_remember", "记游戏事实",
        {
            "game": {"type": "string", "description": "游戏名"},
            "fact": {"type": "string", "description": "事实"},
            "tags": {"type": "string", "description": "标签"},
        },
        ["game", "fact"],
    ),
    "catty_social_account": _make_lazy_schema(
        "catty_social_account", "查猫猫某平台账号",
        {"platform": {"type": "string", "description": "平台"}},
        ["platform"],
    ),
    "catty_group_game_tag": _make_lazy_schema(
        "catty_group_game_tag", "给群打游戏标签 (confidence≥60)",
        {
            "game": {"type": "string", "description": "游戏名"},
            "confidence": {"type": "integer", "description": "0-100"},
            "reason": {"type": "string", "description": "原因"},
            "remove": {"type": "boolean", "description": "移除"},
        },
        ["game"],
    ),
    "catty_hot_trends": _make_lazy_schema(
        "catty_hot_trends", "拉热搜 (微博/B站/知乎/抖音)",
        {
            "sources": {"type": "string", "description": "源"},
            "limit_per_source": {"type": "integer", "description": "条数"},
        },
        [],
    ),
    "catty_now": _make_lazy_schema(
        "catty_now", "拿当前时间/日期/节日",
        {"delta_days": {"type": "integer", "description": "偏移天"}},
        [],
    ),
    "catty_meme_explain": _make_lazy_schema(
        "catty_meme_explain", "萌娘百科查梗/词条",
        {"term": {"type": "string", "description": "词"}},
        ["term"],
    ),
    "catty_remember": _make_lazy_schema(
        "catty_remember", "写长期笔记 (偏好/约定/边界)",
        {
            "scope": {"type": "string", "enum": ["user", "group"], "description": "范围"},
            "text": {"type": "string", "description": "笔记文本"},
            "ttl_days": {"type": "integer", "description": "TTL 天"},
            "tags": {"type": "string", "description": "标签"},
            "event_date": {"type": "string", "description": "ISO 日期"},
        },
        ["scope", "text"],
    ),
    "catty_recall_notes": _make_lazy_schema(
        "catty_recall_notes", "查 sticky 笔记 (查别人/查群)",
        {
            "scope": {"type": "string", "enum": ["user", "group", "both"], "description": "范围"},
            "user_id": {"type": "string", "description": "QQ"},
            "group_id": {"type": "string", "description": "群"},
            "limit": {"type": "integer", "description": "条数"},
        },
        ["scope"],
    ),
    "catty_imagegen": _IMAGEGEN_SCHEMA,  # 已是 lazy 模式 (args 空, fca36bb)
    "catty_nai_director": _make_lazy_schema(
        "catty_nai_director", "NAI 改图 (抠图/线稿/上色等)",
        {
            "req_type": {"type": "string", "enum": list(_NAI_DIRECTOR_REQ_TYPES), "description": "类型"},
            "prompt": {"type": "string", "description": "提示词"},
            "defry": {"type": "integer", "description": "强度"},
        },
        ["req_type"],
    ),
    "catty_story_arc_set": _make_lazy_schema(
        "catty_story_arc_set", "开 story arc (跨多轮故事线)",
        {
            "title": {"type": "string", "description": "标题"},
            "context": {"type": "string", "description": "上下文"},
            "ttl_hours": {"type": "number", "description": "TTL 小时"},
        },
        ["title", "context"],
    ),
    "catty_story_arc_clear": _make_lazy_schema(
        "catty_story_arc_clear", "结束 story arc",
        {"title": {"type": "string", "description": "标题"}},
        ["title"],
    ),
    "catty_recall_user_messages": _make_lazy_schema(
        "catty_recall_user_messages", "拉某群友最近 N 条消息 (群聊接梗用)",
        {
            "user_id": {"type": "string", "description": "QQ"},
            "count": {"type": "integer", "description": "条数 1-20"},
        },
        ["user_id"],
    ),
}


# ── Tool capability metadata ──────────────────────────────────────────

ToolScope = Literal["private", "group", "both"]
ToolExecutionMode = Literal["read", "write", "external"]


@dataclass(frozen=True, slots=True)
class ToolCapability:
    """Tool 的轻量可用性与调度元数据；不改动 OpenAI schema 本体。"""

    scope: ToolScope = "both"
    requires_image: bool = False
    accepts_explicit_image_url: bool = False
    requires_direct_request: bool = False
    persona_feature: str | None = None
    execution_mode: ToolExecutionMode = "read"


_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "catty_recall": ToolCapability(),
    "catty_user_profile": ToolCapability(),
    "catty_mc_status": ToolCapability(execution_mode="external"),
    "catty_emoji": ToolCapability(execution_mode="write"),
    "catty_web_search": ToolCapability(execution_mode="external"),
    "catty_nsfw_search": ToolCapability(
        scope="private",
        persona_feature="nsfw_spark",
        execution_mode="external",
    ),
    "catty_image_search": ToolCapability(
        requires_image=True,
        accepts_explicit_image_url=True,
        execution_mode="external",
    ),
    "catty_meme_query": ToolCapability(execution_mode="external"),
    "catty_game_recall": ToolCapability(),
    "catty_game_remember": ToolCapability(execution_mode="write"),
    "catty_social_account": ToolCapability(),
    "catty_group_game_tag": ToolCapability(scope="group", execution_mode="write"),
    "catty_hot_trends": ToolCapability(execution_mode="external"),
    "catty_now": ToolCapability(),
    "catty_meme_explain": ToolCapability(execution_mode="external"),
    "catty_remember": ToolCapability(execution_mode="write"),
    "catty_recall_notes": ToolCapability(),
    "catty_imagegen": ToolCapability(
        requires_direct_request=True,
        execution_mode="external",
    ),
    "catty_nai_director": ToolCapability(
        requires_image=True,
        requires_direct_request=True,
        execution_mode="external",
    ),
    "catty_story_arc_set": ToolCapability(
        persona_feature="story_arc",
        execution_mode="write",
    ),
    "catty_story_arc_clear": ToolCapability(
        persona_feature="story_arc",
        execution_mode="write",
    ),
    "catty_recall_user_messages": ToolCapability(scope="group"),
}


def tool_capability(name: str) -> ToolCapability | None:
    """返回 tool 的稳定 capability 元数据；未知名称返回 None。"""
    return _TOOL_CAPABILITIES.get(name)


def tool_execution_mode(name: str) -> ToolExecutionMode | None:
    """返回 tool 的 read/write/external 调度类别；未知名称返回 None。"""
    capability = tool_capability(name)
    return capability.execution_mode if capability is not None else None


def _is_catty_persona(persona: Any) -> bool:
    return persona is None or getattr(persona, "name", None) == "catty"


def _persona_text(persona: Any, catty_text: str, non_catty_text: str) -> str:
    return catty_text if _is_catty_persona(persona) else non_catty_text


def _private_disabled_tool_names(config: Config) -> set[str]:
    return {
        str(name).strip()
        for name in (getattr(config, "catty_tools_disabled_in_private", []) or [])
        if str(name).strip()
    }


def _persona_feature_disabled(persona: Any, feature: str | None) -> bool:
    if persona is None or not feature:
        return False
    checker = getattr(persona, "feature_disabled", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(feature))
    except Exception:  # noqa: BLE001
        return False


def _tool_capability_denial_reason(
    name: str,
    *,
    is_private: bool,
    is_group: bool,
    has_image: bool,
    is_directly_requested: bool,
    has_explicit_image_url: bool = False,
    persona: Any = None,
    disabled_in_private: Collection[str] = (),
) -> str | None:
    capability = tool_capability(name)
    if capability is None:
        return None
    if capability.scope == "private" and not is_private:
        return "private_only"
    if capability.scope == "group" and not is_group:
        return "group_only"
    if is_private and name in disabled_in_private:
        return "disabled_in_private"
    if capability.requires_image and not has_image:
        if not (
            capability.accepts_explicit_image_url and has_explicit_image_url
        ):
            return "requires_image"
    if capability.requires_direct_request and not is_directly_requested:
        return "requires_direct_request"
    if _persona_feature_disabled(persona, capability.persona_feature):
        return "persona_feature_disabled"
    return None


_TOOL_CAPABILITY_DENIAL_MESSAGES: dict[str, str] = {
    "not_in_allowlist": "本轮未获准使用该工具。",
    "private_only": "该工具仅限私聊使用。",
    "group_only": "该工具仅限群聊使用。",
    "disabled_in_private": "该工具已被私聊配置禁用。",
    "requires_image": "该工具需要当前或最近上下文中的图片。",
    "requires_direct_request": "该工具只在用户直接请求猫猫时可用。",
    "persona_feature_disabled": "当前人格已关闭该工具对应功能。",
}


def _tool_capability_error(
    name: str,
    reason: str,
    *,
    persona: Any = None,
) -> dict[str, str]:
    message = _TOOL_CAPABILITY_DENIAL_MESSAGES.get(reason, "该工具当前不可用。")
    if reason == "requires_direct_request":
        message = _persona_text(
            persona,
            message,
            "该工具仅在用户明确向当前机器人提出请求时可用。",
        )
    return {
        "error": "tool_not_allowed",
        "tool": name,
        "reason": reason,
        "message": message,
    }


# ── Tool 上下文 / 注入 ─────────────────────────────────────────────────

@dataclass(slots=True)
class ToolContext:
    config: Config
    memory_store: MemoryStore
    event: MessageEvent | None
    emoji_store: EmojiStore | None = None
    # 多人格 (主人 2026-07-06): 当前 scope 的 Persona, __init__.py 装配时注入。
    # None = catty 老路径 (画图外观锁/参考图/planner 简介全走笨猫默认)。
    persona: Any = None
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
    # 主人 2026-05-28: 当前用户原话(event.get_plaintext() 结果)。catty_imagegen 走
    # deepseek 代理模式时, 让 deepseek 自己读原话出 NAI/gpt prompt + provider 选择 + 短评,
    # 主 sonnet 那轮直接短路, 省 follow-up 100K+ 重传 + 杜绝 tool_use 配对 500。
    user_text: str = ""

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
        "note": _persona_text(
            ctx.persona,
            "结果有 30s 本地缓存。reachable=False 通常表示服务器没开或不在白名单网段;"
            "不是猫猫的锅,你可以直接告诉用户'服务器目前掉了/猫猫连不上'。",
            "结果有 30s 本地缓存。reachable=False 通常表示服务器没开或不在白名单网段;"
            "可直接告知用户服务器当前不可达。",
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


async def _exec_emoji(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query 不能为空"}
    if ctx.emoji_store is None or not bool(getattr(ctx.config, "catty_emoji_enabled", True)):
        return {"error": "本地表情库未启用"}
    raw_tags = str(args.get("tags") or "").strip()
    tags = [part.strip() for part in re.split(r"[,，、;；|\s]+", raw_tags) if part.strip()]
    limit = max(int(getattr(ctx.config, "catty_emoji_max_candidates", 8) or 8), 1)
    entries = ctx.emoji_store.select(query, tags=tags, limit=limit)
    if not entries:
        ctx.emoji_store.refresh()
        entries = ctx.emoji_store.select(query, tags=tags, limit=limit)
    if not entries:
        return {"error": "本地表情库没有匹配项", "query": query, "tags": tags}

    entry = entries[0]
    try:
        from nonebot.adapters.onebot.v11 import MessageSegment
    except Exception:  # noqa: BLE001
        return {"error": "MessageSegment 不可用,运行环境异常"}
    segment = MessageSegment.image(file=entry.path.resolve().as_uri())
    ctx.pending_image_segments.append(segment)
    return {
        "ok": True,
        "query": query,
        "selected": {
            "meaning": entry.meaning,
            "tags": entry.tags[:8],
            "source": entry.source,
            "priority": entry.priority,
            "filename": entry.path.name,
        },
        "candidates": [
            {
                "meaning": item.meaning,
                "tags": item.tags[:6],
                "filename": item.path.name,
            }
            for item in entries[:5]
        ],
        "note": "表情图已加入待发送队列,最终回复只要补一句短评即可;不要贴文件名或路径。",
    }


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
            "error": _persona_text(
                ctx.persona,
                "群里禁止 NSFW 搜索;请用猫娘人格让用户加好友私聊再来。",
                "群里禁止 NSFW 搜索;请引导用户在私聊中继续。",
            ),
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
        "guidance": _persona_text(
            ctx.persona,
            "image 命中且 images_already_sent>0:程序已经把图发了,你只补 1-2 句猫娘短评,不要贴链接;"
            "image 但 images_already_sent=0:下载全失败,挑 1-2 个 results URL 给主人,简短;"
            "video:挑 1-3 个 iwara 链接抛出去配短评。禁止编造 URL、禁止安全免责模板。",
            "image 命中且 images_already_sent>0:程序已经把图发了，只补 1-2 句符合当前人格的短评，不要贴链接;"
            "image 但 images_already_sent=0:下载全失败，挑 1-2 个 results URL 给用户，简短;"
            "video:挑 1-3 个 iwara 链接配短评。禁止编造 URL。",
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
            "guidance": _persona_text(
                ctx.persona,
                "用猫娘人格让用户重新发一张图或贴图片直链。",
                "让用户重新发送一张图片或提供图片直链。",
            ),
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
                "error": _persona_text(
                    ctx.persona,
                    f"搜图冷却剩 {int(remaining)}s,稍后再戳人家喵",
                    f"搜图冷却剩 {int(remaining)}s，请稍后再试。",
                ),
                "guidance": _persona_text(
                    ctx.persona,
                    "用猫娘人格说稍等几秒再搜,不要重复调本 tool。",
                    "请告知用户稍等几秒再搜索，不要重复调用本 tool。",
                ),
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
        payload["yandex_blocked_hint"] = _persona_text(
            ctx.persona,
            "Yandex(ya.ru 备用域名也)拿到 stub 页,这是真人/X 搜图主力引擎不可用。"
            "用猫娘人格告诉主人:Yandex 这次没回数据,真人/X 来源搜不到;"
            "可以让主人去 config.json 的 ai.http_proxy 配个代理重试,或换张更清晰的图。"
            "**不要装作搜全了**,SauceNAO/ascii2d 的结果只覆盖二次元。",
            "Yandex（含 ya.ru 备用域名）拿到 stub 页，真人/X 搜图的主力引擎当前不可用。"
            "请如实说明本次没有拿到真人/X 来源；可建议用户在 config.json 的 ai.http_proxy 配置代理后重试，"
            "或换一张更清晰的图片。**不要装作已经搜全**，SauceNAO/ascii2d 的结果主要覆盖二次元。",
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
    if not _is_catty_persona(ctx.persona):
        if not results:
            payload["guidance"] = (
                "搜不到结果时如实告知用户，可以让对方换一张更清晰的图片或提供原图链接，"
                "**禁止编造作者、番名或链接**。"
                + (
                    " 本次 Yandex 被阻断；如果是真人图片，请按 yandex_blocked_hint 提示用户配置代理。"
                    if yandex_blocked else ""
                )
            )
        else:
            x_twitter_note = (
                f" 本轮命中 {len(x_twitter_hits)} 条 X(Twitter) 链接（见 x_twitter_urls），"
                "无论 similarity 多少，都必须优先复述这些 X 链接给用户；"
                "不要因为相似度低跳过 X 链接，只贴二次元 booru。"
                "**禁止**只贴 Konachan/Pixiv 而把 Twitter 链接埋掉。"
                if x_twitter_hits else ""
            )
            payload["guidance"] = (
                "用当前人格复述 1-3 条最关键的结果。**高优先级规则（覆盖相似度判断）**："
                "**source=saucenao 或 source=yandex 的结果必须优先复述**（这两个是主力引擎，"
                "results 数组已经按此规则排序；先看 top N 中 saucenao/yandex 命中，"
                "其它引擎 ascii2d/iqdb/trace.moe 只在 saucenao+yandex 都没有信号时再补）。"
                "在 saucenao+yandex 内部，挑选顺序为："
                "(1) X/Twitter URL（含 is_x_twitter 标记）>(2) similarity > 60>(3) 其它优选结果。"
                "**不要照搬 JSON、复读相似度小数或编造 results 以外的信息**。"
                "**绝对禁止**跳过 saucenao/yandex 结果，只贴 ascii2d/iqdb 链接；后者只是 fallback。"
                + x_twitter_note
                + (
                    " 如果结果全是 booru/Pixiv 二次元站而图片本身是真人或自拍，**必须**按 yandex_blocked_hint "
                    "提示用户配置代理（yandex_blocked=True 时）。"
                    if yandex_blocked else ""
                )
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
    image_data, _content_type, _source_url = winner
    uri = "base64://" + base64.b64encode(image_data).decode("ascii")
    try:
        from nonebot.adapters.onebot.v11 import MessageSegment
    except ImportError:
        return {"error": "MessageSegment 不可用,运行环境异常"}
    ctx.pending_image_segments.append(MessageSegment.image(uri))
    return {
        "ok": True,
        "keywords": keywords,
        "image_queued": True,
        "image_bytes": len(image_data),
        "guidance": "梗图已加入待发送队列,最终回复只补一句短评;不要贴图片 URI、链接或 INLINE_IMAGE 标记。",
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


def _resize_to_director_reference_png(data: bytes) -> str:
    """把任意图片(PNG/JPEG/WEBP)读出来 → 等比缩到 1024x1536 letterbox 黑底居中 → PNG base64。

    NAI v4.5 Precise Reference (director_reference_images) 标准格式,
    跟 novelai-sdk crop_and_resize 一致。
    """
    from PIL import Image as PILImage
    import io
    src = PILImage.open(io.BytesIO(data)).convert("RGB")
    tgt_w, tgt_h = 1024, 1536
    src_w, src_h = src.size
    src_ratio = src_w / src_h
    tgt_ratio = tgt_w / tgt_h
    if src_ratio > tgt_ratio:
        new_w = tgt_w
        new_h = int(tgt_w / src_ratio)
    else:
        new_h = tgt_h
        new_w = int(tgt_h * src_ratio)
    resized = src.resize((new_w, new_h), PILImage.LANCZOS)
    canvas = PILImage.new("RGB", (tgt_w, tgt_h), (0, 0, 0))
    canvas.paste(resized, ((tgt_w - new_w) // 2, (tgt_h - new_h) // 2))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
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
    local_reference_bytes_list: list[bytes] | None = None,
) -> dict[str, Any]:
    """NovelAI 生图执行器。被 _exec_imagegen 在 provider='nai' 时调用。

    返回结构和 _exec_imagegen 保持一致(image_sent / mode / cost / balance_after / guidance)。

    characters: 多角色数组,每项 {prompt, negative_prompt?, position?}。最多 6 个。
    references: vibe transfer/precise reference 数组,每项 {extracted?, strength?}。
                数量决定从 ctx.input_image_urls / recent_image_urls 取多少张参考图。
                最多 _NAI_VIBE_REFERENCE_MAX=4 张。
    local_reference_bytes_list: 主人 2026-05-29 — agent 模式专用旁路, 直接传 PNG/JPEG bytes
                做 Precise Reference (用于自画像锁人设 Miao/miaomiao*.png), 跳过 URL 下载/群图
                查找环节。fidelity=1.0 strength=1.0 抓最大角色细节。
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
    # 主人 2026-05-29: agent 模式 local references (Miao/miaomiao*.png) 跟 URL references
    # 走同一 NAI Precise Reference 通道, 加成数 / cost 计算合并算。
    local_refs_count = len([b for b in (local_reference_bytes_list or []) if b])
    has_refs = bool(references) or local_refs_count > 0

    # Anlas 预测(Opus 免费档归零; 带 references 走 Precise Reference 加 5 Anlas/张)
    predicted_anlas = _nai_predict_anlas(width, height, steps, n_samples=1)
    if _nai_is_opus_free(width, height, steps, 1):
        billable_anlas = 0
    else:
        billable_anlas = predicted_anlas
    if has_refs:
        # Precise Reference 每张额外 5 Anlas (NAI 官方文档) — URL refs + local refs 合并算
        total_refs = min(
            len(references or []) + local_refs_count,
            _NAI_VIBE_REFERENCE_MAX,
        )
        billable_anlas += 5 * total_refs
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
                "user_facing_hint": _persona_text(
                    ctx.persona,
                    f"用猫娘口吻提醒用户:他当前只有 {balance} 积分,这张图(NAI/{aspect_key})要 {cost} 分,"
                    f"还差 {cost - balance} 分。让他发『签到』来领今天的积分,"
                    f"他现在好感等级 Lv{level},今天签到大概能拿 {lo}-{hi} 分。"
                    "傲娇但要把要点说全:差多少、要 Lv 几、发『签到』两个字就能领。"
                    "**禁止**自己再调一次 catty_imagegen 重复发。",
                    f"提醒用户：当前只有 {balance} 积分，这张图（NAI/{aspect_key}）需要 {cost} 分，"
                    f"还差 {cost - balance} 分。可发送『签到』领取今日积分；当前好感等级为 Lv{level}，"
                    f"今天预计可获得 {lo}-{hi} 分。说明差额、等级与签到方式。"
                    "**禁止**再次调用 catty_imagegen 重复生成。",
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
                return {
                    "error": _persona_text(
                        ctx.persona,
                        f"生图冷却剩 {int(remaining)}s,稍后再戳人家喵",
                        f"生图冷却剩 {int(remaining)}s，请稍后再试。",
                    )
                }
            _imagegen_cooldowns[cd_key] = now

    # ── v4 系列必须传结构化 v4_prompt/v4_negative_prompt + SDK 全套字段 ──
    parameters: dict[str, Any] = {
        "width": width,
        "height": height,
        "scale": scale,
        "sampler": sampler,
        "steps": steps,
        "n_samples": 1,
        "seed": int(time.time()) & 0xFFFFFFFF,
        "negative_prompt": neg,
        "ucPreset": 1,
        "qualityToggle": True,
        "sm": False,
        "sm_dyn": False,
        "autoSmea": False,
        "dynamic_thresholding": False,
        "cfg_rescale": 0.0,
        "noise_schedule": noise_schedule,
        "legacy": False,
        "legacy_uc": False,
        "legacy_v3_extend": False,
        "deliberate_euler_ancestral_bug": False,
        "prefer_brownian": True,
        "strength": 0.7,
        "add_original_image": False,
        "controlnet_strength": 1.0,
        "normalize_reference_strength_multiple": False,
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

    # ── references: Precise Reference (director_reference_*, 最多 4 张) ──
    # 注意: AI 传入的 extracted/strength 在 SDK 语义里对应:
    #   extracted (0-1) → director_reference_secondary_strength_values = 1 - fidelity
    #   strength  (0-1) → director_reference_strength_values
    # 默认 fidelity=1.0 strength=1.0 抓最大角色细节
    refs = [r if isinstance(r, dict) else {} for r in (references or [])][:_NAI_VIBE_REFERENCE_MAX]
    director_ref_b64: list[str] = []
    director_ref_strength: list[float] = []
    director_ref_secondary: list[float] = []  # = 1 - fidelity

    # 主人 2026-05-29: agent 模式 local reference bytes 优先插入 (跳过 URL 下载循环)。
    # 用于自画像锁人设 (Miao/miaomiao*.png), 默认 fidelity=1.0 strength=1.0 抓最大角色细节。
    # 主人 2026-07-06 多人格: persona.imagegen 可调低 (1.0 锁死站姿没动作, 机机 0.9);
    # catty persona.imagegen=None → 恒 1.0 老行为。
    # URL refs 仍在下面追加, 总数不超 _NAI_VIBE_REFERENCE_MAX。
    _ref_pi = getattr(getattr(ctx, "persona", None), "imagegen", None)
    try:
        _local_ref_strength = max(0.0, min(1.0, float(getattr(_ref_pi, "ref_strength", 1.0)))) if _ref_pi else 1.0
        _local_ref_fidelity = max(0.0, min(1.0, float(getattr(_ref_pi, "ref_fidelity", 1.0)))) if _ref_pi else 1.0
    except (TypeError, ValueError):
        _local_ref_strength, _local_ref_fidelity = 1.0, 1.0
    for data in (local_reference_bytes_list or []):
        if not data or len(director_ref_b64) >= _NAI_VIBE_REFERENCE_MAX:
            continue
        try:
            ref_b64 = _resize_to_director_reference_png(data)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("imagegen[nai] local reference resize failed: %s", exc)
            continue
        director_ref_b64.append(ref_b64)
        director_ref_strength.append(_local_ref_strength)
        director_ref_secondary.append(round(1.0 - _local_ref_fidelity, 2))
    if director_ref_b64:
        _logger.info(
            "imagegen[nai]: %d local reference(s) attached (agent self-portrait mode)",
            len(director_ref_b64),
        )

    if refs and len(director_ref_b64) < _NAI_VIBE_REFERENCE_MAX:
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
                ref_b64 = _resize_to_director_reference_png(data)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("imagegen[nai] reference resize failed: %s", exc)
                return {"error": f"参考图 #{i+1} 转 PNG 失败: {exc}"}
            director_ref_b64.append(ref_b64)
            # AI 写的 extracted 字段在 SDK 等于 fidelity, 高 extracted = 低 secondary
            try:
                fidelity = float(r.get("extracted") if r.get("extracted") is not None else 1.0)
            except (TypeError, ValueError):
                fidelity = 1.0
            try:
                strength = float(r.get("strength") if r.get("strength") is not None else 1.0)
            except (TypeError, ValueError):
                strength = 1.0
            fidelity = max(0.0, min(1.0, fidelity))
            strength = max(0.0, min(1.0, strength))
            director_ref_strength.append(strength)
            director_ref_secondary.append(round(1.0 - fidelity, 2))

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
        parameters["use_coords"] = use_coords

    if director_ref_b64:
        n = len(director_ref_b64)
        parameters["director_reference_images"] = director_ref_b64
        parameters["director_reference_descriptions"] = [
            {"caption": {"base_caption": "character&style", "char_captions": []}, "legacy_uc": False}
            for _ in range(n)
        ]
        parameters["director_reference_strength_values"] = director_ref_strength
        parameters["director_reference_secondary_strength_values"] = director_ref_secondary
        parameters["director_reference_information_extracted"] = [1.0] * n

    payload: dict[str, Any] = {
        "input": prompt,
        "model": model,
        "action": "generate",
        "use_new_shared_trial": True,
        "parameters": parameters,
    }

    # NAI 专用 proxy 优先, fallback 全局 proxy, 再 fallback 直连
    proxy_str = (
        str(getattr(ctx.config, "catty_imagegen_nai_http_proxy", "") or "").strip()
        or str(getattr(ctx.config, "catty_http_proxy", "") or "").strip()
    )
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=15.0),
        "follow_redirects": True,
        "http2": False,
        "limits": httpx.Limits(max_keepalive_connections=0, max_connections=10),
    }
    if proxy_str:
        client_kwargs["proxy"] = proxy_str

    # 主人 2026-05-28: Python async SOCKS5 实现都有 30-50% ConnectError reset,
    # subprocess curl.exe 100% 稳. 走 curl.exe.
    started = time.monotonic()
    status, body, err = await _nai_curl_post_json(
        url=_NAI_IMAGE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        },
        payload=payload,
        proxy=proxy_str,
        timeout=timeout,
    )
    elapsed = time.monotonic() - started
    if err or status == 0:
        _logger.warning(
            "imagegen[nai] curl error after %.1fs: %s (model=%s %dx%d steps=%d)",
            elapsed, err, model, width, height, steps,
        )
        return {
            "error": f"NovelAI 接口连不上 ({elapsed:.0f}s): {err}",
            "retry_guidance": "网络或 proxy 异常;过 30s 再试,或改 provider='gpt'。",
        }
    if status != 200:
        _logger.warning(
            "imagegen[nai] status=%d elapsed=%.1fs model=%s %dx%d steps=%d prompt_len=%d body=%s",
            status, elapsed, model, width, height, steps, len(prompt), detail,
        )
        if status == 401:
            return {"error": "NovelAI token 失效或未授权,改 provider='gpt' 重试"}
        if status == 402:
            return {"error": "NovelAI 账户 Anlas 余额不足,改 provider='gpt' 重试或精简到三个标准尺寸"}
        if status == 429:
            return {"error": "NovelAI 触发速率限制,30 秒后再试或改 provider='gpt'"}
        return {"error": f"NovelAI HTTP {status}: {detail[:300]}"}

    # 响应是 zip,里面有 image_0.png
    try:
        import io
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(body))
        names = zf.namelist()
        if not names:
            return {"error": "NovelAI 响应 zip 是空的"}
        image_bytes = zf.read(names[0])
    except zipfile.BadZipFile:
        preview = body[:200]
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
        "guidance": _persona_text(
            ctx.persona,
            "图已经程序自动发出去了,你只需补 1-2 句猫娘短评(『画好啦~主人看看喜不喜欢喵 ฅฅ』)。"
            "**禁止**贴 base64 / file 路径 / image_uri 到回复里;**禁止**再调一次 catty_imagegen 重复发。"
            + (
                ""
                if is_owner_charge
                else f" **必须在最终回复里报价**:『这张图消耗了 {cost} 积分,主人现在还剩 {balance_after} 分喵～』(余额低于 100 时加一句『再画要签到啦~』,不用过分强调)。"
            ),
            "图片已经自动发出。只补 1-2 句符合当前人格的短评。"
            "**禁止**贴 base64 / file 路径 / image_uri 到回复里；**禁止**再次调用 catty_imagegen 重复生成。"
            + (
                ""
                if is_owner_charge
                else f" **必须在最终回复里报价**：『这张图消耗了 {cost} 积分，当前余额 {balance_after} 分。』"
                "余额低于 100 时可提醒签到，无需过分强调。"
            ),
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
            "error": _persona_text(
                ctx.persona,
                "用户没直接 @ 猫猫,不允许主动调 director tool。",
                "用户未直接指向当前机器人，不能主动调用 director tool。",
            ),
            "guidance": _persona_text(
                ctx.persona,
                "director 只能在用户明确指向猫猫(@ / 引用回复 / 直呼猫猫) + 明确要求加工时调。",
                "director 只能在用户明确指向当前机器人（@ / 引用回复 / 直呼）并明确要求加工时调用。",
            ),
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
                "user_facing_hint": _persona_text(
                    ctx.persona,
                    f"用猫娘口吻提醒用户:他当前只有 {balance} 积分,这次 director({req_type})要 {cost} 分,"
                    f"还差 {cost - balance} 分。让他发『签到』来领今天的积分,"
                    f"他现在好感等级 Lv{level},今天签到大概能拿 {lo}-{hi} 分。"
                    "**禁止**自己再调一次 catty_nai_director 重复发。",
                    f"提醒用户：当前只有 {balance} 积分，这次 director({req_type})需要 {cost} 分，"
                    f"还差 {cost - balance} 分。可发送『签到』领取今日积分；当前好感等级为 Lv{level}，"
                    f"今天预计可获得 {lo}-{hi} 分。"
                    "**禁止**再次调用 catty_nai_director 重复处理。",
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
                return {
                    "error": _persona_text(
                        ctx.persona,
                        f"生图冷却剩 {int(remaining)}s,稍后再戳人家喵",
                        f"生图冷却剩 {int(remaining)}s，请稍后再试。",
                    )
                }
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

    # NAI director 跟 generate 共用 proxy (走 NovelAI 同 host)
    proxy_str = (
        str(getattr(ctx.config, "catty_imagegen_nai_http_proxy", "") or "").strip()
        or str(getattr(ctx.config, "catty_http_proxy", "") or "").strip()
    )
    # subprocess curl.exe 100% 稳, 走它
    started = time.monotonic()
    status, body, err = await _nai_curl_post_json(
        url=_NAI_AUGMENT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        },
        payload=payload,
        proxy=proxy_str,
        timeout=timeout,
    )
    elapsed = time.monotonic() - started
    if err or status == 0:
        _logger.warning("director[%s] curl error after %.1fs: %s", req_type, elapsed, err)
        return {
            "error": f"NovelAI director 接口连不上 ({elapsed:.0f}s): {err}",
            "retry_guidance": "网络或 proxy 异常;过 30s 再试。",
        }
    if status != 200:
        detail = body[:400].decode("utf-8", "replace")
        _logger.warning(
            "director[%s] status=%d elapsed=%.1fs %dx%d body=%s",
            req_type, status, elapsed, in_w, in_h, detail,
        )
        if status == 401:
            return {"error": "NovelAI token 失效或未授权"}
        if status == 402:
            return {"error": "NovelAI 账户 Anlas 余额不足"}
        if status == 429:
            return {"error": "NovelAI 触发速率限制,30 秒后再试"}
        if status == 400:
            return {"error": f"director payload 不合法: {detail[:300]}"}
        return {"error": f"NovelAI director HTTP {status}: {detail[:300]}"}

    try:
        import io
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(body))
        names = zf.namelist()
        if not names:
            return {"error": "director 响应 zip 是空的"}
        out_image_bytes = zf.read(names[0])
    except zipfile.BadZipFile:
        preview = body[:200]
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
        "guidance": _persona_text(
            ctx.persona,
            f"director({req_type}) 已自动发图。你只需补 1-2 句猫娘短评。"
            "**禁止**贴 base64/file 路径到回复;**禁止**再调一次 catty_nai_director 重复发。"
            + (
                ""
                if is_owner_charge
                else f" **必须在最终回复里报价**:『director({req_type}) 消耗了 {cost} 积分,主人还剩 {balance_after} 分喵～』"
            ),
            f"director({req_type}) 已自动发图。只补 1-2 句符合当前人格的短评。"
            "**禁止**贴 base64/file 路径到回复；**禁止**再次调用 catty_nai_director 重复处理。"
            + (
                ""
                if is_owner_charge
                else f" **必须在最终回复里报价**：『director({req_type}) 消耗了 {cost} 积分，当前余额 {balance_after} 分。』"
            ),
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


# ── 主人 2026-05-29: catty_imagegen agent 模式 (deepseek 代理) ──────────
#
# 主 sonnet 端 schema 极简成 empty args (见 _IMAGEGEN_SCHEMA), 把 provider/prompt/
# 风格判定/参考图/短评 全甩给后端 deepseek (config.ai_fallback 那一套). 主 sonnet
# 那一轮 tool_call 之后通过 _short_circuit_reply sentinel 直接 return, 不发 follow-up,
# 杜绝『tool_use ids without tool_result』500 + 杜绝二次 100K+ 重传 cache miss.

# 自画像 Precise Reference 本地参考图路径 (项目根 Miao/ 下两张).
_SELF_PORTRAIT_SFW_PATH = "Miao/miaomiao.png"
_SELF_PORTRAIT_NSFW_PATH = "Miao/miaomiaonude.png"


def _load_self_portrait_reference_bytes(kind: str, persona: Any = None) -> list[bytes]:
    """Return PNG bytes of self-portrait lock-character reference(s), [] on miss/error.

    多人格: 配置了 imagegen 的非 Catty 人格用其参考图 (机机=Miao/fadianji.png +
    ref_path_extra 里的额外角度图, 无 NSFW 深水参考图时 nsfw 回落 sfw 图);
    Catty 默认两张 (miaomiao/miaomiaonude, 各自单张不叠加), 其余人格不加载参考图。
    """
    from pathlib import Path
    _is_catty = _is_catty_persona(persona)
    _pi = getattr(persona, "imagegen", None) if not _is_catty else None
    if kind not in ("sfw", "nsfw"):
        return []
    if _pi is not None:
        fnames = [(_pi.ref_nsfw_path or _pi.ref_path) if kind == "nsfw" else _pi.ref_path]
        fnames.extend(_pi.ref_path_extra)
    elif _is_catty:
        if kind == "nsfw":
            fnames = [_SELF_PORTRAIT_NSFW_PATH]
        else:
            fnames = [_SELF_PORTRAIT_SFW_PATH]
    else:
        return []
    out: list[bytes] = []
    for fname in fnames:
        if not fname:
            continue
        p = Path(fname)
        if not p.is_file():
            _logger.warning("imagegen agent: self-portrait reference missing: %s", p)
            continue
        try:
            out.append(p.read_bytes())
        except OSError as exc:
            _logger.warning("imagegen agent: self-portrait reference read failed: %s: %s", p, exc)
    return out


async def _deepseek_imagegen_plan(config: Config, user_text: str, persona: Any = None) -> dict[str, Any]:
    """Call deepseek (config.ai_fallback) once, ask it to emit a strict JSON plan.

    返回 dict 字段:
      provider: 'nai' | 'gpt'
      prompt: 实际生图 prompt 字符串 (NAI 用 danbooru 标签, GPT 用自然描述句)
      aspect: 'portrait' | 'landscape' | 'square' (NAI)
      quality: 'low' | 'medium' | 'high' | 'auto' (GPT)
      negative_prompt: str (可选, NAI)
      self_portrait: 'sfw' | 'nsfw' | null — 命中时 agent 自动加 Miao/miaomiao*.png 参考图
      short_review: 1-2 句笨猫语气短评 (画好后发到群里替代 sonnet follow-up)
    """
    from .openai_client import _post_chat_completion_raw
    base_url = str(getattr(config, "catty_ai_fallback_base_url", "") or "").strip()
    api_key = str(getattr(config, "catty_ai_fallback_api_key", "") or "").strip()
    model = str(getattr(config, "catty_ai_fallback_model", "") or "").strip()
    if not (base_url and api_key and model):
        raise RuntimeError("ai_fallback (deepseek) 三件套未配齐, 无法跑 agent 模式")
    timeout = float(
        getattr(config, "catty_ai_fallback_request_timeout", 60.0)
        or getattr(config, "catty_request_timeout", 60.0)
        or 60.0
    )
    proxy = str(getattr(config, "catty_http_proxy", "") or "")
    _is_catty = _is_catty_persona(persona)
    _pi = getattr(persona, "imagegen", None) if persona is not None else None
    if not _is_catty and _pi is not None:
        # 多人格 planner: 外观锁/参考图/短评口吻全按 persona.imagegen 来。
        _char = getattr(persona, "char_name", "机器人")
        system_prompt = (
            f"你是 QQ 群机器人『{_char}』的画图任务调度器, 负责把用户的画图请求翻译成生图参数."
            " 必须严格返回 JSON 对象, 字段如下 (不在 JSON 外输出任何文字):\n"
            '  - "provider": "nai" 或 "gpt". 二次元/动漫/萌系/角色立绘 → "nai" (默认 5 积分最划算);'
            ' 写实/产品/海报/带文字/真实摄影/UI → "gpt".\n'
            '  - "prompt": 实际生图 prompt. NAI 用英文 danbooru 标签 (逗号分隔);'
            ' GPT 用自然中英文描述句. 500 字以内. 不要含 NSFW 显性词.\n'
            f'    仅当画{_char}自己时, 外观锁 tags 必须包含: "{_pi.girl_tags}",'
            ' 并按场景加动作/姿势/表情 tags (dynamic pose 等), 别只出呆板站立立绘.\n'
            '  - "aspect": "portrait"|"landscape"|"square" — NAI 用, 默认 "portrait" (832x1216 立绘).\n'
            '  - "quality": "low"|"medium"|"high"|"auto" — GPT 用, 默认 "low".\n'
            '  - "negative_prompt": 可选, NAI 负面词. 不填留空字符串.\n'
            f'  - "self_portrait": "sfw"|null. **用户明确说画『你/{_char}/自画像/自拍』本人, 或要{_char}的'
            '腿图/脚图/袜子照等局部时, 填 "sfw"** (会自动加本地参考图锁人设, 强制 provider="nai";'
            ' 局部请求 prompt 加 lower body/foot focus/thighhighs 等构图 tags, 擦边不露骨).'
            ' 其他一切请求 (画用户/风景/别的角色/OC/梗图) 一律填 null —'
            ' null 时**不加参考图**, prompt 也**绝不掺上面的外观锁 tags**, 完全按用户需求自由构思.\n'
            f'  - "short_review": 1-2 句{_char}口吻的短评 (画好后会代替主 AI 发到群里). {_pi.short_review_style}\n'
            f"人格简介: {_pi.planner_brief}\n"
            " 不要在 short_review 里出现 OOC / 元评论 / Markdown."
            " 用户没明确说风格时默认 provider=nai. 严禁输出 JSON 以外任何内容."
        )
    elif _is_catty:
        system_prompt = (
        "你是 QQ 群猫娘机器人『笨猫』的画图任务调度器, 负责把用户的画图请求翻译成生图参数."
        " 必须严格返回 JSON 对象, 字段如下 (不在 JSON 外输出任何文字):\n"
        '  - "provider": "nai" 或 "gpt". 二次元/动漫/猫娘/萌系/角色立绘/萝莉/JK → "nai" (默认 5 积分最划算);'
        ' 写实/产品/海报/带文字/真实摄影/UI → "gpt".\n'
        '  - "prompt": 实际生图 prompt. NAI 用英文 danbooru 标签 (逗号分隔, 例 "1girl, white hair, '
        'cat ears, JK uniform, ..."); GPT 用自然中英文描述句. 500 字以内. 不要含 NSFW 显性词 (露/性/裸 等),'
        ' 通过 self_portrait=nsfw 走参考图锁人设即可.\n'
        '  - "aspect": "portrait"|"landscape"|"square" — NAI 用, 默认 "portrait" (832x1216 立绘).\n'
        '  - "quality": "low"|"medium"|"high"|"auto" — GPT 用, 默认 "low".\n'
        '  - "negative_prompt": 可选, NAI 负面词. 不填留空字符串.\n'
        '  - "self_portrait": "sfw"|"nsfw"|null. **用户明确说画『你/猫猫/笨猫/自画像/自拍』时**:\n'
        '      SFW 场景填 "sfw" (会自动加 Miao/miaomiao.png 作参考图锁人设);\n'
        '      NSFW/色情/露骨场景填 "nsfw" (会自动加 Miao/miaomiaonude.png);\n'
        '      其他 (画用户/画风景/画 OC) 填 null. self_portrait 命中时强制 provider="nai".\n'
        '  - "short_review": 1-2 句笨猫语气的短评 (画好后会代替主 AI 发到群里), 必带『喵/喵呜/嗷呜/ฅฅ』之类的语气词,'
        ' 软萌+傲娇+小动作. 示例:\n'
        '      『画好啦~主人看看喜不喜欢嗷呜ฅฅ』\n'
        '      『哼哼, 这就是人家的样子喵~ (偷瞄主人)』\n'
        '      『嗷呜~ 才不是特地为主人画的呢! ฅฅ』\n'
        '笨猫人格简介: 白毛猫耳少女 155cm, JK 制服, 傲娇撒娇, 嘴硬心软, 叫男性『杂鱼』,'
        ' 自称『人家/猫猫/笨猫』, 不要在 short_review 里出现 OOC / 元评论 / Markdown.\n'
        "用户没明确说风格时默认 provider=nai (二次元最划算). 严禁输出 JSON 以外任何内容."
    )
    else:
        system_prompt = (
            "你是 QQ 群机器人的画图任务调度器, 负责把用户的画图请求翻译成生图参数."
            " 必须严格返回 JSON 对象, 字段如下 (不在 JSON 外输出任何文字):\n"
            '  - "provider": "nai" 或 "gpt". 二次元/动漫/萌系/角色立绘 → "nai" (默认 5 积分最划算);'
            ' 写实/产品/海报/带文字/真实摄影/UI → "gpt".\n'
            '  - "prompt": 实际生图 prompt. NAI 用英文 danbooru 标签 (逗号分隔);'
            ' GPT 用自然中英文描述句. 500 字以内. 不要含 NSFW 显性词.\n'
            '  - "aspect": "portrait"|"landscape"|"square" — NAI 用, 默认 "portrait" (832x1216 立绘).\n'
            '  - "quality": "low"|"medium"|"high"|"auto" — GPT 用, 默认 "low".\n'
            '  - "negative_prompt": 可选, NAI 负面词. 不填留空字符串.\n'
            '  - "self_portrait": null. 始终填写 null, 不使用本地人物参考图.\n'
            '  - "short_review": 1-2 句自然简短的配文 (画好后会发到群里).\n'
            "不要在 short_review 里出现 OOC / 元评论 / Markdown. 用户没明确说风格时默认 provider=nai. "
            "严禁输出 JSON 以外任何内容."
        )
    # 主人 2026-07-06 修「自画像没带参考图」: DeepSeek thinking 模型 + json mode 下
    # reasoning 吃掉 max_tokens=800 → content 空 → plan 失败走 fallback (fallback 不带
    # self_portrait → 参考图丢失). 双修: max_tokens 提到 1600 + 空/非 JSON 时裸跑重试一次
    # (无 response_format, 靠 lenient_json_object 从散文里抠 JSON)。
    last_content = ""
    for _attempt, _extra_body in enumerate((
        {"response_format": {"type": "json_object"}},
        {},
    )):
        response = await _post_chat_completion_raw(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"用户原话: {user_text}"},
            ],
            timeout=timeout,
            proxy=proxy,
            temperature=0.6,
            max_tokens=1600,
            extra_headers={},
            extra_body=_extra_body,
            request_route="imagegen_plan",
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"deepseek plan 返回格式错: {repr(response)[:200]}") from exc
        last_content = str(content or "")
        plan = lenient_json_object(last_content)
        if isinstance(plan, dict):
            return plan
        _logger.warning(
            "imagegen planner attempt %d non-JSON content (len=%d), %s",
            _attempt + 1, len(last_content),
            "retrying without json mode" if _attempt == 0 else "giving up",
        )
    raise RuntimeError(f"deepseek plan 不是 JSON 对象: {last_content[:200]}")


async def _persona_image_caption(
    config: Config, persona: Any, user_text: str, gen_prompt: str,
) -> str:
    """非 catty 人格的画图配文 — 用人格 core_persona + instant 模型现写 (主人 2026-07-06:
    机机的画图回复让 AI 自己来写, 不用 planner 的 short_review 模板腔)。失败返 "" 由调用方兜底。"""
    from .openai_client import chat_completion_codex_instant
    messages: list[dict[str, Any]] = []
    core = str(getattr(persona, "core_persona", "") or "")
    if core:
        messages.append({"role": "system", "content": core})
    messages.append({
        "role": "system",
        "content": (
            "情境: 你刚用画图工具画完一张图, 图马上会随这条消息一起发到群里. "
            f"生图内容概要: {(gen_prompt or '')[:200]}\n"
            "给这张图写配文: 1-2 条你的口吻短句 (可换行分条), 别解释画图过程, "
            "别罗列积分/数字 (系统会自动补报价), 不用 Markdown, 只输出正文."
        ),
    })
    messages.append({"role": "user", "content": (user_text or "").strip()[:200] or "画好了"})
    try:
        reply = await chat_completion_codex_instant(config, messages, max_tokens=150)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("persona image caption failed (non-fatal): %s", exc)
        return ""
    return str(reply or "").strip()


async def _exec_imagegen_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Agent 模式入口 — sonnet 传空 args 时执行此函数。

    1. 拿 ctx.user_text 喂给 deepseek 出 plan
    2. plan.self_portrait 命中时加 Miao/miaomiao*.png 作参考图 (强制 provider=nai)
    3. 走 _exec_imagegen_nai / 或递归回 _exec_imagegen (gpt 分支) 生图
    4. 把 deepseek 的 short_review 拼到 _short_circuit_reply, 让 chat loop 直接 return
    """
    user_text = str(getattr(ctx, "user_text", "") or "").strip()
    if not user_text:
        return {
            "error": "拿不到用户原话, agent 模式无法生 prompt",
            "guidance": "主 AI 直接拒绝这次画图, 让用户重新发请求",
        }

    # 调 deepseek 出 plan
    _persona = getattr(ctx, "persona", None)
    _is_catty = _is_catty_persona(_persona)
    _persona_imagegen = getattr(_persona, "imagegen", None) if not _is_catty else None
    try:
        plan = await _deepseek_imagegen_plan(ctx.config, user_text, persona=_persona)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "imagegen agent: deepseek plan failed (%s: %s), falling back to default nai",
            exc.__class__.__name__, exc,
        )
        if _persona_imagegen is not None:
            # fallback prompt 画的就是人格本人 → self_portrait=sfw 保证参考图不丢
            # (主人 2026-07-06: 之前 fallback 不带 self_portrait, planner 一挂参考图就丢)
            plan = {
                "provider": "nai",
                "prompt": f"{_persona_imagegen.girl_tags}, cute, anime style",
                "aspect": "portrait",
                "self_portrait": "sfw",
                "short_review": "画好了. 快看",
            }
        elif _is_catty:
            plan = {
                "provider": "nai",
                "prompt": "1girl, white hair, cat ears, JK uniform, golden eyes, cute, anime style",
                "aspect": "portrait",
                "short_review": "画好啦主人~ ฅฅ",
            }
        else:
            plan = {
                "provider": "nai",
                "prompt": "anime illustration, detailed, high quality",
                "aspect": "portrait",
                "self_portrait": None,
                "short_review": "画好了. 请查看",
            }

    provider = str(plan.get("provider") or "nai").strip().lower()
    if provider not in ("nai", "gpt"):
        provider = "nai"
    gen_prompt = str(plan.get("prompt") or "").strip()
    if not gen_prompt:
        return {"error": "deepseek 没出 prompt, 拒绝本次画图"}
    aspect = str(plan.get("aspect") or "portrait").strip().lower()
    quality = str(plan.get("quality") or "low").strip().lower()
    neg = str(plan.get("negative_prompt") or "").strip()
    self_portrait_kind = (plan.get("self_portrait") or "").strip().lower() \
        if isinstance(plan.get("self_portrait"), str) else ""
    if not _is_catty and _persona_imagegen is None:
        self_portrait_kind = ""
    _default_review = (
        "画好啦主人~ ฅฅ"
        if _is_catty
        else "画好了. 快看" if _persona_imagegen is not None else "画好了. 请查看"
    )
    if not _is_catty and _persona_imagegen is None:
        short_review = _default_review
    else:
        short_review = str(plan.get("short_review") or _default_review).strip()

    # 自画像 → 强制 NAI + 加本地参考图 (锁人设)
    local_ref_bytes: list[bytes] = []
    if self_portrait_kind in ("sfw", "nsfw"):
        ref_data_list = _load_self_portrait_reference_bytes(self_portrait_kind, persona=_persona)
        if ref_data_list:
            local_ref_bytes.extend(ref_data_list)
            if provider != "nai":
                _logger.info(
                    "imagegen agent: self_portrait=%s, forcing provider nai (was %s)",
                    self_portrait_kind, provider,
                )
                provider = "nai"

    _logger.info(
        "imagegen agent: deepseek plan provider=%s self_portrait=%s prompt_len=%d short_review_len=%d",
        provider, self_portrait_kind or "-", len(gen_prompt), len(short_review),
    )

    # 真正生图
    if provider == "nai":
        gen_result = await _exec_imagegen_nai(
            prompt=gen_prompt,
            negative_prompt=neg,
            aspect=aspect,
            ctx=ctx,
            characters=None,
            references=None,
            local_reference_bytes_list=local_ref_bytes or None,
        )
    else:
        # 递归回 _exec_imagegen 走 gpt 分支 (传带 prompt 的 args 触发原直连路径)
        rebuilt_args = {
            "prompt": gen_prompt,
            "provider": "gpt",
            "quality": quality if quality in _ALLOWED_IMAGEGEN_QUALITY else "low",
            "use_input_image": False,
        }
        gen_result = await _exec_imagegen(rebuilt_args, ctx)

    if not isinstance(gen_result, dict) or "error" in gen_result:
        return gen_result  # 生图失败原样返回, 让主 AI 看到 error 并解释

    # 多人格 (主人 2026-07-06): 非 catty 配文由人格 AI 现写 (core_persona + instant),
    # planner 的 short_review 只作 AI 失败时的兜底.
    if _persona_imagegen is not None:
        _ai_review = await _persona_image_caption(ctx.config, _persona, user_text, gen_prompt)
        if _ai_review:
            short_review = _ai_review

    # 把 deepseek 短评 + 报价拼到 _short_circuit_reply
    cost = int(gen_result.get("cost", 0) or 0)
    balance_after = int(gen_result.get("balance_after", -1) or -1)
    is_owner_charge = bool(gen_result.get("is_owner_charge"))
    if is_owner_charge or cost <= 0:
        final_reply = short_review
    else:
        _cost_tail = "分喵～" if _is_catty else "分"
        final_reply = (
            f"{short_review} 这张图消耗了 {cost} 积分, "
            f"现在还剩 {balance_after} {_cost_tail}"
        )

    gen_result["_short_circuit_reply"] = final_reply
    gen_result["_deepseek_plan"] = {
        "provider": provider,
        "self_portrait": self_portrait_kind or None,
        "prompt_len": len(gen_prompt),
    }
    return gen_result


async def _exec_imagegen(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from pathlib import Path
    if not getattr(ctx.config, "catty_imagegen_enabled", True):
        return {"error": "imagegen 已被配置禁用"}
    # 硬 guard:不指向猫猫的群消息(filter 顺便回的旁观回复)不允许主动画图,
    # 避免群里有人随口说『画一张...』就被猫猫接住乱画(主人明确禁止)。
    # 私聊/直接 @ 猫猫的群消息 is_directly_requested=True,放行。
    if not getattr(ctx, "is_directly_requested", True):
        return {
            "error": _persona_text(
                ctx.persona,
                "用户没直接 @ 猫猫,不允许主动画图。把这条 tool_call 取消,改成纯文字回应。",
                "用户未直接指向当前机器人，不能主动画图。取消这条 tool_call，改成纯文字回应。",
            ),
            "guidance": _persona_text(
                ctx.persona,
                "imagegen 只在用户明确指向猫猫(@ / 引用回复 / 直呼猫猫)+ 说画时才能调。",
                "imagegen 只在用户明确指向当前机器人（@ / 引用回复 / 直呼）并明确要求画图时调用。",
            ),
        }
    # 主人 2026-05-29: agent 模式 — sonnet 端 schema 已经全空, args.prompt 为空时
    # 走 deepseek 代理决策 (provider/prompt/参考图/短评 全甩给 deepseek), 主 sonnet
    # 那一轮 tool_call 之后 _short_circuit_reply 让 chat loop 直接 return, 杜绝二次 100K+
    # 重传 + 杜绝『tool_use ids without tool_result』500. 详见 _exec_imagegen_agent.
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return await _exec_imagegen_agent(args, ctx)
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
                return {
                    "error": _persona_text(
                        ctx.persona,
                        f"生图冷却剩 {int(remaining)}s,稍后再戳人家喵",
                        f"生图冷却剩 {int(remaining)}s，请稍后再试。",
                    )
                }
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
                "user_facing_hint": _persona_text(
                    ctx.persona,
                    f"用猫娘口吻提醒用户:他当前只有 {balance} 积分,这张图(quality={quality})要 {image_cost} 分,"
                    f"还差 {image_cost - balance} 分。让他发『签到』来领今天的积分,"
                    f"他现在好感等级 Lv{level},今天签到大概能拿 {lo}-{hi} 分。"
                    "傲娇但要把要点说全:差多少、要 Lv 几、发『签到』两个字就能领。"
                    "**禁止**自己再调一次 catty_imagegen 重复发。",
                    f"提醒用户：当前只有 {balance} 积分，这张图（quality={quality}）需要 {image_cost} 分，"
                    f"还差 {image_cost - balance} 分。可发送『签到』领取今日积分；当前好感等级为 Lv{level}，"
                    f"今天预计可获得 {lo}-{hi} 分。说明差额、等级与签到方式。"
                    "**禁止**再次调用 catty_imagegen 重复生成。",
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
                "user_facing_hint": _persona_text(
                    ctx.persona,
                    "可以对用户说:『主人这次画图被上游网关挡住啦(尾巴垂垂),"
                    "猫猫稍等再试,或者主人精简下要求~』",
                    "可以对用户说：『这次画图被上游网关拦住了，请稍后再试，或者精简一下要求。』",
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
        "guidance": _persona_text(
            ctx.persona,
            "图已经程序自动发出去了,你只需补 1-2 句猫娘短评(『画好啦~主人看看喜不喜欢喵 ฅฅ』)。"
            "**禁止**贴 base64 / file 路径 / image_uri 到回复里;**禁止**再调一次 catty_imagegen 重复发。"
            + (
                ""
                if is_owner_charge
                else f" **必须在最终回复里报价**:『这张图(quality={quality})消耗了 {image_cost} 积分,主人现在还剩 {balance_after} 分喵～』(余额低于 100 时加『再画要签到啦~』)。"
            ),
            "图片已经自动发出。只补 1-2 句符合当前人格的短评。"
            "**禁止**贴 base64 / file 路径 / image_uri 到回复里；**禁止**再次调用 catty_imagegen 重复生成。"
            + (
                ""
                if is_owner_charge
                else f" **必须在最终回复里报价**：『这张图（quality={quality}）消耗了 {image_cost} 积分，当前余额 {balance_after} 分。』"
                "余额低于 100 时可提醒签到。"
            ),
        ),
    }


async def _exec_story_arc_set(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.story_arc_store is None or not ctx.scope_key:
        return {"error": "story_arc 不可用(store 未注入或 scope 缺失)"}
    title = str(args.get("title") or "").strip()[:20]
    context = str(args.get("context") or "").strip()[:150]
    if not title or not context:
        return {"error": "title 和 context 都必填"}
    if len(context) < 40:
        return {"error": "context 至少需要 40 字"}
    try:
        ttl_raw = args.get("ttl_hours")
        ttl_hours = float(ttl_raw) if ttl_raw is not None else 3.0
    except (TypeError, ValueError):
        ttl_hours = 3.0
    if ttl_hours != ttl_hours or ttl_hours in (float("inf"), float("-inf")):
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
    confidence_raw = args.get("confidence")
    try:
        confidence = int(confidence_raw) if confidence_raw is not None else 80
    except (TypeError, ValueError):
        confidence = 80
    confidence = max(0, min(confidence, 100))
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
    payload["guidance"] = _persona_text(
        ctx.persona,
        "挑 1-3 条最有梗/最有讨论价值的复述,加猫娘吐槽,不要贴 URL,"
        "不要照搬整段 JSON,不要复读 rank 数字。",
        "挑 1-3 条最有梗或最有讨论价值的内容复述，可加入当前人格的简短评论，"
        "不要贴 URL、照搬整段 JSON 或复读 rank 数字。",
    )
    return payload


# ── P5.6 catty_recall_user_messages: on-demand 群聊 history ─────────
# 主人 2026-05-28 plan-cattyCacheFixAndPromptSlim P5.6:
# 默认 history per-user (P5.4). 群聊时 AI 看不到别人 history. 当 user msg 提到
# @某人 / "X 怎么说" / "X 刚才聊啥" 时, AI 调本 tool 拉某 QQ 最近 N 条消息.
# 不增加默认 prompt size — 真要才拉.
async def _exec_recall_user_messages(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target_user_id = str(args.get("user_id") or "").strip()
    if not target_user_id:
        return {"error": "user_id 必填 (要拉哪个 QQ 的消息)"}
    try:
        count = int(args.get("count") or 8)
    except (TypeError, ValueError):
        count = 8
    count = max(1, min(count, 20))  # cap 1-20

    if not ctx.group_id:
        return {"error": "本 tool 只在群聊里能调; 私聊本身就是 per-user 不需要"}

    # session key 复合 (group_id, user_id) — 跟 build_history_key 同款
    scope_key = f"group:{ctx.group_id}:user:{target_user_id}"
    try:
        from . import _get_session_cache  # type: ignore
        cache = _get_session_cache()
        msgs = list(cache.get(scope_key) or [])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"无法拉 session_cache: {exc.__class__.__name__}"}

    if not msgs:
        return {
            "user_id": target_user_id,
            "group_id": ctx.group_id,
            "count": 0,
            "messages": [],
            "note": f"该 QQ ({target_user_id}) 在本群无 history 记录 (可能从未发言 / 被 prune)",
        }

    # 取末尾 N 条 + 简短 dump (role + content 前 80 字符)
    recent = msgs[-count:]
    dumped = []
    for m in recent:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "?"))
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text", "")) for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        content_str = str(content)[:120]
        dumped.append({"role": role, "content": content_str})

    return {
        "user_id": target_user_id,
        "group_id": ctx.group_id,
        "count": len(dumped),
        "messages": dumped,
        "note": "QQ 群里这个用户的最近对话 history. 用来接梗 / 回顾 / 找上下文.",
    }


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
            "note": _persona_text(
                ctx.persona,
                "猫猫在这个平台没账号(或者还没设置)",
                "当前机器人在这个平台没有账号（或者尚未设置）。",
            ),
        }
    return {"platform": platform, "url": url}


# Executor 注册表:name → async callable
ToolExecutor = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

_EXECUTORS: dict[str, ToolExecutor] = {
    "catty_recall": _exec_recall,
    "catty_user_profile": _exec_user_profile,
    "catty_mc_status": _exec_mc_status,
    "catty_emoji": _exec_emoji,
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
    "catty_recall_user_messages": _exec_recall_user_messages,  # P5.6 on-demand group history
}


# ── 对外 API ───────────────────────────────────────────────────────────

# 主人 2026-05-28 C15-7: NLU intent → 只发命中意图相关的 tools, 不命中 tools=[]
# 关键词列表覆盖每个 tool 的常见触发词. 命中 1+ tool 时发对应 tool schema 给 AI,
# AI 看完整 description 决策. 不命中 → tools=[], 省 20K+ bytes input.
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "catty_emoji": (
        "表情", "表情包", "斗图", "emoji", "发个表情", "来个表情", "挑一只小猫",
        "害羞", "贴贴", "得意", "被夸", "炸毛", "脸红", "绷不住", "委屈", "疑惑",
    ),
    # 主人 2026-06-06: catty_imagegen 不再用窄关键词表 — 改由 _is_image_intent 统一判断
    # (覆盖 头像/图片/做张图/来一张/出个图/绘制/线稿 等口语 + NSFW explicit override),
    # 与 NSFW spark 短路同一真相源, 消除两表漂移。详见 _detect_tool_intent / _IMAGE_INTENT_WORDS。
    "catty_nai_director": (
        "抠图", "去背景", "transparent", "线稿", "lineart", "sketch", "草图",
        "上色", "colorize", "情绪", "emotion", "去杂物", "declutter",
        "变形", "transform", "director", "加工", "修一下",
    ),
    "catty_image_search": (
        "搜图", "反搜", "反向搜", "找原图", "找作者", "谁画的", "出处",
        "什么番", "哪个动画", "saucenao", "yandex", "tracemoe", "推主",
        "X 账号", "Twitter", "查作者", "查画师",
    ),
    "catty_web_search": (
        "搜", "搜一下", "查", "查一下", "百度", "谷歌", "google", "bing",
        "新闻", "最近怎样", "事件", "热搜", "联网", "上网搜",
    ),
    "catty_nsfw_search": ("pixiv", "p 站", "色图", "本子", "找一张涩"),
    "catty_meme_query": ("梗图", "搜个梗", "meme", "网图", "来张图"),
    "catty_meme_explain": (
        "这是什么梗", "啥梗", "什么意思", "解释一下", "百科", "梗百科",
    ),
    "catty_game_recall": ("游戏记忆", "我之前玩", "我在玩什么", "之前那个游戏"),
    "catty_game_remember": ("记一下我在玩", "记我玩", "记游戏"),
    "catty_hot_trends": ("热搜", "热点", "trending", "现在火什么", "今日热搜"),
    "catty_now": ("现在几点", "几号", "今天日期", "时间", "周几"),
    "catty_remember": ("帮我记", "记一下", "记下来", "存一下", "笔记", "remember"),
    "catty_recall_notes": ("查笔记", "之前记的", "笔记里", "recall notes"),
    "catty_recall": ("上次", "之前", "记得", "那次", "刚才"),
    "catty_user_profile": ("他是谁", "她是谁", "什么人", "什么样的人", "user profile"),
    "catty_social_account": ("Steam", "B 站", "bilibili", "youtube", "github", "推特"),
    "catty_group_game_tag": ("这群玩什么", "群在玩", "group game"),
    "catty_mc_status": ("MC", "minecraft", "我的世界", "服务器", "mc 在线"),
    "catty_story_arc_set": ("开 arc", "记一个故事", "story arc", "开始一条"),
    "catty_story_arc_clear": ("结束 arc", "清掉故事", "arc clear"),
    # P5.6: 群聊提到 @某人 / "X 怎么说" / "X 刚才聊啥" 时 AI 拉 per-user history
    "catty_recall_user_messages": (
        "他怎么说", "她怎么说", "他刚才", "她刚才", "他之前", "她之前",
        "刚才聊啥", "之前聊啥", "聊了什么", "刚才在说", "前面说",
    ),
}


_EXPLICIT_HTTP_URL_RE = re.compile(
    r"https?://[^\s<>()\[\]{}\"'`]+",
    re.IGNORECASE,
)
_COMMON_IMAGE_URL_RE = re.compile(
    r"\.(?:apng|avif|bmp|gif|heic|heif|jpe?g|png|tiff?|webp)(?:[?#][^\s<>()\[\]{}\"'`]*)?$",
    re.IGNORECASE,
)
_URL_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)]}）】》〉」』"


def _has_explicit_image_url(user_text: str) -> bool:
    """Return whether text supplies an http(s) image URL usable by reverse search."""
    text = str(user_text or "")
    urls = [
        url.rstrip(_URL_TRAILING_PUNCTUATION)
        for url in _EXPLICIT_HTTP_URL_RE.findall(text)
    ]
    if not urls:
        return False
    if any(_COMMON_IMAGE_URL_RE.search(url) for url in urls):
        return True
    context_text = _EXPLICIT_HTTP_URL_RE.sub(" ", text).lower()
    return any(
        keyword.lower() in context_text
        for keyword in _INTENT_KEYWORDS["catty_image_search"]
    )


_IMAGEGEN_FORCE_QUERY_WORDS: tuple[str, ...] = (
    "会画图吗", "会不会画图", "能画图吗", "能不能画图", "可以画图吗",
    "可不可以画图", "支持画图吗", "画图功能", "怎么画图", "怎么生图",
)
_IMAGEGEN_FORCE_NSFW_BLOCK_WORDS: tuple[str, ...] = (
    "抽插", "抽送", "插入", "插进", "插到", "插着",
    "操我", "操你", "干你", "干我", "射进", "射满", "内射",
    "精液", "蜜穴", "蜜液", "高潮", "潮吹", "潮喷",
    "肉棒", "鸡巴", "下体", "阴茎", "做爱", "做我", "做你",
)
_IMAGEGEN_FORCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:帮我|给我|替我)?画(?:一张|一个|一幅|一下|张|个|幅|下).+",
        r"(?:帮我|给我|替我).{0,6}(?:画图|生图|出图|生成图)",
        r"(?:生成|做|出|来)(?:一张|一个|一幅|张|个|幅)?(?:图|图片|图像|插画|海报|壁纸|立绘|头像).+",
        r"(?:画|生成|做|出).{0,10}(?:自画像|自拍|头像|立绘|插画|壁纸|海报|原画|线稿|猫娘|你自己|笨猫|猫猫)",
    )
)


def should_force_imagegen_tool(user_text: str, *, is_directly_requested: bool) -> bool:
    """Return True when the user clearly ordered Catty to draw now."""
    if not is_directly_requested:
        return False
    text = re.sub(r"\s+", "", str(user_text or "").strip().lower())
    if not text:
        return False
    if any(word in text for word in _IMAGEGEN_FORCE_QUERY_WORDS):
        return False
    if any(word in text for word in _IMAGEGEN_FORCE_NSFW_BLOCK_WORDS):
        return False
    return any(pattern.search(text) for pattern in _IMAGEGEN_FORCE_PATTERNS)


# 画图意图判断 — 主人 2026-06-06: 从 __init__.py 下沉到此, 作为"画图意图"的单一真相源,
# 同时供 NSFW spark 短路 (__init__ 从这里 import _is_image_intent) 和主 NLU gate
# (_detect_tool_intent) 共用, 消除两表漂移 (此前 _INTENT_KEYWORDS['catty_imagegen'] 远窄于
# spark 表 → 明确画图请求工具不进列表永远不画)。覆盖口语画图措辞: 头像/自拍/图片/做张图/
# 来一张/出个图/绘制/线稿/海报/壁纸 等。
_IMAGE_INTENT_WORDS: tuple[str, ...] = (
    "画一", "画张", "画个", "画下", "画幅", "画起", "画我", "画你", "画猫",
    "画一张", "画张图", "画个图", "画下图", "画图",
    "绘一", "绘画", "绘制", "绘个", "绘出",
    "出图", "出张", "出一张", "出个图",
    "imagegen", "imggen", "image gen",
    "生成图", "生图", "生成一张", "生成图片", "生成插画", "生成一幅",
    "做张图", "做一张图", "做个图", "搞张图", "搞个图", "弄张图", "弄个图",
    "来一张", "来张图",
    # 主人 2026-05-27 十七轮 fix: 砍 '插画' 单独 (会被 NSFW '抽插画X' 误命中),
    # 改成只命中 '张插画 / 画插画 / 来插画' 等显式画图动词搭配
    "二次元", "动漫图", "原画", "线稿", "立绘", "头像", "自拍", "自画像",
    "张插画", "画插画", "来插画", "出插画",
    "图片", "图像", "图一张",
    # 主人 2026-06-06: 并入原 _INTENT_KEYWORDS['catty_imagegen'] 独有词, 保覆盖不回退
    "海报", "壁纸", "猫娘画",
    # 主人 2026-07-06 多人格: 腿/脚福利词也挂 imagegen tool — 机机人格腿图走画图自画像
    # (catty 群不受影响: legs_picture matcher priority 35 先短路, 到不了主 AI).
    "腿图", "看腿", "脚图", "看脚", "腿照", "脚照", "看看腿", "看看脚",
)

# 主人 2026-05-27 十七轮 fix: NSFW explicit 动作词 — 出现这些就**不是**画图请求
# 即使误命中 image_intent 也 override (例如『抽插画圈』 误中 '画X' / '插画')
_IMAGE_INTENT_NSFW_OVERRIDE_WORDS: tuple[str, ...] = (
    "抽插", "抽送", "插入", "插进", "插到", "插着",
    "操我", "操你", "操猫", "操她", "操他", "干你", "干我",
    "射进", "射满", "内射", "射在", "精液", "蜜穴", "蜜液",
    "高潮", "潮吹", "潮喷", "勃起",
    "肉棒", "鸡巴", "下体", "阴茎",
    "舔下", "舔进", "扣下", "扣进",
    "做爱", "做我", "做你",
)


def _is_image_intent(text: str) -> bool:
    """user msg 是否在请求画图 (即使命中 NSFW 触发词也应让位给 imagegen tool).

    主人 2026-05-27 十七轮 fix:
    - 如 text 含 explicit NSFW 动作词 (抽插/内射/蜜穴等) → 强制返 False
      (避免『抽插画圈』『画一下蜜穴』这种 NSFW 上下文被画图意图劫持)
    """
    if not text:
        return False
    # NSFW explicit 动作优先 — 即使含 image_intent 也判 False
    if any(w in text for w in _IMAGE_INTENT_NSFW_OVERRIDE_WORDS):
        return False
    return any(w in text for w in _IMAGE_INTENT_WORDS)


def _detect_tool_intent(
    user_text: str,
    has_image: bool,
    *,
    has_explicit_image_url: bool = False,
) -> set[str]:
    """NLU 简易关键词匹配 — 命中返回相关 tool name set, 不命中返回空 set.

    image_search 可使用文字里的显式图片 URL；nai_director 仍需要实际上下文图片。
    catty_imagegen 走专用 _is_image_intent (含 NSFW explicit override), 与 spark 短路同一真相源。
    """
    if not user_text:
        return set()
    text_lower = user_text.lower()
    hit: set[str] = set()
    for tool_name, keywords in _INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                hit.add(tool_name)
                break
    # 主人 2026-06-06: imagegen 意图统一走 _is_image_intent — 覆盖 头像/图片/做张图/来一张 等
    # _INTENT_KEYWORDS 漏掉、但 spark 短路表 (_IMAGE_INTENT_WORDS) 有的口语措辞, 消除两表漂移。
    if _is_image_intent(user_text):
        hit.add("catty_imagegen")
    # 文字图片 URL 只可供 image_search 使用，不能把它当作可加工的上下文图片。
    if has_explicit_image_url:
        hit.add("catty_image_search")
    if not has_image:
        hit.discard("catty_nai_director")
    if not (has_image or has_explicit_image_url):
        hit.discard("catty_image_search")
    return hit



_NON_CATTY_SCHEMA_DESCRIPTIONS: dict[str, tuple[str | None, str | None]] = {
    "catty_emoji": (
        "从本地表情库搜索并发送一张表情图。适用场景：需要用表情表达害羞、得意、贴贴、"
        "困惑、笑、撒娇或嘲讽等情绪，用户明确请求表情图，或文字回复后配一张本地表情更自然时。"
        "选中的图片会加入待发送队列；最终回复只需补一句短评，不要输出图片路径。"
        "不要用于搜索具体网图、角色图或梗图主题；这些场景使用 catty_meme_query。",
        None,
    ),
    "catty_recall": (
        "查询长期记忆和待压缩语料，定位与“上次/以前/那时候”有关的发言或共识。"
        "适用场景：用户使用时间指代、需要确认某位群友此前说过的偏好、决定、梗或称呼，"
        "或上下文暗示曾讨论过某个陌生话题。不要用于查询当前活跃消息。"
        "返回 long_term_summary（长期摘要）和 matches（命中条目列表）。",
        None,
    ),
    "catty_user_profile": (
        "查询一个 QQ 用户的画像：称呼、性别、印象、置信度和关系标记。"
        "适用场景：群里出现不认识的 QQ 号，或需要确认某位用户的称呼、关系或特别关心状态。"
        "不要每条消息都查；当前发言者画像已在常驻 context 中。",
        None,
    ),
    "catty_mc_status": (
        "查询 Minecraft 服务器实时状态：在线人数和是否可连上。"
        "适用场景：用户询问 MC 在线人数、服务器是否开启或是否有人在玩，"
        "或需要确认是否邀请群友进服。结果有 30 秒缓存。",
        None,
    ),
    "catty_web_search": (
        "Google/Bing 联网搜索最新信息。只在用户询问新闻、版本、价格、教程、特定事实，"
        "明确要求搜索，或模型知识可能过期时调用。图片出处用 catty_image_search，"
        "网络梗和 ACG 词条用 catty_meme_explain，近期热搜用 catty_hot_trends。"
        "每个 scope+用户有 cooldown；返回 results，基于结果生成回复，不得编造链接或输出 marker 文本。",
        None,
    ),
    "catty_nsfw_search": (
        "搜索 R-18 资源：pixiv 图片或 iwara 视频，仅好友私聊可调用。"
        "kind=image 时插件会直接把下载好的图片发到聊天；拿到结果后只补 1-2 句符合当前人格的短评。"
        "kind=video 时返回链接，挑 1-3 个配简短说明。query 的首个候选保留用户原话语种，"
        "后面可用英文逗号追加 1-2 个候选。插件已自动启用 r18，同一用户 30 秒内只能搜索一次。",
        None,
    ),
    "catty_image_search": (
        "反向搜图：把一张图交给 SauceNAO / Yandex / trace.moe / ascii2d / iqdb，"
        "查询作者、作品、角色、社交账号或同款来源。上下文有图片且用户询问出处、作者、原图、"
        "角色、番剧、X 账号或同款时必须调用本工具，不要用文字搜索替代。"
        "kind=anime 用于番剧场景；artwork 用于二次元插画；photo 用于真人自拍、cosplay、"
        "X 或 Instagram；不确定时用 auto。返回 results 后挑 1-3 条关键结论复述，"
        "不要照搬 JSON、复读相似度小数或编造未返回的信息。每个用户 60 秒一次冷却。",
        None,
    ),
    "catty_game_recall": (
        "查询游戏专属事实库：角色、版本、活动、机制和玩家事件。"
        "适用场景：用户或群友聊到某个游戏，需要确认此前记录的信息。"
        "游戏名建议用小写英文，也接受中文；不知道游戏名时可查询总数或已保存的游戏列表。",
        None,
    ),
    "catty_game_remember": (
        "把值得长期保留的游戏事实写入专属事实库。"
        "仅在用户或群友给出具体版本、角色、机制、活动信息，或明确玩家约定时调用。"
        "不要记录模糊吐槽、临时情绪、无法核验的传闻或重复事实。",
        None,
    ),
    "catty_social_account": (
        "查询当前机器人在指定平台的社交账号链接，不是用户的账号。适用场景："
        "群友询问机器人在 Steam、Epic 等平台的账号，或聊到某游戏所属平台后需要提供对应账号。"
        "先按常识判断游戏所属平台，再用 platform 查询。没有账号时返回空 url 和 note，"
        "应如实说明，不得编造 URL。不要在没人询问时主动调用。",
        "查询当前机器人某平台账号",
    ),
    "catty_hot_trends": (
        "拉取中文互联网当下热搜和热梗（微博 / B 站 / 知乎 / 抖音聚合，180 秒缓存）。"
        "适用场景：用户询问最近热点、热梗或榜单，或出现疑似近期网络新词需要确认。"
        "也可在话题冷清时作为谈资。返回 sources 后挑 1-3 条复述并可加入当前人格的简短评论，"
        "不要贴链接或照搬完整 JSON。",
        None,
    ),
    "catty_remember": (
        "把值得长期记住的事实写入用户或群笔记库。"
        "适用场景：稳定偏好或边界、明确约定或承诺、群级长期特征。"
        "不要记录闲聊吐槽、临时情绪、单次玩笑或已经查询到的同一条事实。"
        "TTL 默认 30 天；偏好和边界可延长，约定应写到事件结束日期。",
        None,
    ),
    "catty_meme_explain": (
        "查询萌娘百科解释网络梗、ACG 词条、角色、作品或二次元术语。"
        "适用场景：群友提到不认识的网络流行语、二次元词条、作品或角色，"
        "或需要确认一个梗的精确出处。拿到结果后用当前人格短句复述，"
        "不要照搬 extract、贴 URL 或复读 JSON。",
        None,
    ),
    "catty_imagegen": (
        "用户直接指向当前机器人（@ / 引用回复 / 直呼）并明确要求画图、生成图片时调用。\n"
        "args 留空 {} 即可——provider 选择、NAI 标签 prompt、GPT 描述句、风格判定、参考图、"
        "报价和短评配文全部由后端代理 LLM 自动决定，图片会自动发到聊天。"
        "tool 调完本轮直接结束，不要再写文字回复。\n"
        "禁触发：用户未直接请求当前机器人时只是闲聊提到画画、未明确要图时讨论某物，"
        "或本地表情图已经足够的场景（走 catty_meme_query）。",
        "用户直接向当前机器人明确请求画图时调用。args 留空 {}；图片会自动发到聊天，调用后本轮直接结束。",
    ),
    "catty_nai_director": (
        "调用 NovelAI Director Tools 对一张已有图片做加工（线稿、抠图、上色、换情绪、去杂物等）。\n"
        "前置条件：用户消息中必须有图片，优先当前消息附图，没有则使用群最近 5 分钟图片。"
        "没有图片时直接让用户先发送图片，不要先调用 catty_imagegen。\n"
        "必须由用户直接指向当前机器人（@ / 直呼）并明确提出具体加工要求时调用；"
        "不要在闲聊提到描线或抠图时主动调用。\n"
        "调用前先说明此次加工需要扣除的积分，bg-removal 需要明确提醒成本较高。"
        "图片自动发送；image_sent=true 后只补 1-2 句符合当前人格的短评和报价。"
        "禁止贴 base64 或文件路径，也不要重复调用。",
        None,
    ),
    "catty_story_arc_set": (
        "为当前会话创建会跨多条消息推进的 story arc。适用场景：双方刚开始一个需要后续推进的事情，"
        "例如约好周末活动、对方正在处理某件事、或群友约定一起完成某项安排。"
        "title 要短，context 写清当前状态、期待或关心点；不要为一句玩笑、单次闲聊或已结束的话题创建。",
        None,
    ),
}

_NON_CATTY_SCHEMA_PROPERTY_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "catty_emoji": {
        "query": "表情意图关键词，例如：害羞、贴贴、得意、脸红、困惑或笑。",
        "tags": "可选，逗号分隔补充标签，例如：害羞、贴贴。",
    },
    "catty_remember": {
        "scope": "user=记到当前发言者的画像笔记（跨群通用）；group=记到当前群的群级笔记。",
    },
    "catty_social_account": {
        "platform": "平台标识（小写英文），目前支持 steam。未来可能扩展 epic、xbox、psn、origin 等；传未知平台会返回 error，应自然说明即可。",
    },
    "catty_story_arc_set": {
        "title": "短标题，≤20 字符，核心一句话。例如“等对方画的图”。",
        "context": "当前状态、后续期待或关心点，30-150 字。例如“对方答应画一张戴蝴蝶结的图，聊到此事时保持期待感。”",
    },
}


def _schema_for_persona(
    name: str,
    schema: dict[str, Any],
    *,
    lazy: bool,
    persona: Any,
) -> dict[str, Any]:
    if _is_catty_persona(persona):
        return schema
    descriptions = _NON_CATTY_SCHEMA_DESCRIPTIONS.get(name)
    description = descriptions[1 if lazy else 0] if descriptions else None
    property_descriptions = (
        {} if lazy else _NON_CATTY_SCHEMA_PROPERTY_DESCRIPTIONS.get(name, {})
    )
    if description is None and not property_descriptions:
        return schema
    function = dict(schema["function"])
    if description is not None:
        function["description"] = description
    if property_descriptions:
        parameters = dict(function["parameters"])
        properties = dict(parameters["properties"])
        for field, field_description in property_descriptions.items():
            if field not in properties:
                continue
            property_schema = dict(properties[field])
            property_schema["description"] = field_description
            properties[field] = property_schema
        parameters["properties"] = properties
        function["parameters"] = parameters
    copied = dict(schema)
    copied["function"] = function
    return copied


def available_tool_schemas(
    config: Config,
    *,
    is_private: bool,
    user_text: str = "",
    has_image: bool = False,
    is_directly_requested: bool = False,
    persona: Any = None,
) -> list[dict[str, Any]]:
    """按 NLU intent 挑 tool schemas — 命中关键词才发对应 tool, 不命中 tools=[].

    主人 2026-05-28 C15-7: 之前每次发全 19 tools (~21K bytes) 浪费 input. 现 NLU gate:
    user msg 含画图/搜/记等意图关键词才发对应 tool, AI 看完整 description 决策.
    大部分闲聊 tools=[], 省 20K+ bytes input.
    保留所有 tool 功能 (description 完整不砍), 只控制何时发.

    Args:
        config: catty config
        is_private: True=私聊 (按 catty_tools_disabled_in_private 进一步过滤)
        user_text: 当前 user msg 文本, 用于 NLU intent 检测
        has_image: 当前或最近上下文是否含实际图片（nai_director 必须）
        is_directly_requested: 当前消息是否直接指向猫猫
        persona: 当前 scope 的 Persona；已禁用的 persona feature 不暴露对应 tool
    """
    enabled = bool(getattr(config, "catty_tools_enabled", True))
    if not enabled:
        return []

    # 主人 2026-05-31 cache follow-up: flash+tools 实测 tools schema 约 6K 字符, 且 API 字段
    # 序列化在 current-user 动态尾巴之后；即使 tools 自身 byte-stable, current-user 漂移也会让
    # tools 一起落进 miss 区。恢复「无工具意图 → tools=[]」, 但只门控 schema, 不删 executor；
    # 命中搜索/画图/记忆/查人/时间/MC/story-arc/图片等意图时仍发对应 tool, 功能不砍。
    has_explicit_image_url = _has_explicit_image_url(user_text)
    intent_hits = _detect_tool_intent(
        user_text,
        has_image,
        has_explicit_image_url=has_explicit_image_url,
    )
    if has_image:
        intent_hits.update({"catty_image_search", "catty_nai_director"})
    # 主人 2026-06-06: 明确画图指令 (should_force_imagegen_tool — force pattern 比
    # _IMAGE_INTENT_WORDS 宽, 含 自画像/你自己/笨猫 等自指措辞) 即使 NLU 没把 imagegen 命中,
    # 也强制把 imagegen schema 注入 — 闭合"force=True 但工具不进列表 → 永远不画"的窗口
    # (与 __init__ 的 force tool_choice 判定是同一根因的上下游)。
    if is_directly_requested and should_force_imagegen_tool(
        user_text, is_directly_requested=True
    ):
        intent_hits.add("catty_imagegen")
    if not intent_hits:
        return []

    disabled_in_private = _private_disabled_tool_names(config) if is_private else set()

    # 主人 2026-05-28 P5.5: lazy schema 默认开 — description ≤30 字, properties 极简.
    _lazy = bool(getattr(config, "catty_tools_lazy_schema_enabled", True))
    _schema_pool = _LAZY_TOOL_SCHEMAS if _lazy else ALL_TOOL_SCHEMAS

    return [
        _schema_for_persona(name, schema, lazy=_lazy, persona=persona)
        for name, schema in _schema_pool.items()
        if name in intent_hits
        and _tool_capability_denial_reason(
            name,
            is_private=is_private,
            is_group=not is_private,
            has_image=has_image,
            is_directly_requested=is_directly_requested,
            has_explicit_image_url=has_explicit_image_url,
            persona=persona,
            disabled_in_private=disabled_in_private,
        ) is None
    ]


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
    *,
    allowed_names: Collection[str] | None = None,
) -> dict[str, Any]:
    """执行一次 tool_call。args 解析失败/未知 tool/执行抛错都返回结构化 error,
    让主 AI 在下一轮自己看懂出错原因(而不是把异常丢给用户)。
    """
    if allowed_names is not None and name not in allowed_names:
        _record_tool_call(_ctx_scope_key(ctx), name, (arguments_json or "")[:60], False)
        return _tool_capability_error(name, "not_in_allowlist", persona=ctx.persona)

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
    has_image = bool(ctx.input_image_urls or ctx.recent_image_urls)
    explicit_image_text = ctx.user_text
    if name == "catty_image_search":
        explicit_url = str(args.get("image_url") or "").strip()
        if explicit_url:
            explicit_image_text = f"{explicit_image_text}\n{explicit_url}"
    has_explicit_image_url = _has_explicit_image_url(explicit_image_text)
    denial_reason = _tool_capability_denial_reason(
        name,
        is_private=ctx.is_private,
        is_group=bool(ctx.group_id),
        has_image=has_image,
        is_directly_requested=bool(ctx.is_directly_requested),
        has_explicit_image_url=has_explicit_image_url,
        persona=ctx.persona,
        disabled_in_private=_private_disabled_tool_names(ctx.config),
    )
    if denial_reason is not None:
        _record_tool_call(_ctx_scope_key(ctx), name, args_preview, False)
        return _tool_capability_error(name, denial_reason, persona=ctx.persona)
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


def tools_system_hint(persona: Any = None) -> str:
    """常驻 system 提示 (主人 C16-6: 砍 17 行→4 行通用, 详细 trigger 走 schema).

    多人格: persona=None/catty 时输出与旧文本逐字节相同 (cache prefix 段)。
    """
    if persona is None or getattr(persona, "name", "catty") == "catty":
        char, tone = "笨猫", "猫娘口吻"
    else:
        char = getattr(persona, "char_name", "机器人")
        tone = f"{char}口吻"
    return (
        "工具调用通用: 1) 真需要才调 (每次=延迟); 闲聊/已知不调.\n"
        f"2) 画图请求**铁律**: {char}所有图都从 catty_imagegen 出, 别用文字脑补图、别用 Markdown 图片语法、别贴外部 URL 假装出图.\n"
        f"3) 拿结果别复读 JSON, 别出现 tool_call 标记 (INLINE_IMAGE 除外); error 用{tone}说 '查不到/想不起来'.\n"
        "4) 详细每 tool 的 trigger/参数/边界看 schema description (NLU intent gate 按 user_text 注入相关 tool)."
    )


def _tools_system_hint_legacy() -> str:
    """Legacy 完整版备份, 不再使用 — 主人 C16-6 决定砍."""
    return (
        "你有 17 个本地工具,**真需要时才调**(每次调用 = 回复变慢):\n"
        "1. catty_recall — '上次/记得/之前'类时间指代且 context 无答案时查长期记忆/语料。\n"
        "2. catty_user_profile — 不确定的非当前发言者 QQ 才查;当前发言者画像已在 context。\n"
        "3. catty_mc_status — 用户问 MC 在线人数/可达性时调。\n"
        "4. catty_web_search — 最新新闻/版本/价格/事实/'搜一下'时调; 60s cd (主人豁免); 已知/闲聊不调。\n"
        "5. catty_nsfw_search — pixiv/iwara, **仅好友私聊**; 群里调返 error → 引导加私聊; 图程序自发, 你补 1-2 句短评不贴 URL。\n"
        "6. catty_emoji — 搜本地表情库并加入待发送队列; 撒娇/情绪/斗图/贴贴/炸毛优先调它,不要再手写 EMOJI_QUERY marker。\n"
        "7. catty_meme_query — Bing 拉梗图; 具体网图/梗图主题才调; 命中图片由程序通过 pending_image_segments 在带外自动发送,只补一句短评。\n"
        "8. catty_game_recall — 查游戏专属事实库 (strinova/star_resonance/minecraft/genshin); 跨群共享。\n"
        "9. catty_game_remember — 群友给出具体名词/数字/版本/共识时记; 游戏群 web_search 自动 sink top3, 看到 `auto_sinked_to_game_memory` 别重复记。\n"
        "10. catty_social_account — 查**笨猫本人**在指定平台账号 (不是主人); 群友问起或聊到对应平台游戏时调。\n"
        "11. catty_group_game_tag — 群是某游戏的群 (长期/明确) confidence>=60 才打标签; 私聊返 error; 错了 remove=true 撤销。\n"
        "12. catty_hot_trends — 中文热搜热梗 (微博/B站/知乎/抖音); '最近网上有啥/不认识的网络新词'调; 挑 1-3 条复述加猫娘吐槽; 90s cd (主人豁免)。\n"
        "13. catty_now — 日期/时间/星期/季节/节日; 用户问『几号/几点/是不是 XX 节』或想用时段(深夜/饭点/节日)做反应时调; 明天=1/后天=2/昨天=-1。\n"
        "14. catty_meme_explain — 萌娘百科查网络梗/ACG/角色/作品; not_found 别重试, 新闻/工业词改调 web_search; 拿 extract 短句复述不贴 URL。\n"
        "15. catty_remember — 写用户/群笔记 (偏好/边界 ttl=90-180, 约定带 event_date 自动倒计时, 群特征); 闲聊吐槽/单次玩笑不要记。\n"
        "16. catty_recall_notes — 查别人笔记 (build_context 已自动注入当前发言者笔记, 别重复查); 想看非发言者 QQ 或本群整体笔记时调。\n"
        "17. catty_imagegen — **【铁律: 笨猫所有画图请求都从这个 tool 出, 别走 Markdown 图片语法/外部 URL/文字脑补图】** 其它通道会丢具体文字/列表/细节。"
        "prompt 改写允许精简/重组/重排, 400-700 字; 不能丢: 引号里文字、列表项数、配色/材质/光影/构图/数字。"
        "触发: 用户明确画/生成 + 主语; 不要聊到就主动生图。NSFW/敏感词拒。图自动发, image_sent=true 后只补 1-2 句短评。180s cd (主人豁免); quality 默认 low。\n"
        "通用: 能并发但总开销=延迟, 能不调就不调; 拿结果别复读 JSON, 别出现 tool_call/function_call 标记; error 用猫娘口吻说 '查不到/想不起来' 不贴 error 文本。"
    )
