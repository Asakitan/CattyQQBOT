from __future__ import annotations
import argparse
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path: sys.path.insert(0, str(SRC))
if "catty_qq_ai" not in sys.modules:
    package = types.ModuleType("catty_qq_ai")
    package.__path__ = [str(SRC / "catty_qq_ai")]
    sys.modules["catty_qq_ai"] = package
from catty_qq_ai.fadianji_harness import build_fadianji_evidence_packet

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tune fadianji retrieval/evidence locally.")
    parser.add_argument("text_positional", nargs="?")
    parser.add_argument("--text", default="")
    parser.add_argument("--scope", default="group:922298923")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--max-chars", type=int, default=2800)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    text = str(args.text or args.text_positional or "").strip()
    if not text: parser.error("missing query text; use --text TEXT")
    packet = build_fadianji_evidence_packet(text, persona="fadianji", scope_key=args.scope, is_private=args.private, max_chars=max(0, args.max_chars), semantic=True)
    if args.as_json:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
    else:
        print(packet["text"]); print("\n【retrieval summary】")
        for item in packet["scene_matches"]:
            print(f"- {item['score']:.3f} [{item['source_scope']}/{item['category']}] {item['trigger']} -> {item['reply']}")
    return 0

if __name__ == "__main__": raise SystemExit(main())