"""ChromaDB 向量记忆 RAG — per-scope 把 chat history 向量化 + top-K 召回。

跟现有 memory_store / scope_lorebook 的分工:
- memory_store: group/private/member summaries (5.5 总结后的浓缩段, 持久化 JSON)
- scope_lorebook: AI 学的 per-scope 小事 (5.5 自动总结 + BFS 关键词命中)
- catty_rag (本模块): **per-scope 完整 chat history 向量化** + 语义召回 — 即使关键词没命中,
  语义近的旧对话也能召回, 让笨猫『记得久远的事』

设计:
- 用 chromadb PersistentClient + DefaultEmbeddingFunction (内置 all-MiniLM-L6-v2 ONNX)
- per-scope collection 隔离 (collection_name = catty_scope_<sanitized_scope>)
- 每条 user/assistant 消息 add 一次, doc_id = `<scope>:<timestamp>:<role>`
- 召回 top-K=3 (可调), 按余弦相似度
- **Graceful fallback**: chromadb import 失败 / init 失败 → store._enabled=False,
  add/query 都 no-op, catty 主流程不受影响
- 不重复存: 同一 doc_id 已存就跳过 (用 upsert 而不是 add)

落盘到 memory_dir/chroma/ (chromadb 自动用 sqlite + DuckDB)。
首次启动会下载 ~80MB ONNX embedding 模型, 之后 cache 在 ~/.cache/chroma。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_MAX_RECALL_TOP_K = 5     # query 默认 top-K
_MAX_PER_SCOPE_DOCS = 2000  # per-scope 文档上限, 老的自动 drop
_RECALL_MIN_SIMILARITY = 0.3  # 余弦相似度低于此值不召回 (噪音过滤)


def _sanitize_collection_name(scope: str) -> str:
    """Chromadb collection 名只允许 [a-zA-Z0-9_-], 必须 3-512 字, 首尾字母数字。
    scope 类似 'group:123' / 'private:456', 转成 collection_safe。"""
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", scope or "")
    s = s.strip("_-")
    if not s:
        s = "default"
    if len(s) < 3:
        s = f"scope_{s}"
    if len(s) > 60:
        s = s[:60]
    return f"catty_{s}"


class CattyRAGStore:
    """ChromaDB-based per-scope 向量记忆。enabled=False 时全部 no-op。"""

    def __init__(self, memory_path: str | Path) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._persist_dir = p.parent / "chroma"
        self._lock = threading.RLock()
        self._client = None
        self._collections: dict[str, Any] = {}  # scope → collection (cache)
        self._enabled = False
        self._init_error: str = ""
        self._try_init()

    def _try_init(self) -> None:
        """Try-import chromadb + 初始化 PersistentClient. 失败则 disabled."""
        try:
            import chromadb  # type: ignore
        except ImportError as exc:
            self._init_error = f"chromadb not installed: {exc}"
            logger.warning(f"catty_rag: chromadb not installed, RAG disabled — pip install chromadb to enable")
            return
        try:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self._persist_dir))
            self._enabled = True
            logger.info(f"catty_rag: chromadb initialized at {self._persist_dir}")
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"chromadb init failed: {exc}"
            logger.warning(f"catty_rag: init failed, RAG disabled — {exc}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def init_error(self) -> str:
        return self._init_error

    def _get_collection(self, scope: str):  # noqa: ANN202
        if not self._enabled or not scope:
            return None
        name = _sanitize_collection_name(scope)
        with self._lock:
            if name in self._collections:
                return self._collections[name]
            try:
                col = self._client.get_or_create_collection(
                    name=name,
                    metadata={"scope": scope, "hnsw:space": "cosine"},
                )
                self._collections[name] = col
                return col
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"catty_rag: get_collection({name}) failed: {exc}")
                return None

    def add(
        self,
        scope: str,
        text: str,
        *,
        role: str = "user",
        user_id: str = "",
        ts: float | None = None,
    ) -> None:
        """添加一条消息到 RAG。重复 doc_id 用 upsert 不会重复存。失败静默。"""
        if not self._enabled or not scope or not text or not text.strip():
            return
        col = self._get_collection(scope)
        if col is None:
            return
        ts = ts if ts is not None else time.time()
        doc_id = f"{int(ts * 1000)}:{role}:{user_id or 'na'}"
        try:
            col.upsert(
                ids=[doc_id],
                documents=[text.strip()[:1500]],  # 单文档 max 1500 字
                metadatas=[{
                    "role": role,
                    "user_id": str(user_id) if user_id else "",
                    "ts": ts,
                    "scope": scope,
                }],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"catty_rag.add failed: {exc}")

    def query(
        self,
        scope: str,
        query_text: str,
        *,
        top_k: int = 3,
        min_similarity: float = _RECALL_MIN_SIMILARITY,
    ) -> list[tuple[float, str, dict[str, Any]]]:
        """语义召回 top-K (score, text, metadata)。score 是余弦相似度 [0,1]。

        score < min_similarity 的过滤掉。无结果返回 []。
        """
        if not self._enabled or not scope or not query_text or not query_text.strip():
            return []
        col = self._get_collection(scope)
        if col is None:
            return []
        try:
            res = col.query(
                query_texts=[query_text.strip()[:1000]],
                n_results=max(1, min(top_k, _MAX_RECALL_TOP_K)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"catty_rag.query failed: {exc}")
            return []
        out: list[tuple[float, str, dict[str, Any]]] = []
        docs = (res.get("documents") or [[]])[0] or []
        metas = (res.get("metadatas") or [[]])[0] or []
        dists = (res.get("distances") or [[]])[0] or []
        for doc, meta, dist in zip(docs, metas, dists):
            # chromadb cosine distance 是 1-similarity, score = 1 - distance
            score = 1.0 - float(dist)
            if score < min_similarity:
                continue
            out.append((score, str(doc), dict(meta or {})))
        return out

    def total_docs(self, scope: str) -> int:
        """诊断: 当前 scope 总文档数。"""
        if not self._enabled or not scope:
            return 0
        col = self._get_collection(scope)
        if col is None:
            return 0
        try:
            return int(col.count())
        except Exception:  # noqa: BLE001
            return 0

    def clear_scope(self, scope: str) -> bool:
        """主人 only 命令调用: 清空指定 scope 的所有向量记忆。"""
        if not self._enabled or not scope:
            return False
        name = _sanitize_collection_name(scope)
        with self._lock:
            try:
                self._client.delete_collection(name=name)
                self._collections.pop(name, None)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"catty_rag.clear_scope failed: {exc}")
                return False

    def prune_old_docs(self, scope: str, *, keep_recent: int = _MAX_PER_SCOPE_DOCS) -> int:
        """drop 超出上限的最老文档(按 ts metadata 排序)。返回删除数。"""
        if not self._enabled or not scope:
            return 0
        col = self._get_collection(scope)
        if col is None:
            return 0
        try:
            cur_count = int(col.count())
        except Exception:  # noqa: BLE001
            return 0
        if cur_count <= keep_recent:
            return 0
        # 拿所有 docs + metadata 按 ts 排序, 删最老的
        try:
            all_data = col.get(include=["metadatas"])
        except Exception:  # noqa: BLE001
            return 0
        ids = list(all_data.get("ids") or [])
        metas = list(all_data.get("metadatas") or [])
        if not ids:
            return 0
        paired = sorted(
            zip(ids, metas),
            key=lambda kv: float((kv[1] or {}).get("ts", 0.0) or 0.0),
        )
        n_to_drop = cur_count - keep_recent
        drop_ids = [kv[0] for kv in paired[:n_to_drop]]
        try:
            col.delete(ids=drop_ids)
            return len(drop_ids)
        except Exception:  # noqa: BLE001
            return 0


# ── prompt 注入 ──────────────────────────────────────────────────
def build_rag_recall_prompt(
    store: CattyRAGStore,
    scope: str,
    user_text: str,
    *,
    top_k: int = 3,
) -> str:
    """根据当前 user_text 召回 top-K 历史, 拼成 prompt 段。无召回返回 ''。"""
    if not store.enabled or not scope or not user_text:
        return ""
    hits = store.query(scope, user_text, top_k=top_k)
    if not hits:
        return ""
    lines = ["【RAG 向量召回 (语义近的旧对话片段)】"]
    for score, text, meta in hits:
        role = str(meta.get("role") or "?")
        ts = float(meta.get("ts") or 0.0)
        if ts > 0:
            age_min = int((time.time() - ts) / 60)
            if age_min < 60:
                age = f"{age_min}min 前"
            elif age_min < 1440:
                age = f"{age_min // 60}h 前"
            else:
                age = f"{age_min // 1440}d 前"
        else:
            age = "?"
        lines.append(f"- [{role}/{age}, 相似度 {score:.2f}] {text[:200]}")
    lines.append("(这些是语义近的旧片段, 仅供回忆参考, 不要复述给用户, 不要假装『刚才说过』除非真的相关)")
    return "\n".join(lines)


__all__ = [
    "CattyRAGStore",
    "build_rag_recall_prompt",
]
