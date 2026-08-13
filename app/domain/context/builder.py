from __future__ import annotations

from app.agents.schema.planning import PlanningRequest
from app.domain.context.planning import PlanningContext
from app.domain.context.response import ResponseContext
from app.domain.context.session import SessionContext
from app.domain.memory.manager import memory_manager


def _session_context_from_request(request: PlanningRequest, session_id: str | None) -> SessionContext:
    """从 PlanningRequest 推导最小 SessionContext（planning_agent 内部兼容用）。

    会话状态已由 SessionState（Redis）统一持久化；这里只构造 planning_agent 需要的
    只读会话视图，不再读写内存态会话记忆，避免与新会话层双写分叉。
    """
    confirmed: list[str] = []
    if request.destination:
        confirmed.append("destination")
    if request.days:
        confirmed.append("days")
    if request.budget is not None:
        confirmed.append("budget")
    return SessionContext(
        session_id=session_id,
        confirmed_fields=confirmed,
        pending_questions=[],
        conversation_stage="collecting_destination",
        last_destination=request.destination,
        revision_count=0,
    )


class ContextBuilder:
    def build_planning_context(self, request: PlanningRequest, *, user_id: str | None = None, session_id: str | None = None) -> PlanningContext:
        return PlanningContext(
            request=request,
            user=memory_manager.build_user_context(request, user_id=user_id),
            session=_session_context_from_request(request, session_id),
            status="planning",
        )

    def build_response_context(self, *, needs_follow_up: bool = False, include_alternatives: bool = True) -> ResponseContext:
        return ResponseContext(
            response_mode="follow_up" if needs_follow_up else "final_plan",
            include_alternatives=include_alternatives,
            needs_follow_up=needs_follow_up,
        )


context_builder = ContextBuilder()
