from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.domain.intent.schema import IntentPlanningRequest, IntentRecognitionInput

if TYPE_CHECKING:
    # 仅供类型标注，运行时 intent 层不依赖 session 层（最大隔离）
    from app.domain.session.schema import SessionIntentView


# 边界层 adapter：把 session 层投影出来的视图，转换成 intent 层真正需要的输入对象
# 这样 intent service 不需要知道 session 模块内部状态结构

def adapt_session_view_to_intent_input(
    *,
    request_id: str,
    raw_message: str,
    session_view: SessionIntentView,
    session_id: str | None = None,
    user_id: str | None = None,
    user_context: dict[str, Any] | None = None,
) -> IntentRecognitionInput:
    """把 session 投影视图 → intent 输入对象（intent 与 session 解耦的关键）。

    转换要点：
    1. planning_request 视图是 dict，这里重建为强类型 IntentPlanningRequest，
       顺带做字段校验（视图里多余/类型不符的字段会被拦截报错，早发现早暴露）。
    2. latest_plan_summary 是意图识别判断"是否已有行程"的核心依据
       （new_plan vs revise_plan 的分水岭），从 artifacts 直接透传。
    3. recent_messages/pending_questions 透传给 LLM，用于理解上下文延续
       （如"上面那个"的指代消解）与回应上一轮追问。
    """
    # 把 session 投影视图里的 dict 形态 planning_request，重新构造成强类型对象
    # 这一步会做字段校验：视图里多余字段（extra="forbid"）或类型不符会直接抛 ValidationError，
    # 让 session/intent 字段漂移在入口即暴露，而不是静默丢失
    planning_request_payload = dict(session_view.planning_request or {})
    planning_request = IntentPlanningRequest(**planning_request_payload) if planning_request_payload else None

    # 拼装最终给 LLM 看的输入：
    # - raw_message 是本轮原话（意图判断的主要依据）
    # - planning_request 让 LLM 知道"已有哪些字段"，从而只提取本轮新增的 patch
    # - latest_plan_summary 让 LLM 判断"是否已有行程"，决定 new_plan 还是 revise_plan
    return IntentRecognitionInput(
        request_id=request_id,
        session_id=session_id,
        user_id=user_id,
        raw_message=raw_message,
        planning_request=planning_request,
        session_context=dict(session_view.session_context or {}),
        user_context=dict(user_context or {}),
        latest_plan_summary=session_view.artifacts.plan_summary,
        recent_messages=list(session_view.recent_messages),
        pending_questions=list(session_view.pending_questions),
    )
