"""机机回复前的轻量证据编排。"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .fdj_scene_retrieval import SceneMatch, match_scene_pairs
from .harness_prompt import EVIDENCE_USAGE


@dataclass(frozen=True, slots=True)
class QueryFlags:
    intent: str
    memory_need: bool
    anaphora: bool

    def as_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "memory_need": self.memory_need, "anaphora": self.anaphora}


@dataclass(frozen=True, slots=True)
class Evidence:
    source: str
    scope: str
    text: str
    relevance: float = 0.0
    recency: float = 0.0
    importance: float = 0.0
    authority: float = 0.0
    private: bool = False
    rank: int = 50
    observed_at: float | None = None
    event_date: str = ""
    confidence: str = ""
    flags: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """Content identity, independent of ranking and wall-clock freshness."""
        payload = json.dumps(
            (self.source, self.scope, _normalized_text(self.text), self.private),
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "scope": self.scope, "text": self.text, "relevance": round(self.relevance, 6), "recency": round(self.recency, 6), "importance": round(self.importance, 6), "authority": round(self.authority, 6), "private": self.private, "id": self.id, "rank": self.rank, "observed_at": self.observed_at, "event_date": self.event_date, "confidence": self.confidence, "flags": self.flags}


_DEFAULT_MAX_CHARS = 4000
_MEMORY_BAD_TEXT = ("暂无有效", "无文字交流", "无有效信息", "memory disabled")


class _Unavailable(LookupError):
    """A provider/method is absent, not an empty successful query."""


@dataclass
class _SourceTrace:
    source: str
    diagnostics: list[dict[str, str]] = field(default_factory=list)
    successes: int = 0
    unavailable: int = 0
    rejected: int = 0

    def call(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            result = function(*args, **kwargs)
        except _Unavailable:
            self.unavailable += 1
            return None
        except Exception as exc:
            # Never put exception messages (paths, secrets or provider text) in packets.
            self.diagnostics.append({"source": self.source, "error_type": type(exc).__name__})
            return None
        self.successes += 1
        return result

    def status(self, collected: int, retained: int) -> dict[str, Any]:
        if self.diagnostics:
            state = "partial" if collected else "error"
        elif self.rejected:
            state = "partial" if collected else "scope_mismatch"
        elif self.unavailable:
            state = "partial" if collected else "unavailable"
        else:
            state = "ok" if collected else "empty"
        result: dict[str, Any] = {"state": state, "count": retained, "collected_count": collected}
        if self.rejected:
            result["rejected_count"] = self.rejected
        if self.diagnostics:
            result["error_count"] = len(self.diagnostics)
        return result


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _timestamp(value: Any, *, milliseconds: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            stamp = parsed.timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    else:
        if milliseconds:
            stamp /= 1000.0
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def _metadata(entry: Mapping[str, Any], *time_keys: str, milliseconds: bool = False) -> dict[str, Any]:
    observed = next((stamp for key in time_keys if (stamp := _timestamp(entry.get(key), milliseconds=milliseconds)) is not None), None)
    raw_flags = entry.get("flags") or ()
    flags = tuple(str(value) for value in raw_flags) if isinstance(raw_flags, (list, tuple)) else ()
    return {"observed_at": observed, "event_date": str(entry.get("event_date") or ""), "confidence": str(entry.get("confidence") or ""), "flags": flags}


def _clean(value: Any, max_chars: int = 600) -> str:
    return " ".join(str(value or "").strip().split())[:max_chars]


def _excerpt_metadata(metadata: Mapping[str, Any], *values: tuple[Any, int]) -> dict[str, Any]:
    result = dict(metadata)
    if any(len(" ".join(str(value or "").split())) > limit for value, limit in values):
        result["flags"] = tuple(dict.fromkeys((*result.get("flags", ()), "excerpt")))
    return result


def _persona_name(persona: Any) -> str:
    if isinstance(persona, str):
        return persona.strip().lower() or "fadianji"
    return str(getattr(persona, "name", "fadianji") or "fadianji").strip().lower()


def _scope_value(scope_key: str, *, is_private: bool, user_id: str, group_id: str) -> tuple[str, bool]:
    scope = str(scope_key or "").strip()
    lowered = scope.lower()
    if lowered.startswith("group:"):
        return scope, False
    if lowered.startswith("private:"):
        return scope, True
    if is_private:
        return f"private:{user_id or scope or 'unknown'}", True
    return f"group:{group_id or scope or 'unknown'}", False


def detect_fadianji_query_flags(text: str) -> QueryFlags:
    query = _clean(text, 1000)
    if re.search(r"(怎么|如何|为什么|报错|配置|代码|帮我|解决|修复|设置)", query): intent = "help"
    elif re.search(r"(难过|伤心|哭|累|委屈|焦虑|崩溃|疼|不舒服)", query): intent = "comfort"
    elif re.search(r"(谢谢|感谢|送你|礼物|上舰|打钱)", query): intent = "thanks"
    elif re.search(r"(可爱|好看|厉害|牛逼|牛|喜欢|夸)", query): intent = "praise"
    elif re.search(r"(早安|早上好|晚安|在吗|来了|来了吗)", query): intent = "greeting"
    elif re.search(r"(吗|么|呢|什么|谁|哪|几|是不是|有没有|真的)", query): intent = "question"
    elif re.search(r"(哈哈|笑死|我去|我靠|草|呜|啊|？|!|！)", query): intent = "reaction"
    else: intent = "chat"
    return QueryFlags(intent, bool(re.search(r"(记得|还记得|之前|上次|刚才|我说过|提过|记住|回忆|以前)", query)), bool(re.search(r"(这个|那个|这件事|那件事|他|她|它|他们|她们|继续|然后|上面|下面|刚才)", query)))


def _book_entries(persona: Any) -> Sequence[Any]:
    entries = getattr(persona, "character_book", None) if persona is not None and not isinstance(persona, str) else None
    if entries: return tuple(entries)
    if _persona_name(persona) != "fadianji": return ()
    from .personas.fadianji import FADIANJI_CHARACTER_BOOK
    return FADIANJI_CHARACTER_BOOK


def _activate_character_book(text: str, persona: Any, limit: int) -> list[Evidence]:
    if limit <= 0:
        return []
    query = _clean(text, 1000).casefold()
    scored: list[tuple[float, int, str, Any]] = []
    for entry in _book_entries(persona):
        hits = [key for key in tuple(getattr(entry, "keys", ()) or ()) if key and str(key).casefold() in query]
        if hits:
            scored.append((max(len(str(key)) for key in hits) + len(hits) * 0.25, int(getattr(entry, "order", 100) or 100), str(getattr(entry, "identifier", "character_book")), entry))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [Evidence(f"FACT.character_book:{identifier}", "persona:fadianji", _clean(getattr(entry, "content", ""), 520), min(1.0, score / 12.0), 0.0, 0.72, 0.78, False, 30, **_excerpt_metadata({}, (getattr(entry, "content", ""), 520))) for score, _order, identifier, entry in scored[: max(0, limit)] if _clean(getattr(entry, "content", ""), 520)]


def _call_store(store: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(store, method, None)
    if not callable(function):
        raise _Unavailable(method)
    # Adapt optional keywords before execution; a provider TypeError never retries.
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)
    if not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        optional = {"query": {"persona", "top_k"}, "recall": {"limit"}, "recall_notes": {"limit"}, "recent_feeds": {"limit", "max_chars_each"}}.get(method, set())
        if method == "lookup_user_profile" and kwargs.get("group_id") == "":
            optional = {"group_id"}
        kwargs = {key: value for key, value in kwargs.items() if key not in optional or key in signature.parameters}
    # Unknown scope/identity/keyword selectors fail binding instead of silently
    # broadening a query. Only the method-specific optional controls above adapt.
    signature.bind(*args, **kwargs)
    return function(*args, **kwargs)


def _group_profile(store: Any, user_id: str, group_id: str) -> Mapping[str, Any]:
    for method in ("_profile_for", "group_profile", "lookup_group_profile"):
        if callable(getattr(store, method, None)):
            value = _call_store(store, method, user_id, group_id)
            return value if isinstance(value, Mapping) else {}
    raise _Unavailable("group_profile")


def _profile_evidence(profile: Mapping[str, Any], scope: str, private: bool) -> list[Evidence]:
    parts: list[str] = []
    limits: list[tuple[Any, int]] = []
    for label, key in (("称呼", "preferred_name"), ("性别", "gender"), ("印象", "impression"), ("偏好", "interests"), ("边界", "boundaries"), ("相关梗", "meme_hooks")):
        value = profile.get(key)
        if isinstance(value, (list, tuple, set)):
            limits.extend((item, 40) for item in value)
            value = "/".join(_clean(item, 40) for item in value if _clean(item, 40))
        limits.append((value, 180))
        value = _clean(value, 180)
        if value and not any(bad in value for bad in _MEMORY_BAD_TEXT): parts.append(f"{label}={value}")
    metadata = _excerpt_metadata(_metadata(profile, "snapshot_at", "snapshot_time", "updated_at", "time", "ts"), *limits)
    return [Evidence("MEMORY.profile", scope, "; ".join(parts), 0.8, _recency(metadata["observed_at"]), 0.82, 0.9, private, 10, **metadata)] if parts else []


def _notes_evidence(notes: Any, scope: str, private: bool) -> list[Evidence]:
    if not isinstance(notes, Mapping): return []
    result: list[Evidence] = []
    for bucket in ("user_notes", "group_notes"):
        values = notes.get(bucket) or []
        if not isinstance(values, (list, tuple)): continue
        for entry in values:
            value = _clean(entry.get("text") if isinstance(entry, Mapping) else entry, 300)
            if value and not any(bad in value for bad in _MEMORY_BAD_TEXT):
                metadata = _excerpt_metadata(_metadata(entry if isinstance(entry, Mapping) else {}, "time", "ts", "snapshot_at"), (entry.get("text") if isinstance(entry, Mapping) else entry, 300))
                result.append(Evidence("MEMORY.notes", scope, value, 0.76, _recency(metadata["observed_at"]), 0.86, 0.88, private, 12, **metadata))
    return result


def _recall_evidence(recall: Any, scope: str, private: bool) -> list[Evidence]:
    if not isinstance(recall, Mapping): return []
    result: list[Evidence] = []
    summary = _clean(recall.get("long_term_summary"), 500)
    if summary and not any(bad in summary for bad in _MEMORY_BAD_TEXT):
        metadata = _excerpt_metadata(_metadata(recall, "summary_time", "snapshot_at", "snapshot_time", "time", "ts"), (recall.get("long_term_summary"), 500))
        result.append(Evidence("MEMORY.recall", scope, f"长期摘要: {summary}", 0.7, _recency(metadata["observed_at"]), 0.78, 0.8, private, 20, **metadata))
    for match in recall.get("matches") or ():
        if isinstance(match, Mapping) and _clean(match.get("text"), 320):
            metadata = _metadata(match, "time", "ts", "snapshot_at")
            value_limit = 320
            prefix = ""
            identity_excerpt = False
            if not private:
                labels = []
                for key, limit in (("display_name", 80), ("user_id", 96)):
                    raw = " ".join(str(match.get(key) or "").split())
                    label = json.dumps(raw or "未知", ensure_ascii=False)
                    if len(label) > limit:
                        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
                        label = json.dumps(f"{raw[:8]}~{digest}", ensure_ascii=False)
                        identity_excerpt = True
                    labels.append(label)
                prefix = f"群消息 发言者昵称={labels[0]} UID={labels[1]}："
                value_limit -= len(prefix)
            metadata = _excerpt_metadata(metadata, (match.get("text"), value_limit))
            if identity_excerpt:
                metadata["flags"] = tuple(dict.fromkeys((*metadata["flags"], "excerpt")))
            result.append(Evidence("MEMORY.recall", scope, prefix + _clean(match.get("text"), value_limit), 0.72, _recency(metadata["observed_at"]), 0.62, 0.7, private, 20, **metadata))
    return result


def _memory_evidence(store: Any, text: str, scope: str, private: bool, user_id: str, group_id: str, flags: QueryFlags, *, quoted_text: str = "", trace: _SourceTrace | None = None) -> list[Evidence]:
    if store is None: return []
    trace = trace or _SourceTrace("memory")
    result: list[Evidence] = []

    def profile_rows() -> list[Evidence]:
        profile = _call_store(store, "lookup_user_profile", user_id, group_id="") if private else _group_profile(store, user_id, group_id)
        return _profile_evidence(profile, scope, private) if isinstance(profile, Mapping) else []

    if user_id and (private or group_id):
        result.extend(trace.call(profile_rows) or [])
    if (flags.memory_need or flags.anaphora or quoted_text) and (user_id if private else group_id):
        scope_args = {"user_id": user_id} if private else {"group_id": group_id}
        current_query = ""
        if flags.memory_need or flags.anaphora:
            result.extend(trace.call(lambda: _notes_evidence(_call_store(store, "recall_notes", **scope_args, limit=4), scope, private)) or [])
            current_query = _clean(text, 160) if quoted_text else text
            if not quoted_text or re.search(r"[^\s,，;；]", current_query):
                result.extend(trace.call(lambda: _recall_evidence(_call_store(store, "recall", **scope_args, keywords=current_query, limit=4), scope, private)) or [])
        if quoted_text and _normalized_text(quoted_text) != _normalized_text(current_query):
            result.extend(trace.call(lambda: _recall_evidence(_call_store(store, "recall", **scope_args, keywords=quoted_text, limit=4), scope, private)) or [])
    return result


def _recency(value: Any) -> float:
    timestamp = _timestamp(value)
    if timestamp is None: return 0.0
    return 1.0 / (1.0 + max(0.0, (datetime.now(timezone.utc).timestamp() - timestamp) / 86400.0) / 30.0)


def _rag_evidence(store: Any, text: str, scope: str, persona: Any, private: bool, *, trace: _SourceTrace | None = None) -> list[Evidence]:
    hits = _call_store(store, "query", scope, text, top_k=4, persona=_persona_name(persona))
    result: list[Evidence] = []
    for hit in hits or ():
        if not isinstance(hit, (tuple, list)) or len(hit) < 2: continue
        metadata = hit[2] if len(hit) > 2 and isinstance(hit[2], Mapping) else {}
        metadata_scope = str(metadata.get("scope") or "").strip()
        if metadata_scope != scope or (not private and bool(metadata.get("private"))):
            if trace is not None: trace.rejected += 1
            continue
        value = _clean(hit[1], 360)
        if value:
            provenance = _excerpt_metadata(_metadata(metadata, "ts", "snapshot_at", "snapshot_time"), (hit[1], 360))
            result.append(Evidence(f"RAG.{_clean(metadata.get('role') or 'memory', 40)}", scope, value, max(0.0, min(1.0, float(hit[0] or 0.0))), _recency(provenance["observed_at"]), 0.58, 0.64, private, 40, **provenance))
    return result


def _scene_evidence(matches: Sequence[SceneMatch]) -> list[Evidence]:
    result: list[Evidence] = []
    for match in matches:
        result.append(Evidence(
            f"STYLE_EXAMPLE.{match.source}",
            match.source_scope,
            f"{match.trigger} → 机机: {match.reply}（分类={match.category}）",
            max(0.0, min(1.0, match.score / 4.0)),
            _recency(getattr(match, "observed_at", None)),
            0.75,
            0.72,
            match.private,
            40,
            observed_at=_timestamp(getattr(match, "observed_at", None)),
            event_date=str(getattr(match, "event_date", "") or ""),
            flags=("style_only",),
        ))
    return result


def _feed_evidence(feed_store: Any, scope: str, max_items: int) -> list[Evidence]:
    """QQ空间动态见闻 (2026-08-15): 被点赞动态作为机机的事实证据块, 公开动态不隔离私聊."""
    if feed_store is None or max_items <= 0:
        return []
    feeds = _call_store(feed_store, "recent_feeds", limit=max_items)
    result: list[Evidence] = []
    for feed in feeds or ():
        if not isinstance(feed, Mapping):
            continue
        text = _clean(feed.get("text"), 120)
        if not text:
            continue
        author = _clean(feed.get("author_name"), 40) or "有群友"
        likes = 0
        try:
            likes = max(int(feed.get("like_count") or 0), 0)
        except (TypeError, ValueError):
            likes = 0
        result.append(Evidence(
            "FEED.qzone",
            scope,
            f"{author} 发了动态: {text}（{likes} 赞）",
            0.62,
            _recency(feed.get("feed_time")),
            0.55,
            0.6,
            False,
            15,
            **_excerpt_metadata(_metadata(feed, "feed_time"), (feed.get("text"), 120), (feed.get("author_name"), 40)),
        ))
    return result

def _fmt_year(ts: Any) -> str:
    value = _timestamp(ts, milliseconds=True)
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%y.%m")
    except (ValueError, OverflowError, OSError):
        return ""


def _corpus_profile_evidence(display_name: str, scope: str, private: bool, *, corpus_profile_enabled: bool = True, corpus_profile_max_chars: int = 320, trace: _SourceTrace | None = None) -> list[Evidence]:
    """历史互动统计与口吻样本分开；配置由调用方显式传入。"""
    max_chars = max(0, int(corpus_profile_max_chars))
    if not corpus_profile_enabled or max_chars <= 0 or (not private and not display_name):
        return []
    from . import fdj_corpus_store
    if not fdj_corpus_store.corpus_enabled():
        raise _Unavailable("corpus")
    profile: Mapping[str, Any] | None = None
    if private:
        profile = _call_store(fdj_corpus_store, "private_profile", scope)
    if profile is None and display_name:
        profile = _call_store(fdj_corpus_store, "lookup_user_profile", display_name, scope=scope)
    if not isinstance(profile, Mapping) or not profile:
        return []
    actual_scope = str(profile.get("scope") or "").strip()
    eligible = actual_scope == scope if private else actual_scope in ("", "group", scope)
    if not eligible or (not private and bool(profile.get("private"))):
        if trace is not None: trace.rejected += 1
        return []
    name = _clean(profile.get("name"), 40) or "对方"
    try:
        triggers = int(profile.get("triggers") or 0)
    except (TypeError, ValueError):
        triggers = 0
    span = "-".join(part for part in (_fmt_year(profile.get("first_ts")), _fmt_year(profile.get("last_ts"))) if part)
    metadata = _metadata(profile, "last_ts", milliseconds=True)
    metadata["flags"] = tuple(dict.fromkeys((*metadata["flags"], "historical")))
    if private:
        raw_stats = f"【语料旧识】机机与{name}共互动 {triggers} 次" + (f"({span})" if span else "")
        stats = _clean(raw_stats, max_chars)
        stats_metadata = _excerpt_metadata(metadata, (raw_stats, max_chars), (profile.get("name"), 40))
        result = [Evidence("MEMORY.corpus_profile", scope, stats, 0.8, _recency(metadata["observed_at"]), 0.8, 0.85, private, 11, **stats_metadata)]
    else:
        stats = ""
        result = []
    parts: list[str] = []
    limits: list[tuple[Any, int]] = [(profile.get("register"), 8)]
    register = _clean(profile.get("register"), 8)
    if register:
        parts.append(f"语气档={register}")
    avg_len = profile.get("avg_reply_len")
    if avg_len:
        parts.append(f"平均回复 {avg_len} 字")
    addresses = "/".join(_clean(item, 8) for item in (profile.get("addresses") or [])[:4] if _clean(item, 8))
    limits.extend((item, 8) for item in (profile.get("addresses") or [])[:4])
    if addresses:
        parts.append(f"常用称呼={addresses}")
    if not private:
        terms = "/".join(_clean(item, 10) for item in (profile.get("top_terms") or [])[:5] if _clean(item, 10))
        limits.extend((item, 10) for item in (profile.get("top_terms") or [])[:5])
        if terms:
            parts.append(f"常聊={terms}")
    samples = profile.get("samples") or ()
    if isinstance(samples, Sequence) and samples:
        valid_samples = [item for item in samples if isinstance(item, Mapping)]
        if valid_samples:
            try:
                target_len = float(avg_len) if avg_len else 6.0
            except (TypeError, ValueError):
                target_len = 6.0
            sample = min(valid_samples, key=lambda item: abs(len(_clean(item.get("reply"), 600)) - target_len))
            trigger = _clean(sample.get("trigger"), 40)
            reply = _clean(sample.get("reply"), 60)
            if trigger and reply:
                parts.append(f"旧对话例: “{trigger}”→“{reply}”")
                limits.extend(((sample.get("trigger"), 40), (sample.get("reply"), 60)))
    if not private:
        parts.insert(0, "仅供口吻参考，不确认与当前发言者为同一人")
    remaining = max_chars - len(stats)
    if parts and remaining > 0:
        style_metadata = _excerpt_metadata(dict(metadata, flags=tuple(dict.fromkeys((*metadata["flags"], "style_only")))), ("; ".join(parts), remaining), *limits)
        result.append(Evidence("STYLE_EXAMPLE.corpus_profile", scope, _clean("; ".join(parts), remaining), 0.8, _recency(metadata["observed_at"]), 0.65, 0.65, private, 40, **style_metadata))
    return result


def _evidence_order(item: Evidence) -> tuple[Any, ...]:
    return (item.rank, -item.authority, -item.relevance, -item.recency, item.source, item.scope, item.text)


def _dedupe_and_sort(evidence: Sequence[Evidence], private: bool) -> list[Evidence]:
    chosen: dict[tuple[str, str, str], Evidence] = {}
    for item in evidence:
        if not item.text or (not private and item.private): continue
        key = (item.source.split(".", 1)[0], item.scope, _normalized_text(item.text))
        previous = chosen.get(key)
        if previous is None or _evidence_order(item) < _evidence_order(previous):
            chosen[key] = item
    return sorted(chosen.values(), key=_evidence_order)


def _evidence_bucket(item: Evidence) -> str:
    kind = item.source.split(".", 1)[0]
    return kind if kind in _BUCKET_TITLES else "OTHER"


_BUCKET_TITLES = {"FACT": "【FACT/角色事实】", "MEMORY": "【MEMORY/当前记忆】", "FEED": "【FEED/空间动态见闻】", "RAG": "【RAG/历史事实】", "STYLE_EXAMPLE": "【STYLE_EXAMPLE/口吻母本】", "OTHER": "【OTHER/其他证据】"}


def _bucket_counts(evidence: Sequence[Evidence]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in evidence:
        bucket = _evidence_bucket(item)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _render_row(item: Evidence, *, evidence_id: str | None = None) -> str:
    # Keep complete metadata, including long identifiers. If it cannot fit, omit
    # the whole row; never publish a misleading prefix of an ID/scope/date.
    atom = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        when = datetime.fromtimestamp(item.observed_at, tz=timezone.utc).isoformat().replace("+00:00", "Z") if item.observed_at is not None else "?"
    except (ValueError, OverflowError, OSError):
        when = "?"
    metadata = f"scope={atom(item.scope)} src={atom(item.source)} id={evidence_id if evidence_id is not None else item.id} time={when} relevance={item.relevance:.2f}"
    if item.event_date:
        metadata += f" event_date={atom(item.event_date)}"
    if item.confidence:
        metadata += f" confidence={atom(item.confidence)}"
    if item.flags:
        metadata += f" flags={atom(item.flags)}"
    return f"- [{metadata}] {item.text}\n"


def _render_packet_info(flags: QueryFlags, evidence: Sequence[Evidence], scope: str, private: bool, max_chars: int, *, source_status: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Greedily select complete rows in rank order, including all metadata cost."""
    budget = max(0, int(max_chars))
    ordered = sorted(evidence, key=_evidence_order)
    header = f"【机机·evidence】\nscope={scope}; private={1 if private else 0}; intent={flags.intent}; memory_need={1 if flags.memory_need else 0}; anaphora={1 if flags.anaphora else 0}\n"
    coverage = "sources=" + ",".join(f"{name}:{value['state']}/{value['count']}" for name, value in (source_status or {}).items()) + "\n" if source_status else ""

    def summary(count: int) -> str:
        return f"budget={budget}; rows={count}/{len(ordered)}; omitted={len(ordered) - count}\n"

    usage = "【使用】" + EVIDENCE_USAGE
    fixed_chars = len(header) + len(coverage) + len(usage)
    records = []
    for item in ordered:
        item_id = item.id
        records.append((item, item_id, _evidence_bucket(item), _render_row(item, evidence_id=item_id)))
    selected: list[Evidence] = []
    omitted: list[Evidence] = []
    rendered_ids: list[str] = []
    omitted_ids: list[str] = []
    body: list[str] = []
    body_chars = 0
    previous_bucket = None
    metadata_fits = fixed_chars + len(summary(0)) <= budget
    for item, item_id, bucket, row in records:
        title = _BUCKET_TITLES[bucket] + "\n" if bucket != previous_bucket else ""
        addition_chars = len(title) + len(row)
        if metadata_fits and fixed_chars + len(summary(len(selected) + 1)) + body_chars + addition_chars <= budget:
            body.extend((title, row))
            body_chars += addition_chars
            selected.append(item)
            rendered_ids.append(item_id)
            previous_bucket = bucket
        else:
            omitted.append(item)
            omitted_ids.append(item_id)
    rendered = "".join((header, coverage, summary(len(selected)), *body, usage)) if metadata_fits else ""
    return {"text": rendered, "truncated": bool(omitted) or not metadata_fits, "rendered_counts": _bucket_counts(selected), "omitted_count": len(omitted), "rendered_chars": len(rendered), "rendered_ids": rendered_ids, "omitted_ids": omitted_ids, "omitted_counts": _bucket_counts(omitted)}


def _render_packet(flags: QueryFlags, evidence: Sequence[Evidence], scope: str, private: bool, max_chars: int) -> str:
    return str(_render_packet_info(flags, evidence, scope, private, max_chars)["text"])


def _scene_query_enabled(text: str, flags: QueryFlags) -> bool:
    query = _clean(text, 80)
    if flags.intent == "greeting":
        return False
    if len(query) <= 8 and re.fullmatch(r"(?:好|好的|嗯|嗯嗯|哦|哦哦|行|可以|收到|在|在吗|早|早安|晚安|谢谢|没事|是的|对|对的|确认|6|ok|OK)[！!。．\s]*", query):
        return False
    return True

def build_fadianji_evidence_packet(text: str, persona: Any = "fadianji", scope_key: str = "", is_private: bool = False, user_id: str = "", group_id: str = "", memory_store: Any = None, rag_store: Any = None, max_chars: int = _DEFAULT_MAX_CHARS, *, quoted_text: str = "", scene_k: int = 3, book_k: int = 3, semantic: bool = False, feed_store: Any = None, feed_max_items: int = 5, query_flags: QueryFlags | Mapping[str, Any] | None = None, display_name: str = "", corpus_profile_enabled: bool = True, corpus_profile_max_chars: int = 320) -> dict[str, Any]:
    """Build evidence and a bounded prompt without changing store/scope selection.

    ``max_chars`` limits only ``text`` (Python characters, not bytes or tokens).
    ``evidence``, ``scene_matches`` and ``counts`` remain complete diagnostics;
    ``rendered_counts`` and ``omitted_count`` describe the rows actually in text.
    ``truncated`` is also true when even the complete header/rules do not fit.
    """
    scope, private = _scope_value(scope_key, is_private=is_private, user_id=user_id, group_id=group_id)
    quote_query = _clean(quoted_text, 240)
    if not re.search(r"[^\s,，;；]", quote_query):
        quote_query = ""
    current_query = _clean(text, 160) if quote_query else text
    search_query = (quote_query if len(current_query) <= 8 else f"{current_query} {quote_query}") if quote_query else text
    if isinstance(query_flags, QueryFlags): flags = query_flags
    elif isinstance(query_flags, Mapping):
        detected = detect_fadianji_query_flags(text); flags = QueryFlags(_clean(query_flags.get("intent") or detected.intent, 40), bool(query_flags.get("memory_need", detected.memory_need)), bool(query_flags.get("anaphora", detected.anaphora)))
    else: flags = detect_fadianji_query_flags(text)
    traces = {name: _SourceTrace(name) for name in ("memory", "corpus", "feed", "book", "rag", "scene")}
    source_status: dict[str, dict[str, Any]] = {}
    collected: dict[str, list[Evidence]] = {}
    scene_matches: list[SceneMatch] = []

    def collect(name: str, operation: Any, *, blocked: str = "") -> None:
        if blocked:
            source_status[name] = {"state": blocked, "count": 0, "collected_count": 0}
            collected[name] = []
            return
        trace = traces[name]
        rows = trace.call(operation) or []
        accepted: list[Evidence] = []
        for row in rows:
            if not private and (row.private or row.scope.startswith("private:")):
                trace.rejected += 1
            elif row.text:
                accepted.append(row)
        collected[name] = accepted

    def store_gate(store: Any) -> str:
        return "unavailable" if store is None else "disabled" if getattr(store, "enabled", True) is False else ""

    memory_gate = store_gate(memory_store)
    if not memory_gate and not (user_id if private else group_id):
        memory_gate = "skipped"
    if not memory_gate and quote_query:
        scope_parts = scope.split(":")
        expected = str(user_id if private else group_id).strip()
        valid_scope = len(scope_parts) >= 2 and scope_parts[1] == expected
        if private:
            valid_scope = valid_scope and len(scope_parts) == 2
        elif len(scope_parts) > 2:
            valid_scope = valid_scope and len(scope_parts) == 4 and scope_parts[2] == "user" and scope_parts[3] == str(user_id or "").strip()
        if not valid_scope:
            memory_gate = "scope_mismatch"
    collect("memory", lambda: _memory_evidence(memory_store, text, scope, private, str(user_id or ""), str(group_id or ""), flags, quoted_text=quote_query, trace=traces["memory"]), blocked=memory_gate)
    collect("corpus", lambda: _corpus_profile_evidence(str(display_name or ""), scope, private, corpus_profile_enabled=corpus_profile_enabled, corpus_profile_max_chars=corpus_profile_max_chars, trace=traces["corpus"]), blocked="disabled" if not corpus_profile_enabled or corpus_profile_max_chars <= 0 else "skipped" if not private and not str(display_name or "").strip() else "")
    collect("feed", lambda: _feed_evidence(feed_store, scope, int(feed_max_items)), blocked="disabled" if not feed_max_items or feed_max_items <= 0 else store_gate(feed_store))
    collect("book", lambda: _activate_character_book(text, persona, book_k), blocked="disabled" if book_k <= 0 else "")
    collect("rag", lambda: _rag_evidence(rag_store, search_query, scope, persona, private, trace=traces["rag"]), blocked=store_gate(rag_store))

    def scene_rows() -> list[Evidence]:
        matches = match_scene_pairs(search_query, k=scene_k, scope_key=scope, is_private=private, semantic=semantic)
        for match in matches:
            if (match.private and (not private or match.source_scope != scope)) or (match.source_scope.startswith("group:") and match.source_scope != scope):
                traces["scene"].rejected += 1
            else:
                scene_matches.append(match)
        return _scene_evidence(scene_matches)

    collect("scene", scene_rows, blocked="disabled" if scene_k <= 0 else "skipped" if not quote_query and not _scene_query_enabled(text, flags) else "")
    all_rows = [item for rows in collected.values() for item in rows]
    evidence = _dedupe_and_sort(all_rows, private)
    owners: dict[int, str] = {}
    for name, rows in collected.items():
        for item in rows:
            owners.setdefault(id(item), name)
    for name, trace in traces.items():
        if name not in source_status:
            source_status[name] = trace.status(len(collected[name]), sum(owners[id(item)] == name for item in evidence))
    source_status = {name: source_status[name] for name in traces}
    budget = max(0, int(max_chars))
    return {**_render_packet_info(flags, evidence, scope, private, budget, source_status=source_status), "schema_version": 2, "scope": scope, "is_private": private, "flags": flags.as_dict(), "evidence": [item.as_dict() for item in evidence], "scene_matches": [match.as_dict() for match in scene_matches], "counts": _bucket_counts(evidence), "max_chars": budget, "source_status": source_status, "diagnostics": [diagnostic for trace in traces.values() for diagnostic in trace.diagnostics], "duplicate_count": len(all_rows) - len(evidence)}


def build_fadianji_harness_context(text: str, persona: Any = "fadianji", scope_key: str = "", is_private: bool = False, user_id: str = "", group_id: str = "", memory_store: Any = None, rag_store: Any = None, max_chars: int = _DEFAULT_MAX_CHARS, **kwargs: Any) -> str:
    return str(build_fadianji_evidence_packet(text, persona, scope_key, is_private, user_id, group_id, memory_store, rag_store, max_chars, **kwargs)["text"])


__all__ = ["EVIDENCE_USAGE", "Evidence", "QueryFlags", "build_fadianji_evidence_packet", "build_fadianji_harness_context", "detect_fadianji_query_flags"]
