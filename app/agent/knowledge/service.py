from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict

from app.infrastructure.settings import settings
from app.infrastructure.llm_client import get_llm_client
from app.agent.knowledge.embedder import (
    SparseTextEmbedder,
    TextEmbedder,
    sparse_text_embedder,
    text_embedder,
)
from app.agent.knowledge.ingest.qa import load_qa_documents
from app.agent.knowledge.reranker import reranker as _reranker
from app.agent.knowledge.retriever import embed_cached, retrieve
from app.agent.knowledge.schemas import RetrievalItem, RetrievalResult, TextChunk
from app.agent.knowledge.store import QdrantStore, qdrant_store

logger = logging.getLogger(__name__)

# 检索结果级 LRU 缓存：(collection, 归一化 query, top_k, 序列化 where) → RetrievalResult
# 与 retriever.embed_cached（缓存的向量）互补：向量缓存省"编码"，此处缓存省"向量库搜索"，
# 命中时整次检索（embed + store.query）都被跳过。进程内实现，无外部依赖、永不阻塞；
# 需要跨请求/多 worker 共享时，可把 get/set 换成 Redis（同一接口，直接替换即可）。
_RESULT_CACHE: "OrderedDict[tuple, RetrievalResult]" = OrderedDict()
_MAX_RESULT_CACHE = 256
_RESULT_CACHE_LOCK = threading.Lock()


def _retrieve_cache_key(collection: str, query: str, top_k: int, where: dict | None, rerank: bool, candidate_k: int) -> tuple:
    """构造结果缓存键：归一化 query 提升命中率，where 排序序列化保证稳定；
    rerank 与 candidate_k 参与键，避免"开启/关闭重排"或候选数变化时误命中错版本结果
    """
    return (
        collection,
        " ".join(query.strip().lower().split()),
        max(1, int(top_k)),
        tuple(sorted((str(k), str(v)) for k, v in (where or {}).items())),
        rerank,
        max(1, int(candidate_k)),
    )


def _cache_put(cache_key: tuple, result: RetrievalResult) -> None:
    """加锁写入结果缓存：缓存满时挤出最旧，避免无界增长"""
    with _RESULT_CACHE_LOCK:
        if len(_RESULT_CACHE) >= _MAX_RESULT_CACHE:
            _RESULT_CACHE.popitem(last=False)
        _RESULT_CACHE[cache_key] = result


_ASK_SYSTEM_PROMPT = (
    "你是旅行规划助手。请基于提供的参考资料回答用户问题，"
    "引用时说明来源；资料中没有的信息，如实说明不知道，不要编造。"
)


def chunks_from_entries(entries: list[dict], text_key: str = "text", metadata_keys: tuple[str, ...] = ()) -> list[TextChunk]:
    """把结构化条目（如景点清单、FAQ）转成知识块：text 为正文，其余字段作为 metadata
    条目可携带稳定 id（entry["id"]），用于重复入库时覆盖而非追加"""
    chunks: list[TextChunk] = []
    for entry in entries or []:
        text = str(entry.get(text_key) or "").strip()
        if not text:
            continue
        metadata = {k: str(entry[k]) for k in metadata_keys if entry.get(k) is not None}
        chunk_id = str(entry["id"]).strip() if entry.get("id") else None
        chunks.append(TextChunk(text=text, metadata=metadata, id=chunk_id))
    return chunks


class KnowledgeService:
    """知识层高层接口：入库（ingest）、检索（retrieve）、问答（ask）

    使用方式（三个知识源）：
      knowledge_service.ingest_entries(ATTRACTION_COLLECTION, spots, ...)   # 城市景点
      knowledge_service.ingest_documents(QA_COLLECTION, ["docs/"])          # 攻略文档
      knowledge_service.ingest_chunks(CHAT_COLLECTION, ...)                 # 历史对话
    """

    def __init__(
        self,
        store: QdrantStore | None = None,
        embedder: TextEmbedder | None = None,
        sparse_embedder: SparseTextEmbedder | None = None,
    ):
        self.store = store or qdrant_store
        self.embedder = embedder or text_embedder
        self.sparse_embedder = sparse_embedder or sparse_text_embedder

    @property
    def _hybrid_enabled(self) -> bool:
        """混合检索：稀疏向量（BM25）可用时自动开启"""
        return self.sparse_embedder.is_enabled()

    # ---------- 入库 ----------

    def ingest_chunks(self, collection: str, chunks: list[TextChunk]) -> int:
        """向量化并写入知识块，返回实际写入数量。同内容重复入库自动覆盖（幂等）
        Qdrant 后端自动附带稀疏向量（BM25），开启混合检索"""
        chunks = [c for c in chunks or [] if (c.text or "").strip()]
        if not chunks or not self.embedder.is_enabled():
            return 0
        texts = [c.text for c in chunks]
        embeddings = self.embedder.embed_texts(texts)
        if not embeddings:
            logger.warning("ingest_chunks 向量化失败/为空，跳过写入: collection=%s chunks=%d", collection, len(chunks))
            return 0
        ids = [c.id or self._chunk_id(collection, c) for c in chunks]
        metadatas = [c.metadata for c in chunks]
        sparse_embeddings = self.sparse_embedder.embed_texts(texts) if self._hybrid_enabled else None
        self.store.upsert(collection, ids, texts, metadatas, embeddings, sparse_embeddings=sparse_embeddings)
        return len(chunks)

    def ingest_documents(self, collection: str, paths: list[str]) -> int:
        """加载文档并入库；按 source 先清旧点再重导，避免文档删除/改动后残留脏向量"""
        return self.reingest_by_source(collection, load_qa_documents(paths), source_key="source")

    def reingest_by_source(self, collection: str, chunks: list[TextChunk], source_key: str = "source") -> int:
        """按来源重建：先清该来源旧点再重导，返回实际写入数量
        不携带 source_key 的块按增量追加（不清空共享集合），避免误删他人数据
        """
        chunks = [c for c in chunks or [] if (c.text or "").strip()]
        if not chunks:
            return 0
        grouped: dict[str, list[TextChunk]] = {}
        unsourced: list[TextChunk] = []
        for c in chunks:
            src = str((c.metadata or {}).get(source_key) or "").strip()
            if src:
                grouped.setdefault(src, []).append(c)
            else:
                unsourced.append(c)
        total = 0
        for src, group in grouped.items():
            self.clear(collection, where={source_key: src})
            total += self.ingest_chunks(collection, group)
        if unsourced:
            total += self.ingest_chunks(collection, unsourced)
        return total

    def ingest_entries(
        self,
        collection: str,
        entries: list[dict],
        text_key: str,
        metadata_keys: tuple[str, ...] = (),
    ) -> int:
        """把结构化条目（景点清单/FAQ 等）入库，text_key 为正文，其余字段作 metadata"""
        return self.ingest_chunks(collection, chunks_from_entries(entries, text_key, metadata_keys))

    def upsert_entry(
        self,
        collection: str,
        entry: dict,
        text_key: str,
        metadata_keys: tuple[str, ...] = (),
    ) -> int:
        """单条幂等入库：编辑/新增单个条目只同步这一条（id 稳定则覆盖）
        供"单个景点编辑/删除"使用，避免整城重导"""
        if not entry or not str(entry.get(text_key) or "").strip():
            return 0
        return self.ingest_entries(collection, [entry], text_key, metadata_keys)

    def delete_entries(self, collection: str, ids: list[str]) -> None:
        """按稳定 id 删除若干知识块（单个景点删除）；不存在的 id 自动忽略"""
        if not ids:
            return
        self.store.delete_by_ids(collection, ids)

    # ---------- 检索 ----------

    def retrieve(self, collection: str, query: str, top_k: int = 5, where: dict | None = None) -> RetrievalResult:
        if not query or not query.strip():
            return RetrievalResult(collection=collection, query=query, items=[])

        rerank = _reranker.is_enabled()
        candidate_k = settings.retrieval_candidate_k if rerank else top_k
        cache_key = _retrieve_cache_key(collection, query, top_k, where, rerank, candidate_k)
        cached = _RESULT_CACHE.get(cache_key)
        if cached is not None:
            # 加锁保护 move_to_end：避免缓存满时其它线程刚 popitem 挤出该键导致 KeyError
            with _RESULT_CACHE_LOCK:
                if _RESULT_CACHE.get(cache_key) is not None:
                    _RESULT_CACHE.move_to_end(cache_key)
                else:
                    cached = None
            if cached is None:
                # 命中后又被并发挤出的极窄窗口：直接走重新检索路径
                query_sparse = embed_cached(self.sparse_embedder, query, "sparse") if self._hybrid_enabled else None
                result = retrieve(self.store, self.embedder, collection, query, top_k=top_k, where=where, query_sparse=query_sparse, reranker=_reranker if rerank else None, candidate_k=candidate_k)
                _cache_put(cache_key, result)
                return result.model_copy(deep=True)
            logger.info(
                "kb retrieve cache hit: collection=%s query=%r top_k=%s rerank=%s",
                collection,
                query,
                top_k,
                rerank,
            )
            # 返回深拷贝，避免调用方修改返回值污染共享缓存
            return cached.model_copy(deep=True)

        query_sparse = embed_cached(self.sparse_embedder, query, "sparse") if self._hybrid_enabled else None
        result = retrieve(self.store, self.embedder, collection, query, top_k=top_k, where=where, query_sparse=query_sparse, reranker=_reranker if rerank else None, candidate_k=candidate_k)

        _cache_put(cache_key, result)
        return result.model_copy(deep=True)

    def get_all(self, collection: str, where: dict | None = None) -> list[RetrievalItem]:
        return self.store.get_all(collection, where=where)

    # ---------- 问答（检索增强生成） ----------

    def ask(
        self,
        collection: str,
        question: str,
        top_k: int = 5,
        where: dict | None = None,
        system_prompt: str | None = None,
    ) -> str | None:
        """检索相关知识块，连同问题交给 LLM 生成回答；LLM 不可用时返回 None

        返回 None 说明"无命中或 LLM 不可用"，二者差异通过日志区分（便于排障与降级策略）
        """
        result = self.retrieve(collection, question, top_k=top_k, where=where)
        if not result.items:
            logger.info("ask 未命中知识库: collection=%s question=%r", collection, question)
            return None
        context = "\n\n".join(
            self._format_context_item(i, item) for i, item in enumerate(result.items)
        )
        llm = get_llm_client()
        if not llm.is_enabled():
            logger.warning("ask LLM 不可用，仅检索未生成: collection=%s", collection)
            return None
        return llm.generate_chat_reply(
            system_prompt=system_prompt or _ASK_SYSTEM_PROMPT,
            user_prompt=f"参考资料：\n{context}\n\n问题：{question}",
        )

    # ---------- 管理 ----------

    def count(self, collection: str) -> int:
        return self.store.count(collection)

    def clear(self, collection: str, where: dict | None = None) -> None:
        self.store.delete(collection, where=where)

    @staticmethod
    def _format_context_item(index: int, item: RetrievalItem) -> str:
        """拼装送给 LLM 的上下文条目；块带章节/来源时展示归属（可溯源）"""
        meta = item.metadata or {}
        parts = [p for p in (meta.get("section"), meta.get("source")) if p]
        prefix = f"（{'｜'.join(parts)}）" if parts else ""
        return f"[{index + 1}] {prefix}{item.text}"

    @staticmethod
    def _chunk_id(collection: str, chunk: TextChunk) -> str:
        """正文或元数据不同则生成不同 id，避免同文不同源的块在重复入库时互相覆盖"""
        meta = chunk.metadata or {}
        pieces = "\x00".join(f"{k}\x01{v}" for k, v in sorted(meta.items()))
        anchor = f"{chunk.text or ''}\x02{pieces}"
        digest = hashlib.sha256(anchor.encode("utf-8")).hexdigest()
        return f"{collection}:{digest}"


knowledge_service = KnowledgeService()
