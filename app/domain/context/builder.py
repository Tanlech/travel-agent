from __future__ import annotations

from app.agents.schema.planning import PlanningRequest
from app.domain.context.planning import PlanningContext
from app.domain.context.response import ResponseContext
from app.domain.memory.manager import memory_manager


class ContextBuilder:
    def build_planning_context(self, request: PlanningRequest, *, user_id: str | None = None, session_id: str | None = None) -> PlanningContext:
        return PlanningContext(
            request=request,
            user=memory_manager.build_user_context(request, user_id=user_id),
            session=memory_manager.build_session_context(request, session_id=session_id),
            status="planning",
        )

    def build_response_context(self, *, needs_follow_up: bool = False, include_alternatives: bool = True) -> ResponseContext:
        return ResponseContext(
            response_mode="follow_up" if needs_follow_up else "final_plan",
            include_alternatives=include_alternatives,
            needs_follow_up=needs_follow_up,
        )


context_builder = ContextBuilder()
