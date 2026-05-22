"""QQ / 网络黑话本地字典 —— 让笨猫看到群友的缩写/网络梗就秒懂,不用问也不用调外网。

设计目标:
- 静态字典 + 正则匹配。零网络,纯本地 O(n)。
- 覆盖高频 QQ 网络黑话:`xs/u1s1/yyds/awsl/绷/破防/泪目/绝绝子/yysy/666/6/3Q` 等。
- ASCII 缩写必须**词边界匹配**(防止 `xs` 误中 `extra` / `explosion` 之类英文词);
  中文/全角项允许子串匹配。
- 大小写无关。
- 提供两个 API:
    annotate_slang(text) -> list[(matched_term, meaning)]
    build_slang_context(text) -> str  # 给主回复直接拼 system prompt

不做的事:
- LLM 解释/语义级判断(这是给静态高频项打标签,不是百科)。
  生僻或新词应该走 catty_meme_explain(萌娘百科)兜底。
- 多语种(只覆盖中文 + 中英混杂场景)。

新增条目准则:**至少在 2024 后还在大量使用,且歧义低到不会误伤正常文本**。
不确定就别加,宁可漏报别误报。
"""
from __future__ import annotations

import re
from typing import Iterable


# 字典格式:term(小写) -> meaning(给 AI 看的解释)
# 添加时注意:ASCII 项必须用纯小写;中文项用最常见写法。
_SLANG: dict[str, str] = {
    # ── 高频英数缩写 ──────────────────────────────────────────────
    "xs": "笑死",
    "xswl": "笑死我了",
    "u1s1": "有一说一",
    "yyds": "永远的神(夸爆了)",
    "yysy": "有一说一",
    "awsl": "啊我死了(可爱到死/激动到死)",
    "rnm": "辱骂(略带粗口,看语气判断是吐槽还是真骂)",
    "tql": "太强了",
    "nbcs": "nobody cares",
    "nb": "牛逼",
    "yjgj": "永居广技/永居广记(玩梗写法,通常是『永居关键』)",
    "ggsg": "搞搞事故(玩笑)",
    "kdl": "磕到了(嗑 CP 嗑到了)",
    "dbq": "对不起",
    "bdjw": "不懂就问",
    "xdm": "兄弟们",
    "jms": "姐妹们",
    "ssfd": "瑟瑟发抖",
    "djll": "顶级流量",
    "blx": "玻璃心",
    "ssr": "Super Special Rare(抽卡最高稀有度,泛指最强/最稀缺)",
    "ow": "Overwatch(守望先锋)",
    "wc": "卧槽(惊讶/感叹)",
    "wtf": "What The Fuck(同 wc)",
    "bgm": "背景音乐",
    "cp": "Couple(配对/磕的两个人)",
    "bg": "异性恋向 CP",
    "bl": "男男向 CP",
    "gl": "女女向 CP",
    "drl": "打人了/打人啦(吐槽)",
    "lsp": "老色批(看色图的人,泛吐槽)",
    "kfc": "KFC(肯德基,常配『疯狂星期四 V 我 50』梗)",
    "vme50": "V 我 50(肯德基疯狂星期四梗)",
    # ── 中文短词/玩梗 ────────────────────────────────────────────
    "绷不住": "绷不住了(笑出来/破防)",
    "绷": "绷不住的省略(笑出来)",
    "破防": "情感被击中(感动/委屈/破大防)",
    "泪目": "感动到眼眶湿",
    "绝绝子": "太绝了(夸/吐槽都行)",
    "嘎嘎": "非常/很(嘎嘎好用)",
    "破防了": "情感被击中",
    "下头": "看到不舒服/扫兴",
    "上头": "投入/上瘾/激动",
    "炸了": "群里热闹爆了/事件大热",
    "蚌埠住了": "绷不住了的谐音梗(笑出来)",
    "蚌": "绷的谐音(笑出来)",
    "麻了": "脑子麻了(看到太离谱)",
    "评价是": "玩梗用语,跟『这味儿太对了』",
    "典中典": "经典中的经典(常带讽刺)",
    "典": "典中典的省略",
    "孝": "孝子(讽刺无脑追捧)",
    "急了": "对方破防/恼羞成怒(讽刺用)",
    "麻了麻了": "麻了的强调",
    "我哭死": "感动到哭",
    "啊这": "无语/欲言又止",
    "笑不活了": "笑死",
    "无语死了": "无语",
    "牛蛙": "牛逼的谐音(夸)",
    "盖了帽了": "厉害死了",
    "栓Q": "Thank you(玩梗,常带无语意味)",
    "栓q": "Thank you(玩梗,常带无语意味)",
    "退退退": "走开/讨厌(玩梗咒语)",
    "不愧是你": "夸/吐槽两用",
    "yue": "呕(恶心)",
    # ── 高频纯数字/单字 ────────────────────────────────────────
    "6": "厉害/狠的省略(夸/讽刺都用)",
    "666": "强(夸)",
    "6666": "强(夸)",
    "+1": "同意/我也是",
    "+10086": "强烈同意",
    "3Q": "Thank you(谢谢)",
    "3q": "Thank you(谢谢)",
    "草": "玩笑用语,通常是『草(笑/无语)』,**不是脏话**",
    "蛤": "啊?(疑问)",
}

# ASCII-only(主要由 [a-z0-9+]+ 组成,需要词边界)和中文/全角(可子串匹配)分两套匹配。
_ASCII_RE = re.compile(r"[a-z0-9+]+")


def _is_ascii_token(term: str) -> bool:
    """判断字典 key 是否纯 ASCII-style(英文/数字/+),需要按 token 边界匹配。"""
    return bool(_ASCII_RE.fullmatch(term))


# 按是否 ASCII 分桶,中文项可直接 in-string 匹配
_ASCII_TERMS = {t: m for t, m in _SLANG.items() if _is_ascii_token(t)}
_CN_TERMS = {t: m for t, m in _SLANG.items() if not _is_ascii_token(t)}


def annotate_slang(text: str) -> list[tuple[str, str]]:
    """扫描 text,返回命中的 (原词, 含义) 列表。同一项不重复返回。

    匹配规则:
    - ASCII 缩写:走 re.findall 拆 token,**完全匹配**字典 key(不模糊)。
    - 中文/全角项:直接 substring 检查。
    """
    if not text or not isinstance(text, str):
        return []
    seen: set[str] = set()
    hits: list[tuple[str, str]] = []
    # ASCII 扫描:把所有英数 token 抽出来,做字典命中
    lower = text.lower()
    for token in _ASCII_RE.findall(lower):
        if token in _ASCII_TERMS and token not in seen:
            seen.add(token)
            hits.append((token, _ASCII_TERMS[token]))
    # 中文扫描:直接 substring
    for term, meaning in _CN_TERMS.items():
        if term in text and term not in seen:
            seen.add(term)
            hits.append((term, meaning))
    return hits


def build_slang_context(text: str, *, max_items: int = 6) -> str:
    """命中黑话时返回 system prompt 文本,无命中返回空串。

    限制 max_items 防止整段消息全是黑话时把 context 撑爆。
    """
    hits = annotate_slang(text)
    if not hits:
        return ""
    truncated = hits[:max_items]
    lines = "; ".join(f"`{term}`={meaning}" for term, meaning in truncated)
    overflow = "" if len(hits) <= max_items else f"(还有 {len(hits) - max_items} 个未列出)"
    return (
        "群友消息里出现的 QQ 网络黑话/缩写翻译(供你理解原文,不要在回复里逐条复读):"
        f"{lines}{overflow}。直接当作群友说了对应中文意思去回应,自然接梗即可。"
    )


def known_terms() -> Iterable[str]:
    """对外暴露字典 key,供测试/调试用。"""
    return _SLANG.keys()
