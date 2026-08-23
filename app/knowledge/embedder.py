from __future__ import annotations

import logging
import time

from fastembed import SparseTextEmbedding
from openai import OpenAI

from app.infrastructure.settings import settings
from app.knowledge.schemas import SparseVector

logger = logging.getLogger(__name__)

# 稠密向量调用异常时的降级策略：最多重试 3 次，退避递增；仍失败返回空结果而非抛错
_EMBED_RETRIES = 3
_EMBED_BACKOFF = 0.3


class TextEmbedder:
    """文本向量化封装：默认阿里云 Qwen-Embedding（qwen3.7-text-embedding，1024 维）

    模型通过 settings.embedding_model 配置，切换模型只需改配置（注意维度需与已入库数据一致）
    """

    def __init__(self, model: str | None = None, batch_size: int | None = None):
        self.model = model or settings.embedding_model
        self.batch_size = batch_size or settings.embedding_batch_size
        self._client = (
            OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
            if settings.openai_api_key
            else None
        )

    def is_enabled(self) -> bool:
        return self._client is not None

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回与输入等长的向量列表；调用异常时有限重试，仍失败返回空列表"""
        if not texts or not self._client:
            return []
        for attempt in range(_EMBED_RETRIES):
            try:
                results: list[list[float]] = []
                for i in range(0, len(texts), self.batch_size):
                    batch = texts[i : i + self.batch_size]
                    resp = self._client.embeddings.create(model=self.model, input=batch)
                    rows = sorted(resp.data, key=lambda item: item.index)
                    results.extend([item.embedding for item in rows])
                return results
            except Exception as exc:  # noqa: BLE001
                if attempt < _EMBED_RETRIES - 1:
                    time.sleep(_EMBED_BACKOFF * (attempt + 1))
                    continue
                logger.warning("dense embedding 调用失败，降级为空结果: %s", exc)
        return []

    def embed_text(self, text: str) -> list[float]:
        result = self.embed_texts([text])
        return result[0] if result else []


text_embedder = TextEmbedder()


class SparseTextEmbedder:
    """稀疏向量生成：Qdrant/bm25（多语言 BM25 的 ONNX 版），用于混合检索的关键词路

    稀疏向量与稠密向量互补：稠密懂语义/同义词，稀疏保证专有名词/精确术语命中
    模型首次使用会自动下载到本地缓存
    """

    _MODEL_NAME = "Qdrant/bm25"

    def __init__(self, model_name: str = _MODEL_NAME):
        self._model: SparseTextEmbedding | None = None
        try:
            self._model = SparseTextEmbedding(model_name=model_name)
        except Exception as exc:  # noqa: BLE001
            self._model = None
            logger.warning("稀疏向量模型初始化失败，降级为仅稠密检索: %s", exc)

    def is_enabled(self) -> bool:
        return self._model is not None

    def embed_texts(self, texts: list[str]) -> list[SparseVector]:
        """返回 Qdrant SparseVector 参数格式列表：{"indices": [...], "values": [...]}"""
        if not texts or not self._model:
            return []
        results: list[SparseVector] = []
        for sparse in self._model.embed(list(texts)):
            results.append(
                {
                    "indices": [int(i) for i in sparse.indices],
                    "values": [float(v) for v in sparse.values],
                }
            )
        return results

    def embed_text(self, text: str) -> SparseVector:
        results = self.embed_texts([text])
        return results[0] if results else {"indices": [], "values": []}


sparse_text_embedder = SparseTextEmbedder()
