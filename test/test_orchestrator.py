"""orchestrator 编排层核心行为测试。

聚焦四个分支路由、成功/失败统一收口（_finalize_success / _fail_branch /
_notify_and_respond）、请求级幂等缓存与并发冲突重试路径；
通过 monkeypatch 隔离 Redis / LLM / 子 agent / memory，不依赖外部服务。
"""

import pytest

from app.agent.agents.orchestrator import TravelOrchestrator, _BranchCtx
from app.agent.agents.schema.orchestrator import AgentRequest, AgentResponse
from app.agent.agents.schema.planning import TripPlan
from app.agent.domain.common.itinerary import ItineraryDraftSchema
from app.agent.domain.common.user import UserContext
from app.agent.domain.session.schema import SessionIntentResult
from app.agent.domain.session.service import session_state_service
from app.observability.token_budget import TokenBudgetTracker, default_budget_policy


class _DisabledLLM:
    def is_enabled(self):
        return False


@pytest.fixture(autouse=True)
def _isolate_external(monkeypatch):
    """统一隔离外部依赖：Redis、LLM、memory、幂等缓存。各测试按需覆写。"""
    monkeypatch.setattr("app.agent.agents.orchestrator.get_llm_client", lambda: _DisabledLLM())
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.load", lambda sid: None)
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.save", lambda st: st)
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.save_with_artifacts", lambda st, p, d: st)
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.load_artifacts", lambda sid: (None, None))
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.load_idempotent_response", lambda sid, rid: None)
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.save_idempotent_response", lambda *a, **k: None)
    monkeypatch.setattr("app.agent.agents.orchestrator.memory_manager.build_user_context", lambda req, user_id=None: UserContext())
    monkeypatch.setattr("app.agent.agents.orchestrator.memory_manager.load_trip_history", lambda user_id: [])
    monkeypatch.setattr("app.agent.agents.orchestrator.memory_manager.persist_user_memory", lambda *a, **k: None)
    monkeypatch.setattr("app.agent.agents.orchestrator.memory_manager.persist_trip_memory", lambda *a, **k: None)


def _make_ctx(*, stage="qa", intent_type="qa", message="你好", pending=None, patch=None,
              has_plan=False, mode="qa", prefetched=None):
    st = session_state_service.initialize("s1", "u1")
    st.conversation_stage = stage
    if pending:
        st.pending_questions = list(pending)
    if has_plan:
        st.artifacts.has_plan = True
        st.artifacts.plan_summary = {"destination": "北京", "days": 2}
    intent = SessionIntentResult(intent_type=intent_type, extracted_request_patch=patch or {})
    req = AgentRequest(request_id="r1", user_id="u1", session_id="s1", message=message)
    return _BranchCtx(
        mode=mode,
        agent_request=req,
        session_state=st,
        intent=intent,
        user_context={},
        trip_history=[],
        prefetched_artifacts=prefetched,
        trace_id="t1",
        budget_tracker=TokenBudgetTracker(default_budget_policy()),
        session_id="s1",
    )


@pytest.fixture
def orch():
    return TravelOrchestrator()


# ---- 分支一：clarify ----

def test_clarify_returns_follow_up(orch):
    ctx = _make_ctx(stage="collecting_destination", intent_type="clarification",
                    pending=["destination"], mode="clarify")
    r = orch._handle_clarify(ctx)
    assert r.status == "needs_follow_up"
    assert r.mode == "clarify"
    assert r.follow_up_question and "想去哪个目的地呢" in r.follow_up_question
    # 追问已落 recent_messages
    assert ctx.session_state.recent_messages[-1].content == r.follow_up_question


# ---- 分支四：qa ----

def test_closed_session_intercepts(monkeypatch, orch):
    # closed 终态：任何消息被拦截为"会话已结束"，不再落入 qa 闲聊分支
    st = session_state_service.initialize("s1", "u1")
    st.conversation_stage = "closed"
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.load", lambda sid: st)
    r = orch.handle(AgentRequest(request_id="r1", user_id="u1", session_id="s1", message="你好"))
    assert r.mode == "closed"
    assert r.status == "completed"
    assert "已结束" in r.summary
    assert r.debug.get("error") == "session_closed"


def test_qa_confirm_uses_static_reply(orch):
    r = orch._handle_qa(_make_ctx(intent_type="confirm", mode="qa"))
    assert r.status == "completed" and r.summary == "好的，行程已确认。"


def test_qa_reject_uses_static_reply(orch):
    r = orch._handle_qa(_make_ctx(intent_type="reject", mode="qa"))
    assert r.status == "completed" and "不规划了" in r.summary


def test_qa_unknown_falls_back_when_llm_disabled(orch):
    r = orch._handle_qa(_make_ctx(intent_type="unknown", mode="qa"))
    assert r.status == "completed" and "旅行规划助手" in r.summary


# ---- 分支三：revise 守卫 ----

def test_revise_without_artifacts_fails(orch):
    r = orch._handle_revise(_make_ctx(stage="revise_ready", intent_type="revise_plan",
                                      message="换住宿", mode="revise"))
    assert r.status == "failed" and "还没有可修改" in r.summary


def test_revise_without_plan_falls_back_to_planning(monkeypatch, orch):
    # 没有可改的行程但字段已齐（至少目的地）→ 自动降级为规划，而不是卡在"还没有行程"
    captured = {}

    def _fake_run_pipeline(plan_input):
        captured["request"] = plan_input.request
        return {
            "plan": TripPlan(destination="北京", days=2, summary="已为你生成北京2日行程。"),
            "final_draft": None,
            "final_decision": {"summary": "已为你生成北京2日行程。"},
        }

    monkeypatch.setattr("app.agent.agents.orchestrator.planning_agent.run_pipeline", _fake_run_pipeline)
    monkeypatch.setattr(orch, "_commit_artifacts_with_retry", lambda st, p, d, s: (True, st))

    st = session_state_service.initialize("s1", "u1")
    st.conversation_stage = "revise_ready"
    st.current_request_state.destination = "北京"
    st.current_request_state.days = 2
    ctx = _BranchCtx(
        mode="revise",
        agent_request=AgentRequest(request_id="r1", user_id="u1", session_id="s1", message="换成详细版"),
        session_state=st,
        intent=SessionIntentResult(intent_type="revise_plan", extracted_request_patch={}),
        user_context={},
        trip_history=[],
        prefetched_artifacts=None,
        trace_id="t1",
        budget_tracker=TokenBudgetTracker(default_budget_policy()),
        session_id="s1",
    )
    r = orch._handle_revise(ctx)
    assert r.status == "completed"
    assert r.mode == "planning"  # 降级后按规划模式返回，日志/指标能区分
    assert captured["request"].destination == "北京"
    assert ctx.session_state.conversation_stage == "completed"


def test_revise_expired_artifacts_clears_markers(orch):
    # has_plan 为真但产物已丢：提示过期并清空摘要标记，避免死循环
    ctx = _make_ctx(stage="revise_ready", intent_type="revise_plan", message="换住宿",
                    has_plan=True, mode="revise")
    r = orch._handle_revise(ctx)
    assert r.status == "failed" and "已过期" in r.summary
    assert ctx.session_state.artifacts.has_plan is False
    assert ctx.session_state.artifacts.plan_summary is None


def test_revise_empty_revision_asks_follow_up(orch):
    ctx = _make_ctx(stage="revise_ready", intent_type="revise_plan", message="改一下",
                    has_plan=True, mode="revise", prefetched=({"destination": "北京", "summary": "s"},
                                                               {"destination": "北京", "summary": "s"}))
    r = orch._handle_revise(ctx)
    assert r.status == "needs_follow_up" and r.follow_up_question


def test_revise_success_finalizes_and_persists_memory(monkeypatch, orch):
    persisted = {"user": [], "trip": []}
    monkeypatch.setattr("app.agent.agents.orchestrator.memory_manager.persist_user_memory",
                        lambda uid, uctx: persisted["user"].append(uctx))
    monkeypatch.setattr("app.agent.agents.orchestrator.memory_manager.persist_trip_memory",
                        lambda *a, **k: persisted["trip"].append(a))
    monkeypatch.setattr(orch, "_commit_artifacts_with_retry", lambda st, p, d, s: (True, st))

    class _FakeArtifacts:
        plan = TripPlan(destination="北京", summary="updated",
                        daily_plan=[{"day_index": 1, "items": [{"item_type": "attraction", "title": "故宫"}]}])
        draft = ItineraryDraftSchema(destination="北京", summary="updated")

    class _FakeResult:
        artifacts = _FakeArtifacts()
        summary = "已按你的要求更新行程。"
        revision_summary = "更新第二天"

    monkeypatch.setattr("app.agent.agents.orchestrator.revise_agent.run", lambda inp: _FakeResult())

    ctx = _make_ctx(stage="revise_ready", intent_type="revise_plan", message="把第二天改成室内的",
                    patch={"preferences": ["历史"]}, has_plan=True, mode="revise",
                    prefetched=({"destination": "北京", "summary": "s"}, {"destination": "北京", "summary": "s"}))
    r = orch._handle_revise(ctx)
    assert r.status == "completed"
    assert r.plan["destination"] == "北京"
    assert ctx.session_state.conversation_stage == "completed"
    # 偏好并入长期记忆 + 行程记忆都执行
    assert persisted["user"], "改稿带偏好应沉淀用户记忆"
    assert persisted["trip"], "改稿应沉淀行程记忆"


# ---- 分支二：planning ----

def test_planning_exception_fails(monkeypatch, orch):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.agent.agents.orchestrator.planning_agent.run_pipeline", _boom)
    r = orch._handle_planning(_make_ctx(stage="ready_to_plan", intent_type="new_plan",
                                        message="8月10号去北京", mode="planning"))
    assert r.status == "failed"
    assert r.debug.get("error") == "boom"


def test_planning_missing_plan_fails(monkeypatch, orch):
    monkeypatch.setattr("app.agent.agents.orchestrator.planning_agent.run_pipeline",
                        lambda *a, **k: {"plan": None, "final_draft": None, "final_decision": {}})
    r = orch._handle_planning(_make_ctx(stage="ready_to_plan", intent_type="new_plan",
                                        message="8月10号去北京", mode="planning"))
    assert r.status == "failed"
    assert r.debug.get("error") == "plan_missing_in_pipeline_result"


# ---- 成功/失败统一收口 ----

def test_finalize_success_commits_and_advances_stage(monkeypatch, orch):
    monkeypatch.setattr(orch, "_commit_artifacts_with_retry", lambda st, p, d, s: (True, st))
    ctx = _make_ctx(stage="ready_to_plan", intent_type="new_plan", message="8月10号去北京", mode="planning")
    r = orch._finalize_success(
        ctx,
        plan_payload={"destination": "北京", "daily_plan": [{"day_index": 1}]},
        draft_payload=None,
        summary="已为你生成旅行行程。",
        token_budget=5000,
        fail_message="保存失败",
    )
    assert r.status == "completed" and r.plan["destination"] == "北京"
    assert ctx.session_state.conversation_stage == "completed"
    assert ctx.session_state.artifacts.has_plan is True


def test_finalize_success_commit_exhausted_fails(monkeypatch, orch):
    monkeypatch.setattr(orch, "_commit_artifacts_with_retry", lambda st, p, d, s: (False, st))
    ctx = _make_ctx(stage="ready_to_plan", intent_type="new_plan", message="8月10号去北京", mode="planning")
    r = orch._finalize_success(
        ctx,
        plan_payload={"destination": "北京"},
        draft_payload=None,
        summary="已为你生成旅行行程。",
        token_budget=5000,
        fail_message="行程已生成但保存失败",
    )
    assert r.status == "failed"
    assert r.debug.get("error") == "commit_conflict_exhausted"
    assert r.summary == "行程已生成但保存失败"


def test_fail_branch_returns_failed_with_error(orch):
    r = orch._fail_branch(_make_ctx(mode="planning"), error="commit_conflict_exhausted", message="保存失败")
    assert r.status == "failed"
    assert r.debug.get("error") == "commit_conflict_exhausted"
    assert r.summary == "保存失败"


# ---- 状态机 ----

def test_advance_to_completed_moves_stage(orch):
    st = _make_ctx(stage="ready_to_plan").session_state
    assert orch._advance_to_completed(st).conversation_stage == "completed"


# ---- 改稿信号 ----

def test_has_revision_signal(orch):
    assert orch._has_revision_signal("把第二天改成室内的") is True
    assert orch._has_revision_signal("换住宿") is True
    assert orch._has_revision_signal("改一下") is False   # 空泛改稿
    assert orch._has_revision_signal("随便聊聊") is False


# ---- 请求级幂等缓存 ----

def _install_in_memory_idempotent_cache(monkeypatch):
    store = {}
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.save_idempotent_response",
                        lambda sid, rid, payload: store.__setitem__(rid, payload))
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.load_idempotent_response",
                        lambda sid, rid: store.get(rid))
    return store


def test_idempotent_cache_roundtrip(monkeypatch, orch):
    _install_in_memory_idempotent_cache(monkeypatch)
    resp = AgentResponse(request_id="r1", session_id="s1", status="completed", mode="qa", summary="好的，行程已确认。")
    orch._cache_completed_response("s1", "r1", "你好", resp)
    loaded = orch._load_cached_response("s1", "r1", "你好")
    assert loaded is not None and loaded.summary == "好的，行程已确认。"
    # 缓存绑定 message：同 request_id 不同消息不命中
    assert orch._load_cached_response("s1", "r1", "别的话") is None


def test_idempotent_only_caches_completed(monkeypatch, orch):
    calls = []
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.save_idempotent_response",
                        lambda *a, **k: calls.append(a))
    orch._cache_completed_response("s1", "r1", "msg",
                                   AgentResponse(request_id="r1", session_id="s1", status="needs_follow_up", mode="clarify"))
    assert calls == []


# ---- 意图识别快路径 ----

def test_resolve_intent_empty_message_unknown(monkeypatch, orch):
    saved = []
    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.save", lambda st: saved.append(st) or st)
    st, intent, _, _, _ = orch._resolve_intent(
        session_id="s1", user_id="u1", request_id="r1", raw_message="  ", trace_id="t1")
    assert intent.intent_type == "unknown"
    assert len(saved) == 1  # 空消息快路径也落一次会话


def test_resolve_intent_retries_on_save_conflict(monkeypatch, orch):
    saved = []

    def fake_save(st):
        saved.append(st)
        if len(saved) == 1:
            return None  # 首次写入冲突（其他请求已推进）
        return st.model_copy(update={"version": st.version + 1})

    monkeypatch.setattr("app.agent.agents.orchestrator.redis_session_repository.save", fake_save)
    st, intent, _, _, _ = orch._resolve_intent(
        session_id="s1", user_id="u1", request_id="r1", raw_message="你好", trace_id="t1")
    assert intent is not None
    assert len(saved) == 2  # 1 次冲突重试 + 1 次成功
    assert st.version == 1
