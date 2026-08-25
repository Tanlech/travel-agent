"""RAG 知识层测试：入库（ingest）→ 检索（retrieve）→ 问答（ask）+ attraction_tool 命中知识库

单元测试（分块逻辑）不依赖外部服务；
集成测试需要真实 embedding API（qwen3.7-text-embedding）+ Chroma：
    python -m pytest test/knowledge/test_knowledge.py -m integration -s
"""

import pytest

from app.agent.knowledge import knowledge_service
from app.agent.knowledge.chunker import chunk_document_structured, chunk_text
from app.agent.tools.attraction import attraction_tool
from app.agent.tools.schema.attraction import AttractionInput

TEST_COLLECTION = "test_knowledge"
TEST_CITY = "知识测试城"

SAMPLE_ENTRIES = [
    {
        "text": "三峡大坝是世界最大水利枢纽工程，位于宜昌市夷陵区，建议游玩3小时，级别main。",
        "name": "三峡大坝",
        "city": "宜昌",
        "entity_level": "main",
        "tags": "地标,水利工程",
        "duration": "3.0",
        "reason": "世界级水利枢纽，国之重器。",
    },
    {
        "text": "清江画廊是宜昌著名的自然山水景区，位于长阳土家族自治县，建议游玩4小时。",
        "name": "清江画廊",
        "city": "宜昌",
        "entity_level": "main",
        "tags": "自然景观,山水",
        "duration": "4.0",
        "reason": "八百里清江美如画。",
    },
    {
        "text": "武侯祠是成都纪念诸葛亮的祠堂，位于成都武侯区，历史文化类景点。",
        "name": "武侯祠",
        "city": "成都",
        "entity_level": "main",
        "tags": "历史文化,博物馆",
        "duration": "2.0",
        "reason": "三国文化代表景点。",
    },
]


# ---------- 单元测试（离线） ----------


def test_chunker_short_text():
    assert chunk_text("一句话") == ["一句话"]


def test_chunker_long_text():
    long_text = "句子一。" + "很长很长" * 80 + "句子二。" * 60
    chunks = chunk_text(long_text, chunk_size=100, overlap=20)
    assert len(chunks) >= 2
    assert all(c.strip() for c in chunks)
    # 块间有重叠，内容不丢失
    assert "句子一" in chunks[0]


def test_chunker_short_doc_section():
    """短文档也应提取首标题作为章节（#2）"""
    pieces = chunk_document_structured("# 交通\n打车快。", chunk_size=40, overlap=10)
    assert pieces == [("打车快。", "交通")]


def test_chunker_doc_sections_and_plain_fallback():
    """多节长文档按结构分块并带章节路径；无标题文档退化为纯文本切块（section 为空）"""
    doc = (
        "# 交通\n打车方便，但高峰会堵。\n\n"
        "## 地铁\n一二号线都方便。\n\n"
        + "始终分段，为让该节文本超过块长从而多块。" * 30
    )
    pieces = chunk_document_structured(doc, chunk_size=100, overlap=20)
    assert pieces, "多节文档不应返回空"
    # 有章节的块带"交通"路径
    for piece, section in pieces:
        if section:
            assert "交通" in section
    # 无标题文档退化：全部块没有章节
    plain = chunk_document_structured("纯文本。" * 60, chunk_size=100, overlap=20)
    assert plain and all(not s for _, s in plain)
    # 短文本（<=块长）整体作为一条返回
    assert chunk_document_structured("一句话", chunk_size=100, overlap=20) == [("一句话", "")]


def test_chunker_no_fragment():
    """剩余不足一个整块时应整体收尾，避免 overlap 在末尾逐字空转出碎片"""
    body = "句子一，内容比较重要。" * 40  # 约 480 字符，远超 chunk_size
    chunks = chunk_text(body, chunk_size=120, overlap=30)
    # 不应产生几十上百个逐字碎片：块数应与文本长度/块长同量级
    assert 0 < len(chunks) <= 8
    # 块间不能出现"只差一两个字"的碎片
    assert all(len(c) > 10 for c in chunks)


# ---------- 集成测试（真实 API） ----------


@pytest.mark.integration
def test_ingest_and_retrieve():
    collection = TEST_COLLECTION
    knowledge_service.clear(collection)
    count = knowledge_service.ingest_entries(
        collection, SAMPLE_ENTRIES, text_key="text",
        metadata_keys=("name", "city", "entity_level", "tags", "duration", "reason"),
    )
    assert count == len(SAMPLE_ENTRIES)

    # 按 city 精确过滤
    items = knowledge_service.get_all(collection, where={"city": "宜昌"})
    assert len(items) == 2

    # 语义检索命中相关内容
    result = knowledge_service.retrieve(collection, "宜昌有什么水利工程景点", top_k=2)
    assert result.items, "语义检索无结果"
    assert "三峡" in result.items[0].text

    knowledge_service.clear(collection)


@pytest.mark.integration
def test_ask():
    collection = TEST_COLLECTION
    knowledge_service.clear(collection)
    knowledge_service.ingest_entries(
        collection, SAMPLE_ENTRIES, text_key="text",
        metadata_keys=("name", "city", "entity_level", "tags", "duration", "reason"),
    )
    answer = knowledge_service.ask(collection, "宜昌有什么值得看的景点？", top_k=3)
    assert answer and len(answer) > 5, "ask 未返回有效回答"
    print(f"\n[ask] 回答: {answer}")
    knowledge_service.clear(collection)


@pytest.mark.integration
def test_attraction_tool_kb_hit(monkeypatch):
    """知识库命中：attraction_tool 直接返回知识库结果（不依赖高德/LLM）"""
    from app.agent.knowledge import ATTRACTION_COLLECTION
    from app.agent.tools.schema.attraction import SpotSelection

    city = TEST_CITY
    kb_entries = [
        {
            "text": "测试山：知识测试城的标志性山峰，建议游玩3小时，级别main。",
            "name": "测试山",
            "city": city,
            "entity_level": "main",
            "tags": "自然景观",
            "duration": "3.0",
            "reason": "知识测试城第一地标。",
        },
        {
            "text": "测试博物馆：知识测试城的历史博物馆，建议游玩2小时，级别main。",
            "name": "测试博物馆",
            "city": city,
            "entity_level": "main",
            "tags": "博物馆,历史文化",
            "duration": "2.0",
            "reason": "展示城市历史。",
        },
    ]
    try:
        # 直接落库重建，避免依赖历史集合配置（若有残留的纯稠密集合会触发配置漂移守卫）
        knowledge_service.clear(ATTRACTION_COLLECTION)
        knowledge_service.ingest_entries(
            ATTRACTION_COLLECTION, kb_entries, text_key="text",
            metadata_keys=("name", "city", "entity_level", "tags", "duration", "reason"),
        )
        # 偏好排序：自然景观 → 测试山应排在测试博物馆前
        # 用桩固定 LLM 择优顺序（真实 LLM 排序不确定，直接断言其相对顺序会抖动）
        monkeypatch.setattr(
            attraction_tool,
            "_classify_selection",
            lambda *a, **k: SpotSelection(names=["测试山", "测试博物馆"]),
        )
        result = attraction_tool.run(AttractionInput(city=city, days=2, preferences=["自然景观"]))
        assert result is not None
        assert result.raw and result.raw.get("kb_hit") is True, f"未命中知识库: {result.raw}"
        names = [c.name for c in result.candidates]
        print(f"\n[kb] {city} 候选: {names}")
        assert any("测试山" in n for n in names), f"知识库景点丢失: {names}"
        assert names.index([n for n in names if "测试山" in n][0]) < names.index([n for n in names if "博物馆" in n][0])
    finally:
        knowledge_service.clear(ATTRACTION_COLLECTION)
