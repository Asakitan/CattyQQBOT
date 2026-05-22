#!/usr/bin/env python
"""Catty memory 健康度 audit 脚本 —— 输出每个文件/目录的状态报告。

用法:
    python scripts/audit_memory.py              # 在 cwd 找 memory.json
    python scripts/audit_memory.py --root /path/to/CattyQQAI

报告:
- memory.json 顶层各 key 占用大小 (images/anger/users/groups/...)
- memory_groups/*.json 大小排序 (top 10) + 总大小 + 数量
- memory_users/*.json 大小排序 (top 10) + 总大小 + 数量
- memory_games/*.json 同上 (如果存在)
- anger 字典里超时(>30/90天)条目计数
- images 字典里空 keys[] 条目计数
- 报告任何接近压缩阈值(200KB game) 的文件

不修改任何文件,只读 + 报告。给运维 / 主人定期看健康用。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _human_size(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count} B"
    if byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.1f} KB"
    return f"{byte_count / (1024 * 1024):.2f} MB"


def _audit_dir(directory: Path, label: str, *, top_n: int = 10) -> None:
    print(f"\n=== {label} ({directory.name}/) ===")
    if not directory.is_dir():
        print(f"  目录不存在")
        return
    files = list(directory.glob("*.json"))
    if not files:
        print(f"  无 json 文件")
        return
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    total = sum(f.stat().st_size for f in files)
    print(f"  共 {len(files)} 个文件,总 {_human_size(total)}")
    print(f"  最大 {min(len(files), top_n)} 个:")
    for f in files[:top_n]:
        size = f.stat().st_size
        warn = ""
        if size >= 200_000:
            warn = "  ⚠ 接近/超过 200KB game 压缩阈值"
        elif size >= 500_000:
            warn = "  ⚠⚠ 超过 500KB(异常大)"
        print(f"    {f.name:40} {_human_size(size):>10}{warn}")


def _audit_root_memory(memory_path: Path) -> None:
    print(f"\n=== memory.json (根文件) ===")
    if not memory_path.is_file():
        print(f"  文件不存在: {memory_path}")
        return
    print(f"  路径: {memory_path}")
    print(f"  总大小: {_human_size(memory_path.stat().st_size)}")

    try:
        data = json.loads(memory_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ⚠ 无法解析: {exc}")
        return

    print(f"  顶层字段占比:")
    rows: list[tuple[str, int, str]] = []
    for key, value in data.items():
        sz = len(json.dumps(value, ensure_ascii=False))
        if isinstance(value, dict):
            detail = f"dict, {len(value)} 项"
        elif isinstance(value, list):
            detail = f"list, {len(value)} 项"
        else:
            detail = type(value).__name__
        rows.append((key, sz, detail))
    rows.sort(key=lambda r: r[1], reverse=True)
    for key, sz, detail in rows:
        print(f"    {key:20} {_human_size(sz):>10}  ({detail})")

    # 深度检查:anger 时间分布
    anger = data.get("anger", {})
    if isinstance(anger, dict) and anger:
        now = datetime.now(timezone.utc)
        stale_30 = stale_90 = unparsed = 0
        for record in anger.values():
            if not isinstance(record, dict):
                unparsed += 1
                continue
            upd = record.get("updated_at", "")
            if not upd:
                unparsed += 1
                continue
            try:
                dt = datetime.fromisoformat(upd)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age = (now - dt).days
                if age > 90:
                    stale_90 += 1
                elif age > 30:
                    stale_30 += 1
            except ValueError:
                unparsed += 1
        print(f"  anger 老化: >30天={stale_30}, >90天={stale_90}, 无法解析={unparsed}")

    # 深度检查:images 字典空 entry
    images = data.get("images", {})
    if isinstance(images, dict) and images:
        empty = sum(
            1
            for v in images.values()
            if isinstance(v, dict) and isinstance(v.get("keys"), list) and not v["keys"]
        )
        print(f"  images 字典: 总 {len(images)} entries; 空 keys[] entry: {empty}")


def main() -> int:
    p = argparse.ArgumentParser(description="Catty memory 健康度 audit")
    p.add_argument(
        "--root",
        default=".",
        help="Catty 项目根目录(找 memory.json + memory_groups/ + memory_users/ + memory_games/)",
    )
    args = p.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"⚠ 根目录不存在: {root}", file=sys.stderr)
        return 2

    print(f"Audit root: {root}")
    _audit_root_memory(root / "memory.json")
    _audit_dir(root / "memory_groups", "memory_groups")
    _audit_dir(root / "memory_users", "memory_users")
    _audit_dir(root / "memory_games", "memory_games")

    print()
    print("说明:")
    print("- 200KB game 压缩阈值由 catty_memory_game_size_compress_threshold_bytes 控制(默认 200000)")
    print("- group/user 文件没有强制压缩阈值,只受 max_corpus_messages + summary loop 控制")
    print("- anger >90天且对应群/用户已不活跃可手动清理(不影响业务)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
