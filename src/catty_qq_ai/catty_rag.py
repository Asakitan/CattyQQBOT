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
    """ChromaDB-based per-scope 向量记忆。enabled=False 时全部 no-op。

    Embedding 优先级:
    1. config.catty_openai_* 配置存在 → OpenAIEmbeddingFunction (推荐, 不依赖本地 ONNX)
       需要 host 支持 /v1/embeddings 端点 + model 'text-embedding-3-small' 或类似
    2. fallback: chromadb 默认 ONNX (all-MiniLM-L6-v2)
       Python 3.14+ 上 onnxruntime wheel DLL 不兼容会自动 disable
    """

    def __init__(self, memory_path: str | Path, config: Any = None) -> None:
        p = Path(memory_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        self._persist_dir = p.parent / "chroma"
        self._lock = threading.RLock()
        self._config = config
        self._client = None
        self._embedding_fn = None  # 显式传给 collection, 不用 chromadb default
        self._embedding_source: str = ""  # debug: "openai" / "onnx" / ""
        self._collections: dict[str, Any] = {}  # scope → collection (cache)
        self._enabled = False
        self._init_error: str = ""
        self._try_init()

    def _try_init(self) -> None:
        """Try-import chromadb + 初始化 PersistentClient + 选 embedding backend.

        embedding 优先 OpenAI compatible (无 ONNX 依赖, Python 3.14 兼容),
        fallback ONNX (Python 3.14 已知 DLL fail → disable)。
        """
        try:
            import chromadb  # type: ignore
        except ImportError as exc:
            self._init_error = f"chromadb not installed: {exc}"
            logger.warning("catty_rag: chromadb not installed, RAG disabled — pip install chromadb to enable")
            return
        # 选 embedding backend
        ef = self._try_openai_embedding()
        if ef is None:
            ef = self._try_onnx_embedding()
        if ef is None:
            self._init_error = "no embedding backend available (OpenAI 配置缺失 + ONNX DLL fail)"
            logger.warning(
                "catty_rag: 无可用 embedding backend, RAG disabled. "
                "配 catty_openai_* + host 支持 /v1/embeddings 端点 (推荐), "
                "或修复 Python<3.14 onnxruntime DLL 问题。"
            )
            return
        self._embedding_fn = ef
        try:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self._persist_dir))
            self._enabled = True
            logger.info(
                f"catty_rag: chromadb initialized at {self._persist_dir} (embedding={self._embedding_source})"
            )
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"chromadb init failed: {exc}"
            logger.warning(f"catty_rag: init failed, RAG disabled — {exc}")

    def _try_openai_embedding(self):  # noqa: ANN202
        """优先尝试 OpenAI compatible /v1/embeddings (无 ONNX 依赖, Python 3.14 兼容)。

        从 config.catty_openai_api_key + catty_openai_base_url 复用。
        模型名优先 config.catty_embedding_model, 默认 'text-embedding-3-small'。
        host 不支持 /embeddings 端点会在第一次 upsert 时 fail, 这里只检查配置存在。
        """
        if self._config is None:
            return None
        api_key = getattr(self._config, "catty_openai_api_key", "") or ""
        base_url = getattr(self._config, "catty_openai_base_url", "") or ""
        if not api_key or not base_url:
            return None
        # base_url 可能是 /v1/chat/completions 这样的完整路径, 取到 /v1
        api_base = base_url.rstrip("/")
        if "/chat/completions" in api_base:
            api_base = api_base.split("/chat/completions")[0]
        if not api_base.endswith("/v1"):
            # 不强制 /v1 后缀, 因为有的 host 没有 /v1; OpenAI client 会按 base/embeddings 拼
            pass
        model_name = (
            getattr(self._config, "catty_embedding_model", "")
            or "text-embedding-3-small"
        )
        try:
            from chromadb.utils import embedding_functions  # type: ignore
            ef = embedding_functions.OpenAIEmbeddingFunction(
                api_key=api_key,
                api_base=api_base,
                model_name=model_name,
            )
            self._embedding_source = f"openai:{model_name}@{api_base}"
            logger.info(f"catty_rag: using OpenAI embedding ({model_name} @ {api_base})")
            return ef
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"catty_rag: OpenAIEmbeddingFunction init failed: {exc}")
            return None

    def _try_onnx_embedding(self):  # noqa: ANN202
        """Fallback: chromadb 默认 ONNX (all-MiniLM-L6-v2). Python 3.14 上 DLL fail."""
        try:
            import onnxruntime  # type: ignore  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"catty_rag: onnxruntime 不可用 ({exc}); "
                "Python 3.14+ + onnxruntime wheel DLL 不兼容是已知问题"
            )
            return None
        try:
            from chromadb.utils import embedding_functions  # type: ignore
            ef = embedding_functions.ONNXMiniLM_L6_V2()
            self._embedding_source = "onnx:all-MiniLM-L6-v2"
            logger.info("catty_rag: using ONNX embedding (all-MiniLM-L6-v2 local)")
            return ef
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"catty_rag: ONNX embedding init failed: {exc}")
            return None

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
                    embedding_function=self._embedding_fn,
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
        boost_recency: bool = True,
    ) -> list[tuple[float, str, dict[str, Any]]]:
        """语义召回 top-K (score, text, metadata)。score 是余弦相似度 [0,1]。

        score < min_similarity 的过滤掉。无结果返回 []。

        boost_recency (default True):
        - 内部 fetch top_k*3 候选, 按 ts 给 score 加权重排序后切 top_k
        - 加权: <24h *1.30 / <7d *1.15 / <30d *1.05 / 更老 *1.00
        - 返回的 score 仍是 raw similarity (下游显示 + age 自然交代时效), boosted 只用于排序
        - 目的: 最近发生的事更容易被召回, 几周前的旧记忆次要
        """
        if not self._enabled or not scope or not query_text or not query_text.strip():
            return []
        col = self._get_collection(scope)
        if col is None:
            return []
        # boost 模式下拿 top_k*3 候选给重排序留余量, 否则照原样
        fetch_n = top_k * 3 if boost_recency else top_k
        fetch_n = max(1, min(fetch_n, _MAX_RECALL_TOP_K))
        try:
            res = col.query(
                query_texts=[query_text.strip()[:1000]],
                n_results=fetch_n,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"catty_rag.query failed: {exc}")
            return []
        docs = (res.get("documents") or [[]])[0] or []
        metas = (res.get("metadatas") or [[]])[0] or []
        dists = (res.get("distances") or [[]])[0] or []
        now = time.time()
        candidates: list[tuple[float, float, str, dict[str, Any]]] = []  # (raw, boosted, text, meta)
        for doc, meta, dist in zip(docs, metas, dists):
            # chromadb cosine distance 是 1-similarity, score = 1 - distance
            score = 1.0 - float(dist)
            if score < min_similarity:
                continue
            meta_d = dict(meta or {})
            if boost_recency:
                ts = float(meta_d.get("ts") or 0.0)
                if ts > 0:
                    age_s = max(0.0, now - ts)
                    if age_s < 86400:
                        mult = 1.30
                    elif age_s < 7 * 86400:
                        mult = 1.15
                    elif age_s < 30 * 86400:
                        mult = 1.05
                    else:
                        mult = 1.00
                else:
                    mult = 1.00
                boosted = score * mult
            else:
                boosted = score
            candidates.append((score, boosted, str(doc), meta_d))
        # 按 boosted score 倒序排序, 切 top_k. 不 boost 时 boosted==score 行为不变。
        candidates.sort(key=lambda c: -c[1])
        return [(raw, doc, meta) for raw, _boosted, doc, meta in candidates[:top_k]]

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

    def backfill_memory(self, memory_store: Any) -> int:
        """从 memory_store 导入所有 group/private/mention summaries 到 RAG (idempotent upsert)。

        - group summary → scope=`group:<id>`, role=memory_summary
        - private user summary → scope=`private:<id>`, role=memory_summary
        - member-in-group mention summary → scope=`group:<id>`, role=member_profile
        返回 add 数 (含 upsert)。
        """
        if not self._enabled:
            return 0
        try:
            data = getattr(memory_store, "_data", {}) or {}
        except Exception:  # noqa: BLE001
            return 0
        groups_n = len(data.get("groups") or {})
        users_n = len(data.get("users") or {})
        # debug: 看 _data 实际加载量 + 第一个 group 的 keys (诊断为啥 +0)
        sample_keys = ""
        if groups_n > 0:
            first_g = next(iter(data["groups"].values()), {})
            sample_keys = ", ".join(list(first_g.keys())[:8]) if isinstance(first_g, dict) else "<not dict>"
        logger.info(f"catty_rag.backfill_memory: groups={groups_n} users={users_n} sample_group_keys=[{sample_keys}]")
        count = 0
        # groups: group["summary"] + group["member_profiles"][user_id]["impression"]
        for group_id, group in (data.get("groups") or {}).items():
            if not isinstance(group, dict):
                continue
            # group-level summary (整群对话总结)
            summary = str(group.get("summary") or "").strip()
            if summary:
                try:
                    ts = float(group.get("last_summary_at") or 0.0) or time.time()
                    self.add(f"group:{group_id}", summary, role="memory_summary", user_id="", ts=ts)
                    count += 1
                except Exception:  # noqa: BLE001
                    pass
            # member-in-group profiles (每个群友的笨猫印象)
            member_profiles = group.get("member_profiles") or {}
            if isinstance(member_profiles, dict):
                for mp_user_id, prof in member_profiles.items():
                    if not isinstance(prof, dict):
                        continue
                    parts: list[str] = []
                    impression = str(prof.get("impression") or "").strip()
                    evidence = str(prof.get("evidence") or "").strip()
                    display = str(prof.get("display_name") or "").strip()
                    title = str(prof.get("inferred_title") or "").strip()
                    if display or title:
                        parts.append(f"{display or title}")
                    if impression:
                        parts.append(f"印象: {impression}")
                    if evidence:
                        parts.append(f"依据: {evidence[:120]}")
                    text = " | ".join(parts).strip()
                    if text:
                        try:
                            ts_raw = prof.get("updated_at") or ""
                            try:
                                from datetime import datetime as _dt
                                ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
                            except Exception:  # noqa: BLE001
                                ts = time.time()
                            self.add(
                                f"group:{group_id}",
                                text,
                                role="member_profile",
                                user_id=str(mp_user_id),
                                ts=ts,
                            )
                            count += 1
                        except Exception:  # noqa: BLE001
                            continue
        # private users: user["private_summary"] + user["profile"]
        for user_id, user in (data.get("users") or {}).items():
            if not isinstance(user, dict):
                continue
            priv = str(user.get("private_summary") or "").strip()
            if priv:
                try:
                    ts = float(user.get("last_private_summary_at") or 0.0) or time.time()
                    self.add(f"private:{user_id}", priv, role="memory_summary",
                             user_id=str(user_id), ts=ts)
                    count += 1
                except Exception:  # noqa: BLE001
                    pass
            # 私聊 user 也有 profile (gender/title/impression) 可以入 RAG 给 private:scope
            profile = user.get("profile") or {}
            if isinstance(profile, dict):
                impression = str(profile.get("impression") or "").strip()
                title = str(profile.get("inferred_title") or "").strip()
                gender = str(profile.get("gender") or "").strip()
                parts: list[str] = []
                if title:
                    parts.append(title)
                if gender:
                    parts.append(f"性别: {gender}")
                if impression:
                    parts.append(f"印象: {impression}")
                text = " | ".join(parts).strip()
                if text:
                    try:
                        self.add(
                            f"private:{user_id}",
                            text,
                            role="user_profile",
                            user_id=str(user_id),
                        )
                        count += 1
                    except Exception:  # noqa: BLE001
                        pass
        return count

    def backfill_lorebook(self, scope_lorebook_store: Any) -> int:
        """从 scope_lorebook 导入所有 entries 到 RAG (idempotent upsert)。"""
        if not self._enabled:
            return 0
        try:
            data = getattr(scope_lorebook_store, "_data", {}) or {}
        except Exception:  # noqa: BLE001
            return 0
        count = 0
        for scope, entries in data.items():
            for entry in entries or []:
                try:
                    self.add(
                        scope,
                        getattr(entry, "content", ""),
                        role="lore_entry",
                        user_id="",
                        ts=float(getattr(entry, "created_at", 0.0) or 0.0) or time.time(),
                    )
                    count += 1
                except Exception:  # noqa: BLE001
                    continue
        return count

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
