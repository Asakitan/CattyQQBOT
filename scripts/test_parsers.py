"""Smoke test for parsers.py — run from project root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# parsers.py 是纯函数模块,不依赖 nonebot;直接走模块文件加载绕开包级 __init__
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "catty_qq_ai_parsers",
    Path(__file__).resolve().parent.parent / "src" / "catty_qq_ai" / "parsers.py",
)
_parsers_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_parsers_mod)

iter_catty_markers = _parsers_mod.iter_catty_markers
lenient_json_loads = _parsers_mod.lenient_json_loads
lenient_json_object = _parsers_mod.lenient_json_object
strip_catty_markers = _parsers_mod.strip_catty_markers


JSON_CASES = [
    ("plain", '{"reply":true}', "dict_true"),
    ("fence_json", "```json\n{\"reply\":true}\n```", "dict_true"),
    ("fence_nolang", "```\n{\"reply\":true}\n```", "dict_true"),
    ("smart_quotes", "{“reply”:true}", "dict_true"),
    ("trailing_comma", '{"reply":true,}', "dict_true"),
    ("mixed_text", 'Sure: {"reply":true} done.', "dict_true"),
    ("single_quotes", "{'reply':true}", "dict_true"),
    ("nested_string_with_brace", '{"text":"a } b","reply":true}', "dict_true"),
    ("not_json", "just a string", "none"),
    ("empty", "", "none"),
    ("array_only", "[1,2,3,]", "list_or_none"),
    ("dict_inside_array", '[{"reply":true}]', "list"),
    ("multi_block", 'Block 1: {"foo": 1}. Block 2: {"reply": true}', "dict_first"),
    ("escaped_quotes", '{"text":"he said \\"hi\\"","reply":true}', "dict_true"),
    ("crlf", "{\r\n  \"reply\":true\r\n}", "dict_true"),
]


def run_json():
    print("=== JSON parsing ===")
    for name, raw, expected in JSON_CASES:
        obj = lenient_json_object(raw)
        loose = lenient_json_loads(raw)
        ok = False
        if expected == "dict_true":
            ok = isinstance(obj, dict) and obj.get("reply") is True
        elif expected == "dict_first":
            ok = isinstance(obj, dict) and "foo" in obj
        elif expected == "none":
            ok = obj is None and loose is None
        elif expected == "list":
            ok = isinstance(loose, list) and len(loose) > 0
        elif expected == "list_or_none":
            ok = isinstance(loose, list) or loose is None
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {name}: dict={obj} loose={type(loose).__name__}")


def run_markers():
    print("\n=== Marker extraction ===")
    samples = [
        "normal: <<<CATTY_RECALL:foo>>> stuff",
        "short close: <<<CATTY_MEME:abc>> trailing",
        "long close: <<<CATTY_MEME:abc>>>> trailing",
        "short open: <<CATTY_INLINE_IMAGE:http://x>>>",
        "long open: <<<<CATTY_NO_REPLY>>>",
        "mixed: hi <<<CATTY_RECALL:a>>> mid <<<CATTY_INLINE_IMAGE:b>>>",
        "no payload: <<<CATTY_NO_REPLY>>>",
    ]
    for s in samples:
        markers = [(n, p) for _, n, p in iter_catty_markers(s)]
        stripped = strip_catty_markers(s, keep={"INLINE_IMAGE", "EMOJI_QUERY", "NO_REPLY"})
        print(f"  src={s!r}")
        print(f"    found={markers}")
        print(f"    after strip_keep_safe={stripped!r}")


if __name__ == "__main__":
    run_json()
    run_markers()
