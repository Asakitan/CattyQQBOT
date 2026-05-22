#!/usr/bin/env python
"""分析 bot_live.log 里所有真实 user 消息,跑解析层 + n-gram 频次找词典 gap。

用法:
    python scripts/lex_audit.py                       # 默认看最近 12000 行
    python scripts/lex_audit.py --tail 30000          # 看更多
    python scripts/lex_audit.py --no-hit-only         # 只列 no-hit 短消息

输出:
- 整体命中率(slang/intent/topic/entity/no-hit)
- 各层 top 命中项
- 未收录的高频 n-gram(候选补词)
- no-hit 短消息样本(供人工挑标)

给运维 / 开发者用,定期跑发现哪些真实消息没被本地解析覆盖,需要补词典。
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import types
from collections import Counter
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = types.ModuleType("catty_qq_ai")
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault("catty_qq_ai", _PACKAGE)


def _load(name: str, fname: str):
    p = _ROOT / "src" / "catty_qq_ai" / fname
    spec = importlib.util.spec_from_file_location(f"catty_qq_ai.{name}", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


for _name, _file in (
    ("time_normalizer", "time_normalizer.py"),
    ("entity_extractor", "entity_extractor.py"),
    ("intent_classifier", "intent_classifier.py"),
    ("topic_classifier", "topic_classifier.py"),
    ("slang_dict", "slang_dict.py"),
):
    _load(_name, _file)


_sd = sys.modules["catty_qq_ai.slang_dict"]
_ic = sys.modules["catty_qq_ai.intent_classifier"]
_tc = sys.modules["catty_qq_ai.topic_classifier"]
_ee = sys.modules["catty_qq_ai.entity_extractor"]

_HANDLE_RE = re.compile(r"handle_event:538.*message\.group")
_TEXT_RE = re.compile(r"\] '(.+)'$")
_CQ_PREFIXES = ("[image:", "[at:", "[reply:", "[markdown:", "[face:", "[json:")


def _extract_user_texts(log_path: Path, tail: int) -> list[str]:
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if tail and len(lines) > tail:
        lines = lines[-tail:]
    texts: list[str] = []
    for line in lines:
        if not _HANDLE_RE.search(line):
            continue
        m = _TEXT_RE.search(line)
        if not m:
            continue
        txt = m.group(1)
        if any(txt.startswith(p) for p in _CQ_PREFIXES):
            continue
        if len(txt) < 2:
            continue
        texts.append(txt)
    return texts


def _covered_keywords() -> set[str]:
    covered: set[str] = set()
    for kws in _tc._TOPICS.values():
        for kw in kws:
            covered.add(kw.lower())
    for kw in _sd.known_terms():
        covered.add(kw.lower())
    return covered


def main() -> int:
    p = argparse.ArgumentParser(description="Catty 词典 audit:基于真实 user 消息找 gap")
    p.add_argument("--log", default="logs/bot_live.log", help="bot_live.log 路径(相对 cwd)")
    p.add_argument("--tail", type=int, default=12000, help="只看最后 N 行(默认 12000)")
    p.add_argument("--no-hit-only", action="store_true", help="只输出 no-hit 短消息")
    p.add_argument("--ngram-limit", type=int, default=30, help="未收录 n-gram 候选 top N")
    args = p.parse_args()

    log_path = Path(args.log).resolve()
    texts = _extract_user_texts(log_path, args.tail)
    print(f"Audit root: {log_path}")
    print(f"Extracted {len(texts)} user-group messages from last {args.tail} lines")
    print()

    if not texts:
        print("No user messages found")
        return 1

    hit_count = Counter()
    intent_hits = Counter()
    topic_hits = Counter()
    slang_hits = Counter()
    entity_hits = Counter()
    no_hit_short: list[str] = []
    no_hit_long: list[str] = []

    for t in texts:
        sl = [w for w, _ in _sd.annotate_slang(t)]
        it = _ic.classify_intent(t)
        tp = _tc.classify_topic(t)
        ents = _ee.extract_entities(t)
        any_hit = bool(sl or it or tp or ents)
        if sl: hit_count["slang"] += 1
        if it: hit_count["intent"] += 1
        if tp: hit_count["topic"] += 1
        if ents: hit_count["entity"] += 1
        if not any_hit:
            hit_count["no_hit"] += 1
            if 2 <= len(t) <= 30:
                no_hit_short.append(t)
            else:
                no_hit_long.append(t)
        intent_hits.update(it)
        topic_hits.update(tp)
        slang_hits.update(sl)
        for e in ents: entity_hits[e.kind] += 1

    total = len(texts)
    print(f"=== Hit stats over {total} messages ===")
    for layer in ("intent", "topic", "entity", "slang", "no_hit"):
        n = hit_count.get(layer, 0)
        print(f"  {layer:8} {n:5} ({n*100/total:.1f}%)")

    if args.no_hit_only:
        print(f"\n=== No-hit short messages (2-30 chars, top 40) ===")
        for t in no_hit_short[:40]:
            print(f"  {t!r}")
        return 0

    print(f"\n=== Top hits per layer ===")
    print(f"  intent: {dict(intent_hits.most_common(8))}")
    print(f"  topic:  {dict(topic_hits.most_common(8))}")
    print(f"  slang:  {dict(slang_hits.most_common(8))}")
    print(f"  entity: {dict(entity_hits.most_common(5))}")

    # n-gram 频次(2/3 字),只显示未收录
    print(f"\n=== Top {args.ngram_limit} 未收录 2-3 字 n-gram(候选补词) ===")
    covered = _covered_keywords()
    ngrams: Counter = Counter()
    for t in texts:
        t_clean = re.sub(r"\[[^\]]+\]", "", t)
        chars = re.sub(r"[\s\W_]+", "", t_clean)
        for n in (2, 3):
            for i in range(len(chars) - n + 1):
                g = chars[i:i + n]
                if all(c.isascii() for c in g):
                    continue
                ngrams[g] += 1
    uncovered_top = [(g, c) for g, c in ngrams.most_common(200) if g.lower() not in covered]
    for g, c in uncovered_top[: args.ngram_limit]:
        print(f"  {g} ({c})")
    print(f"\n注意:大多数高频 n-gram 是通用语法词(『不是』『什么』『怎么』),不要盲目加进词典,")
    print(f"     只挑那些**强领域信号**的(具体游戏名/作品名/技术品牌/特定梗)再补。")

    print(f"\n=== No-hit 短消息样本(可能值得人工挑标的)(top 20) ===")
    for t in no_hit_short[:20]:
        print(f"  {t!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
