"""Bootstrap CPU 引擎 L3 大语料库 (qa_corpus.jsonl) 从聊天历史蒸馏.

用法:
    python scripts/bootstrap_corpus.py \
        --source sessions \
        --out src/catty_qq_ai/data/cpu_engine/corpus/qa_corpus.jsonl \
        --limit 2000

来源: session_cache JSON 文件 (key/messages: [{role: user|assistant, content}, ...]).
配对策略: 相邻 (user, assistant) 抽出, 跳过含 [CQ:image/record/...] 和过长/过短.

主人 2026-05-28 plan-cpu-alicebot-nlu-ai S2.4:
仅采群聊 + 私聊全采 (CLI 开关 --skip-private), 主人后续手工审 / 用 evolution 自动审清洗.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


_RE_CQ_MEDIA = re.compile(r"\[CQ:(image|record|video|file|share|music|forward)")
_RE_CQ_ANY = re.compile(r"\[CQ:[a-z_]+(?:,[^\]]*)?\]")
_RE_BLOCKLIST_PHRASES = re.compile(r"(http[s]?://|www\.)", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default="sessions", help="session_cache 目录 (默认 sessions)")
    p.add_argument(
        "--out",
        default="src/catty_qq_ai/data/cpu_engine/corpus/qa_corpus.jsonl",
        help="输出 jsonl 路径",
    )
    p.add_argument("--limit", type=int, default=2000, help="最多输出 N 条 QA pair")
    p.add_argument("--min-q-length", type=int, default=2)
    p.add_argument("--max-q-length", type=int, default=80)
    p.add_argument("--min-a-length", type=int, default=4)
    p.add_argument("--max-a-length", type=int, default=200)
    p.add_argument(
        "--skip-private",
        action="store_true",
        help="仅采群聊样本 (隐私: 私聊不进通用语料)",
    )
    p.add_argument(
        "--dedup",
        action="store_true",
        default=True,
        help="按 (q,a) 去重 (默认开)",
    )
    p.add_argument("--dry-run", action="store_true", help="只统计不写出")
    return p.parse_args()


def iter_session_files(source_dir: Path) -> Iterable[Path]:
    if not source_dir.exists():
        print(f"!! source dir not found: {source_dir}", file=sys.stderr)
        return
    yield from source_dir.glob("*.json")


def extract_pairs(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for i in range(len(messages) - 1):
        cur = messages[i]
        nxt = messages[i + 1]
        if cur.get("role") != "user" or nxt.get("role") != "assistant":
            continue
        q = _extract_text(cur.get("content"))
        a = _extract_text(nxt.get("content"))
        if not q or not a:
            continue
        pairs.append((q, a))
    return pairs


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
        return " ".join(parts).strip()
    return ""


def is_clean(text: str, *, min_len: int, max_len: int) -> bool:
    if not text:
        return False
    if not (min_len <= len(text) <= max_len):
        return False
    if _RE_CQ_MEDIA.search(text):
        return False
    if _RE_BLOCKLIST_PHRASES.search(text):
        return False
    return True


def normalize_for_dedup(text: str) -> str:
    return _RE_CQ_ANY.sub("", text).strip().lower()


def scope_is_private(session_key: str) -> bool:
    return session_key.lower().startswith("private")


def main() -> int:
    args = parse_args()
    source_dir = Path(args.source)
    out_path = Path(args.out)

    raw_pairs: list[tuple[str, str, str]] = []  # (q, a, source_key)
    files = 0
    sessions_seen = 0
    sessions_skipped_private = 0

    for path in iter_session_files(source_dir):
        files += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"!! skip {path.name}: {exc}", file=sys.stderr)
            continue
        key = data.get("key", "")
        messages = data.get("messages") or []
        if not isinstance(messages, list):
            continue
        sessions_seen += 1
        if args.skip_private and scope_is_private(str(key)):
            sessions_skipped_private += 1
            continue
        for q, a in extract_pairs(messages):
            raw_pairs.append((q, a, str(key)))

    print(
        f"scanned {files} files, {sessions_seen} sessions "
        f"(skipped {sessions_skipped_private} private), "
        f"raw pairs {len(raw_pairs)}"
    )

    seen: set[str] = set()
    kept: list[tuple[str, str, str]] = []
    dropped_clean = 0
    dropped_dedup = 0
    for q, a, src in raw_pairs:
        if not is_clean(q, min_len=args.min_q_length, max_len=args.max_q_length):
            dropped_clean += 1
            continue
        if not is_clean(a, min_len=args.min_a_length, max_len=args.max_a_length):
            dropped_clean += 1
            continue
        if args.dedup:
            sig = normalize_for_dedup(q) + "||" + normalize_for_dedup(a)
            if sig in seen:
                dropped_dedup += 1
                continue
            seen.add(sig)
        kept.append((q, a, src))
        if len(kept) >= args.limit:
            break

    print(
        f"after clean: {len(kept)} kept, "
        f"{dropped_clean} dropped (clean), {dropped_dedup} dropped (dedup)"
    )

    if args.dry_run:
        sample = kept[:5]
        for q, a, src in sample:
            print(f"  Q: {q[:60]}")
            print(f"  A: {a[:60]}")
            print(f"  src: {src}")
            print()
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for q, a, src in kept:
            entry = {
                "q": q,
                "a": a,
                "intent": "default",
                "tags": [],
                "weight": 1.0,
                "source": f"bootstrap:{src}",
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"wrote {len(kept)} entries -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
