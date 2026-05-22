from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .config import Config
from .parsers import lenient_json_object
from .reply_markers import NO_REPLY_MARKER


def _atomic_write_text(path: Path, content: str) -> None:
    """写到 .tmp 再 os.replace 替换目标，防止写一半被中断留下半截 JSON。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


OWNER_TITLE = "主人"
CONTENT_TEMPERATURE_INITIAL = 1.0
CONTENT_TEMPERATURE_HALF_LIFE_MINUTES = 30.0
CONTENT_TEMPERATURE_TURN_DECAY = 0.92
CONTENT_TEMPERATURE_COLD_THRESHOLD = 0.25
CONTENT_TEMPERATURE_SUMMARY_COLD_THRESHOLD = 0.30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().date().isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clamp_temperature(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = CONTENT_TEMPERATURE_INITIAL
    return max(0.0, min(numeric, 1.0))


def _content_temperature(item: dict[str, Any], *, newer_messages: int = 0, now: datetime | None = None) -> float:
    base = _clamp_temperature(
        item.get("content_temperature", item.get("temperature", CONTENT_TEMPERATURE_INITIAL))
    )
    seen_at = _parse_time(item.get("content_touched_at") or item.get("last_seen_at") or item.get("time"))
    if seen_at is None:
        age_factor = 1.0
    else:
        current = now or _utc_now()
        age_minutes = max((_as_aware_utc(current) - _as_aware_utc(seen_at)).total_seconds() / 60.0, 0.0)
        age_factor = 0.5 ** (age_minutes / CONTENT_TEMPERATURE_HALF_LIFE_MINUTES)
    turn_factor = CONTENT_TEMPERATURE_TURN_DECAY ** max(int(newer_messages), 0)
    return max(0.01, min(base * age_factor * turn_factor, 1.0))


def _temperature_label(value: float) -> str:
    if value >= 0.65:
        return "热"
    if value >= CONTENT_TEMPERATURE_COLD_THRESHOLD:
        return "降温"
    return "冷"


def _new_corpus_entry(
    *,
    user_id: str,
    display_name: str,
    text: str,
    has_image: bool,
) -> dict[str, Any]:
    now = _now()
    return {
        "time": now,
        "user_id": user_id,
        "display_name": display_name,
        "text": text,
        "has_image": has_image,
        "content_temperature": CONTENT_TEMPERATURE_INITIAL,
        "content_touched_at": now,
    }


def _sender_name(event: MessageEvent) -> str:
    sender: Any = getattr(event, "sender", None)
    for attr in ("card", "nickname"):
        value = getattr(sender, attr, "") if sender is not None else ""
        if value:
            return str(value)
    return str(event.user_id)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """LLM 输出宽容 JSON 对象解析:统一走 parsers.lenient_json_object。"""
    return lenient_json_object(text)


def _clean_gender(value: Any) -> str:
    gender = str(value or "").strip()
    if gender in {"男", "男性", "男生", "男士"}:
        return "男"
    if gender in {"女", "女性", "女生", "女士"}:
        return "女"
    return "未知"


def _clean_confidence(value: Any) -> str:
    confidence = str(value or "").strip()
    return confidence if confidence in {"低", "中", "高"} else "低"


def _message_mentions(event: MessageEvent) -> list[str]:
    mentions: list[str] = []
    for segment in event.message:
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq") or "").strip()
        if qq and qq != "all":
            mentions.append(qq)
    return list(dict.fromkeys(mentions))


def _safe_storage_id(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip())
    return safe or "unknown"


def _image_record_id(image_keys: list[str]) -> str:
    keys = [key.strip() for key in image_keys if key.strip()]
    if not keys:
        return ""
    payload = json.dumps(keys, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"image_set:{digest}"


class MemoryStore:
    def __init__(self, config: Config) -> None:
        self.enabled = config.catty_memory_enabled
        self.path = Path(config.catty_memory_path).expanduser()
        self.group_storage_dir = self._resolve_storage_dir(
            config.catty_memory_group_storage_dir,
            f"{self.path.stem}_groups",
        )
        self.user_storage_dir = self._resolve_storage_dir(
            config.catty_memory_user_storage_dir,
            f"{self.path.stem}_users",
        )
        self.game_storage_dir = self._resolve_storage_dir(
            getattr(config, "catty_memory_game_storage_dir", "") or "",
            f"{self.path.stem}_games",
        )
        self.max_game_facts = max(int(getattr(config, "catty_memory_max_game_facts", 200) or 200), 20)
        self.max_game_fact_chars = max(int(getattr(config, "catty_memory_max_game_fact_chars", 360) or 360), 60)
        self.game_summary_min_facts = max(int(getattr(config, "catty_memory_game_summary_min_facts", 60) or 60), 10)
        self.game_summary_interval_minutes = max(
            float(getattr(config, "catty_memory_game_summary_interval_minutes", 360.0) or 360.0), 5.0
        )
        self.game_keep_recent_facts = max(int(getattr(config, "catty_memory_game_keep_recent_facts", 20) or 20), 5)
        # 单游戏 JSON 文件大小阈值:超过这个就强制触发一次 LLM 压缩,把旧 facts 压成 summary 释放空间
        self.game_size_compress_threshold_bytes = max(
            int(getattr(config, "catty_memory_game_size_compress_threshold_bytes", 200_000) or 200_000),
            10_000,
        )
        # group_id → game_name 反向索引,从 config.game_context.*_group_ids 推
        self._group_to_game: dict[str, str] = {}
        for group_id in getattr(config, "catty_game_context_star_resonance_group_ids", set()) or set():
            self._group_to_game[str(group_id)] = "star_resonance"
        for group_id in getattr(config, "catty_game_context_strinova_group_ids", set()) or set():
            self._group_to_game[str(group_id)] = "strinova"
        self.max_known_members = max(config.catty_memory_max_known_members, 0)
        self.special_group_ids = {str(group_id) for group_id in config.catty_memory_special_group_ids}
        self.summary_interval_minutes = max(config.catty_memory_summary_interval_minutes, 1)
        self.max_corpus_messages = max(config.catty_memory_max_corpus_messages, 20)
        self.private_summary_messages = max(config.catty_memory_private_summary_messages, 1)
        self.member_mention_threshold = max(config.catty_memory_member_mention_threshold, 1)
        self.proactive_response_window_minutes = max(config.catty_proactive_response_window_minutes, 1.0)
        self.proactive_recent_messages = max(config.catty_proactive_recent_messages, 1)
        self.special_care_user_ids = {str(user_id) for user_id in config.catty_special_care_user_ids}
        self.group_special_care_user_ids = {
            str(group_id): {str(user_id) for user_id in user_ids}
            for group_id, user_ids in config.catty_group_special_care_user_ids.items()
        }
        self.special_care_response_window_minutes = max(config.catty_special_care_response_window_minutes, 1.0)
        self.group_titles = dict(config.catty_group_titles)
        self.user_titles = dict(config.catty_user_titles)
        self.group_user_titles = dict(config.catty_group_user_titles)
        self.save_debounce_seconds = max(
            float(getattr(config, "catty_memory_save_debounce_seconds", 2.0) or 2.0),
            0.1,
        )
        self._data: dict[str, Any] = {"users": {}, "groups": {}, "images": {}, "anger": {}, "games": {}, "group_game_tags": {}}
        self._dirty: bool = False
        if self.enabled:
            self._load()

    def refresh(self) -> None:
        # 热重载前先把内存里待落盘的脏数据写下去，避免被 _load 直接覆盖。
        if self._dirty:
            try:
                self._persist_now()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"memory_store: refresh pre-flush failed: {exc}")
            self._dirty = False
        self._data = {"users": {}, "groups": {}, "images": {}, "anger": {}, "games": {}, "group_game_tags": {}}
        if self.enabled:
            self._load()

    def _resolve_storage_dir(self, configured: str, fallback_name: str) -> Path:
        raw = configured.strip()
        directory = Path(raw).expanduser() if raw else self.path.with_name(fallback_name)
        if not directory.is_absolute():
            directory = self.path.parent / directory
        return directory

    def _group_file(self, group_id: str) -> Path:
        return self.group_storage_dir / f"group_{_safe_storage_id(group_id)}.json"

    def _user_file(self, user_id: str) -> Path:
        return self.user_storage_dir / f"user_{_safe_storage_id(user_id)}.json"

    def _game_file(self, game_name: str) -> Path:
        return self.game_storage_dir / f"game_{_safe_storage_id(game_name)}.json"

    @staticmethod
    def _normalize_game_name(game: str) -> str:
        return re.sub(r"[\s/\\]+", "_", str(game or "").strip().lower())

    def _load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                self._data["users"] = loaded.get("users", {}) if isinstance(loaded.get("users", {}), dict) else {}
                self._data["groups"] = loaded.get("groups", {}) if isinstance(loaded.get("groups", {}), dict) else {}
                self._data["images"] = loaded.get("images", {}) if isinstance(loaded.get("images", {}), dict) else {}
                self._data["anger"] = loaded.get("anger", {}) if isinstance(loaded.get("anger", {}), dict) else {}
                tags_raw = loaded.get("group_game_tags", {})
                self._data["group_game_tags"] = tags_raw if isinstance(tags_raw, dict) else {}
        self._load_entity_files(self.user_storage_dir, "user_id", "user_", self._data.setdefault("users", {}))
        self._load_entity_files(self.group_storage_dir, "group_id", "group_", self._data.setdefault("groups", {}))
        self._load_entity_files(self.game_storage_dir, "game_name", "game_", self._data.setdefault("games", {}))

    def _load_entity_files(self, directory: Path, id_key: str, filename_prefix: str, target: dict[str, Any]) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(loaded, dict):
                continue
            fallback_id = path.stem
            if fallback_id.startswith(filename_prefix):
                fallback_id = fallback_id[len(filename_prefix) :]
            entity_id = str(loaded.get(id_key) or fallback_id)
            raw_data = loaded.get("data")
            if isinstance(raw_data, dict):
                data = raw_data
            else:
                data = {key: value for key, value in loaded.items() if key not in {id_key, "updated_at"}}
            if data:
                target[entity_id] = data

    def _save(self) -> None:
        """标记内存有变更，等 background_flush_loop 或 flush_sync 真正落盘。

        语义保持和旧版兼容：调用方只管"我改完了"，不必关心 I/O。
        debounce 把高频小改动合并成每 save_debounce_seconds 一次写盘。
        """
        if not self.enabled:
            return
        self._dirty = True

    def _persist_now(self) -> None:
        """全量同步把 self._data 落盘（index + 每个 user/group 一个文件）。

        所有文件都走 atomic write，防止写到一半被中断后留下损坏 JSON。
        """
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        index = {
            "version": 2,
            "users": {},
            "groups": {},
            "images": self._data.get("images", {}) if isinstance(self._data.get("images"), dict) else {},
            "anger": self._data.get("anger", {}) if isinstance(self._data.get("anger"), dict) else {},
            "group_game_tags": self._data.get("group_game_tags", {}) if isinstance(self._data.get("group_game_tags"), dict) else {},
            "user_ids": sorted(str(user_id) for user_id in self._data.get("users", {})),
            "group_ids": sorted(str(group_id) for group_id in self._data.get("groups", {})),
            "user_storage_dir": str(self.user_storage_dir),
            "group_storage_dir": str(self.group_storage_dir),
        }
        _atomic_write_text(self.path, json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        for user_id, user in self._data.get("users", {}).items():
            if isinstance(user, dict):
                self._write_entity_file(self._user_file(str(user_id)), "user_id", str(user_id), user)
        for group_id, group in self._data.get("groups", {}).items():
            if isinstance(group, dict):
                self._write_entity_file(self._group_file(str(group_id)), "group_id", str(group_id), group)
        for game_name, game in self._data.get("games", {}).items():
            if isinstance(game, dict):
                self._write_entity_file(self._game_file(str(game_name)), "game_name", str(game_name), game)

    def flush_sync(self) -> bool:
        """如果有脏数据则真正落盘。返回是否实际写了。供 shutdown / hot-reload 钩子调用。"""
        if not self.enabled:
            self._dirty = False
            return False
        if not self._dirty:
            return False
        try:
            self._persist_now()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"memory_store: flush_sync failed: {exc}")
            return False
        finally:
            self._dirty = False
        return True

    async def background_flush_loop(self) -> None:
        """后台 debounce flush。在 on_startup 里 create_task 起来即可。"""
        while True:
            try:
                await asyncio.sleep(self.save_debounce_seconds)
                if self._dirty:
                    self.flush_sync()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"memory_store: background flush failed: {exc}")

    def group_ids(self) -> list[str]:
        groups = self._data.get("groups", {})
        if not isinstance(groups, dict):
            return []
        return sorted(str(group_id) for group_id in groups)

    def remove_group_memory(self, group_id: object) -> bool:
        group_id_text = str(group_id)
        groups = self._data.setdefault("groups", {})
        existed = isinstance(groups, dict) and group_id_text in groups
        if isinstance(groups, dict):
            groups.pop(group_id_text, None)
        anger = self._data.get("anger")
        if isinstance(anger, dict):
            prefix = f"group:{group_id_text}:"
            for key in [key for key in anger if str(key).startswith(prefix)]:
                anger.pop(key, None)
        group_file = self._group_file(group_id_text)
        file_existed = group_file.exists()
        try:
            group_file.unlink(missing_ok=True)
        except OSError:
            pass
        self._save()
        return existed or file_existed

    def _write_entity_file(self, path: Path, id_key: str, entity_id: str, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            id_key: entity_id,
            "updated_at": _now(),
            "data": data,
        }
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _anger_key(self, event: MessageEvent) -> str:
        if isinstance(event, GroupMessageEvent):
            return f"group:{event.group_id}:user:{event.user_id}"
        return f"private:{event.user_id}"

    def user_anger_score(self, event: MessageEvent) -> int:
        anger = self._data.setdefault("anger", {})
        record = anger.get(self._anger_key(event)) if isinstance(anger, dict) else None
        if not isinstance(record, dict):
            return 0
        return max(min(int(record.get("score") or 0), 100), 0)

    def is_user_in_anger_cooldown(self, event: MessageEvent) -> bool:
        return self.user_anger_cooldown_remaining_seconds(event) > 0

    def user_anger_cooldown_remaining_seconds(self, event: MessageEvent) -> float:
        if not self.enabled:
            return 0.0
        anger = self._data.setdefault("anger", {})
        record = anger.get(self._anger_key(event)) if isinstance(anger, dict) else None
        if not isinstance(record, dict):
            return 0.0
        muted_until = _parse_time(record.get("muted_until"))
        if muted_until is None:
            return 0.0
        now = datetime.now(timezone.utc)
        if muted_until > now:
            return (muted_until - now).total_seconds()
        record["muted_until"] = ""
        record["score"] = 0
        record["last_reason"] = "冷却结束"
        record["updated_at"] = _now()
        self._save()
        return 0.0

    def update_user_anger(
        self,
        event: MessageEvent,
        *,
        delta: int,
        reason: str,
        useless: bool,
        mute_threshold: int,
        cooldown_seconds: int,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"score": 0, "muted": False, "reason": ""}
        anger = self._data.setdefault("anger", {})
        if not isinstance(anger, dict):
            anger = {}
            self._data["anger"] = anger
        key = self._anger_key(event)
        record = anger.setdefault(key, {})
        if not isinstance(record, dict):
            record = {}
            anger[key] = record

        old_score = max(min(int(record.get("score") or 0), 100), 0)
        cleaned_delta = max(min(int(delta), 40), -20)
        if useless:
            new_score = min(old_score + max(cleaned_delta, 0), 100)
        else:
            new_score = max(old_score + min(cleaned_delta, -5), 0)

        muted = new_score >= mute_threshold
        record.update(
            {
                "scope": "group" if isinstance(event, GroupMessageEvent) else "private",
                "group_id": str(getattr(event, "group_id", "")),
                "user_id": str(event.user_id),
                "score": new_score,
                "last_delta": cleaned_delta,
                "last_useless": bool(useless),
                "last_reason": reason.strip()[:120],
                "updated_at": _now(),
            }
        )
        if muted:
            muted_until = datetime.now(timezone.utc) + timedelta(seconds=max(cooldown_seconds, 60))
            record["muted_until"] = muted_until.isoformat(timespec="seconds")
        else:
            record["muted_until"] = ""
        self._save()
        return {"score": new_score, "muted": muted, "reason": str(record.get("last_reason") or "")}

    def build_anger_context(self, event: MessageEvent, *, warn_threshold: int) -> str:
        if not self.enabled:
            return ""
        anger = self._data.setdefault("anger", {})
        record = anger.get(self._anger_key(event)) if isinstance(anger, dict) else None
        if not isinstance(record, dict):
            return ""
        score = max(min(int(record.get("score") or 0), 100), 0)
        if score < warn_threshold:
            return ""
        reason = str(record.get("last_reason") or "连续发送无用/复读内容").strip()
        return (
            "用户耐心条状态："
            f"当前怒气值 {score}/100，原因：{reason}。"
            "回复时可以表现出明显不耐烦、少搭理复读和无意义纠缠；但不要攻击现实身份。"
        )

    def get_image_summary(self, image_keys: list[str]) -> str:
        if not self.enabled:
            return ""
        record_id = _image_record_id(image_keys)
        if not record_id:
            return ""
        images = self._data.setdefault("images", {})
        record = images.get(record_id) if isinstance(images, dict) else None
        if not isinstance(record, dict):
            return ""
        summary = str(record.get("summary") or "").strip()
        if summary:
            record["last_seen"] = _now()
            record["seen_count"] = int(record.get("seen_count") or 0) + 1
            self._save()
        return summary

    def remember_image_record(self, image_keys: list[str], image_summary: str) -> None:
        summary = image_summary.strip()
        if not self.enabled or not summary:
            return
        record_id = _image_record_id(image_keys)
        if not record_id:
            return
        images = self._data.setdefault("images", {})
        if not isinstance(images, dict):
            images = {}
            self._data["images"] = images
        existing = images.get(record_id)
        seen_count = int(existing.get("seen_count") or 0) if isinstance(existing, dict) else 0
        images[record_id] = {
            "keys": [key.strip() for key in image_keys if key.strip()],
            "summary": summary,
            "updated_at": _now(),
            "last_seen": _now(),
            "seen_count": seen_count + 1,
        }
        self._save()

    def remember_event(self, event: MessageEvent) -> None:
        if not self.enabled:
            return
        user_id = str(event.user_id)
        name = _sender_name(event)

        users = self._data.setdefault("users", {})
        user = users.setdefault(user_id, {})
        user["display_name"] = name
        user["last_seen"] = _now()
        if user_id in self.user_titles:
            user["title"] = self.user_titles[user_id]

        if isinstance(event, GroupMessageEvent):
            group_id = str(event.group_id)
            groups = self._data.setdefault("groups", {})
            group = groups.setdefault(group_id, {})
            group["last_seen"] = _now()
            group.setdefault("summary", "")
            group.setdefault("member_profiles", {})
            group.setdefault("mention_profiles", {})
            group.setdefault("corpus", [])
            if group_id in self.group_titles:
                group["title"] = self.group_titles[group_id]
            members = group.setdefault("members", {})
            member = members.setdefault(user_id, {})
            member["display_name"] = name
            member["last_seen"] = _now()
            configured_title = self.group_user_titles.get(group_id, {}).get(user_id) or self.user_titles.get(user_id)
            if configured_title:
                member["title"] = configured_title
        self._save()

    def remember_corpus_event(self, event: MessageEvent, text: str, *, has_image: bool = False) -> None:
        if not self.enabled or not isinstance(event, GroupMessageEvent):
            return
        group_id = str(event.group_id)
        self.remember_event(event)
        group = self._data.setdefault("groups", {}).setdefault(group_id, {})
        content = text.strip() if text.strip() else ("[图片]" if has_image else "")
        if not content:
            return

        corpus = group.setdefault("corpus", [])
        if not isinstance(corpus, list):
            corpus = []
            group["corpus"] = corpus
        entry = _new_corpus_entry(
            user_id=str(event.user_id),
            display_name=_sender_name(event),
            text=content,
            has_image=has_image,
        )
        corpus.append(entry)
        del corpus[:-self.max_corpus_messages]

        mention_profiles = group.setdefault("mention_profiles", {})
        for target_id in _message_mentions(event):
            target = mention_profiles.setdefault(target_id, {"corpus": [], "count": 0})
            target_corpus = target.setdefault("corpus", [])
            if not isinstance(target_corpus, list):
                target_corpus = []
                target["corpus"] = target_corpus
            target["count"] = int(target.get("count") or 0) + 1
            target_corpus.append(entry)
            del target_corpus[:-self.member_mention_threshold]
        self._record_proactive_human_message(group)
        if self.is_special_care_user(event):
            self._record_special_care_human_message(group, str(event.user_id))
        self._save()

    def remember_private_corpus_event(self, event: MessageEvent, text: str, *, has_image: bool = False) -> None:
        if not self.enabled or not isinstance(event, PrivateMessageEvent):
            return
        self.remember_event(event)
        user_id = str(event.user_id)
        user = self._data.setdefault("users", {}).setdefault(user_id, {})
        user.setdefault("private_summary", "")
        user.setdefault("private_profile", {})
        corpus = user.setdefault("private_corpus", [])
        if not isinstance(corpus, list):
            corpus = []
            user["private_corpus"] = corpus
        content = text.strip() if text.strip() else ("[图片]" if has_image else "")
        if not content:
            return
        corpus.append(
            _new_corpus_entry(
                user_id=str(event.user_id),
                display_name=_sender_name(event),
                text=content,
                has_image=has_image,
            )
        )
        del corpus[:-self.private_summary_messages]
        self._save()

    def remember_image_summary(self, event: MessageEvent, image_summary: str) -> None:
        summary = image_summary.strip()
        if not self.enabled or not summary:
            return
        content = f"图片内容：{summary}"

        if isinstance(event, GroupMessageEvent):
            group_id = str(event.group_id)
            self.remember_event(event)
            group = self._data.setdefault("groups", {}).setdefault(group_id, {})
            corpus = group.setdefault("corpus", [])
            if not isinstance(corpus, list):
                corpus = []
                group["corpus"] = corpus
            entry = _new_corpus_entry(
                user_id=str(event.user_id),
                display_name=_sender_name(event),
                text=content,
                has_image=True,
            )
            corpus.append(entry)
            del corpus[:-self.max_corpus_messages]

            mention_profiles = group.setdefault("mention_profiles", {})
            for target_id in _message_mentions(event):
                target = mention_profiles.setdefault(target_id, {"corpus": [], "count": 0})
                target_corpus = target.setdefault("corpus", [])
                if not isinstance(target_corpus, list):
                    target_corpus = []
                    target["corpus"] = target_corpus
                target_corpus.append(entry)
                del target_corpus[:-self.member_mention_threshold]
            self._save()
            return

        if isinstance(event, PrivateMessageEvent):
            self.remember_event(event)
            user_id = str(event.user_id)
            user = self._data.setdefault("users", {}).setdefault(user_id, {})
            user.setdefault("private_summary", "")
            user.setdefault("private_profile", {})
            corpus = user.setdefault("private_corpus", [])
            if not isinstance(corpus, list):
                corpus = []
                user["private_corpus"] = corpus
            corpus.append(
                _new_corpus_entry(
                    user_id=str(event.user_id),
                    display_name=_sender_name(event),
                    text=content,
                    has_image=True,
                )
            )
            del corpus[:-self.private_summary_messages]
            self._save()

    def due_group_ids(self) -> list[str]:
        if not self.enabled:
            return []
        due: list[str] = []
        now = datetime.now(timezone.utc)
        min_messages = min(50, self.max_corpus_messages)
        for group_id, group in self._data.get("groups", {}).items():
            corpus = group.get("corpus", []) if isinstance(group, dict) else []
            if len(corpus) < min_messages:
                continue
            last_summary = _parse_time(group.get("last_summary_at")) if isinstance(group, dict) else None
            if last_summary is None or (now - last_summary).total_seconds() >= self.summary_interval_minutes * 60:
                due.append(str(group_id))
        return due

    def due_special_group_ids(self) -> list[str]:
        return self.due_group_ids()

    def due_private_user_ids(self) -> list[str]:
        if not self.enabled:
            return []
        due: list[str] = []
        for user_id, user in self._data.get("users", {}).items():
            if not isinstance(user, dict):
                continue
            corpus = user.get("private_corpus", [])
            if isinstance(corpus, list) and len(corpus) >= self.private_summary_messages:
                due.append(str(user_id))
        return due

    def due_mentioned_members(self) -> list[tuple[str, str]]:
        if not self.enabled:
            return []
        due: list[tuple[str, str]] = []
        for group_id, group in self._data.get("groups", {}).items():
            mentions = group.get("mention_profiles", {}) if isinstance(group, dict) else {}
            if not isinstance(mentions, dict):
                continue
            for user_id, data in mentions.items():
                corpus = data.get("corpus", []) if isinstance(data, dict) else []
                if isinstance(corpus, list) and len(corpus) >= self.member_mention_threshold:
                    due.append((group_id, str(user_id)))
        return due

    def _proactive_state(self, group: dict[str, Any]) -> dict[str, Any]:
        state = group.setdefault("proactive", {})
        if not isinstance(state, dict):
            state = {}
            group["proactive"] = state
        today = _today()
        if state.get("date") != today:
            state["date"] = today
            state["daily_sent"] = 0
            state["daily_human_messages"] = 0
        return state

    def _interaction_score(self, state: dict[str, Any]) -> int:
        try:
            score = int(state.get("interaction_score") or 0)
        except (TypeError, ValueError):
            score = 0
        return max(min(score, 100), 0)

    def _set_interaction_score(self, state: dict[str, Any], score: int) -> None:
        state["interaction_score"] = max(min(int(score), 100), 0)

    def _expire_proactive_pending(self, state: dict[str, Any]) -> bool:
        pending = state.get("pending")
        if not isinstance(pending, dict):
            return False
        if pending.get("responded_at") or pending.get("missed_at"):
            return False
        sent_at = _parse_time(pending.get("sent_at"))
        if sent_at is None:
            pending["missed_at"] = _now()
            return True
        now = datetime.now(timezone.utc)
        if (now - sent_at).total_seconds() < self.proactive_response_window_minutes * 60:
            return False
        pending["missed_at"] = _now()
        state["last_unanswered_at"] = pending["missed_at"]
        state["unanswered_count"] = int(state.get("unanswered_count") or 0) + 1
        self._set_interaction_score(state, self._interaction_score(state) - 8)
        return True

    def _record_proactive_human_message(self, group: dict[str, Any]) -> None:
        state = self._proactive_state(group)
        state["daily_human_messages"] = int(state.get("daily_human_messages") or 0) + 1
        self._set_interaction_score(state, self._interaction_score(state) + 1)

        pending = state.get("pending")
        if not isinstance(pending, dict) or pending.get("responded_at") or pending.get("missed_at"):
            return
        sent_at = _parse_time(pending.get("sent_at"))
        if sent_at is None:
            return
        now = datetime.now(timezone.utc)
        if (now - sent_at).total_seconds() > self.proactive_response_window_minutes * 60:
            self._expire_proactive_pending(state)
            return
        pending["responded_at"] = _now()
        state["last_response_at"] = pending["responded_at"]
        state["last_unanswered_at"] = ""
        state["response_count"] = int(state.get("response_count") or 0) + 1
        self._set_interaction_score(state, self._interaction_score(state) + 8)

    def is_special_care_user(self, event: MessageEvent) -> bool:
        user_id = str(event.user_id)
        if user_id in self.special_care_user_ids:
            return True
        if isinstance(event, GroupMessageEvent):
            return user_id in self.group_special_care_user_ids.get(str(event.group_id), set())
        return False

    def _special_care_state(self, group: dict[str, Any], user_id: str) -> dict[str, Any]:
        special = group.setdefault("special_care", {})
        if not isinstance(special, dict):
            special = {}
            group["special_care"] = special
        state = special.setdefault(user_id, {})
        if not isinstance(state, dict):
            state = {}
            special[user_id] = state
        return state

    def _expire_special_care_pending(self, state: dict[str, Any]) -> bool:
        pending = state.get("pending")
        if not isinstance(pending, dict):
            return False
        if pending.get("responded_at") or pending.get("missed_at"):
            return False
        sent_at = _parse_time(pending.get("sent_at"))
        if sent_at is None:
            pending["missed_at"] = _now()
            return True
        now = datetime.now(timezone.utc)
        if (now - sent_at).total_seconds() < self.special_care_response_window_minutes * 60:
            return False
        pending["missed_at"] = _now()
        state["last_ignored_at"] = pending["missed_at"]
        state["ignored_count"] = int(state.get("ignored_count") or 0) + 1
        return True

    def _record_special_care_human_message(self, group: dict[str, Any], user_id: str) -> bool:
        state = self._special_care_state(group, user_id)
        state["last_seen_at"] = _now()
        pending = state.get("pending")
        if not isinstance(pending, dict) or pending.get("responded_at") or pending.get("missed_at"):
            return False
        sent_at = _parse_time(pending.get("sent_at"))
        if sent_at is None:
            return False
        now = datetime.now(timezone.utc)
        if (now - sent_at).total_seconds() > self.special_care_response_window_minutes * 60:
            self._expire_special_care_pending(state)
            return True
        pending["responded_at"] = _now()
        state["last_response_at"] = pending["responded_at"]
        state["last_ignored_at"] = ""
        state["response_count"] = int(state.get("response_count") or 0) + 1
        return True

    def _has_group_proactive_memory(self, group: dict[str, Any]) -> bool:
        summary = str(group.get("summary") or "").strip()
        if summary:
            return True
        for key in ("corpus",):
            value = group.get(key)
            if isinstance(value, list) and value:
                return True
        for key in ("members", "member_profiles", "mention_profiles"):
            value = group.get(key)
            if isinstance(value, dict) and value:
                return True
        return False

    def build_special_care_context(
        self,
        event: MessageEvent,
        *,
        cooldown_seconds: int,
        enforce_cooldown: bool,
    ) -> str:
        if not self.enabled or not isinstance(event, GroupMessageEvent) or not self.is_special_care_user(event):
            return ""
        group_id = str(event.group_id)
        user_id = str(event.user_id)
        group = self._data.setdefault("groups", {}).setdefault(group_id, {})
        if not isinstance(group, dict):
            return ""
        state = self._special_care_state(group, user_id)
        changed = self._expire_special_care_pending(state)
        now = datetime.now(timezone.utc)
        last_considered = _parse_time(state.get("last_considered_at"))
        if enforce_cooldown and last_considered is not None:
            elapsed = (now - last_considered).total_seconds()
            if elapsed < max(cooldown_seconds, 0):
                if changed:
                    self._save()
                return ""
        state["last_considered_at"] = _now()
        state["last_seen_at"] = state["last_considered_at"]
        self._save()

        title = self._title_for(user_id, group_id)
        ignored_count = int(state.get("ignored_count") or 0)
        last_ignored = "是" if state.get("last_ignored_at") else "否"
        last_text = ""
        pending = state.get("pending")
        if isinstance(pending, dict):
            last_text = str(pending.get("text") or "").strip()[:120]
        return (
            "特别关心触发："
            f"{_sender_name(event)}({user_id}) 是你特别关心的人，称呼可用「{title}」。"
            "这**不是强制回复**，请根据 ta 的发言内容、群聊上下文和自然程度判断要不要跟上去。"
            f"**特别注意**：如果 ta 正在跟群里其他人对话（聊车/游戏/吃喝/工作等不指向你的话题、跟群友互相吐槽、帮群友答问题、和群友互相 @），不要抢话，让 ta 专心跟群友聊，输出 {NO_REPLY_MARKER}。"
            "也不要因为是熟人就连续接 ta 每一句话；一个话题接一次就够，别追着每条回。"
            f"如果不适合回复，只输出 {NO_REPLY_MARKER}。上次贴上去无人接住：{last_ignored}；累计没被理 {ignored_count} 次。"
            "如果最近没被理，可以表现一点败犬感、酸酸失落或嘴硬贴贴，但不要抱怨、道德绑架或刷存在感。"
            + (f"上次你贴过去的内容：{last_text}" if last_text else "")
        )

    def record_special_care_reply_sent(self, event: MessageEvent, text: str) -> None:
        if not self.enabled or not isinstance(event, GroupMessageEvent) or not self.is_special_care_user(event):
            return
        group = self._data.setdefault("groups", {}).setdefault(str(event.group_id), {})
        if not isinstance(group, dict):
            return
        state = self._special_care_state(group, str(event.user_id))
        now = _now()
        state["last_reply_at"] = now
        state["last_reply_text"] = text.strip()[:500]
        state["pending"] = {"sent_at": now, "text": text.strip()[:500]}
        self._save()

    def proactive_daily_target(self, group_id: str, *, max_daily: int) -> int:
        group = self._data.setdefault("groups", {}).setdefault(str(group_id), {})
        state = self._proactive_state(group)
        score = self._interaction_score(state)
        daily_human_messages = int(state.get("daily_human_messages") or 0)
        signal = score + daily_human_messages // 10
        if signal >= 60:
            target = 5
        elif signal >= 40:
            target = 4
        elif signal >= 24:
            target = 3
        elif signal >= 10:
            target = 2
        else:
            target = 1
        return max(min(target, max(max_daily, 0)), 0)

    def _recent_human_message_count(
        self,
        group: dict[str, Any],
        *,
        window_minutes: float,
        now: datetime | None = None,
    ) -> int:
        if window_minutes <= 0:
            return 0
        corpus = group.get("corpus")
        if not isinstance(corpus, list) or not corpus:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=window_minutes)
        count = 0
        for item in reversed(corpus):
            if not isinstance(item, dict):
                continue
            ts = _parse_time(item.get("time"))
            if ts is None:
                continue
            if _as_aware_utc(ts) < cutoff:
                break
            count += 1
        return count

    def due_proactive_group_ids(
        self,
        group_ids: list[str],
        *,
        max_daily: int,
        min_interval_minutes: float,
        active_window_minutes: float = 0.0,
        active_min_messages: int = 0,
    ) -> list[str]:
        if not self.enabled:
            return []
        due: list[str] = []
        now = datetime.now(timezone.utc)
        changed = False
        groups = self._data.setdefault("groups", {})
        for raw_group_id in group_ids:
            group_id = str(raw_group_id)
            group = groups.get(group_id) if isinstance(groups, dict) else None
            if not isinstance(group, dict) or not self._has_group_proactive_memory(group):
                continue
            state = self._proactive_state(group)
            changed = self._expire_proactive_pending(state) or changed
            target = self.proactive_daily_target(group_id, max_daily=max_daily)
            if target <= 0 or int(state.get("daily_sent") or 0) >= target:
                continue
            send_blocked_until = _parse_time(state.get("send_blocked_until"))
            if send_blocked_until is not None:
                if now < _as_aware_utc(send_blocked_until):
                    continue
                state["send_blocked_until"] = ""
                changed = True
            pending = state.get("pending")
            if isinstance(pending, dict) and not pending.get("responded_at") and not pending.get("missed_at"):
                continue
            last_sent = _parse_time(state.get("last_sent_at"))
            if last_sent is not None and (now - last_sent).total_seconds() < max(min_interval_minutes, 1.0) * 60:
                continue
            if active_window_minutes > 0 and active_min_messages > 0:
                recent_count = self._recent_human_message_count(
                    group,
                    window_minutes=active_window_minutes,
                    now=now,
                )
                if recent_count < active_min_messages:
                    continue
            due.append(group_id)
        if changed:
            self._save()
        return due

    def record_proactive_bubble_sent(self, group_id: str, text: str) -> None:
        if not self.enabled:
            return
        group = self._data.setdefault("groups", {}).setdefault(str(group_id), {})
        state = self._proactive_state(group)
        now = _now()
        state["daily_sent"] = int(state.get("daily_sent") or 0) + 1
        state["last_sent_at"] = now
        state["last_sent_text"] = text.strip()[:500]
        state["pending"] = {"sent_at": now, "text": text.strip()[:500]}
        state["send_blocked_until"] = ""
        state["send_failure_count"] = 0
        state["last_send_failure_reason"] = ""
        self._save()

    def record_proactive_bubble_failed(self, group_id: str, reason: str, *, retry_after_minutes: float) -> None:
        if not self.enabled:
            return
        group = self._data.setdefault("groups", {}).setdefault(str(group_id), {})
        state = self._proactive_state(group)
        now = datetime.now(timezone.utc)
        retry_after = now + timedelta(minutes=max(float(retry_after_minutes), 1.0))
        state["last_send_failed_at"] = now.isoformat(timespec="seconds")
        state["send_blocked_until"] = retry_after.isoformat(timespec="seconds")
        state["last_send_failure_reason"] = reason.strip()[:500]
        state["send_failure_count"] = int(state.get("send_failure_count") or 0) + 1
        self._save()

    def _corpus_lines(
        self,
        corpus: list[Any],
        limit: int,
        *,
        min_temperature: float = 0.0,
        include_temperature: bool = True,
    ) -> list[str]:
        lines: list[str] = []
        selected = corpus[-limit:] if limit > 0 else []
        start_index = max(len(corpus) - len(selected), 0)
        now = _utc_now()
        for offset, item in enumerate(selected):
            if not isinstance(item, dict):
                continue
            newer_messages = max(len(corpus) - (start_index + offset) - 1, 0)
            temperature = _content_temperature(item, newer_messages=newer_messages, now=now)
            if temperature < min_temperature:
                continue
            name = str(item.get("display_name") or item.get("user_id") or "群友")
            user_id = str(item.get("user_id") or "")
            text = str(item.get("text") or "")
            image = "+图" if item.get("has_image") else ""
            temperature_prefix = ""
            if include_temperature:
                temperature_prefix = f"[温度{temperature:.2f}/{_temperature_label(temperature)}] "
            lines.append(f"{temperature_prefix}{name}({user_id}{image}): {text}")
        return lines

    def build_summary_messages(self, group_id: str) -> list[dict[str, object]]:
        group = self._data.get("groups", {}).get(group_id, {})
        corpus = group.get("corpus", []) if isinstance(group, dict) else []
        old_summary = str(group.get("summary") or "") if isinstance(group, dict) else ""
        old_profiles = group.get("member_profiles", {}) if isinstance(group, dict) else {}
        lines = self._corpus_lines(corpus, min(self.max_corpus_messages, 300))
        prompt = (
            '压缩QQ群长期记忆，省token。只输出JSON：'
            '{"summary":"<=2500字","members":[{"user_id":"QQ","display_name":"名","gender":"男/女/未知","title":"称呼","impression":"<=30字","confidence":"低/中/高"}]}。'
            f"语料行前的温度0-1表示当前话题热度；低于{CONTENT_TEMPERATURE_SUMMARY_COLD_THRESHOLD:.2f}的旧梗、脏话、攻击性/露骨玩笑只作背景，"
            "除非多次重复或明确是稳定偏好/事实，否则不要写进摘要或人物画像，避免以后主动复读。"
            "只写有证据的信息；性别不确定写未知；不要Markdown/emoji。"
        )
        user_content = (
            f"群:{group_id}\n旧摘要:{old_summary or '无'}\n旧画像:{json.dumps(old_profiles, ensure_ascii=False)}\n"
            + "\n".join(lines)
        )
        return [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}]

    def build_proactive_context(self, group_id: str, *, recent_limit: int | None = None) -> str:
        group = self._data.get("groups", {}).get(str(group_id), {})
        if not isinstance(group, dict):
            return f"当前群：{group_id}；暂无长期记忆。"
        state = self._proactive_state(group)
        self._expire_proactive_pending(state)
        summary = str(group.get("summary") or "").strip() or "暂无"
        corpus = group.get("corpus", [])
        recent_lines = self._corpus_lines(
            corpus if isinstance(corpus, list) else [],
            recent_limit or self.proactive_recent_messages,
            min_temperature=CONTENT_TEMPERATURE_COLD_THRESHOLD,
        )
        members = group.get("members", {})
        member_profiles = group.get("member_profiles", {})
        known: list[str] = []
        if isinstance(members, dict):
            for member_id, member in list(members.items())[-self.max_known_members :]:
                if not isinstance(member, dict):
                    continue
                display_name = str(member.get("display_name") or member_id)
                profile = member_profiles.get(member_id, {}) if isinstance(member_profiles, dict) else {}
                impression = ""
                if isinstance(profile, dict):
                    impression = str(profile.get("impression") or "").strip()
                if not impression:
                    impression = str(member.get("impression") or "").strip()
                title = self._title_for(str(member_id), str(group_id), include_user_memory=False)
                known.append(f"{display_name}({member_id})=>{title}/{impression[:40] or '暂无印象'}")
        sadness = "是" if state.get("last_unanswered_at") else "否"
        return "\n".join(
            [
                f"当前群：{group_id}",
                f"群摘要：{summary}",
                f"互动分：{self._interaction_score(state)}/100；今日群友消息：{int(state.get('daily_human_messages') or 0)}；今日已主动冒泡：{int(state.get('daily_sent') or 0)}。",
                f"上次主动冒泡无人回应：{sadness}",
                "记忆温度规则：近期群聊里的温度越低，说明越像旧梗/冷掉的话题；低温内容不要主动续，除非群友重新提起。",
                "已知群友：" + ("；".join(known) if known else "暂无"),
                "近期群聊：",
                "\n".join(recent_lines[-(recent_limit or self.proactive_recent_messages) :]) if recent_lines else "暂无热话题",
            ]
        )

    def build_private_summary_messages(self, user_id: str) -> list[dict[str, object]]:
        user = self._data.get("users", {}).get(user_id, {})
        corpus = user.get("private_corpus", []) if isinstance(user, dict) else []
        old_summary = str(user.get("private_summary") or "") if isinstance(user, dict) else ""
        old_profile = user.get("private_profile", {}) if isinstance(user, dict) else {}
        display_name = str(user.get("display_name") or user_id) if isinstance(user, dict) else user_id
        lines = self._corpus_lines(corpus, self.private_summary_messages)
        prompt = (
            '压缩QQ私聊记忆，省token。只输出JSON：'
            '{"summary":"<=2500字","profile":{"gender":"男/女/未知","title":"称呼","impression":"<=30字","confidence":"低/中/高"}}。'
            "语料行前的温度0-1表示当前话题热度；低温旧梗、一次性玩笑或情绪化片段只作背景，"
            "除非反复出现或是稳定偏好/边界，否则不要写进长期摘要。"
            "只写偏好、事实、称呼、边界；不要Markdown/emoji。"
        )
        user_content = (
            f"用户:{user_id}/{display_name}\n旧摘要:{old_summary or '无'}\n旧画像:{json.dumps(old_profile, ensure_ascii=False)}\n"
            + "\n".join(lines)
        )
        return [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}]

    def build_member_mention_summary_messages(self, group_id: str, user_id: str) -> list[dict[str, object]]:
        group = self._data.get("groups", {}).get(group_id, {})
        mentions = group.get("mention_profiles", {}) if isinstance(group, dict) else {}
        mention_data = mentions.get(user_id, {}) if isinstance(mentions, dict) else {}
        corpus = mention_data.get("corpus", []) if isinstance(mention_data, dict) else []
        old_profiles = group.get("member_profiles", {}) if isinstance(group, dict) else {}
        old_profile = old_profiles.get(user_id, {}) if isinstance(old_profiles, dict) else {}
        lines = self._corpus_lines(corpus, self.member_mention_threshold)
        prompt = (
            '根据群里50次提到某人的上下文做短画像。只输出JSON：'
            '{"user_id":"QQ","gender":"男/女/未知","title":"称呼","impression":"<=140字","evidence":"<=60字","confidence":"低/中/高"}。'
            "语料行前的温度0-1表示当前话题热度；低温单次调侃、脏梗或攻击性称呼不要当成人物稳定特征。"
            "只根据上下文证据；不要Markdown/emoji。"
        )
        user_content = (
            f"群:{group_id}\n目标QQ:{user_id}\n旧画像:{json.dumps(old_profile, ensure_ascii=False)}\n"
            + "\n".join(lines)
        )
        return [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}]

    def save_group_summary(self, group_id: str, summary: str) -> None:
        group = self._data.setdefault("groups", {}).setdefault(group_id, {})
        parsed = _extract_json_object(summary)
        if parsed is None:
            group["summary"] = summary.strip()
        else:
            group["summary"] = str(parsed.get("summary") or "").strip()
            profiles = group.setdefault("member_profiles", {})
            members = group.setdefault("members", {})
            raw_members = parsed.get("members", [])
            if isinstance(raw_members, list):
                for raw_member in raw_members:
                    self._save_member_profile(group_id, profiles, members, raw_member)
        group["last_summary_at"] = _now()
        group["corpus"] = []
        self._save()

    def save_private_summary(self, user_id: str, summary: str) -> None:
        user = self._data.setdefault("users", {}).setdefault(user_id, {})
        parsed = _extract_json_object(summary)
        if parsed is None:
            user["private_summary"] = summary.strip()
        else:
            user["private_summary"] = str(parsed.get("summary") or "").strip()
            raw_profile = parsed.get("profile", {})
            if isinstance(raw_profile, dict):
                profile = {
                    "gender": _clean_gender(raw_profile.get("gender")),
                    "inferred_title": self._safe_title(user_id, None, raw_profile.get("title")),
                    "impression": str(raw_profile.get("impression") or "").strip(),
                    "confidence": _clean_confidence(raw_profile.get("confidence")),
                    "updated_at": _now(),
                }
                user["private_profile"] = profile
                user["gender"] = profile["gender"]
                user["inferred_title"] = profile["inferred_title"]
                user["impression"] = profile["impression"]
                user["profile_confidence"] = profile["confidence"]
        user["last_private_summary_at"] = _now()
        user["private_corpus"] = []
        self._save()

    def save_member_mention_summary(self, group_id: str, user_id: str, summary: str) -> None:
        group = self._data.setdefault("groups", {}).setdefault(group_id, {})
        parsed = _extract_json_object(summary)
        if parsed is None:
            parsed = {"user_id": user_id, "impression": summary.strip(), "confidence": "低"}
        parsed["user_id"] = user_id
        profiles = group.setdefault("member_profiles", {})
        members = group.setdefault("members", {})
        self._save_member_profile(group_id, profiles, members, parsed)
        mention_data = group.setdefault("mention_profiles", {}).setdefault(user_id, {})
        mention_data["last_summary_at"] = _now()
        mention_data["corpus"] = []
        mention_data["count"] = 0
        self._save()

    def _save_member_profile(self, group_id: str, profiles: dict[str, Any], members: dict[str, Any], raw_member: Any) -> None:
        if not isinstance(raw_member, dict):
            return
        user_id = str(raw_member.get("user_id") or "").strip()
        if not user_id:
            return
        profile = {
            "display_name": str(raw_member.get("display_name") or "").strip(),
            "gender": _clean_gender(raw_member.get("gender")),
            "inferred_title": self._safe_title(user_id, group_id, raw_member.get("title")),
            "impression": str(raw_member.get("impression") or "").strip(),
            "evidence": str(raw_member.get("evidence") or "").strip(),
            "confidence": _clean_confidence(raw_member.get("confidence")),
            "updated_at": _now(),
        }
        profiles[user_id] = {**profiles.get(user_id, {}), **profile}
        member = members.setdefault(user_id, {})
        if profile["display_name"]:
            member.setdefault("display_name", profile["display_name"])
        member["gender"] = profile["gender"]
        member["inferred_title"] = profile["inferred_title"]
        member["impression"] = profile["impression"]
        member["profile_confidence"] = profile["confidence"]

    def _configured_title_for(self, user_id: str, group_id: str | None = None) -> str:
        if group_id and self.group_user_titles.get(group_id, {}).get(user_id):
            return str(self.group_user_titles[group_id][user_id]).strip()
        if user_id in self.user_titles:
            return str(self.user_titles[user_id]).strip()
        return ""

    def _is_configured_owner(self, user_id: str, group_id: str | None = None) -> bool:
        return self._configured_title_for(user_id, group_id) == OWNER_TITLE

    def _safe_title(self, user_id: str, group_id: str | None, title: Any) -> str:
        cleaned = str(title or "").strip() or "群友"
        if OWNER_TITLE in cleaned and not self._is_configured_owner(user_id, group_id):
            return "群友"
        return cleaned

    def _title_for(self, user_id: str, group_id: str | None = None, *, include_user_memory: bool = True) -> str:
        configured_title = self._configured_title_for(user_id, group_id)
        if configured_title:
            return configured_title
        if group_id:
            group = self._data.get("groups", {}).get(group_id, {})
            member = group.get("members", {}).get(user_id, {}) if isinstance(group, dict) else {}
            for key in ("title", "inferred_title"):
                title = member.get(key) if isinstance(member, dict) else None
                if title:
                    return self._safe_title(user_id, group_id, title)
            profile = group.get("member_profiles", {}).get(user_id, {}) if isinstance(group, dict) else {}
            profile_title = profile.get("inferred_title") if isinstance(profile, dict) else None
            if profile_title:
                return self._safe_title(user_id, group_id, profile_title)
            group_title = self.group_titles.get(group_id) or group.get("title") if isinstance(group, dict) else None
            if group_title:
                return self._safe_title(user_id, group_id, group_title)
            if not include_user_memory:
                return "群友"
        user = self._data.get("users", {}).get(user_id, {})
        if isinstance(user, dict):
            for source in (user, user.get("private_profile", {})):
                if isinstance(source, dict):
                    title = source.get("title") or source.get("inferred_title")
                    if title:
                        return self._safe_title(user_id, group_id, title)
        return "群友"

    def _same_user_memory_lines(self, user_id: str) -> list[str]:
        user = self._data.get("users", {}).get(user_id, {})
        if not isinstance(user, dict):
            return []
        lines: list[str] = []
        summary = str(user.get("private_summary") or "").strip()
        if summary:
            lines.append("- 同一 QQ 用户私聊摘要：" + summary)
        profile = user.get("private_profile", {})
        if isinstance(profile, dict) and profile:
            lines.append(
                f"- 同一 QQ 用户画像：性别={_clean_gender(profile.get('gender'))}，"
                f"印象={str(profile.get('impression') or '暂无')[:80]}。"
            )
        return lines

    def _profile_for(self, user_id: str, group_id: str) -> dict[str, Any]:
        group = self._data.get("groups", {}).get(group_id, {})
        if not isinstance(group, dict):
            return {}
        result: dict[str, Any] = {}
        profile = group.get("member_profiles", {}).get(user_id, {})
        member = group.get("members", {}).get(user_id, {})
        if isinstance(profile, dict):
            result.update(profile)
        if isinstance(member, dict):
            for key in ("gender", "inferred_title", "impression", "profile_confidence"):
                if member.get(key):
                    result[key] = member[key]
        return result

    def clear_cache(self, event: MessageEvent) -> str:
        if not self.enabled:
            return "记忆功能没开，暂时没有缓存可清。"
        if isinstance(event, GroupMessageEvent):
            group_id = str(event.group_id)
            group = self._data.setdefault("groups", {}).setdefault(group_id, {})
            corpus = group.get("corpus", [])
            corpus_count = len(corpus) if isinstance(corpus, list) else 0
            mention_count = 0
            mentions = group.get("mention_profiles", {})
            if isinstance(mentions, dict):
                for data in mentions.values():
                    if not isinstance(data, dict):
                        continue
                    mention_corpus = data.get("corpus", [])
                    if isinstance(mention_corpus, list):
                        mention_count += len(mention_corpus)
                    data["corpus"] = []
                    data["count"] = 0
            group["corpus"] = []
            self._save()
            return (
                f"当前群待压缩缓存清掉啦：群语料 {corpus_count} 条，"
                f"@人物语料 {mention_count} 条；长期摘要和人物画像保留。"
            )
        if isinstance(event, PrivateMessageEvent):
            user_id = str(event.user_id)
            user = self._data.setdefault("users", {}).setdefault(user_id, {})
            corpus = user.get("private_corpus", [])
            corpus_count = len(corpus) if isinstance(corpus, list) else 0
            user["private_corpus"] = []
            self._save()
            return f"当前私聊人物待压缩缓存清掉啦：私聊语料 {corpus_count} 条；长期摘要和人物画像保留。"
        return "这个消息类型没有可清理的记忆缓存。"

    def build_memory_view(self, event: MessageEvent) -> str:
        if not self.enabled:
            return "记忆功能没开喔。"
        if isinstance(event, GroupMessageEvent):
            return self._build_group_memory_view(str(event.group_id), str(event.user_id))
        if isinstance(event, PrivateMessageEvent):
            return self._build_private_memory_view(str(event.user_id))
        return "这个消息类型还没有记忆视图。"

    def _build_group_memory_view(self, group_id: str, current_user_id: str) -> str:
        group = self._data.get("groups", {}).get(group_id, {})
        if not isinstance(group, dict):
            return f"当前群 {group_id} 还没有存储记忆。"
        corpus = group.get("corpus", [])
        members = group.get("members", {})
        profiles = group.get("member_profiles", {})
        corpus_count = len(corpus) if isinstance(corpus, list) else 0
        member_count = len(members) if isinstance(members, dict) else 0
        profile_count = len(profiles) if isinstance(profiles, dict) else 0
        lines = [
            f"当前群记忆：{group_id}",
            f"存储文件：{self._group_file(group_id)}",
            f"待压缩语料：{corpus_count} 条；已知群友：{member_count} 个；人物画像：{profile_count} 个。",
        ]
        summary = str(group.get("summary") or "").strip()
        lines.append("群摘要：" + (summary or "暂无"))
        current_profile = self._profile_for(current_user_id, group_id)
        if current_profile:
            lines.append(
                "当前用户画像："
                f"称呼={self._title_for(current_user_id, group_id)}，"
                f"性别={_clean_gender(current_profile.get('gender'))}，"
                f"印象={str(current_profile.get('impression') or '暂无')[:80]}。"
            )
        known: list[str] = []
        if isinstance(members, dict):
            limit = self.max_known_members or 20
            for member_id, member in list(members.items())[-limit:]:
                if not isinstance(member, dict):
                    continue
                display_name = str(member.get("display_name") or member_id)
                profile = self._profile_for(str(member_id), group_id)
                impression = str(profile.get("impression") or "").strip()[:40] or "暂无印象"
                known.append(
                    f"- {display_name}({member_id})："
                    f"{self._title_for(str(member_id), group_id, include_user_memory=False)}；{impression}"
                )
            remaining = max(len(members) - len(known), 0)
            if remaining:
                known.append(f"- 还有 {remaining} 个群友没有展开。")
        lines.append("群友信息：")
        lines.extend(known or ["- 暂无"])
        return "\n".join(lines)

    def _build_private_memory_view(self, user_id: str) -> str:
        user = self._data.get("users", {}).get(user_id, {})
        if not isinstance(user, dict):
            return f"私聊用户 {user_id} 还没有存储记忆。"
        corpus = user.get("private_corpus", [])
        corpus_count = len(corpus) if isinstance(corpus, list) else 0
        profile = user.get("private_profile", {})
        lines = [
            f"当前私聊人物记忆：{user_id}",
            f"存储文件：{self._user_file(user_id)}",
            f"待压缩语料：{corpus_count} 条。",
            "私聊摘要：" + (str(user.get("private_summary") or "").strip() or "暂无"),
        ]
        if isinstance(profile, dict) and profile:
            lines.append(
                "人物画像："
                f"称呼={self._title_for(user_id)}，"
                f"性别={_clean_gender(profile.get('gender'))}，"
                f"印象={str(profile.get('impression') or '暂无')[:80]}，"
                f"置信度={_clean_confidence(profile.get('confidence'))}。"
            )
        else:
            lines.append("人物画像：暂无")
        return "\n".join(lines)

    def reply_boost_signal(self, event: MessageEvent) -> dict[str, Any]:
        if not self.enabled:
            return {}
        if isinstance(event, GroupMessageEvent):
            group = self._data.get("groups", {}).get(str(event.group_id), {})
            if not isinstance(group, dict):
                return {"scope": "group", "corpus_count": 0, "has_summary": False, "profile_count": 0}
            corpus = group.get("corpus", [])
            profiles = group.get("member_profiles", {})
            return {
                "scope": "group",
                "corpus_count": len(corpus) if isinstance(corpus, list) else 0,
                "has_summary": bool(str(group.get("summary") or "").strip()),
                "profile_count": len(profiles) if isinstance(profiles, dict) else 0,
            }
        if isinstance(event, PrivateMessageEvent):
            user = self._data.get("users", {}).get(str(event.user_id), {})
            if not isinstance(user, dict):
                return {"scope": "private", "corpus_count": 0, "has_summary": False, "profile_count": 0}
            corpus = user.get("private_corpus", [])
            profile = user.get("private_profile", {})
            return {
                "scope": "private",
                "corpus_count": len(corpus) if isinstance(corpus, list) else 0,
                "has_summary": bool(str(user.get("private_summary") or "").strip()),
                "profile_count": 1 if isinstance(profile, dict) and profile else 0,
            }
        return {}

    def build_context(self, event: MessageEvent) -> str:
        if not self.enabled:
            return ""
        user_id = str(event.user_id)
        name = _sender_name(event)
        lines = ["记忆与称呼规则：", f"- 当前用户：QQ {user_id}，显示名「{name}」。"]

        if isinstance(event, GroupMessageEvent):
            group_id = str(event.group_id)
            title = self._title_for(user_id, group_id)
            lines.append(f"- 当前群：{group_id}；优先称呼当前用户为「{title}」。")
            if self._is_configured_owner(user_id, group_id):
                lines.append("- 当前用户在配置里被定义为「主人」，可以这样称呼。")
            else:
                lines.append("- 只有配置称呼明确为「主人」的 QQ 才能被叫主人；当前用户不能叫主人。")
            group = self._data.get("groups", {}).get(group_id, {})
            if isinstance(group, dict):
                summary = str(group.get("summary") or "").strip()
                if summary:
                    lines.append("- 群摘要：" + summary)
                # 群级别 sticky notes(AI 通过 catty_remember 写入的长期备忘)
                group_notes_line = self._notes_context_line(group, label="本群笔记")
                if group_notes_line:
                    lines.append(group_notes_line)
                profile = self._profile_for(user_id, group_id)
                if profile:
                    lines.append(
                        f"- 当前用户画像：性别={_clean_gender(profile.get('gender'))}，"
                        f"印象={str(profile.get('impression') or '暂无')[:80]}。"
                    )
                # 用户级 sticky notes(跨群,只在用户对象上)
                user_obj = self._data.get("users", {}).get(user_id, {})
                if isinstance(user_obj, dict):
                    user_notes_line = self._notes_context_line(user_obj, label="对此用户的笔记")
                    if user_notes_line:
                        lines.append(user_notes_line)
                lines.extend(self._same_user_memory_lines(user_id))
                members = group.get("members", {})
                known: list[str] = []
                if isinstance(members, dict) and self.max_known_members:
                    for member_id, member in list(members.items())[-self.max_known_members:]:
                        if not isinstance(member, dict):
                            continue
                        display_name = str(member.get("display_name") or member_id)
                        member_title = self._title_for(str(member_id), group_id, include_user_memory=False)
                        profile = self._profile_for(str(member_id), group_id)
                        impression = str(profile.get("impression") or "").strip()
                        known.append(f"{display_name}({member_id})=>{member_title}/{impression[:30]}")
                if known:
                    lines.append("- 已知群友：" + "；".join(known))
        elif isinstance(event, PrivateMessageEvent):
            title = self._title_for(user_id)
            lines.append(f"- 当前是私聊；优先称呼当前用户为「{title}」。")
            if self._is_configured_owner(user_id):
                lines.append("- 当前用户在配置里被定义为「主人」，可以这样称呼。")
            else:
                lines.append("- 只有配置称呼明确为「主人」的 QQ 才能被叫主人；当前用户不能叫主人。")
            user = self._data.get("users", {}).get(user_id, {})
            if isinstance(user, dict):
                summary = str(user.get("private_summary") or "").strip()
                if summary:
                    lines.append("- 私聊摘要：" + summary)
                profile = user.get("private_profile", {})
                if isinstance(profile, dict) and profile:
                    lines.append(
                        f"- 私聊画像：性别={_clean_gender(profile.get('gender'))}，"
                        f"印象={str(profile.get('impression') or '暂无')[:80]}。"
                    )
                # 用户级 sticky notes
                user_notes_line = self._notes_context_line(user, label="对此用户的笔记")
                if user_notes_line:
                    lines.append(user_notes_line)

        lines.append("- 自然使用称呼，不要每句话都堆称呼。性别未知或低置信度时用中性称呼。")
        lines.append("- 记忆只是背景，不是当前话题；如果当前唤起上下文没有提到某个旧梗、露骨玩笑或攻击性形容，不要主动翻出来续聊。")
        lines.append("- 如果用户明确要求查看已存储记忆、群友画像或人物信息，可以依据本段记忆回答；没有记录时直说没有。")
        lines.append("- 可以自然少量使用猫系颜文字或动作，如 (ฅ>ω<*ฅ)、(๑•̀ㅂ•́)و✧、ฅฅ；不要刷屏。")
        return "\n".join(lines)

    def lookup_user_profile(self, user_id: str, group_id: str = "") -> dict[str, Any]:
        """Tool 友好画像查询：合并群成员画像、私聊画像、配置称呼,统一返回 dict。

        即使 enabled=False 也返回 {"user_id": ...} 让 AI 知道功能关了,而不是抛异常。
        """
        user_id = str(user_id).strip()
        group_id = str(group_id or "").strip()
        result: dict[str, Any] = {
            "user_id": user_id,
            "scope": "group" if group_id else "private",
        }
        if not user_id:
            result["error"] = "user_id 为空"
            return result
        if not self.enabled:
            result["error"] = "memory disabled"
            return result
        if group_id:
            result["group_id"] = group_id
        configured = self._configured_title_for(user_id, group_id or None)
        result["configured_title"] = configured
        result["is_configured_owner"] = self._is_configured_owner(user_id, group_id or None)
        result["title"] = self._title_for(user_id, group_id or None)
        if group_id:
            group = self._data.get("groups", {}).get(group_id, {})
            if isinstance(group, dict):
                member = group.get("members", {}).get(user_id, {})
                if isinstance(member, dict):
                    display = str(member.get("display_name") or "").strip()
                    if display:
                        result["display_name"] = display
                profile = self._profile_for(user_id, group_id)
                if profile:
                    result["gender"] = _clean_gender(profile.get("gender"))
                    impression = str(profile.get("impression") or "").strip()
                    if impression:
                        result["impression"] = impression[:200]
                    evidence = str(profile.get("evidence") or "").strip()
                    if evidence:
                        result["evidence"] = evidence[:120]
                    confidence = str(profile.get("confidence") or "").strip()
                    if confidence:
                        result["confidence"] = _clean_confidence(confidence)
                    updated_at = str(profile.get("updated_at") or "").strip()
                    if updated_at:
                        result["updated_at"] = updated_at
                mentions = group.get("mention_profiles", {}) if isinstance(group, dict) else {}
                mention_count = 0
                if isinstance(mentions, dict):
                    mention_data = mentions.get(user_id, {})
                    if isinstance(mention_data, dict):
                        mention_count = int(mention_data.get("count") or 0)
                if mention_count:
                    result["mention_count_since_last_summary"] = mention_count
        user = self._data.get("users", {}).get(user_id, {})
        if isinstance(user, dict):
            display = str(user.get("display_name") or "").strip()
            if display and "display_name" not in result:
                result["display_name"] = display
            private_profile = user.get("private_profile", {})
            if isinstance(private_profile, dict) and private_profile:
                if "gender" not in result:
                    result["gender"] = _clean_gender(private_profile.get("gender"))
                impression = str(private_profile.get("impression") or "").strip()
                if impression and "impression" not in result:
                    result["impression"] = impression[:200]
                if "confidence" not in result:
                    result["confidence"] = _clean_confidence(private_profile.get("confidence"))
            summary = str(user.get("private_summary") or "").strip()
            if summary:
                result["private_summary"] = summary[:400]
        return result

    def recall(
        self,
        *,
        user_id: str = "",
        group_id: str = "",
        keywords: str = "",
        limit: int = 8,
    ) -> dict[str, Any]:
        """Tool 友好语料/摘要检索：在指定 scope 内 substring 匹配 keywords。

        keywords 为空时返回最近的几条语料 + 摘要。匹配大小写不敏感、忽略空白。
        """
        user_id = str(user_id or "").strip()
        group_id = str(group_id or "").strip()
        scope = "group" if group_id else "private"
        limit = max(1, min(int(limit or 8), 20))
        result: dict[str, Any] = {"scope": scope, "limit": limit}
        if group_id:
            result["group_id"] = group_id
        if user_id:
            result["user_id"] = user_id
        if not self.enabled:
            result["error"] = "memory disabled"
            return result
        keyword_list = [kw.strip().casefold() for kw in re.split(r"[\s,，;；]+", keywords or "") if kw.strip()]
        result["keywords"] = keyword_list

        def _match_text(text: str) -> bool:
            if not keyword_list:
                return True
            normalized = text.casefold()
            return all(kw in normalized for kw in keyword_list)

        matches: list[dict[str, Any]] = []
        summary_text = ""
        if group_id:
            group = self._data.get("groups", {}).get(group_id, {})
            if isinstance(group, dict):
                summary_text = str(group.get("summary") or "").strip()
                corpus_lists: list[tuple[str, list[Any]]] = []
                raw_corpus = group.get("corpus", [])
                if isinstance(raw_corpus, list):
                    corpus_lists.append(("group_corpus", raw_corpus))
                mention_profiles = group.get("mention_profiles", {})
                if isinstance(mention_profiles, dict):
                    for mention_user_id, mention_data in mention_profiles.items():
                        if user_id and str(mention_user_id) != user_id:
                            continue
                        if isinstance(mention_data, dict):
                            mention_corpus = mention_data.get("corpus", [])
                            if isinstance(mention_corpus, list):
                                corpus_lists.append((f"mention:{mention_user_id}", mention_corpus))
                for source, corpus in corpus_lists:
                    for entry in reversed(corpus):
                        if len(matches) >= limit:
                            break
                        if not isinstance(entry, dict):
                            continue
                        if user_id and str(entry.get("user_id") or "") != user_id:
                            continue
                        text = str(entry.get("text") or "").strip()
                        if not text or not _match_text(text):
                            continue
                        matches.append(
                            {
                                "source": source,
                                "time": str(entry.get("time") or "").strip(),
                                "user_id": str(entry.get("user_id") or ""),
                                "display_name": str(entry.get("display_name") or ""),
                                "text": text[:240],
                                "has_image": bool(entry.get("has_image")),
                            }
                        )
                    if len(matches) >= limit:
                        break
        elif user_id:
            user = self._data.get("users", {}).get(user_id, {})
            if isinstance(user, dict):
                summary_text = str(user.get("private_summary") or "").strip()
                corpus = user.get("private_corpus", [])
                if isinstance(corpus, list):
                    for entry in reversed(corpus):
                        if len(matches) >= limit:
                            break
                        if not isinstance(entry, dict):
                            continue
                        text = str(entry.get("text") or "").strip()
                        if not text or not _match_text(text):
                            continue
                        matches.append(
                            {
                                "source": "private_corpus",
                                "time": str(entry.get("time") or "").strip(),
                                "user_id": user_id,
                                "display_name": str(entry.get("display_name") or ""),
                                "text": text[:240],
                                "has_image": bool(entry.get("has_image")),
                            }
                        )
        else:
            result["error"] = "需要 group_id 或 user_id"
            return result
        if summary_text:
            result["long_term_summary"] = summary_text[:600]
        result["matches"] = matches
        result["match_count"] = len(matches)
        return result

    # ── 游戏记忆库 ──────────────────────────────────────────────────────

    def game_for_group(self, group_id: str | int | None) -> str:
        """根据 group_id 推这个群对应的游戏名。空字符串 = 不在任何游戏群。"""
        if group_id is None:
            return ""
        return self._group_to_game.get(str(group_id), "")

    def record_game_fact(
        self,
        game: str,
        text: str,
        *,
        source: str = "manual",
        url: str = "",
        group_id: str = "",
        user_id: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """把一条事实写入指定游戏的记忆库。

        返回 ``{"ok": bool, "game": str, "facts_count": int}`` 表示落盘后状态。
        游戏名做小写归一化;facts 超过 ``max_game_facts`` 时淘汰最旧。
        """
        if not self.enabled:
            return {"ok": False, "error": "memory disabled"}
        game_name = self._normalize_game_name(game)
        text = str(text or "").strip()
        if not game_name:
            return {"ok": False, "error": "game 名为空"}
        if not text:
            return {"ok": False, "error": "fact 文本为空"}
        if len(text) > self.max_game_fact_chars:
            text = text[: self.max_game_fact_chars]

        games = self._data.setdefault("games", {})
        game_record = games.setdefault(game_name, {"facts": []})
        if not isinstance(game_record.get("facts"), list):
            game_record["facts"] = []
        facts: list[dict[str, Any]] = game_record["facts"]

        entry = {
            "time": _now(),
            "text": text,
            "source": str(source or "manual").strip()[:64],
        }
        if url:
            entry["url"] = str(url).strip()[:500]
        if group_id:
            entry["group_id"] = str(group_id)
        if user_id:
            entry["user_id"] = str(user_id)
        if tags:
            entry["tags"] = [str(t).strip() for t in tags if str(t).strip()][:8]

        # 去重:同 text + 同 source 不重复写入
        dedup_key = (text, entry["source"])
        for existing in facts[-50:]:  # 只在最近 50 条里查重,避免 O(n) 扫全表
            if not isinstance(existing, dict):
                continue
            if (str(existing.get("text") or ""), str(existing.get("source") or "")) == dedup_key:
                return {"ok": True, "game": game_name, "facts_count": len(facts), "deduplicated": True}

        facts.append(entry)
        # LRU 截断:超过 max 时丢最早的
        if len(facts) > self.max_game_facts:
            game_record["facts"] = facts[-self.max_game_facts :]
        game_record["last_updated"] = _now()
        self._save()
        return {"ok": True, "game": game_name, "facts_count": len(game_record["facts"])}

    def recall_game(
        self,
        game: str,
        *,
        keywords: str = "",
        limit: int = 8,
    ) -> dict[str, Any]:
        """查指定游戏的事实记忆库。

        keywords 用空格/逗号分隔,做 substring AND 匹配(大小写不敏感);
        留空返回最近 ``limit`` 条。
        """
        result: dict[str, Any] = {"game": self._normalize_game_name(game), "limit": max(1, min(int(limit or 8), 50))}
        if not self.enabled:
            result["error"] = "memory disabled"
            result["matches"] = []
            return result
        game_name = result["game"]
        if not game_name:
            result["error"] = "game 名为空"
            result["matches"] = []
            return result
        games = self._data.get("games", {}) if isinstance(self._data.get("games"), dict) else {}
        game_record = games.get(game_name, {}) if isinstance(games, dict) else {}
        if not isinstance(game_record, dict):
            game_record = {}
        facts = game_record.get("facts", [])
        if not isinstance(facts, list):
            facts = []
        result["total_facts"] = len(facts)
        summary = str(game_record.get("summary") or "").strip()
        if summary:
            result["long_term_summary"] = summary[:600]

        keyword_list = [kw.strip().casefold() for kw in re.split(r"[\s,，;；]+", keywords or "") if kw.strip()]
        result["keywords"] = keyword_list

        def _match_text(text: str) -> bool:
            if not keyword_list:
                return True
            normalized = text.casefold()
            return all(kw in normalized for kw in keyword_list)

        matches: list[dict[str, Any]] = []
        for entry in reversed(facts):  # 倒序遍历,新的优先
            if len(matches) >= result["limit"]:
                break
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "")
            if not text or not _match_text(text):
                continue
            matches.append(
                {
                    "time": str(entry.get("time") or ""),
                    "text": text,
                    "source": str(entry.get("source") or ""),
                    "url": str(entry.get("url") or ""),
                    "group_id": str(entry.get("group_id") or ""),
                    "tags": list(entry.get("tags") or []),
                }
            )
        result["matches"] = matches
        result["match_count"] = len(matches)
        if not facts:
            result["note"] = f"游戏『{game_name}』暂无任何事实,可以 web_search 后自动收集,或主 AI 主动调 catty_game_remember 写入"
        return result

    def list_games(self) -> list[str]:
        games = self._data.get("games", {}) if isinstance(self._data.get("games"), dict) else {}
        if not isinstance(games, dict):
            return []
        return sorted(games.keys())

    def collect_recent_image_descriptions(
        self,
        event: MessageEvent,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """从当前 scope corpus 拉最近 ``limit`` 条 has_image=True 的条目。

        给 _build_messages 在用户回指『刚才那张图/认得这张吗』时强 inject 用,
        让主 AI 直接看到最近图片的 vision 描述,不用再调 catty_recall。
        """
        if not self.enabled:
            return []
        if isinstance(event, GroupMessageEvent):
            group = self._data.get("groups", {}).get(str(event.group_id), {})
            corpus = group.get("corpus", []) if isinstance(group, dict) else []
        elif isinstance(event, PrivateMessageEvent):
            user = self._data.get("users", {}).get(str(event.user_id), {})
            corpus = user.get("private_corpus", []) if isinstance(user, dict) else []
        else:
            return []
        if not isinstance(corpus, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in reversed(corpus):
            if not isinstance(entry, dict):
                continue
            if not entry.get("has_image"):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            out.append(
                {
                    "time": str(entry.get("time") or ""),
                    "user_id": str(entry.get("user_id") or ""),
                    "display_name": str(entry.get("display_name") or ""),
                    "text": text[:240],
                }
            )
            if len(out) >= max(1, int(limit)):
                break
        return out

    # ── 游戏摘要周期压缩 ───────────────────────────────────────────────

    def due_games_for_summary(self) -> list[str]:
        """返回需要 LLM 压缩的游戏名列表。

        触发条件(满足其一即可):
        - facts 数 >= game_summary_min_facts 且距上次摘要 >= interval
        - 游戏单文件大小 >= game_size_compress_threshold_bytes(默认 200KB)且 facts 数 > keep_recent

        新游戏无 last_summary_at 视为永远 due,只要 facts 攒够就压。
        """
        if not self.enabled:
            return []
        games = self._data.get("games", {})
        if not isinstance(games, dict):
            return []
        now = datetime.now(timezone.utc)
        size_threshold = max(
            int(getattr(self, "game_size_compress_threshold_bytes", 200_000) or 200_000),
            10_000,
        )
        due: list[str] = []
        for game_name, game_record in games.items():
            if not isinstance(game_record, dict):
                continue
            facts = game_record.get("facts", [])
            if not isinstance(facts, list):
                continue
            facts_count = len(facts)

            # 触发条件 1:数量阈值 + 时间间隔
            if facts_count >= self.game_summary_min_facts:
                last = _parse_time(game_record.get("last_summary_at"))
                if last is None or (now - last).total_seconds() >= self.game_summary_interval_minutes * 60:
                    due.append(str(game_name))
                    continue

            # 触发条件 2:单文件 >= 200KB 且还有可压缩的旧 facts
            if facts_count <= self.game_keep_recent_facts:
                continue
            try:
                size = self._game_file(str(game_name)).stat().st_size
            except OSError:
                size = 0
            if size >= size_threshold:
                due.append(str(game_name))
        return due

    # ── 群-游戏标签 ────────────────────────────────────────────────────

    def tag_group_with_game(
        self,
        group_id: str,
        game: str,
        *,
        confidence: int = 80,
        reason: str = "",
    ) -> dict[str, Any]:
        """给一个群打"和某游戏相关"的标签。主 AI 通过 catty_group_game_tag 调用。

        confidence < 60 拒绝写入,避免把不确定的归类污染长期记忆。
        同游戏重复 tag 会更新 confidence(取较大值)和 last_updated。
        """
        if not self.enabled:
            return {"ok": False, "error": "memory disabled"}
        group_id = str(group_id).strip()
        game_name = self._normalize_game_name(game)
        if not group_id:
            return {"ok": False, "error": "group_id 为空"}
        if not game_name:
            return {"ok": False, "error": "game 名为空"}
        confidence = max(0, min(int(confidence or 0), 100))
        if confidence < 60:
            return {
                "ok": False,
                "error": f"confidence={confidence} 太低,至少需要 60 才打标签。如果你不确定,先 catty_recall 看看历史语料再决定。",
            }
        tags_root = self._data.setdefault("group_game_tags", {})
        if not isinstance(tags_root, dict):
            tags_root = {}
            self._data["group_game_tags"] = tags_root
        group_entry = tags_root.setdefault(group_id, {"games": {}, "first_tagged_at": _now()})
        if not isinstance(group_entry, dict):
            group_entry = {"games": {}, "first_tagged_at": _now()}
            tags_root[group_id] = group_entry
        games_map = group_entry.setdefault("games", {})
        if not isinstance(games_map, dict):
            games_map = {}
            group_entry["games"] = games_map
        existing = games_map.get(game_name)
        if isinstance(existing, dict):
            existing["confidence"] = max(int(existing.get("confidence") or 0), confidence)
            existing["last_updated"] = _now()
            if reason:
                existing["last_reason"] = str(reason)[:240]
        else:
            games_map[game_name] = {
                "confidence": confidence,
                "first_tagged_at": _now(),
                "last_updated": _now(),
                "last_reason": str(reason or "")[:240],
            }
        group_entry["last_updated"] = _now()
        self._save()
        return {
            "ok": True,
            "group_id": group_id,
            "game": game_name,
            "confidence": games_map[game_name]["confidence"],
            "total_games_in_group": len(games_map),
        }

    def remove_group_game_tag(self, group_id: str, game: str) -> bool:
        if not self.enabled:
            return False
        group_id = str(group_id).strip()
        game_name = self._normalize_game_name(game)
        tags_root = self._data.get("group_game_tags", {})
        if not isinstance(tags_root, dict):
            return False
        group_entry = tags_root.get(group_id)
        if not isinstance(group_entry, dict):
            return False
        games_map = group_entry.get("games", {})
        if not isinstance(games_map, dict) or game_name not in games_map:
            return False
        games_map.pop(game_name, None)
        if not games_map:
            tags_root.pop(group_id, None)
        else:
            group_entry["last_updated"] = _now()
        self._save()
        return True

    def get_group_games(self, group_id: str | int | None, *, min_confidence: int = 60) -> list[str]:
        """返回这个群被标记的所有游戏名(置信度过滤后,按 confidence 倒序)。"""
        if not self.enabled or group_id is None:
            return []
        tags_root = self._data.get("group_game_tags", {})
        if not isinstance(tags_root, dict):
            return []
        group_entry = tags_root.get(str(group_id))
        if not isinstance(group_entry, dict):
            return []
        games_map = group_entry.get("games", {})
        if not isinstance(games_map, dict):
            return []
        scored: list[tuple[int, str]] = []
        for game_name, info in games_map.items():
            if not isinstance(info, dict):
                continue
            conf = int(info.get("confidence") or 0)
            if conf < min_confidence:
                continue
            scored.append((conf, str(game_name)))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [name for _conf, name in scored]

    def build_dynamic_game_context(self, game: str, *, recent_facts_limit: int = 6) -> str:
        """拼一个游戏的动态记忆 context:long_term_summary + 最近 N 条 facts。

        给 _build_messages 调用,把 game_memory 里 AI 自己写入的内容注入主回复 prompt。
        留空表示这个游戏没有任何动态记忆,调用方可以直接不挂这段。
        """
        if not self.enabled:
            return ""
        game_name = self._normalize_game_name(game)
        if not game_name:
            return ""
        games = self._data.get("games", {})
        if not isinstance(games, dict):
            return ""
        record = games.get(game_name)
        if not isinstance(record, dict):
            return ""
        summary = str(record.get("summary") or "").strip()
        facts = record.get("facts", [])
        if not isinstance(facts, list):
            facts = []
        lines: list[str] = []
        if summary:
            lines.append(f"长期摘要:{summary[:800]}")
        recent: list[str] = []
        for entry in reversed(facts):
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            recent.append(f"- {text[:200]}")
            if len(recent) >= max(1, int(recent_facts_limit)):
                break
        if recent:
            lines.append("最近事实:")
            lines.extend(recent)
        return "\n".join(lines).strip()

    def build_game_summary_messages(self, game_name: str) -> list[dict[str, object]]:
        """构造给 LLM 的游戏摘要 prompt。压缩前 (n - keep_recent) 条 facts 为 summary。"""
        game_name = self._normalize_game_name(game_name)
        games = self._data.get("games", {})
        game_record = games.get(game_name, {}) if isinstance(games, dict) else {}
        if not isinstance(game_record, dict):
            game_record = {}
        old_summary = str(game_record.get("summary") or "").strip()
        facts = game_record.get("facts", [])
        if not isinstance(facts, list):
            facts = []
        # 待压缩 = facts 全部除最近 keep_recent_facts 条
        keep = self.game_keep_recent_facts
        to_compress = facts[: max(0, len(facts) - keep)]

        lines: list[str] = []
        for entry in to_compress:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            source = str(entry.get("source") or "").strip()
            url = str(entry.get("url") or "").strip()
            tags = entry.get("tags") or []
            tag_str = "|".join(str(t) for t in tags) if isinstance(tags, list) else ""
            extras = []
            if source:
                extras.append(f"src={source}")
            if url:
                extras.append(f"url={url[:80]}")
            if tag_str:
                extras.append(f"tags={tag_str}")
            extra = f" [{', '.join(extras)}]" if extras else ""
            lines.append(f"- {text}{extra}")

        prompt = (
            "压缩游戏长期记忆,只输出 JSON:"
            '{"summary":"<=2000字"}。'
            "把待压缩条目里**反复出现的事实、版本变化、角色机制、活动规则、玩家共识**整合成结构化摘要,"
            "按主题(角色/版本/活动/机制/玩家)分类列出。"
            "**剔除掉**单次玩笑、临时情绪、过时信息、未确认传言。"
            "如果旧摘要已有相关条目,新事实优先覆盖旧的。"
            "保留具体的版本号、角色名、数字、来源 url(简短引用即可)。"
            "不要 Markdown,不要 emoji,不要解释你做了什么。"
        )
        user_content = (
            f"游戏:{game_name}\n"
            f"旧摘要:{old_summary or '无'}\n"
            f"待压缩 {len(lines)} 条事实(最近 {min(len(facts), keep)} 条保留为原始记录,不在这里):\n"
            + "\n".join(lines)
        )
        return [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}]

    def save_game_summary(self, game_name: str, summary: str) -> None:
        """写入压缩后的 summary;只保留最近 keep_recent_facts 条原始 facts。"""
        if not self.enabled:
            return
        game_name = self._normalize_game_name(game_name)
        if not game_name:
            return
        games = self._data.setdefault("games", {})
        game_record = games.setdefault(game_name, {})
        if not isinstance(game_record, dict):
            game_record = {}
            games[game_name] = game_record

        parsed = _extract_json_object(summary)
        if parsed is None:
            game_record["summary"] = str(summary or "").strip()[:4000]
        else:
            game_record["summary"] = str(parsed.get("summary") or "").strip()[:4000]

        facts = game_record.get("facts", [])
        if isinstance(facts, list) and len(facts) > self.game_keep_recent_facts:
            game_record["facts"] = facts[-self.game_keep_recent_facts :]
        game_record["last_summary_at"] = _now()
        self._save()

    # ── 长期备忘 notes(AI 主动通过 catty_remember 写入) ──────────────
    # 数据结构:user[uid]["notes"] / group[gid]["notes"] = [{time, text, expires_at?, tags?}]
    # 和 corpus(对话记录) / impression(画像) 区分:notes 是"AI 当场决定要长期记住的事实",
    # 例如"这个群友讨厌被叫笨蛋""主人下周日要 raid""猫粉俱乐部群"。
    # 自动过期 + LRU 截断:每个实体最多 _NOTES_MAX 条,过期不返回但保留 7 天后才物理删。

    _NOTES_MAX_PER_ENTITY = 50
    _NOTES_DEFAULT_TTL_DAYS = 30

    def record_note(
        self,
        *,
        scope: str,
        text: str,
        user_id: str = "",
        group_id: str = "",
        ttl_days: int | None = None,
        tags: list[str] | None = None,
        event_date: str = "",
    ) -> dict[str, Any]:
        """写一条长期备忘 note。

        scope = "user" → 写到 user[user_id]["notes"]
        scope = "group" → 写到 group[group_id]["notes"]

        text 限 200 字。同 text 在未过期范围内不重复写入。
        ttl_days 不传走 _NOTES_DEFAULT_TTL_DAYS(30 天);0 表示永久。
        event_date(ISO YYYY-MM-DD)如果传:
        - 自动算 ttl_days = (event_date - today).days + 7 (事件日期 + 7 天缓冲)
        - 存到 entry["event_date"], build_context 会优先展示并显示倒计时
        - 已过的事件日期 ttl 仍保留 7 天给 AI 回忆"刚过去的事"
        """
        if not self.enabled:
            return {"ok": False, "error": "memory disabled"}
        clean_text = str(text or "").strip()
        if not clean_text:
            return {"ok": False, "error": "text 不能为空"}
        if len(clean_text) > 200:
            clean_text = clean_text[:200]
        scope = (scope or "").strip().lower()
        if scope == "user":
            if not user_id:
                return {"ok": False, "error": "scope=user 必须传 user_id"}
            container = self._data.setdefault("users", {}).setdefault(str(user_id), {})
        elif scope == "group":
            if not group_id:
                return {"ok": False, "error": "scope=group 必须传 group_id"}
            container = self._data.setdefault("groups", {}).setdefault(str(group_id), {})
        else:
            return {"ok": False, "error": "scope 必须是 user 或 group"}

        notes = container.setdefault("notes", [])
        if not isinstance(notes, list):
            notes = []
            container["notes"] = notes

        # 如果传了 event_date,根据它推 ttl_days(覆盖 caller 传的 ttl_days)
        parsed_event_date = ""
        if event_date:
            try:
                ev = datetime.fromisoformat(str(event_date).strip()).date()
                today = _utc_now().date()
                derived_ttl = max((ev - today).days + 7, 7)  # 至少 7 天保留(已过事件留给 AI 回忆)
                ttl_days = derived_ttl
                parsed_event_date = ev.isoformat()
            except ValueError:
                # 非法日期:忽略 event_date,仍按原 ttl_days 走
                parsed_event_date = ""

        ttl = self._NOTES_DEFAULT_TTL_DAYS if ttl_days is None else max(int(ttl_days), 0)
        now_iso = _now()
        expires_at = ""
        if ttl > 0:
            expires_at = (_utc_now() + timedelta(days=ttl)).isoformat(timespec="seconds")

        # 去重:同 text 命中已有(且未过期)条目 → 仅刷新 last_seen
        now_dt = _utc_now()
        for existing in notes:
            if not isinstance(existing, dict):
                continue
            if str(existing.get("text") or "").strip() == clean_text:
                exp = _parse_time(existing.get("expires_at") or "")
                if exp is None or _as_aware_utc(exp) > now_dt:
                    existing["last_seen"] = now_iso
                    if expires_at and (exp is None or _as_aware_utc(exp) < now_dt + timedelta(days=ttl // 2)):
                        existing["expires_at"] = expires_at
                    self._save()
                    return {
                        "ok": True, "deduplicated": True,
                        "scope": scope, "count": len(notes),
                    }

        entry: dict[str, Any] = {"time": now_iso, "text": clean_text}
        if expires_at:
            entry["expires_at"] = expires_at
        if tags:
            entry["tags"] = [str(t).strip() for t in tags if str(t).strip()][:6]
        if parsed_event_date:
            entry["event_date"] = parsed_event_date
        notes.append(entry)
        if len(notes) > self._NOTES_MAX_PER_ENTITY:
            container["notes"] = notes[-self._NOTES_MAX_PER_ENTITY :]
        self._save()
        return {"ok": True, "scope": scope, "count": len(container["notes"])}

    def _active_notes(self, container: dict[str, Any]) -> list[dict[str, Any]]:
        """从 container[notes] 拿未过期条目,按时间倒序(最新在前)。"""
        notes = container.get("notes")
        if not isinstance(notes, list):
            return []
        now_dt = _utc_now()
        active: list[dict[str, Any]] = []
        for entry in notes:
            if not isinstance(entry, dict):
                continue
            exp_raw = entry.get("expires_at")
            if exp_raw:
                exp = _parse_time(exp_raw)
                if exp is not None and _as_aware_utc(exp) <= now_dt:
                    continue
            active.append(entry)
        active.sort(key=lambda e: str(e.get("time") or ""), reverse=True)
        return active

    def recall_notes(
        self,
        *,
        user_id: str = "",
        group_id: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """读未过期 notes。可单读用户/群,也可同时读(返回字典分桶)。"""
        if not self.enabled:
            return {"user_notes": [], "group_notes": []}
        limit = max(min(int(limit or 10), 50), 1)
        out: dict[str, Any] = {}
        if user_id:
            user = self._data.get("users", {}).get(str(user_id), {})
            if isinstance(user, dict):
                out["user_notes"] = self._active_notes(user)[:limit]
            else:
                out["user_notes"] = []
        if group_id:
            group = self._data.get("groups", {}).get(str(group_id), {})
            if isinstance(group, dict):
                out["group_notes"] = self._active_notes(group)[:limit]
            else:
                out["group_notes"] = []
        return out

    def _notes_context_line(self, container: dict[str, Any], *, label: str, limit: int = 5) -> str:
        """build_context 用:把 active notes 拼成一行简短描述。

        带 event_date 的 note 优先排在前面,并显示倒计时『还剩 N 天』/『今天』/『已过 N 天』。
        """
        active = self._active_notes(container)
        if not active:
            return ""
        # 按"有 event_date 的优先 + event_date 越近越前" 重排
        today = _utc_now().date()

        def _sort_key(n: dict[str, Any]) -> tuple[int, int]:
            ev_raw = n.get("event_date")
            if not ev_raw:
                return (1, 0)  # 无 event_date 排后
            try:
                ev = datetime.fromisoformat(str(ev_raw)).date()
                days = (ev - today).days
                # 未来的优先(days >= 0 排在 days < 0 之前),近的越前
                if days >= 0:
                    return (0, days)
                else:
                    return (2, -days)  # 已过的排最后
            except ValueError:
                return (1, 0)

        active.sort(key=_sort_key)
        chosen = active[:limit]
        snippets: list[str] = []
        for n in chosen:
            text = str(n.get("text") or "").strip()
            if not text:
                continue
            ev_raw = n.get("event_date")
            if ev_raw:
                try:
                    ev = datetime.fromisoformat(str(ev_raw)).date()
                    days = (ev - today).days
                    if days == 0:
                        prefix = "[今天]"
                    elif days == 1:
                        prefix = "[明天]"
                    elif days > 1:
                        prefix = f"[还剩{days}天]"
                    else:
                        prefix = f"[已过{-days}天]"
                    snippets.append(f"{prefix}{text[:70]}")
                    continue
                except ValueError:
                    pass
            snippets.append(text[:80])
        if not snippets:
            return ""
        return f"- {label}: " + "; ".join(snippets)
