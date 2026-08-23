from __future__ import annotations

from pathlib import Path

from app.knowledge.chunker import chunk_text
from app.knowledge.schemas import TextChunk

_TEXT_EXTENSIONS = {".md", ".txt", ".rst"}


def load_text_files(paths: list[str]) -> list[TextChunk]:
    """加载 md/txt/rst 文档：按目录遍历或单文件，切块后带 source 元数据"""
    files: list[Path] = []
    for path in paths or []:
        p = Path(path)
        if p.is_dir():
            files.extend(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in _TEXT_EXTENSIONS)
        elif p.is_file() and p.suffix.lower() in _TEXT_EXTENSIONS:
            files.append(p)

    chunks: list[TextChunk] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, piece in enumerate(chunk_text(text)):
            chunks.append(
                TextChunk(
                    text=piece,
                    metadata={"source": str(file), "chunk_index": str(i)},
                )
            )
    return chunks


def chunks_from_entries(entries: list[dict], text_key: str = "text", metadata_keys: tuple[str, ...] = ()) -> list[TextChunk]:
    """把结构化条目（如景点清单、FAQ）转成知识块：text 为正文，其余字段作为 metadata。
    条目可携带稳定 id（entry["id"]），用于重复入库时覆盖而非追加。"""
    chunks: list[TextChunk] = []
    for entry in entries or []:
        text = str(entry.get(text_key) or "").strip()
        if not text:
            continue
        metadata = {k: str(entry[k]) for k in metadata_keys if entry.get(k) is not None}
        chunk_id = str(entry["id"]).strip() if entry.get("id") else None
        chunks.append(TextChunk(text=text, metadata=metadata, id=chunk_id))
    return chunks
