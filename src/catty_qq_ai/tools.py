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

from .config import Config
from .hot_trends import fetch_hot_trends, normalize_sources
from .mc_status import _default_probe
from .meme_dict import lookup_term
from .memory import MemoryStore
from .nsfw_search import NsfwResult, search_nsfw
from .parsers import lenient_json_object
from .time_awareness import compute_now
from .web_search import format_search_context, search_image_urls, search_web


_logger = logging.getLogger("catty_qq_ai.tools")


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
            "每个 scope+用户有 600s cooldown(主人/特别关心用户豁免)。"
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


ALL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "catty_recall": _RECALL_SCHEMA,
    "catty_user_profile": _USER_PROFILE_SCHEMA,
    "catty_mc_status": _MC_STATUS_SCHEMA,
    "catty_web_search": _WEB_SEARCH_SCHEMA,
    "catty_nsfw_search": _NSFW_SEARCH_SCHEMA,
    "catty_meme_query": _MEME_QUERY_SCHEMA,
    "catty_game_recall": _GAME_RECALL_SCHEMA,
    "catty_game_remember": _GAME_REMEMBER_SCHEMA,
    "catty_social_account": _SOCIAL_ACCOUNT_SCHEMA,
    "catty_group_game_tag": _GROUP_GAME_TAG_SCHEMA,
    "catty_hot_trends": _HOT_TRENDS_SCHEMA,
    "catty_now": _NOW_SCHEMA,
    "catty_meme_explain": _MEME_EXPLAIN_SCHEMA,
    "catty_remember": _REMEMBER_SCHEMA,
}


# ── Tool 上下文 / 注入 ─────────────────────────────────────────────────

@dataclass(slots=True)
class ToolContext:
    config: Config
    memory_store: MemoryStore
    event: MessageEvent | None
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
# 热搜按 scope(group/private + user) 做 cooldown,主人/特别关心豁免。
# 默认 90s 一次,本身底层已经有 180s 的聚合缓存,这里只是防一个用户连戳。
_hot_trends_cooldowns: dict[str, float] = {}
_HOT_TRENDS_COOLDOWN_SECONDS = 90.0
# 梗百科查询按用户 cooldown,底层已 600s LRU 缓存,这里只防同一人连戳。
_meme_explain_cooldowns: dict[str, float] = {}
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
                "error": f"web_search 冷却剩 {int(remaining)}s,请基于已有知识回答(每 scope+用户 10 分钟一次)。"
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
    "catty_meme_query": _exec_meme_query,
    "catty_game_recall": _exec_game_recall,
    "catty_game_remember": _exec_game_remember,
    "catty_social_account": _exec_social_account,
    "catty_group_game_tag": _exec_group_game_tag,
    "catty_hot_trends": _exec_hot_trends,
    "catty_now": _exec_now,
    "catty_meme_explain": _exec_meme_explain,
    "catty_remember": _exec_remember,
}


# ── 对外 API ───────────────────────────────────────────────────────────

def available_tool_schemas(config: Config, *, is_private: bool) -> list[dict[str, Any]]:
    """按场景挑出本次主回复应该挂的 tool schemas。

    主人选择的是'始终挂载',所以默认返回全部三个;但允许通过 config 在私聊里
    剔除特定 tool(默认私聊不挂 catty_user_profile,私聊只有一个人没必要查别人画像)。
    """
    if not getattr(config, "catty_tools_enabled", True):
        return []
    excluded: set[str] = set()
    if is_private:
        for name in getattr(config, "catty_tools_disabled_in_private", []) or []:
            excluded.add(str(name).strip())
    return [schema for name, schema in ALL_TOOL_SCHEMAS.items() if name not in excluded]


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
        return {"error": f"未知 tool: {name}"}
    raw = (arguments_json or "").strip()
    if not raw:
        args: dict[str, Any] = {}
    else:
        # 走 lenient_json_object 让 fence / 智能引号 / 尾逗号 / 单引号都能恢复。
        parsed = lenient_json_object(raw)
        if parsed is None:
            return {"error": "arguments 不是合法 JSON 对象,无法解析"}
        args = parsed
    try:
        return await executor(args, ctx)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Tool %s execution failed: %s", name, exc, exc_info=True)
        return {"error": f"{name} 执行失败: {exc.__class__.__name__}: {exc}"}


def tools_system_hint() -> str:
    """常驻 system 提示:告诉主 AI 工具的存在和调用边界。"""
    return (
        "你接入了十四个本地工具,**只在真的需要时调用**(每次调用都让回复变慢):\n"
        "1. catty_recall — 查历史记忆/语料/长期摘要。用户用'上次/记得/之前/还记得'等时间指代,"
        "且常驻 context 没给答案时再调。\n"
        "2. catty_user_profile — 查用户画像/称呼/性别/是否主人。群里冒出一个你不确定怎么称呼的"
        "非当前发言者 QQ 号时再调;当前发言者画像已在 context 里,不要重复查。\n"
        "3. catty_mc_status — 查 MC 服务器在线人数与可达性。用户问 MC 在不在/几个人在玩 时调。\n"
        "4. catty_web_search — Google/Bing 联网搜索。用户问最新新闻/版本/价格/教程/具体事实,"
        "或明确说'搜一下/查一下/联网'时调;有 10 分钟 cooldown(主人豁免)。普通闲聊/已经知道的事不要调。\n"
        "5. catty_nsfw_search — pixiv 图 / iwara 视频。**仅好友私聊里可调**,群里调会返回 error"
        "(此时引导用户加好友私聊,不要重试);kind=image 时图片由程序下载并自动发送,"
        "你只补 1-2 句猫娘短评,不要贴链接也不要复读 URL。\n"
        "6. catty_meme_query — Bing 图片搜索拉一张梗图嵌入回复。**只有特定主题画面需求时调**——"
        "撒娇/玩梗/情绪反应让本地表情库随机配(<<<CATTY_EMOJI_QUERY:意图>>> marker 老路径)更快。"
        "拿到 image_uri 后必须用 <<<CATTY_INLINE_IMAGE:URI>>> 标记嵌入最终回复指定位置。\n"
        "7. catty_game_recall — 查指定游戏的**专属事实记忆库**(跨群跨用户共用,独立于 catty_recall)。"
        "群友聊起游戏角色/版本/活动/机制,你想用之前积累的事实回答时调。"
        "游戏名小写英文优先(strinova / star_resonance / minecraft / genshin),中文也接受。\n"
        "8. catty_game_remember — 把值得长期记住的游戏事实写入记忆库。"
        "**只在群友给出了具体名词/数字/版本/共识结论时才记**;闲聊吐槽/单次玩笑不要记。"
        "在游戏群里调 catty_web_search 时**程序会自动 sink top 3 结果**到对应游戏库,"
        "你拿到 web_search 返回看到 `auto_sinked_to_game_memory` 字段就说明已自动收集,"
        "**不要再调 catty_game_remember 写同样的内容**(去重也会拦,但浪费一次工具调用)。\n"
        "9. catty_social_account — 查**你自己(笨猫本人)**在指定平台的社交账号链接,不是主人的。"
        "**只在群友问起你某个平台账号、或聊到某游戏想给出你自己对应平台账号时调**。"
        "调用前要先用常识判断游戏属于哪个平台(CS2/Dota2/PUBG → steam;原神 → 自有平台不在 steam)。"
        "没人问就别主动报账号。\n"
        "10. catty_group_game_tag — 给**当前群**打『和某游戏相关』的长期标签,以后这个群聊该游戏就自动注入游戏记忆库 context。"
        "**只在你非常确定群本身就是这个游戏的群(多人多消息长期讨论 / 群名简介明确)时才调**;"
        "confidence 必须 >= 60 才接受,所以宁可不调也别瞎打;一次零星提及不要打标签。"
        "私聊里调返回 error。发现标错了可以 remove=true 移除。\n"
        "11. catty_hot_trends — 拉中文互联网当下热搜/热梗(微博/B站/知乎/抖音,180s 聚合缓存)。"
        "**群友问『最近网上有啥热点/热梗/瓜』、用了一个像最近网络新词的不认识词、或话题真的冷场需要谈资时**调。"
        "拿到结果挑 1-3 条最有梗的复述,加猫娘吐槽,不要贴 URL,不要复读 JSON 原文。"
        "用户明确点了具体源(『微博热搜』)就 sources 只填那一个,省一次外网调用。"
        "每个 scope+用户 90s cooldown(主人/特别关心豁免)。\n"
        "12. catty_now — 当前(或偏移)日期/时间/星期/季节/节日感知。纯本地零网络,但**不要每条消息都调**。"
        "用户问『今天几号/星期几/几点了/是不是 XX 节』,或你想根据时段(深夜→唠叨早睡、饭点→吃了没、"
        "周末→放假气氛)/节日(春节/中秋/双十一)做合适反应时才调。"
        "拿到结果别复读 JSON,自然融进回复就好。明天=delta_days=1,后天=2,昨天=-1。\n"
        "14. catty_remember — 把『稳定偏好/边界/约定/群级标签』写入用户/群笔记库,长期沉淀。"
        "和 catty_game_remember 区别:game_remember 给特定游戏库,catty_remember 给当前用户或当前群本身。"
        "**不要**记闲聊吐槽/临时情绪/单次玩笑;只记 (a) 偏好/边界(ttl 90-180) (b) 约定(ttl 到事件结束) "
        "(c) 群本身特征。**约定/事件**额外传 event_date(ISO YYYY-MM-DD)——用 entity 解析出的 iso 直接喂,"
        "ttl 自动算,build_context 倒计时『还剩 N 天/今天/明天』自动展示。"
        "写入后下次主回复 system context 自动加载,你不用再读。\n"
        "13. catty_meme_explain — 萌娘百科查**网络梗 / ACG 词条 / 角色 / 作品 / 二次元术语**。"
        "群友冒出一个你不认识的网络流行语(yyds/绷不住了)、二次元词条(孤独摇滚/雷电将军)、角色/作品名;"
        "或想确认梗的精确出处时调。**只覆盖网络梗 + ACG**——拿到 error=not_found 不要重试,"
        "如果词是新闻/工业/金融/政治名词改调 catty_web_search。"
        "结果缓存 10 分钟。拿到 extract 用猫娘口吻短句复述,不要照搬整段,不要贴 URL,不要复读 JSON。\n"
        "通用规则:\n"
        "- 多个 tool 调用可以并发(同一轮发起多个 tool_calls)但**总开销=回复延迟**,能不调就不调。\n"
        "- 拿到 tool 结果后基于结果写最终回复;**禁止复读 tool 返回的 JSON 原文**,"
        "也禁止在最终回复里出现 tool_call/function_call/[[CATTY_*]] 标记(INLINE_IMAGE 除外)。\n"
        "- 拿到 error 字段时用猫娘口吻自然说一句'人家想不起来/查不到/服掉了/群里说不太合适'即可,"
        "不要把 error 文本贴给用户看。"
    )
