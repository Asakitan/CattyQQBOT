"""主人手动跑这个，验证 pixiv cookie 是否能取到 R-18 搜索结果。

用法：
    python scripts/diagnose_pixiv.py                  # 用 config.json 里的 cookie 测默认关键词
    python scripts/diagnose_pixiv.py 篠ノ目要           # 自定义关键词
    python scripts/diagnose_pixiv.py 篠ノ目要 --proxy http://127.0.0.1:7890

不会把 cookie 上传到外部，只会按照 config.json 里的设置调用 pixiv 自己的 API。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)


def _normalize_cookie(cookie: str) -> str:
    cookie = (cookie or "").strip()
    if not cookie:
        return ""
    if "=" not in cookie:
        return f"PHPSESSID={cookie}"
    return cookie


def _load_config() -> dict:
    root = Path(__file__).resolve().parent.parent
    candidates = [root / "config.json", Path.cwd() / "config.json"]
    for path in candidates:
        if path.is_file():
            with path.open("r", encoding="utf-8-sig") as f:
                return json.load(f)
    print("[!] 找不到 config.json，请先在仓库根目录放好。")
    sys.exit(1)


async def _diag(query: str, proxy: str | None) -> int:
    config = _load_config()
    cookie_raw = (
        config.get("nsfw_search", {}).get("pixiv_cookie")
        or ""
    )
    if not cookie_raw:
        print("[!] config.json -> nsfw_search.pixiv_cookie 是空的，必须先填。")
        return 2
    cookie = _normalize_cookie(cookie_raw)

    enc = quote(query)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.6",
        "Referer": "https://www.pixiv.net/",
        "Cookie": cookie,
    }
    search_url = (
        f"https://www.pixiv.net/ajax/search/illustrations/{enc}"
        f"?word={enc}&mode=r18&p=1&order=date_d&s_mode=s_tag&type=illust"
    )
    print(f"[*] 测试关键词：{query!r}")
    print(f"[*] cookie 字段数：{cookie.count('=')}（通常 1=只有 PHPSESSID，5+ 才接近完整浏览器 cookie）")
    print(f"[*] 调用：{search_url}")

    timeout = config.get("nsfw_search", {}).get("request_timeout") or 15
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=proxy) as client:
        try:
            response = await client.get(search_url, headers=headers)
        except httpx.HTTPError as exc:
            print(f"[x] HTTP 异常：{exc}")
            return 3
        print(f"[*] 状态码：{response.status_code}")
        ctype = response.headers.get("content-type", "")
        print(f"[*] content-type：{ctype}")
        if response.status_code == 403:
            print("[x] 403：cookie 失效 / 没解锁 R-18 / 被 IP 风控")
            return 4
        if response.status_code >= 400:
            print(f"[x] 非预期状态码，返回 body 前 400 字：\n{response.text[:400]}")
            return 5
        try:
            data = response.json()
        except ValueError:
            print(f"[x] 不是 JSON，返回 body 前 400 字：\n{response.text[:400]}")
            return 6
        if isinstance(data, dict) and data.get("error"):
            print(f"[x] pixiv 接口报错：{data.get('message')}")
            return 7
        body = data.get("body") if isinstance(data, dict) else None
        illust = body.get("illust") if isinstance(body, dict) else {}
        works = illust.get("data") if isinstance(illust, dict) else []
        if not isinstance(works, list):
            print("[x] body.illust.data 不是数组")
            return 8
        print(f"[+] 搜到 {len(works)} 个作品")
        if not works:
            print("[!] 列表为空。可能账号设置里没开『查看 R-18 作品』，去 pixiv 设置打开。")
            return 9
        for w in works[:3]:
            print(
                f"    - id={w.get('id')} xRestrict={w.get('xRestrict')} "
                f"title={w.get('title')!r} author={w.get('userName')!r}"
            )
        sample_id = str(works[0].get("id"))
        detail_url = f"https://www.pixiv.net/ajax/illust/{sample_id}"
        print(f"\n[*] 顺便拉一下顶端作品详情：{detail_url}")
        detail_resp = await client.get(detail_url, headers=headers)
        print(f"[*] 详情状态码：{detail_resp.status_code}")
        if detail_resp.status_code >= 400:
            print(f"[x] 详情接口失败，body 前 400 字：\n{detail_resp.text[:400]}")
            return 10
        ddata = detail_resp.json()
        dbody = ddata.get("body") if isinstance(ddata, dict) else None
        if isinstance(dbody, dict):
            print(
                f"[+] 收藏:{dbody.get('bookmarkCount')} 赞:{dbody.get('likeCount')} "
                f"浏览:{dbody.get('viewCount')} xRestrict:{dbody.get('xRestrict')}"
            )
            urls = dbody.get("urls") or {}
            print(f"[+] urls 字段：{list(urls.keys())}")
            print(f"[+] regular 直链：{urls.get('regular')}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="diagnose pixiv R-18 access via local config.json cookie")
    parser.add_argument("query", nargs="?", default="篠ノ目要")
    parser.add_argument("--proxy", default=None, help="HTTP proxy (e.g. http://127.0.0.1:7890)")
    args = parser.parse_args()
    return asyncio.run(_diag(args.query, args.proxy))


if __name__ == "__main__":
    raise SystemExit(main())
