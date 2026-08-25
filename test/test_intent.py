"""intent 层核心行为测试。

聚焦意图识别（固定走 fallback 规则路径）、核心解析函数与 schema 消毒规则；
不逐边界穷举。意图判定测试通过 monkeypatch 禁用 LLM，避免依赖真实 key 配置。
"""

import pytest

from app.agent.domain.intent.schema import (
    IntentPlanningRequest,
    IntentRecognitionInput,
    IntentRecognitionOutput,
    IntentType,
)
from app.agent.domain.intent.service import (
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
    # 只有目的地必填，日期可选；"我想去北京玩" 已含目的地 → 应直接规划
    out = recognizer.recognize(_input("我想去北京玩"))
    assert out.intent_type == IntentType.NEW_PLAN
    assert out.missing_fields == []


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
    # 改稿改口句的动词残留不得被当成目的地（宁可追问，不让脏值入库）
    assert extract_destination("不去了，改成上海") is None
    assert extract_destination("我要去成都改成北京") is None


# ---- schema 消毒 ----

def test_clarification_downgrades_to_new_plan():
    assert IntentRecognitionOutput(intent_type="clarification").intent_type == IntentType.NEW_PLAN


def test_non_revise_clears_scope():
    out = IntentRecognitionOutput(
        intent_type="new_plan",
        revision_scope_hint="day_level",
    )
    assert out.revision_scope_hint is None


def test_patch_whitelist_and_float_int():
    out = IntentRecognitionOutput(
        intent_type="new_plan",
        extracted_request_patch={"destination": "北京", "unknown_field": "x", "days": 3.0},
    )
    assert "unknown_field" not in out.extracted_request_patch
    assert out.extracted_request_patch.get("days") == 3
    assert out.patch_dropped_fields == ["unknown_field"]


# ---- qa 意图保留闲聊中透露的规划字段（目的地累计，避免后续规划忘记重新追问） ----

def test_qa_keeps_mentioned_destination_in_patch():
    # "我想去阳江，有什么好玩的吗" 判为 qa，但原话明确提到目的地 → patch 必须保留
    out = IntentRecognitionOutput(
        intent_type="qa",
        extracted_request_patch={"destination": "阳江"},
        missing_fields=["destination"],
    )
    assert out.extracted_request_patch.get("destination") == "阳江"
    assert out.missing_fields == []  # qa 不驱动追问


def test_conversational_intents_clear_patch():
    # 收尾类意图仍清空 patch，避免"谢谢/再见"等污染累计需求
    for it in ("confirm", "reject", "end_session", "unknown"):
        out = IntentRecognitionOutput(intent_type=it, extracted_request_patch={"destination": "北京"})
        assert out.extracted_request_patch == {}


def test_qa_guard_keeps_destination_when_none_confirmed():
    inp = _input("我想去阳江，有什么好玩的吗")
    out = IntentRecognitionOutput(intent_type="qa", extracted_request_patch={"destination": "阳江"})
    guarded = recognizer._guard_qa_patch(inp, out)
    assert guarded.extracted_request_patch == {"destination": "阳江"}


def test_qa_guard_does_not_overwrite_confirmed_destination():
    # 已确认目的地（北京）后，闲聊随口提到另一地点不得覆盖
    inp = _input("上海有什么好吃的？", planning_request=IntentPlanningRequest(destination="北京"))
    out = IntentRecognitionOutput(intent_type="qa", extracted_request_patch={"destination": "上海"})
    guarded = recognizer._guard_qa_patch(inp, out)
    assert guarded.extracted_request_patch == {}


def test_qa_mention_destination_then_plan_no_reask(monkeypatch):
    """端到端：闲聊透露目的地（判 qa）→ 再要 3 天行程，不应再追问目的地"""
    from app.agent.domain.session.pipeline import intent_session_pipeline
    from app.agent.domain.session.schema import SessionState

    responses = iter(
        [
            IntentRecognitionOutput(intent_type="qa", extracted_request_patch={"destination": "阳江"}),
            IntentRecognitionOutput(intent_type="new_plan", extracted_request_patch={"days": 3}),
        ]
    )
    monkeypatch.setattr(IntentRecognizer, "_recognize_with_llm", lambda self, intent_input: next(responses))

    state = SessionState(session_id="s1")

    r1 = intent_session_pipeline.run(session_state=state, request_id="r1", raw_message="我想去阳江，有什么好玩的地方吗")
    assert r1.session_state.current_request_state.destination == "阳江"

    r2 = intent_session_pipeline.run(session_state=r1.session_state, request_id="r2", raw_message="给我一个三天的旅行呗")
    assert r2.session_state.current_request_state.destination == "阳江"
    assert r2.session_state.current_request_state.days == 3
    assert "destination" not in r2.merge_result.remaining_missing_fields
    assert r2.session_state.conversation_stage == "ready_to_plan"
