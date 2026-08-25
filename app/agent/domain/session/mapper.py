from __future__ import annotations

from datetime import date

from app.agent.domain.common.planning import PlanningRequest
from app.agent.domain.common.session_context import SessionContext, compute_confirmed_fields
from app.agent.domain.session.schema import SessionState


# missing_fields → 自然的中文追问
_FIELD_PROMPTS = {
    "destination": "想去哪个目的地呢",
    "start_date": "计划哪天出发呢",
    "end_date": "计划玩到哪天呢",
}


def _compute_days(start_date: str | None, end_date: str | None) -> int:
    """从 start/end_date 推算天数，解析失败回退 1 天"""
    if not start_date or not end_date:
        return 1
    try:
        s = date.fromisoformat(start_date)
        e = date.fromisoformat(end_date)
        return max(1, (e - s).days + 1)
    except ValueError:
        return 1


def session_state_to_planning_request(state: SessionState) -> PlanningRequest:
    """SessionState 累计需求 → PlanningRequest（days 缺省从日期推算；travelers 转 list；budget 恒 None）"""
    req = state.current_request_state
    days = req.days if req.days else _compute_days(req.start_date, req.end_date)
    travelers: list[str] = [f"{req.travelers} 位成人"] if req.travelers else []
    return PlanningRequest(
        destination=req.destination or "",
        days=days,
        budget=None,
        start_date=req.start_date,
        end_date=req.end_date,
        departure_city=req.departure_city,
        travelers=travelers,
        preferences=list(req.preferences),
        must_visit_spots=list(req.must_visit_spots),
        optional_spots=list(req.optional_spots),
        avoid_spots=list(req.avoid_spots),
    )


def session_state_to_session_context(state: SessionState) -> SessionContext:
    """SessionState → SessionContext（planning_agent 内部仍读 SessionContext，这里做桥接）"""
    req = state.current_request_state
    confirmed = compute_confirmed_fields(req)
    return SessionContext(
        session_id=state.session_id,
        confirmed_fields=confirmed,
        pending_questions=list(state.pending_questions),
        conversation_stage=state.conversation_stage,
        last_destination=req.destination,
        revision_count=state.revision_count,
    )


def build_follow_up_question(pending_questions: list[str]) -> str:
    if not pending_questions:
        return "再告诉我一些想法，我就能开始规划啦～"
    prompts = [_FIELD_PROMPTS.get(f, f) for f in pending_questions]
    if len(prompts) == 1:
        return "还差最后一步：" + prompts[0] + "？"
    return "还差几个信息：" + "、".join(prompts) + "？"


def stage_to_execution_mode(stage: str) -> str:
    """conversation_stage → ExecutionPlan.mode"""
    if stage in {"collecting_destination", "collecting_dates", "collecting_requirements"}:
        return "clarify"
    if stage == "ready_to_plan":
        return "planning"
    if stage in {"revise_collecting", "revise_ready"}:
        return "revise"
    if stage in {"qa", "completed", "closed"}:
        return "qa"
    return "clarify"


def build_plan_summary(plan: dict) -> dict:
    """从 TripPlan 生成轻量摘要（只保留判断 new_plan vs revise_plan 的最少信息，避免灌给 LLM）"""
    daily_plan = plan.get("daily_plan") or []
    return {
        "destination": plan.get("destination"),
        "days": len(daily_plan),
        "route_intent_summary": plan.get("route_intent_summary"),
        "summary": plan.get("summary"),
        "daily_areas": [
            {"day_index": day.get("day_index"), "primary_area": day.get("primary_area")}
            for day in daily_plan
            if isinstance(day, dict)
        ],
        "stay": plan.get("stay_recommendation") or [],
    }
