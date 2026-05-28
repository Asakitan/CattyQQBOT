"""S6 v2 片段拼装生成器 (Semantic Composer).

主人 2026-05-29: "NLU 这种, 向量模型理解语义了进行拼装" — 不是乱拼.

两条互补的语义约束:
1. **Mood 一致性** (主路径): 每片段标 mood (intimate/playful/teasing/tired/shy/...)
   compose 选完 body 后, opener/closer 必从 body.mood ∪ "neutral" 的子集里选.
   避免 "(假装严厉) + 主人想看猫猫嘛 + (凑过去亲一下)" 这种 mood 冲突.

2. **bge 向量召回** (可选, 主对话场景): 给 user_text embed 后,
   对每 slot 算 cosine top-K, 在 top-K 内 random. 真"理解语义".

JSON schema v2:
{
  "openers": [
    {"text": "(脸红)", "mood": "shy", "intensity": 0.4},
    {"text": "嗷呜～", "mood": "energetic", "intensity": 0.7}
  ],
  "bodies": [...],
  "closers": [...]
}

兼容 v1 字符串格式: 字符串 = {"text": ..., "mood": "neutral", "intensity": 0.5}.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

from loguru import logger

try:
    import numpy as np  # type: ignore

    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None  # type: ignore[assignment]


_RECENT_FRAG: dict[tuple[str, str, str], Deque[int]] = defaultdict(
    lambda: deque(maxlen=8)
)


# Mood 兼容矩阵: 选 body.mood 后允许的 opener/closer mood.
# "neutral" 兼容所有, 其他 mood 优先匹配同 mood, 否则用 neutral.
_MOOD_NEUTRAL = "neutral"
_MOOD_COMPATIBLE: dict[str, set[str]] = {
    "shy": {"shy", "intimate", _MOOD_NEUTRAL},
    "intimate": {"intimate", "shy", _MOOD_NEUTRAL},
    "playful": {"playful", "teasing", "energetic", _MOOD_NEUTRAL},
    "teasing": {"teasing", "playful", _MOOD_NEUTRAL},
    "energetic": {"energetic", "playful", _MOOD_NEUTRAL},
    "tired": {"tired", "shy", _MOOD_NEUTRAL},
    "tsundere": {"tsundere", "teasing", "shy", _MOOD_NEUTRAL},
    _MOOD_NEUTRAL: set(),  # neutral body 不约束 opener/closer mood
}


class _SafeMap(dict):
    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


@dataclass(slots=True)
class Fragment:
    text: str
    mood: str = _MOOD_NEUTRAL
    intensity: float = 0.5
    affection_min: int = -1  # 桶过滤 (用户 affection 下限, -1 不过滤)
    affection_max: int = 999
    embed: Any = None  # np.ndarray (bge embed) 或 None


def _to_fragment(item: Any) -> Fragment:
    if isinstance(item, str):
        return Fragment(text=item)
    if isinstance(item, dict):
        return Fragment(
            text=str(item.get("text", "")),
            mood=str(item.get("mood", _MOOD_NEUTRAL)),
            intensity=float(item.get("intensity", 0.5)),
            affection_min=int(item.get("affection_min", -1)),
            affection_max=int(item.get("affection_max", 999)),
        )
    return Fragment(text=str(item))


@dataclass(slots=True)
class FragmentComposer:
    name: str
    openers: list[Fragment] = field(default_factory=list)
    bodies: list[Fragment] = field(default_factory=list)
    closers: list[Fragment] = field(default_factory=list)
    separator: str = " "
    opener_cooldown: int = 8
    body_cooldown: int = 15
    closer_cooldown: int = 5
    _embeds_ready: bool = False

    @property
    def combinations(self) -> int:
        a = max(len(self.openers), 1)
        b = max(len(self.bodies), 1)
        c = max(len(self.closers), 1)
        return a * b * c

    def prepare_embeddings(self, embed_batch_fn: Any) -> bool:
        """启动时调用. bge embed 所有 bodies (opener/closer 可选).

        embed_batch_fn: Callable[[list[str]], np.ndarray | None]
        body slot 是语义主体, opener/closer 通常太短无语义.
        """
        if self._embeds_ready or not _HAS_NUMPY or embed_batch_fn is None:
            return self._embeds_ready
        try:
            body_texts = [b.text for b in self.bodies]
            if body_texts:
                vecs = embed_batch_fn(body_texts)
                if vecs is not None and vecs.shape[0] == len(self.bodies):
                    for i, b in enumerate(self.bodies):
                        b.embed = vecs[i].astype(np.float32, copy=False)
            self._embeds_ready = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[composer.{self.name}] embed prepare fail: {exc}")
            return False

    def compose(
        self,
        *,
        user_id: str = "",
        affection_level: int | None = None,
        user_text_vec: Any = None,
        body_top_k: int = 5,
        render_vars: dict[str, Any] | None = None,
        cat_suffixes: list[str] | None = None,
    ) -> str | None:
        """语义对齐拼装:
        1) body 池 → affection 桶过滤 → user_text_vec 向量召回 top-K → random
        2) opener/closer 池 → body.mood 兼容子集 → recently-used 抑制 → random
        """
        if not self.bodies:
            return None

        # 1) 选 body
        body_candidates = self._filter_by_affection(self.bodies, affection_level)
        if not body_candidates:
            body_candidates = list(self.bodies)
        body = self._pick_body(body_candidates, user_text_vec, body_top_k, user_id)
        if body is None:
            return None
        body_mood = body.mood

        # 2) opener/closer 必从 body.mood 兼容子集里选
        compat = _MOOD_COMPATIBLE.get(body_mood, {body_mood, _MOOD_NEUTRAL}) or {body_mood, _MOOD_NEUTRAL}
        if body_mood == _MOOD_NEUTRAL:
            compat = set()  # 不约束

        opener = self._pick_mood_matched(
            self.openers, compat, "opener", self.opener_cooldown, user_id, affection_level
        )
        closer = self._pick_mood_matched(
            self.closers, compat, "closer", self.closer_cooldown, user_id, affection_level
        )

        parts = [p.text for p in (opener, body, closer) if p is not None and p.text]
        raw = self.separator.join(parts)
        return self._render(raw, render_vars, cat_suffixes)

    @staticmethod
    def _filter_by_affection(pool: list[Fragment], affection: int | None) -> list[Fragment]:
        if affection is None:
            return list(pool)
        out: list[Fragment] = []
        for f in pool:
            lo = f.affection_min if f.affection_min >= 0 else 0
            hi = f.affection_max
            if lo <= affection <= hi:
                out.append(f)
        return out

    def _pick_body(
        self,
        pool: list[Fragment],
        user_text_vec: Any,
        top_k: int,
        user_id: str,
    ) -> Fragment | None:
        if not pool:
            return None
        # recently-used 抑制
        recent_idx = self._recent_idx_set("body", user_id, len(pool))
        # 向量召回 (如果有 user_text_vec 且 bodies 有 embed)
        if user_text_vec is not None and _HAS_NUMPY and any(b.embed is not None for b in pool):
            scores: list[tuple[int, float]] = []
            try:
                q = np.asarray(user_text_vec, dtype=np.float32)
                for i, b in enumerate(pool):
                    if b.embed is None:
                        continue
                    cos = float(np.dot(q, b.embed))
                    scores.append((i, cos))
            except Exception:  # noqa: BLE001
                scores = []
            if scores:
                scores.sort(key=lambda x: x[1], reverse=True)
                top = [i for i, _ in scores[:top_k] if i not in recent_idx]
                if not top:
                    top = [scores[0][0]]
                chosen = random.choice(top)
                self._record_used("body", user_id, chosen)
                return pool[chosen]

        # 无 user_text_vec → recently-used 抑制 + random
        available = [i for i in range(len(pool)) if i not in recent_idx]
        if not available:
            available = list(range(len(pool)))
        chosen = random.choice(available)
        self._record_used("body", user_id, chosen)
        return pool[chosen]

    def _pick_mood_matched(
        self,
        pool: list[Fragment],
        mood_compat: set[str],
        slot: str,
        cooldown: int,
        user_id: str,
        affection: int | None,
    ) -> Fragment | None:
        if not pool:
            return None
        # affection 过滤
        candidates = self._filter_by_affection(pool, affection)
        if not candidates:
            candidates = list(pool)
        # mood 过滤 (空 compat = 不约束)
        if mood_compat:
            mood_filtered = [c for c in candidates if c.mood in mood_compat]
            if mood_filtered:
                candidates = mood_filtered
        # recently-used 抑制
        recent_idx = self._recent_idx_set(slot, user_id, len(candidates))
        available_local = [i for i in range(len(candidates)) if i not in recent_idx]
        if not available_local:
            available_local = list(range(len(candidates)))
        chosen_local = random.choice(available_local)
        self._record_used(slot, user_id, chosen_local)
        return candidates[chosen_local]

    def _recent_idx_set(self, slot: str, user_id: str, pool_size: int) -> set[int]:
        if not user_id:
            return set()
        key = (user_id, self.name, slot)
        if key not in _RECENT_FRAG:
            _RECENT_FRAG[key] = deque(maxlen=max(getattr(self, f"{slot}_cooldown", 8), 1))
        return set(_RECENT_FRAG[key])

    def _record_used(self, slot: str, user_id: str, idx: int) -> None:
        if not user_id:
            return
        key = (user_id, self.name, slot)
        if key not in _RECENT_FRAG:
            _RECENT_FRAG[key] = deque(maxlen=max(getattr(self, f"{slot}_cooldown", 8), 1))
        _RECENT_FRAG[key].append(idx)

    def _render(
        self,
        template: str,
        render_vars: dict[str, Any] | None,
        cat_suffixes: list[str] | None,
    ) -> str:
        if not template:
            return ""
        vars_with_suffix = dict(render_vars or {})
        if "cat_suffix" not in vars_with_suffix:
            suffixes = cat_suffixes or ["喵～", "嗷呜～", "ฅฅ"]
            vars_with_suffix["cat_suffix"] = random.choice(suffixes)
        try:
            return template.format_map(_SafeMap(vars_with_suffix))
        except Exception:  # noqa: BLE001
            return template


def load_composer_from_json(json_path: str | Path) -> FragmentComposer:
    json_path = Path(json_path)
    if not json_path.exists():
        logger.warning(f"[composer] {json_path} not found")
        return FragmentComposer(name=json_path.stem)
    try:
        with json_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[composer] {json_path} parse fail: {exc}")
        return FragmentComposer(name=json_path.stem)

    if not isinstance(data, dict):
        return FragmentComposer(name=json_path.stem)

    comp = FragmentComposer(
        name=json_path.stem,
        openers=[_to_fragment(s) for s in (data.get("openers") or [])],
        bodies=[_to_fragment(s) for s in (data.get("bodies") or [])],
        closers=[_to_fragment(s) for s in (data.get("closers") or [])],
        separator=str(data.get("separator", " ")),
        opener_cooldown=int(data.get("opener_cooldown", 8)),
        body_cooldown=int(data.get("body_cooldown", 15)),
        closer_cooldown=int(data.get("closer_cooldown", 5)),
    )
    moods_seen = {f.mood for f in comp.bodies}
    logger.info(
        f"[composer.{comp.name}] loaded openers={len(comp.openers)} "
        f"bodies={len(comp.bodies)} closers={len(comp.closers)} "
        f"combinations={comp.combinations} body_moods={sorted(moods_seen)}"
    )
    return comp


_GLOBAL_COMPOSERS: dict[str, FragmentComposer] = {}


def get_composer(fragments_dir: str | Path, name: str) -> FragmentComposer:
    key = f"{fragments_dir}::{name}"
    comp = _GLOBAL_COMPOSERS.get(key)
    if comp is None:
        comp = load_composer_from_json(Path(fragments_dir) / f"{name}.json")
        # 启动时 prepare embeddings (复用 bge ONNX)
        try:
            from ..nlu.text2vec_engine import embed_sync_batch  # type: ignore
            comp.prepare_embeddings(embed_sync_batch)
        except Exception:  # noqa: BLE001
            pass
        _GLOBAL_COMPOSERS[key] = comp
    return comp


def reset_composers_for_test() -> None:
    _GLOBAL_COMPOSERS.clear()
    _RECENT_FRAG.clear()
