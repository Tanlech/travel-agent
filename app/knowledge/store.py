from __future__ import annotations

import time
import uuid

from qdrant_client import QdrantClient, models

from app.infrastructure.settings import settings
from app.knowledge.schemas import RetrievalItem, SparseVector


# 单次 upsert 最大点数，避免大文档集一次提交触发 Qdrant 最大 batch 限制
_UPSERT_BATCH_SIZE = 256

# Qdrant 网络操作瞬时故障的软重试：最多重试 3 次，退避递增，仍失败才抛错
_STORE_RETRIES = 3
_STORE_BACKOFF = 0.3


class QdrantStore:
    """向量库封装：Qdrant（Docker 服务），支持稠密+稀疏向量混合检索（RRF 融合）

    正文存 payload["_text"]，检索按"相似度降序"返回
    """

    def __init__(self, url: str | None = None, api_key: str | None = None, collection_prefix: str | None = None):
        self.url = url or settings.qdrant_url
        self._prefix = collection_prefix if collection_prefix is not None else settings.qdrant_collection_prefix
        self._client = QdrantClient(url=self.url, api_key=api_key or settings.qdrant_api_key)

    def _collection_name(self, collection: str) -> str:
        return f"{self._prefix}{collection}"

    @staticmethod
    def _retry(fn, *args, **kwargs):
        """调用 fn，瞬时异常时软重试，重试耗尽仍失败才抛出"""
        for attempt in range(_STORE_RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                if attempt < _STORE_RETRIES - 1:
                    time.sleep(_STORE_BACKOFF * (attempt + 1))
                    continue
                raise

    def _ensure_collection(self, collection: str, vector_size: int, sparse: bool) -> None:
        name = self._collection_name(collection)
        if self._retry(self._client.collection_exists, name):
            self._validate_existing(name, vector_size, sparse)
            return
        vectors_config: dict = {"dense": models.VectorParams(size=vector_size, distance=models.Distance.COSINE)}
        kwargs: dict = {"vectors_config": vectors_config}
        if sparse:
            kwargs["sparse_vectors_config"] = {"sparse": models.SparseVectorParams()}
        self._retry(self._client.create_collection, collection_name=name, **kwargs)

    def _validate_existing(self, name: str, vector_size: int, sparse: bool) -> None:
        """已存在集合需与当前配置一致，避免维度/稀疏配置漂移导致难读的写入错误"""
        info = self._retry(self._client.get_collection, name)
        vectors = getattr(info, "vectors", None)
        current_size: int | None = None
        if isinstance(vectors, dict):
            dense = vectors.get("dense")
            current_size = getattr(dense, "size", None) if dense else None
        if current_size is not None and current_size != vector_size:
            raise ValueError(f"集合 {name} 向量维度 {current_size} 与当前配置 {vector_size} 不一致，需重建集合")
        sparse_configured = bool(getattr(info, "sparse_vectors_config", None))
        if sparse != sparse_configured:
            raise ValueError(f"集合 {name} 稀疏向量配置与当前要求不一致，需重建集合")

    def upsert(self, collection: str, ids: list[str], documents: list[str], metadatas: list[dict], embeddings: list[list[float]], sparse_embeddings: list[SparseVector] | None = None) -> None:
        """写入/覆盖知识块；传 sparse_embeddings 时启用稀疏向量路（混合检索）"""
        if not documents:
            return
        vector_size = len(embeddings[0]) if embeddings else settings.embedding_dim
        self._ensure_collection(collection, vector_size, sparse=bool(sparse_embeddings))
        points: list[models.PointStruct] = []
        for i, pid in enumerate(ids):
            payload = {"_text": documents[i], **metadatas[i]}
            vector: dict = {"dense": embeddings[i]}
            if sparse_embeddings:
                vector["sparse"] = models.SparseVector(**sparse_embeddings[i])
            points.append(models.PointStruct(id=self._point_id(pid), vector=vector, payload=payload))
        name = self._collection_name(collection)
        for i in range(0, len(points), _UPSERT_BATCH_SIZE):
            self._retry(self._client.upsert, collection_name=name, points=points[i : i + _UPSERT_BATCH_SIZE])

    def query(self, collection: str, query_embedding: list[float], n_results: int = 5, where: dict | None = None, query_sparse: SparseVector | None = None) -> list[RetrievalItem]:
        """检索：稠密+稀疏两路 → RRF 融合；只提供稀疏向量时退化为纯稀疏；否则纯稠密"""
        name = self._collection_name(collection)
        if not self._retry(self._client.collection_exists, name):
            return []
        if query_sparse and not query_sparse.get("indices"):
            query_sparse = None  # 空 token 的稀疏向量视作空，避免把空向量发给 Qdrant
        if not query_embedding and not query_sparse:
            return []  # 两路向量皆空，直接返回，避免把空向量发给 Qdrant
        query_filter = self._build_filter(where)
        prefetch_limit = n_results * settings.retrieval_prefetch_multiplier
        if query_sparse and query_embedding:
            resp = self._retry(self._client.query_points,
                collection_name=name,
                prefetch=[
                    models.Prefetch(query=query_embedding, using="dense", limit=prefetch_limit, filter=query_filter),
                    models.Prefetch(query=models.SparseVector(**query_sparse), using="sparse", limit=prefetch_limit, filter=query_filter),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=n_results,
                with_payload=True,
            )
        elif query_sparse:
            resp = self._retry(self._client.query_points,
                collection_name=name,
                query=models.SparseVector(**query_sparse),
                using="sparse",
                limit=n_results,
                query_filter=query_filter,
                with_payload=True,
            )
        else:
            resp = self._retry(self._client.query_points,
                collection_name=name,
                query=query_embedding,
                using="dense",
                limit=n_results,
                query_filter=query_filter,
                with_payload=True,
            )
        items: list[RetrievalItem] = []
        # distance：纯稠密路为余弦分数；混合路为 RRF 融合分（越大越相关，非原始余弦）
        for hit in resp.points:
            items.append(self._to_retrieval_item(dict(hit.payload or {}), hit.id, hit.score))
        return items

    def get_all(self, collection: str, where: dict | None = None, batch: int = 100) -> list[RetrievalItem]:
        """按条件取回全部知识块（scroll 分页）"""
        name = self._collection_name(collection)
        if not self._retry(self._client.collection_exists, name):
            return []
        query_filter = self._build_filter(where)
        items: list[RetrievalItem] = []
        next_offset: object = None
        while True:
            points, next_offset = self._retry(self._client.scroll,
                collection_name=name, limit=batch, offset=next_offset, scroll_filter=query_filter, with_payload=True
            )
            for point in points:
                items.append(self._to_retrieval_item(dict(point.payload or {}), point.id))
            if next_offset is None:
                break
        return items

    @staticmethod
    def _to_retrieval_item(payload: dict, point_id: int | str, score: float | None = None) -> RetrievalItem:
        """把 Qdrant point 的 payload/score 转成 RetrievalItem（query 与 get_all 共用）"""
        text = str(payload.pop("_text", ""))
        return RetrievalItem(
            id=str(point_id),
            text=text,
            metadata={k: str(v) for k, v in payload.items()},
            distance=score,
        )

    def count(self, collection: str) -> int:
        name = self._collection_name(collection)
        if not self._retry(self._client.collection_exists, name):
            return 0
        return self._retry(self._client.count, collection_name=name).count

    def delete(self, collection: str, where: dict | None = None) -> None:
        """删除满足条件的知识块；不传 where 时清空整个集合"""
        name = self._collection_name(collection)
        if not self._retry(self._client.collection_exists, name):
            return
        if where:
            self._retry(self._client.delete,
                collection_name=name, points_selector=models.FilterSelector(filter=self._build_filter(where))
            )
        else:
            self._retry(self._client.delete_collection, name)

    @staticmethod
    def _build_filter(where: dict | None) -> models.Filter | None:
        if not where:
            return None
        must = [
            models.FieldCondition(key=k, match=models.MatchValue(value=v))
            for k, v in where.items()
        ]
        return models.Filter(must=must)

    @staticmethod
    def _point_id(pid: str) -> str:
        """字符串 id 稳定映射为 uuid（Qdrant 的字符串 id 兼容性最稳）"""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(pid)))


qdrant_store = QdrantStore()
