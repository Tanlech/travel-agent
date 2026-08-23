from __future__ import annotations

from app.knowledge.embedder import TextEmbedder
from app.knowledge.schemas import RetrievalResult
from app.knowledge.store import QdrantStore


def retrieve(
    store: QdrantStore,
    embedder: TextEmbedder,
    collection: str,
    query: str,
    top_k: int = 5,
    where: dict | None = None,
    query_sparse: dict | None = None,
) -> RetrievalResult:
    """检索：问题向量化 → 向量库检索。

    提供 query_sparse（稀疏向量）时走混合检索（稠密语义 + 稀疏关键词 → RRF 融合），
    否则仅稠密相似度检索。
    """
    query_embedding = embedder.embed_text(query)
    if not query_embedding:
        return RetrievalResult(collection=collection, query=query, items=[])
    items = store.query(collection, query_embedding, n_results=max(1, top_k), where=where, query_sparse=query_sparse)
    return RetrievalResult(collection=collection, query=query, items=items)
