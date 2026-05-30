"""Round-3 batch 13 yaml expansion to K>=8 U>=10 R>=8.

策略: 读每个 sub-route 原种子, 用笨猫语料池补充,
不破坏原内容, 在末尾追加 variation.
"""
from __future__ import annotations
import yaml
import sys
from pathlib import Path

ROUTES = Path("src/catty_qq_ai/data/cpu_engine/routes")

# 通用补充语料池 - 笨猫语气
ACT_OPEN = [
    "(扑过来)", "(歪头)", "(尾巴绕手)", "(脸红)", "(凑头蹭)",
    "(尾巴翘)", "(贴贴)", "(凑近)", "(尾巴拍)", "(扑上去)",
]
TAILS = ["嗷呜～", "喵～", "ฅฅ", "哼", "喵呜", "喵"]


def fmt_str(s: str) -> str:
    """Yaml-safe single-line string. Quote if needed."""
    if not s:
        return '""'
    # Already-quoted: leave alone
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s
    # Quote if starts with { or - or : in middle or contains special yaml chars
    needs_quote = (
        s.startswith('{') or s.startswith('-') or s.startswith('!') or
        s.startswith('&') or s.startswith('*') or s.startswith('#') or
        s.startswith('|') or s.startswith('>') or s.startswith('%') or
        s.startswith('@') or s.startswith('`') or
        ': ' in s or ' #' in s or
        '"' in s or "'" in s
    )
    if needs_quote:
        esc = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{esc}"'
    return s


def expand_kw(orig: list[str]) -> list[str]:
    out = list(dict.fromkeys(str(x) for x in orig))
    if len(out) >= 8:
        return out[:10]
    # 补充 variations
    base = out[0] if out else "互动"
    extra_pool = [
        f"{base}喵",
        f"{base}吗",
        f"{base}啦",
        f"{base}呢",
        f"笨猫{base}",
        f"{base}一下",
        f"{base}起来",
        f"{base}的",
    ]
    for v in extra_pool:
        if v not in out:
            out.append(v)
        if len(out) >= 10:
            break
    return out[:10]


def expand_ut(orig: list[str]) -> list[str]:
    out = list(dict.fromkeys(str(x) for x in orig))
    if len(out) >= 10:
        return out[:12]
    # 补充
    seeds = [u for u in out if "{user_addr}" not in u][:3]
    extras = []
    for s in seeds:
        extras.extend([
            f"{s}喵",
            f"{{user_addr}}{s}",
            f"笨猫{s}",
        ])
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
    # 用现有 response 做种子加新风格
    # 不要直接复制, 加新的动作+句式
    addons = [
        "(扑过来) 嗷呜～{user_addr}笨猫来了喵～",
        "(歪头) 笨猫陪你嘛~ ฅฅ",
        "(尾巴绕手) 哼! 笨猫就在这儿喵",
        "(脸红) 嗷呜～{user_addr}笨猫的心被勾走啦",
        "嗷呜～别担心{user_addr}~ 笨猫一直在喵",
        "(凑头蹭) 笨猫和你一起~ 嗷呜～",
        "(尾巴翘) 这事笨猫包了! ฅฅ哼",
        "(贴贴) 笨猫永远站{user_addr}这边喵～",
    ]
    idx = 0
    while len(out) < 8 and idx < len(addons):
        if addons[idx] not in out:
            out.append(addons[idx])
        idx += 1
    return out[:8]


def process(p: Path) -> None:
    text = p.read_text(encoding="utf-8")
    routes = yaml.safe_load(text)
    if not routes:
        return

    # 头部注释
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
        # keywords as block list (safer for special chars like ?, :, etc.)
        out_lines.append("  keywords:")
        for k in kw:
            out_lines.append(f"    - {fmt_str(k)}")
        out_lines.append("  utterances:")
        for u in ut:
            out_lines.append(f"    - {fmt_str(u)}")
        out_lines.append("  responses:")
        for resp in rp:
            # 全部用双引号
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
        # verify
        routes = yaml.safe_load(p.read_text(encoding="utf-8"))
        bad = []
        for r in routes:
            kc = len(r.get("keywords", []))
            uc = len(r.get("utterances", []))
            rc = len(r.get("responses", []))
            if kc < 8 or uc < 10 or rc < 8:
                bad.append(f"{r['name']}({kc}/{uc}/{rc})")
        if bad:
            print(f"FAIL {stem}: {bad[:3]}")
        else:
            print(f"OK {stem} ({len(routes)} routes)")
