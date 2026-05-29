"""L3 txtai 大语料库向量召回.

与 L2 (Semantic Router) 的差异:
- L2: 200-500 条精挑细选 routes, 每条多 utterances + 多 responses, 强人格化
- L3: 1000-10000+ 条 QA pair, 来源 = 主 AI 输出蒸馏 + bootstrap 历史聊天
- L2 命中即直答, L3 命中作为候选, 可能被 L4 Ollama 风格化

语料格式 (qa_corpus.jsonl):
    {"q": "你今天吃饭了吗", "a": "嗷呜～吃过啦主人...", "intent": "question",
     "tags": ["daily"], "weight": 1.0, "source": "evolution-2026-05-28"}

txtai 用自己内置的 BAAI/bge-small-zh-v1.5 加载 (和 text2vec_engine 同模型),
索引到磁盘 (catty_cpu_engine_txtai_index_path) 后续启动直接 load 无需重 build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from txtai.embeddings import Embeddings  # type: ignore

    _HAS_TXTAI = True
except ImportError:
    _HAS_TXTAI = False
    Embeddings = None  # type: ignore[assignment]


@dataclass(slots=True)
class CorpusEntry:
    qid: int
    q: str
    a: str
    intent: str = "default"
    tags: list[str] = None  # type: ignore[assignment]
    weight: float = 1.0
    source: str = ""

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []


@dataclass(slots=True)
class CorpusMatchResult:
    qid: int
    q: str
    a: str
    intent: str
    score: float
    weight: float
    is_direct: bool


class TxtaiCorpus:
    """L3 大语料库, 用 txtai 做持久化向量召回."""

    def __init__(
        self,
        corpus_path: str | Path,
        index_path: str | Path,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        live_corpus_path: str | Path | None = None,
    ) -> None:
        self._corpus_path = Path(corpus_path)
        self._index_path = Path(index_path)
        self._model_name = model_name
        # S6 (主人 2026-05-29): live 蒸馏语料 (DeepSeek 回答自动写入), 与 bootstrap 静态 corpus 分文件
        self._live_corpus_path = Path(live_corpus_path) if live_corpus_path else None
        self._entries: list[CorpusEntry] = []
        self._embeddings: Any = None
        self._prepared = False
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def size(self) -> int:
        return len(self._entries)

    def prepare(self) -> bool:
        if self._prepared:
            return self._ready
        self._prepared = True

        if not _HAS_TXTAI:
            logger.warning("[cpu_engine.L3] txtai not installed, L3 disabled")
            return False

        if not self._corpus_path.exists():
            logger.warning(
                f"[cpu_engine.L3] corpus not found: {self._corpus_path}, L3 disabled"
            )
            return False

        self._entries = self._load_corpus()
        if not self._entries:
            logger.warning("[cpu_engine.L3] empty corpus, L3 disabled")
            return False

        try:
            if self._index_path.exists() and (self._index_path / "config").exists():
                self._embeddings = Embeddings()
                self._embeddings.load(str(self._index_path))
                if self._embeddings.count() == len(self._entries):
                    logger.info(
                        f"[cpu_engine.L3] loaded index from {self._index_path} "
                        f"({len(self._entries)} entries)"
                    )
                    self._ready = True
                    return True
                logger.info(
                    f"[cpu_engine.L3] index count {self._embeddings.count()} != "
                    f"corpus {len(self._entries)}, rebuilding"
                )

            self._embeddings = Embeddings({"path": self._model_name, "content": False})
            self._embeddings.index(
                [(e.qid, e.q, None) for e in self._entries]
            )
            self._index_path.mkdir(parents=True, exist_ok=True)
            self._embeddings.save(str(self._index_path))
            logger.info(
                f"[cpu_engine.L3] built and saved index: {len(self._entries)} entries"
            )
            self._ready = True
            return True
        except Exception as exc:
            logger.exception(f"[cpu_engine.L3] index init failed: {exc}")
            self._ready = False
            return False

    def match(
        self,
        text: str,
        *,
        direct_threshold: float = 0.82,
        candidate_threshold: float = 0.70,
        intent_filter: list[str] | None = None,
    ) -> CorpusMatchResult | None:
        if not self._ready or self._embeddings is None or not text:
            return None
        try:
            results = self._embeddings.search(text, limit=5)
        except Exception as exc:
            logger.warning(f"[cpu_engine.L3] search failed: {exc}")
            return None
        if not results:
            return None

        entries_by_qid = {e.qid: e for e in self._entries}
        best: CorpusMatchResult | None = None
        for qid, score in results:
            entry = entries_by_qid.get(qid)
            if entry is None:
                continue
            if intent_filter and entry.intent not in intent_filter and entry.intent != "default":
                continue
            weighted = float(score) * entry.weight
            if weighted < candidate_threshold:
                continue
            if best is None or weighted > best.score:
                best = CorpusMatchResult(
                    qid=entry.qid,
                    q=entry.q,
                    a=entry.a,
                    intent=entry.intent,
                    score=weighted,
                    weight=entry.weight,
                    is_direct=weighted >= direct_threshold,
                )
        return best

    def _load_corpus(self) -> list[CorpusEntry]:
        entries: list[CorpusEntry] = []
        # 静态 bootstrap corpus
        self._load_one_jsonl(self._corpus_path, entries)
        # S6 live 蒸馏 corpus (DeepSeek 自动写入), 文件可能不存在
        if self._live_corpus_path and self._live_corpus_path.exists():
            before = len(entries)
            self._load_one_jsonl(self._live_corpus_path, entries)
            logger.info(
                f"[cpu_engine.L3] loaded {len(entries) - before} live entries from {self._live_corpus_path}"
            )
        return entries

    def _load_one_jsonl(self, path: Path, entries: list[CorpusEntry]) -> None:
        """共享: 读 jsonl 追加 entries, qid 自动递增. path 不存在直接返回."""
        if not path.exists():
            return
        with path.open(encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        f"[cpu_engine.L3] {path}:{line_no} JSON error: {exc}"
                    )
                    continue
                if not isinstance(data, dict):
                    continue
                q = data.get("q") or data.get("question") or ""
                a = data.get("a") or data.get("answer") or ""
                if not q or not a:
                    continue
                entries.append(
                    CorpusEntry(
                        qid=len(entries),
                        q=str(q),
                        a=str(a),
                        intent=str(data.get("intent", "default")),
                        tags=list(data.get("tags", [])),
                        weight=float(data.get("weight", 1.0)),
                        source=str(data.get("source", "")),
                    )
                )

    def reload(self) -> bool:
        """S6 (主人 2026-05-29) 重载 corpus + 重 build index.

        用于 corpus_distill 累积 N 条新增后异步触发, 不会影响正在跑的请求 (旧 _embeddings 被替换).
        失败时保留旧 _ready 状态 (不退化).
        """
        if not _HAS_TXTAI:
            return False
        try:
            new_entries = self._load_corpus()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cpu_engine.L3] reload load_corpus failed: {exc}")
            return False
        if not new_entries:
            logger.warning("[cpu_engine.L3] reload got empty corpus, keep old state")
            return False
        try:
            new_embeddings = Embeddings({"path": self._model_name, "content": False})
            new_embeddings.index([(e.qid, e.q, None) for e in new_entries])
            # 原子替换
            self._entries = new_entries
            self._embeddings = new_embeddings
            self._index_path.mkdir(parents=True, exist_ok=True)
            new_embeddings.save(str(self._index_path))
            self._ready = True
            logger.info(
                f"[cpu_engine.L3] reload OK: rebuilt index with {len(new_entries)} entries"
            )
            return True
        except Exception as exc:
            logger.exception(f"[cpu_engine.L3] reload rebuild failed: {exc}")
            return False
