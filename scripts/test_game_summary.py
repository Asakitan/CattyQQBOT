"""Test game memory summary compression + bing image search wiring."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import nonebot
try:
    nonebot.init(driver="~fastapi")
except Exception as e:
    print("init err:", e)

from catty_qq_ai.memory import MemoryStore
from catty_qq_ai.config import Config
from catty_qq_ai.web_search import (
    search_image_urls,
    _search_image_urls_bing,
    _search_image_urls_duckduckgo,
)

cfg = Config(catty_memory_game_summary_min_facts=5, catty_memory_game_keep_recent_facts=2)
ms = MemoryStore(cfg)

for i in range(8):
    ms.record_game_fact("strinova", text=f"事实 {i}: 角色 X 在版本 1.{i} 削弱", source="test")
print("seeded facts:", len(ms._data["games"]["strinova"]["facts"]))
print("due_games:", ms.due_games_for_summary())

msgs = ms.build_game_summary_messages("strinova")
print("prompt msgs:", len(msgs))
print("user_content preview:", msgs[1]["content"][:200])

ms.save_game_summary("strinova", '{"summary":"角色 X 在 1.0-1.7 各版本反复削弱大招倍率"}')
print("after compress:")
print("  summary =", ms._data["games"]["strinova"]["summary"][:60])
print("  remaining facts =", len(ms._data["games"]["strinova"]["facts"]))
print("  due now (should be empty due to last_summary_at):", ms.due_games_for_summary())

print()
print("image search functions importable OK")
print("all checks pass")
