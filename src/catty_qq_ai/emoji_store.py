from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlparse

from .config import Config


EMOJI_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
QUERY_EDGE_PUNCTUATION = " \t\r\n。！？!?；;，,、：:….'\"`“”‘’（）()[]【】<>《》"
logger = logging.getLogger(__name__)

EMOJI_QUERY_ALIASES = {
    "开心": ["高兴", "唐猫高兴", "喵喵喵", "欸嘿", "起飞喵"],
    "高兴": ["开心", "唐猫高兴", "喵喵喵", "欸嘿", "起飞喵"],
    "喜欢": ["我喜欢你喵", "贴贴", "喵喵看着你"],
    "贴贴": ["我喜欢你喵", "喜欢", "喵喵看着你"],
    "撒娇": ["我喜欢你喵", "喵喵喵", "喵喵看着你"],
    "害羞": ["事后喵", "我喜欢你喵", "色诱喵"],
    "涩": ["事后喵", "色诱喵"],
    "色": ["事后喵", "色诱喵"],
    "疑惑": ["何意味", "何意味？", "喵喵看着你"],
    "问号": ["何意味", "何意味？"],
    "震惊": ["何意味", "拍桌跳", "喵喵喵"],
    "无语": ["无语喵", "唐猫不屑"],
    "嫌弃": ["唐猫不屑", "无语喵"],
    "不屑": ["唐猫不屑", "敢惹我"],
    "生气": ["炸毛喵", "敢惹我", "给你一拳喵"],
    "炸毛": ["炸毛喵", "敢惹我", "给你一拳喵"],
    "打": ["给你一拳喵", "挨打喵", "拍桌跳"],
    "哭": ["哭哭喵", "燃尽了喵"],
    "难过": ["哭哭喵", "燃尽了喵"],
    "累": ["燃尽了喵", "睡觉喵", "睡觉了喵"],
    "睡": ["睡觉喵", "睡觉了喵"],
    "出击": ["出击喵", "火力大喵", "起飞喵"],
    "自豪": ["自豪喵", "唐猫高兴"],
}


@dataclass(slots=True)
class EmojiEntry:
    path: Path
    meaning: str
    tags: list[str]
    source: str
    priority: int
    category: str = ""
    use_when: list[str] = field(default_factory=list)
    avoid_when: list[str] = field(default_factory=list)
    intensity: str = "medium"
    motion: str = "static"


def _safe_tokens(text: str) -> list[str]:
    text = text.strip(QUERY_EDGE_PUNCTUATION)
    tokens = re.split(r"[\s,，、;；|_\\/\-.]+", text.lower())
    result = [token for token in tokens if token]
    compact = "".join(result)
    expanded: list[str] = []
    for token in [*result, compact]:
        if not token:
            continue
        for keyword, aliases in EMOJI_QUERY_ALIASES.items():
            keyword_lower = keyword.lower()
            if keyword_lower in token or token in keyword_lower:
                expanded.extend(alias.lower() for alias in aliases)
    for token in expanded:
        if token and token not in result:
            result.append(token)
    return result


def _clean_query(text: str) -> str:
    return text.strip(QUERY_EDGE_PUNCTUATION)


def _metadata_list(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif value:
        values = [value]
    else:
        values = []
    result: list[str] = []
    for item in values:
        item = str(item).strip()
        if item and item not in result:
            result.append(item)
    return result


def _field_match_score(wanted: set[str], values: list[str], *, weight: int) -> int:
    if not wanted or not values:
        return 0
    haystack_tokens: set[str] = set()
    haystack_text: list[str] = []
    for value in values:
        clean_value = str(value or "").strip()
        if not clean_value:
            continue
        haystack_tokens.update(_safe_tokens(clean_value))
        haystack_text.append(clean_value.lower())
    token_hits = len(wanted & haystack_tokens)
    fuzzy_hits = sum(
        1
        for token in wanted
        if token not in haystack_tokens and any(token in text or text in token for text in haystack_text)
    )
    return weight * token_hits + (weight // 2) * fuzzy_hits


def _match_score(
    query: str,
    *,
    wanted_tags: list[str] | None = None,
    haystack_tags: list[str] | None = None,
    meaning: str,
    base_score: int = 0,
) -> int:
    wanted = set(_safe_tokens(query))
    for tag in wanted_tags or []:
        wanted.update(_safe_tokens(tag))
    if not wanted:
        return 0

    haystack = set(_safe_tokens(meaning))
    for tag in haystack_tags or []:
        haystack.update(_safe_tokens(tag))
    haystack_text = {meaning.lower(), *[tag.lower() for tag in haystack_tags or []]}
    token_hits = len(wanted & haystack)
    fuzzy_hits = sum(
        1
        for token in wanted
        if token not in haystack and any(token in text or text in token for text in haystack_text)
    )
    if token_hits == 0 and fuzzy_hits == 0:
        return 0
    return base_score + 40 * token_hits + 25 * fuzzy_hits


def _extension_from(content_type: str, source_url: str) -> str:
    content_type = content_type.lower()
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    if "bmp" in content_type:
        return ".bmp"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    suffix = Path(urlparse(source_url).path).suffix.lower()
    if suffix in EMOJI_EXTENSIONS:
        return suffix
    return ".jpg"


class EmojiStore:
    def __init__(
        self,
        config: Config,
        *,
        root: str | Path | None = None,
        download_dir: str | Path | None = None,
        manifest_path: str | Path | None = None,
        allow_downloads: bool | None = None,
        bundled_root: str | Path | None = None,
        allow_bundled_fallback: bool = True,
    ) -> None:
        self.enabled = config.catty_emoji_enabled
        self.root = Path(root if root is not None else config.catty_emoji_dir).expanduser()
        self.download_dir = Path(
            download_dir if download_dir is not None else config.catty_emoji_download_dir
        ).expanduser()
        self.manifest_path = Path(
            manifest_path if manifest_path is not None else config.catty_emoji_manifest_path
        ).expanduser()
        self.allow_downloads = (
            bool(allow_downloads)
            if allow_downloads is not None
            else bool(getattr(config, "catty_emoji_allow_downloads", True))
        )
        self.bundled_root = (
            Path(bundled_root).expanduser() if bundled_root is not None else None
        )
        self.allow_bundled_fallback = bool(allow_bundled_fallback)
        self.max_candidates = max(int(config.catty_emoji_max_candidates), 1)
        self._entries: list[EmojiEntry] = []
        self._manifest: dict[str, Any] = {"version": 1, "emojis": {}}
        if self.enabled:
            self.refresh()

    def _has_emoji_files(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        excluded_download_dir: Path | None = None
        if not self.allow_downloads:
            try:
                download_relative = self.download_dir.resolve().relative_to(self.root.resolve())
                excluded_download_dir = (path / download_relative).resolve()
            except ValueError:
                if path.resolve() == self.root.resolve():
                    excluded_download_dir = self.download_dir.resolve()
        for item in path.rglob("*"):
            if not item.is_file() or item.suffix.lower() not in EMOJI_EXTENSIONS:
                continue
            if excluded_download_dir is not None and item.resolve().is_relative_to(excluded_download_dir):
                continue
            return True
        return False

    def _use_bundled_root_if_needed(self) -> None:
        if self._has_emoji_files(self.root) or not self.allow_bundled_fallback:
            return
        bundled_root = self.bundled_root
        if bundled_root is None:
            bundle_root_value = getattr(sys, "_MEIPASS", "")
            if not bundle_root_value:
                return
            bundled_root = Path(str(bundle_root_value)) / "emojis"
        if self._has_emoji_files(bundled_root):
            logger.info("Using bundled emoji directory: %s", bundled_root)
            self.root = bundled_root

    def refresh(self) -> None:
        self._use_bundled_root_if_needed()
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
        if self.allow_downloads:
            self.download_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._manifest = self._load_manifest()
        self._scan_files()
        manifest_count = len(self._manifest.get("emojis", {})) if isinstance(self._manifest.get("emojis"), dict) else 0
        if manifest_count and not self._entries:
            logger.warning(
                "Emoji manifest has %s entries but no listed image files were found under %s. "
                "Put the referenced jpg/png/gif/webp files in emoji.dir or update config.json.",
                manifest_count,
                self.root,
            )
        self._save_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return {"version": 1, "emojis": {}}
        try:
            loaded = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "emojis": {}}
        if not isinstance(loaded, dict):
            return {"version": 1, "emojis": {}}
        emojis = loaded.get("emojis")
        if not isinstance(emojis, dict):
            loaded["emojis"] = {}
        categories = loaded.get("categories")
        if categories is not None and not isinstance(categories, dict):
            loaded["categories"] = {}
        loaded.setdefault("version", 1)
        return loaded

    def _save_manifest(self) -> None:
        if not self.enabled:
            return
        self.manifest_path.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _relative_key(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.name

    def _default_meta(self, path: Path, source: str) -> dict[str, Any]:
        tags = _safe_tokens(path.stem)
        return {
            "meaning": path.stem,
            "tags": tags,
            "source": source,
            "priority": 100 if source == "default" else 50,
        }

    def _entry_metadata(self, meta: dict[str, Any]) -> tuple[str, list[str], list[str], str, str]:
        category = str(meta.get("category") or "").strip()
        categories = self._manifest.get("categories")
        category_meta = categories.get(category) if isinstance(categories, dict) else None
        if not isinstance(category_meta, dict):
            category_meta = {}
        use_when = _metadata_list(category_meta.get("use_when"))
        use_when.extend(item for item in _metadata_list(meta.get("use_when")) if item not in use_when)
        avoid_when = _metadata_list(category_meta.get("avoid_when"))
        avoid_when.extend(item for item in _metadata_list(meta.get("avoid_when")) if item not in avoid_when)
        intensity = str(meta.get("intensity") or "medium").strip() or "medium"
        motion = str(meta.get("motion") or "static").strip() or "static"
        return category, use_when, avoid_when, intensity, motion

    def _scan_files(self) -> None:
        emojis = self._manifest.setdefault("emojis", {})
        if not isinstance(emojis, dict):
            emojis = {}
            self._manifest["emojis"] = emojis
        entries: list[EmojiEntry] = []
        download_root = self.download_dir.resolve()
        skipped_unindexed = 0
        auto_registered = 0
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in EMOJI_EXTENSIONS:
                continue
            key = self._relative_key(path)
            try:
                path.resolve().relative_to(download_root)
                source = "downloaded"
            except ValueError:
                source = "default"
            if source == "downloaded" and not self.allow_downloads:
                continue
            meta = emojis.get(key)
            if not isinstance(meta, dict):
                if source == "default":
                    meta = self._default_meta(path, source)
                    emojis[key] = meta
                    auto_registered += 1
                else:
                    skipped_unindexed += 1
                    continue
            meta.setdefault("source", source)
            meta.setdefault("priority", 100 if meta.get("source") == "default" else 50)
            raw_tags = meta.get("tags")
            if isinstance(raw_tags, list):
                tags = [str(item).strip().lower() for item in raw_tags if str(item).strip()]
            else:
                tags = _safe_tokens(str(raw_tags or ""))
            tags.extend(token for token in _safe_tokens(path.stem) if token not in tags)
            meaning = str(meta.get("meaning") or path.stem).strip()
            category, use_when, avoid_when, intensity, motion = self._entry_metadata(meta)
            entries.append(
                EmojiEntry(
                    path=path,
                    meaning=meaning,
                    tags=tags,
                    source=str(meta.get("source") or source),
                    priority=int(meta.get("priority") or 0),
                    category=category,
                    use_when=use_when,
                    avoid_when=avoid_when,
                    intensity=intensity,
                    motion=motion,
                )
            )
        self._entries = entries
        if skipped_unindexed:
            logger.info(
                "Skipped %s emoji image files that are not listed in manifest %s.",
                skipped_unindexed,
                self.manifest_path,
            )
        if auto_registered:
            logger.info(
                "Auto-registered %s default emoji image files in manifest %s.",
                auto_registered,
                self.manifest_path,
            )

    def candidates_text(self, query: str, tags: list[str] | None = None) -> str:
        entries = self.select(query, tags=tags, limit=self.max_candidates)
        if not entries:
            return ""
        lines = []
        for index, entry in enumerate(entries, 1):
            tag_text = ", ".join(entry.tags[:8])
            use_text = ", ".join(entry.use_when[:4]) or "-"
            avoid_text = ", ".join(entry.avoid_when[:4]) or "-"
            lines.append(
                f"{index}. meaning={entry.meaning} category={entry.category or '-'} "
                f"tags=[{tag_text}] use=[{use_text}] avoid=[{avoid_text}] source={entry.source}"
            )
        return "\n".join(lines)

    def select(self, query: str, *, tags: list[str] | None = None, limit: int | None = None) -> list[EmojiEntry]:
        if not self.enabled:
            return []
        query = _clean_query(query)
        wanted = set(_safe_tokens(query))
        for tag in tags or []:
            wanted.update(_safe_tokens(tag))
        if not wanted:
            return sorted(
                self._entries,
                key=lambda entry: entry.priority + self._source_bonus(entry.source),
                reverse=True,
            )[: limit or 1]

        scored: list[tuple[int, EmojiEntry]] = []
        for entry in self._entries:
            match_score = self._entry_match_score(wanted, entry)
            if match_score <= 0:
                continue
            scored.append((match_score + entry.priority + self._source_bonus(entry.source), entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for _score, entry in scored[: limit or 1]]

    @staticmethod
    def _source_bonus(source: str) -> int:
        return {"curated": 80, "default": 30, "downloaded": 0}.get(source, 0)

    def _entry_match_score(self, wanted: set[str], entry: EmojiEntry) -> int:
        score = 0
        score += _field_match_score(wanted, [entry.meaning], weight=40)
        score += _field_match_score(wanted, entry.tags, weight=30)
        categories = self._manifest.get("categories")
        category_meta = categories.get(entry.category) if isinstance(categories, dict) else None
        category_values = [entry.category]
        if isinstance(category_meta, dict):
            category_values.append(str(category_meta.get("meaning") or ""))
        score += _field_match_score(wanted, category_values, weight=30)
        score += _field_match_score(wanted, entry.use_when, weight=25)
        avoid_score = _field_match_score(wanted, entry.avoid_when, weight=35)
        if avoid_score:
            score -= avoid_score * 4 + 100
        return score

    def choose(self, query: str, *, tags: list[str] | None = None, refresh_on_miss: bool = False) -> EmojiEntry | None:
        entries = self.select(query, tags=tags, limit=1)
        if entries:
            return entries[0]
        if refresh_on_miss and self.enabled:
            self.refresh()
            entries = self.select(query, tags=tags, limit=1)
        return entries[0] if entries else None

    def adopt_downloaded(self, query: str, *, tags: list[str] | None = None) -> EmojiEntry | None:
        query = _clean_query(query)
        if not self.enabled or not self.allow_downloads or not query or not self.download_dir.is_dir():
            return None
        emojis = self._manifest.setdefault("emojis", {})
        if not isinstance(emojis, dict):
            emojis = {}
            self._manifest["emojis"] = emojis

        scored: list[tuple[int, Path]] = []
        for path in sorted(self.download_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in EMOJI_EXTENSIONS:
                continue
            key = self._relative_key(path)
            if isinstance(emojis.get(key), dict):
                continue
            path_tags = _safe_tokens(path.stem)
            score = _match_score(
                query,
                wanted_tags=tags,
                haystack_tags=path_tags,
                meaning=path.stem,
                base_score=50,
            )
            if score > 0:
                scored.append((score, path))
        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        path = scored[0][1]
        key = self._relative_key(path)
        clean_tags = [tag.strip().lower() for tag in (tags or []) if tag.strip()]
        clean_tags.extend(token for token in _safe_tokens(query) if token not in clean_tags)
        clean_tags.extend(token for token in _safe_tokens(path.stem) if token not in clean_tags)
        emojis[key] = {
            "meaning": query.strip() or path.stem,
            "tags": clean_tags,
            "source": "downloaded",
            "priority": 50,
        }
        self._scan_files()
        self._save_manifest()
        return self.choose(query, tags=clean_tags)

    def save_downloaded(
        self,
        *,
        image_data: bytes,
        content_type: str,
        source_url: str,
        meaning: str,
        tags: list[str],
        interest: int,
    ) -> EmojiEntry | None:
        if not self.enabled or not self.allow_downloads or not image_data:
            return None
        digest = hashlib.sha256(image_data).hexdigest()[:20]
        suffix = _extension_from(content_type, source_url)
        path = self.download_dir / f"{digest}{suffix}"
        if not path.exists():
            path.write_bytes(image_data)
        key = self._relative_key(path)
        emojis = self._manifest.setdefault("emojis", {})
        if not isinstance(emojis, dict):
            emojis = {}
            self._manifest["emojis"] = emojis
        clean_tags = [tag.strip().lower() for tag in tags if tag.strip()]
        emojis[key] = {
            "meaning": meaning.strip() or "高兴趣表情",
            "tags": clean_tags,
            "source": "downloaded",
            "priority": max(min(int(interest), 100), 0),
            "source_url": source_url,
        }
        self._scan_files()
        self._save_manifest()
        return self.choose(" ".join(clean_tags) or meaning, tags=clean_tags)

    def update_metadata(
        self,
        entry: EmojiEntry,
        *,
        meaning: str,
        tags: list[str],
        source: str | None = None,
        priority: int | None = None,
        category: str | None = None,
        use_when: list[str] | None = None,
        avoid_when: list[str] | None = None,
        intensity: str | None = None,
        motion: str | None = None,
    ) -> EmojiEntry | None:
        if not self.enabled:
            return None
        key = self._relative_key(entry.path)
        emojis = self._manifest.setdefault("emojis", {})
        if not isinstance(emojis, dict):
            emojis = {}
            self._manifest["emojis"] = emojis
        meta = emojis.get(key)
        if not isinstance(meta, dict):
            meta = self._default_meta(entry.path, source or entry.source)
        clean_tags: list[str] = []
        for tag in tags:
            tag = str(tag).strip().lower()
            if tag and tag not in clean_tags:
                clean_tags.append(tag)
        for token in _safe_tokens(meaning):
            if token not in clean_tags:
                clean_tags.append(token)
        meta["meaning"] = meaning.strip() or entry.meaning or entry.path.stem
        meta["tags"] = clean_tags or entry.tags
        meta["source"] = source or str(meta.get("source") or entry.source)
        if priority is not None:
            meta["priority"] = max(min(int(priority), 100), 0)
        else:
            meta.setdefault("priority", entry.priority)
        if category is not None:
            meta["category"] = category.strip()
        if use_when is not None:
            meta["use_when"] = _metadata_list(use_when)
        if avoid_when is not None:
            meta["avoid_when"] = _metadata_list(avoid_when)
        if intensity is not None:
            meta["intensity"] = intensity.strip() or "medium"
        if motion is not None:
            meta["motion"] = motion.strip() or "static"
        emojis[key] = meta
        self._scan_files()
        self._save_manifest()
        return self.choose(meta["meaning"], tags=clean_tags) or entry
