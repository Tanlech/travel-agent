from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, Field


class SparseVector(TypedDict):
    """Qdrant 稀疏向量参数结构：{"indices": [...], "values": [...]}

    属数据结构契约，放在契约层，供 embedder 定义、store/retriever 消费共用
    """
    indices: list[int]
    values: list[float]


class TextChunk(BaseModel):
    """一个可入库的知识块：正文 + 元数据（用于过滤/溯源）"""

    text: str
    metadata: dict[str, str] = Field(default_factory=dict)
    id: str | None = None  # 不传则由 service 自动生成（collection:内容hash）


class RetrievalItem(BaseModel):
    """一次检索命中的一个知识块"""

    id: str
    text: str
    metadata: dict[str, str] = Field(default_factory=dict)
    distance: float | None = None  # 相似度分数：混合路为 RRF 融合分（越大越相关，非原始余弦）


class RetrievalResult(BaseModel):
    """检索结果：命中的知识块列表，按相似度降序"""

    collection: str
    query: str
    items: list[RetrievalItem] = Field(default_factory=list)
