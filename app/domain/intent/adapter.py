from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.domain.intent.schema import IntentPlanningRequest, IntentRecognitionInput

if TYPE_CHECKING:
    # 仅供类型标注，运行时 intent 层不依赖 session 层
    from app.domain.session.schema import SessionIntentView


def adapt_session_view_to_intent_input(
    *,
    request_id: str,
    raw_message: str,
    session_view: SessionIntentView,
    session_id: str | None = None,
    user_id: str | None = None,
    user_context: dict[str, Any] | None = None,
) -> IntentRecognitionInput:
    """把 session 投影视图转换成 intent 输入对象（层间解耦）"""
    # 重建强类型 planning_request，字段漂移在此立即报错（extra="forbid"）
    planning_request_payload = dict(session_view.planning_request or {})
    planning_request = IntentPlanningRequest(**planning_request_payload) if planning_request_payload else None

    return IntentRecognitionInput(
        request_id=request_id,
        session_id=session_id,
        user_id=user_id,
        raw_message=raw_message,
        planning_request=planning_request,
        session_context=session_view.session_context,
        user_context=dict(user_context or {}),
        latest_plan_summary=session_view.artifacts.plan_summary,
        recent_messages=list(session_view.recent_messages),
        pending_questions=list(session_view.pending_questions),
    )
