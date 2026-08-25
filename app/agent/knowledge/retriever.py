from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Literal

from app.infrastructure.settings import settings
from app.agent.knowledge.embedder import SparseTextEmbedder, TextEmbedder
from app.agent.knowledge.schemas import RetrievalResult, SparseVector
from app.agent.knowledge.store import QdrantStore

# 进程内 query 向量缓存：dense/sparse 各自命名空间，键为 (namespace, 原问题文本)
# 同一次会话内对相同问题重复检索时复用向量，省掉重复的 embedding 调用（优化 1/2）
# 用 OrderedDict 做 LRU：每次命中 move_to_end，满时挤出最旧一条（优化 5）
_EMBED_CACHE: OrderedDict[tuple[str, str], Any] = OrderedDict()
_MAX_EMBED_CACHE = 1024
_EMBED_CACHE_LOCK = threading.Lock()


def embed_cached(embedder: TextEmbedder | SparseTextEmbedder, text: str, namespace: Literal["dense", "sparse"]) -> list[float] | SparseVector:
    """带缓存的单条向量化；namespace 区分 dense/sparse（两者结果结构不同）
    dense 返回 list[float]，sparse 返回 SparseVector（{"indices","values"}）
    """
    key = (namespace, text)
    with _EMBED_CACHE_LOCK:
        cached = _EMBED_CACHE.get(key)
        if cached is not None:
            _EMBED_CACHE.move_to_end(key)
            return cached
    value = embedder.embed_text(text)
    if not value:
        return value
    with _EMBED_CACHE_LOCK:
        if len(_EMBED_CACHE) >= _MAX_EMBED_CACHE:
            _EMBED_CACHE.popitem(last=False)  # 挤出最旧一条，避免无限增长
        _EMBED_CACHE[key] = value
    return value


def retrieve(
    store: QdrantStore,
    embedder: TextEmbedder,
    collection: str,
    query: str,
    top_k: int = 5,
    where: dict | None = None,
    query_sparse: SparseVector | None = None,
) -> RetrievalResult:
    """检索：问题向量化 → 向量库检索

    提供 query_sparse（稀疏向量）时走混合检索（稠密语义 + 稀疏关键词 → RRF 融合）
    稠密向量不可用（如未配 key）但稀疏可用时退化为纯稀疏检索；否则仅稠密
    空 query 由上层（service.retrieve）统一早退，这里不再重复判断
    """
    top_k = max(1, min(int(top_k), settings.retrieval_max_top_k))
    query_embedding = embed_cached(embedder, query, "dense")
    if not query_embedding and not query_sparse:
        return RetrievalResult(collection=collection, query=query, items=[])
    items = store.query(collection, query_embedding, n_results=max(1, top_k), where=where, query_sparse=query_sparse)
    return RetrievalResult(collection=collection, query=query, items=items)
