from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import re
from pathlib import Path
from typing import Any


_SPLIT_RE = re.compile(
    r"(?m)(?=^【|^<<<CATTY_INTERNAL_INSTRUCTION|^<<<END_INTERNAL|^当前时刻|^群节奏感知|^记忆与称呼|^特别关心触发|^必须回复场景|^疑似直接喊你)"
)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")


def _load_messages(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    messages = data.get("messages") or data.get("body", {}).get("messages") or []
    return [m for m in messages if isinstance(m, dict)]


def _heading(text: str) -> str:
    one_line = text.replace("\r", " ").replace("\n", " ")
    if one_line.startswith("【") and "】" in one_line:
        return one_line.split("】", 1)[0] + "】"
    if one_line.startswith("<<<"):
        return one_line[:100]
    if "[DYN_SYS]" in one_line:
        return "USER_WITH_DYN_SYS"
    return one_line[:100]


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _text_of(message)
    return ""


def _dyn_parts(user_text: str) -> tuple[str, list[str]]:
    dyn = user_text.split("[DYN_SYS]", 1)[1] if "[DYN_SYS]" in user_text else ""
    parts = [p.strip() for p in _SPLIT_RE.split(dyn) if p.strip()]
    return dyn, parts


def _scope_name(path: Path) -> str:
    return path.name.rsplit("_", 1)[0]


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[idx]


def _print_summary(paths: list[Path]) -> None:
    by_heading: dict[str, list[int]] = defaultdict(list)
    dyn_lengths: list[int] = []
    for path in paths:
        messages = _load_messages(path)
        dyn, parts = _dyn_parts(_last_user_text(messages))
        dyn_lengths.append(len(dyn))
        for part in parts:
            by_heading[_heading(part)].append(len(part))
    if not paths:
        return
    print(
        "\n## summary "
        f"files={len(paths)} dyn_median={_percentile(dyn_lengths, 0.50)} "
        f"dyn_p95={_percentile(dyn_lengths, 0.95)} dyn_max={max(dyn_lengths or [0])}"
    )
    rows = sorted(
        ((max(lengths), _percentile(lengths, 0.95), _percentile(lengths, 0.50), len(lengths), heading)
         for heading, lengths in by_heading.items()),
        reverse=True,
    )
    for max_len, p95, median, count, heading in rows[:20]:
        print(f"  max={max_len:4d} p95={p95:4d} med={median:4d} n={count:3d} {heading}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose Catty request dump cache tail.")
    parser.add_argument("root", nargs="?", default="logs/req_dumps", help="Directory containing req_dumps json files")
    parser.add_argument("--scope", action="append", default=[], help="Scope prefix, e.g. private_993255714")
    parser.add_argument("--latest", type=int, default=4, help="Latest dumps per scope to print")
    parser.add_argument("--summary", action="store_true", help="Print aggregate dynamic chunk length summary for selected dumps")
    args = parser.parse_args()

    root = Path(args.root)
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime)
    scopes = args.scope or []
    if not scopes:
        seen: set[str] = set()
        for path in reversed(files):
            scope = _scope_name(path)
            if scope.startswith("noscope") or scope in seen:
                continue
            seen.add(scope)
            scopes.append(scope)
            if len(scopes) >= 5:
                break

    for scope in scopes:
        selected = sorted(root.glob(scope + "_*.json"), key=lambda p: p.stat().st_mtime)[-args.latest :]
        if not selected:
            continue
        if args.summary:
            print(f"\n### {scope}")
            _print_summary(selected)
            continue
        print(f"\n### {scope}")
        for path in selected:
            messages = _load_messages(path)
            last_user = _last_user_text(messages)
            dyn, _ = _dyn_parts(last_user)
            prefix = " ".join(
                f"{i}{str(m.get('role') or '?')[:1]}{len(_text_of(m))}#{_md5(_text_of(m))}"
                for i, m in enumerate(messages[:8])
            )
            print(f"{path.name} msgs={len(messages)} last={len(last_user)} dyn={len(dyn)} prefix={prefix}")

        messages = _load_messages(selected[-1])
        dyn, parts = _dyn_parts(_last_user_text(messages))
        print("latest_dyn_parts:")
        for part in parts:
            print(f"  {len(part):4d} {_md5(part)} {_heading(part)}")


if __name__ == "__main__":
    main()
