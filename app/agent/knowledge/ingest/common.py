"""ingest 子包共享工具与集合常量（无对 service 的顶层依赖，避免循环导入）"""

from __future__ import annotations

from app.infrastructure.settings import settings
from app.agent.knowledge.chunker import CHUNK_SIZE_DEFAULT

# 三个知识库的集合常量（集中一处，供全库各文件/入口与子模块共享）
ATTRACTION_COLLECTION = "attraction"
QA_COLLECTION = "qa_kb"
CHAT_COLLECTION = "chat_kb"


def capacity_chunk_size() -> int:
    """按 embedding 输入 token 上限换算并封顶的默认 chunk_size（容量对齐）"""
    cap = (
        int(settings.embedding_max_tokens / settings.embedding_token_per_char)
        if settings.embedding_token_per_char > 0
        else CHUNK_SIZE_DEFAULT
    )
    return cap if cap > 0 else CHUNK_SIZE_DEFAULT