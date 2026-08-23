from __future__ import annotations

from fastembed import SparseTextEmbedding


class SparseTextEmbedder:
    """稀疏向量生成：Qdrant/bm25（多语言 BM25 的 ONNX 版），用于混合检索的关键词路。

    稀疏向量与稠密向量互补：稠密懂语义/同义词，稀疏保证专有名词/精确术语命中。
    模型首次使用会自动下载到本地缓存。
    """

    _MODEL_NAME = "Qdrant/bm25"

    def __init__(self, model_name: str = _MODEL_NAME):
        self._model: SparseTextEmbedding | None = None
        try:
            self._model = SparseTextEmbedding(model_name=model_name)
        except Exception:
            self._model = None

    def is_enabled(self) -> bool:
        return self._model is not None

    def embed_texts(self, texts: list[str]) -> list[dict]:
        """返回 Qdrant SparseVector 参数格式列表：{"indices": [...], "values": [...]}"""
        if not texts or not self._model:
            return []
        results: list[dict] = []
        for sparse in self._model.embed(list(texts)):
            results.append(
                {
                    "indices": [int(i) for i in sparse.indices],
                    "values": [float(v) for v in sparse.values],
                }
            )
        return results

    def embed_text(self, text: str) -> dict:
        results = self.embed_texts([text])
        return results[0] if results else {"indices": [], "values": []}


sparse_text_embedder = SparseTextEmbedder()
