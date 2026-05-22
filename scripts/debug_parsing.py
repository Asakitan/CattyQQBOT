#!/usr/bin/env python
"""Dry-run 所有本地解析层,看一段 incoming 文本会触发哪些 system prompt 注入。

用法:
    python scripts/debug_parsing.py "猫猫帮我看看明天 8 点的活动"
    python scripts/debug_parsing.py --pulse busy "刷屏文本"
    python scripts/debug_parsing.py --layer slang --layer entity "yyds 明天"

不依赖运行中的 bot,直接 import 解析模块。用来:
- 验证某条用户消息会让 AI 看到哪些本地解析提示
- 评估某层 prompt 是不是太长/冗余
- 调试某个新加的解析规则
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = types.ModuleType("catty_qq_ai")
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault("catty_qq_ai", _PACKAGE)


def _load(name: str, fname: str):
    path = _ROOT / "src" / "catty_qq_ai" / fname
    spec = importlib.util.spec_from_file_location(f"catty_qq_ai.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


for _name, _file in (
    ("time_normalizer", "time_normalizer.py"),
    ("time_awareness", "time_awareness.py"),
    ("entity_extractor", "entity_extractor.py"),
    ("intent_classifier", "intent_classifier.py"),
    ("conversation_pulse", "conversation_pulse.py"),
    ("slang_dict", "slang_dict.py"),
    ("topic_classifier", "topic_classifier.py"),
    ("action_hints", "action_hints.py"),
):
    _load(_name, _file)


_sd = sys.modules["catty_qq_ai.slang_dict"]
_cp = sys.modules["catty_qq_ai.conversation_pulse"]
_ic = sys.modules["catty_qq_ai.intent_classifier"]
_ee = sys.modules["catty_qq_ai.entity_extractor"]
_ah = sys.modules["catty_qq_ai.action_hints"]
_ta = sys.modules["catty_qq_ai.time_awareness"]
_tc = sys.modules["catty_qq_ai.topic_classifier"]


@dataclass
class _Msg:
    user_id: str
    text: str
    created_at: float
    is_bot: bool = False
    display_name: str = ""


def main() -> int:
    p = argparse.ArgumentParser(description="Dry-run Catty 本地解析层")
    p.add_argument("text", help="要测试的入向消息文本")
    p.add_argument(
        "--pulse",
        choices=["normal", "cold", "burst", "echo", "busy"],
        default="normal",
        help="模拟群节奏 phase(默认 normal)",
    )
    p.add_argument("--sender-qq", default="999", help="模拟发言者 QQ 号(默认 999)")
    p.add_argument(
        "--layer",
        action="append",
        choices=["time", "slang", "pulse", "intent", "topic", "entity", "hints"],
        help="只显示这些层(可重复);默认显示全部",
    )
    p.add_argument(
        "--reference",
        default="",
        help="time normalize 参考时间(ISO,默认现在)",
    )
    args = p.parse_args()

    layers = set(args.layer) if args.layer else {"time", "slang", "pulse", "intent", "topic", "entity", "hints"}

    if args.reference:
        try:
            ref = datetime.fromisoformat(args.reference)
        except ValueError:
            print(f"[!] --reference 不是合法 ISO datetime: {args.reference!r}", file=sys.stderr)
            return 2
    else:
        ref = datetime.now()

    # 模拟 deque 让 pulse 有数据;phase=normal 时给一个常态聊天,其它根据 phase 造对应数据
    now_mono = 1000.0
    if args.pulse == "cold":
        msgs = [_Msg(user_id="A", text="hi", created_at=now_mono - 600, display_name="A")]
    elif args.pulse == "burst":
        msgs = [_Msg(user_id="X", text=f"刷{i}", created_at=now_mono - 60 + 12 * i, display_name="小狗") for i in range(5)]
    elif args.pulse == "echo":
        msgs = [_Msg(user_id=str(i), text="xs", created_at=now_mono - 50 + 10 * i, display_name=str(i)) for i in range(4)]
    elif args.pulse == "busy":
        msgs = [
            _Msg(user_id=chr(65 + i % 4), text=f"x{i}", created_at=now_mono - 90 + 7 * i, display_name=chr(65 + i % 4))
            for i in range(12)
        ]
    else:
        msgs = [
            _Msg(user_id="A", text="hi", created_at=now_mono - 30, display_name="A"),
            _Msg(user_id="B", text="ok", created_at=now_mono - 20, display_name="B"),
        ]

    total = 0
    print(f"[incoming] {args.text!r}")
    print(f"[pulse]    phase={args.pulse}  sender_qq={args.sender_qq}  reference={ref.isoformat(timespec='minutes')}")
    print(f"[layers]   {sorted(layers)}")
    print()

    if "time" in layers:
        out = _ta.build_time_context(reference=ref)
        _dump("time", out)
        total += len(out)

    if "slang" in layers:
        out = _sd.build_slang_context(args.text)
        _dump("slang", out)
        total += len(out)

    if "pulse" in layers:
        out = _cp.build_pulse_context(msgs, now=now_mono)
        _dump("pulse", out)
        total += len(out)

    if "intent" in layers:
        out = _ic.build_intent_context(args.text)
        _dump("intent", out)
        total += len(out)

    if "topic" in layers:
        out = _tc.build_topic_context(args.text)
        _dump("topic", out)
        total += len(out)

    if "entity" in layers:
        out = _ee.build_entity_context(args.text, reference=ref)
        _dump("entity", out)
        total += len(out)

    if "hints" in layers:
        out = _ah.build_action_hints(
            args.text,
            pulse_phase=args.pulse,
            sender_qq=args.sender_qq,
            reference=ref,
        )
        _dump("hints", out)
        total += len(out)

    print()
    print(f"[TOTAL] {total} chars across {len(layers)} active layers")
    return 0


def _dump(label: str, ctx: str) -> None:
    if not ctx:
        print(f"[{label:7}] (空)")
        return
    print(f"[{label:7}] {len(ctx):4} chars")
    print(f"          {ctx}")


if __name__ == "__main__":
    sys.exit(main())
