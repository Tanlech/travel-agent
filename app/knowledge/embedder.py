from __future__ import annotations

from openai import OpenAI

from app.infrastructure.settings import settings


class TextEmbedder:
    """文本向量化封装：默认阿里云 Qwen-Embedding（qwen3.7-text-embedding，1024 维）

    模型通过 settings.embedding_model 配置，切换模型只需改配置（注意维度需与已入库数据一致）。
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
        """批量向量化，返回与输入等长的向量列表"""
        if not texts or not self._client:
            return []
        results: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            resp = self._client.embeddings.create(model=self.model, input=batch)
            rows = sorted(resp.data, key=lambda item: item.index)
            results.extend([item.embedding for item in rows])
        return results

    def embed_text(self, text: str) -> list[float]:
        result = self.embed_texts([text])
        return result[0] if result else []


text_embedder = TextEmbedder()
