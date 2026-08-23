"""chat（历史对话）知识库专门入库：默认写入 CHAT_COLLECTION

对话以已整理好的知识块（TextChunk）为单位入库，块结构与 service.ingest_chunks 一致
对话数据格式可能随上层演变，这里集中一处便于后续按格式调整
"""

from __future__ import annotations

from app.knowledge.ingest.common import CHAT_COLLECTION
from app.knowledge.schemas import TextChunk


def ingest_chat(chunks: list[TextChunk], collection: str = CHAT_COLLECTION, source_key: str = "source") -> int:
    """把对话知识块写入对话库，按来源重建，返回实际写入数量
    对 service 采用惰性导入，避免与上层 service 形成循环依赖
    """
    from app.knowledge.service import knowledge_service

    return knowledge_service.reingest_by_source(collection, chunks or [], source_key=source_key)