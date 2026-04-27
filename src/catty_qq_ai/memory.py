from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .config import Config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _sender_name(event: MessageEvent) -> str:
    sender: Any = getattr(event, "sender", None)
    for attr in ("card", "nickname"):
        value = getattr(sender, attr, "") if sender is not None else ""
        if value:
            return str(value)
    return str(event.user_id)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            loaded = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


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
        self.max_known_members = max(config.catty_memory_max_known_members, 0)
        self.special_group_ids = {str(group_id) for group_id in config.catty_memory_special_group_ids}
        self.summary_interval_minutes = max(config.catty_memory_summary_interval_minutes, 1)
        self.max_corpus_messages = max(config.catty_memory_max_corpus_messages, 20)
        self.private_summary_messages = max(config.catty_memory_private_summary_messages, 1)
        self.member_mention_threshold = max(config.catty_memory_member_mention_threshold, 1)
        self.group_titles = dict(config.catty_group_titles)
        self.user_titles = dict(config.catty_user_titles)
        self.group_user_titles = dict(config.catty_group_user_titles)
        self._data: dict[str, Any] = {"users": {}, "groups": {}}
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

    def _load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                self._data["users"] = loaded.get("users", {}) if isinstance(loaded.get("users", {}), dict) else {}
                self._data["groups"] = loaded.get("groups", {}) if isinstance(loaded.get("groups", {}), dict) else {}
        self._load_entity_files(self.user_storage_dir, "user_id", "user_", self._data.setdefault("users", {}))
        self._load_entity_files(self.group_storage_dir, "group_id", "group_", self._data.setdefault("groups", {}))

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
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        index = {
            "version": 2,
            "users": {},
            "groups": {},
            "user_ids": sorted(str(user_id) for user_id in self._data.get("users", {})),
            "group_ids": sorted(str(group_id) for group_id in self._data.get("groups", {})),
            "user_storage_dir": str(self.user_storage_dir),
            "group_storage_dir": str(self.group_storage_dir),
        }
        self.path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for user_id, user in self._data.get("users", {}).items():
            if isinstance(user, dict):
                self._write_entity_file(self._user_file(str(user_id)), "user_id", str(user_id), user)
        for group_id, group in self._data.get("groups", {}).items():
            if isinstance(group, dict):
                self._write_entity_file(self._group_file(str(group_id)), "group_id", str(group_id), group)

    def _write_entity_file(self, path: Path, id_key: str, entity_id: str, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            id_key: entity_id,
            "updated_at": _now(),
            "data": data,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
        entry = {
            "time": _now(),
            "user_id": str(event.user_id),
            "display_name": _sender_name(event),
            "text": content,
            "has_image": has_image,
        }
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
        corpus.append({"time": _now(), "display_name": _sender_name(event), "text": content, "has_image": has_image})
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

    def _corpus_lines(self, corpus: list[Any], limit: int) -> list[str]:
        lines: list[str] = []
        for item in corpus[-limit:]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("display_name") or item.get("user_id") or "群友")
            user_id = str(item.get("user_id") or "")
            text = str(item.get("text") or "")
            image = "+图" if item.get("has_image") else ""
            lines.append(f"{name}({user_id}{image}): {text}")
        return lines

    def build_summary_messages(self, group_id: str) -> list[dict[str, object]]:
        group = self._data.get("groups", {}).get(group_id, {})
        corpus = group.get("corpus", []) if isinstance(group, dict) else []
        old_summary = str(group.get("summary") or "") if isinstance(group, dict) else ""
        old_profiles = group.get("member_profiles", {}) if isinstance(group, dict) else {}
        lines = self._corpus_lines(corpus, min(self.max_corpus_messages, 300))
        prompt = (
            '压缩QQ群长期记忆，省token。只输出JSON：'
            '{"summary":"<=300字","members":[{"user_id":"QQ","display_name":"名","gender":"男/女/未知","title":"称呼","impression":"<=30字","confidence":"低/中/高"}]}。'
            "只写有证据的信息；性别不确定写未知；不要Markdown/emoji。"
        )
        user_content = (
            f"群:{group_id}\n旧摘要:{old_summary or '无'}\n旧画像:{json.dumps(old_profiles, ensure_ascii=False)}\n"
            + "\n".join(lines)
        )
        return [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}]

    def build_private_summary_messages(self, user_id: str) -> list[dict[str, object]]:
        user = self._data.get("users", {}).get(user_id, {})
        corpus = user.get("private_corpus", []) if isinstance(user, dict) else []
        old_summary = str(user.get("private_summary") or "") if isinstance(user, dict) else ""
        old_profile = user.get("private_profile", {}) if isinstance(user, dict) else {}
        display_name = str(user.get("display_name") or user_id) if isinstance(user, dict) else user_id
        lines = self._corpus_lines(corpus, self.private_summary_messages)
        prompt = (
            '压缩QQ私聊记忆，省token。只输出JSON：'
            '{"summary":"<=300字","profile":{"gender":"男/女/未知","title":"称呼","impression":"<=30字","confidence":"低/中/高"}}。'
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
            '{"user_id":"QQ","gender":"男/女/未知","title":"称呼","impression":"<=40字","evidence":"<=60字","confidence":"低/中/高"}。'
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
                    self._save_member_profile(profiles, members, raw_member)
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
                    "inferred_title": str(raw_profile.get("title") or "").strip() or "群友",
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
        self._save_member_profile(profiles, members, parsed)
        mention_data = group.setdefault("mention_profiles", {}).setdefault(user_id, {})
        mention_data["last_summary_at"] = _now()
        mention_data["corpus"] = []
        mention_data["count"] = 0
        self._save()

    def _save_member_profile(self, profiles: dict[str, Any], members: dict[str, Any], raw_member: Any) -> None:
        if not isinstance(raw_member, dict):
            return
        user_id = str(raw_member.get("user_id") or "").strip()
        if not user_id:
            return
        profile = {
            "display_name": str(raw_member.get("display_name") or "").strip(),
            "gender": _clean_gender(raw_member.get("gender")),
            "inferred_title": str(raw_member.get("title") or "").strip() or "群友",
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

    def _title_for(self, user_id: str, group_id: str | None = None) -> str:
        if group_id and self.group_user_titles.get(group_id, {}).get(user_id):
            return self.group_user_titles[group_id][user_id]
        if user_id in self.user_titles:
            return self.user_titles[user_id]
        if group_id:
            group = self._data.get("groups", {}).get(group_id, {})
            member = group.get("members", {}).get(user_id, {}) if isinstance(group, dict) else {}
            for key in ("title", "inferred_title"):
                title = member.get(key) if isinstance(member, dict) else None
                if title:
                    return str(title)
            profile = group.get("member_profiles", {}).get(user_id, {}) if isinstance(group, dict) else {}
            profile_title = profile.get("inferred_title") if isinstance(profile, dict) else None
            if profile_title:
                return str(profile_title)
            group_title = self.group_titles.get(group_id) or group.get("title") if isinstance(group, dict) else None
            if group_title:
                return str(group_title)
        user = self._data.get("users", {}).get(user_id, {})
        if isinstance(user, dict):
            for source in (user, user.get("private_profile", {})):
                if isinstance(source, dict):
                    title = source.get("title") or source.get("inferred_title")
                    if title:
                        return str(title)
        return "群友"

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
                known.append(f"- {display_name}({member_id})：{self._title_for(str(member_id), group_id)}；{impression}")
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
            group = self._data.get("groups", {}).get(group_id, {})
            if isinstance(group, dict):
                summary = str(group.get("summary") or "").strip()
                if summary:
                    lines.append("- 群摘要：" + summary)
                profile = self._profile_for(user_id, group_id)
                if profile:
                    lines.append(
                        f"- 当前用户画像：性别={_clean_gender(profile.get('gender'))}，"
                        f"印象={str(profile.get('impression') or '暂无')[:80]}。"
                    )
                members = group.get("members", {})
                known: list[str] = []
                if isinstance(members, dict) and self.max_known_members:
                    for member_id, member in list(members.items())[-self.max_known_members:]:
                        if not isinstance(member, dict):
                            continue
                        display_name = str(member.get("display_name") or member_id)
                        member_title = self._title_for(str(member_id), group_id)
                        profile = self._profile_for(str(member_id), group_id)
                        impression = str(profile.get("impression") or "").strip()
                        known.append(f"{display_name}({member_id})=>{member_title}/{impression[:30]}")
                if known:
                    lines.append("- 已知群友：" + "；".join(known))
        elif isinstance(event, PrivateMessageEvent):
            title = self._title_for(user_id)
            lines.append(f"- 当前是私聊；优先称呼当前用户为「{title}」。")
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

        lines.append("- 自然使用称呼，不要每句话都堆称呼。性别未知或低置信度时用中性称呼。")
        lines.append("- 如果用户明确要求查看已存储记忆、群友画像或人物信息，可以依据本段记忆回答；没有记录时直说没有。")
        lines.append("- 默认不用 emoji/颜文字，除非用户明确要求。")
        return "\n".join(lines)
