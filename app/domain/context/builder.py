from __future__ import annotations

from app.domain.common.planning import PlanningRequest
from app.domain.common.session_context import SessionContext, compute_confirmed_fields
from app.domain.context.planning import PlanningContext
from app.domain.memory.manager import memory_manager

"""上下文工厂：统一构造 PlanningContext（内存态只读，不含会话状态写回）"""

def _session_context_from_request(request: PlanningRequest, session_id: str | None) -> SessionContext:
    """从 PlanningRequest 推导最小 SessionContext（planning_agent 内部兼容用）
    会话状态已由 SessionState（Redis）统一持久化；这里只构造 planning_agent 需要的
    只读会话视图，不再读写内存态会话记忆，避免与新会话层双写分叉。
    """
    confirmed = compute_confirmed_fields(request)
    return SessionContext(
        session_id=session_id,
        confirmed_fields=confirmed,
        pending_questions=[],
        # planning_agent 仅在字段齐全（ready_to_plan）后进入，这里固定为真实阶段而非 collecting
        conversation_stage="ready_to_plan",
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
