"""intent 层核心行为测试。

聚焦意图识别（固定走 fallback 规则路径）、核心解析函数与 schema 消毒规则；
不逐边界穷举。意图判定测试通过 monkeypatch 禁用 LLM，避免依赖真实 key 配置。
"""

import pytest

from app.domain.intent.schema import (
    IntentRecognitionInput,
    IntentRecognitionOutput,
    IntentType,
)
from app.domain.intent.service import (
    IntentRecognizer,
    extract_dates,
    extract_destination,
    extract_travelers,
)


@pytest.fixture(autouse=True)
def _force_fallback(monkeypatch):
    # 意图判定测试固定走 fallback，不依赖 LLM 配置（mock 或真实 key）
    monkeypatch.setattr(IntentRecognizer, "_recognize_with_llm", lambda self, intent_input: None)


recognizer = IntentRecognizer()


def _input(raw_message, *, planning_request=None, latest_plan_summary=None):
    return IntentRecognitionInput(
        request_id="test",
        raw_message=raw_message,
        planning_request=planning_request,
        latest_plan_summary=latest_plan_summary,
    )


# ---- 意图判定（fallback 路径） ----

def test_end_session():
    assert recognizer.recognize(_input("再见")).intent_type == IntentType.END_SESSION


def test_reject():
    assert recognizer.recognize(_input("算了，不用规划了")).intent_type == IntentType.REJECT


def test_revise_plan():
    out = recognizer.recognize(_input("把第二天改成室内的", latest_plan_summary={"destination": "北京"}))
    assert out.intent_type == IntentType.REVISE_PLAN


def test_confirm():
    out = recognizer.recognize(_input("好的", latest_plan_summary={"destination": "北京"}))
    assert out.intent_type == IntentType.CONFIRM


def test_qa():
    assert recognizer.recognize(_input("你好")).intent_type == IntentType.QA


def test_clarification_missing_fields():
    out = recognizer.recognize(_input("我想去北京玩"))
    assert out.intent_type == IntentType.CLARIFICATION
    assert {"start_date", "end_date"} <= set(out.missing_fields)


def test_new_plan_complete():
    out = recognizer.recognize(_input("8月10号到12号去北京"))
    assert out.intent_type == IntentType.NEW_PLAN
    assert out.extracted_request_patch.get("destination") == "北京"


# ---- 核心解析函数 ----

def test_extract_dates_range():
    start, end = extract_dates("8月10号到8月12号")
    assert start and end and start < end


def test_extract_dates_full():
    start, end = extract_dates("2026-08-10 到 2026-08-12")
    assert (start, end) == ("2026-08-10", "2026-08-12")


def test_extract_dates_single():
    start, end = extract_dates("8月10号")
    assert start and end is None


def test_extract_travelers():
    assert extract_travelers("我们3个人去") == 3
    assert extract_travelers("两个人") == 2
    assert extract_travelers("2大1小") == 3


def test_extract_destination():
    assert extract_destination("我想去北京玩") == "北京"


# ---- schema 消毒 ----

def test_clarification_downgrades_to_new_plan():
    assert IntentRecognitionOutput(intent_type="clarification").intent_type == IntentType.NEW_PLAN


def test_non_revise_clears_load_flag():
    out = IntentRecognitionOutput(
        intent_type="new_plan",
        should_load_existing_artifacts=True,
        revision_scope_hint="day_level",
    )
    assert out.should_load_existing_artifacts is False
    assert out.revision_scope_hint is None


def test_patch_whitelist_and_float_int():
    out = IntentRecognitionOutput(
        intent_type="new_plan",
        extracted_request_patch={"destination": "北京", "unknown_field": "x", "days": 3.0},
    )
    assert "unknown_field" not in out.extracted_request_patch
    assert out.extracted_request_patch.get("days") == 3
    assert out.patch_dropped_fields == ["unknown_field"]
