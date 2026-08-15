from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import httpx

from catty_qq_ai.qzone_feed_corpus import (
    feed_entry_to_delta,
    is_approved_liked_feed,
    upsert_feed_delta,
)

logger = logging.getLogger("generate_qzone_scene_delta")


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _load_feeds(path: Path) -> list[Mapping[str, Any]]:
    value = _load_json(path)
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        items = value.get("feeds") or value.get("entries") or value.get("items") or []
    else:
        items = []
    return [item for item in items if isinstance(item, Mapping)]


def _chat_completions_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _clean_reply(value: Any) -> str:
    reply = re.sub(r"\s+", " ", str(value or "")).strip()
    reply = re.sub(r"^```(?:text)?\s*|\s*```$", "", reply, flags=re.IGNORECASE).strip()
    reply = reply.strip("\"'“”‘’")
    reply = re.sub(r"^机机\s*[:：]\s*", "", reply).strip()
    return reply[:60]


def _ai_reply(feed: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    ai = config.get("ai", {})
    if not isinstance(ai, Mapping):
        ai = {}
    base_url = str(ai.get("base_url") or "").strip()
    model = str(ai.get("model") or "").strip()
    api_key = str(ai.get("api_key") or "").strip()
    if not base_url or not model:
        raise ValueError("config ai.base_url and ai.model are required")
    text = re.sub(r"\s+", " ", str(feed.get("text") or "")).strip()
    author_name = str(feed.get("author_name") or "有人").strip() or "有人"
    prompt = (
        "你是机机：偏阳光、爱吃瓜的 QQ 机器人，偶尔带‘杂鱼’和‘喵’口癖，"
        "语气短促自然，不称呼对方为主人。请根据下面的空间动态写一句群聊回复，"
        "只输出回复正文，不要引号、前缀或解释；单句，最多60字。\n"
        f"发布者：{author_name}\n"
        f"动态内容：{text[:1200]}\n"
        f"点赞数：{feed.get('like_count', 0)}"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你负责生成机机的空间动态吃瓜回复。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.8,
        "max_tokens": 120,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.post(_chat_completions_url(base_url), headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    reply = _clean_reply(payload["choices"][0]["message"]["content"])
    if not reply:
        raise ValueError("AI returned an empty reply")
    return reply


def main() -> None:
    parser = argparse.ArgumentParser(description="生成被点赞 Qzone 动态的机机场景 delta")
    parser.add_argument("--store", default=str(_ROOT / "data" / "qzone_feeds.json"))
    parser.add_argument("--config", default=str(_ROOT / "config.json"))
    parser.add_argument("--min-likes", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    selected = []
    for feed in _load_feeds(Path(args.store).expanduser()):
        try:
            likes = int(feed.get("like_count") or 0)
        except (TypeError, ValueError):
            likes = 0
        if likes >= args.min_likes and (not args.approve or is_approved_liked_feed(feed)):
            selected.append(feed)
    if args.limit > 0:
        selected = selected[:args.limit]

    config = _load_json(Path(args.config).expanduser())
    if not isinstance(config, Mapping):
        raise ValueError("config root must be an object")
    for feed in selected:
        try:
            delta = feed_entry_to_delta(feed)
            if delta is None:
                continue
            delta["reply"] = _ai_reply(feed, config)
            if args.approve:
                generated_feed = dict(feed)
                generated_feed["_generated_reply"] = delta["reply"]
                if not upsert_feed_delta(generated_feed):
                    raise RuntimeError("failed to persist generated delta")
            print(json.dumps(delta, ensure_ascii=False))
        except Exception as exc:
            logger.warning("skip feed %s: %s", feed.get("feed_id", ""), exc)


if __name__ == "__main__":
    main()