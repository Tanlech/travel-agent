from __future__ import annotations

from app.agents.schema.planning import PlanningRequest
from app.domain.context.planning import PlanningContext
from app.domain.context.session import SessionContext
from app.domain.memory.manager import memory_manager

"""上下文工厂：统一构造 PlanningContext（内存态只读，不含会话状态写回）"""

def _session_context_from_request(request: PlanningRequest, session_id: str | None) -> SessionContext:
    """从 PlanningRequest 推导最小 SessionContext（planning_agent 内部兼容用）
    会话状态已由 SessionState（Redis）统一持久化；这里只构造 planning_agent 需要的
    只读会话视图，不再读写内存态会话记忆，避免与新会话层双写分叉。
    """
    confirmed: list[str] = []
    if request.destination:
        confirmed.append("destination")
    if request.start_date:
        confirmed.append("start_date")
    if request.end_date:
        confirmed.append("end_date")
    return SessionContext(
        session_id=session_id,
        confirmed_fields=confirmed,
        pending_questions=[],
        conversation_stage="collecting_destination",
        last_destination=request.destination,
        revision_count=0,
    )


class ContextBuilder:
    """规划上下文工厂（无状态单例）"""

    def build_planning_context(self, request: PlanningRequest, *, user_id: str | None = None, session_id: str | None = None) -> PlanningContext:
        """组装 PlanningContext：用户偏好融合记忆 + 会话视图推导"""
        return PlanningContext(
            request=request,
            user=memory_manager.build_user_context(request, user_id=user_id),
            session=_session_context_from_request(request, session_id),
            status="planning",
        )


context_builder = ContextBuilder()
