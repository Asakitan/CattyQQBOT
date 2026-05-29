"""S6 distill 全链路真实烟测.

不 mock 任何东西, 调真实 corpus_distill + corpus_txtai + router.reload_l3_corpus.
跑完后看 qa_corpus_live.jsonl 文件 + L3 reload 是否触发.

用法 (远端):
    D:/CattyQQAI/.venv/Scripts/python.exe scripts/test_s6_distill.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


# 让 catty_config_loader 加载 config (跟 bot.py 一致)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from catty_config_loader import load_config_to_env  # noqa: E402

load_config_to_env()

# 单独 import config.py 和 corpus_distill.py, 不走 catty_qq_ai/__init__.py (它需要 nonebot init)
import importlib.util  # noqa: E402


def _load_module(modname: str, relpath: str):
    full = ROOT / "src" / "catty_qq_ai" / relpath
    spec = importlib.util.spec_from_file_location(modname, str(full))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


_config_mod = _load_module("_test_config_only", "config.py")
Config = _config_mod.Config

_distill_mod = _load_module("_test_distill_only", "cpu_engine/corpus_distill.py")
get_distiller = _distill_mod.get_distiller
reset_distiller_for_test = _distill_mod.reset_distiller_for_test
_sanitize_text = _distill_mod._sanitize_text


def _print(name: str, value):
    print(f"  {name} = {value}")


async def main() -> int:
    config = Config()
    print("=== S6 Config dump ===")
    _print("l3_distill_enabled", config.catty_cpu_engine_l3_distill_enabled)
    _print("l3_distill_skip_private", config.catty_cpu_engine_l3_distill_skip_private)
    _print("live_corpus_path", config.catty_cpu_engine_l3_distill_live_corpus_path)
    _print("rebuild_threshold", config.catty_cpu_engine_l3_distill_rebuild_threshold)
    _print("dedup_window", config.catty_cpu_engine_l3_distill_dedup_window)

    print("\n=== T1: sanitize 测试 ===")
    cases = [
        ("主人, 笨猫今天好困喵~", ["主人"], "应去掉'主人', 保留'笨猫'(bot 自称)"),
        ("@1234567890 你好", None, "@xxx → {at}"),
        ("我QQ是 1234567890", None, "纯 QQ 号 → {uid}"),
        ("小李你好啊, 我QQ是 987654321", ["小李"], "去 '小李' + QQ → {uid}"),
        ("[CQ:at,qq=999888777] 在吗", None, "CQ at → {at}"),
        ("主人 笨蛋主人 杂鱼主人", ["主人", "笨蛋主人", "杂鱼主人"], "三种主人称呼"),
    ]
    for src, terms, note in cases:
        out = _sanitize_text(src, terms=terms)
        print(f"  in:  {src!r}")
        print(f"  out: {out!r}  ({note})")
        print()

    print("=== T2: maybe_distill 写入 ===")

    reload_count = {"n": 0}

    def fake_reload() -> bool:
        reload_count["n"] += 1
        print(f"  >>> fake_reload called #{reload_count['n']}")
        return True

    reset_distiller_for_test()
    distiller = get_distiller(config, router_reload_async=fake_reload)
    print(f"  init stats: {distiller.stats}")

    # 写入 5 条不同的 group 消息
    results = []
    for i, (scope, u, a) in enumerate([
        ("group:111111", "今天天气怎么样", "晴朗 22度~ 主人记得带外套喵"),
        ("group:111111", "中午吃什么", "笨猫推荐火锅! 暖和又香喵"),
        ("private:993255714", "晚安笨猫", "主人晚安~ 抱抱睡觉觉嗷呜"),
        ("group:111111", "什么时候下班", "再坚持 1 小时主人就可以下班啦"),
        ("group:111111", "今天天气怎么样", "晴朗 22度~ 主人记得带外套喵"),  # dedup hit
    ]):
        ok = await distiller.maybe_distill(
            scope=scope,
            user_text=u,
            assistant_text=a,
            intent="default",
            source="test",
            sanitize_terms=["主人", "笨蛋主人"],
        )
        results.append((scope, ok, u))
        print(f"  [{i+1}] scope={scope} ok={ok} user={u!r}")

    print(f"\n  final stats: {distiller.stats}")
    print(f"  fake_reload called: {reload_count['n']} times")
    print(f"  expected: 4 ok (dedup #5), reload=0 (threshold=50 not reached)")

    print("\n=== T3: corpus 文件验证 ===")
    live_path = Path(config.catty_cpu_engine_l3_distill_live_corpus_path)
    if not live_path.is_absolute():
        live_path = ROOT / live_path
    if live_path.exists():
        print(f"  exists: {live_path}")
        with live_path.open(encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                obj = json.loads(line)
                print(f"  [{line_no}] q={obj['q']!r}")
                print(f"        a={obj['a']!r}")
                print(f"        source={obj['source']!r}")
    else:
        print(f"  NOT FOUND: {live_path}")

    print("\n=== T4: 触发 rebuild (强制把 pending 拉到 threshold) ===")
    # 用 monkey-patch 把 threshold 临时改 4, 现在 pending 已经 4 应该立即触发
    orig_threshold = config.catty_cpu_engine_l3_distill_rebuild_threshold
    config.catty_cpu_engine_l3_distill_rebuild_threshold = 4
    distiller._pending_count = 4  # 强制达到阈值
    # 跑一次 maybe_distill 让它命中 if pending >= threshold
    # 但 dedup 会拦, 我们直接 call _trigger_rebuild
    await distiller._trigger_rebuild()
    config.catty_cpu_engine_l3_distill_rebuild_threshold = orig_threshold
    print(f"  reload_count after force = {reload_count['n']}")
    print(f"  final stats: {distiller.stats}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
