"""直连 ai.hugou.cc IP 复现 504 诊断,避开 PowerShell 引号转义地狱。

主人在站里测试 gpt-5.5 1.02s 成功,但 catty 这边 gpt-image-2 504(69s)。
这个脚本走和 catty 完全一致的 payload 实测,看是否能复现。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    sys.path.insert(0, r"D:\CattyQQAI\src")
    sys.path.insert(0, r"D:\CattyQQAI")
    os.chdir(r"D:\CattyQQAI")

    from catty_config_loader import load_config_to_env

    load_config_to_env()
    import nonebot
    nonebot.init()
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

    nonebot.get_driver().register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("catty_qq_ai")
    from catty_qq_ai import config as cfg

    import httpx

    url = "http://146.235.196.206:3000/v1/images/generations"
    api_key = cfg.catty_openai_api_key

    # 1) 先测 gpt-5.5 chat (像主人后台测试一样,看是否 1s 内回)
    print("=== Test 1: gpt-5.5 chat 直连 IP ===")
    chat_url = "http://146.235.196.206:3000/v1/chat/completions"
    t0 = time.time()
    try:
        r = httpx.post(
            chat_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "say hi in one word"}], "max_tokens": 32},
            timeout=30.0,
        )
        dt = time.time() - t0
        srv = r.headers.get("server", "-")
        via = r.headers.get("via", "-")
        print(f"  status={r.status_code} elapsed={dt:.2f}s server={srv} via={via}")
        print(f"  body[:300]={r.text[:300]}")
    except Exception as e:
        print(f"  EXC: {type(e).__name__}: {e}")

    print()
    print("=== Test 2: gpt-image-2 短 prompt 直连 IP (POST 和 catty 同一 payload) ===")
    print(f"  URL: {url}")
    print(f"  key prefix: {api_key[:8]}")
    payload = {
        "model": "gpt-image-2",
        "prompt": "a cute white cat on a windowsill",
        "n": 1,
        "size": "1024x1024",
        "quality": "low",
        "output_format": "png",
    }
    t0 = time.time()
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=240.0,
        )
        dt = time.time() - t0
        srv = r.headers.get("server", "-")
        via = r.headers.get("via", "-")
        print(f"  status={r.status_code} elapsed={dt:.2f}s server={srv} via={via}")
        print(f"  body[:400]={r.text[:400]}")
    except Exception as e:
        dt = time.time() - t0
        print(f"  EXC after {dt:.2f}s: {type(e).__name__}: {e}")

    print()
    print("=== Test 3: GET /v1/models 看 gpt-image-2 路由信息 ===")
    try:
        r = httpx.get(
            "http://146.235.196.206:3000/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        if r.status_code == 200:
            data = r.json()
            models = data.get("data") if isinstance(data, dict) else []
            for m in (models or []):
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or "")
                if "image" in mid.lower() or "gpt-5" in mid.lower():
                    print(f"  {m}")
        else:
            print(f"  status={r.status_code} body[:200]={r.text[:200]}")
    except Exception as e:
        print(f"  EXC: {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
