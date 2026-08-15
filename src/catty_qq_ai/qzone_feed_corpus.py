"""把 Qzone 被点赞动态转换为机机场景 delta 语料，并原子维护增量文件。

QzoneFeedStore 的 TTL 只限制当前 FEED 事实窗口；已经批准的 scene delta 是长期口吻语料，
不会随 feed 过期自动撤回。需要删除时由审核/管理流程显式调用 remove_feed_delta().

语料定位 (Review 2026-08-15): 模板池是阳光/中性吃瓜态样本, 不覆盖丧女/魅魔两态;
需要更丰富口吻时用 scripts/generate_qzone_scene_delta.py 离线 AI 生成 (feed["_generated_reply"]).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

REPLY_TEMPLATES = (
    "那条动态机机也看到了啦，{excerpt}…{like_count}个赞，杂鱼们还挺捧场的喵",
    "哇，{excerpt}居然有{like_count}个赞，机机来围观啦，喵",
    "这条瓜机机记下了，{excerpt}，点赞的杂鱼不少嘛",
    "阳光吃瓜时间到，{excerpt}也太会发了吧，机机喵",
    "机机路过点个赞，{excerpt}，杂鱼们快来吃瓜喵",
)

_FILE_NAME = "qzone_feed.jsonl"


def _delta_root(value: Path | None) -> Path:
    if value is not None:
        return Path(value)
    from .fdj_scene_retrieval import _DELTA_ROOT
    return Path(_DELTA_ROOT)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _excerpt(text: str, limit: int) -> str:
    first = re.split(r"[。！？!?；;]", text, maxsplit=1)[0].strip()
    return (first or text)[:limit].strip()


def _like_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_approved_liked_feed(feed: Mapping[str, Any]) -> bool:
    liked_by = {
        str(item or "").strip()
        for item in (feed.get("liked_by") or [])
        if str(item or "").strip()
    }
    return (
        str(feed.get("auto_like_state") or "") == "liked"
        and str(feed.get("ai_like_decision") or "") == "LIKE"
        and _like_count(feed.get("like_count")) >= 1
        and bool(liked_by)
    )


def _record_id(feed_id: str) -> str:
    return f"qz_{hashlib.sha1(feed_id.encode('utf-8')).hexdigest()[:12]}"


def _pick_template(feed_id: str) -> str:
    """按 feed_id 哈希确定性选模板: 同一动态重复 upsert 语料幂等, 不随机漂移."""
    digest = hashlib.sha1(feed_id.encode("utf-8")).digest()
    return REPLY_TEMPLATES[digest[-1] % len(REPLY_TEMPLATES)]


def feed_entry_to_delta(feed: Mapping[str, Any]) -> dict[str, Any] | None:
    """将单条动态转为 approved 的群聊场景 delta。"""
    text = _clean_text(feed.get("text"))
    if not text:
        return None
    author_name = _clean_text(feed.get("author_name")) or "有人"
    topic_excerpt = _excerpt(text, 40)
    reply_excerpt = _excerpt(text, 30)
    likes = _like_count(feed.get("like_count"))
    generated_reply = _clean_text(feed.get("_generated_reply"))
    reply = generated_reply or _pick_template(str(feed.get("feed_id") or "")).format(
        excerpt=reply_excerpt, like_count=likes
    )
    reply = _clean_text(reply)[:60]
    feed_id = str(feed.get("feed_id") or "")
    return {
        "record_id": _record_id(feed_id),
        "op": "upsert",
        "status": "approved",
        "query_text": f"你们看到{author_name}发的动态了吗，{topic_excerpt}"[:320],
        "reply": reply,
        "category": "空间动态",
        "source_scope": "group",
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("delta line must be an object")
            entries.append(value)
    return entries


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": 1, "active_files": []}
    with path.open("r", encoding="utf-8-sig") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    active_files = manifest.get("active_files", [])
    if not isinstance(active_files, list):
        raise ValueError("manifest active_files must be a list")
    manifest["schema_version"] = 1
    return manifest


def _update_manifest(root: Path) -> None:
    manifest_path = root / "manifest.json"
    manifest = _load_manifest(manifest_path)
    active_files = manifest.setdefault("active_files", [])
    if not any(
        (item.get("path") if isinstance(item, Mapping) else item) == _FILE_NAME
        for item in active_files
    ):
        active_files.append(_FILE_NAME)
    manifest["revision"] = f"qz-{int(time.time())}"
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _write_entries(root: Path, entries: list[dict[str, Any]]) -> None:
    _atomic_write(
        root / _FILE_NAME,
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
    )
    _update_manifest(root)


# 读改写串行锁 (Review 2026-08-15): asyncio.to_thread 会让多个 upsert 并发进线程池,
# os.replace 只防半写入, 防不住 read-modify-write 竞态丢行.
_RMW_LOCK = threading.Lock()


@contextmanager
def _interprocess_rmw_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".qzone_feed.rmw.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def upsert_feed_delta(feed: Mapping[str, Any], *, delta_root: Path | None = None) -> bool:
    """按动态 ID 替换或追加一条 delta，并原子更新 manifest。"""
    try:
        if not is_approved_liked_feed(feed):
            return False
        delta = feed_entry_to_delta(feed)
        if delta is None:
            return False
        root = _delta_root(delta_root)
        with _RMW_LOCK, _interprocess_rmw_lock(root):
            entries = _read_jsonl(root / _FILE_NAME)
            record_id = delta["record_id"]
            replaced = False
            updated: list[dict[str, Any]] = []
            for entry in entries:
                if entry.get("record_id") == record_id:
                    if not replaced:
                        updated.append(delta)
                        replaced = True
                    continue
                updated.append(entry)
            if not replaced:
                updated.append(delta)
            _write_entries(root, updated)
        return True
    except Exception as exc:
        logger.warning("upsert qzone feed delta failed: %s", exc)
        return False


def remove_feed_delta(feed_id: str, *, delta_root: Path | None = None) -> bool:
    """移除指定动态对应的 delta，并原子重写 qzone_feed.jsonl。"""
    try:
        root = _delta_root(delta_root)
        with _RMW_LOCK, _interprocess_rmw_lock(root):
            path = root / _FILE_NAME
            if not path.is_file():
                return True
            target = _record_id(str(feed_id or ""))
            entries = _read_jsonl(path)
            _write_entries(root, [entry for entry in entries if entry.get("record_id") != target])
        return True
    except Exception as exc:
        logger.warning("remove qzone feed delta failed: %s", exc)
        return False