from __future__ import annotations

import uuid

from qdrant_client import QdrantClient, models

from app.infrastructure.settings import settings
from app.knowledge.schemas import RetrievalItem


class QdrantStore:
    """向量库封装：Qdrant（Docker 服务），支持稠密+稀疏向量混合检索（RRF 融合）。

    正文存 payload["_text"]，检索按"相似度降序"返回。
    """

    def __init__(self, url: str | None = None, api_key: str | None = None, collection_prefix: str | None = None):
        self.url = url or settings.qdrant_url
        self._prefix = collection_prefix if collection_prefix is not None else settings.qdrant_collection_prefix
        self._client = QdrantClient(url=self.url, api_key=api_key or settings.qdrant_api_key)

    def _collection_name(self, collection: str) -> str:
        return f"{self._prefix}{collection}"

    def _ensure_collection(self, collection: str, vector_size: int, sparse: bool) -> None:
        name = self._collection_name(collection)
        if self._client.collection_exists(name):
            return
        vectors_config: dict = {"dense": models.VectorParams(size=vector_size, distance=models.Distance.COSINE)}
        kwargs: dict = {"vectors_config": vectors_config}
        if sparse:
            kwargs["sparse_vectors_config"] = {"sparse": models.SparseVectorParams()}
        self._client.create_collection(collection_name=name, **kwargs)

    def upsert(self, collection: str, ids: list[str], documents: list[str], metadatas: list[dict], embeddings: list[list[float]], sparse_embeddings: list[dict] | None = None) -> None:
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
        self._client.upsert(collection_name=self._collection_name(collection), points=points)

    def query(self, collection: str, query_embedding: list[float], n_results: int = 5, where: dict | None = None, query_sparse: dict | None = None) -> list[RetrievalItem]:
        """混合检索：稠密（语义）+ 稀疏（关键词）两路召回 → RRF 融合排序；无稀疏向量时仅稠密路"""
        name = self._collection_name(collection)
        if not self._client.collection_exists(name):
            return []
        query_filter = self._build_filter(where)
        if query_sparse:
            resp = self._client.query_points(
                collection_name=name,
                prefetch=[
                    models.Prefetch(query=query_embedding, using="dense", limit=n_results * 2, filter=query_filter),
                    models.Prefetch(query=models.SparseVector(**query_sparse), using="sparse", limit=n_results * 2, filter=query_filter),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=n_results,
                with_payload=True,
            )
        else:
            resp = self._client.query_points(
                collection_name=name,
                query=query_embedding,
                using="dense",
                limit=n_results,
                query_filter=query_filter,
                with_payload=True,
            )
        items: list[RetrievalItem] = []
        for hit in resp.points:
            payload = dict(hit.payload or {})
            text = str(payload.pop("_text", ""))
            items.append(
                RetrievalItem(
                    id=str(hit.id),
                    text=text,
                    metadata={k: str(v) for k, v in payload.items()},
                    distance=hit.score,
                )
            )
        return items

    def get_all(self, collection: str, where: dict | None = None) -> list[RetrievalItem]:
        """按条件取回全部知识块（scroll 分页）"""
        name = self._collection_name(collection)
        if not self._client.collection_exists(name):
            return []
        query_filter = self._build_filter(where)
        items: list[RetrievalItem] = []
        next_offset: object = None
        while True:
            points, next_offset = self._client.scroll(
                collection_name=name, limit=100, offset=next_offset, scroll_filter=query_filter, with_payload=True
            )
            for point in points:
                payload = dict(point.payload or {})
                text = str(payload.pop("_text", ""))
                items.append(
                    RetrievalItem(
                        id=str(point.id),
                        text=text,
                        metadata={k: str(v) for k, v in payload.items()},
                    )
                )
            if next_offset is None:
                break
        return items

    def count(self, collection: str) -> int:
        name = self._collection_name(collection)
        if not self._client.collection_exists(name):
            return 0
        return self._client.count(collection_name=name).count

    def delete(self, collection: str, where: dict | None = None) -> None:
        """删除满足条件的知识块；不传 where 时清空整个集合"""
        name = self._collection_name(collection)
        if not self._client.collection_exists(name):
            return
        if where:
            self._client.delete(
                collection_name=name, points_selector=models.FilterSelector(filter=self._build_filter(where))
            )
        else:
            self._client.delete_collection(name)

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
