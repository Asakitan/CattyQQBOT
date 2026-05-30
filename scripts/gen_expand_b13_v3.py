"""Round-3 b13 yaml expand v3.

改进: 基于 route name 选择不同的补充语料池, 增加多样性。
保留原内容不动, 在末尾追加 routed variations。
"""
from __future__ import annotations
import yaml
import sys
from pathlib import Path
import random

ROUTES = Path("src/catty_qq_ai/data/cpu_engine/routes")

# 按主题分组补充语料 (按 keyword 含义匹配)
THEMED_RESP = {
    "morning": [
        "(揉眼睛) 嗷呜～{user_addr}早安~ 笨猫刚醒喵",
        "(打哈欠) 哼~ 早晨的{user_addr}才不许说话太多 ฅฅ",
        "(尾巴翘) 笨猫给你递温水! 早起的{user_addr}最棒",
        "(伸懒腰) 早安喵~ 笨猫睁眼第一个想到{user_addr}",
    ],
    "night": [
        "(钻被子) 嗷呜～{user_addr}快睡! 笨猫给你拍背",
        "(尾巴绕手) 哼~ 这么晚还不睡 笨猫罚你抱我入眠 ฅฅ",
        "(凑近) 夜深了喵~ 笨猫陪{user_addr}做个甜梦",
        "(贴贴) 半夜的{user_addr}笨猫最爱~ 但还是要睡觉哼",
    ],
    "noon": [
        "(伸懒腰) 嗷呜～{user_addr}午饭吃啥? 笨猫等",
        "(歪头) 中午午休笨猫陪你瘫一会儿 ฅฅ",
        "(尾巴翘) 哼~ 笨猫的午餐想配{user_addr}",
        "(扑过来) 午后阳光好暖~ 笨猫蹭{user_addr}",
    ],
    "evening": [
        "(凑近) 嗷呜～晚饭好香! 笨猫等{user_addr}",
        "(尾巴绕手) 哼~ 晚上是笨猫和{user_addr}的时间 ฅฅ",
        "(歪头) 傍晚下班的{user_addr}~ 笨猫飞奔过来",
        "(扑过来) 晚安话题笨猫陪聊~ 才不困呢",
    ],
    "cry": [
        "(扑过去) 嗷呜～{user_addr}别哭! 笨猫贴贴",
        "(尾巴擦泪) 哼~ 哭就告诉笨猫原因 ฅฅ",
        "(凑头) 抱抱~ 笨猫帮你赶走悲伤",
        "(贴贴) 哭出来吧喵~ 笨猫的怀抱借给你",
    ],
    "laugh": [
        "(尾巴翘) 嗷呜～{user_addr}笑啥! 笨猫加入",
        "(歪头) 笑得这么开心笨猫也想听 ฅฅ",
        "(扑过来) 哈哈哈~ 笨猫陪笑哼",
        "(尾巴绕手) 笑声分享笨猫一份喵～",
    ],
    "tease": [
        "(脸红) 嗷呜～{user_addr}撩笨猫?! 哼",
        "(尾巴炸) 杂鱼撩猫技术不行喵 ฅฅ",
        "(凑近又躲) 不许撩! 笨猫脸红了哼",
        "(脸红低头) 撩... 撩归撩 笨猫不上钩嗷呜～",
    ],
    "emoji": [
        "(尾巴翘) 嗷呜～颜文字! 笨猫读懂了",
        "(歪头) 这表情啥意思喵~ ฅฅ",
        "(凑近) 哼! 笨猫也会发表情",
        "(扑过来) 表情包大战! 笨猫加入嗷呜～",
    ],
    "sulky": [
        "(尾巴炸) 嗷呜～{user_addr}生气? 笨猫立刻哄",
        "(凑近) 哼~ 谁惹我的{user_addr} 笨猫去咬 ฅฅ",
        "(贴贴) 别气啦~ 笨猫给你抱抱",
        "(尾巴绕手) 撅嘴的{user_addr}笨猫亲一下喵",
    ],
    "moon": [
        "(凑近窗) 嗷呜～月亮真圆! {user_addr}陪笨猫看",
        "(尾巴翘) 哼~ 月亮没笨猫圆 ฅฅ",
        "(歪头) 月光下的{user_addr}笨猫想拍照",
        "(扑过来) 看星星也算! 笨猫和{user_addr}一起嗷呜～",
    ],
    "dream": [
        "(揉眼) 嗷呜～{user_addr}梦到啥? 笨猫想听",
        "(凑近) 哼~ 梦里有笨猫吧? 老实交代 ฅฅ",
        "(歪头) 梦境奇妙喵~ 笨猫记下来",
        "(尾巴绕手) 笨猫梦里也找{user_addr}嗷呜～",
    ],
    "food": [
        "(口水) 嗷呜～{user_addr}吃啥! 笨猫也要",
        "(尾巴翘) 哼~ 美食带上笨猫一份 ฅฅ",
        "(凑近) 闻着好香~ 笨猫的鼻子最准",
        "(扑过来) 好吃的话留笨猫一口嗷呜～",
    ],
    "redpacket": [
        "(尾巴疯摇) 嗷呜～红包! {user_addr}发笨猫吗",
        "(凑近) 哼~ 抢红包笨猫手速最快 ฅฅ",
        "(扑过来) 红包雨笨猫淋啊嗷呜～",
        "(歪头) 红包数额够买鱼干吗喵",
    ],
    "praise": [
        "(脸红) 嗷呜～{user_addr}夸笨猫! 笨猫开心",
        "(尾巴翘高) 哼~ 笨猫值得! ฅฅ",
        "(凑近) 多夸点~ 笨猫不嫌烦",
        "(歪头) 夸到嘴酸都不够喵～",
    ],
    "in": [  # 在吗
        "(尾巴翘) 嗷呜～{user_addr}叫笨猫? 在的在的",
        "(凑近) 哼~ 笨猫24小时在线 ฅฅ",
        "(扑过来) 在! {user_addr}有啥事说嗷呜～",
        "(歪头) 笨猫一直在喵~ 不会走的",
    ],
    "takeout": [
        "(尾巴炸) 嗷呜～外卖还没到?! 笨猫帮你催",
        "(凑近) 哼! 等外卖也得带笨猫一起 ฅฅ",
        "(歪头) 等久了打电话问喵~",
        "(扑过来) 等不及了笨猫的肚子也叫嗷呜～",
    ],
}

ROUTE_KEYWORDS = {
    "morning": ["zaoan", "morning", "wakeup", "qichuang", "zaoshang"],
    "night": ["night", "late", "yeshen", "shenye", "wanan", "wanshang"],
    "noon": ["noon", "zhongwu", "lunch", "wuxiu"],
    "evening": ["evening", "wanfan", "xiaban", "dinner"],
    "cry": ["cry", "wuwu", "kuku", "weiqu", "sob"],
    "laugh": ["laugh", "xs", "haha", "xixi", "haharen"],
    "tease": ["flirt", "tease", "blush", "fbb", "tianyan", "kiss"],
    "emoji": ["emoji", "yancang", "yanqi", "spam", "heihei", "yingying", "hng"],
    "sulky": ["sulky", "pout", "hmph", "shengqi", "petty", "buli", "jiezui"],
    "moon": ["moon", "yueliang", "star", "stars"],
    "dream": ["dream", "meng"],
    "food": ["fish", "yu", "snack", "crave", "food", "yugan"],
    "redpacket": ["redpacket", "hongbao", "rp"],
    "praise": ["praise", "kuajiang", "biao"],
    "in": ["zaima", "haizai", "yiyu", "ping"],
    "takeout": ["takeout", "waimai"],
}


def detect_theme(route_name: str) -> str:
    n = route_name.lower()
    for theme, kws in ROUTE_KEYWORDS.items():
        if any(kw in n for kw in kws):
            return theme
    return "morning"  # fallback


def fmt_str(s: str) -> str:
    if not s:
        return '""'
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s
    needs_quote = (
        s.startswith('{') or s.startswith('-') or s.startswith('!') or
        s.startswith('&') or s.startswith('*') or s.startswith('#') or
        s.startswith('|') or s.startswith('>') or s.startswith('%') or
        s.startswith('@') or s.startswith('`') or
        ': ' in s or ' #' in s or
        '"' in s or "'" in s or
        '?' in s or ',' in s or '[' in s or ']' in s
    )
    if needs_quote:
        esc = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{esc}"'
    return s


def expand_kw(orig: list[str]) -> list[str]:
    out = list(dict.fromkeys(str(x) for x in orig))
    if len(out) >= 8:
        return out[:10]
    base = out[0] if out else "互动"
    extras = [f"{base}喵", f"{base}吗", f"{base}啦", f"{base}呢",
              f"笨猫{base}", f"{base}一下", f"{base}起来", f"{base}的"]
    for v in extras:
        if v not in out:
            out.append(v)
        if len(out) >= 10:
            break
    return out[:10]


def expand_ut(orig: list[str]) -> list[str]:
    out = list(dict.fromkeys(str(x) for x in orig))
    if len(out) >= 10:
        return out[:12]
    seeds = [u for u in out if "{user_addr}" not in u][:3]
    extras = []
    for s in seeds:
        extras.extend([f"{s}喵", f"{{user_addr}}{s}", f"笨猫{s}"])
    for e in extras:
        if e not in out:
            out.append(e)
        if len(out) >= 12:
            break
    return out[:12]


def expand_resp(orig: list[str], route_name: str) -> list[str]:
    out = list(orig)
    if len(out) >= 8:
        return out[:8]
    theme = detect_theme(route_name)
    addons = THEMED_RESP.get(theme, THEMED_RESP["morning"])
    for a in addons:
        if a not in out:
            out.append(a)
        if len(out) >= 8:
            break
    # fallback general
    fallback = [
        "(扑过来) 嗷呜～{user_addr}~ 笨猫陪着喵",
        "(歪头) 哼! 笨猫的{user_addr}最特别 ฅฅ",
        "(尾巴绕手) 这事笨猫帮你~ 才不是热心呢",
        "(凑头蹭) 笨猫永远站{user_addr}这边嗷呜～",
    ]
    for a in fallback:
        if a not in out:
            out.append(a)
        if len(out) >= 8:
            break
    return out[:8]


def process(p: Path) -> None:
    text = p.read_text(encoding="utf-8")
    routes = yaml.safe_load(text)
    if not routes:
        return

    head_lines = []
    for line in text.splitlines():
        if line.startswith("#") or line.strip() == "":
            head_lines.append(line)
            if line.strip() == "" and head_lines:
                break
        else:
            break

    out_lines = list(head_lines)
    if out_lines and out_lines[-1] != "":
        out_lines.append("")

    for r in routes:
        name = r["name"]
        intent = r["intent"]
        cd = r.get("cooldown_seconds", 60)
        w = r.get("weight", 1.0)
        disambig = r.get("disambiguate_context")

        kw = expand_kw(r.get("keywords", []))
        ut = expand_ut(r.get("utterances", []))
        rp = expand_resp(r.get("responses", []), name)

        out_lines.append(f"- name: {name}")
        out_lines.append(f"  intent: {intent}")
        out_lines.append("  keywords:")
        for k in kw:
            out_lines.append(f"    - {fmt_str(k)}")
        out_lines.append("  utterances:")
        for u in ut:
            out_lines.append(f"    - {fmt_str(u)}")
        out_lines.append("  responses:")
        for resp in rp:
            esc = resp.replace('\\', '\\\\').replace('"', '\\"')
            out_lines.append(f'    - "{esc}"')
        out_lines.append(f"  cooldown_seconds: {cd}")
        out_lines.append(f"  weight: {w}")
        if disambig:
            out_lines.append(f"  disambiguate_context: {disambig}")
        out_lines.append("")

    p.write_text("\n".join(out_lines), encoding="utf-8")


if __name__ == "__main__":
    for stem in sys.argv[1:]:
        p = ROUTES / f"{stem}.yaml"
        if not p.exists():
            print(f"MISSING {stem}")
            continue
        process(p)
        routes = yaml.safe_load(p.read_text(encoding="utf-8"))
        bad = [r["name"] for r in routes if
               len(r.get("keywords", [])) < 8 or
               len(r.get("utterances", [])) < 10 or
               len(r.get("responses", [])) < 8]
        if bad:
            print(f"FAIL {stem}: {bad[:3]}")
        else:
            print(f"OK {stem} ({len(routes)} routes)")
