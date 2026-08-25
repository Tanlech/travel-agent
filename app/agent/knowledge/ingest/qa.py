"""qa（攻略问答）知识库专门入库：读取 md/txt/rst 文档并结构化分块"""

from __future__ import annotations

import logging
from pathlib import Path

from app.agent.knowledge.chunker import chunk_document_structured
from app.agent.knowledge.ingest.common import capacity_chunk_size
from app.agent.knowledge.schemas import TextChunk

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS = {".md", ".txt", ".rst"}


def load_qa_documents(paths: list[str], chunk_size: int | None = None, overlap: int = 50) -> list[TextChunk]:
    """加载 md/txt/rst 文档：按目录遍历或单文件，结构化分块后带 source 元数据

    chunk_size 不传时取 embedding 容量封顶值（保证不超出模型 token 上限）
    """
    files: list[Path] = []
    for path in paths or []:
        p = Path(path)
        if p.is_dir():
            files.extend(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in _TEXT_EXTENSIONS)
        elif p.is_file() and p.suffix.lower() in _TEXT_EXTENSIONS:
            files.append(p)
    files.sort(key=lambda p: str(p))  # 固定顺序，保证重复入库的确定性

    chunk_size = chunk_size or capacity_chunk_size()
    chunks: list[TextChunk] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取文档失败，跳过: %s: %s", file, exc)
            continue
        if not text.strip():
            continue  # 空文档不产生知识块
        for i, (piece, section) in enumerate(chunk_document_structured(text, chunk_size=chunk_size, overlap=overlap)):
            metadata = {"source": str(file), "chunk_index": str(i)}
            if section:
                metadata["section"] = section
            chunks.append(TextChunk(text=piece, metadata=metadata))
    return chunks