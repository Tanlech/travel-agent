"""attraction tool 手动测试：评估输出质量（知识库/未入库/改稿去重/沉淀）

标记 integration，需要真实高德 + LLM 配置（.env），运行：
    python -m pytest test/tool/test_attraction.py -m integration -s
"""

import json

import pytest

from app.tools.attraction import attraction_tool
from app.tools.schema.attraction import AttractionCandidate, AttractionInput


def _quality_summary(result, must_spots=None, avoid_spots=None) -> dict:
    """汇总输出质量指标，便于人工核对"""
    names = [c.name for c in result.candidates]
    missing_reason = [c.name for c in result.candidates if not c.reason]
    missing_duration = [c.name for c in result.candidates if not c.estimated_visit_duration_hours]
    return {
        "city": result.city,
        "candidate_count": len(names),
        "names": names,
        "error": result.error,
        "must_visit_verified": [c.name for c in result.must_visit_verified],
        "avoid_verified": [c.name for c in result.avoid_verified],
        "must_all_present": all(any(m in n for n in names) for m in (must_spots or [])),
        "avoid_all_excluded": all(not any(a in n for n in names) for a in (avoid_spots or [])),
        "empty_reason": missing_reason,
        "empty_duration": missing_duration,
        "raw": result.raw,
    }


def _run_case(case_name: str, **kwargs) -> None:
    must_spots = kwargs.get("must_visit_spots", [])
    avoid_spots = kwargs.get("avoid_spots", [])
    result = attraction_tool.run(AttractionInput(**kwargs))
    summary = _quality_summary(result, must_spots, avoid_spots)
    print(f"\n===== {case_name} =====")
    print("--- quality summary ---")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("--- full JSON (to agent) ---")
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))


@pytest.mark.integration
def test_attraction_manual():
    """知识库为主：成都 must 库内 + avoid + 偏好排序"""
    _run_case(
        "北京4天 自然/亲子",
        city="北京",
        days=4,
        preferences=["自然景观", "亲子"],
        must_visit_spots=[],
        avoid_spots=[],
        target_count_min=8,
        target_count_max=12,
    )


@pytest.mark.integration
def test_attraction_unindexed_city():
    """城市未入库：只搜用户必去景点；无必去时返回空并提示"""
    _run_case(
        "未入库城市+必去(触发搜索沉淀)",
        city="格尔木",
        days=2,
        must_visit_spots=["察尔汗盐湖"],
        target_count_min=5,
        target_count_max=8,
    )
    # 无必去、未入库 → 应返回 error 提示
    _run_case(
        "未入库城市+无必去(应提示)",
        city="玉树",
        days=1,
        target_count_min=5,
        target_count_max=8,
    )


@pytest.mark.integration
def test_attraction_incremental_no_duplicate():
    """改稿增量：已有候选（existing_candidates）中同名必去不应重复返回"""
    existing = AttractionCandidate(name="武侯祠", area="成都")
    result = attraction_tool.run(
        AttractionInput(
            city="成都",
            days=2,
            must_visit_spots=["武侯祠", "宽窄巷子"],
            existing_candidates=[existing],
        )
    )
    names = [c.name for c in result.candidates]
    print(f"\n===== 改稿增量去重 =====")
    print("names:", names)
    # 武侯祠已在 existing，且为 must，不应在新增结果里重复出现；宽窄巷子应补充进来
    assert not any("武侯祠" in n for n in names), f"改稿重复推荐: {names}"
    assert any("宽窄巷子" in n for n in names), f"宽窄巷子丢失: {names}"


@pytest.mark.integration
def test_attraction_must_visit_keep():
    """必去景点必须在最终结果中保留"""
    result = attraction_tool.run(
        AttractionInput(
            city="西安",
            days=2,
            must_visit_spots=["兵马俑"],
            preferences=["历史文化"],
        )
    )
    names = [c.name for c in result.candidates]
    print(f"\n===== 必去保留 =====")
    print("names:", names)
    assert any("兵马俑" in name or "秦始皇帝陵" in name for name in names), f"必去景点丢失: {names}"


@pytest.mark.integration
def test_attraction_persist_missing_must():
    """知识库缺失的必去景点：搜索确认后沉淀进城市 json 并重导"""
    _run_case(
        "库外必去(触发沉淀写json+重导)",
        city="成都",
        days=3,
        must_visit_spots=["成都自然博物馆"],
        preferences=["博物馆"],
        target_count_max=10,
    )