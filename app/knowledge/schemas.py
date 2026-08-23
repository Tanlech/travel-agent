from __future__ import annotations

from pydantic import BaseModel, Field


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
    distance: float | None = None  # 余弦距离，越小越相似


class RetrievalResult(BaseModel):
    """检索结果：命中的知识块列表，按相似度降序"""

    collection: str
    query: str
    items: list[RetrievalItem] = Field(default_factory=list)
