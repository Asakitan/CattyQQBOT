from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from .config import Config


EMOJI_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


@dataclass(slots=True)
class EmojiEntry:
    path: Path
    meaning: str
    tags: list[str]
    source: str
    priority: int


def _safe_tokens(text: str) -> list[str]:
    tokens = re.split(r"[\s,，、;；|_\\/\-.]+", text.lower())
    return [token for token in tokens if token]


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
    def __init__(self, config: Config) -> None:
        self.enabled = config.catty_emoji_enabled
        self.root = Path(config.catty_emoji_dir).expanduser()
        self.download_dir = Path(config.catty_emoji_download_dir).expanduser()
        self.manifest_path = Path(config.catty_emoji_manifest_path).expanduser()
        self.max_candidates = max(int(config.catty_emoji_max_candidates), 1)
        self._entries: list[EmojiEntry] = []
        self._manifest: dict[str, Any] = {"version": 1, "emojis": {}}
        if self.enabled:
            self.refresh()

    def refresh(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._manifest = self._load_manifest()
        self._scan_files()
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

    def _scan_files(self) -> None:
        emojis = self._manifest.setdefault("emojis", {})
        if not isinstance(emojis, dict):
            emojis = {}
            self._manifest["emojis"] = emojis
        entries: list[EmojiEntry] = []
        download_root = self.download_dir.resolve()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in EMOJI_EXTENSIONS:
                continue
            key = self._relative_key(path)
            try:
                path.resolve().relative_to(download_root)
                source = "downloaded"
            except ValueError:
                source = "default"
            meta = emojis.get(key)
            if not isinstance(meta, dict):
                meta = self._default_meta(path, source)
                emojis[key] = meta
            meta.setdefault("source", source)
            meta.setdefault("priority", 100 if meta.get("source") == "default" else 50)
            raw_tags = meta.get("tags")
            if isinstance(raw_tags, list):
                tags = [str(item).strip().lower() for item in raw_tags if str(item).strip()]
            else:
                tags = _safe_tokens(str(raw_tags or ""))
            tags.extend(token for token in _safe_tokens(path.stem) if token not in tags)
            meaning = str(meta.get("meaning") or path.stem).strip()
            entries.append(
                EmojiEntry(
                    path=path,
                    meaning=meaning,
                    tags=tags,
                    source=str(meta.get("source") or source),
                    priority=int(meta.get("priority") or 0),
                )
            )
        self._entries = entries

    def candidates_text(self, query: str, tags: list[str] | None = None) -> str:
        entries = self.select(query, tags=tags, limit=self.max_candidates)
        if not entries:
            return ""
        lines = []
        for index, entry in enumerate(entries, 1):
            tag_text = ", ".join(entry.tags[:8])
            lines.append(f"{index}. {entry.meaning} [{tag_text}] source={entry.source}")
        return "\n".join(lines)

    def select(self, query: str, *, tags: list[str] | None = None, limit: int | None = None) -> list[EmojiEntry]:
        if not self.enabled:
            return []
        wanted = set(_safe_tokens(query))
        for tag in tags or []:
            wanted.update(_safe_tokens(tag))
        if not wanted:
            return sorted(self._entries, key=lambda entry: entry.priority, reverse=True)[: limit or 1]

        scored: list[tuple[int, EmojiEntry]] = []
        for entry in self._entries:
            haystack = set(entry.tags)
            haystack.update(_safe_tokens(entry.meaning))
            score = entry.priority
            score += 40 * len(wanted & haystack)
            if entry.source == "default":
                score += 30
            if wanted and len(wanted & haystack) == 0:
                score -= 120
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for _score, entry in scored[: limit or 1]]

    def choose(self, query: str, *, tags: list[str] | None = None) -> EmojiEntry | None:
        entries = self.select(query, tags=tags, limit=1)
        return entries[0] if entries else None

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
        if not self.enabled or not image_data:
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
