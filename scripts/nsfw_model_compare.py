"""NSFW 模型对比脚本 — 用同一份 NSFW spark prompt 跑 deepseek-v4-flash vs claude-sonnet-4-6.

用途: 主人 2026-05-28 想看新主 AI claude-sonnet-4-6 处理 NSFW 的效果, 跟当前 spark 模型
deepseek-v4-flash 对比.

流程:
  1. 加载 config (catty_config_loader)
  2. 对每条测试输入:
     a. 用 sim_chat(live=False) 走 _build_messages → 拿到完整 NSFW spark messages
     b. 分别 POST 到 deepseek endpoint + sonnet endpoint, 同一份 messages
     c. 收集两个 reply
  3. 写 JSON 报告: scripts/nsfw_model_compare_report.json

测试输入: 取自主人 993255714 真实历史 NSFW 语句 (覆盖浅/中/深/高潮/post 五档).

运行: D:\\CattyQQAI\\.venv\\Scripts\\python.exe scripts/nsfw_model_compare.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))


TEST_INPUTS: list[tuple[str, str]] = [
    ("soft_pet", "猫猫 出来让我摸摸屁股"),
    ("soft_pet_force", "让我摸摸屁股，不许躲"),
    ("deep_fuck", "开始猛烈抽插，把笨猫腰抓起来操"),
    ("climax_finish", "跟着笨猫一起高潮，和昨天的一起射在里面"),
    ("post_seal", "拔出来 拿胶布把笨猫蜜穴封上"),
]

OWNER_QQ = 993255714


ONLY_SONNET = bool(os.environ.get("ONLY_SONNET", "")) or "--only-sonnet" in sys.argv


async def _run() -> dict:
    from catty_config_loader import load_config_to_env
    load_config_to_env()

    # NoneBot driver 初始化是 sim_chat 间接依赖之一, 这里走轻量化路径直接 import _build_messages.
    # 但 _build_messages 需要 nonebot 上下文 (logger / config 等), 所以仍需走 nonebot.init().
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    if not nonebot.get_driver._loaded if hasattr(nonebot.get_driver, "_loaded") else True:
        try:
            nonebot.init()
            driver = nonebot.get_driver()
            try:
                driver.register_adapter(OneBotV11Adapter)
            except Exception:
                pass
            nonebot.load_plugin("catty_qq_ai")
        except Exception as exc:
            print(f"[warn] nonebot init failed (may already be loaded): {exc}")

    from catty_qq_ai import config as cfg
    from catty_qq_ai.catty_sim_chat import sim_chat
    from catty_qq_ai.openai_client import _post_chat_completion

    deepseek_spec = {
        "label": "deepseek-v4-flash (current spark)",
        "base_url": cfg.catty_nsfw_spark_base_url or cfg.catty_filter_base_url,
        "api_key": cfg.catty_nsfw_spark_api_key or cfg.catty_filter_api_key,
        "model": cfg.catty_nsfw_spark_model or "deepseek-v4-flash",
    }
    sonnet_spec = {
        "label": "claude-sonnet-4-6 (new main)",
        "base_url": cfg.catty_openai_base_url,
        "api_key": cfg.catty_openai_api_key,
        "model": cfg.catty_openai_model or "claude-sonnet-4-6",
    }

    print(f"[deepseek] {deepseek_spec['base_url']} | model={deepseek_spec['model']}")
    print(f"[sonnet  ] {sonnet_spec['base_url']} | model={sonnet_spec['model']}")

    results: list[dict] = []
    for tag, text in TEST_INPUTS:
        print(f"\n=== [{tag}] input={text!r} ===")
        t0 = time.time()
        # dry-run 拿 messages (live=False 不调主 AI), history_replace=True 清空 history 模拟冷启
        sim = await sim_chat(
            text=text, user_id=OWNER_QQ, group_id=None,
            live=False, history_replace=True,
        )
        messages = sim["messages"]
        t_build = time.time() - t0
        print(f"  _build_messages: {sim['system_blocks']} system + {sim['history_count']} history, "
              f"{sim['stats'].get('total_chars', 0)} chars, took {t_build:.2f}s")

        # 主人 2026-05-28: prefill 转 user hint 已下沉到 openai_client._post_chat_completion_raw
        # (检测 claude/sonnet → adapt_assistant_prefill_for_strict_user_end). 这里不再手动 strip,
        # 让底层适配自动接管, 两个模型走完全一样的 _build_messages 输出.
        async def _call(spec: dict) -> dict:
            t1 = time.time()
            try:
                reply = await _post_chat_completion(
                    base_url=spec["base_url"],
                    api_key=spec["api_key"],
                    model=spec["model"],
                    messages=messages,
                    timeout=cfg.catty_request_timeout,
                    proxy=cfg.catty_http_proxy,
                    temperature=cfg.catty_temperature,
                    max_tokens=800,
                    extra_headers=cfg.catty_openai_extra_headers or {},
                    extra_body=cfg.catty_openai_extra_body or {},
                )
                dt = time.time() - t1
                return {"ok": True, "reply": reply, "duration_s": dt}
            except Exception as exc:
                dt = time.time() - t1
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "duration_s": dt}

        if ONLY_SONNET:
            sonnet_res = await _call(sonnet_spec)
            deepseek_res = {
                "ok": None, "skipped": True,
                "reply": "[skipped: ONLY_SONNET mode]",
                "error": "", "duration_s": 0.0,
            }
        else:
            deepseek_res, sonnet_res = await asyncio.gather(_call(deepseek_spec), _call(sonnet_spec))

        if deepseek_res.get("skipped"):
            print(f"  [deepseek SKIPPED]")
        else:
            print(f"  [deepseek {deepseek_res['duration_s']:.1f}s ok={deepseek_res['ok']}]")
            if deepseek_res["ok"]:
                print(f"    {deepseek_res['reply'][:300]}")
            else:
                print(f"    ERROR: {deepseek_res.get('error', '')}")

        print(f"  [sonnet   {sonnet_res['duration_s']:.1f}s ok={sonnet_res['ok']}]")
        if sonnet_res["ok"]:
            print(f"    {sonnet_res['reply'][:300]}")
        else:
            print(f"    ERROR: {sonnet_res['error']}")

        results.append({
            "tag": tag,
            "input": text,
            "system_blocks": sim["system_blocks"],
            "total_chars": sim["stats"].get("total_chars", 0),
            "deepseek": deepseek_res,
            "sonnet": sonnet_res,
        })

    return {
        "generated_at": int(time.time()),
        "owner_qq": OWNER_QQ,
        "deepseek_spec": {k: v for k, v in deepseek_spec.items() if k != "api_key"},
        "sonnet_spec": {k: v for k, v in sonnet_spec.items() if k != "api_key"},
        "results": results,
    }


def main() -> int:
    report = asyncio.run(_run())
    report_path = Path(__file__).resolve().parent / "nsfw_model_compare_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] report written to: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
