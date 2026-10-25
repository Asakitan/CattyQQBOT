"""机机自学 (主人 2026-10-25): 自己逛 B站 / 贴吧 / 群, 把逛到的东西蒸馏成自己的语料库。

规则 (主人给的三条硬约束):
- 逛是有预算的: 每天 ``daily_token_budget`` (默认 100M) 封顶, 抓页面 + 蒸馏调用都记账;
- 逛是有时间窗口的: 只在 ``active_hours`` 内、单次 ``session_minutes`` 上限、每天 ``runs_per_day`` 上限;
- 逛出来的东西单独落一份库 (``data/fdj_self_study/notes.jsonl``), 总量 ``corpus_max_tokens``
  (默认 100K) 封顶, 超了按得分/时间淘汰最差的那条。

对外接口:
- ``collect_once(config)``       : 跑一轮, 返回 ``StudyRun``
- ``build_self_study_context(text, config)`` : 按相关性挑笔记拼成 prompt 块
- ``stats()`` / ``notes_snapshot()``          : 给 /status 和测试用

设计取舍: 网络/LLM 都是注入点 (``_SEARCH_FN`` / ``_FETCH_FN`` / ``_LLM_FN``), 单测不联网;
所有失败都静默返回 (自学坏了不能影响正常回复)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote as urlquote, urlsplit

_logger = logging.getLogger("catty_qq_ai.fdj_self_study")

_NOTES_FILE = "notes.jsonl"
_STATE_FILE = "state.json"
_MAX_NOTES = 2000
_DEFAULT_STORAGE_DIR = "data/fdj_self_study"

# 网络注入点 (测试替换)
_SEARCH_FN: Callable[..., Any] | None = None
_FETCH_FN: Callable[..., Any] | None = None
_LLM_FN: Callable[..., Any] | None = None


# ── token 估算 ─────────────────────────────────────────────────────────
def count_tokens(text: str) -> int:
    """粗略 token 数: 中文 1 字 ≈ 1 token, 其它 ≈ 4 字符 / token。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
    other = len(text) - cjk
    return int(cjk + other / 4) + 1


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", str(text or ""))
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


# ── 数据结构 ───────────────────────────────────────────────────────────
@dataclass(slots=True)
class StudyNote:
    id: str
    source: str
    title: str
    text: str
    tokens: int
    score: float
    created_at: float
    url: str = ""
    keywords: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "text": self.text,
            "tokens": self.tokens,
            "score": self.score,
            "created_at": self.created_at,
            "url": self.url,
            "keywords": list(self.keywords),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StudyNote | None":
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        return cls(
            id=str(data.get("id") or ""),
            source=str(data.get("source") or "unknown"),
            title=str(data.get("title") or "").strip(),
            text=text,
            tokens=int(data.get("tokens") or count_tokens(text)),
            score=float(data.get("score") or 1.0),
            created_at=float(data.get("created_at") or time.time()),
            url=str(data.get("url") or ""),
            keywords=tuple(str(k) for k in (data.get("keywords") or ()) if str(k).strip()),
        )


@dataclass(slots=True)
class StudyRun:
    ok: bool = False
    skipped: str = ""
    pages: int = 0
    tokens_used: int = 0
    notes_added: int = 0
    notes_evicted: int = 0
    corpus_tokens: int = 0
    sources: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""


# ── 存储 ───────────────────────────────────────────────────────────────
def storage_dir(storage: str | None = None) -> Path:
    raw = (storage or _DEFAULT_STORAGE_DIR).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / raw
    return path


def _notes_path(storage: str | None) -> Path:
    return storage_dir(storage) / _NOTES_FILE


def _state_path(storage: str | None) -> Path:
    return storage_dir(storage) / _STATE_FILE


_NOTES_CACHE: dict[str, tuple[tuple[int, int], list[StudyNote]]] = {}


def _load_notes(storage: str | None) -> list[StudyNote]:
    path = _notes_path(storage)
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _NOTES_CACHE.pop(str(path), None)
        return []
    cached = _NOTES_CACHE.get(str(path))
    if cached is not None and cached[0] == signature:
        return list(cached[1])
    notes: list[StudyNote] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                note = StudyNote.from_dict(data)
                if note is not None:
                    notes.append(note)
    except OSError:
        return []
    _NOTES_CACHE[str(path)] = (signature, notes)
    return list(notes)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        try:
            import os

            os.fsync(handle.fileno())
        except OSError:
            pass
    tmp.replace(path)


def _save_notes(storage: str | None, notes: Sequence[StudyNote]) -> None:
    body = "\n".join(json.dumps(note.to_dict(), ensure_ascii=False) for note in notes)
    _atomic_write(_notes_path(storage), body + ("\n" if body else ""))


def _today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))


def _load_state(storage: str | None) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(storage).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(storage: str | None, state: dict[str, Any]) -> None:
    try:
        _atomic_write(_state_path(storage), json.dumps(state, ensure_ascii=False, indent=2))
    except OSError as exc:
        _logger.warning("fdj self-study state write failed: %s", exc)


def _state_for_today(storage: str | None, now: float | None = None) -> dict[str, Any]:
    state = _load_state(storage)
    today = _today(now)
    if str(state.get("date") or "") != today:
        state = {"date": today, "used_tokens": 0, "runs": 0, "last_run_ts": 0.0}
    state.setdefault("used_tokens", 0)
    state.setdefault("runs", 0)
    state.setdefault("last_run_ts", 0.0)
    return state


# ── 时间窗口 ───────────────────────────────────────────────────────────
def parse_active_hours(spec: str) -> tuple[int, int] | None:
    """解析 ``"09:00-23:30"``; 返回 (start_minutes, end_minutes), 支持跨零点。"""
    raw = str(spec or "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", raw)
    if not match:
        return None
    sh, sm, eh, em = (int(part) for part in match.groups())
    if not (0 <= sh <= 24 and 0 <= eh <= 24 and 0 <= sm < 60 and 0 <= em < 60):
        return None
    return (sh * 60 + sm, eh * 60 + em)


def within_active_hours(spec: str, now: float | None = None) -> bool:
    window = parse_active_hours(spec)
    if window is None:
        return True
    stamp = time.localtime(now if now is not None else time.time())
    minute = stamp.tm_hour * 60 + stamp.tm_min
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


# ── 素材采集 ───────────────────────────────────────────────────────────
def _pick_keyword(config: Any, rng: random.Random) -> str:
    raw = getattr(config, "catty_self_study_keywords", None) or []
    keywords = [str(item).strip() for item in raw if str(item).strip()]
    if not keywords:
        keywords = ["游戏", "二次元"]
    return rng.choice(keywords)


def _source_group(config: Any, keyword: str, rng: random.Random, limit: int) -> dict[str, Any] | None:
    """逛「群」: 直接翻本地语料库里的真实对话, 不联网。"""
    try:
        from . import fdj_corpus_store
    except Exception:  # noqa: BLE001
        return None
    try:
        entries = fdj_corpus_store.scene_entries(limit=0)
    except Exception:  # noqa: BLE001
        return None
    if not entries:
        return None
    pool = [entry for entry in entries if str(entry.get("trigger") or "").strip()]
    if not pool:
        return None
    picked = rng.sample(pool, min(limit, len(pool)))
    lines: list[str] = []
    for entry in picked:
        trigger = str(entry.get("trigger") or "").strip()
        reply = str(entry.get("reply") or "").strip()
        if trigger and reply:
            lines.append(f"{trigger} → {reply}")
    if not lines:
        return None
    return {
        "source": "group",
        "title": f"群聊语料·{keyword}",
        "url": "",
        "text": "\n".join(lines),
    }


def _search_sites(keyword: str) -> list[tuple[str, str]]:
    """用现成的 web_search 找 B站/贴吧 的落地页 (不做 API 签名)。"""
    if _SEARCH_FN is not None:
        try:
            return list(_SEARCH_FN(keyword))
        except Exception:  # noqa: BLE001
            return []
    try:
        from .web_search import search_web
    except Exception:  # noqa: BLE001
        return []
    try:
        results = search_web(keyword, max_results=8)
    except Exception:  # noqa: BLE001
        return []
    pairs: list[tuple[str, str]] = []
    for item in results or []:
        url = str(getattr(item, "url", "") or "")
        title = str(getattr(item, "title", "") or "")
        if url:
            pairs.append((url, title))
    return pairs


def _tieba_url(keyword: str) -> str:
    return f"https://tieba.baidu.com/f?kw={urlquote(keyword)}&ie=utf-8"


def _bilibili_query(keyword: str) -> str:
    return f"site:bilibili.com {keyword}"


async def _fetch_page(url: str, *, max_chars: int, timeout: float) -> dict[str, Any] | None:
    if _FETCH_FN is not None:
        try:
            result = _FETCH_FN(url)
            if inspect.isawaitable(result):
                result = await result
        except Exception:  # noqa: BLE001
            return None
        return result if isinstance(result, dict) else None
    try:
        from .auto_web_fetch import _fetch_page_text
    except Exception:  # noqa: BLE001
        return None
    try:
        return await _fetch_page_text(url, max_chars=max_chars, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None


def _host_matches(url: str, hosts: Sequence[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == item or host.endswith("." + item) for item in hosts)


async def _collect_remote(
    config: Any,
    source: str,
    keyword: str,
    *,
    pages: int,
    page_chars: int,
    timeout: float,
) -> list[dict[str, Any]]:
    """抓 B站 / 贴吧 的正文页。抓不到就返回空 (自学失败不影响主链路)。"""
    if source == "bilibili":
        candidates = _search_sites(_bilibili_query(keyword))
        allowed_hosts = ("bilibili.com", "b23.tv")
        urls = [url for url, _title in candidates if _host_matches(url, allowed_hosts)]
        urls = urls[:pages]
    elif source == "tieba":
        candidates = _search_sites(f"site:tieba.baidu.com {keyword}")
        urls = [
            url for url, _title in candidates
            if _host_matches(url, ("tieba.baidu.com",))
        ][: max(pages - 1, 0)]
    else:
        return []

    fetched: list[dict[str, Any]] = []
    if source == "tieba":
        index_page = await _fetch_page(
            _tieba_url(keyword), max_chars=page_chars, timeout=timeout
        )
        if index_page is not None:
            fetched.append(index_page)

    seen: set[str] = set()
    for url in urls:
        if url in seen or len(fetched) >= pages:
            continue
        seen.add(url)
        body = await _fetch_page(url, max_chars=page_chars, timeout=timeout)
        if body is not None:
            fetched.append(body)
    return fetched


# ── 蒸馏 ───────────────────────────────────────────────────────────────
_DISTILL_PROMPT = (
    "你是机机本人（女、矮子、毒舌但心软的群聊角色）。下面是你刚在网上逛到的素材。\n"
    "把它压成你自己记住的短笔记：每条 ≤80 字，第一人称，只留你以后聊天可能用得上的事实/梗/说法，"
    "不要复述原文，不要写「我查了/我看到」这类过程话。\n"
    "只输出 JSON 数组，形如 [{\"text\": \"...\", \"keywords\": [\"词\"]}]，最多 {max_items} 条。\n"
    "素材：\n{material}"
)


def _parse_notes_json(raw: str) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return []
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        loaded = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(loaded, list):
        return []
    items: list[dict[str, Any]] = []
    for item in loaded:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        body = str(item.get("text") or "").strip()
        if not body:
            continue
        keywords = item.get("keywords")
        items.append({
            "text": body[:400],
            "keywords": [str(k).strip() for k in (keywords or []) if str(k).strip()][:6],
        })
    return items


async def _distill(config: Any, material: str, *, max_items: int) -> tuple[list[dict[str, Any]], int]:
    """调小模型把素材压成笔记; 返回 (items, 消耗 token)。"""
    prompt = _DISTILL_PROMPT.replace("{max_items}", str(max(1, max_items))).replace(
        "{material}", material[:6000]
    )
    messages = [{"role": "user", "content": prompt}]
    raw = ""
    if _LLM_FN is not None:
        try:
            raw = _LLM_FN(messages)
            if inspect.isawaitable(raw):
                raw = await raw
            raw = str(raw or "")
        except Exception:  # noqa: BLE001
            return [], count_tokens(prompt)
    else:
        try:
            from .openai_client import chat_completion_codex_instant
        except Exception:  # noqa: BLE001
            return [], count_tokens(prompt)
        try:
            raw = await chat_completion_codex_instant(
                config,
                messages,
                max_tokens=900,
                request_route="self_study",
                timeout_override=float(
                    getattr(config, "catty_self_study_request_timeout", 15.0) or 15.0
                ) + 15.0,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.info("fdj self-study distill failed: %s", exc)
            return [], count_tokens(prompt)
    used = count_tokens(prompt) + count_tokens(raw)
    return _parse_notes_json(raw), used


# ── 语料库维护 (100K token 上限) ───────────────────────────────────────
def _trim_corpus(notes: list[StudyNote], max_tokens: int) -> tuple[list[StudyNote], int]:
    """超上限时按 (得分, 时间) 从低到高淘汰。返回 (保留, 淘汰数)。"""
    if max_tokens <= 0:
        return notes, 0
    total = sum(note.tokens for note in notes)
    if total <= max_tokens:
        return notes, 0
    ordered = sorted(notes, key=lambda note: (note.score, note.created_at))
    dropped: set[str] = set()
    for note in ordered:
        if total <= max_tokens:
            break
        dropped.add(note.id)
        total -= note.tokens
    kept = [note for note in notes if note.id not in dropped]
    return kept, len(dropped)


def _note_id(source: str, text: str) -> str:
    digest = hashlib.sha1(f"{source}|{text}".encode("utf-8", "ignore")).hexdigest()
    return digest[:16]


def _merge_notes(
    existing: list[StudyNote],
    new_items: Iterable[dict[str, Any]],
    *,
    source: str,
    title: str,
    url: str,
    now: float,
) -> tuple[list[StudyNote], int]:
    known = {note.id: note for note in existing}
    added = 0
    for item in new_items:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        note_id = _note_id(source, text)
        if note_id in known:
            continue
        tokens = count_tokens(text)
        score = 1.0 + min(tokens / 400.0, 1.0)
        note = StudyNote(
            id=note_id,
            source=source,
            title=title,
            text=text,
            tokens=tokens,
            score=score,
            created_at=now,
            url=url,
            keywords=tuple(str(k) for k in (item.get("keywords") or ()) if str(k).strip()),
        )
        known[note_id] = note
        added += 1
    return list(known.values()), added


# ── 主入口: 跑一轮 ─────────────────────────────────────────────────────
def _sources(config: Any) -> list[str]:
    raw = getattr(config, "catty_self_study_sources", None) or []
    allowed = {"group", "bilibili", "tieba"}
    picked = [str(item).strip().lower() for item in raw if str(item).strip().lower() in allowed]
    return picked or ["group"]


async def collect_once(
    config: Any,
    *,
    now: float | None = None,
    rng: random.Random | None = None,
) -> StudyRun:
    """跑一轮自学。任何失败都返回 ok=False, 不抛异常。"""
    run = StudyRun()
    if not bool(getattr(config, "catty_self_study_enabled", False)):
        run.skipped = "disabled"
        return run
    stamp = time.time() if now is None else now
    storage = str(getattr(config, "catty_self_study_storage_dir", _DEFAULT_STORAGE_DIR) or "")
    if not within_active_hours(
        str(getattr(config, "catty_self_study_active_hours", "09:00-23:30") or ""), stamp
    ):
        run.skipped = "outside_active_hours"
        return run

    state = _state_for_today(storage, stamp)
    try:
        daily_budget = int(getattr(config, "catty_self_study_daily_token_budget", 100_000_000) or 0)
    except (TypeError, ValueError):
        daily_budget = 100_000_000
    try:
        runs_per_day = int(getattr(config, "catty_self_study_runs_per_day", 6) or 0)
    except (TypeError, ValueError):
        runs_per_day = 6
    if runs_per_day > 0 and int(state.get("runs") or 0) >= runs_per_day:
        run.skipped = "daily_runs_exhausted"
        return run
    used_today = int(state.get("used_tokens") or 0)
    if daily_budget > 0 and used_today >= daily_budget:
        run.skipped = "daily_budget_exhausted"
        return run
    remaining_budget = daily_budget - used_today if daily_budget > 0 else 10 ** 12

    rng = rng or random.Random()
    try:
        pages_limit = max(int(getattr(config, "catty_self_study_pages_per_run", 6) or 1), 1)
    except (TypeError, ValueError):
        pages_limit = 6
    try:
        page_chars = max(int(getattr(config, "catty_self_study_page_chars", 1500) or 500), 200)
    except (TypeError, ValueError):
        page_chars = 1500
    try:
        timeout = float(getattr(config, "catty_self_study_request_timeout", 15.0) or 15.0)
    except (TypeError, ValueError):
        timeout = 15.0
    try:
        session_budget = float(getattr(config, "catty_self_study_session_minutes", 8.0) or 0.0) * 60.0
    except (TypeError, ValueError):
        session_budget = 0.0
    session_started = time.monotonic()

    sources = _sources(config)
    run.sources = tuple(sources)
    keyword = _pick_keyword(config, rng)
    notes = _load_notes(storage)
    tokens_spent = 0

    for source in sources:
        if session_budget > 0 and time.monotonic() - session_started > session_budget:
            run.skipped = "session_time_limit"
            break
        if tokens_spent >= remaining_budget:
            run.skipped = "daily_budget_exhausted"
            break
        material_pages: list[dict[str, Any]] = []
        if source == "group":
            page = _source_group(config, keyword, rng, limit=8)
            if page is not None:
                material_pages.append(page)
        else:
            material_pages.extend(
                await _collect_remote(
                    config,
                    source,
                    keyword,
                    pages=pages_limit,
                    page_chars=page_chars,
                    timeout=timeout,
                )
            )
        if not material_pages:
            continue
        run.pages += len(material_pages)
        material = "\n\n".join(
            f"[{page.get('title') or page.get('url') or source}]\n{page.get('text') or ''}"
            for page in material_pages
        )
        tokens_spent += count_tokens(material)
        if tokens_spent >= remaining_budget:
            run.skipped = "daily_budget_exhausted"
            break
        items, used = await _distill(config, material, max_items=pages_limit)
        tokens_spent += int(used)
        if not items:
            continue
        title = str(material_pages[0].get("title") or keyword)
        url = str(material_pages[0].get("url") or "")
        notes, added = _merge_notes(
            notes,
            items,
            source=source,
            title=title,
            url=url,
            now=stamp,
        )
        run.notes_added += added

    try:
        corpus_max = int(getattr(config, "catty_self_study_corpus_max_tokens", 100_000) or 0)
    except (TypeError, ValueError):
        corpus_max = 100_000
    notes, evicted = _trim_corpus(notes, corpus_max)
    if len(notes) > _MAX_NOTES:
        notes = sorted(notes, key=lambda note: (note.score, note.created_at))[-_MAX_NOTES:]
    run.notes_evicted = evicted
    run.tokens_used = tokens_spent
    run.corpus_tokens = sum(note.tokens for note in notes)

    if run.notes_added or evicted or tokens_spent:
        try:
            _save_notes(storage, notes)
        except OSError as exc:
            _logger.warning("fdj self-study notes write failed: %s", exc)
            run.error = f"write:{exc}"
            return run

    if tokens_spent or run.notes_added:
        state["used_tokens"] = int(state.get("used_tokens") or 0) + tokens_spent
        state["runs"] = int(state.get("runs") or 0) + 1
        state["last_run_ts"] = stamp
        _save_state(storage, state)

    run.ok = bool(run.notes_added or tokens_spent)
    if not run.ok and not run.skipped:
        run.skipped = "nothing_found"
    _logger.info(
        "fdj self-study run: ok=%s src=%s keyword=%r pages=%s added=%s evicted=%s "
        "tokens_used=%s corpus_tokens=%s skipped=%s",
        run.ok, ",".join(sources), keyword, run.pages, run.notes_added,
        run.notes_evicted, run.tokens_used, run.corpus_tokens, run.skipped or "-",
    )
    return run


# ── 注入 prompt ────────────────────────────────────────────────────────
def build_self_study_context(
    text: str,
    config: Any,
    *,
    limit_tokens: int | None = None,
) -> str:
    """挑跟当前消息最相关的自学笔记, 拼成 prompt 块。没笔记返回空串。"""
    if not bool(getattr(config, "catty_self_study_enabled", False)):
        return ""
    storage = str(getattr(config, "catty_self_study_storage_dir", _DEFAULT_STORAGE_DIR) or "")
    notes = _load_notes(storage)
    if not notes:
        return ""
    if limit_tokens is None:
        try:
            limit_tokens = int(getattr(config, "catty_self_study_context_tokens", 1200) or 0)
        except (TypeError, ValueError):
            limit_tokens = 1200
    if limit_tokens <= 0:
        return ""
    wanted = _bigrams(text)
    scored: list[tuple[float, StudyNote]] = []
    for note in notes:
        overlap = len(wanted & _bigrams(note.title + note.text))
        keyword_hit = sum(1 for kw in note.keywords if kw and kw in (text or ""))
        weighted = overlap + keyword_hit * 3 + note.score * 0.5
        scored.append((weighted, note))
    scored.sort(key=lambda item: (-item[0], -item[1].created_at))
    picked: list[StudyNote] = []
    used = 0
    for _weight, note in scored:
        if used + note.tokens > limit_tokens:
            continue
        picked.append(note)
        used += note.tokens
        if used >= limit_tokens * 0.9:
            break
    if not picked:
        return ""
    lines = [
        "【自学笔记】（你自己平时逛到、记下来的东西；当成自己本来就知道的事用，别提后台过程）"
    ]
    for note in picked:
        head = f"{note.title}｜" if note.title else ""
        lines.append(f"- {head}{note.text}")
    return "\n".join(lines)


# ── 状态 (给 /status 和测试) ───────────────────────────────────────────
def stats(config: Any, *, now: float | None = None) -> dict[str, Any]:
    storage = str(getattr(config, "catty_self_study_storage_dir", _DEFAULT_STORAGE_DIR) or "")
    notes = _load_notes(storage)
    state = _state_for_today(storage, now)
    return {
        "enabled": bool(getattr(config, "catty_self_study_enabled", False)),
        "notes": len(notes),
        "corpus_tokens": sum(note.tokens for note in notes),
        "used_tokens_today": int(state.get("used_tokens") or 0),
        "runs_today": int(state.get("runs") or 0),
        "last_run_ts": float(state.get("last_run_ts") or 0.0),
        "sources": sorted({note.source for note in notes}),
    }


def reset_state(storage: str | None = None) -> None:
    """测试/运维用: 清空笔记和当日预算账本。"""
    for path in (_notes_path(storage), _state_path(storage)):
        try:
            path.unlink()
        except OSError:
            continue
