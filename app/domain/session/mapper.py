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
    """从 start_date/end_date 推算天数；解析失败回退 1 天。"""
    if not start_date or not end_date:
        return 1
    try:
        s = date.fromisoformat(start_date)
        e = date.fromisoformat(end_date)
        return max(1, (e - s).days + 1)
    except ValueError:
        return 1


def session_state_to_planning_request(state: SessionState) -> PlanningRequest:
    """把 SessionState 的累计需求映射成 planning_agent 可消费的 PlanningRequest。

    - days 优先取显式值，否则从 start/end_date 推算
    - travelers 在 SessionRequestState 是 int，PlanningRequest 是 list[str]，做转换
    - budget 不在对话层强制（与 intent prompt 对齐），传 None
    """
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
    """把 SessionState 投影成系统 A 的 SessionContext，供 PlanningContext.session 使用。

    planning_agent 内部仍读 state.session（SessionContext），这里做桥接，planning_agent 不动。
    """
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
    """显式映射 SessionContext → ReviseSessionContext，避免 schema 演进时 ** 展开静默出错。"""
    return ReviseSessionContext(
        session_id=ctx.session_id,
        confirmed_fields=list(ctx.confirmed_fields),
        pending_questions=list(ctx.pending_questions),
        conversation_stage=ctx.conversation_stage,
        last_destination=ctx.last_destination,
        revision_count=ctx.revision_count,
    )


def user_context_to_revise_user_context(user_context: dict) -> ReviseUserContext:
    """UserContext(dict) → ReviseUserContext，含 pace 值映射。

    UserContext.pace_preference 用 relaxed/dense，
    ReviseUserContext.pace_preference 用 slow/balanced/fast，需做转换。
    """
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
    """把 SessionState 的 conversation_stage 映射成 ExecutionPlan.mode。"""
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
    """从 TripPlan payload 生成轻量摘要，供意图识别判断"是否已有行程"。

    只保留判断 new_plan vs revise_plan 所需的最少信息（目的地、天数、路线意图、
    每日区域、住宿），避免把完整行程灌给 LLM。plan 可以是 dict 或 TripPlan.model_dump()。
    """
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
