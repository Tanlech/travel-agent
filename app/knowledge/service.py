from __future__ import annotations

import hashlib
from typing import Any

from app.infrastructure.llm_client import get_llm_client
from app.knowledge.embedder import TextEmbedder, text_embedder
from app.knowledge.embedder_sparse import SparseTextEmbedder, sparse_text_embedder
from app.knowledge.loader import chunks_from_entries, load_text_files
from app.knowledge.retriever import retrieve
from app.knowledge.schemas import RetrievalItem, RetrievalResult, TextChunk
from app.knowledge.store import QdrantStore, qdrant_store

_ASK_SYSTEM_PROMPT = (
    "你是旅行规划助手。请基于提供的参考资料回答用户问题，"
    "引用时说明来源；资料中没有的信息，如实说明不知道，不要编造。"
)


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
        """向量化并写入知识块，返回实际写入数量。同内容重复入库自动覆盖（幂等）。
        Qdrant 后端自动附带稀疏向量（BM25），开启混合检索。"""
        chunks = [c for c in chunks or [] if (c.text or "").strip()]
        if not chunks or not self.embedder.is_enabled():
            return 0
        texts = [c.text for c in chunks]
        embeddings = self.embedder.embed_texts(texts)
        if not embeddings:
            return 0
        ids = [c.id or self._chunk_id(collection, c) for c in chunks]
        metadatas = [c.metadata for c in chunks]
        sparse_embeddings = self.sparse_embedder.embed_texts(texts) if self._hybrid_enabled else None
        self.store.upsert(collection, ids, texts, metadatas, embeddings, sparse_embeddings=sparse_embeddings)
        return len(chunks)

    def ingest_documents(self, collection: str, paths: list[str]) -> int:
        """加载文档（目录/文件，支持 md/txt/rst）并入库"""
        return self.ingest_chunks(collection, load_text_files(paths))

    def ingest_entries(
        self,
        collection: str,
        entries: list[dict],
        text_key: str,
        metadata_keys: tuple[str, ...] = (),
    ) -> int:
        """把结构化条目（景点清单/FAQ 等）入库，text_key 为正文，其余字段作 metadata"""
        return self.ingest_chunks(collection, chunks_from_entries(entries, text_key, metadata_keys))

    # ---------- 检索 ----------

    def retrieve(self, collection: str, query: str, top_k: int = 5, where: dict | None = None) -> RetrievalResult:
        query_sparse = self.sparse_embedder.embed_text(query) if self._hybrid_enabled else None
        return retrieve(self.store, self.embedder, collection, query, top_k=top_k, where=where, query_sparse=query_sparse)

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
        """检索相关知识块，连同问题交给 LLM 生成回答；LLM 不可用时返回 None"""
        result = self.retrieve(collection, question, top_k=top_k, where=where)
        if not result.items:
            return None
        context = "\n\n".join(f"[{i + 1}] {item.text}" for i, item in enumerate(result.items))
        llm = get_llm_client()
        if not llm.is_enabled():
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
    def _chunk_id(collection: str, chunk: TextChunk) -> str:
        digest = hashlib.md5((chunk.text or "").encode("utf-8")).hexdigest()
        return f"{collection}:{digest}"


knowledge_service = KnowledgeService()
