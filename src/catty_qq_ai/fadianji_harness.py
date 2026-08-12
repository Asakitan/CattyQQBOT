"""机机回复前的轻量证据编排。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .fdj_scene_retrieval import SceneMatch, match_scene_pairs


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

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "scope": self.scope, "text": self.text, "relevance": round(self.relevance, 6), "recency": round(self.recency, 6), "importance": round(self.importance, 6), "authority": round(self.authority, 6), "private": self.private}


_DEFAULT_MAX_CHARS = 2800
_MEMORY_BAD_TEXT = ("暂无有效", "无文字交流", "无有效信息", "memory disabled")


def _clean(value: Any, max_chars: int = 600) -> str:
    return " ".join(str(value or "").strip().split())[:max_chars]


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
    try:
        from .personas.fadianji import FADIANJI_CHARACTER_BOOK
        return FADIANJI_CHARACTER_BOOK
    except Exception:
        return ()


def _activate_character_book(text: str, persona: Any, limit: int) -> list[Evidence]:
    query = _clean(text, 1000).casefold()
    scored: list[tuple[float, int, str, Any]] = []
    for entry in _book_entries(persona):
        hits = [key for key in tuple(getattr(entry, "keys", ()) or ()) if key and str(key).casefold() in query]
        if hits:
            scored.append((max(len(str(key)) for key in hits) + len(hits) * 0.25, int(getattr(entry, "order", 100) or 100), str(getattr(entry, "identifier", "character_book")), entry))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [Evidence(f"character_book:{identifier}", "persona:fadianji", _clean(getattr(entry, "content", ""), 520), min(1.0, score / 12.0), 0.0, 0.72, 0.78, False, 30) for score, _order, identifier, entry in scored[: max(0, limit)] if _clean(getattr(entry, "content", ""), 520)]


def _call_store(store: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(store, method, None)
    if not callable(function): return None
    try: return function(*args, **kwargs)
    except TypeError:
        try: return function(*args)
        except Exception: return None
    except Exception: return None


def _group_profile(store: Any, user_id: str, group_id: str) -> Mapping[str, Any]:
    function = getattr(store, "_profile_for", None)
    if callable(function):
        try:
            value = function(user_id, group_id)
            if isinstance(value, Mapping): return value
        except Exception: pass
    for method in ("group_profile", "lookup_group_profile"):
        value = _call_store(store, method, user_id, group_id)
        if isinstance(value, Mapping): return value
    return {}


def _profile_evidence(profile: Mapping[str, Any], scope: str, private: bool) -> list[Evidence]:
    parts: list[str] = []
    for label, key in (("称呼", "preferred_name"), ("性别", "gender"), ("印象", "impression"), ("偏好", "interests"), ("边界", "boundaries"), ("相关梗", "meme_hooks")):
        value = profile.get(key)
        if isinstance(value, (list, tuple, set)): value = "/".join(_clean(item, 40) for item in value if _clean(item, 40))
        value = _clean(value, 180)
        if value and not any(bad in value for bad in _MEMORY_BAD_TEXT): parts.append(f"{label}={value}")
    return [Evidence("memory.profile", scope, "; ".join(parts), 0.8, 0.65, 0.82, 0.9, private, 10)] if parts else []


def _notes_evidence(notes: Any, scope: str, private: bool) -> list[Evidence]:
    if not isinstance(notes, Mapping): return []
    result: list[Evidence] = []
    for bucket in ("user_notes", "group_notes"):
        values = notes.get(bucket) or []
        if not isinstance(values, (list, tuple)): continue
        for entry in values:
            value = _clean(entry.get("text") if isinstance(entry, Mapping) else entry, 300)
            if value and not any(bad in value for bad in _MEMORY_BAD_TEXT): result.append(Evidence("memory.notes", scope, value, 0.76, 0.82, 0.86, 0.88, private, 12))
    return result


def _recall_evidence(recall: Any, scope: str, private: bool) -> list[Evidence]:
    if not isinstance(recall, Mapping): return []
    result: list[Evidence] = []
    summary = _clean(recall.get("long_term_summary"), 500)
    if summary and not any(bad in summary for bad in _MEMORY_BAD_TEXT): result.append(Evidence("memory.recall", scope, f"长期摘要: {summary}", 0.7, 0.55, 0.78, 0.8, private, 20))
    for match in recall.get("matches") or ():
        if isinstance(match, Mapping) and _clean(match.get("text"), 320): result.append(Evidence("memory.recall", scope, _clean(match.get("text"), 320), 0.72, 0.62, 0.62, 0.7, private, 20))
    return result


def _memory_evidence(store: Any, text: str, scope: str, private: bool, user_id: str, group_id: str, flags: QueryFlags) -> list[Evidence]:
    if store is None: return []
    result: list[Evidence] = []
    if private:
        profile = _call_store(store, "lookup_user_profile", user_id, "") if user_id else {}
        if isinstance(profile, Mapping): result.extend(_profile_evidence(profile, scope, True))
        if flags.memory_need or flags.anaphora:
            notes = _call_store(store, "recall_notes", user_id=user_id, limit=4) if user_id else {}
            recall = _call_store(store, "recall", user_id=user_id, keywords=text, limit=4) if user_id else {}
            result.extend(_notes_evidence(notes, scope, True)); result.extend(_recall_evidence(recall, scope, True))
    else:
        profile = _group_profile(store, user_id, group_id) if user_id and group_id else {}
        result.extend(_profile_evidence(profile, scope, False))
        if flags.memory_need or flags.anaphora:
            notes = _call_store(store, "recall_notes", group_id=group_id, limit=4) if group_id else {}
            recall = _call_store(store, "recall", group_id=group_id, keywords=text, limit=4) if group_id else {}
            result.extend(_notes_evidence(notes, scope, False)); result.extend(_recall_evidence(recall, scope, False))
    return result


def _recency(value: Any) -> float:
    try: timestamp = float(value or 0.0)
    except (TypeError, ValueError):
        try: timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError): timestamp = 0.0
    if timestamp <= 0: return 0.0
    return 1.0 / (1.0 + max(0.0, (datetime.now(timezone.utc).timestamp() - timestamp) / 86400.0) / 30.0)


def _rag_evidence(store: Any, text: str, scope: str, persona: Any, private: bool) -> list[Evidence]:
    query = getattr(store, "query", None) if store is not None else None
    if not callable(query): return []
    try: hits = query(scope, text, top_k=4, persona=_persona_name(persona))
    except TypeError:
        try: hits = query(scope, text, top_k=4)
        except Exception: return []
    except Exception: return []
    result: list[Evidence] = []
    for hit in hits or ():
        if not isinstance(hit, (tuple, list)) or len(hit) < 2: continue
        metadata = hit[2] if len(hit) > 2 and isinstance(hit[2], Mapping) else {}
        metadata_scope = _clean(metadata.get("scope"), 120)
        if metadata_scope and metadata_scope != scope: continue
        if not private and (metadata_scope.startswith("private:") or bool(metadata.get("private"))): continue
        value = _clean(hit[1], 360)
        if value: result.append(Evidence(f"rag.{_clean(metadata.get('role') or 'memory', 40)}", scope, value, max(0.0, min(1.0, float(hit[0] or 0.0))), _recency(metadata.get("ts")), 0.58, 0.64, private, 40))
    return result


def _scene_evidence(matches: Sequence[SceneMatch]) -> list[Evidence]:
    return [Evidence(f"scene.{match.source}", match.source_scope, f"{match.trigger} → 机机: {match.reply}（分类={match.category}）", max(0.0, min(1.0, match.score / 8.0)), 0.15, 0.34, 0.38, match.private, 60) for match in matches]


def _dedupe_and_sort(evidence: Sequence[Evidence], private: bool) -> list[Evidence]:
    chosen: dict[str, Evidence] = {}
    for item in evidence:
        if not item.text or (not private and item.private): continue
        key = re.sub(r"\s+", " ", item.text.casefold()).strip(); previous = chosen.get(key)
        current = (item.rank, -item.authority, -item.relevance, -item.recency, item.source, item.scope, item.text)
        old = (previous.rank, -previous.authority, -previous.relevance, -previous.recency, previous.source, previous.scope, previous.text) if previous else None
        if previous is None or current < old: chosen[key] = item
    result = list(chosen.values()); result.sort(key=lambda item: (item.rank, -item.authority, -item.relevance, -item.recency, item.source, item.scope, item.text)); return result


def _render_packet(flags: QueryFlags, evidence: Sequence[Evidence], scope: str, private: bool, max_chars: int) -> str:
    header = f"【机机·post-boundary evidence】\nscope={scope}; private={1 if private else 0}; intent={flags.intent}; memory_need={1 if flags.memory_need else 0}; anaphora={1 if flags.anaphora else 0}\n"
    guidance = "【来源与冲突规则】\n当前消息与本轮明确事实 > 同 scope 的 MemoryStore profile/notes/recall > 人格 character_book > 同 scope RAG > scene 真实对话。\nscene 只提供机机的口吻、长度和反应方式，不能覆盖事实、制造实时状态，也不能把私聊证据带入群聊。\n来源冲突时优先直接、较新、scope 一致的事实；无法确认就保留未知，不补写确定结论。\n"
    output = [header]
    for title, prefix in (("【MemoryStore/事实背景】", "memory."), ("【character_book/角色知识】", "character_book:"), ("【RAG/历史片段】", "rag."), ("【scene/口吻母本】", "scene.")):
        items = [item for item in evidence if item.source.startswith(prefix)]
        if items:
            output.append(title + "\n")
            output.extend(f"- [{item.source} scope={item.scope} relevance={item.relevance:.2f} recency={item.recency:.2f} importance={item.importance:.2f} authority={item.authority:.2f}] {item.text}\n" for item in items)
    output.append(guidance); rendered = "".join(output).strip()
    return rendered if len(rendered) <= max_chars else ("" if max_chars <= 0 else rendered[:max_chars].rstrip())


def build_fadianji_evidence_packet(text: str, persona: Any = "fadianji", scope_key: str = "", is_private: bool = False, user_id: str = "", group_id: str = "", memory_store: Any = None, rag_store: Any = None, max_chars: int = _DEFAULT_MAX_CHARS, *, scene_k: int = 5, book_k: int = 3, semantic: bool = True, query_flags: QueryFlags | Mapping[str, Any] | None = None) -> dict[str, Any]:
    scope, private = _scope_value(scope_key, is_private=is_private, user_id=user_id, group_id=group_id)
    if isinstance(query_flags, QueryFlags): flags = query_flags
    elif isinstance(query_flags, Mapping):
        detected = detect_fadianji_query_flags(text); flags = QueryFlags(_clean(query_flags.get("intent") or detected.intent, 40), bool(query_flags.get("memory_need", detected.memory_need)), bool(query_flags.get("anaphora", detected.anaphora)))
    else: flags = detect_fadianji_query_flags(text)
    scene_matches = match_scene_pairs(text, k=scene_k, scope_key=scope, is_private=private, semantic=semantic)
    evidence: list[Evidence] = []; evidence.extend(_memory_evidence(memory_store, text, scope, private, str(user_id or ""), str(group_id or ""), flags)); evidence.extend(_activate_character_book(text, persona, book_k)); evidence.extend(_rag_evidence(rag_store, text, scope, persona, private)); evidence.extend(_scene_evidence(scene_matches)); evidence = _dedupe_and_sort(evidence, private)
    counts: dict[str, int] = {}
    for item in evidence:
        bucket = "character_book" if item.source.startswith("character_book:") else item.source; counts[bucket] = counts.get(bucket, 0) + 1
    return {"text": _render_packet(flags, evidence, scope, private, max(0, int(max_chars))), "scope": scope, "is_private": private, "flags": flags.as_dict(), "evidence": [item.as_dict() for item in evidence], "scene_matches": [match.as_dict() for match in scene_matches], "counts": counts, "max_chars": max(0, int(max_chars))}


def build_fadianji_harness_context(text: str, persona: Any = "fadianji", scope_key: str = "", is_private: bool = False, user_id: str = "", group_id: str = "", memory_store: Any = None, rag_store: Any = None, max_chars: int = _DEFAULT_MAX_CHARS, **kwargs: Any) -> str:
    return str(build_fadianji_evidence_packet(text, persona, scope_key, is_private, user_id, group_id, memory_store, rag_store, max_chars, **kwargs)["text"])


__all__ = ["Evidence", "QueryFlags", "build_fadianji_evidence_packet", "build_fadianji_harness_context", "detect_fadianji_query_flags"]