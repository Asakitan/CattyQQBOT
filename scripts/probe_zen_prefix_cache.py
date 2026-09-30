"""Wave ④ 探针: 实测 zen 通道前缀缓存回传 (2026-08-25)。

双发同前缀请求: 第一轮写缓存, 第二轮只变尾部 user, 对比 usage 里
cached_tokens / prompt_cache_hit_tokens 是否回传、命中比例多少。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ai = cfg["ai"]
BASE_URL = ai["base_url"]
API_KEY = ai["api_key"]
MODEL = ai["model"]

# ~2K token 稳定前缀 (模拟场景库常驻段)
LONG_PREFIX = ("【机机场景母本探针】群友: 在吗 / 机机: 在 / 群友: 播吗 / 机机: 不知道啊 / " * 120)

def call(user_text: str) -> dict:
    body = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 32,
        "messages": [
            {"role": "system", "content": LONG_PREFIX},
            {"role": "user", "content": user_text},
        ],
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    with httpx.Client(timeout=120) as client:
        resp = client.post(BASE_URL, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    data["_elapsed"] = round(time.perf_counter() - t0, 2)
    return data


def main() -> int:
    print(f"endpoint={BASE_URL} model={MODEL}")
    r1 = call("探针第一轮, 回一个字: 好")
    u1 = r1.get("usage") or {}
    print(f"R1 elapsed={r1['_elapsed']}s usage={json.dumps(u1, ensure_ascii=False)}")
    time.sleep(2)
    r2 = call("探针第二轮换个尾巴, 回一个字: 嗯")
    u2 = r2.get("usage") or {}
    print(f"R2 elapsed={r2['_elapsed']}s usage={json.dumps(u2, ensure_ascii=False)}")
    hit = (u2.get("prompt_tokens_details") or {}).get("cached_tokens") or u2.get("prompt_cache_hit_tokens")
    total = u2.get("prompt_tokens") or 0
    if hit:
        print(f"RESULT: cache HIT {hit}/{total} = {hit / max(1, total):.1%}")
    else:
        print("RESULT: usage 里没有 cached_tokens / prompt_cache_hit_tokens 字段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
