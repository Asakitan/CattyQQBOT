"""A/B 缓存命中报告 — 解析 bot_live.log 的 HIT_TARGET 行 (openai-claude-95 §五).

用法 (远端 D:/CattyQQAI 下):
  python scripts/ab_hit_report.py --scope group:731001 [--since "2026-07-06 19:00"] \
      [--log logs/bot_live.log] [--warm-hist 2]

输出: 按 provider|model 分桶的 warm token 加权命中率 + cold 明细 + 达标判定。
口径: warm = HIT_TARGET 行的 hist >= --warm-hist (默认 2, 与 cache_metrics.warm 一致);
token 加权命中 = Σhit_tok / Σ(hit_tok+miss_tok+create_tok)。
openai 桶里 prompt_tok < 1024 的行单独剔除报告 (OpenAI 隐式缓存最小前缀, 物理不可缓存)。
达标线: 私聊 warm ≥95% / 群聊 warm ≥86% (per 主人 2026-07-06 拍板, warm 会话口径)。
兼容旧格式 HIT_TARGET 行 (无 provider/warm 字段时按 provider=deepseek, warm 用 msgs 缺省 1)。
"""

from __future__ import annotations

import argparse
import glob
import re
from collections import defaultdict

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\S*.*?HIT_TARGET "
    r"model=(?P<model>\S+) this=(?P<this>[\d.]+)% .*?"
    r"hit_tok=(?P<hit>\d+) miss_tok=(?P<miss>\d+)"
    r"(?: create_tok=(?P<create>\d+))?"
    r"(?: prompt_tok=(?P<prompt>\d+))?"
    r"(?: provider=(?P<provider>\S+))?"
    r"(?: msgs=(?P<msgs>\d+))?"
    r"(?: hist=(?P<hist>\d+))?"
    r"(?: warm=(?P<warm>\d))?"
    r" scope=(?P<scope>\S*)",
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", required=True, help="如 group:731001 / private:707701")
    ap.add_argument("--since", default="", help='如 "2026-07-06 19:00" (含)')
    ap.add_argument("--log", default="logs/bot_live.log", help="log 路径, 支持 glob")
    ap.add_argument("--warm-hist", type=int, default=2, help="warm 口径: hist >= N")
    ap.add_argument("--min-prompt-openai", type=int, default=1024)
    args = ap.parse_args()

    rows: list[dict] = []
    for lf in sorted(glob.glob(args.log)) or [args.log]:
        try:
            fh = open(lf, encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"!! 打不开 {lf}: {exc}")
            continue
        with fh:
            for line in fh:
                m = _LINE_RE.search(line)
                if not m:
                    continue
                d = m.groupdict()
                if d["scope"] != args.scope:
                    continue
                if args.since and d["ts"] < args.since:
                    continue
                rows.append({
                    "ts": d["ts"],
                    "model": d["model"],
                    "provider": d["provider"] or "deepseek",
                    "hit": int(d["hit"]),
                    "miss": int(d["miss"]),
                    "create": int(d["create"] or 0),
                    "prompt": int(d["prompt"] or 0)
                    or (int(d["hit"]) + int(d["miss"]) + int(d["create"] or 0)),
                    "hist": int(d["hist"]) if d["hist"] is not None else None,
                    "warm": (
                        int(d["hist"]) >= args.warm_hist
                        if d["hist"] is not None
                        else (d["warm"] == "1" if d["warm"] is not None else True)
                    ),
                })

    if not rows:
        print(f"scope={args.scope} 没有匹配的 HIT_TARGET 行 (since={args.since!r})")
        return

    is_private = args.scope.startswith("private:")
    target = 0.95 if is_private else 0.86
    print(f"scope={args.scope} rows={len(rows)} 口径: warm hist>={args.warm_hist} "
          f"达标线={'私聊95%' if is_private else '群聊86%'}")

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[f"{r['provider']}|{r['model']}"].append(r)

    overall_pass = True
    for key, rs in sorted(buckets.items()):
        provider = key.split("|", 1)[0]
        warm = [r for r in rs if r["warm"]]
        cold = [r for r in rs if not r["warm"]]
        na = []
        if provider == "openai":
            na = [r for r in warm if r["prompt"] < args.min_prompt_openai]
            warm = [r for r in warm if r["prompt"] >= args.min_prompt_openai]
        th = sum(r["hit"] for r in warm)
        tt = sum(r["hit"] + r["miss"] + r["create"] for r in warm)
        rate = th / tt if tt else 0.0
        ok = rate >= target if warm else False
        overall_pass = overall_pass and (ok or not warm)
        print(f"\n== {key} ==")
        print(f"  warm n={len(warm)} token加权命中={rate:.1%} "
              f"{'PASS' if ok else 'FAIL' if warm else 'N/A'} (target {target:.0%})")
        for r in warm:
            this = r["hit"] / (r["hit"] + r["miss"] + r["create"]) if (r["hit"] + r["miss"] + r["create"]) else 0
            print(f"    {r['ts']} this={this:.1%} hit={r['hit']} miss={r['miss']} "
                  f"create={r['create']} prompt={r['prompt']} hist={r['hist']}")
        if cold:
            print(f"  cold n={len(cold)} (冷启/前缀检测滞后, 不计口径):")
            for r in cold:
                this = r["hit"] / (r["hit"] + r["miss"] + r["create"]) if (r["hit"] + r["miss"] + r["create"]) else 0
                print(f"    {r['ts']} this={this:.1%} hist={r['hist']}")
        if na:
            print(f"  N/A n={len(na)} (openai prompt_tok<{args.min_prompt_openai}, 物理不可缓存)")

    print(f"\n>>> 总判定: {'PASS' if overall_pass else 'FAIL'}")


if __name__ == "__main__":
    main()
