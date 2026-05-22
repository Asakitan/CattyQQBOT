#!/usr/bin/env python
"""query logs/conversation_feed.jsonl by parsing-layer labels — 看 bot 在特定场景下的表现。

依赖前提:conversation_feed.user entries 的 ``extra.parsing`` 字段
(由 __init__.py 的 _summarize_text_parsing_for_feed 写入)。
历史 entry(在该字段被加入之前)没有 parsing,会被自动跳过。

用法:
    python scripts/feed_query.py --intent tease_cat --hours 24
    python scripts/feed_query.py --topic finance --limit 5
    python scripts/feed_query.py --slang yyds
    python scripts/feed_query.py --intent question --topic tech --hours 48

输出 user 消息 + 之后 60s 内的 assistant 回复(成对显示)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _load_entries(feed_path: Path):
    if not feed_path.is_file():
        print(f"⚠ feed file not found: {feed_path}", file=sys.stderr)
        return []
    out = []
    with feed_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _matches(entry: dict, *, intent: str, topic: str, slang: str, entity_kind: str) -> bool:
    if entry.get("kind") != "user":
        return False
    parsing = (entry.get("extra") or {}).get("parsing") or {}
    if intent and intent not in parsing.get("intent", []):
        return False
    if topic and topic not in parsing.get("topic", []):
        return False
    if slang and slang not in parsing.get("slang", []):
        return False
    if entity_kind:
        ents = parsing.get("entities") or []
        if not any(e.get("k") == entity_kind for e in ents):
            return False
    return True


def _find_paired_reply(entries: list, user_idx: int, *, scope: str, max_seconds: float = 60.0) -> dict | None:
    """找该 user entry 之后 max_seconds 内,同 scope 的第一条 assistant 回复。"""
    user_ts = entries[user_idx].get("ts", 0)
    for j in range(user_idx + 1, len(entries)):
        ent = entries[j]
        if ent.get("kind") != "assistant":
            continue
        if ent.get("scope") != scope:
            continue
        dt = ent.get("ts", 0) - user_ts
        if dt < 0:
            continue
        if dt > max_seconds:
            return None
        return ent
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="Query Catty conversation_feed by parsing labels")
    p.add_argument("--feed", default="logs/conversation_feed.jsonl", help="feed jsonl 路径")
    p.add_argument("--hours", type=float, default=24.0, help="只看过去 N 小时(默认 24)")
    p.add_argument("--intent", default="", help="intent 标签过滤,如 tease_cat/command_to_cat/question")
    p.add_argument("--topic", default="", help="topic 标签过滤,如 gaming/finance/health")
    p.add_argument("--slang", default="", help="slang 命中过滤,如 yyds/awsl/xs")
    p.add_argument("--entity", default="", help="entity kind 过滤,如 time/money/url/qq_id")
    p.add_argument("--limit", type=int, default=20, help="最多显示多少对(默认 20)")
    p.add_argument("--show-no-reply", action="store_true", help="也显示没有 assistant 回复的 user")
    args = p.parse_args()

    if not (args.intent or args.topic or args.slang or args.entity):
        print("⚠ 至少传一个过滤条件: --intent/--topic/--slang/--entity", file=sys.stderr)
        return 2

    feed_path = Path(args.feed).resolve()
    entries = _load_entries(feed_path)
    if not entries:
        print(f"没有可读 entry from {feed_path}", file=sys.stderr)
        return 1

    cutoff = time.time() - args.hours * 3600
    pairs: list[tuple[dict, dict | None]] = []
    for i, ent in enumerate(entries):
        if ent.get("ts", 0) < cutoff:
            continue
        if not _matches(ent, intent=args.intent, topic=args.topic, slang=args.slang, entity_kind=args.entity):
            continue
        reply = _find_paired_reply(entries, i, scope=ent.get("scope", ""))
        if reply is None and not args.show_no_reply:
            continue
        pairs.append((ent, reply))
        if len(pairs) >= args.limit:
            break

    if not pairs:
        print(f"过去 {args.hours}h 没有匹配条目")
        print(f"  filters: intent={args.intent!r} topic={args.topic!r} slang={args.slang!r} entity={args.entity!r}")
        return 0

    print(f"=== 找到 {len(pairs)} 对匹配(过去 {args.hours}h) ===\n")
    for i, (user, reply) in enumerate(pairs, start=1):
        scope = user.get("scope", "")
        sender = user.get("sender_name") or user.get("sender_id", "")
        utext = (user.get("text") or "").replace("\n", " | ")
        parsing = (user.get("extra") or {}).get("parsing") or {}
        tags_part = []
        for key in ("intent", "topic", "slang"):
            vals = parsing.get(key) or []
            if vals:
                tags_part.append(f"{key}={','.join(vals[:3])}")
        ents = parsing.get("entities") or []
        if ents:
            tags_part.append(
                "entity=" + ",".join(
                    f"{e.get('k')}({e.get('r','')[:15]})" for e in ents[:3]
                )
            )
        tags_str = "; ".join(tags_part) if tags_part else "(no parsing meta)"
        print(f"[{i}] [{scope}] {sender}: {utext[:200]}")
        print(f"    tags: {tags_str}")
        if reply:
            rtext = (reply.get("text") or "").replace("\n", " | ")
            print(f"    猫: {rtext[:200]}")
        else:
            print(f"    猫: (无回复 / 60s 内未找到)")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
