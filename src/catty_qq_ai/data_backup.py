"""本地数据每日快照 (主人 2026-10-25).

memory / 好感 / 会话等 JSON 在磁盘上只有一份: 2026-07-03 断电把 383 个 memory 文件清零,
服务器上没有任何备份可以恢复。这里定期把数据文件打成一个 zip 放到 backup/,
只保留最近 N 份。只做文件读写, 不依赖 nonebot, 由插件的后台循环在线程里调用。
"""
from __future__ import annotations

import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

SNAPSHOT_PREFIX = "data-"
SNAPSHOT_SUFFIX = ".zip"
# 数据目录 (= memory.json 所在目录) 下不进快照的 JSON: 配置文件含明文密钥, 不该再多复制几份
EXCLUDED_ROOT_FILES = frozenset({
    "config.json",
    "config.example.json",
    "config.local.json",
    "file_sync_config.json",
})


def snapshot_sources(data_root: Path, data_dirs: Iterable[Path]) -> list[tuple[Path, str]]:
    """要进快照的 (文件, zip 内路径): 数据目录下的 *.json + 各数据子目录里的 *.json。"""
    sources: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(path: Path, arcname: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            sources.append((path, arcname))

    if data_root.is_dir():
        for entry in sorted(data_root.iterdir()):
            if entry.is_file() and entry.suffix.lower() == ".json" and entry.name not in EXCLUDED_ROOT_FILES:
                add(entry, entry.name)
    for directory in data_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            if path.is_file():
                add(path, f"{directory.name}/{path.relative_to(directory).as_posix()}")
    return sources


def list_snapshots(backup_dir: Path) -> list[Path]:
    if not backup_dir.is_dir():
        return []
    return sorted(
        path for path in backup_dir.iterdir()
        if path.is_file() and path.name.startswith(SNAPSHOT_PREFIX) and path.name.endswith(SNAPSHOT_SUFFIX)
    )


def newest_snapshot_age_seconds(backup_dir: Path, now: float) -> float | None:
    snapshots = list_snapshots(backup_dir)
    if not snapshots:
        return None
    try:
        return max(now - snapshots[-1].stat().st_mtime, 0.0)
    except OSError:
        return None


def prune_snapshots(backup_dir: Path, keep: int) -> list[Path]:
    snapshots = list_snapshots(backup_dir)
    removed: list[Path] = []
    for path in snapshots[: max(len(snapshots) - max(int(keep), 1), 0)]:
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def write_snapshot(
    data_root: Path,
    data_dirs: Iterable[Path],
    backup_dir: Path,
    *,
    keep: int,
    now: datetime | None = None,
) -> tuple[Path | None, int]:
    """写一份快照并清掉多余的旧快照。返回 (快照路径, 收进去的文件数); 没有数据时不写。

    每个文件只在 read_bytes() 的瞬间打开: 各 store 用「写 .tmp → replace」落盘, 在 Windows 上
    目标文件被别人打开时 replace 会失败, 读得越快越不容易撞上 (撞上了 store 下一轮会重试)。
    """
    sources = snapshot_sources(data_root, data_dirs)
    if not sources:
        return None, 0
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{SNAPSHOT_PREFIX}{stamp}{SNAPSHOT_SUFFIX}"
    tmp = target.with_name(target.name + ".tmp")
    written = 0
    try:
        with tmp.open("wb") as raw:
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, arcname in sources:
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                    archive.writestr(arcname, data)
                    written += 1
            raw.flush()
            os.fsync(raw.fileno())
        tmp.replace(target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    prune_snapshots(backup_dir, keep)
    return target, written
