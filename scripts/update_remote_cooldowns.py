"""把远端 config.json 的 web_search.cooldown_seconds 600→60 (imagegen 是 None 用 default 180)。

带备份,只动这一处。
"""
import json
import shutil
from pathlib import Path


def main() -> int:
    p = Path(r"D:\CattyQQAI\config.json")
    bak = p.with_suffix(".json.bak.cd")
    shutil.copy2(p, bak)
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    web = data.setdefault("web_search", {})
    old = web.get("cooldown_seconds")
    web["cooldown_seconds"] = 60
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(f"web_search.cooldown_seconds: {old} -> {web['cooldown_seconds']}")
    print(f"backup: {bak.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
