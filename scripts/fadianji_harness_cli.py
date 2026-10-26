from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if "catty_qq_ai" not in sys.modules:
    package = types.ModuleType("catty_qq_ai")
    package.__path__ = [str(SRC / "catty_qq_ai")]
    sys.modules["catty_qq_ai"] = package
from catty_qq_ai.fadianji_harness import build_fadianji_evidence_packet


# Preserve the original compact text field, while excluding new raw payloads.
_COMPACT_KEYS = (
    "text", "schema_version", "scope", "is_private", "flags", "counts", "source_status",
    "diagnostics", "truncated", "rendered_counts", "omitted_count", "omitted_counts",
    "rendered_ids", "omitted_ids", "duplicate_count", "max_chars", "rendered_chars",
)


def _nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负整数") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return number


def _scope_arg(value: str) -> str:
    scope = value.strip()
    if not re.fullmatch(r"(?:group|private):[0-9A-Za-z_-]+", scope):
        raise argparse.ArgumentTypeError("scope 格式应为 group:ID 或 private:ID，例如 group:local")
    return scope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "本地预览机机场景检索与证据格式，不启动 bot、不调用远程模型。"
            "可能读取本机挖掘语料、角色/场景语料及匹配 scope 的语料画像，"
            "未注入 live stores（运行时记忆、RAG、空间动态），"
            "无 store 的预览不代表真实记忆，也不等同于完整生产上下文。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            '示例：python scripts/fadianji_harness_cli.py --text "测试问题" --compact-json；'
            '私聊预览加 --private，或显式传 --scope private:local。'
            "本命令预览默认 scene-k=3、book-k=3、max-chars=2800；"
            "--production-defaults 仅把未指定项改为 12/0/12000，"
            "只对齐设置预算，不注入真实记忆或运行时 stores。"
        ),
    )
    parser.add_argument("text_positional", nargs="?", help="查询文本，也可使用 --text")
    parser.add_argument("--text", default="", help="查询文本；提供时优先于位置参数")
    parser.add_argument("--scope", type=_scope_arg, help="检索 scope；未指定时为 group:local，私聊为 private:local")
    parser.add_argument("--private", action="store_true", help="使用私聊 scope；不能与显式 group:ID 同时使用")
    parser.add_argument("--production-defaults", action="store_true", help="仅对齐未指定项的生产设置预算 12/0/12000，不加载运行时 stores")
    parser.add_argument("--scene-k", type=_nonnegative_int, default=None, help="最多检索场景对数；0 关闭；未指定时预览为 3，生产预算为 12")
    parser.add_argument("--book-k", type=_nonnegative_int, default=None, help="最多激活角色事实条数；0 关闭；未指定时预览为 3，生产预算为 0")
    parser.add_argument("--semantic", action=argparse.BooleanOptionalAction, default=True, help="启用本地轻量语义重排，不下载模型或调用网络")
    parser.add_argument("--max-chars", type=_nonnegative_int, default=None, help="仅限制 text 字符数，不是 token 数，也不限制 JSON/诊断摘要总长；0 返回空 text；未指定时预览为 2800，生产预算为 12000")
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument("--json", action="store_true", dest="as_json", help="完整诊断 JSON，保留 evidence 和 scene_matches 数组")
    output_mode.add_argument("--compact-json", action="store_true", help="紧凑白名单 JSON：保留 text、版本、来源状态、诊断、ID 与计数；省略 evidence/scene_matches；总长不受 text 预算限制")
    parser.add_argument("--metadata-only", action="store_true", help="配合 --compact-json 显式省略 text 正文，只输出诊断元数据")
    parser.add_argument("--details", action="store_true", help="文本模式额外显示已渲染/省略 ID、来源状态和检索摘要，不受 text 预算限制")
    args = parser.parse_args(argv)
    text = str(args.text or args.text_positional or "").strip()
    if not text:
        parser.error("missing query text; use --text TEXT")
    if args.private and args.scope and args.scope.startswith("group:"):
        parser.error("--private 与 --scope group:ID 冲突，请使用 private:ID 或省略 --scope")
    if args.details and (args.as_json or args.compact_json):
        parser.error("--details 仅用于文本模式；完整诊断请使用 --json")
    if args.metadata_only and not args.compact_json:
        parser.error("--metadata-only 需要 --compact-json")
    scope = args.scope or ("private:local" if args.private else "group:local")
    defaults = (12, 0, 12000) if args.production_defaults else (3, 3, 2800)
    scene_k = args.scene_k if args.scene_k is not None else defaults[0]
    book_k = args.book_k if args.book_k is not None else defaults[1]
    max_chars = args.max_chars if args.max_chars is not None else defaults[2]
    # Library/import diagnostics must never prefix a machine-readable JSON value.
    with contextlib.redirect_stdout(sys.stderr):
        packet = build_fadianji_evidence_packet(
            text,
            persona="fadianji",
            scope_key=scope,
            is_private=scope.startswith("private:"),
            max_chars=max_chars,
            scene_k=scene_k,
            book_k=book_k,
            semantic=args.semantic,
        )
    if args.as_json:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
    elif args.compact_json:
        compact = {key: packet[key] for key in _COMPACT_KEYS if key in packet}
        if args.metadata_only:
            compact.pop("text", None)
        print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
    else:
        if packet["text"]:
            print(packet["text"])
        else:
            print("text 预算不足以容纳完整 scope 与使用边界，已返回空文本；可增大 --max-chars 或查看 --compact-json。", file=sys.stderr)
        if packet["truncated"] and packet["text"]:
            print(f"text 预算内省略了 {packet['omitted_count']} 条完整证据；诊断计数可见 --compact-json。", file=sys.stderr)
        if args.details:
            print("\n【retrieval summary】")
            print("rendered_ids: " + (", ".join(packet.get("rendered_ids", ())) or "-"))
            print("omitted_ids: " + (", ".join(packet.get("omitted_ids", ())) or "-"))
            statuses = packet.get("source_status", {})
            print("source_status: " + ("; ".join(
                f"{source}={status['state']}({status['count']})"
                for source, status in statuses.items()
            ) or "-"))
            for item in packet["scene_matches"]:
                print(f"- {item['score']:.3f} [{item['source_scope']}/{item['category']}] {item['trigger']} -> {item['reply']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
