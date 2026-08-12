from __future__ import annotations

from typing import Any

from app.domain.session.schema import SessionIntentResult, SessionIntentView, SessionState


# 边界层 adapter：把外部 intent 结果转换成 session 自己可消费的 contract
# 这样 session service 不需要知道 intent 模块的具体 schema 实现

def adapt_intent_result_to_session(payload: Any) -> SessionIntentResult:
    if isinstance(payload, SessionIntentResult):
        return payload
    if hasattr(payload, "model_dump"):
        # mode="json"：把 StrEnum（intent 层）等内部类型转成纯 JSON 值，
        # 避免枚举对象传入 session 的 Literal 字段产生兼容问题
        return SessionIntentResult(**payload.model_dump(mode="json"))
    if isinstance(payload, dict):
        return SessionIntentResult(**payload)
    raise TypeError("Unsupported intent result payload for session adaptation.")


# 边界层 adapter：把 session state 投影成 intent 可消费的上下文视图
# 这样 session service 只维护内部状态，不直接关心对外视图拼装细节

def adapt_session_state_to_intent_view(session_state: SessionState) -> SessionIntentView:
    # 投影规则：只暴露 intent 需要的字段，隐藏 session 内部实现细节
    # - planning_request: 当前累计需求（让 LLM 知道已有哪些字段，避免重复提取）
    # - session_context: 阶段+改稿次数（让 LLM 判断是新规划还是改稿场景）
    # - artifacts: 产物摘要（让 LLM 判断是否已有可修改的行程）
    # - recent_messages: 近期对话（用于上下文延续，如"上面那个"的指代消解）
    # - pending_questions: 上轮待补字段（让 LLM 知道刚才在追问什么）
    return SessionIntentView(
        planning_request=session_state.current_request_state.model_dump(),
        session_context={
            "conversation_stage": session_state.conversation_stage,
            "revision_count": session_state.revision_count,
        },
        artifacts=session_state.artifacts.model_copy(deep=True),
        recent_messages=list(session_state.recent_messages),
        pending_questions=list(session_state.pending_questions),
    )
