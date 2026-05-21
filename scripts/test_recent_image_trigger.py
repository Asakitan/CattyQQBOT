"""Smoke test for _references_recent_image regex trigger."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import nonebot
try:
    nonebot.init(driver="~fastapi")
except Exception as e:
    print("init err:", e)

from catty_qq_ai import _references_recent_image

POSITIVE = [
    "刚才那张图是啥",
    "猫猫认得这张图吗",
    "前面那个截图说什么的",
    "没看清这张图",
    "上次发的那张照",
    "这图什么意思",
    "猫猫还记得刚才那张图吗",
    "认不出来这图",
    "那张图片我看不清",
]

NEGATIVE = [
    "今天天气真好",
    "猫猫贴贴",
    "这个怎么做",
    "那个项目怎么样了",
    "猫猫认得吗",
    "这张表写完了",
    "上次那个方案",
    "之前说的那个",
]

print("=== should trigger ===")
miss = 0
for t in POSITIVE:
    ok = _references_recent_image(t)
    label = "OK  " if ok else "MISS"
    if not ok:
        miss += 1
    print(f"  [{label}] {t}")

print("=== should NOT trigger ===")
false_pos = 0
for t in NEGATIVE:
    triggered = _references_recent_image(t)
    label = "FALSE" if triggered else "OK   "
    if triggered:
        false_pos += 1
    print(f"  [{label}] {t}")

print(f"\nResults: positive miss={miss}, false positive={false_pos}")
