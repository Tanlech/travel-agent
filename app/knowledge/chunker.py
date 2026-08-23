from __future__ import annotations

import re

# 默认块长（字符），ingest 据此结合 embedding 容量封顶后传入
CHUNK_SIZE_DEFAULT = 500

# 断点优先级：越靠前越倾向优先断开（结构化边界优先于单纯字符切）
# \n\n 段落 > 换行 > 中文句/分号 > 英文句点。标题（markdown "# "）尽量留在块首
# 不应塞在上一个块的末尾
_BOUNDARY_MARKERS: tuple[tuple[str, int], ...] = (
    ("\n\n", 2),  # 空行 = 段落边界
    ("\n", 1),    # 换行
    ("。", 1),    # 中文句号
    ("！", 1),
    ("？", 1),
    ("；", 1),
    (". ", 2),    # 英文句点+空格，避免折断单词
    (".", 1),
)

# ---- 统一的标题识别：markdown 层级（#）、中文编号（一、）、数字编号（1. / 1、） ----
# 所有判定共用同一套正则，避免"分节"与"切分防护"用的标题定义不一致（#3）
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_CN_HEADER_RE = re.compile(r"^([一二三四五六七八九十]+、)\s*(.*)$")
_DIGIT_HEADER_RE = re.compile(r"^(\d+(?:\.\d+)?[.、])\s*(.*)$")
# 仅用于判定"某行是否以标题开头"（不带捕获组，供 _find_cut 防护）
_HEADER_START = (
    re.compile(r"^#{1,6}\s+"),
    re.compile(r"^[一二三四五六七八九十]+、"),
    re.compile(r"^\d+(?:\.\d+)?[.、]"),
)


def _is_heading_start(text: str) -> bool:
    """判断文本是否以任一标题模式开头（供切分防护用）"""
    return any(p.match(text) for p in _HEADER_START)


def _match_heading(line: str) -> tuple[int, str] | None:
    """识别一行是否为标题，返回 (层级, 标题文本)。非标题返回 None。中文/数字编号视为一级"""
    m = _HEADER_RE.match(line)
    if m:
        return len(m.group(1)), m.group(2).strip()
    m = _CN_HEADER_RE.match(line)
    if m:
        return 1, f"{m.group(1)}{m.group(2)}".strip()
    m = _DIGIT_HEADER_RE.match(line)
    if m:
        return 1, f"{m.group(1)}{m.group(2)}".strip()
    return None


def _leading_title(text: str) -> tuple[str, str]:
    """短文档处理：把首行标题提取为 section，剩余作为正文；无标题返回 ("", text)（#2）"""
    lines = text.split("\n")
    if not lines:
        return "", text
    got = _match_heading(lines[0].rstrip())
    if not got:
        return "", text
    body = "\n".join(lines[1:]).strip("\n")
    return got[1], body


def chunk_document_structured(text: str, chunk_size: int = 500, overlap: int = 50) -> list[tuple[str, str]]:
    """按文档结构分块，返回 [(正文, 所属章节)]，章节不进正文（供存 metadata）

    以 markdown / 中文编号 / 数字编号标题为节边界，把正文按节流式切块；
    无标题文档退化为 chunk_text 的纯字节切块。短文档也尽量提取首标题作 section
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        section, body = _leading_title(text)
        return [(body or text, section)]

    parts = _split_by_sections(text)
    # 无标题时退化为对整篇按字符切块（无章节归属）
    plain = [(p, "") for p in chunk_text(text, chunk_size, overlap)]
    if not parts:
        return plain

    out: list[tuple[str, str]] = []
    for prefix, body in parts:
        section = " > ".join(prefix)
        for piece in chunk_text(body, chunk_size, overlap):
            out.append((piece, section))
    return out or plain


def _split_by_sections(text: str) -> list[tuple[list[str], str]]:
    """解析文本为 [(章节路径前缀, 该节正文)]。无标题时返回 []"""
    prefix: list[str] = []
    cur: list[str] = []
    sections: list[tuple[list[str], list[str]]] = []

    def flush() -> None:
        if cur:
            sections.append((list(prefix), list(cur)))
            cur.clear()

    for raw in text.split("\n"):
        line = raw.rstrip()
        got = _match_heading(line)
        if got:
            flush()
            level, title = got
            prefix = prefix[: level - 1] + [title]
            continue
        cur.append(line)
    flush()

    result: list[tuple[list[str], str]] = []
    for p, lines in sections:
        body = "\n".join(lines).strip("\n")
        if body:
            result.append((p, body))
    return result


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """按字符长度切块，带重叠"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        remaining = len(text) - start
        # 剩余不足一个整块：整体收尾，避免 overlap 在末尾逐字空转出碎片
        if remaining <= chunk_size:
            stripped = text[start:].strip()
            if stripped:
                chunks.append(stripped)
            break
        end = start + chunk_size
        cut = _find_cut(text, start, end)
        # 只在"边界足够靠近块尾"（超过半块）才回退，避免过度回退导致块过小
        if cut is not None and (cut - start) >= chunk_size // 2:
            end = cut
        stripped = text[start:end].strip()
        if stripped:
            chunks.append(stripped)
        start = max(end - overlap, start + 1)
    return chunks


def _find_cut(text: str, start: int, end: int) -> int | None:
    """在 [start, end) 内找最优断点（绝对下标）。找不到合适边界返回 None"""
    seg = text[start:end]
    for marker, consume in _BOUNDARY_MARKERS:
        idx = seg.rfind(marker)
        if idx == -1:
            continue
        cut = start + idx + consume
        # 若断点后紧跟标题标记，说明标题被切到了下一个块，回退断点到该标记前，避免割裂标题前缀
        if cut < end and _is_heading_start(text[cut:]):
            continue
        return cut
    return None