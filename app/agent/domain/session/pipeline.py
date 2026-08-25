from __future__ import annotations

from typing import Any

from app.agent.domain.intent.adapter import adapt_session_view_to_intent_input
from app.agent.domain.intent.schema import IntentRecognitionInput, IntentRecognitionOutput
from app.agent.domain.intent.service import IntentRecognizer, intent_recognizer
from app.agent.domain.common.intent_type import IntentType
from app.agent.domain.common.session_context import SessionContextView
from app.agent.domain.session.schema import SessionApplyIntentResult, SessionIntentResult, SessionIntentView, SessionState
from app.agent.domain.session.service import SessionStateService, session_state_service


def adapt_intent_result_to_session(payload: Any) -> SessionIntentResult:
    """把 intent 输出转成 session 层可消费的契约（mode="json" 把 StrEnum 等转成纯 JSON 值）"""
    if isinstance(payload, SessionIntentResult):
        return payload
    if hasattr(payload, "model_dump"):
        return SessionIntentResult(**payload.model_dump(mode="json"))
    if isinstance(payload, dict):
        return SessionIntentResult(**payload)
    raise TypeError("Unsupported intent result payload for session adaptation.")


def adapt_session_state_to_intent_view(session_state: SessionState) -> SessionIntentView:
    """把 SessionState 投影成 intent 可消费的轻量视图，隐藏 session 内部细节"""
    return SessionIntentView(
        planning_request=session_state.current_request_state.model_dump(),
        session_context=SessionContextView(
            conversation_stage=session_state.conversation_stage,
            revision_count=session_state.revision_count,
        ),
        artifacts=session_state.artifacts.model_copy(deep=True),
        recent_messages=list(session_state.recent_messages),
        pending_questions=list(session_state.pending_questions),
    )


class IntentSessionPipeline:
    # 串联链路：session state -> view -> intent input -> result -> session intent result -> apply
    # 分工：intent 层只做"理解"（判意图+提取 patch）；session 层只做"累积"（merge+状态机），不调 LLM
    def __init__(
        self,
        *,
        intent_service: IntentRecognizer | None = None,
        session_service: SessionStateService | None = None,
    ) -> None:
        # 默认全局单例，可注入替换（便于 mock）
        self.intent_service = intent_service or intent_recognizer
        self.session_service = session_service or session_state_service

    def run(
        self,
        *,
        session_state: SessionState,
        request_id: str,
        raw_message: str,
        user_context: dict[str, Any] | None = None,
        trip_history: list[dict[str, Any]] | None = None,
    ) -> SessionApplyIntentResult:
        # 空输入防护：不调 LLM，直接 unknown 兜底
        if not (raw_message or "").strip():
            intent_output = IntentRecognitionOutput(intent_type=IntentType.UNKNOWN, reasoning="空输入。")
            return self.session_service.apply_intent_result(
                session_state,
                adapt_intent_result_to_session(intent_output),
            )

        # 1. session 完整状态 → intent 轻量视图
        session_view = adapt_session_state_to_intent_view(session_state)

        # 2. 视图 + 原话 + 用户偏好 → intent 输入
        intent_input = adapt_session_view_to_intent_input(
            request_id=request_id,
            session_id=session_state.session_id,
            user_id=session_state.user_id,
            raw_message=raw_message,
            session_view=session_view,
            user_context=user_context,
            trip_history=trip_history,
        )

        # 3. 意图识别（LLM 优先，失败走 fallback）→ intent_type + 本轮 patch
        intent_output = self.intent_service.recognize(intent_input)

        # 4. intent 输出 → session 契约
        session_intent_result = adapt_intent_result_to_session(intent_output)

        # 5. 应用：merge patch 进累计需求 + 状态机推进
        return self.session_service.apply_intent_result(session_state, session_intent_result)

    # ---- 以下供测试/诊断：链路可拆段单独调用 ----

    def build_intent_input(
        self,
        *,
        session_state: SessionState,
        request_id: str,
        raw_message: str,
        user_context: dict[str, Any] | None = None,
        trip_history: list[dict[str, Any]] | None = None,
    ) -> IntentRecognitionInput:
        # 只到步骤2，返回 intent 输入
        session_view = adapt_session_state_to_intent_view(session_state)
        return adapt_session_view_to_intent_input(
            request_id=request_id,
            session_id=session_state.session_id,
            user_id=session_state.user_id,
            raw_message=raw_message,
            session_view=session_view,
            user_context=user_context,
            trip_history=trip_history,
        )

    def recognize_only(
        self,
        *,
        session_state: SessionState,
        request_id: str,
        raw_message: str,
        user_context: dict[str, Any] | None = None,
        trip_history: list[dict[str, Any]] | None = None,
    ) -> IntentRecognitionOutput:
        # 只识别不应用
        intent_input = self.build_intent_input(
            session_state=session_state,
            request_id=request_id,
            raw_message=raw_message,
            user_context=user_context,
            trip_history=trip_history,
        )
        return self.intent_service.recognize(intent_input)

    def adapt_output_only(self, intent_output: IntentRecognitionOutput | dict[str, Any]) -> SessionIntentResult:
        # 只做 schema 转换，不应用
        return adapt_intent_result_to_session(intent_output)


intent_session_pipeline = IntentSessionPipeline()
