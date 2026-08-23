from __future__ import annotations


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """按字符长度切块，带重叠；尽量在句末/换行处断开，避免切断语义"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        if end < len(text):
            # 在句子/换行边界处回退截断，保证块语义完整
            cut = max(chunk.rfind("。"), chunk.rfind("！"), chunk.rfind("？"), chunk.rfind("\n"), chunk.rfind("."))
            if cut > chunk_size // 2:
                end = start + cut + 1
                chunk = text[start:end]
        stripped = chunk.strip()
        if stripped:
            chunks.append(stripped)
        start = max(end - overlap, start + 1)
    return chunks
