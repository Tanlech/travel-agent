from __future__ import annotations

from datetime import date

from app.agents.schema.planning import PlanningRequest
from app.agents.schema.revise import ReviseSessionContext, ReviseUserContext
from app.domain.context.session import SessionContext
from app.domain.session.schema import SessionState


# missing_fields → 中文追问文案
_FIELD_LABELS = {
    "destination": "目的地",
    "start_date": "游玩开始日期",
    "end_date": "游玩结束日期",
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
    travelers: list[str] = [f"{req.travelers} adults"] if req.travelers else []
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
    confirmed: list[str] = []
    if req.destination:
        confirmed.append("destination")
    if req.start_date:
        confirmed.append("start_date")
    if req.end_date:
        confirmed.append("end_date")
    return SessionContext(
        session_id=state.session_id,
        confirmed_fields=confirmed,
        pending_questions=list(state.pending_questions),
        conversation_stage=state.conversation_stage,
        last_destination=req.destination,
        revision_count=state.revision_count,
    )


def session_context_to_revise_session_context(ctx: SessionContext) -> ReviseSessionContext:
    """显式映射 SessionContext → ReviseSessionContext（防 ** 展开静默出错）"""
    return ReviseSessionContext(
        session_id=ctx.session_id,
        confirmed_fields=list(ctx.confirmed_fields),
        pending_questions=list(ctx.pending_questions),
        conversation_stage=ctx.conversation_stage,
        last_destination=ctx.last_destination,
        revision_count=ctx.revision_count,
    )


def user_context_to_revise_user_context(user_context: dict) -> ReviseUserContext:
    """UserContext(dict) → ReviseUserContext（pace: relaxed/dense → slow/fast）"""
    pace_map = {"relaxed": "slow", "dense": "fast"}
    raw_pace = user_context.get("pace_preference")
    revise_pace = pace_map.get(raw_pace) if raw_pace else None
    return ReviseUserContext(
        preferred_styles=list(user_context.get("preferred_styles") or []),
        disliked_styles=list(user_context.get("disliked_styles") or []),
        accept_theme_park=user_context.get("accept_theme_park"),
        accept_nightlife=user_context.get("accept_nightlife"),
        pace_preference=revise_pace,
        family_friendly=user_context.get("family_friendly"),
        senior_friendly=user_context.get("senior_friendly"),
    )


def build_follow_up_question(pending_questions: list[str]) -> str:
    if not pending_questions:
        return "请补充更多旅行信息后我再开始规划。"
    labels = [_FIELD_LABELS.get(f, f) for f in pending_questions]
    return "请补充以下信息：" + "、".join(labels)


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
