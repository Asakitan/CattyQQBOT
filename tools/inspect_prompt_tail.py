#!/usr/bin/env python3
"""Inspect non-cache dynamic tail chunks in saved OpenAI request dumps.

Usage:
  python tools/inspect_prompt_tail.py D:/CattyQQAI/logs/req_dumps
  python tools/inspect_prompt_tail.py D:/CattyQQAI/logs/req_dumps/group_477970838_x.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


_MARKER_RE = re.compile(
    r"(?=(【[^】]+】|<<<CATTY_INTERNAL_INSTRUCTION[^\n]*|\[DYN_SYS\]|\[DYNAMIC_CONTEXT[^\]]*\]|记忆与称呼参数|当前时刻|群节奏感知[^\n]*))"
)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _latest_json(path: Path) -> Path:
    if path.is_file():
        return path
    candidates = sorted(path.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(f"no json dumps under {path}")
    return candidates[-1]


def _chunks(text: str) -> list[tuple[str, str]]:
    matches = list(_MARKER_RE.finditer(text))
    out: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        if not chunk:
            continue
        title = match.group(1)
        out.append((title, chunk))
    return out


def inspect(path: Path) -> None:
    dump = _latest_json(path)
    data = json.loads(dump.read_text(encoding="utf-8"))
    messages = data.get("messages") or []
    print(f"dump={dump}")
    print(f"messages={len(messages)}")
    for idx, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _content_to_text(message.get("content"))
        found = _chunks(text)
        if not found:
            continue
        print(f"MSG {idx} role={role} len={len(text)} chunks={len(found)}")
        for title, chunk in found:
            preview = " ".join(chunk.split())[:180]
            print(f"- {len(chunk):5d} {title} :: {preview}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="request dump json file or directory")
    args = parser.parse_args()
    inspect(args.path)


if __name__ == "__main__":
    main()
